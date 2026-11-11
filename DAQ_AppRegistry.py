"""
DAQ_AppRegistry.py
------------------
Single source of truth for the Apps bar.

There are two kinds of app:

Built-in apps
  Bundled into the executable. Adding one means:
    1. Write the app module, giving it a module-level APP_SPEC dict.
    2. Add its module name to APP_MODULES below.
    3. Add a --hidden-import line for it to Wrap_Executable.bat (the build's
       drift check refuses to build until you do).
  Changing a built-in app means rebuilding the executable.

Plugin apps
  Live in the Plugins folder next to the program -- beside the .exe when
  built (Wrap_Executable.bat copies the project's Plugins folder there), or
  beside this file when run from source. Every subfolder is listed in
  Settings -> App Settings -> App Bar, but a plugin is loaded only once it is
  ticked Enable there: listing runs none of its code, so a folder dropped in
  does nothing until someone chooses to run it. The enabled folder names are
  stored in the "Plugin" / "Enabled" columns of the settings workbook's
  "app_bar" sheet. Adding, changing or enabling a plugin needs no rebuild,
  only a restart.

  Layout -- one subfolder per app, its entry file named after the folder:

      Plugins/
          DAQ_FlowFailure/
              DAQ_FlowFailure.py     must define APP_SPEC
              helpers.py             optional; import with `from . import helpers`

  Loose files directly in Plugins are ignored. A subfolder without its entry
  file is listed as not loadable.

  Each folder is loaded as its own package under a unique internal name, so
  two plugins can both have a helpers.py, and a copy of an app
  (DAQ_FlowFailure2/DAQ_FlowFailure2.py) can be loaded alongside the
  original. An __init__.py in the folder is not used.

  A plugin can import anything bundled into the executable -- the DAQ_*
  support modules (DAQ_AppBase, DAQ_Settings, ...) and the libraries listed in
  the "plugin SDK" block of Wrap_Executable.bat. Needing anything else, or a
  change to a bundled module, means rebuilding the executable.

Shortcuts
  Not apps: a named button that starts an outside program (any .exe, or
  anything else Windows can open). Added from Settings -> App Settings ->
  App Bar -> "Add Shortcut…" and stored in the settings workbook's
  "app_shortcuts" sheet (Name, Path, Show). The main app knows nothing about
  what the program does, so a shortcut takes part in no locking or gating.
  Read fresh each time the Apps bar is built, so a saved change shows
  straight away -- no restart.

Nothing in Multichannel_DAQ.py or DAQ_Settings.py names an individual app. In
particular, apps never refer to each other: mutual lockout is derived from the
`claims` sets, so app N does not have to be added to the lock list of apps
1..N-1.

APP_SPEC fields
---------------
id                    str   stable key; used in the settings workbook's
                            "app_bar" sheet, so DO NOT rename it once shipped.
                            Must be unique across built-in and plugin apps --
                            give a copied plugin a new id.
label                 str   button text
cls                   str   dialog class name inside the module
color, hover          str   button colours (optional)
show_by_default       bool  ticked in Settings -> Apps bar on a fresh install
                            (for a plugin: when it is added)
requires_daq_hw       bool  greyed out when no DAQ device is present
requires_daq_stopped  bool  greyed out while acquisition is running
blocks_calibration    bool  disables Calibrate inside CalibrationWindow
blocks_digital_output bool  disables the manual On/Off toggles on digital
                            output rows inside CalibrationWindow. Defaults to
                            blocks_calibration, since an app that owns the
                            channels well enough to block calibration also owns
                            them well enough that a second writer would clash.
                            Set it explicitly to override.
locks_run_stop        bool  disables the main Run/Stop while this app is open
owns_recording_length bool  the app decides when a recording stops (e.g. a
                            stopping event), so the main Auto Stop is unticked
                            and greyed while it is open
claims                set   exclusive resources; any two apps sharing a claim
                            cannot be open at once. 'ao' means the app drives
                            analog output.
build                 callable(ctx, parent) -> QDialog, defined in the module.
                            Kept there because the dialog signatures are not
                            uniform, and the module that owns a signature is
                            the right place to know it.

Set by the registry, not by the app:
plugin_path           str or None   the plugin's folder; None for built-ins
plugin_name           str           the plugin's folder name (plugins only)
"""

import hashlib
import importlib
import importlib.machinery
import importlib.util
import os
import re
import sys
import traceback


# Explicit, not a directory glob: PyInstaller bundles these files into the
# executable, so there is nothing on disk to glob at runtime. Comment a line
# out to disable an app.
APP_MODULES = [
    'DAQ_StaticLines',
    'DAQ_FunctionGenerator',
    'DAQ_GaugeAdder',
    'DAQ_CustomTriggers',
]

# Where the enabled plugins are recorded in the settings workbook.
APP_BAR_SHEET  = 'app_bar'
PLUGIN_COLUMN  = 'Plugin'     # the plugin's folder name; blank for built-ins
ENABLED_COLUMN = 'Enabled'

PLUGINS_DIR_NAME = 'Plugins'

# Where shortcuts are listed in the settings workbook.
SHORTCUT_SHEET   = 'app_shortcuts'
SHORTCUT_COLUMNS = ['Name', 'Path', 'Show']

_REQUIRED = ('id', 'label', 'cls', 'build')

_specs = None
_failures = []
# folder name -> (spec or None, failure reason or None), for every plugin
# that was enabled when load_apps() ran.
_plugin_status = {}
_enabled_at_start = set()


class PluginError(Exception):
    """A plugin folder could not be loaded. The message says why."""


# ---------------------------------------------------------------------------
# Spec validation (shared by built-in and plugin apps)
# ---------------------------------------------------------------------------

def _defaults(spec, module):
    spec.setdefault('module', module.__name__)
    spec.setdefault('color', '')
    spec.setdefault('hover', '')
    spec.setdefault('show_by_default', True)
    spec.setdefault('requires_daq_hw', False)
    spec.setdefault('requires_daq_stopped', False)
    spec.setdefault('blocks_calibration', False)
    # An app that claims the digital bus, or that already blocks calibration,
    # blocks manual digital output by default — so this needed no edits to the
    # existing app modules.
    spec.setdefault('blocks_digital_output',
                    spec['blocks_calibration']
                    or bool(set(spec.get('claims', ())) & {'do', 'digital'}))
    spec.setdefault('locks_run_stop', False)
    spec.setdefault('owns_recording_length', False)
    spec.setdefault('claims', set())
    spec['claims'] = set(spec['claims'])
    return spec


def _spec_from_module(module):
    """The validated APP_SPEC of `module`. Raises PluginError if unusable."""
    spec = getattr(module, 'APP_SPEC', None)
    if not isinstance(spec, dict):
        raise PluginError("no module-level APP_SPEC dict")
    spec = _defaults(dict(spec), module)
    missing = [f for f in _REQUIRED if not spec.get(f)]
    if missing:
        raise PluginError(f"APP_SPEC missing {', '.join(missing)}")
    spec['_module_obj'] = module
    return spec


# ---------------------------------------------------------------------------
# Plugin folders
# ---------------------------------------------------------------------------

def plugin_key(folder):
    """Normalised form of a path, for comparing two paths."""
    return os.path.normcase(os.path.normpath(os.path.abspath(folder)))


def _plugin_package_name(folder):
    """Unique, stable internal package name for a plugin folder.

    Derived from the full path, so two folders with the same name (an app and
    its copy in another directory) never share a sys.modules entry, and loading
    the same folder twice in one session returns the module already loaded.
    """
    base = re.sub(r'\W', '_', os.path.basename(os.path.normpath(folder)))
    digest = hashlib.sha1(plugin_key(folder).encode('utf-8')).hexdigest()[:10]
    return f"_daq_plugin_{base}_{digest}"


def _import_plugin(folder):
    """Import a plugin folder's entry module and return it.

    The folder becomes a package (without needing an __init__.py) so the entry
    module can import its siblings relatively. Raises PluginError for a layout
    problem; any exception the plugin's own code raises propagates.
    """
    folder = os.path.abspath(folder)
    if not os.path.isdir(folder):
        raise PluginError(f"folder not found: {folder}")
    base = os.path.basename(os.path.normpath(folder))
    entry_path = os.path.join(folder, base + '.py')
    if not os.path.isfile(entry_path):
        raise PluginError(f"no {base}.py in {folder} — the entry file must be "
                          f"named after its folder")

    pkg_name   = _plugin_package_name(folder)
    entry_name = pkg_name + '.' + re.sub(r'\W', '_', base)
    if entry_name in sys.modules:
        return sys.modules[entry_name]

    pkg_spec = importlib.machinery.ModuleSpec(pkg_name, None, is_package=True)
    pkg_spec.submodule_search_locations = [folder]
    pkg = importlib.util.module_from_spec(pkg_spec)

    entry_spec = importlib.util.spec_from_file_location(entry_name, entry_path)
    module = importlib.util.module_from_spec(entry_spec)

    sys.modules[pkg_name]   = pkg
    sys.modules[entry_name] = module
    try:
        entry_spec.loader.exec_module(module)
    except BaseException:
        # Leave nothing half-imported behind, so a fixed plugin can be tried
        # again (Add App…) without a stale broken module being returned.
        for name in [n for n in sys.modules
                     if n == pkg_name or n.startswith(pkg_name + '.')]:
            del sys.modules[name]
        raise
    setattr(pkg, entry_name.rsplit('.', 1)[1], module)
    return module


def _load_plugin_spec(folder):
    """Import a plugin folder and return its validated spec.

    Raises PluginError for every failure, with the plugin's traceback folded
    into the message when its own code raised.
    """
    try:
        module = _import_plugin(folder)
    except PluginError:
        raise
    except Exception:
        raise PluginError(traceback.format_exc()) from None
    spec = _spec_from_module(module)
    spec['plugin_path'] = folder
    return spec


def _read_sheet(sheet, settings_path, what):
    """One sheet of the settings workbook as a DataFrame, or None.

    Forgiving: no workbook or no such sheet means None, because the callers
    run while the Apps bar is being built and an exception there would stop
    the program starting.
    """
    try:
        import pandas as pd
        if settings_path is None:
            from DAQ_Settings import _get_settings_path
            settings_path = _get_settings_path()
        if not os.path.isfile(settings_path):
            return None
        xls = pd.ExcelFile(settings_path)
        if sheet not in xls.sheet_names:
            return None
        return pd.read_excel(xls, sheet_name=sheet)
    except Exception as e:
        print(f"{what} unreadable ({e}); none loaded.")
        return None


def plugins_dir():
    """The Plugins folder: beside the .exe when built, beside this file when
    run from source."""
    if getattr(sys, 'frozen', False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, PLUGINS_DIR_NAME)


def discover_plugins():
    """[(folder name, folder path, has entry file)] for every subfolder of
    the Plugins folder, by name. Nothing is imported. Loose files, and
    folders Python or Jupyter make (__pycache__, .ipynb_checkpoints), are
    skipped."""
    root = plugins_dir()
    try:
        names = sorted(os.listdir(root), key=str.lower)
    except OSError:
        return []
    out = []
    for name in names:
        path = os.path.join(root, name)
        if not os.path.isdir(path) or name.startswith(('.', '_')):
            continue
        out.append((name, path, os.path.isfile(os.path.join(path, name + '.py'))))
    return out


def read_enabled_plugins(settings_path=None):
    """Folder names ticked Enable in the settings workbook."""
    df = _read_sheet(APP_BAR_SHEET, settings_path, "Plugin list")
    if df is None or not {PLUGIN_COLUMN, ENABLED_COLUMN} <= set(df.columns):
        return set()
    out = set()
    for _, row in df.iterrows():
        name = row.get(PLUGIN_COLUMN)
        if not (isinstance(name, str) and name.strip()):
            continue
        try:
            if int(row.get(ENABLED_COLUMN)):
                out.add(name.strip())
        except (TypeError, ValueError):
            pass
    return out


def read_shortcuts(settings_path=None):
    """Shortcuts listed in the settings workbook, in sheet order.

    [{'key', 'label', 'path', 'show'}]. 'key' identifies the shortcut by its
    program path, so it stays the same when the list is reordered. Rows
    without both a name and a path are skipped.
    """
    df = _read_sheet(SHORTCUT_SHEET, settings_path, "Shortcut list")
    if df is None or not {'Name', 'Path'} <= set(df.columns):
        return []
    out, seen = [], set()
    for _, row in df.iterrows():
        name, path = row.get('Name'), row.get('Path')
        if not (isinstance(name, str) and name.strip()
                and isinstance(path, str) and path.strip()):
            continue
        key = 'shortcut:' + plugin_key(path.strip())
        if key in seen:
            continue
        seen.add(key)
        try:
            show = bool(int(row.get('Show', 1)))
        except (TypeError, ValueError):
            show = True
        out.append({'key': key, 'label': name.strip(),
                    'path': path.strip(), 'show': show})
    return out


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_apps(force=False):
    """Import every built-in app module and every plugin folder, and collect
    their APP_SPECs. Built-ins come first, so a plugin can never take over a
    built-in app's id.

    Each import is guarded. A module or folder that is missing, has a syntax
    error, or lacks a usable APP_SPEC is skipped and recorded in
    load_failures() — it does not stop the program from starting. Before this
    existed the app modules were hard top-level imports of Multichannel_DAQ,
    so one bad app file prevented the whole program from launching, which
    under a windowed build failed silently.
    """
    global _specs, _failures, _plugin_status, _enabled_at_start
    if _specs is not None and not force:
        return _specs

    _specs = []
    _failures = []
    _plugin_status = {}
    _enabled_at_start = read_enabled_plugins()
    owner = {}          # id -> label of the app that took it

    def _accept(name, spec):
        if spec['id'] in owner:
            return (f"duplicate app id {spec['id']!r} "
                    f"(already used by {owner[spec['id']]})")
        owner[spec['id']] = spec['label']
        _specs.append(spec)
        return None

    for name in APP_MODULES:
        try:
            module = importlib.import_module(name)
        except Exception:
            _failures.append((name, traceback.format_exc()))
            continue
        try:
            spec = _spec_from_module(module)
        except PluginError as e:
            _failures.append((name, str(e)))
            continue
        spec['plugin_path'] = None
        why = _accept(name, spec)
        if why:
            _failures.append((name, why))

    for name, folder, _has_entry in discover_plugins():
        if name not in _enabled_at_start:
            continue                    # listed in Settings, not run
        try:
            spec = _load_plugin_spec(folder)
            why = _accept(name, spec)
        except PluginError as e:
            spec, why = None, str(e)
        if why:
            _failures.append((name, why))
            _plugin_status[name] = (None, why)
        else:
            spec['plugin_name'] = name
            _plugin_status[name] = (spec, None)

    for name, why in _failures:
        print(f"App '{name}' not loaded: {why.strip().splitlines()[-1]}")

    return _specs


def load_failures():
    load_apps()
    return list(_failures)


def by_id(app_id):
    for spec in load_apps():
        if spec['id'] == app_id:
            return spec
    return None


def builtin_app_bar_items():
    """[(id, label, show_by_default)] for the built-in rows of Settings ->
    App Bar. Plugin rows are listed from the Plugins folder instead (see
    discover_plugins), loaded or not."""
    return [(s['id'], s['label'], s['show_by_default'])
            for s in load_apps() if s['plugin_path'] is None]


def plugin_status(name):
    """How a plugin (by folder name) fared at startup.

    ('loaded', spec), ('failed', reason), or ('disabled', None) for one that
    was not enabled when the app started.
    """
    load_apps()
    entry = _plugin_status.get(name)
    if entry is None:
        return ('disabled', None)
    spec, why = entry
    return ('loaded', spec) if spec is not None else ('failed', why)


def enabled_at_start():
    """Folder names of the plugins that were enabled when the app started."""
    load_apps()
    return set(_enabled_at_start)


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------

def resolve_gating(open_ids, no_daq=False, daq_running=False, extra_held=None):
    """Work out which app buttons are usable right now.

    open_ids     ids of apps whose dialog is currently open
    extra_held   claims held by something that is not a dialog in open_ids —
                 used for the Function Generator, which holds the AO bus while
                 generating even if its window has been closed.

    Returns {app_id: (enabled: bool, tooltip: str)}.

    This is the whole mutual-exclusion rule in one place. Previously each app
    hardcoded the button names of every other app, so adding app N meant
    editing N-1 files and a missed edit let two apps fight over the AO bus.
    """
    specs = load_apps()
    open_ids = set(open_ids or ())
    extra_held = set(extra_held or ())

    # Which app is holding each claimed resource.
    holder_of = {}
    for spec in specs:
        if spec['id'] in open_ids:
            for claim in spec['claims']:
                holder_of.setdefault(claim, spec)

    out = {}
    for spec in specs:
        app_id = spec['id']
        enabled, tip = True, ''

        if no_daq and spec['requires_daq_hw']:
            enabled = False
            tip = 'No DAQ device detected — connect a device and restart'
        elif daq_running and spec['requires_daq_stopped']:
            enabled = False
            tip = f"Stop the DAQ before opening {spec['label']}"
        else:
            # Blocked by another app holding a resource this one needs. An app
            # never blocks itself, so its own button stays live and can raise
            # the window it already owns.
            for claim in spec['claims']:
                holder = holder_of.get(claim)
                if holder is not None and holder['id'] != app_id:
                    enabled = False
                    tip = f"Close {holder['label']} before using {spec['label']}"
                    break
                if holder is None and claim in extra_held and app_id not in open_ids:
                    enabled = False
                    tip = f"Stop the running output before using {spec['label']}"
                    break

        out[app_id] = (enabled, tip)
    return out


def calibration_blocker(open_ids):
    """Label of an open app that blocks calibration, or None."""
    for spec in load_apps():
        if spec['id'] in (open_ids or ()) and spec['blocks_calibration']:
            return spec['label']
    return None


def digital_output_blocker(open_ids):
    """Label of an open app that blocks manual digital output, or None."""
    for spec in load_apps():
        if spec['id'] in (open_ids or ()) and spec['blocks_digital_output']:
            return spec['label']
    return None
