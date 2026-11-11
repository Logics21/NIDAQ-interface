
import sys
import os
import shutil
import json
import stat
import tempfile
import zipfile
from typing import NamedTuple
from dataclasses import dataclass

# Shown in the window title and stamped into the log header, so a log sent in
# from the field identifies the build it came from. Defined here, above the
# diagnostics bootstrap, because the log header is written before anything else.
APP_VERSION = "0.2.6"


# ---------------------------------------------------------------------------
# Diagnostics bootstrap — must run before anything else can print
# ---------------------------------------------------------------------------
def _install_log_file():
    """Send stdout/stderr to a log file when there is no console.

    PyInstaller's --windowed mode leaves sys.stdout and sys.stderr as None, so
    every print() in this program is discarded and, worse, a print() inside an
    except block can itself raise AttributeError and turn a handled error into
    a silent crash. This module and its apps report faults almost entirely
    through print(), so without this the packaged .exe has no diagnostics.

    Also installs an excepthook, because an unhandled exception in a windowed
    build otherwise closes the app with nothing shown and nothing recorded.

    Best effort throughout: logging must never be the reason the app fails.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return None            # console present, leave everything alone

    import tempfile, datetime, traceback as _tb

    # Beside the .exe normally; temp dir if that location is read-only
    # (Program Files) so a locked-down install still produces a log.
    base = os.path.dirname(sys.executable if getattr(sys, 'frozen', False)
                           else os.path.abspath(__file__))
    for candidate in (base, tempfile.gettempdir()):
        path = os.path.join(candidate, 'GioDAQ_log.txt')
        try:
            # Rotate once past ~2 MB so the file cannot grow without bound.
            if os.path.exists(path) and os.path.getsize(path) > 2 * 1024 * 1024:
                prev = path + '.1'
                if os.path.exists(prev):
                    os.remove(prev)
                os.replace(path, prev)
            stream = open(path, 'a', buffering=1, encoding='utf-8', errors='replace')
        except Exception:
            continue

        sys.stdout = stream
        sys.stderr = stream
        stream.write("\n" + "=" * 70 + "\n")
        stream.write(f"GioDAQ v{APP_VERSION} started "
                     f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}\n")
        stream.write("=" * 70 + "\n")

        def _hook(exc_type, exc, tb, _s=stream):
            _s.write("\nUNHANDLED EXCEPTION\n")
            _tb.print_exception(exc_type, exc, tb, file=_s)
            _s.flush()
        sys.excepthook = _hook
        return path
    return None


LOG_PATH = _install_log_file()

import glob
import re
import numpy as np
import pandas as pd
from datetime import datetime
from DAQ_Settings import (DAQSettingsWidget, DURATION_FACTORS, DURATION_UNITS,
                          UnsupportedChannelType, add_ai_channel, channel_unit,
                          log_daq_versions, row_channel_type,
                          timing_group as _timing_group)

# Straight after the log file is opened, so every log names the driver/package
# pairing that decided what the cards look like.
log_daq_versions()
# App dialogs are not imported here any more. DAQ_AppRegistry imports each app
# behind a try/except, so a missing or broken app file disables only that app
# instead of stopping the whole program from starting.
from functools import partial
from contextlib import contextmanager
from types import SimpleNamespace
import DAQ_AppRegistry as app_registry
import DAQ_Theme as daq_theme

import threading
import traceback
from scipy.signal import butter, sosfilt, sosfilt_zi, medfilt, firwin, lfilter, lfilter_zi

import nidaqmx
from nidaqmx.constants import TerminalConfiguration
from nidaqmx.stream_readers import AnalogMultiChannelReader

from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                             QGroupBox, QLabel, QLineEdit, QComboBox, QCheckBox, QPushButton,
                             QSpinBox, QDoubleSpinBox, QTabWidget, QFileDialog,
                             QGridLayout, QSizePolicy, QDialog, QDialogButtonBox, QGraphicsOpacityEffect,
                             QMessageBox, QTextEdit, QAction,
                             QTabBar, QStylePainter, QStyleOptionTab, QStyle)
from PyQt5.QtCore import Qt, QTimer, QObject, QEvent, pyqtSignal, QRectF, QLocale
from PyQt5.QtGui import (QColor, QPalette, QPainter, QDoubleValidator, QFont,
                         QFontMetrics)

import pyqtgraph as pg
from pyqtgraph import mkPen

daqSys = nidaqmx.system.System()
daqList = daqSys.devices.device_names
daqObjects = [daqSys.devices[name] for name in daqList]

NO_DAQ = len(daqList) == 0


def _safe_reset_device(name):
    """Reset the device named `name`, never raising, and never resetting a
    USB CompactDAQ/CompactRIO chassis.

    On a USB cDAQ (e.g. cDAQ-9178), reset_device() on the *chassis* drops the
    whole unit off the USB bus and forces a re-enumeration — the Windows
    connect/disconnect chime, a brief disappearance, and failed enumeration /
    acquisition. So:
      • if `name` is a chassis, reset each of its modules individually
        (clears them without disturbing the chassis' USB link);
      • otherwise reset exactly the named device/module.
    """
    if not name:
        return
    try:
        dev = nidaqmx.system.Device(str(name))
        modules = []
        try:
            cat = getattr(dev, "product_category", None)
            cat_name = getattr(cat, "name", str(cat)) if cat is not None else ""
            is_chassis = cat_name in ("COMPACT_DAQ_CHASSIS", "COMPACT_RIO_CHASSIS")
            modules = list(getattr(dev, "chassis_module_devices", []) or [])
        except Exception:
            is_chassis = False
        if is_chassis or modules:
            for m in modules:
                try:
                    m.reset_device()
                except Exception as e:
                    print(f"DAQ reset warning ({getattr(m, 'name', '?')}): {e}")
        else:
            dev.reset_device()
    except Exception as e:
        print(f"DAQ reset warning: {e}")


def _row_device(row, fallback):
    """Return the device/module name to use for a settings row.

    Prefers the row's own 'DAQ' value (so input, output and digital channels
    can each live on a different module of the same cDAQ chassis) and falls
    back to the global selected_daq when the column is absent or blank.
    """
    try:
        if 'DAQ' in row:
            val = row['DAQ']
            if pd.notna(val) and str(val).strip():
                return str(val).strip()
    except Exception:
        pass
    return fallback


def _input_index(ch_to_idx, value):
    """Row index of the input channel a custom channel refers to.

    The sheet stores this either bare ('ai0') or device-qualified
    ('Dev3/ai0') depending on which version wrote it, so the qualified form is
    retried without its prefix. Falls back to 0, as before, when the channel is
    not among the selected inputs.
    """
    key = str(value).strip()
    if key in ch_to_idx:
        return ch_to_idx[key]
    return ch_to_idx.get(key.rsplit('/', 1)[-1], 0)


def clean_device_names(names):
    result = []
    unnamed_count = 0
    for name in names:
        if pd.isna(name) or str(name).strip() == "":
            label = "unnamed" if unnamed_count == 0 else f"unnamed{unnamed_count}"
            result.append(label)
            unnamed_count += 1
        else:
            result.append(str(name))
    return result


def _app_dir():
    """Directory the app works from: beside the .exe when frozen, else the
    current working directory."""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.getcwd()


def _get_settings_path():
    return os.path.join(_app_dir(), 'default_settings.xlsx')


class PlottedChannel(NamedTuple):
    """One curve on the graph, as built by create_graph and read by plot_data.

    Was a 7-tuple whose first, second and sixth fields were never read: the plot
    widget (already reachable as self.graphDataWidget.plot_widget), a
    'plot_buffer{i}' string built only to be discarded, and a constant 'left'.
    DAQ_ProportionalSweep saves and restores axes_plotting wholesale to freeze
    plotting, but never looks inside the items, so the shape is free to change.
    """
    color: object
    device_name: str
    units: str
    ch_index: int


@dataclass
class HeldRun:
    """One recording kept for the session by "Hold Run to Session Memory".

    The samples stay on disk in `folder` (the recording's chunk files, moved
    there at Stop) and are read back only when the run's tab is shown or the
    run is saved. Everything else needed to redraw it as it was recorded is
    captured here, because by the time it is viewed the live controls may
    describe a different run. Position in VoltageContinuousInput.held_runs
    is its number: "Run 1" is index 0.
    """
    folder: str
    chunk_files: list
    channels: list          # PlottedChannel for each curve that was plotted
    x_name: str             # X axis selection ("Samples", "Run Duration", a channel)
    x_index: object         # ring index of the X channel, None for Samples/Run Duration
    x_label: str
    rate: int
    max_samples: object     # recording length cap in samples, None for no cap
    x_range: object         # (lo, hi) or None — the run's starting ranges
    y_ranges: dict
    notes: str
    saved: bool = False


def unit_key(value):
    """A channel's 'Units' cell as the string that names its Y axis.

    Blank/NaN -> ''. Used wherever a unit identifies an axis (the plot's
    per-unit axes and the Axis Ranges choices), so both name it the same way.
    """
    return "" if value is None or pd.isna(value) else str(value).strip()


def load_axis_range_defaults(path=None):
    """{unit: {'mode', 'min', 'max'}} from the settings workbook's "axis_ranges" sheet.

    The defaults set in Settings -> App Settings -> Axis Ranges, which the
    Axis Ranges dialog starts from. Forgiving, like load_app_bar_visibility:
    a missing sheet means no defaults (each axis takes its built-in default),
    and a bad row is skipped rather than losing the rest. Whether a mode suits
    a unit is left to PlotSettings._range_choice, which falls back to the
    built-in default for one it does not offer.
    """
    if NO_SETTINGS:
        return {}
    try:
        df = pd.read_excel(path or _get_settings_path(), sheet_name="axis_ranges")
    except Exception as e:
        print(f"axis_ranges sheet not readable ({e}); using built-in axis range defaults.")
        return {}
    defaults = {}
    for _, row in df.iterrows():
        unit = unit_key(row.get("Unit"))
        mode = "" if pd.isna(row.get("Range")) else str(row.get("Range")).strip()
        if not unit or not mode:
            continue
        lo = hi = None
        if mode == "Custom":
            try:
                lo, hi = float(row.get("Min")), float(row.get("Max"))
                if not (np.isfinite(lo) and np.isfinite(hi) and lo < hi):
                    raise ValueError
            except (TypeError, ValueError):
                print(f"axis_ranges: Custom range for '{unit}' needs Min < Max; skipped.")
                continue
        defaults[unit] = {'mode': mode, 'min': lo, 'max': hi}
    return defaults


# A channel's range is 'Min'/'Max' in the unit named by the sheet's 'Range
# Unit' — not necessarily volts, since 'Input Type' may be Current. Both are
# read by create_nidaq_task, which opens each channel through
# DAQ_Settings.add_ai_channel so the measurement type matches the card.
#
# The sheet's 'Scaled Units' is the engineering unit the Mx/b calibration
# produces and is what labels the calibrated trace. It is distinct from 'Range
# Unit', the raw electrical unit coming off the card. read_defaults renames it
# to 'Units' on load so the input, custom and output frames can be concatenated.


def read_defaults(xlsx_path=None):
    if xlsx_path is None:
        xlsx_path = _get_settings_path()
    def ensure_float_column(df, col, default=0.0):
        if col not in df.columns:
            df[col] = default
        df[col] = pd.to_numeric(df[col], errors='coerce').fillna(default).astype(float)

    def read_sheet(sheet):
        return pd.read_excel(xlsx_path, sheet_name=sheet)

    def filter_selected(df):
        if 'Selected Channel' in df.columns:
            df = df[df['Selected Channel'] == 1]
        return df.reset_index(drop=True)

    def get_setting(expert_df, label, numeric=False):
        try:
            s = expert_df.loc[expert_df['Setting'] == label, 'Value']
            if s.empty:
                return None
            v = s.iloc[0]
            if pd.isna(v):
                return None
            if numeric:
                v = pd.to_numeric(v, errors='coerce')
                return None if pd.isna(v) else float(v)
            return str(v)
        except Exception:
            return None

    default_settings = read_sheet('input_settings')
    default_settings = filter_selected(default_settings)
    # The input sheet calls the engineering unit 'Scaled Units'; the custom sheet
    # calls it 'Units' and the output frame has none at all (synthesised as 'V'
    # below). combined_settings concatenates all three, so normalise to the one
    # name here — this is a single one-way rename at load, not an alias: only
    # 'Units' exists in memory from this point on.
    if 'Scaled Units' in default_settings.columns:
        default_settings = default_settings.rename(columns={'Scaled Units': 'Units'})
    ensure_float_column(default_settings, 'Error', 0.0)
    ensure_float_column(default_settings, 'Mx', 1.0)
    ensure_float_column(default_settings, 'b', 0.0)

    default_settings_custom = read_sheet('custom_channel_settings')
    default_settings_custom = filter_selected(default_settings_custom)

    default_settings_output = read_sheet('output_settings')
    default_settings_output = filter_selected(default_settings_output)
    ensure_float_column(default_settings_output, 'Error', 0.0)

    expert_settings = read_sheet('expert_settings')

    sample_rate     = get_setting(expert_settings, "Sample Rate (Hz)", numeric=True)
    refresh_rate    = get_setting(expert_settings, "Refresh Rate (Hz)", numeric=True)
    max_buffer_size = get_setting(expert_settings, "Max Buffer Size",   numeric=True)
    max_plot_points = get_setting(expert_settings, "Max Plot Points",   numeric=True)
    # Starting values for the main window's two duration fields. Kept as text
    # so a blank Duration to Plot stays blank — that is what means "plot the
    # whole recording" — rather than being coerced to a number here.
    duration_acquire      = get_setting(expert_settings, "Duration to Acquire",      numeric=False)
    duration_acquire_unit = get_setting(expert_settings, "Duration to Acquire Unit", numeric=False)
    duration_plot         = get_setting(expert_settings, "Duration to Plot",         numeric=False)
    duration_plot_unit    = get_setting(expert_settings, "Duration to Plot Unit",    numeric=False)
    _dtf            = get_setting(expert_settings, "Decimate To Fit",   numeric=False)
    # Plot appearance. Read once here like every other expert setting, so a
    # change takes effect on the next start.
    theme_name      = get_setting(expert_settings, "Theme",             numeric=False)
    if not theme_name:
        theme_name = daq_theme.DEFAULT_THEME
    theme           = daq_theme.get_theme(theme_name)
    decimate_to_fit = (_dtf is not None) and (str(_dtf).strip().lower() in ("1", "true", "yes", "on"))

    ao_sample_rate_setting = get_setting(expert_settings, "AO Sample Rate (Hz)", numeric=True)
    filter_mode   = get_setting(expert_settings, "Filter Mode",        numeric=False)
    filter_type   = get_setting(expert_settings, "Filter Type",        numeric=False)
    filter_order  = get_setting(expert_settings, "Filter Order",       numeric=True)
    filter_cutoff = get_setting(expert_settings, "Filter Cutoff (Hz)", numeric=True)
    filter_fh     = get_setting(expert_settings, "Filter fh (Hz)",     numeric=True)
    # Which input channels the filter applies to: "All" (or blank) means every
    # channel; otherwise a comma-separated list of input Device Names.
    filter_channels = get_setting(expert_settings, "Filter Channels", numeric=False)

    default_settings["Device Name"]        = clean_device_names(default_settings["Device Name"].tolist())
    default_settings_custom["Device Name"] = clean_device_names(default_settings_custom["Device Name"].tolist())
    default_settings_output["Device Name"] = clean_device_names(default_settings_output["Device Name"].tolist())

    # The output sheet has no Units column, so derive one — combined_settings
    # needs a unit on every row for the axis labels and the saved file headers.
    # Derived from each row's 'Output Type' rather than assumed to be volts: a
    # current output is commanded in amps, and hardcoding 'V' put a 4-20 mA
    # trace into the export under a volts header.
    _out_units = [channel_unit(row_channel_type(r, 'ao'), 'ao')
                  for _, r in default_settings_output.iterrows()]
    if 'Units' not in default_settings_output.columns:
        default_settings_output = default_settings_output.copy()
        default_settings_output['Units'] = _out_units
    elif default_settings_output['Units'].isna().any():
        # Column present but some rows blank — fill only those, so a unit the
        # user set by hand survives.
        default_settings_output = default_settings_output.copy()
        default_settings_output['Units'] = (
            default_settings_output['Units'].fillna(
                pd.Series(_out_units, index=default_settings_output.index)))

    # Only 'Device Name' and 'Units' are ever read from combined_settings — it
    # resolves the X-axis channel to a ring index and labels that axis — which
    # is why the no-settings fallback declares it with just those two columns.
    # The three sheets have different schemas, so concatenating them whole built
    # a frame where most columns were empty or all-NA in two frames out of
    # three; pandas warns that the result dtype of such columns is changing in a
    # future version. Taking the two columns that are used removes the clash
    # rather than silencing it, and casting to object first keeps the dtype the
    # same across all three so a sheet that happens to lack 'Units' cannot
    # reintroduce it. Row order stays inputs -> custom -> outputs: the index
    # doubles as the ring-buffer index (see No_chans / No_chans_cust below).
    _combined_cols = ['Device Name', 'Units']
    combined_settings = pd.concat(
        [f.reindex(columns=_combined_cols).astype(object)
         for f in (default_settings, default_settings_custom,
                   default_settings_output)],
        ignore_index=True
    ).fillna('')

    # Inputs and custom channels only. plot_options drives the "Select Y-Axis"
    # checkboxes; AO channels are deliberately excluded there and are offered
    # on the X-axis instead (see out_chan_list / on_x_axis_selection_change).
    # Every column is kept here because the apps iterate whole rows, so instead
    # of trimming columns the empty frames are dropped — a sheet with no rows
    # contributes nothing but does force the dtype question above.
    _plot_frames = [f for f in (default_settings, default_settings_custom)
                    if not f.empty]
    plot_options = (pd.concat(_plot_frames, ignore_index=True) if _plot_frames
                    else default_settings.iloc[0:0].copy())

    No_chans      = len(default_settings)
    No_chans_out  = len(default_settings_output)
    No_chans_cust = len(default_settings_custom)

    if len(default_settings) == 0 and len(default_settings_output) == 0:
        raise ValueError("No selected channels found in XLSX (Selected Channel == 1).")

    selected_daq = (
        default_settings['DAQ'].iloc[0]
        if len(default_settings) > 0
        else default_settings_output['DAQ'].iloc[0]
    )
    daq = nidaqmx.system.Device(selected_daq)

    # Digital settings (used by Custom Triggers for digital source/stop options)
    try:
        default_settings_dig = read_sheet('digital_settings')
        default_settings_dig = filter_selected(default_settings_dig)
    except Exception:
        default_settings_dig = pd.DataFrame(columns=['Channel', 'Device Name', 'Direction', 'Selected Channel'])

    return {
        'default_settings': default_settings,
        'default_settings_output': default_settings_output,
        'default_settings_custom': default_settings_custom,
        'default_settings_dig': default_settings_dig,
        'combined_settings': combined_settings,
        'plot_options': plot_options,
        'No_chans': No_chans,
        'No_chans_out': No_chans_out,
        'No_chans_cust': No_chans_cust,
        'daq': daq,
        'selected_daq': selected_daq,
        'sample_rate': sample_rate,
        'refresh_rate': refresh_rate,
        'max_buffer_size': max_buffer_size,
        'max_plot_points': max_plot_points,
        'duration_acquire': duration_acquire,
        'duration_acquire_unit': duration_acquire_unit,
        'duration_plot': duration_plot,
        'duration_plot_unit': duration_plot_unit,
        'decimate_to_fit': decimate_to_fit,
        'filter_mode': filter_mode,
        'filter_type': filter_type,
        'filter_order': filter_order,
        'filter_cutoff': filter_cutoff,
        'filter_fh': filter_fh,
        'filter_channels': filter_channels,
        'ao_sample_rate_setting': ao_sample_rate_setting,
        'theme_name': theme_name,
        'theme': theme,
    }


def _load_settings_into_globals(xlsx_path=None):
    """Publish every value read_defaults() produces as a module global.

    The settings are module globals because the widgets and the acquisition
    loop read them directly; going through a dict here keeps the (long) list of
    names in exactly one place instead of repeating it at every reload site.
    """
    globals().update(read_defaults(xlsx_path))


try:
    _load_settings_into_globals()
    NO_SETTINGS = False
except FileNotFoundError:
    print("default_settings.xlsx not found — opening settings to create it.")
    NO_SETTINGS = True
except ValueError as e:
    # e.g. a saved file with no selected channels. Don't crash at startup —
    # fall back to the same empty/"open settings" path so the user can fix it.
    print(f"Settings file unusable ({e}) — opening settings to fix it.")
    NO_SETTINGS = True
if NO_SETTINGS:
    default_settings = pd.DataFrame(columns=[
        'DAQ', 'Channel', 'Device Name', 'Input Type', 'Min', 'Max', 'Range Unit',
        'Terminal Configuration', 'Mx', 'b', 'Error', 'Units', 'Selected Channel'
    ])
    default_settings_output = pd.DataFrame(columns=[
        'DAQ', 'Channel', 'Device Name', 'Output Type', 'Min', 'Max', 'Range Unit',
        'Error', 'Units', 'Selected Channel'
    ])
    default_settings_custom = pd.DataFrame(columns=[
        'Device Name', 'First Channel', 'Second Channel',
        'Operation', 'Units', 'Selected Channel'
    ])
    default_settings_dig = pd.DataFrame(columns=[
        'Channel', 'Device Name', 'Direction', 'Selected Channel'
    ])
    combined_settings = pd.DataFrame(columns=['Device Name', 'Units'])
    plot_options    = pd.DataFrame(columns=['Channel', 'Device Name', 'Units', 'Error'])
    No_chans = No_chans_out = No_chans_cust = 0
    daq = None
    selected_daq = ''
    sample_rate = refresh_rate = max_buffer_size = max_plot_points = decimate_to_fit = None
    duration_acquire = duration_acquire_unit = None
    duration_plot = duration_plot_unit = None
    filter_mode = filter_type = filter_order = filter_cutoff = filter_fh = None
    ao_sample_rate_setting = None
    filter_channels = "All"
    # The settings dialog still renders a graph area, so the theme has to exist
    # even when there is no workbook to read it from.
    theme_name = daq_theme.DEFAULT_THEME
    theme      = daq_theme.get_theme(theme_name)


_SETTINGS_SHEETS = (
    "input_settings", "default_settings", "output_settings",
    "custom_channel_settings", "digital_settings",
    "expert_settings", "transducers_settings", "key_mappings",
)


def _validate_data_file(path):
    """Check `path` is a recorded-data workbook before plotting it.

    Returns (ok, message). The mirror of _validate_settings_file: that one stops
    a data file being loaded as settings, this one stops a settings file (or any
    other spreadsheet) being loaded as data. Without it the first column was
    converted to float on trust and a channel-name column raised a bare
    ValueError out of the menu handler.
    """
    if not path or not os.path.isfile(path):
        return False, "File not found."
    try:
        xls = pd.ExcelFile(path)
    except Exception as e:
        return False, ("This file could not be opened as an Excel workbook.\n\n"
                       f"Details: {e}")
    settings_sheets = set(xls.sheet_names) & set(_SETTINGS_SHEETS)
    if settings_sheets:
        return False, ("This is a DAQ settings file, not a data file.\n\n"
                       f"It contains: {', '.join(sorted(settings_sheets))}.\n\n"
                       "To apply it as the active configuration use "
                       "File \u2192 Load Settings instead.")
    try:
        df = pd.read_excel(path)
    except Exception as e:
        return False, f"The first sheet could not be read.\n\nDetails: {e}"
    if df.empty or len(df.columns) < 2:
        return False, ("This file has no usable data \u2014 a data file needs an "
                       "X column and at least one channel column.")
    # Column 0 is the X axis and every value has to be a number. Report the
    # first offending cell: "could not convert string to float: 'ai0'" gives no
    # clue as to which column or row is at fault.
    x_col = df.columns[0]
    coerced = pd.to_numeric(df[x_col], errors="coerce")
    bad = coerced.isna() & df[x_col].notna()
    if bad.any():
        i = int(bad.to_numpy().nonzero()[0][0])
        return False, (f"The first column ('{x_col}') is the X axis, so it must "
                       "be numeric, but it contains text.\n\n"
                       f"First non-numeric value: '{df[x_col].iloc[i]}' "
                       f"(row {i + 2} of the sheet).\n\n"
                       "This does not look like a recorded data file.")
    return True, "OK"


def _validate_settings_file(path):
    """Check that `path` is a DAQ settings workbook before loading it.
    Returns (ok: bool, message: str). Guards against a user picking an
    arbitrary .xlsx that isn't a settings file."""
    if not path or not os.path.isfile(path):
        return False, "File not found."
    try:
        xls = pd.ExcelFile(path)
    except Exception as e:
        return False, ("This file could not be opened as an Excel workbook. "
                       "Please choose a valid .xlsx settings file.\n\n"
                       f"Details: {e}")
    sheets = set(xls.sheet_names)
    if not (sheets & set(_SETTINGS_SHEETS)):
        found = ", ".join(sorted(sheets)) or "none"
        return False, ("This does not look like a DAQ settings file.\n\n"
                       "It is missing the expected sheets (e.g. "
                       "'input_settings', 'output_settings').\n\n"
                       f"Sheets found: {found}")
    input_sheet = ("input_settings" if "input_settings" in sheets else
                   "default_settings" if "default_settings" in sheets else None)
    if input_sheet is None:
        return False, ("This settings file has no input-channel sheet "
                       "('input_settings'), so it cannot be loaded.")
    try:
        df = pd.read_excel(xls, sheet_name=input_sheet, nrows=1)
    except Exception as e:
        return False, f"The '{input_sheet}' sheet could not be read.\n\nDetails: {e}"
    required = {"Channel", "Selected Channel"}
    missing = required - set(str(c) for c in df.columns)
    if missing:
        return False, (f"The '{input_sheet}' sheet is missing required "
                       f"column(s): {', '.join(sorted(missing))}.\n\n"
                       "This file is not a valid settings file.")
    return True, "OK"


def _reload_settings_globals():
    """Re-read default_settings.xlsx into the module globals.

    Returns True if the reload succeeded. Callers that own a window follow up
    with _apply_reloaded_settings().
    """
    global NO_SETTINGS
    try:
        _load_settings_into_globals()
    except Exception as e:
        print(f"Could not reload settings: {e}")
        return False
    NO_SETTINGS = False
    return True


def _apply_reloaded_settings(window):
    """Repaint the chrome, then rebuild `window` against the new settings.

    Chrome first, so the new widgets are created against the palette they are
    meant to have.
    """
    apply_app_palette()
    if window is None:
        return
    window.refresh_ui()
    window._load_key_bindings()     # pick up any key mapping changes
    if not NO_DAQ:
        window.outputSettingsWidget.start_button.setEnabled(True)
        window.outputSettingsWidget.start_button.setToolTip("")


def _make_settings_active(path, window):
    """Make the workbook at `path` the active configuration.

    Copies it over the working settings file (unless it is that file),
    reloads the globals and rebuilds `window`. Shared by Load Settings and
    Load Session. Raises OSError if the copy fails; anything else raised comes
    from applying it.
    """
    dest = _get_settings_path()
    try:
        same = os.path.abspath(path) == os.path.abspath(dest)
    except Exception:
        same = False
    if not same:
        shutil.copyfile(path, dest)
    if _reload_settings_globals():
        _apply_reloaded_settings(window)


def _reload_settings_from_dialog():
    """settings_saved handler for the startup device-mismatch prompt.

    That prompt can open Settings before any window exists, so it looks the
    window up rather than being handed one, and it must reload the globals:
    otherwise after you fix the device and save, create_nidaq_task() still
    builds a task for the OLD device and fails with -201003 until you relaunch.
    """
    if not _reload_settings_globals():
        return
    try:
        _apply_reloaded_settings(globals().get("window"))
    except Exception as e:
        print(f"UI refresh after reload failed: {e}")


# The style's own palette, captured before any theme touches it. Switching to a
# theme that specifies no palette restores this, which is how Light gets back to
# exactly the stock Fusion appearance.
_BASE_PALETTE = None


def apply_app_palette():
    """Push the current theme's chrome colours onto the running application.

    Called at startup and again whenever settings are reloaded. Without the
    second case the palette was applied once and never revisited, so changing
    the theme in Settings repainted only the graph and the Log — those get
    rebuilt by refresh_ui() and re-read the theme — while every button, panel
    and tab kept the colours from whichever theme the app happened to start in.
    """
    global _BASE_PALETTE
    app = QApplication.instance()
    if app is None:
        return
    if _BASE_PALETTE is None:
        # Copy, so a later setPalette() cannot mutate the pristine original.
        _BASE_PALETTE = QPalette(app.palette())
    pal = daq_theme.qt_palette(theme_name, _BASE_PALETTE)
    app.setPalette(pal if pal is not None else _BASE_PALETTE)


def load_app_bar_visibility(path=None):
    """Read the "app_bar" sheet of the settings workbook and return a
    {app key: bool} map of which Apps-bar buttons should be shown.

    An empty map means "show everything", which is the right default whenever
    the sheet cannot be read: a first launch with no settings file, or a sheet
    missing its App/Show columns.

    This has to be forgiving. It runs while AppsWidget is being built, so an
    exception here stops the whole program from starting — and under a windowed
    build that failure is completely silent.
    """
    if NO_SETTINGS:
        return {}
    try:
        df = pd.read_excel(path or _get_settings_path(), sheet_name="app_bar")
    except Exception as e:
        print(f"app_bar sheet not readable ({e}); showing all apps.")
        return {}

    vis = {}
    for _, row in df.iterrows():
        # Skip malformed rows rather than losing the whole sheet to one of them.
        try:
            vis[str(row["App"]).strip()] = bool(int(row["Show"]))
        except Exception:
            continue
    return vis


def _exec_settings_dialog(parent, on_saved):
    """Open the DAQ Settings dialog modally, calling `on_saved` on each save."""
    dlg = QDialog(parent)
    dlg.setWindowTitle("DAQ Settings")
    dlg.resize(1000, 680)
    layout = QVBoxLayout(dlg)
    widget = DAQSettingsWidget(dlg)
    widget.settings_saved.connect(on_saved)
    layout.addWidget(widget)
    dlg.exec_()


def _short_chunk_path(path):
    """A chunk path as 'cache_dir/chunk_000001.npz', for log lines."""
    return os.path.join(os.path.basename(os.path.dirname(path)),
                        os.path.basename(path))


def _referenced_devices():
    """Set of device/module names referenced by the currently SELECTED
    channels across input, output and digital settings. (read_defaults already
    filters each frame to selected rows.)"""
    devs = set()
    for df in (default_settings, default_settings_output, default_settings_dig):
        try:
            if 'DAQ' in getattr(df, "columns", []):
                for v in df['DAQ'].tolist():
                    if pd.notna(v) and str(v).strip():
                        devs.add(str(v).strip())
        except Exception:
            pass
    return devs


def _check_daq_mismatch_and_prompt():
    """
    Called once at startup after the QApplication exists.
    If ANY selected channel in the settings file lives on a device that is not
    currently connected, inform the user and open Settings so they can fix it.
    """
    if NO_SETTINGS or NO_DAQ:
        return  # handled elsewhere

    referenced = _referenced_devices()
    missing = sorted(d for d in referenced if d not in daqList)

    if not missing:
        # Every selected channel's device is present — reset each so they
        # start clean, then continue.
        for d in sorted(referenced):
            _safe_reset_device(d)
        return

    # ── Mismatch: one or more selected channels reference missing devices ──
    connected = ", ".join(daqList) if daqList else "(none)"
    missing_str = ", ".join(missing)
    reply = QMessageBox.warning(
        None,
        "DAQ Device Mismatch",
        f"The settings file has selected channels on device(s) that are not "
        f"connected:\n    {missing_str}\n\n"
        f"Currently connected device(s):\n    {connected}\n\n"
        f"Please open Settings, select channels on the connected device(s),\n"
        f"and save to update the file.",
        QMessageBox.Open | QMessageBox.Ignore,
        QMessageBox.Open,
    )

    # Reset whichever referenced devices ARE present so they're in a clean state
    for d in sorted(referenced):
        if d in daqList:
            _safe_reset_device(d)

    if reply == QMessageBox.Open:
        # Reload the in-memory globals when the user saves, so the new device
        # is used immediately instead of requiring an app relaunch.
        _exec_settings_dialog(None, _reload_settings_from_dialog)
    else:
        print(f"DAQ mismatch ignored — saved device '{daq.name}' not found. "
              "Update settings before running.")


class LogStream(QObject):
    """Redirects sys.stdout to the LogWidget text area, thread-safely via signal."""
    message = pyqtSignal(str)

    def write(self, text):
        text = text.strip()
        if text:
            self.message.emit(text)

    def flush(self):
        pass


class DAQSignals(QObject):
    data_ready = pyqtSignal(object)
    task_stopped = pyqtSignal()


class GraphData(QGroupBox):
    def __init__(self, parent=None, title=""):
        # Untitled by default: the graphs sit in tabs ("Live Graph", "Run N")
        # that already name them.
        super().__init__(title)
        self.parent = parent
        self._notes_item = None
        # Overlay: reference curves from a file, display only (see set_overlay).
        self._overlay = []          # [(label, unit, x, y, color)]
        self._overlay_items = {}    # label -> (PlotDataItem, ViewBox) while drawn
        self.plot_items = {}
        self.unit_axes = {}
        self.axis_order = []
        # View-range bookkeeping: 'x' and 'y:<unit>' -> (lo, hi) for a fixed
        # axis, None for autorange. Seeded at Run (set_run_ranges), then kept
        # up to date with what the user does in the plot (_capture_range), so
        # axes rebuilt mid-run by Clear Data come back as the user left them,
        # and with "Apply on Run" off the next Run starts from it. A range
        # change also forces the curves to recompute their displayed data.
        self._axis_ranges = {}
        self._range_guard = False
        self._main_range_slot = None
        self.create_widgets()

    def create_widgets(self):
        layout = QVBoxLayout(self)
        self.plot_widget = pg.PlotWidget()
        layout.addWidget(self.plot_widget)
        self.main_vb = self.plot_widget.getViewBox()
        self.plot_widget.addLegend()
        self.plot_widget.plotItem.showAxis('left', True)
        self.plot_widget.plotItem.showAxis('right', False)
        self._unit_labels = {}
        # Backgrounds, grid, axis ink, title, legend and curves in one call —
        # the same call printing goes through, so the screen look has a single
        # source rather than one here and a copy of it elsewhere.
        self._apply_plot_style()
        self.main_vb.sigResized.connect(self._update_extra_views)
        # '__left__' is the main ViewBox: it owns the X axis and the left Y axis.
        self._main_range_slot = self._bind_range_capture(self.main_vb, '__left__')
        self._tame_axis_menu(self.main_vb)

    # Print styling. Paper is white whatever the screen theme is, so the plot is
    # restyled for the page rather than printed as it appears.
    PRINT_GRID_PEN  = '#9a9a9a'     # the axis pen also draws the grid
    PRINT_INK       = '#000000'     # axis numbers, captions, title, legend
    PRINT_PEN_WIDTH = 3             # traces, heavier than the screen's 2
    # Pixel width the plot is re-laid-out to for printing. About 360 dpi across a
    # 10 in page — fine detail without building a needlessly huge image.
    PRINT_IMAGE_WIDTH = 3600
    # Base text sizes. Styling always rewrites text from these rather than from
    # whatever size the text currently has, which is what keeps it idempotent.
    LEGEND_PT = 9                   # pyqtgraph's own default for legend labels
    TITLE_PT  = 11

    def _all_axes(self):
        """Every axis currently on the plot, including the per-unit right axes."""
        axes = []
        for side in ('left', 'bottom'):
            try:
                axes.append(self.plot_widget.plotItem.getAxis(side))
            except Exception:
                pass
        for pack in self.unit_axes.values():
            ax = pack.get('axis')
            if ax is not None:
                axes.append(ax)
        return axes

    @staticmethod
    def _print_ink(color):
        """`color` darkened until it will read on white paper.

        Judged on perceptual luminance, not HSV value or HSL lightness. Both of
        those call amber (#ffd400) mid-range, when on white it is the least
        readable colour in the Instrument palette — the green channel carries
        most of the apparent brightness, which is what the weights here account
        for. Hue is preserved, so a channel stays recognisably its own colour.
        """
        c = QColor(color)
        if not c.isValid():
            return None

        def lum(q):
            return 0.299 * q.red() + 0.587 * q.green() + 0.114 * q.blue()

        for _ in range(12):                 # bounded; darker() is monotonic
            if lum(c) <= 145:
                break
            c = c.darker(115)
        return c

    def _print_text_scale(self):
        """How much to enlarge the legend text so it prints at screen size.

        The legend alone needs this. Everything else on the canvas already
        follows the exporter when it enlarges the plot: axis numbers are painted
        by the axis, and the title and unit captions are in the PlotItem's
        layout, which is scaled with it. The legend is an anchored overlay, so
        its labels render at a fixed device size and shrink in proportion as the
        image grows — measured on a 3600 px export, a 9pt label came out at about
        2.9pt on the page. Scaling by the same factor the plot grows by restores
        it to its screen proportion (measured 1.58% of page height against 1.67%
        on screen). Applying this to the title as well made the title nearly
        three times too big, since it was already being scaled.

        Clamped, because a very small or very large plot widget would otherwise
        give an absurd factor.
        """
        try:
            width = max(int(self.plot_widget.width()), 1)
            return max(1.0, min(6.0, float(self.PRINT_IMAGE_WIDTH) / width))
        except Exception:
            return 1.0

    def _apply_plot_style(self, printing=False):
        """Apply the plot's entire look: theme colours, or print colours.

        One function, two sets of values, and it can be called at any time
        without knowing what was applied before — every element is written from
        the theme or the print constants, never derived from the element's
        current state.

        That is deliberate, and replaced an earlier arrangement where printing
        applied a pile of overrides and then reversed them one at a time. The
        reversals had to mirror the overrides exactly, and one did not: label
        sizes were restored but the legend's layout was never re-measured, so the
        legend came back from a print with print spacing. Rather than add the
        missing reversal, there are no reversals — going back to the screen look
        re-runs this with printing=False, which is the same code that produced
        that look in the first place and so cannot drift from it.
        """
        if printing:
            widget_bg = canvas_bg = 'w'
            axis_pen, axis_text = self.PRINT_GRID_PEN, self.PRINT_INK
            title_col = legend_col = self.PRINT_INK
            legend_fill = None          # a dark fill would print as a block
            legend_scale = self._print_text_scale()
            pen_width = self.PRINT_PEN_WIDTH
            curve_ink = self._print_ink
        else:
            # Two backgrounds: the widget covers the margin holding the axis
            # numbers, the ViewBox covers the plotting area. Separate so a theme
            # can colour them differently and pick axis text to suit.
            widget_bg, canvas_bg = theme['widget'], theme['canvas']
            axis_pen = theme['axis']
            axis_text = theme.get('axis_text') or theme['axis']
            title_col = daq_theme.title_color(theme_name)
            legend_col = theme.get('legend_text')
            legend_fill = daq_theme.legend_bg(theme_name)
            legend_scale = 1.0
            pen_width = 2
            curve_ink = QColor

        pw = self.plot_widget
        try:
            pw.setBackground(widget_bg)
            pw.showGrid(x=True, y=True, alpha=theme['grid_alpha'])
            self.main_vb.setBackgroundColor(canvas_bg)
        except Exception:
            pass

        for ax in self._all_axes():
            self._apply_axis_ink(ax, axis_pen, axis_text)

        # Captions and title are LabelItems: colour is baked into their HTML, so
        # setText is the only thing that re-renders them.
        for lbl in list(getattr(self, '_unit_labels', {}).values()):
            try:
                lbl.setText(lbl.text, color=axis_text)
            except Exception:
                pass
        try:
            tl = pw.plotItem.titleLabel
            if tl.text:
                tl.setText(tl.text, color=title_col, size=f'{self.TITLE_PT}pt')
        except Exception:
            pass

        self._style_legend(getattr(pw.plotItem, 'legend', None),
                           legend_col, legend_fill, legend_scale)

        for rec in self.plot_items.values():
            try:
                item = rec.get('item')
                if item is None:
                    continue
                ink = curve_ink(rec.get('color'))
                col = rec.get('color') if ink is None else ink.name()
                mode = rec.get('pen_mode', False)
                # Reuses _apply_pen_mode so a channel in dot mode stays dots.
                self._apply_pen_mode(item, col, mode)
                if not mode and pen_width != 2:
                    item.setPen(mkPen(color=col, width=pen_width))
            except Exception:
                pass

    def _style_legend(self, legend, color, fill, scale=1.0):
        """Rebuild the legend's entries in the given colour and text size.

        pyqtgraph bakes a label's colour and size into its HTML when the entry is
        created, and its layout keeps the geometry it was handed. Restyling an
        existing legend therefore never fully takes: the labels can be rewritten
        with setText, but the box stays the size it was. That is what distorted
        the legend after a print — the labels came back to 9pt inside a box still
        sized for 29pt — and nudging it with updateSize only shrank it a step per
        call, which is the shape of a workaround, not a fix.

        Clearing and re-adding the entries goes through pyqtgraph's own
        construction path instead: addItem reads exactly the two options set
        below and bakes them into each new label, so colour, size and geometry
        come out consistent by construction, and calling this twice with the same
        values leaves the legend identical.

        Membership comes from plot_items, which is the record of what is plotted,
        so the legend cannot drift out of step with the curves either.
        """
        if legend is None:
            return None
        # Written straight to opts, not through the setters: the setters do not
        # exist on older pyqtgraph, while addItem has read these keys throughout.
        try:
            legend.opts['labelTextSize'] = f"{self.LEGEND_PT * scale:.0f}pt"
            if color:
                legend.opts['labelTextColor'] = color
            legend.setBrush(pg.mkBrush(fill) if fill else pg.mkBrush(None))
        except Exception:
            pass
        try:
            legend.clear()
            for name, rec in self.plot_items.items():
                item = rec.get('item')
                if item is not None:
                    legend.addItem(item, name)
            for name, (item, _vb) in self._overlay_items.items():
                legend.addItem(item, name)
        except Exception:
            pass
        return legend

    @contextmanager
    def print_colors(self):
        """Hold the plot in print colours for the duration of the block."""
        try:
            self._apply_plot_style(printing=True)
            yield
        finally:
            self._apply_plot_style(printing=False)

    def render_to_painter(self, painter, target):
        """Draw the plot into `target` on `painter`, fitted and centred.

        Goes through pyqtgraph's ImageExporter rather than rendering the
        QGraphicsScene onto the page directly. Scene rendering is the obvious
        route and is wrong here: scaling the scene up to printer resolution
        scrambles the axis text, because pyqtgraph lays that out in device pixels
        rather than scene units — tick labels came out the size of the plot
        itself and in the wrong place. The exporter re-lays the plot out at the
        target pixel size instead, so axes, title and legend keep the
        proportions they have on screen.

        The exporter's own background is forced white as a second line of
        defence. It only covers the margin — the ViewBox paints its own
        background, which is why print_colors() has to whiten that separately —
        but it means a future theme colouring something this method does not
        know about still cannot produce a dark page.
        """
        image = None
        try:
            from pyqtgraph.exporters import ImageExporter
            exporter = ImageExporter(self.plot_widget.plotItem)
            exporter.parameters()['width'] = self.PRINT_IMAGE_WIDTH
            exporter.parameters()['background'] = QColor('white')
            image = exporter.export(toBytes=True)
        except Exception:
            image = None
        if image is None or image.isNull():
            # Fall back to the plot at screen resolution: coarser on paper, but
            # a usable print beats an error message.
            image = self.plot_widget.grab().toImage()

        target = QRectF(target)
        iw, ih = max(image.width(), 1), max(image.height(), 1)
        # Fit, centred, aspect preserved — a stretched graph misreads.
        scale = min(target.width() / iw, target.height() / ih)
        w, h = iw * scale, ih * scale
        spot = QRectF(target.left() + (target.width() - w) / 2.0,
                      target.top() + (target.height() - h) / 2.0, w, h)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        painter.drawImage(spot, image)

    def set_overlay(self, curves):
        """Show reference curves `curves` = [(label, unit, x, y, color)].

        Display only. Each curve goes on the axis for its unit when that axis
        exists — now, or when a Run builds it — and a curve whose unit never
        gets an axis is not drawn. Dashed and faded so it reads as reference,
        and left out of autorange so the live data alone sets the scale.
        """
        self._detach_overlay()
        self._overlay = list(curves)
        for unit in list(self.unit_axes):
            self._attach_overlay(unit)
        self._ensure_legend()

    def _attach_overlay(self, unit):
        pack = self.unit_axes.get(unit)
        if pack is None:
            return
        vb = pack['vb'] or self.main_vb
        for label, curve_unit, x, y, color in self._overlay:
            if curve_unit != unit or label in self._overlay_items:
                continue
            ink = pg.mkColor(color)
            ink.setAlpha(170)
            item = pg.PlotDataItem(x, y, pen=mkPen(color=ink, width=2, style=Qt.DashLine))
            vb.addItem(item, ignoreBounds=True)
            # After addItem, as update_plot does: clip-to-view needs the item's
            # ViewBox, and while it is being added it has none yet.
            self._set_data_reduction(item, x)
            self._overlay_items[label] = (item, vb)

    def _detach_overlay(self):
        for item, vb in self._overlay_items.values():
            try:
                vb.removeItem(item)
            except Exception:
                pass
        self._overlay_items = {}

    def set_notes(self, text):
        """Show `text` fixed to the top-left corner of the plot; '' hides it.

        Parented to the main ViewBox rather than added to the plot, so it sits
        in pixel coordinates (does not pan or zoom) and clear_plots(), which
        removes plotted items, leaves it alone.
        """
        if self._notes_item is None:
            self._notes_item = pg.TextItem(anchor=(0, 0))
            self._notes_item.setParentItem(self.main_vb)
            self._notes_item.setPos(8, 8)
            self._notes_item.setZValue(100)
        color = theme.get('legend_text') or theme.get('axis_text')
        self._notes_item.setText(text, color=color)
        self._notes_item.setVisible(bool(text))

    def set_graph_title(self, text):
        """Set the graph title in the current theme's title colour.

        The colour has to be passed together with the text: pyqtgraph builds the
        title as an HTML LabelItem with the colour baked in, so setting it
        afterwards does not re-render (the same trap as the unit captions in
        _set_unit_label). Left to itself pyqtgraph uses its default foreground,
        a light grey meant for its dark default background, which was all but
        invisible on the white canvas.
        """
        col = daq_theme.title_color(theme_name)
        try:
            self.plot_widget.setTitle(text, color=col, size=f'{self.TITLE_PT}pt')
        except Exception:
            self.plot_widget.setTitle(text)

    def clear_plots(self):
        # The overlay sits in the ViewBoxes directly, which plot_widget.clear()
        # does not reach; it is drawn again as the new axes appear.
        self._detach_overlay()
        try:
            self.plot_widget.clear()
        except Exception:
            pass

        for unit, pack in list(self.unit_axes.items()):
            vb = pack.get('vb', None)
            ax = pack.get('axis', None)
            if vb is not None:
                # Cut the old ViewBox off before it goes. Taken out of the scene
                # it still emits range signals once the event loop next runs,
                # and its capture slot then wrote the range it was left at over
                # the ranges the next Run had just seeded for that unit — so a
                # right-hand axis came back as the user left it instead of as
                # Axis Ranges said. The X link goes too, for the same reason.
                slots = pack.get('slot')
                if slots is not None:
                    on_range, on_state = slots
                    for signal, slot in ((vb.sigRangeChanged, on_range),
                                         (vb.sigStateChanged, on_state)):
                        try:
                            signal.disconnect(slot)
                        except Exception:
                            pass
                try:
                    vb.setXLink(None)
                except Exception:
                    pass
                for item in list(getattr(vb, 'addedItems', [])):
                    try: vb.removeItem(item)
                    except Exception: pass
                try:
                    self.plot_widget.scene().removeItem(vb)
                except Exception:
                    pass
            if ax is not None:
                # Three steps, not one. layout.removeItem() only takes the axis
                # out of the grid: it stays a child of the PlotItem and stays in
                # the scene, so it carries on painting at its last geometry. The
                # replacement axis built on the next Run lands in the same column
                # and the two are drawn on top of each other — one frozen with
                # the old ticks, one live.
                try:
                    self.plot_widget.plotItem.layout.removeItem(ax)
                except Exception:
                    pass
                try:
                    ax.setParentItem(None)
                except Exception:
                    pass
                try:
                    sc = ax.scene()
                    if sc is not None:
                        sc.removeItem(ax)
                except Exception:
                    pass

        self.plot_items = {}
        self.unit_axes = {}
        self.axis_order = []

        try:
            self.plot_widget.plotItem.showAxis('left', True)
            self.plot_widget.plotItem.showAxis('right', False)
            _la = self.plot_widget.plotItem.getAxis('left')
            _la.setLabel("")
            self._apply_axis_ink(_la)
            # Drop every unit caption; they are recreated as channels replot.
            for _k in list(getattr(self, '_unit_labels', {})):
                self._set_unit_label(_k, '')
        except Exception:
            pass

        if getattr(self.plot_widget.plotItem, 'legend', None) is not None:
            try:
                self.plot_widget.plotItem.legend.clear()
            except Exception:
                pass

    def _ensure_legend(self):
        if getattr(self.plot_widget.plotItem, 'legend', None) is None:
            self.plot_widget.addLegend()
        return self._theme_legend(self.plot_widget.plotItem.legend)

    def _theme_legend(self, legend):
        """Style a legend with the current theme's values (see _style_legend)."""
        return self._style_legend(legend, theme.get('legend_text'),
                                 daq_theme.legend_bg(theme_name))

    def _ensure_axis_for_unit(self, unit: str, color=None):
        """Make sure a Y axis exists for `unit`.

        `color` is accepted but no longer tints the axis: every Y axis uses the
        theme's axis ink, matching the X axis, rather than borrowing the colour
        of whichever channel happened to create it.
        """
        if unit in self.unit_axes:
            return

        if not self.axis_order:
            left_axis = self.plot_widget.plotItem.getAxis('left')
            left_axis.setLabel("")            # caption goes above, not alongside
            self._set_unit_label('left', unit)
            self._apply_axis_ink(left_axis)
            self._apply_stored_y_range(self.main_vb, unit)
            self.unit_axes[unit] = {"vb": None, "axis": None}
            self.axis_order.append(unit)
            self._attach_overlay(unit)
            return

        vb = pg.ViewBox()
        vb.enableAutoRange(axis=pg.ViewBox.YAxis, enable=True)
        slot = self._bind_range_capture(vb, unit)
        self._tame_axis_menu(vb)
        # A right-hand axis is destroyed and rebuilt on every Run and on Clear
        # Data, so its range is put on here rather than relying on the ViewBox
        # surviving.
        self._apply_stored_y_range(vb, unit)
        vb.setXLink(self.main_vb)
        # Extra ViewBoxes default to zValue -100 and are added as top-level scene
        # items, while the main ViewBox sits inside the PlotItem at z 0. With an
        # opaque canvas background that ordering painted the PlotItem — and its
        # background — straight over these, so every channel on a second unit
        # silently vanished. Raising them to the PlotItem's level fixes it.
        vb.setZValue(0)
        self.plot_widget.scene().addItem(vb)

        axis = pg.AxisItem('right')
        axis.setLabel("")
        self._apply_axis_ink(axis)

        existing_right_axes = sum(1 for u in self.axis_order if self.unit_axes[u]['axis'] is not None)
        col = 4 + existing_right_axes
        layout = self.plot_widget.plotItem.layout
        layout.addItem(axis, 2, col)

        try:
            layout.setColumnFixedWidth(col, 60)
        except Exception:
            pass
        # Caption above this axis's own column, matching the left axis.
        self._set_unit_label(f'right:{unit}', unit, row=1, col=col)

        axis.linkToView(vb)
        vb.setGeometry(self.main_vb.sceneBoundingRect())
        self._update_extra_views()

        self.unit_axes[unit] = {"vb": vb, "axis": axis, "slot": slot}
        self.axis_order.append(unit)
        self._attach_overlay(unit)

    def _apply_axis_ink(self, axis, pen=None, text=None):
        """Paint an axis, defaulting to the theme's neutral ink.

        Two colours, because pyqtgraph draws the grid with the axis *pen*:
        theme['axis'] covers the axis line, ticks and grid, while
        theme['axis_text'] covers only the numbers. On a dark canvas a pen dark
        enough to look right on the margin made the grid invisible.

        `pen` and `text` let a caller override both — printing passes its own.
        """
        try:
            axis.setPen(pg.mkPen(theme['axis'] if pen is None else pen))
            axis.setTextPen(pg.mkPen(
                (theme.get('axis_text') or theme['axis']) if text is None else text))
        except Exception:
            pass

    def _set_unit_label(self, key, text, row=None, col=None):
        """Put a unit caption in a free cell of the PlotItem's grid.

        pyqtgraph's own axis label is drawn rotated alongside the numbers. The
        instrument convention is a horizontal caption above the Y numbers, so
        the axis label is left empty and a LabelItem is parked in the
        neighbouring layout cell instead:

            (1, 0)      above the left axis        -> Y unit
            (1, col)    above an extra right axis  -> that axis's unit

        The X unit is not a caption — it goes under the axis via pyqtgraph's own
        bottom label (see plot_data), because a LabelItem at cell (3, 2) widened
        that column and shoved every extra right-hand Y axis further right.

        Colour and size must be passed to the constructor; setting them
        afterwards with setAttr does not re-render the text.
        """
        if not hasattr(self, '_unit_labels'):
            self._unit_labels = {}
        layout = self.plot_widget.plotItem.layout
        old = self._unit_labels.pop(key, None)
        if old is not None:
            try:
                layout.removeItem(old)
                old.setParentItem(None)
            except Exception:
                pass
        if not text:
            return
        if key == 'left':
            row, col, just = 1, 0, 'right'
        else:
            just = 'center'
        lbl = pg.LabelItem(str(text), justify=just, size='9pt',
                           color=theme.get('axis_text') or theme['axis'])
        try:
            layout.addItem(lbl, row, col)
            self._unit_labels[key] = lbl
        except Exception:
            pass

    def _update_extra_views(self):
        for unit, pack in self.unit_axes.items():
            vb = pack['vb']
            if vb is None:
                continue
            vb.setGeometry(self.main_vb.sceneBoundingRect())
            vb.linkedViewChanged(self.main_vb, vb.XAxis)

    # ------------------------------------------------------------------
    # Manual axis ranges
    #
    # Typing a range into an axis context menu turns that axis's autorange off
    # and sets the range in one go. Two things used to go wrong:
    #
    #   * The curves draw a reduced copy of the data (clip-to-view and
    #     auto-downsampling), and that copy is only rebuilt when the view tells
    #     them the range moved. When it was not rebuilt the trace came back
    #     empty or clipped, and nudging the wheel over the axis was the only way
    #     to force it. _refresh_curve_display now does that explicitly on every
    #     range change, for curves on the extra right-hand ViewBoxes too.
    #
    #   * Clear Data rebuilds the per-unit axes mid-run, which discarded
    #     whatever the user had set. The range is recorded here as it is set
    #     and put back when the axis is recreated.
    #
    # At Run, set_run_ranges writes the ranges the run starts with, once: the
    # Axis Ranges choices with "Apply on Run" ticked, otherwise carried_ranges()
    # — the axes as the previous run left them. From then on the plot is the
    # user's to change.
    # ------------------------------------------------------------------

    def _tame_axis_menu(self, vb):
        """Stop the axis context menu rewriting itself while it is open.

        pyqtgraph connects ViewBox.sigStateChanged to ViewBoxMenu.viewStateChanged,
        which calls updateState() whenever the menu's boxes are on screen.
        updateState() overwrites the Min/Max text with the view's current range
        and re-checks the Auto radio. A ViewBox on autorange emits that signal
        once per incoming block — so at the refresh rate — which means anything
        typed into X/Y Axis -> Manual was overwritten before it could be
        committed. That is why only the mouse wheel worked: it is not a text
        field, so there was nothing to overwrite.

        The live connection is dropped and replaced with a single refresh when
        the menu opens. Consequence: numbers in an open menu are from the moment
        it was opened rather than live, which is what makes them typeable.
        """
        menu = getattr(vb, 'menu', None)
        if menu is None:
            return
        try:
            vb.sigStateChanged.disconnect(menu.viewStateChanged)
        except Exception:
            pass                     # already disconnected, or a different build
        try:
            menu.aboutToShow.connect(menu.updateState)
        except Exception:
            pass

    def _bind_range_capture(self, vb, key):
        """Watch `vb` so its manual ranges are remembered and curves refreshed.

        Returns the slots. The caller keeps them — a function connected to a
        signal has no other owner and would otherwise be free to disappear —
        and clear_plots() uses them to disconnect the ViewBox it tears down.
        """
        def on_range(*_args, _k=key, _vb=vb):
            self._capture_range(_k, _vb)
        def on_state(*_args, _k=key, _vb=vb):
            self._capture_range(_k, _vb, refresh=False)
        try:
            vb.sigRangeChanged.connect(on_range)
            vb.sigStateChanged.connect(on_state)
        except Exception:
            pass
        return on_range, on_state

    def _capture_range(self, key, vb, refresh=True):
        """Record the range if the axis is manual, forget it if it is auto.

        Called for every range change and every state change. The state change
        matters for switching an axis back to Auto (or the plot's auto button):
        that does not move the range when the data already fits it, so on
        range changes alone the old manual range stayed recorded. `refresh` is
        off for state changes, which come with no new range to redraw for.
        """
        if self._range_guard:
            return
        try:
            auto_x, auto_y = vb.state['autoRange'][0], vb.state['autoRange'][1]
            x_rng, y_rng = vb.viewRange()
        except Exception:
            return
        if key == '__left__':
            self._axis_ranges['x'] = None if auto_x else (float(x_rng[0]), float(x_rng[1]))
            # The main ViewBox carries whichever unit was plotted first; with
            # no axis built yet (just cleared) its Y belongs to no unit.
            key = self.axis_order[0] if self.axis_order else None
        if key is not None:
            self._axis_ranges['y:' + str(key)] = (
                None if auto_y else (float(y_rng[0]), float(y_rng[1])))
        if refresh:
            self._refresh_curve_display()

    def _refresh_curve_display(self):
        """Make every curve rebuild the reduced copy of its data it draws.

        This is what the mouse-wheel workaround was doing by accident.
        """
        if self._range_guard:
            return
        self._range_guard = True
        try:
            for rec in self.plot_items.values():
                item = rec.get('item')
                if item is None:
                    continue
                try:
                    item.viewRangeChanged()
                except Exception:
                    pass
                try:
                    item.update()
                except Exception:
                    pass
        finally:
            self._range_guard = False

    def _apply_stored_y_range(self, vb, key):
        """Put the run's Y range for unit `key` on `vb`, or autorange it."""
        rng = self._axis_ranges.get('y:' + str(key))
        self._range_guard = True
        try:
            if rng is None:
                vb.enableAutoRange(axis=pg.ViewBox.YAxis, enable=True)
            else:
                vb.enableAutoRange(axis=pg.ViewBox.YAxis, enable=False)
                vb.setYRange(rng[0], rng[1], padding=0)
        except Exception:
            pass
        finally:
            self._range_guard = False

    def carried_ranges(self):
        """(x_range, {unit: y_range}) as the axes were last left.

        What the previous run ended with, user changes included: (lo, hi) for
        an axis set by hand, None for one on autorange. Units are by name, so a
        range follows its unit whichever side its axis lands on next time.
        """
        x_range = self._axis_ranges.get('x')
        y_ranges = {key[2:]: rng for key, rng in self._axis_ranges.items()
                    if key.startswith('y:')}
        return x_range, y_ranges

    def set_run_ranges(self, x_range, y_ranges):
        """Write the ranges a Run starts with: once, then the plot is the user's.

        `x_range` and each value of `y_ranges` (unit -> range) is (lo, hi) to
        fix that axis or None to autorange it. X is applied here. The Y axes do
        not exist yet — clear_plots dropped them and the first plot update
        builds them — so each Y range lands in _ensure_axis_for_unit.
        """
        self._axis_ranges = {'x': x_range}
        self._axis_ranges.update({'y:' + str(u): r for u, r in y_ranges.items()})
        self._range_guard = True
        try:
            if x_range is None:
                self.main_vb.enableAutoRange(axis=pg.ViewBox.XAxis, enable=True)
            else:
                self.main_vb.enableAutoRange(axis=pg.ViewBox.XAxis, enable=False)
                self.main_vb.setXRange(x_range[0], x_range[1], padding=0)
        except Exception:
            pass
        finally:
            self._range_guard = False
        self._refresh_curve_display()

    @staticmethod
    def _x_is_ordered(x_data):
        """True if x never decreases — the assumption clip/downsample rely on."""
        try:
            arr = np.asarray(x_data)
            if arr.ndim != 1 or arr.shape[0] < 2:
                return True
            return bool(np.all(np.diff(arr) >= 0))
        except Exception:
            return False

    def _set_data_reduction(self, curve, x_data):
        """Use clip-to-view and auto-downsampling only on a sorted X axis.

        Both features locate the visible span with a binary search over x, so
        they are only valid when x increases — "Samples" and "Run Duration". Put
        a channel on X and x oscillates, the search returns a meaningless slice,
        and setting an X range drops most of the trace. Switching them off there
        costs nothing: plot_data() has already thinned the data to Max Plot
        Points before it gets here.
        """
        ordered = self._x_is_ordered(x_data)
        if getattr(curve, '_daq_reduction', None) == ordered:
            return
        try:
            curve.setClipToView(ordered)
            if ordered:
                curve.setDownsampling(auto=True, method='peak')
            else:
                curve.setDownsampling(ds=1, auto=False, method='peak')
            curve._daq_reduction = ordered
        except Exception:
            pass

    def update_plot(self, channel_name, x_data, y_data, color, pen_mode=False, unit: str = None):
        if unit is None:
            unit = ""

        self._ensure_axis_for_unit(unit, color=color)

        dest_vb = self.unit_axes[unit]['vb'] if unit in self.unit_axes else None
        if channel_name not in self.plot_items:
            # Always create the curve with both pen and symbol defined;
            # visibility of each is controlled by _apply_pen_mode below.
            pen = mkPen(color=color, width=2)
            curve = pg.PlotDataItem(x_data, y_data,
                                    pen=pen,
                                    symbol='o', symbolSize=5,
                                    symbolBrush=color,
                                    name=channel_name)
            # Apply the correct initial style before the item is shown
            self._apply_pen_mode(curve, color, pen_mode)

            if dest_vb is None:
                self.plot_widget.addItem(curve)
            else:
                dest_vb.addItem(curve)

            # Only valid for a sorted X axis — see _set_data_reduction.
            self._set_data_reduction(curve, x_data)

            self.plot_items[channel_name] = {"item": curve, "unit": unit,
                                              "pen_mode": pen_mode, "color": color}
            # After the record exists, not before: the legend is rebuilt from
            # plot_items, so a curve added here is only listed once it is in
            # there. This also covers the entry PlotItem.addItem() makes by
            # itself for a named curve, which would otherwise carry pyqtgraph's
            # default grey label.
            self._ensure_legend()
        else:
            rec = self.plot_items[channel_name]
            curve = rec["item"]
            old_unit = rec["unit"]
            if old_unit != unit:
                old_vb = self.unit_axes.get(old_unit, {}).get('vb', None)
                if old_vb is None:
                    try:
                        self.plot_widget.removeItem(curve)
                    except Exception:
                        pass
                else:
                    try:
                        old_vb.removeItem(curve)
                    except Exception:
                        pass
                new_vb = self.unit_axes[unit]['vb'] if unit in self.unit_axes else None
                if new_vb is None:
                    self.plot_widget.addItem(curve)
                else:
                    new_vb.addItem(curve)
                rec["unit"] = unit

            # Re-apply style whenever pen_mode changes so toggling mid-run works
            if rec.get("pen_mode") != pen_mode:
                self._apply_pen_mode(curve, rec.get("color", color), pen_mode)
                rec["pen_mode"] = pen_mode

            # Always supply the full data array; pen mode controls rendering style,
            # not which points are sent (sending only the last point caused the
            # trace to disappear when pen was toggled on mid-run).
            # The X axis can be switched mid-run, so re-check whether clipping
            # and downsampling are still safe for this data.
            self._set_data_reduction(curve, x_data)

            if pen_mode and len(y_data) > 0:
                curve.setData([x_data[-1]], [y_data[-1]])
            else:
                curve.setData(x_data, y_data)

    @staticmethod
    def _apply_pen_mode(curve, color, pen_mode: bool):
        """Switch a PlotDataItem between line mode and dot (pen) mode."""
        if pen_mode:
            # Dot mode: no connecting line, single marker at the latest point
            curve.setPen(None)
            curve.setSymbol('o')
            curve.setSymbolSize(5)
            curve.setSymbolBrush(color)
            curve.setSymbolPen(None)
        else:
            # Line mode: full trace, no markers
            curve.setPen(mkPen(color=color, width=2))
            curve.setSymbol(None)
            curve.setSymbolBrush(None)


class ChannelSettings(QGroupBox):
    # The buttons are labelled with symbols, so the tooltip is what says what
    # they do. Kept here so the main window can put it back after clearing a
    # disabled-reason tooltip.
    CALIBRATE_TIP = "View / Calibrate channels"

    def __init__(self, parent=None):
        super().__init__("Display Settings")
        self.parent = parent
        self._cal_window = None
        self.create_widgets()

    def create_widgets(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        self.clear_button = QPushButton("Clear Data")
        self.clear_button.clicked.connect(self.clear_graph)
        self.clear_button.setFixedHeight(25)
        self.clear_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._set_clear_mode(False)

        self.calibrate_button = QPushButton("View / \u2316 CH's")
        self.calibrate_button.setToolTip(self.CALIBRATE_TIP)
        self.calibrate_button.clicked.connect(self.open_calibration)
        self.calibrate_button.setFixedHeight(25)
        self.calibrate_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        # A checkable button drawn as the bare symbol: no frame, dimmed when
        # off, full strength when on. isChecked/setChecked behave as a
        # checkbox's, so readers need no change. U+270E is a plain text glyph
        # (not an emoji), so it is drawn in the theme's text colour; dimming by
        # opacity keeps that colour whichever theme is active.
        self.pen_var = QPushButton("\u270E")
        self.pen_var.setCheckable(True)
        self.pen_var.setFlat(True)
        self.pen_var.setStyleSheet(
            "QPushButton, QPushButton:checked, QPushButton:pressed "
            "{ border: none; background: transparent; padding: 0; font-size: 14pt; }")
        self.pen_var.setCursor(Qt.PointingHandCursor)
        self.pen_var.setToolTip("Pen: show only a dot at the latest point instead of the full trace")
        self.pen_var.setFixedSize(30, 25)
        self._pen_opacity = QGraphicsOpacityEffect(self.pen_var)
        self.pen_var.setGraphicsEffect(self._pen_opacity)
        self.pen_var.toggled.connect(self._show_pen_state)
        self._show_pen_state(self.pen_var.isChecked())

        layout.addWidget(self.pen_var)
        layout.addWidget(self.clear_button)
        layout.addWidget(self.calibrate_button)
        # Fixed to what the contents need: the title, the layout margins and
        # the 25 px buttons. Was a hardcoded 58, which is less than that once
        # the title's height (font, DPI and theme dependent) is counted, so Qt
        # took the shortfall out of the bottom margin and it showed as 0.
        self.setFixedHeight(self.sizeHint().height())

    PEN_OFF_OPACITY = 0.35

    def _show_pen_state(self, on):
        self._pen_opacity.setOpacity(1.0 if on else self.PEN_OFF_OPACITY)

    def _set_clear_mode(self, running):
        """Relabel the button for what it actually does in the current state.

        Stopped, it only wipes the displayed curves. Running, it also discards
        the buffered samples, which is a different enough action to deserve a
        different name.
        """
        if running:
            self.clear_button.setText("Refresh Graph")
            self.clear_button.setToolTip(
                "Discard all buffered samples and restart the graph from now.\n"
                "Acquisition and any recording in progress continue uninterrupted."
            )
        else:
            self.clear_button.setText("Clear Data")
            self.clear_button.setToolTip("Clear the plotted curves.")

    def clear_graph(self):
        """Clear the display, and while running also drop the buffered samples.

        The reset is requested rather than performed here: the ring buffer is
        written by the acquisition thread, so zeroing it from the UI thread could
        land in the middle of a block write. The acquisition loop applies it at a
        block boundary instead.
        """
        mw = self.parent
        if mw is None:
            return
        if getattr(mw, 'continue_running', False):
            mw.request_ring_reset()
        elif hasattr(mw, 'reset_ring_now'):
            mw.reset_ring_now()
        if hasattr(mw, 'graphDataWidget'):
            mw.graphDataWidget.clear_plots()

    def open_calibration(self):
        """Open the CalibrationWindow (embedded import, not subprocess)."""
        from DAQ_Calibrate import CalibrationWindow as _CalWin
        if self._cal_window is not None and self._cal_window.isVisible():
            self._cal_window.raise_()
            self._cal_window.activateWindow()
            return
        self._cal_window = _CalWin(
            settings_df=default_settings.copy(),
            custom_df=default_settings_custom.copy(),
            output_df=default_settings_output.copy(),
            digital_df=default_settings_dig.copy(),
            selected_daq=selected_daq,
            main_app=self.parent,
        )
        # Start the digital poll timer when the window opens; stop it on close
        mw = self.parent
        if mw is not None:
            self._cal_window.destroyed.connect(mw._stop_dig_poll)
            # Also hook the close event via a wrapper so the timer stops
            # even if the user just hides the window rather than destroying it.
            orig_close = self._cal_window.closeEvent
            def _on_close(event, _orig=orig_close):
                mw._stop_dig_poll()
                _orig(event)
                # The window is still visible until closeEvent returns, so
                # Start Recording is re-evaluated on the next loop pass.
                if event.isAccepted():
                    QTimer.singleShot(0, mw._refresh_record_button)
            self._cal_window.closeEvent = _on_close
            mw._start_dig_poll()
        self._cal_window.show()
        # After show(), not before: _notify_cal_window_app_state() early-returns
        # on a window that is not yet visible, so gating applied here was
        # silently dropped and the window opened with stale button states.
        if mw is not None:
            mw._notify_cal_window_app_state()
            mw._refresh_record_button()

    def calibration_open(self):
        """True while the View / Calibrate window is on screen."""
        win = self._cal_window
        try:
            return win is not None and win.isVisible()
        except RuntimeError:        # the window was deleted on close
            return False


    def edit_settings(self):
        # When the user saves, reload defaults and refresh the main UI.
        _exec_settings_dialog(self, self._reload_and_refresh)

        # Reset the DAQ after the settings dialog closes so any previously
        # reserved tasks/channels are fully released before the next run.
        if self.parent and hasattr(self.parent, 'reset_device'):
            try:
                self.parent.reset_device()
            except Exception as e:
                print(f"DAQ reset on settings close: {e}")

    def load_settings_file(self):
        """Pick a settings .xlsx, validate it, and (on confirm) make it the
        active configuration: copy it over the working settings file, reload
        the globals, and refresh the UI."""
        # Don't swap settings out from under a running acquisition.
        mw = self.parent
        if mw is not None and getattr(mw, "continue_running", False):
            QMessageBox.warning(
                self, "Stop First",
                "Please stop the current acquisition before loading a settings file.")
            return

        path, _ = QFileDialog.getOpenFileName(
            self, "Load Settings File", "",
            "Excel settings files (*.xlsx *.xls);;All files (*)")
        if not path:
            return

        ok, msg = _validate_settings_file(path)
        if not ok:
            QMessageBox.critical(self, "Invalid Settings File", msg)
            return

        dest = _get_settings_path()
        try:
            same = os.path.abspath(path) == os.path.abspath(dest)
        except Exception:
            same = False

        reply = QMessageBox.question(
            self, "Load Settings",
            "Load and apply this settings file?\n\n"
            f"{path}\n\n"
            "It becomes the active configuration"
            + ("." if same else f" and replaces:\n{dest}"),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if reply != QMessageBox.Yes:
            return

        try:
            _make_settings_active(path, self.parent)
        except OSError as e:
            QMessageBox.critical(
                self, "Load Failed",
                f"Could not copy the settings file into place.\n\nDetails: {e}")
            return
        except Exception as e:
            QMessageBox.critical(
                self, "Load Failed",
                f"The file was copied but could not be applied.\n\nDetails: {e}")
            return

        # Release any reserved tasks/channels so the next run starts clean.
        if self.parent and hasattr(self.parent, 'reset_device'):
            try:
                self.parent.reset_device()
            except Exception as e:
                print(f"DAQ reset on settings load: {e}")

        print(f"Settings loaded and active: {path}")

    def _reload_and_refresh(self):
        if _reload_settings_globals():
            _apply_reloaded_settings(self.parent)


class _GraphTabBar(QTabBar):
    """Graph tab bar that draws the first tab, Live Graph, in bold green.

    QTabBar has no per-tab font, so that tab is painted here with its own font
    and text colour, and sized for the bold text.
    """
    LIVE_COLOR = QColor("#2e9e3e")

    def _live_font(self):
        font = QFont(self.font())
        font.setBold(True)
        return font

    def tabSizeHint(self, index):
        size = super().tabSizeHint(index)
        if index == 0:
            text = self.tabText(0)
            extra = (QFontMetrics(self._live_font()).horizontalAdvance(text)
                     - self.fontMetrics().horizontalAdvance(text))
            size.setWidth(size.width() + max(0, extra))
        return size

    def paintEvent(self, _event):
        painter = QStylePainter(self)
        # The current tab last, so it overlaps its neighbours as QTabBar does.
        order = [i for i in range(self.count()) if i != self.currentIndex()]
        if self.currentIndex() >= 0:
            order.append(self.currentIndex())
        for i in order:
            opt = QStyleOptionTab()
            self.initStyleOption(opt, i)
            if i == 0:
                font = self._live_font()
                painter.save()
                painter.setFont(font)
                opt.fontMetrics = QFontMetrics(font)
                for role in (QPalette.WindowText, QPalette.ButtonText):
                    opt.palette.setColor(role, self.LIVE_COLOR)
                painter.drawControl(QStyle.CE_TabBarTab, opt)
                painter.restore()
            else:
                painter.drawControl(QStyle.CE_TabBarTab, opt)


class PlotSettings(QGroupBox):
    # Axis range modes offered in the Axis Ranges dialog. Every axis offers
    # Auto and Custom; between them come the presets that axis has:
    #   * channel axes: "Widest Range" spans the lowest min to the highest max
    #     of every channel on the axis, "Narrowest Range" is the single
    #     smallest channel span, both from the card range scaled by Mx/b.
    #     A custom channel's unit or an output channel on X has no card range,
    #     so no presets.
    #   * Samples / Run Duration: "Duration to Plot" (only when that field is
    #     filled) and "Duration to Acquire", each as the window actually shown.
    LIVE_NOTES_HINT = "Optional, shown on the graph; clears at Stop"

    RANGE_AUTO      = "Auto"
    RANGE_WIDEST    = "Widest Range"
    RANGE_NARROWEST = "Narrowest Range"
    RANGE_PLOT      = "Duration to Plot"
    RANGE_ACQUIRE   = "Duration to Acquire"
    RANGE_CUSTOM    = "Custom"

    def __init__(self, parent=None):
        super().__init__("Session Settings")
        self.parent = parent
        # Choices made in the Axis Ranges dialog, kept for the life of this
        # widget so the dialog reopens as it was left. Keyed ('y', unit) or
        # ('x', x-axis selection); each value is {'mode', 'min', 'max'}, with
        # min/max set only for Custom (the other modes are recomputed on open).
        # The Y units start from the settings file's Axis Ranges defaults.
        self.axis_ranges = {('y', unit): choice
                            for unit, choice in load_axis_range_defaults().items()}
        # Channel and X-axis selection locked (while acquiring, or while runs
        # are held so every run has the same channels). See set_selection_locked.
        self._selection_locked = False
        self.create_widgets()

    def create_widgets(self):
        self.tabs = QTabWidget()
        layout = QVBoxLayout(self)
        layout.addWidget(self.tabs)

        self.plotSettings1 = QWidget()
        self.graphConfig = QWidget()
        self.saveSettings = QWidget()

        # A wrench stands in for "Settings" to keep the Run tab narrow.
        self.tabs.addTab(self.plotSettings1, "Channels")
        self.tabs.addTab(self.graphConfig, "Plots")
        self.tabs.addTab(self.saveSettings, "Run Settings")

        self.setup_plot_settings1()
        self.setup_graph_config()
        self.setup_input_settings()

    def setup_plot_settings1(self):
        layout = QGridLayout(self.plotSettings1)
        # 4, not 5. Column 0 holds the bold module name (105 px for a name like
        # 'cDAQ1Mod1') and each 'ai0' checkbox is 51 px, so 5 columns needed
        # 435 px — past the 400 px panel cap, and the rightmost column was being
        # clipped. 4 columns need 378 px and cost nothing in height: 8 channels
        # per module wrap as 4+4 instead of 5+3, the same two rows either way.
        CHAN_COLS = 4           # channel checkboxes per row (label sits in col 0)

        # Group the Y-axis channels by device, custom channels under "Custom".
        # Each channel keeps its ORIGINAL plot_options index so the plotting code
        # that looks up plot_check_box{i} still works. That name is also what the
        # app modules reach for (DAQ_FlowFailure, DAQ_ProportionalSweep), so it
        # is the one to keep — a second alias for the same QCheckBox used to be
        # published as plot_check_var{i} and was easy to update inconsistently.
        groups = {}          # device -> list of (i, row)
        group_order = []
        for i, row in plot_options.iterrows():
            dev = ""
            try:
                if 'DAQ' in row and pd.notna(row['DAQ']) and str(row['DAQ']).strip():
                    dev = str(row['DAQ']).strip()
            except Exception:
                dev = ""
            if not dev:
                dev = "Custom"
            if dev not in groups:
                groups[dev] = []
                group_order.append(dev)
            groups[dev].append((i, row))

        r = 0
        layout.addWidget(QLabel("Select Y-Axis"), r, 0, 1, CHAN_COLS + 1); r += 1

        for dev in group_order:
            hdr = QLabel(dev)
            hdr.setStyleSheet("font-weight: bold;")
            hdr.setAlignment(Qt.AlignLeft | Qt.AlignTop)
            layout.addWidget(hdr, r, 0)     # label on the group's first row, col 0
            col = 1
            for i, row in groups[dev]:
                plot_check_box = QCheckBox(row['Channel'])
                plot_check_box.setToolTip(str(row['Device Name']))
                setattr(self, f"plot_check_box{i}", plot_check_box)
                layout.addWidget(plot_check_box, r, col)
                # Apps listing plotted channels (Custom Triggers) follow this.
                plot_check_box.toggled.connect(
                    lambda *_: self.parent.session_state_changed.emit())
                col += 1
                if col > CHAN_COLS:         # wrap to next row, channels still start at col 1
                    col = 1
                    r += 1
            if col != 1:
                r += 1                      # advance past a partially filled row

        # X-axis / axis ranges, placed after the (variable-height) Y section.
        layout.addWidget(QLabel("Select X-Axis"), r, 0, 1, 3); r += 1
        self.Non_chan_list = ["Run Duration", "Samples"]
        self.chan_list = default_settings["Device Name"].tolist()
        self.out_chan_list = default_settings_output["Device Name"].tolist()
        self.x_axis_list = self.Non_chan_list + self.chan_list + self.out_chan_list

        self.x_axis_selection = QComboBox()
        # Items are full device names, and by default a QComboBox is as wide as
        # its longest one, which dragged the whole panel out. Cap the width used
        # for sizing; the popup list still shows every name in full.
        self.x_axis_selection.setSizeAdjustPolicy(
            QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.x_axis_selection.setMinimumContentsLength(12)
        self.x_axis_selection.addItems(self.x_axis_list)
        self.x_axis_selection.setCurrentText("Samples")
        # Across every column of the grid, like the "Select Y-Axis" heading.
        layout.addWidget(self.x_axis_selection, r, 0, 1, CHAN_COLS + 1); r += 1

        self.x_axis_selection.currentTextChanged.connect(self.on_x_axis_selection_change)
        self.x_axis_selection.currentTextChanged.connect(
            lambda *_: self.parent.session_state_changed.emit())

        self.axis_ranges_button = QPushButton("Axis Ranges")
        self.axis_ranges_button.clicked.connect(self.open_axis_ranges)
        layout.addWidget(self.axis_ranges_button, r, 0, 1, 3)
        # Off: Run autoranges every axis and the Axis Ranges choices sit unused.
        self.apply_ranges_var = QCheckBox("Apply on Run")
        self.apply_ranges_var.toggled.connect(self.axis_ranges_button.setEnabled)
        layout.addWidget(self.apply_ranges_var, r, 3, 1, 2)
        self.axis_ranges_button.setEnabled(self.apply_ranges_var.isChecked())

    def setup_graph_config(self):
        layout = QGridLayout(self.graphConfig)
        layout.addWidget(QLabel("Graph Title"), 0, 0)
        self.title_label_entry = QLineEdit()
        layout.addWidget(self.title_label_entry, 1, 0)

        layout.addWidget(QLabel("Graph Notes"), 2, 0)
        self.notes_entry = QLineEdit()
        self.notes_entry.setPlaceholderText(self.LIVE_NOTES_HINT)
        # Shown only while a Run tab is selected: there the field edits that
        # run's notes, kept only when applied.
        self.apply_notes_button = QPushButton("Apply Notes")
        self.apply_notes_button.setVisible(False)
        self.apply_notes_button.clicked.connect(lambda: self.parent.apply_run_notes())
        notes_row = QHBoxLayout()
        notes_row.setContentsMargins(0, 0, 0, 0)
        notes_row.addWidget(self.notes_entry)
        notes_row.addWidget(self.apply_notes_button)
        layout.addLayout(notes_row, 3, 0)

        # Overlay: a saved data file drawn on Live Graph for reference.
        self.overlay_entry = QLineEdit()
        self.overlay_entry.setReadOnly(True)
        self.overlay_entry.setPlaceholderText("Display only; choose a data file with Browse\u2026")
        self.overlay_browse_button = QPushButton("Browse\u2026")
        self.overlay_show_var = QCheckBox("Show")
        self.overlay_remove_button = QPushButton("Remove")
        # "Overlay" on the left with Show at the far right; the file row below.
        overlay_head = QHBoxLayout()
        overlay_head.setContentsMargins(0, 0, 0, 0)
        overlay_head.addWidget(QLabel("Overlay"))
        overlay_head.addStretch(1)
        overlay_head.addWidget(self.overlay_show_var)
        layout.addLayout(overlay_head, 4, 0)
        overlay_row = QHBoxLayout()
        overlay_row.setContentsMargins(0, 0, 0, 0)
        overlay_row.addWidget(self.overlay_entry, 1)
        overlay_row.addWidget(self.overlay_browse_button)
        overlay_row.addWidget(self.overlay_remove_button)
        layout.addLayout(overlay_row, 5, 0)
        self.overlay_browse_button.clicked.connect(lambda: self.parent.browse_overlay())
        self.overlay_show_var.clicked.connect(
            lambda checked: self.parent.apply_overlay(report=checked))
        self.overlay_remove_button.clicked.connect(lambda: self.parent.remove_overlay())

        layout.setRowStretch(6, 1)      # keep the fields at the top of the tab

        # The main window owns the run state these feed; looked up when fired,
        # since refresh_ui() rebuilds this panel.
        self.title_label_entry.editingFinished.connect(
            lambda: self.parent.on_title_edited())
        self.notes_entry.textChanged.connect(
            lambda t: self.parent.on_notes_changed(t))

    # ------------------------------------------------------------------
    # Axis ranges
    # ------------------------------------------------------------------
    @staticmethod
    def _scaled_card_range(row):
        """(low, high) of an input row's card range after its Mx/b scaling.

        'Min'/'Max' are the card range in 'Range Unit'; Mx*v + b puts them in
        the channel's engineering unit. Sorted, since a negative Mx flips them.
        None when the row has no usable numbers.
        """
        try:
            ends = [float(row['Mx']) * float(row[c]) + float(row['b'])
                    for c in ('Min', 'Max')]
        except (KeyError, TypeError, ValueError):
            return None
        if not all(np.isfinite(ends)):
            return None
        return (min(ends), max(ends))

    @classmethod
    def _card_presets(cls, spans):
        """Widest/Narrowest presets for a channel axis; {} with no card range.

        `spans` is the list of scaled card spans on the axis, or None when no
        card is behind it. A preset whose numbers could not be read is None.
        """
        if spans is None:
            return {}
        if not spans:
            return {cls.RANGE_WIDEST: None, cls.RANGE_NARROWEST: None}
        return {
            cls.RANGE_WIDEST: (min(s[0] for s in spans), max(s[1] for s in spans)),
            cls.RANGE_NARROWEST: min(spans, key=lambda s: s[1] - s[0]),
        }

    @staticmethod
    def _shown_samples(seconds, rate):
        """Samples actually on screen for a window of `seconds`.

        Max Plot Points caps it unless Decimate To Fit keeps the whole window.
        """
        full_window = int(seconds * rate)
        if decimate_to_fit or not max_plot_points:
            return full_window
        return int(min(full_window, max_plot_points))

    def plot_window(self, rate):
        """(plot_duration_s, samples_to_plot) for the current Run Settings.

        Duration to Plot, blank or unreadable meaning the whole recording, and
        never longer than the recording.
        Raises ValueError when Duration to Acquire is not a number.
        """
        recording = self.recording_duration_seconds()
        try:
            plot_dur = self.plot_duration_seconds()
        except ValueError:
            plot_dur = None
        plot_duration = min(recording if plot_dur is None else plot_dur, recording)
        return plot_duration, self._shown_samples(plot_duration, rate)

    def _window_presets(self, x_name):
        """Duration to Plot / Duration to Acquire presets for Samples or Run Duration.

        Each is the window as it would be shown: Duration to Plot never longer
        than Duration to Acquire (as at Run), then capped by Max Plot Points
        through _shown_samples. Run Duration is that sample count in seconds,
        so it matches the time axis. Duration to Plot is left out while its
        field is blank or not a number.
        """
        try:
            rate = float(sample_rate)
        except (TypeError, ValueError):
            rate = None
        try:
            acquire = self.recording_duration_seconds()
        except ValueError:
            acquire = None
        try:
            plot = self.plot_duration_seconds()
        except ValueError:
            plot = None

        windows = {}
        if plot is not None:
            windows[self.RANGE_PLOT] = plot if acquire is None else min(plot, acquire)
        windows[self.RANGE_ACQUIRE] = acquire

        presets = {}
        for mode, seconds in windows.items():
            if not rate or not seconds or seconds <= 0:
                presets[mode] = None
                continue
            samples = self._shown_samples(seconds, rate)
            presets[mode] = (0.0, float(samples) if x_name == "Samples" else samples / rate)
        return presets

    def _y_unit_presets(self):
        """unit -> presets, for every Y unit in plot_options order."""
        spans = {unit_key(u): None for u in plot_options['Units']}
        for _, row in default_settings.iterrows():
            unit = unit_key(row['Units'])
            if spans.get(unit) is None:
                spans[unit] = []
            span = self._scaled_card_range(row)
            if span is not None:
                spans[unit].append(span)
        return {unit: self._card_presets(s) for unit, s in spans.items()}

    def _x_unit_presets(self, x_name):
        """(unit label, presets) for the X axis selection."""
        if x_name == "Samples":
            return "Samples", self._window_presets(x_name)
        if x_name == "Run Duration":
            return "s", self._window_presets(x_name)

        inp = default_settings[default_settings['Device Name'] == x_name]
        if not inp.empty:
            span = self._scaled_card_range(inp.iloc[0])
            return (unit_key(inp['Units'].iloc[0]),
                    self._card_presets([span] if span is not None else []))

        out = default_settings_output[default_settings_output['Device Name'] == x_name]
        unit = unit_key(out['Units'].iloc[0]) if not out.empty else ""
        return unit, {}

    @classmethod
    def range_modes(cls, presets):
        """The dropdown entries for an axis with these presets, in order."""
        return [cls.RANGE_AUTO, *presets, cls.RANGE_CUSTOM]

    def _range_choice(self, key, presets):
        """The stored choice for axis `key`, or its default.

        Channel axes default to Widest Range, everything else to Auto. A stored
        mode the axis no longer offers (Duration to Plot since cleared, say)
        falls back to that default too.
        """
        default_mode = (self.RANGE_WIDEST if self.RANGE_WIDEST in presets
                        else self.RANGE_AUTO)
        choice = self.axis_ranges.get(key)
        if choice is None or choice['mode'] not in self.range_modes(presets):
            return {'mode': default_mode, 'min': None, 'max': None}
        return choice

    def _resolve_range(self, key, presets):
        """(lo, hi) axis `key` should start a Run at, or None to autorange."""
        choice = self._range_choice(key, presets)
        if choice['mode'] == self.RANGE_CUSTOM:
            return (choice['min'], choice['max'])
        rng = presets.get(choice['mode'])
        # A zero-width span cannot be shown as a range, so that axis follows
        # the data instead.
        return rng if rng is not None and rng[1] > rng[0] else None

    def output_x_lock(self):
        """(lo, hi) to lock X to when an output channel is on X, else None.

        The output ring buffer is never written (AO is output-only), so
        autorange on its flat-zero data gives a meaningless span like
        [-5000, 5000]. Such an X axis is locked to the channel's configured
        Min/Max instead, with 5% either side, wherever it would autorange.
        """
        x_name = self.x_axis_selection.currentText()
        out = default_settings_output[default_settings_output['Device Name'] == x_name]
        if out.empty:
            return None
        lo, hi = float(out['Min'].iloc[0]), float(out['Max'].iloc[0])
        pad = 0.05 * (hi - lo)
        return (lo - pad, hi + pad)

    def run_axis_ranges(self):
        """(x_range, {unit: y_range}) from the Axis Ranges choices.

        Used at Run when "Apply on Run" is ticked. Each range is (lo, hi) or
        None for autorange; an output channel on X on Auto gets
        output_x_lock() rather than autoranging.
        """
        y_ranges = {unit: self._resolve_range(('y', unit), presets)
                    for unit, presets in self._y_unit_presets().items()}
        x_name = self.x_axis_selection.currentText()
        _, x_presets = self._x_unit_presets(x_name)
        x_range = self._resolve_range(('x', x_name), x_presets)
        if (x_range is None
                and self._range_choice(('x', x_name), x_presets)['mode'] == self.RANGE_AUTO):
            x_range = self.output_x_lock()
        return x_range, y_ranges

    def open_axis_ranges(self):
        """Open the Axis Ranges dialog and keep what the user accepts."""
        def spec(key, label, presets):
            return {'key': key, 'label': label, 'presets': presets,
                    'stored': self._range_choice(key, presets)}

        rows = [spec(('y', unit), f"Y axis - ({unit}) range", presets)
                for unit, presets in self._y_unit_presets().items()]
        x_name = self.x_axis_selection.currentText()
        x_unit, x_presets = self._x_unit_presets(x_name)
        rows.append(spec(('x', x_name), f"X axis - ({x_unit}) range", x_presets))

        dlg = AxisRangesDialog(rows, self)
        if dlg.exec_() == QDialog.Accepted:
            self.axis_ranges.update(dlg.results)

    def on_x_axis_selection_change(self, text):
        # The channel used as X cannot also be a Y curve.
        for i, row in plot_options.iterrows():
            if row["Device Name"] == text:
                getattr(self, f"plot_check_box{i}").setChecked(False)
        self._apply_selection_lock()

    def set_selection_locked(self, locked):
        """Lock or unlock the Y-channel checkboxes and the X-axis selection."""
        self._selection_locked = locked
        self._apply_selection_lock()

    def _apply_selection_lock(self):
        x_name = self.x_axis_selection.currentText()
        self.x_axis_selection.setEnabled(not self._selection_locked)
        for i, row in plot_options.iterrows():
            getattr(self, f"plot_check_box{i}").setEnabled(
                not self._selection_locked and row["Device Name"] != x_name)

    # Both imported from DAQ_Settings, which writes the saved unit strings.
    _DURATION_UNITS   = DURATION_UNITS
    _DURATION_FACTORS = DURATION_FACTORS

    @staticmethod
    def _expert_duration(value, unit, fallback_value, fallback_unit="s"):
        """(text, unit) to start a duration field at, from expert settings.

        Falls back when there is no workbook, or the setting is absent. The unit
        is only honoured if this build knows it, so a hand-edited workbook
        cannot leave a dropdown showing something that is not in the list.
        """
        text = "" if value is None else str(value).strip()
        if text.lower() == "nan":
            text = ""
        if value is None:
            text = fallback_value
        u = "" if unit is None else str(unit).strip()
        return text, (u if u in DURATION_FACTORS else fallback_unit)

    def recording_duration_seconds(self):
        """Duration to Acquire converted to seconds (value x selected unit).
        Raises ValueError on blank/invalid text, exactly like float() did."""
        val = float(self.recording_duration_entry.text())
        return val * self._DURATION_FACTORS[self.recording_duration_unit.currentText()]

    def plot_duration_seconds(self):
        """Duration to Plot in seconds, or None if blank (=> use full recording)."""
        txt = self.plot_duration_entry.text().strip()
        if not txt:
            return None
        return float(txt) * self._DURATION_FACTORS[self.plot_duration_unit.currentText()]

    def setup_input_settings(self):
        layout = QGridLayout(self.saveSettings)

        # Both fields start from Expert Settings rather than a literal here, so
        # the value a site normally runs at is configured in one place. They stay
        # editable — this only sets what they open at.
        acq_text, acq_unit = self._expert_duration(
            duration_acquire, duration_acquire_unit, "180")
        plot_text, plot_unit = self._expert_duration(
            duration_plot, duration_plot_unit, "")

        # Columns: label | entry | unit. Rows that are not a label/entry pair
        # span all three.
        layout.addWidget(QLabel("Duration to Acquire:"), 0, 0)
        self.recording_duration_entry = QLineEdit(acq_text)
        layout.addWidget(self.recording_duration_entry, 0, 1)
        self.recording_duration_unit = QComboBox()
        self.recording_duration_unit.addItems(self._DURATION_UNITS)
        self.recording_duration_unit.setCurrentText(acq_unit)
        layout.addWidget(self.recording_duration_unit, 0, 2)

        layout.addWidget(QLabel("Duration to Plot:"), 1, 0)
        self.plot_duration_entry = QLineEdit(plot_text)
        self.plot_duration_entry.setPlaceholderText("Blank = full duration")
        self.plot_duration_entry.setToolTip("Leave blank to plot the full duration")
        layout.addWidget(self.plot_duration_entry, 1, 1)
        self.plot_duration_unit = QComboBox()
        self.plot_duration_unit.addItems(self._DURATION_UNITS)
        self.plot_duration_unit.setCurrentText(plot_unit)
        layout.addWidget(self.plot_duration_unit, 1, 2)
        layout.setColumnStretch(1, 1)

        # Their own row rather than the grid's columns, so the label column and
        # this text do not compete for the panel's capped width.
        self.auto_stop_var = QCheckBox("Autostop")
        self.hold_runs_var = QCheckBox("Hold Run to Session Memory")
        checks_row = QHBoxLayout()
        checks_row.setContentsMargins(0, 0, 0, 0)
        checks_row.addWidget(self.auto_stop_var)
        checks_row.addSpacing(12)
        checks_row.addWidget(self.hold_runs_var)
        checks_row.addStretch(1)
        layout.addLayout(checks_row, 2, 0, 1, 3)
        self.hold_runs_var.clicked.connect(
            lambda checked: self.parent.on_hold_clicked(checked))

        # The name the next held recording will get. Display only.
        self.next_run_entry = QLineEdit()
        self.next_run_entry.setReadOnly(True)
        self.next_run_entry.setEnabled(False)
        layout.addWidget(self.next_run_entry, 3, 0, 1, 3)

        # Session Autosave: the whole session (settings + every held run),
        # rewritten to one .mcz whenever the runs change — the same file
        # File -> Save Session writes. Usable only with Hold on (see
        # _refresh_autosave_controls).
        self.autosave_var = QCheckBox("Session Autosave")
        self.autosave_path_entry = QLineEdit()
        self.autosave_path_entry.setReadOnly(True)
        self.autosave_path_entry.setPlaceholderText("Choose a session file with Browse\u2026")
        self.autosave_browse_button = QPushButton("Browse\u2026")
        autosave_row = QHBoxLayout()
        autosave_row.setContentsMargins(0, 0, 0, 0)
        autosave_row.addWidget(self.autosave_var)
        autosave_row.addWidget(self.autosave_path_entry, 1)
        autosave_row.addWidget(self.autosave_browse_button)
        layout.addLayout(autosave_row, 4, 0, 1, 3)
        layout.setRowStretch(5, 1)      # keep the rows at the top of the tab
        for w in (self.autosave_var, self.autosave_browse_button):
            w.setEnabled(False)         # Hold starts off
        self.autosave_var.clicked.connect(
            lambda checked: self.parent.on_autosave_clicked(checked))
        self.autosave_browse_button.clicked.connect(
            lambda: self.parent.browse_autosave_path())
        # Any change to Hold or Session Autosave, by the user or by code.
        for sig in (self.hold_runs_var.toggled, self.autosave_var.toggled,
                    self.autosave_path_entry.textChanged):
            sig.connect(lambda *_: self.parent.session_state_changed.emit())


class AxisRangesDialog(QDialog):
    """Modal editor for the per-axis range choices PlotSettings keeps.

    One row per axis: a mode dropdown and Min/Max fields. The fields are only
    editable for Custom, which opens blank; a preset shows the numbers it
    stands for; Auto is blank. This only records choices — nothing here
    touches the plot. `rows` are dicts with 'key', 'label', 'presets'
    ({mode: (lo, hi) or None}) and 'stored' ({'mode', 'min', 'max'}).
    """

    def __init__(self, rows, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Axis Ranges")
        self.setModal(True)
        self.results = {}
        self._rows = []

        grid = QGridLayout()
        for c, text in enumerate(("Axis", "Range", "Min", "Max")):
            hdr = QLabel(text)
            hdr.setStyleSheet("font-weight: bold;")
            grid.addWidget(hdr, 0, c)

        for r, spec in enumerate(rows, start=1):
            combo = QComboBox()
            combo.addItems(PlotSettings.range_modes(spec['presets']))
            min_edit, max_edit = QLineEdit(), QLineEdit()
            for edit in (min_edit, max_edit):
                validator = QDoubleValidator(edit)
                validator.setLocale(QLocale.c())    # '.' decimal, whatever the OS locale
                edit.setValidator(validator)
                edit.setMaximumWidth(100)
            row = dict(spec, combo=combo, min=min_edit, max=max_edit)
            self._rows.append(row)

            grid.addWidget(QLabel(spec['label']), r, 0)
            grid.addWidget(combo, r, 1)
            grid.addWidget(min_edit, r, 2)
            grid.addWidget(max_edit, r, 3)

            stored = spec['stored']
            combo.setCurrentText(stored['mode'])
            self._fill(row, (stored['min'], stored['max']))
            combo.currentTextChanged.connect(partial(self._on_mode_changed, row))

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(grid)
        layout.addWidget(buttons)

    def _on_mode_changed(self, row, _text):
        self._fill(row, (None, None))      # switching to Custom starts blank

    @staticmethod
    def _fill(row, custom):
        """Show the row's Min/Max for its current mode; `custom` is used for Custom."""
        mode = row['combo'].currentText()
        if mode == PlotSettings.RANGE_CUSTOM:
            values = custom
        else:
            values = row['presets'].get(mode) or (None, None)
        editable = mode == PlotSettings.RANGE_CUSTOM
        for edit, v in zip((row['min'], row['max']), values):
            edit.setText("" if v is None else f"{v:g}")
            edit.setEnabled(editable)

    def accept(self):
        results = {}
        for row in self._rows:
            mode = row['combo'].currentText()
            lo = hi = None
            if mode == PlotSettings.RANGE_CUSTOM:
                try:
                    lo = float(row['min'].text())
                    hi = float(row['max'].text())
                except ValueError:
                    QMessageBox.warning(self, "Axis Ranges",
                                        f"Enter both a Min and a Max for {row['label']}.")
                    return
                if lo >= hi:
                    QMessageBox.warning(self, "Axis Ranges",
                                        f"Min must be less than Max for {row['label']}.")
                    return
            results[row['key']] = {'mode': mode, 'min': lo, 'max': hi}
        self.results = results
        super().accept()


class OutputSettings(QGroupBox):
    def __init__(self, parent=None):
        super().__init__("Session Actions")
        self.parent = parent
        self.create_widgets()

    def create_widgets(self):
        outer = QVBoxLayout(self)

        top_row = QHBoxLayout()
        top_row.setSpacing(8)

        self.start_button = QPushButton("Run")
        self.start_button.clicked.connect(self.start_task)

        self.record_button = QPushButton("Start Recording")
        self.record_button.setEnabled(False)
        self.record_button.clicked.connect(self.start_recording)

        # Takes Start Recording's place while the All Runs tab is shown.
        self.delete_all_button = QPushButton("Delete All Runs")
        self.delete_all_button.setVisible(False)
        self.delete_all_button.clicked.connect(
            lambda: self.parent.delete_all_runs() if self.parent else None)
        self.delete_all_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        self.stop_button = QPushButton("Stop")
        self.stop_button.clicked.connect(self.stop_task)

        # Run and Stop are paired opposites and sit together. Start Recording
        # takes its own full-width row: its label needs 140 px on its own, so
        # three buttons abreast forced a 368 px minimum on the whole left panel
        # — more than the channel grid ever asked for. Splitting it drops this
        # group's minimum to 250 px.
        for btn in (self.start_button, self.stop_button):
            btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            btn.setMinimumWidth(110)
        self.record_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        top_row.addWidget(self.start_button)
        top_row.addWidget(self.stop_button)
        outer.addLayout(top_row)
        outer.addWidget(self.record_button)
        outer.addWidget(self.delete_all_button)

        outer.setSpacing(10)
        outer.setContentsMargins(10, 10, 10, 10)

    # Run/Stop read "Re-Run"/"Delete Run" while a held run's tab is shown, so
    # the main window decides what a click means.
    def start_task(self):
        if self.parent: self.parent.on_run_button()
    def stop_task(self):
        if self.parent: self.parent.on_stop_button()
    def start_recording(self):
        if self.parent: self.parent.start_recording()


class LogWidget(QGroupBox):
    """Replaces the Functions panel — captures all print() output in a scrollable log."""

    def __init__(self, parent=None):
        super().__init__("Log")
        self.parent = parent
        self._original_stdout = sys.stdout
        self.create_widgets()
        self._setup_redirect()

    def create_widgets(self):
        layout = QVBoxLayout(self)
        self.text_area = QTextEdit()
        self.text_area.setReadOnly(True)
        font = self.text_area.font()
        font.setFamily("Courier New")
        font.setPointSize(8)
        self.text_area.setFont(font)
        # Themes may treat the Log as a second instrument readout. Set directly
        # on the widget so the application stylesheet does not override it.
        _log_bg, _log_fg = daq_theme.log_colors(theme_name)
        if _log_bg:
            self.text_area.setStyleSheet(
                f"QTextEdit {{ background-color: {_log_bg}; color: {_log_fg};"
                f" border: 1px solid #5a5a5a; }}"
            )
        layout.addWidget(self.text_area)

        clear_btn = QPushButton("Clear Log")
        clear_btn.clicked.connect(self.text_area.clear)
        layout.addWidget(clear_btn)

    def _setup_redirect(self):
        self._stream = LogStream()
        self._stream.message.connect(self._append)
        sys.stdout = self._stream

    def _append(self, text):
        ts = datetime.now().strftime("%H:%M:%S")
        self.text_area.append(f"[{ts}]  {text}")
        sb = self.text_area.verticalScrollBar()
        sb.setValue(sb.maximum())

    def restore_stdout(self):
        sys.stdout = self._original_stdout




class Apps(QGroupBox):
    def __init__(self, parent=None):
        super().__init__("Apps")
        self.parent = parent
        self.create_widgets()

    def create_widgets(self):
        layout = QHBoxLayout(self)
        # One button per registered app, built from DAQ_AppRegistry. Nothing
        # here names an individual app, so adding an entry to APP_MODULES is
        # enough to put a working button on the bar.
        self._app_buttons = {}
        self._app_dialogs = {}
        for spec in app_registry.load_apps():
            btn = QPushButton(spec['label'])
            if spec['color']:
                hover = spec['hover'] or spec['color']
                btn.setStyleSheet(
                    "QPushButton { background-color: %s; color: white; font-weight: bold; }"
                    "QPushButton:hover { background-color: %s; }"
                    "QPushButton:disabled { background-color: #888; color: #ccc; }"
                    % (spec['color'], hover)
                )
            btn.clicked.connect(partial(self.open_app, spec['id']))
            layout.addWidget(btn)
            self._app_buttons[spec['id']] = btn
            self._app_dialogs[spec['id']] = None

        # Surface a failed import on the bar itself, so a broken app file shows
        # up as a disabled button rather than as an app that silently vanished.
        for _name, _why in app_registry.load_failures():
            bad = QPushButton("%s (failed)" % _name)
            bad.setEnabled(False)
            bad.setToolTip("This app failed to load — see the Log for details")
            layout.addWidget(bad)

        # Shortcuts: buttons that start an outside program. Read fresh on every
        # build (unlike apps, which load once), so a saved change shows as soon
        # as Settings rebuilds the window. Only shown ones get a button; they
        # take no part in the app interlocks, since what the program does with
        # the DAQ is unknown here.
        self._shortcut_buttons = {}
        for sc in app_registry.read_shortcuts():
            if not sc['show']:
                continue
            btn = QPushButton(sc['label'])
            btn.setToolTip(sc['path'])
            btn.clicked.connect(partial(self.launch_shortcut, sc))
            layout.addWidget(btn)
            self._shortcut_buttons[sc['key']] = btn

        # Hide any app the user unticked in Settings → App Settings → App Bar.
        self.apply_app_bar_visibility()

        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.setFixedHeight(self.sizeHint().height())

    # ------------------------------------------------------------------
    # Registry accessors
    # ------------------------------------------------------------------

    def app_button(self, app_id):
        """The Apps-bar button for an app id, or None if not registered."""
        return self._app_buttons.get(app_id)

    def app_dialog(self, app_id):
        """The app's dialog if it has been built, else None.

        None means "never opened this session"; it does not mean the app is
        unavailable. Use app_button() to ask about availability.
        """
        return self._app_dialogs.get(app_id)

    def open_app_ids(self):
        """ids of every app whose dialog is currently open."""
        return {aid for aid, dlg in self._app_dialogs.items()
                if dlg is not None and dlg.isVisible()}

    def apply_app_bar_visibility(self, vis=None):
        """Show/hide each Apps-bar button per the saved settings. If every
        app is hidden, and there are no shortcuts, the whole Apps group box is
        hidden too."""
        if vis is None:
            vis = load_app_bar_visibility()
        any_shown = bool(self._shortcut_buttons)
        for key, btn in self._app_buttons.items():
            shown = vis.get(key, True)
            btn.setVisible(shown)
            any_shown = any_shown or shown
        if not any_shown:
            self.setVisible(False)
        else:
            # Only call show() once we actually have a parent — a parentless
            # QGroupBox made visible would pop up as its own window.
            if self.parentWidget() is not None:
                self.setVisible(True)
            self.setFixedHeight(self.sizeHint().height())

    def launch_shortcut(self, sc):
        """Start a shortcut's program in its own folder, independent of this
        app — it keeps running if GioDAQ is closed."""
        path = sc['path']
        if not os.path.isfile(path):
            QMessageBox.warning(self, sc['label'],
                                f"Program not found:\n{path}\n\n"
                                "Fix or remove the shortcut in Settings → "
                                "App Settings → App Bar.")
            return
        try:
            os.startfile(path, cwd=os.path.dirname(path))
        except Exception as e:
            QMessageBox.warning(self, sc['label'],
                                f"Could not start:\n{path}\n\n{e}")
            return
        print(f"Started {sc['label']}: {path}")

    def _app_context(self):
        """Everything an app's build() might need, in one object.

        The dialog signatures are not uniform, so each app module converts this
        into its own constructor call. That keeps the odd signatures in the
        files that own them instead of leaking a special case into the engine
        for every app.
        """
        return SimpleNamespace(
            main_window=self.parent,
            plot_options=plot_options,
            input_settings=default_settings,
            output_settings=default_settings_output,
            dig_settings=default_settings_dig,
            selected_daq=selected_daq,
            sample_rate=sample_rate,
            ao_sample_rate=ao_sample_rate_setting,
            no_chans=No_chans,
        )

    def ensure_app_dialog(self, app_id):
        """Build an app's dialog if it does not exist yet, without showing it.

        The single construction path for app dialogs: open_app() uses it to
        build on demand, and the main window uses it to bring up an app that
        has to be live before the user ever clicks its button. Returns the
        dialog, or None if the app is not registered or its build() raised.
        """
        spec = app_registry.by_id(app_id)
        if spec is None:
            print(f"ensure_app_dialog: no such app {app_id!r}")
            return None
        dlg = self._app_dialogs.get(app_id)
        if dlg is not None:
            return dlg
        try:
            dlg = spec['build'](self._app_context(), self)
        except Exception:
            print(f"Could not open {spec['label']}:\n" + traceback.format_exc())
            return None
        self._app_dialogs[app_id] = dlg
        self._wire_app_dialog(app_id, spec, dlg)
        return dlg

    def open_app(self, app_id):
        """Open (or raise) one app's dialog.

        Replaces the per-app open_* methods. The interlocks that used to be
        written out for each app are now derived from its APP_SPEC.
        """
        spec = app_registry.by_id(app_id)
        if spec is None:
            print(f"open_app: no such app {app_id!r}")
            return
        mw = self.parent

        # Guard: apps that own the hardware need acquisition stopped first.
        if spec['requires_daq_stopped'] and mw and getattr(mw, 'continue_running', False):
            QMessageBox.warning(
                self, "DAQ Running",
                f"Stop the DAQ acquisition before opening {spec['label']}.\n\n"
                "Press 'Stop' in the Output panel, then try again."
            )
            return

        dlg = self.ensure_app_dialog(app_id)
        if dlg is None:
            QMessageBox.critical(
                self, spec['label'],
                f"{spec['label']} failed to open — see the Log for details."
            )
            return

        dlg.show()
        dlg.raise_()
        if mw is not None:
            mw.refresh_app_availability()

    def _wire_app_dialog(self, app_id, spec, dlg):
        """Hook a freshly built dialog up to the main window."""
        mw = self.parent
        if mw is None:
            return

        # Every app re-evaluates the bar when it closes, which is what removes
        # the need for apps to disable each other's buttons by name.
        try:
            dlg.finished.connect(lambda _=None: mw.refresh_app_availability())
        except Exception:
            pass

        # Apps that hide instead of destroying on close need their closeEvent
        # wrapped, because finished never fires for them.
        _orig_close = dlg.closeEvent
        def _on_close(ev, _orig=_orig_close):
            _orig(ev)
            mw.refresh_app_availability()
        dlg.closeEvent = _on_close

        # The Function Generator also reports start/stop, since it holds the AO
        # bus while generating even if its window is closed.
        for sig_name in ('generation_started', 'generation_stopped'):
            sig = getattr(dlg, sig_name, None)
            if sig is not None:
                try:
                    sig.connect(mw.refresh_app_availability)
                except Exception:
                    pass


# Filter Type spellings accepted from the expert_settings sheet, mapped to the
# canonical kind used to build the filter. Anything absent means "no filtering".
_FILTER_KINDS = {
    "lowpass":  "low",  "low_pass":  "low",  "low":   "low",
    "highpass": "high", "high_pass": "high", "high":  "high",
    "bandpass": "band", "band_pass": "band", "band":  "band",
    "bandstop": "stop", "band_stop": "stop", "notch": "stop",
    "median":   "median",
}


# Qt key code -> the name used in the key_mappings sheet. Letters and digits
# are handled by range instead, since their key codes are their ASCII codes.
_KEY_NAMES = {
    getattr(Qt, f"Key_F{n}"): f"F{n}" for n in range(1, 13)
}
_KEY_NAMES.update({
    Qt.Key_Space:     "Space",
    Qt.Key_Return:    "Return",
    Qt.Key_Enter:     "Enter",
    Qt.Key_Escape:    "Escape",
    Qt.Key_Tab:       "Tab",
    Qt.Key_Backspace: "Backspace",
    Qt.Key_Delete:    "Delete",
    Qt.Key_Insert:    "Insert",
    Qt.Key_Home:      "Home",
    Qt.Key_End:       "End",
    Qt.Key_PageUp:    "PageUp",
    Qt.Key_PageDown:  "PageDown",
    Qt.Key_Up:        "Up",
    Qt.Key_Down:      "Down",
    Qt.Key_Left:      "Left",
    Qt.Key_Right:     "Right",
})

# Widgets that own their keystrokes; a binding must not fire while one has focus.
_TEXT_ENTRY_WIDGETS = (QLineEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox)


def _key_name(key):
    """Name for a Qt key code, or None if it cannot be bound."""
    if key in _KEY_NAMES:
        return _KEY_NAMES[key]
    if Qt.Key_A <= key <= Qt.Key_Z or Qt.Key_0 <= key <= Qt.Key_9:
        return chr(key)
    return None


class _GlobalKeyFilter(QObject):
    """Application-wide key filter driving the user's configured bindings."""

    def __init__(self, main_window):
        super().__init__(main_window)
        self.main_window = main_window

    def eventFilter(self, obj, event):
        if event.type() != QEvent.KeyPress:
            return False
        if isinstance(QApplication.focusWidget(), _TEXT_ENTRY_WIDGETS):
            return False

        key_str = _key_name(event.key())
        if key_str is None:
            return False

        mw = self.main_window
        actions = mw._key_action_map()
        for action, bound_key in mw._key_bindings.items():
            if bound_key == key_str and action in actions:
                widget, callback = actions[action]
                if widget.isEnabled():
                    callback()
                return True   # always consume — prevents the key from also
                              # reaching the focused child widget
        return False          # key not bound — let Qt handle it normally


class VoltageContinuousInput(QMainWindow):
    # Hold, Session Autosave (on/off, file, whether it can be changed), the
    # selected run tab or the plotted channels changed. For apps that follow the session (Custom
    # Triggers mirrors Session Autosave while Hold is on, and offers Re-Start
    # on a run tab), so the main window never has to know which apps those are.
    session_state_changed = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"GioDAQ V{APP_VERSION} — Analog Voltage Data Acquisition")
        self.setGeometry(100, 100, 1400, 825)
        self.continue_running = False
        self.signals = DAQSignals()
        self.signals.data_ready.connect(self.update_data)
        self.signals.task_stopped.connect(self.on_task_stopped)
        # Menu bar first: enable_buttons() touches its actions, and it is built
        # once here rather than in refresh_ui() so a settings reload does not
        # discard it.
        self._build_menu_bar()
        # Held runs (see HeldRun). Kept on the window, not a panel, because
        # refresh_ui() rebuilds the panels; the run tabs are rebuilt from this.
        # Set before create_widgets(), which builds those tabs.
        self.held_runs = []
        self._held_serial = 1        # folder names; never reused in a session
        self._held_title = ""        # the title every held run shares
        self._rerun_index = None     # held run a Re-Run will overwrite
        self._rerun_confirmed = False  # the overwrite was already confirmed (by an app)
        self._run_axis_ranges = (None, {})
        self._last_x_name = None     # X axis of the previous run (range carry-over)
        # The overlay file, read once (see _read_overlay_file). Kept on the
        # window so it survives refresh_ui() rebuilding the panels.
        self._overlay_file = None
        # The notes field edits a held run's notes while its tab is shown, so
        # Live Graph's notes are kept here and put back on leaving that tab.
        self._live_notes = ""
        self.create_widgets()
        self.setup_layout()

        if NO_DAQ:
            _no_daq_tip = "No DAQ device detected — connect a device and restart"
            self.outputSettingsWidget.start_button.setEnabled(False)
            self.outputSettingsWidget.start_button.setToolTip(_no_daq_tip)
            self.reset_daq_action.setEnabled(False)
            self.reset_daq_action.setStatusTip(_no_daq_tip)
            # Purely from APP_SPEC — no app is named here.
            for _spec in app_registry.load_apps():
                if _spec['requires_daq_hw']:
                    _btn = self.appsWidget.app_button(_spec['id'])
                    if _btn is not None:
                        _btn.setEnabled(False)
                        _btn.setToolTip(_no_daq_tip)

        if NO_SETTINGS:
            _no_set_tip = "No settings file found — configure settings first"
            self.outputSettingsWidget.start_button.setEnabled(False)
            self.outputSettingsWidget.start_button.setToolTip(_no_set_tip)
            self.outputSettingsWidget.record_button.setEnabled(False)
            # Auto-open settings dialog so user can create the file
            QTimer.singleShot(300, self._open_settings_on_startup)

        self.total_written = 0

        # Per-theme, not shared: the light palette contains 'black', which is
        # invisible on a dark canvas, so a theme that only changed the background
        # would silently drop a channel off the graph.
        self.plot_colors = daq_theme.curve_colors(theme_name)
        self.axes_plotting = []
        self.sample_axis = None
        self.time_axis = None
        # No buffers until Run builds them (create_buffers). Empty, not None,
        # so "no buffers yet" is a valid state: reset_ring_now then simply has
        # nothing to clear.
        self.rings = []
        self.rpos = np.zeros(0, dtype=np.int64)
        self.tasks = []
        self.sos = None
        self.zi = None
        self.samples_per_frame = None

        # --- Key bindings (action -> key string, loaded from Excel) ---
        self._key_bindings = {}
        self._load_key_bindings()
        self._install_key_filter()

        # --- Recording state ---
        self.is_recording = False
        self.record_buffers = []
        self.record_lock = threading.Lock()
        self.chunk_files = []
        self.chunk_sample_counts = []
        self.chunk_counter = 0
        self.chunk_timer = None
        self.autostop_timer = None
        self.record_chunks_dir = os.path.join(_app_dir(), '_daq_cache')
        self._check_for_orphaned_chunks()

    def create_widgets(self):
        self.channelSettingsWidget = ChannelSettings(self)
        self.plotSettingsWidget = PlotSettings(self)
        self.outputSettingsWidget = OutputSettings(self)
        self.graphDataWidget = GraphData(self)
        self.graphTabs = QTabWidget()
        self.graphTabs.setTabBar(_GraphTabBar())
        self.graphTabs.addTab(self.graphDataWidget, "Live Graph")
        self._run_views = []
        self._all_runs_view = None
        self._shown_run_view = None
        self.graphTabs.currentChanged.connect(self._on_graph_tab_changed)
        self.logWidget = LogWidget(self)
        self.appsWidget = Apps(self)
        # Static reference lines decorate the graph, so they are not something
        # the user should have to open an app to get: build the dialog now and
        # it imports the saved lines itself. Done here rather than in __init__
        # so a settings reload (refresh_ui -> create_widgets) restores them too.
        self.appsWidget.ensure_app_dialog('static_lines')
        self._rebuild_run_tabs()
        self._sync_overlay_controls()
        self.apply_overlay()

    def _recovered_meta(self, folder):
        """(x_name, channels) to save a recovered recording by."""
        return self._read_recording_meta(folder) or self._all_channels_meta()

    def _check_for_orphaned_chunks(self):
        """On startup, offer to recover recordings left behind by a crash.

        Two kinds can be left: an unfinished recording's chunk files at the top
        of the cache folder, and held runs' folders (held_NNNN). With no held
        runs it is saved as one file, as before; with held runs everything goes
        into one Excel workbook, a sheet per run.
        """
        orphans = sorted(glob.glob(os.path.join(self.record_chunks_dir, 'chunk_*.npz')))
        held_dirs = sorted(d for d in glob.glob(os.path.join(self.record_chunks_dir, 'held_*'))
                           if os.path.isdir(d))
        if not orphans and not held_dirs:
            return
        try:
            rate = self._settings_rate()
        except (TypeError, ValueError):
            print("Unsaved recordings were found from a previous session, but no "
                  "settings are loaded to read them with. They are kept; recovery "
                  "will be offered again at the next start.")
            return

        found = []
        if held_dirs:
            found.append(f"{len(held_dirs)} held run(s)")
        if orphans:
            found.append(f"{len(orphans)} autosave chunk(s) of an unfinished recording")
        where = ("into one Excel file, one sheet per run" if held_dirs
                 else "and save them now")
        reply = QMessageBox.question(
            self,
            "Recover Data from Previous Session?",
            f"Found {' and '.join(found)} from a previous session that was not "
            f"closed cleanly (possibly a crash).\n\n"
            f"Would you like to recover them {where}?\n\n"
            f"Choosing No will permanently delete them.",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            if held_dirs:
                filepath, _ = QFileDialog.getSaveFileName(
                    self, "Save Recovered Data", "", "Excel Files (*.xlsx)")
                if filepath:
                    sheets = [(f"Run {k}", self._merge_recording(
                                  sorted(glob.glob(os.path.join(d, 'chunk_*.npz'))),
                                  rate, None, *self._recovered_meta(d)))
                              for k, d in enumerate(held_dirs, 1)]
                    if orphans:
                        sheets.append(("Unfinished Recording", self._merge_recording(
                            orphans, rate, None,
                            *self._recovered_meta(self.record_chunks_dir))))
                    self._write_workbook(sheets, filepath)
            else:
                filepath, _ = QFileDialog.getSaveFileName(
                    self, "Save Recovered Data", "",
                    "CSV Files (*.csv);;Excel Files (*.xlsx)"
                )
                if filepath:
                    data = self._merge_recording(
                        orphans, rate, None, *self._recovered_meta(self.record_chunks_dir))
                    if data is None:
                        QMessageBox.warning(self, "No Data", "No recorded data found to save.")
                    else:
                        self._write_data_file(data, filepath, "The recovered recording")
        # Whether saved or discarded, clean up
        for path in orphans:
            try:
                os.remove(path)
            except Exception:
                pass
        for d in held_dirs:
            shutil.rmtree(d, ignore_errors=True)
        try:
            os.remove(os.path.join(self.record_chunks_dir, self._META_NAME))
        except Exception:
            pass
        try:
            os.rmdir(self.record_chunks_dir)
        except Exception:
            pass   # not empty or doesn't exist — fine
        self.chunk_files = []
        self.chunk_sample_counts = []

    # ------------------------------------------------------------------
    # Key binding support
    # ------------------------------------------------------------------

    def _install_key_filter(self):
        """Install a persistent app-level event filter for key bindings.

        keyPressEvent on QMainWindow only fires when no child widget has
        focus.  Buttons and checkboxes grab focus and consume Space/Enter
        themselves, so the main window never sees those keys.  Filtering at
        the QApplication level intercepts every key event first, before Qt
        routes it to the focused widget.
        """
        self._key_filter = _GlobalKeyFilter(self)
        QApplication.instance().installEventFilter(self._key_filter)

    def _key_action_map(self):
        """action name -> (widget that gates it, callable that performs it).

        Rebuilt per keypress because refresh_ui() replaces these widgets.
        """
        ow = self.outputSettingsWidget
        cs = self.channelSettingsWidget
        return {
            "Pen":         (cs.pen_var,      lambda: cs.pen_var.setChecked(not cs.pen_var.isChecked())),
            "Run":         (ow.start_button, ow.start_button.click),
            "Stop":        (ow.stop_button,  ow.stop_button.click),
            # Binding name kept as-is: it is the key stored in the key_mappings
            # sheet. The button it drives is now Clear Data, in the Display
            # Settings panel.
            "Clear Graph": (cs.clear_button, cs.clear_button.click),
            "Save Data":   (self.save_action, self.save_action.trigger),
        }

    def _load_key_bindings(self):
        """Read the key_mappings sheet from the settings Excel file."""
        try:
            xls = pd.ExcelFile(_get_settings_path())
            if "key_mappings" in xls.sheet_names:
                df = pd.read_excel(xls, sheet_name="key_mappings")
                for _, row in df.iterrows():
                    action = str(row.get("Action", ""))
                    key    = row.get("Key", "")
                    key    = "" if pd.isna(key) else str(key)
                    if action and key:
                        self._key_bindings[action] = key
        except Exception:
            pass

    def setup_layout(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        main_layout = QHBoxLayout(central_widget)
        # Qt's default layout margin leaves a visible band between the menu bar
        # and the first row of panels. Trim it, keeping a little at the sides.
        main_layout.setContentsMargins(4, 2, 4, 4)
        main_layout.setSpacing(4)

        left_v = QVBoxLayout()

        # Warning row shown only when no DAQ is connected
        if NO_DAQ:
            no_daq_row = QHBoxLayout()
            self.no_daq_label = QLabel("⚠  No DAQ detected — connect device and reload")
            self.no_daq_label.setStyleSheet(
                "color: white; background-color: #c0392b;"
                "padding: 4px; border-radius: 3px;"
            )
            self.no_daq_label.setAlignment(Qt.AlignCenter)
            self.reload_daq_btn = QPushButton("↻")
            self.reload_daq_btn.setFixedWidth(32)
            self.reload_daq_btn.setToolTip("Re-check for connected DAQ devices")
            self.reload_daq_btn.clicked.connect(self._reload_daq)
            no_daq_row.addWidget(self.no_daq_label)
            no_daq_row.addWidget(self.reload_daq_btn)
            left_v.addLayout(no_daq_row)

        if NO_SETTINGS:
            no_set_label = QLabel("⚠  No settings file — configure channels first")
            no_set_label.setStyleSheet(
                "color: white; background-color: #e67e22;"
                "padding: 4px; border-radius: 3px;"
            )
            no_set_label.setAlignment(Qt.AlignCenter)
            left_v.addWidget(no_set_label)

        # Stretch given with each widget, not by position afterwards: the
        # warning rows above shift the positions, which put every stretch on
        # the wrong item whenever one was shown. Display Settings is fixed-
        # height so stretch 0; Log gets the freed space.
        left_v.addWidget(self.plotSettingsWidget, 1)
        left_v.addWidget(self.channelSettingsWidget, 0)
        left_v.addWidget(self.outputSettingsWidget, 1)
        left_v.addWidget(self.logWidget, 3)

        left_container = QWidget()
        left_container.setLayout(left_v)
        # Was setFixedWidth(400), which pinned the panel at exactly 400 whatever
        # it held. A maximum plus the Maximum policy means "take the width the
        # content actually needs, never more than 400", so a handful of channels
        # gives a narrow panel while many channels across several modules grow
        # it out to the cap.
        #
        # The cap never goes below what the content needs as a minimum,
        # though: a cap under that squeezed every widget below its own minimum,
        # clipping labels such as "Duration to Acquire:" mid-word.
        left_container.setMaximumWidth(
            max(400, left_container.minimumSizeHint().width()))
        left_container.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Expanding)

        right_v = QVBoxLayout()
        right_v.addWidget(self.graphTabs, 3)
        right_v.addWidget(self.appsWidget, 1)

        right_container = QWidget()
        right_container.setLayout(right_v)
        right_container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        # Stretch 0/1: every pixel the left panel gives up goes to the graph
        # rather than being shared between them.
        main_layout.addWidget(left_container, 0)
        main_layout.addWidget(right_container, 1)

    def _build_menu_bar(self):
        """File menu. Built once, and deliberately not rebuilt by refresh_ui().

        The actions are held on the main window because other code enables and
        disables them: Save Data follows whether there is anything in the buffer,
        and Load File is locked while a recording is running.
        """
        bar = self.menuBar()
        bar.clear()
        file_menu = bar.addMenu("&File")

        self.save_action = QAction("&Save Data", self)
        self.save_action.setShortcut("Ctrl+S")
        self.save_action.setStatusTip("Save the acquired data to a file")
        self.save_action.triggered.connect(self.save_data)
        self.save_action.setEnabled(False)      # nothing acquired yet
        file_menu.addAction(self.save_action)

        self.save_all_runs_action = QAction("Save &All Runs…", self)
        self.save_all_runs_action.setStatusTip(
            "Save every held run to one Excel file, one sheet per run")
        self.save_all_runs_action.triggered.connect(self._save_all_runs)
        self.save_all_runs_action.setEnabled(False)
        file_menu.addAction(self.save_all_runs_action)

        file_menu.addSeparator()

        self.save_session_action = QAction("Save Sess&ion\u2026", self)
        self.save_session_action.setStatusTip(
            "Save the settings and every held run to one .mcz session file")
        self.save_session_action.triggered.connect(self.save_session)
        self.save_session_action.setEnabled(False)
        file_menu.addAction(self.save_session_action)

        self.load_session_action = QAction("Load Sessio&n\u2026", self)
        self.load_session_action.setStatusTip(
            "Load a .mcz session: its settings, Hold, and all its runs")
        self.load_session_action.triggered.connect(self.load_session)
        file_menu.addAction(self.load_session_action)

        file_menu.addSeparator()

        self.load_action = QAction("&Load File", self)
        self.load_action.setShortcut("Ctrl+O")
        self.load_action.setStatusTip("Load a previously saved data file")
        self.load_action.triggered.connect(self.load_file)
        file_menu.addAction(self.load_action)

        file_menu.addSeparator()

        self.print_action = QAction("&Print", self)
        self.print_action.setShortcut("Ctrl+P")
        self.print_action.setStatusTip("Print the current graph")
        self.print_action.triggered.connect(self.print_graph)
        file_menu.addAction(self.print_action)

        file_menu.addSeparator()

        self.load_settings_action = QAction("Load Se&ttings…", self)
        self.load_settings_action.setStatusTip(
            "Load a different channel-settings workbook")
        self.load_settings_action.triggered.connect(self._menu_load_settings)
        file_menu.addAction(self.load_settings_action)

        # ── Edit ──────────────────────────────────────────────────────────
        edit_menu = bar.addMenu("&Edit")

        self.system_settings_action = QAction("S&ystem Settings…", self)
        self.system_settings_action.setStatusTip(
            "Edit channels, transducers and expert settings")
        self.system_settings_action.triggered.connect(self._menu_system_settings)
        edit_menu.addAction(self.system_settings_action)

        edit_menu.addSeparator()

        self.reset_daq_action = QAction("&Reset DAQ", self)
        self.reset_daq_action.setStatusTip(
            "Reset the DAQ device, clearing any stuck task")
        self.reset_daq_action.triggered.connect(self.reset_device)
        edit_menu.addAction(self.reset_daq_action)

    # The panel that owns these operations is rebuilt by refresh_ui(), so the
    # menu actions go through the main window and look it up when triggered
    # rather than binding to a widget that may since have been replaced.
    def _menu_load_settings(self):
        w = getattr(self, 'channelSettingsWidget', None)
        if w is not None:
            w.load_settings_file()

    def _menu_system_settings(self):
        w = getattr(self, 'channelSettingsWidget', None)
        if w is not None:
            w.edit_settings()

    def print_graph(self):
        """Print the graph, on white regardless of the screen theme."""
        try:
            from PyQt5.QtPrintSupport import QPrinter, QPrintDialog
        except ImportError:
            QMessageBox.warning(
                self, "Print",
                "Printing is unavailable: this build has no QtPrintSupport.")
            return

        tabs = getattr(self, 'graphTabs', None)
        gd = tabs.currentWidget() if tabs is not None else None
        if gd is None:
            return

        printer = QPrinter(QPrinter.HighResolution)
        printer.setOrientation(QPrinter.Landscape)   # graphs are wider than tall
        dlg = QPrintDialog(printer, self)
        dlg.setWindowTitle("Print Graph")
        if dlg.exec_() != QDialog.Accepted:
            return

        painter = QPainter()
        if not painter.begin(printer):
            QMessageBox.warning(self, "Print", "Could not start the print job.")
            return
        try:
            # The restyle has to be inside the paint job and outside the render,
            # so the page gets print colours and the screen gets its own back.
            with gd.print_colors():
                gd.render_to_painter(painter, printer.pageRect())
        except Exception as e:
            QMessageBox.warning(self, "Print", f"Printing failed.\n\n{e}")
        finally:
            painter.end()

    def refresh_ui(self):
        # Rebuilding recreates Session Settings, which would switch Hold and
        # Session Autosave off. Settings can be changed with Hold on as long as
        # no runs are held, so carry them across the rebuild.
        ps = getattr(self, 'plotSettingsWidget', None)
        session = None
        if ps is not None:
            session = (ps.hold_runs_var.isChecked(), ps.autosave_var.isChecked(),
                       ps.autosave_path_entry.text())
        for attr in ('channelSettingsWidget', 'plotSettingsWidget', 'outputSettingsWidget',
                     'graphDataWidget', 'graphTabs', 'logWidget', 'appsWidget'):
            old = getattr(self, attr, None)
            if old is not None:
                old.setParent(None)
        self.create_widgets()
        self.setup_layout()
        if session is not None and session[0]:
            hold, autosave, path = session
            ps = self.plotSettingsWidget
            ps.hold_runs_var.setChecked(True)       # set, not clicked: no prompt
            ps.autosave_path_entry.setText(path)
            self._rebuild_run_tabs()                # All Runs is there while Hold is on
            self._apply_session_locks(getattr(self, 'continue_running', False))
            ps.autosave_var.setChecked(autosave)

    def _open_settings_on_startup(self):
        QMessageBox.information(
            self, "Settings Required",
            "No settings file was found.\n\n"
            "The Settings window will open so you can configure your channels.\n"
            "After saving, the application will reload automatically."
        )
        self.channelSettingsWidget.edit_settings()

    def _reload_daq(self):
        global NO_DAQ, daqList, daqObjects
        daqSys = nidaqmx.system.System()
        new_list = daqSys.devices.device_names
        if len(new_list) == 0:
            QMessageBox.warning(self, "No DAQ Found",
                                "Still no DAQ detected.\nPlease check your connection and try again.")
            return
        NO_DAQ = False
        daqList = new_list
        daqObjects = [daqSys.devices[name] for name in daqList]
        _safe_reset_device(daqObjects[0].name)

        self.outputSettingsWidget.start_button.setEnabled(True)
        self.outputSettingsWidget.start_button.setToolTip("")
        self.reset_daq_action.setEnabled(True)
        self.reset_daq_action.setStatusTip("Reset the DAQ device, clearing any stuck task")
        # Hardware is back — let the normal interlock rules decide the bar.
        self.refresh_app_availability()

        if hasattr(self, 'no_daq_label'):
            self.no_daq_label.hide()
        if hasattr(self, 'reload_daq_btn'):
            self.reload_daq_btn.hide()

        print("DAQ detected — device ready.")

    # ------------------------------------------------------------------
    # Run / Stop
    # ------------------------------------------------------------------

    def start_task(self):
        self.disable_buttons()
        # Everything up to a running task can fail on the hardware: a channel
        # type the card does not accept, a terminal configuration it rejects, a
        # device already reserved. disable_buttons() has already run, so without
        # this the window is left stranded mid-start — Run greyed out, no task
        # running, and only a traceback in the log to say why.
        try:
            self.get_user_settings()
            self.create_graph()
            self.create_buffers()
            self.create_nidaq_task()
        except Exception as e:
            self.continue_running = False
            self._rerun_index = None
            self._rerun_confirmed = False
            self.enable_buttons()
            print("Could not start acquisition:\n" + traceback.format_exc())
            if isinstance(e, UnsupportedChannelType):
                detail = str(e)
            else:
                detail = (f"{type(e).__name__}: {e}\n\n"
                          "See the Log for the full details.")
            QMessageBox.critical(self, "Cannot Start Acquisition",
                                 "The acquisition task could not be created.\n\n"
                                 + detail)
            return
        self.continue_running = True
        # Re-gate the cal window now that the flag is actually set. disable_buttons()
        # above ran while continue_running was still False, so anything keyed on
        # "is the DAQ running" (the digital-output toggles, Start Recording) was
        # left switched off.
        self._notify_cal_window_app_state()
        self._refresh_record_button()
        # Reapply any saved static reference lines once the new plot has data
        # (the unit axes they attach to don't exist until the first frame is
        # plotted, so this is consumed in update_data, not here).
        self._static_lines_pending = True
        self.acquisition_thread = threading.Thread(target=self.run_task, daemon=True)
        self.acquisition_thread.start()

    def disable_buttons(self):
        # Apps flagged requires_daq_stopped go grey while acquiring; the rest
        # (Function Generator, Static Lines, Gauge Adder) stay usable, as before.
        self.refresh_app_availability()
        self.outputSettingsWidget.start_button.setEnabled(False)
        # Reset Graph stays available while acquiring — it is how you discard a
        # noisy startup transient without losing the recording.
        self.channelSettingsWidget._set_clear_mode(True)
        self.channelSettingsWidget.clear_button.setEnabled(not self.is_recording)
        # File actions are unavailable while acquiring.
        self.load_action.setEnabled(False)
        self.reset_daq_action.setEnabled(False)
        # Held runs are viewed only while stopped; acquisition shows Live Graph.
        self.graphTabs.setCurrentIndex(0)
        for i in range(1, self.graphTabs.count()):
            self.graphTabs.setTabEnabled(i, False)
        self._apply_session_locks(running=True)
        self.plotSettingsWidget.plot_duration_entry.setEnabled(False)
        self.plotSettingsWidget.plot_duration_unit.setEnabled(False)
        self.plotSettingsWidget.title_label_entry.setEnabled(False)
        self.plotSettingsWidget.apply_ranges_var.setEnabled(False)
        self.plotSettingsWidget.axis_ranges_button.setEnabled(False)

    def get_user_settings(self):
        self.sample_rate = int(float(sample_rate))
        self.recording_duration = self.plotSettingsWidget.recording_duration_seconds()
        self.number_of_samples = int(self.recording_duration * self.sample_rate)

        self.refresh_rate = max(1, min(int(float(refresh_rate)), 30))
        self.samples_per_frame = max(1, self.sample_rate // self.refresh_rate)
        self.selected_x_axis = self.plotSettingsWidget.x_axis_selection.currentText()

        # Duration to Plot caps how much data is shown on screen at once.
        # "Decimate To Fit" expert setting decides how Max Plot Points is
        # applied to the plot-duration window:
        #   ON  -> keep the whole window in view; the point cap is applied
        #          by decimating at draw time (older data stays visible at
        #          lower resolution).
        #   OFF -> show only the most recent max_plot_points samples at
        #          full resolution.
        # Recorded/saved data is full-rate and unaffected either way. Computed
        # by PlotSettings so the Axis Ranges dialog sees the same window.
        self.plot_duration, self.samples_to_plot = \
            self.plotSettingsWidget.plot_window(self.sample_rate)
        self.sample_axis = np.arange(self.samples_to_plot, dtype=np.int32)
        self.time_axis = np.linspace(0.0, self.samples_to_plot / self.sample_rate,
                                     self.samples_to_plot, endpoint=False).astype(np.float32)

        self._configure_filter()

        # ------------------------------------------------------------------
        # Cache calibration coefficients as numpy arrays so run_task never
        # touches pandas on the hot path.
        # ------------------------------------------------------------------
        if No_chans > 0:
            mx_vals  = default_settings['Mx'].to_numpy(dtype=np.float32)
            b_vals   = default_settings['b'].to_numpy(dtype=np.float32)
            err_vals = default_settings['Error'].to_numpy(dtype=np.float32)
            self.calib_mx   = mx_vals                          # shape (No_chans,)
            self.calib_bias = (b_vals - err_vals)              # shape (No_chans,)
        else:
            self.calib_mx   = np.empty(0, dtype=np.float32)
            self.calib_bias = np.empty(0, dtype=np.float32)

        # Resolve custom-channel source indices once so run_task does no
        # DataFrame string-equality filtering per frame.
        ch_to_idx = {str(row['Channel']).strip(): i
                     for i, row in default_settings.iterrows()}
        self.custom_pairs = []   # list of (f_idx, s_idx, op_or_callable)
        for _, row in default_settings_custom.iterrows():
            f_idx = _input_index(ch_to_idx, row['First Channel'])
            # Second Channel is empty string when B was set to None
            sec = str(row.get('Second Channel', '')).strip()
            s_idx = _input_index(ch_to_idx, sec) if sec else 0
            op    = str(row['Operation']).strip()
            # Compile expression strings into a callable once here so
            # run_task never calls eval() on the hot path.
            if op not in ('+', '-', 'x', '÷'):
                import math as _math
                normalised = op.replace('^', '**')
                _safe_ns = {
                    '__builtins__': {},
                    'log':   _math.log,   'log10': _math.log10,
                    'sqrt':  _math.sqrt,  'abs':   abs,
                    'exp':   _math.exp,   'sin':   _math.sin,
                    'cos':   _math.cos,   'tan':   _math.tan,
                }
                try:
                    code = compile(normalised, '<custom_expr>', 'eval')
                    def _make_fn(c, ns):
                        def _fn(A, B):
                            # B is always passed but only used if expression
                            # references it; if B was None, B=zeros is harmless
                            return eval(c, {**ns, 'A': A, 'B': B})
                        return _fn
                    op = _make_fn(code, _safe_ns)
                except Exception as e:
                    print(f"Custom channel expression compile error: {e} — defaulting to A")
                    op = '+'
            self.custom_pairs.append((f_idx, s_idx, op))

    def _configure_filter(self):
        """Build the input filter described by the expert settings.

        Sets self.sos / self.fir_taps (at most one is non-None; both None means
        no filtering), the per-channel state in self.zi, and the mask saying
        which channels the filter applies to.
        """
        nyq = 0.5 * self.sample_rate
        self.sos      = None   # IIR filter coefficients (sosfilt)
        self.fir_taps = None   # FIR filter coefficients (lfilter)
        self.zi       = None   # per-channel filter state

        ft   = (filter_type or "none").strip().lower()
        mode = (filter_mode or "iir").strip().lower()   # "iir (sosfilt)" or "fir"
        kind = _FILTER_KINDS.get(ft)                    # None => unfiltered
        fo = int(filter_order) if filter_order else 2
        fc = float(filter_cutoff) if filter_cutoff else 1.0
        fh = float(filter_fh)    if filter_fh     else 1.0

        # For band filters scipy requires Wn[0] < Wn[1] and both strictly
        # between 0 and 1 (normalised). Swap if the user entered them backwards,
        # and clamp to a safe distance from the Nyquist boundary.
        if kind in ("band", "stop"):
            f_lo, f_hi = min(fc, fh), max(fc, fh)
            f_lo = max(f_lo, 0.5)                # at least 0.5 Hz from DC
            f_hi = min(f_hi, nyq - 0.5)          # at least 0.5 Hz from Nyquist
            if f_lo >= f_hi:
                print(f"Band filter: low cutoff ({f_lo} Hz) must be less than "
                      f"high cutoff ({f_hi} Hz) — running unfiltered.")
            else:
                fc, fh = f_lo, f_hi

        use_fir = "fir" in mode and ft not in ("median", "none", "off")

        try:
            if use_fir:
                # numtaps derived from order (scaled for useful length): minimum
                # 3, always odd, which firwin requires for some pass types.
                numtaps = max(3, fo * 50 + 1) | 1
                if kind == "low":
                    self.fir_taps = firwin(numtaps, fc, fs=self.sample_rate, pass_zero=True)
                elif kind == "high":
                    self.fir_taps = firwin(numtaps, fc, fs=self.sample_rate, pass_zero=False)
                elif kind == "band":
                    self.fir_taps = firwin(numtaps, [fc, fh], fs=self.sample_rate, pass_zero=False)
                elif kind == "stop":
                    self.fir_taps = firwin(numtaps, [fc, fh], fs=self.sample_rate, pass_zero=True)
            elif kind == "low":
                self.sos = butter(fo, fc / nyq, btype='low', output='sos')
            elif kind == "high":
                self.sos = butter(fo, fc / nyq, btype='high', output='sos')
            elif kind == "band":
                self.sos = butter(fo, [fc / nyq, fh / nyq], btype='band', output='sos')
            elif kind == "stop":
                self.sos = butter(fo, [fc / nyq, fh / nyq], btype='bandstop', output='sos')
            # "median" is applied per-block in run_task; anything else, and
            # "none", leaves both coefficient sets None and so unfiltered.
        except Exception as e:
            print(f"Filter configuration error ({ft}, order={fo}, fc={fc}, fh={fh}): {e} — running unfiltered.")
            self.sos = None
            self.fir_taps = None

        # Per-channel filter state, seeded to zero here and primed from each
        # channel's first real sample on the first frame in run_task (see
        # self._filter_primed), so the filtered trace starts at the true signal
        # level instead of ramping up from zero. The "base" template is the
        # filter's steady-state response to a unit (1.0) input; scaling it by
        # the first sample x0 represents "the signal sat at x0 before the run
        # started". Coefficients are untouched, so the frequency response is
        # unchanged — only the startup transient differs.
        self._zi_base_sos = None
        self._zi_base_fir = None
        self._filter_primed = True   # nothing to prime unless a stateful filter is active
        if self.sos is not None:
            self._zi_base_sos = sosfilt_zi(self.sos)            # (n_sections, 2)
            self.zi = [np.zeros((self.sos.shape[0], 2)) for _ in range(No_chans)]
            self._filter_primed = False
        elif self.fir_taps is not None:
            self._zi_base_fir = lfilter_zi(self.fir_taps, 1.0)  # (numtaps-1,)
            self.zi = [self._zi_base_fir.copy() for _ in range(No_chans)]
            self._filter_primed = False

        self._filter_type = ft   # stored so run_task can branch for median

        # Which input channels the filter applies to. "All" (or blank/None) ->
        # every channel; otherwise match input Device Names against the
        # comma-separated selection. Channels outside the mask pass through
        # unfiltered in run_task.
        sel = filter_channels
        if sel is None or str(sel).strip().lower() in ("", "all"):
            self._filter_mask = [True] * No_chans
            return
        wanted = {s.strip() for s in str(sel).split(",") if s.strip()}
        try:
            dev_names = default_settings["Device Name"].tolist()
        except Exception:
            dev_names = []
        self._filter_mask = [
            (i < len(dev_names) and dev_names[i] in wanted)
            for i in range(No_chans)
        ]
        if not any(self._filter_mask):
            print("Filter 'Apply to': no listed channels matched current "
                  "input Device Names — running all channels unfiltered.")

    def create_nidaq_task(self):
        """Build one AI task per hardware timing domain.

        All selected input channels are grouped by _timing_group() so that
        modules sharing a chassis go into a single synchronized task, while
        independent devices each get their own task. Every task reads into its
        own sub-frame; run_task() then scatters those rows back into the single
        combined self.frame at their original global channel indices, so all the
        downstream filtering / calibration / custom-channel / ring-buffer code
        (which indexes channels 0..No_chans-1 in default_settings row order) is
        unchanged.
        """
        # Group selected input rows by timing domain, preserving global order.
        groups = {}          # group_key -> list of (global_index, physical, mn, mx, term)
        group_order = []     # keep first-seen ordering of groups
        for gi, (_, row) in enumerate(default_settings.iterrows()):
            row_daq = _row_device(row, selected_daq)
            physical_channel = f"{row_daq}/{row['Channel']}"
            min_val = float(row['Min'])
            max_val = float(row['Max'])
            terminal_config = getattr(TerminalConfiguration,
                                      str(row['Terminal Configuration']).upper())
            # The measurement type the card reports for this channel. Min/Max
            # above are already in that type's unit, so they need no conversion.
            chan_type = row_channel_type(row, 'ai')
            key = _timing_group(row_daq)
            if key not in groups:
                groups[key] = []
                group_order.append(key)
            groups[key].append((gi, physical_channel, min_val, max_val,
                                terminal_config, chan_type))

        driver_buf = int(max(self.samples_per_frame * 6, max_buffer_size))

        # Combined frame in global channel order (what run_task/filters expect).
        self.frame = np.empty((No_chans, int(self.samples_per_frame)), dtype=np.float64)

        # Defensive: close any tasks left over from a previous failed attempt
        # so we never accumulate orphaned tasks that keep the device reserved.
        self._close_all_tasks()

        # Build a task per group. If ANY step fails partway, close everything we
        # created (including the task being built) before re-raising — otherwise
        # the half-built tasks stay reserved and the device becomes inaccessible
        # (DAQ error -201003) on every subsequent start until the process exits.
        self.tasks = []      # list of dicts: {task, reader, frame, gidx}
        task = None
        try:
            for key in group_order:
                members = groups[key]
                task = nidaqmx.Task()
                for _gi, physical, mn, mx, term, ctype in members:
                    add_ai_channel(task, physical, ctype,
                                   min_val=mn, max_val=mx,
                                   terminal_config=term)
                task.timing.cfg_samp_clk_timing(
                    self.sample_rate,
                    sample_mode=nidaqmx.constants.AcquisitionType.CONTINUOUS,
                    samps_per_chan=int(driver_buf)
                )
                gidx = [m[0] for m in members]   # global indices, in add order
                sub = np.empty((len(members), int(self.samples_per_frame)), dtype=np.float64)
                self.tasks.append({
                    "task":   task,
                    "reader": AnalogMultiChannelReader(task.in_stream),
                    "frame":  sub,
                    "gidx":   gidx,
                })
                task = None   # successfully stored; nothing dangling

            # Start all tasks. (Within one chassis a single task is inherently
            # synchronized. Across independent devices the tasks free-run at the
            # same sample rate; they are not hardware-synchronized to each other
            # — that would require a shared sample clock or start trigger.)
            for t in self.tasks:
                t["task"].start()
        except Exception:
            # Close the task currently being built (not yet stored) then all
            # stored tasks, so no resources stay reserved.
            if task is not None:
                try:
                    task.close()
                except Exception:
                    pass
            self._close_all_tasks()
            raise

    def _close_all_tasks(self):
        """Stop and close every per-domain task and clear the references."""
        for t in getattr(self, "tasks", []):
            try:
                t["task"].close()
            except Exception:
                pass
        self.tasks = []


    def create_buffers(self):
        n_total = No_chans + No_chans_cust + No_chans_out
        # Ring buffer sized for full recording duration so save_data captures everything.
        # Plotting reads only the last samples_to_plot samples from the tail.
        N = max(self.number_of_samples, self.samples_per_frame * 6)
        self.rings = [np.zeros(N, dtype=np.float32) for _ in range(n_total)]
        self.rpos = np.zeros(n_total, dtype=np.int64)
        self.total_written = 0
        self._reset_ring_pending = False
        # Output channel-string list (oi -> 'ao0'), used to apply direct-write
        # voltage overrides published via notify_output_voltage().
        try:
            self._out_channels = [str(c) for c in default_settings_output['Channel'].tolist()]
        except Exception:
            self._out_channels = [str(i) for i in range(No_chans_out)]

    def request_ring_reset(self):
        """Ask the acquisition thread to drop everything buffered so far.

        A flag rather than a direct reset, because run_task() owns the ring and
        total_written. It is honoured at the top of the next acquisition block.
        """
        self._reset_ring_pending = True

    def reset_ring_now(self):
        """Discard buffered samples. Only safe with no concurrent writer.

        Touches the ring buffer only — record_buffers and the .npz chunks are a
        separate path, so a recording in progress keeps every sample.
        """
        for r in self.rings:
            r.fill(0)
        self.rpos[:] = 0
        self.total_written = 0

    def rb_write(self, ch, data):
        b = self.rings[ch]; n = b.size
        p = int(self.rpos[ch] % n)
        m = data.size
        end = (p + m) % n
        if p + m <= n:
            b[p:p+m] = data
        else:
            k = n - p
            b[p:] = data[:k]; b[:end] = data[k:]
        self.rpos[ch] = (p + m) % n

    def rb_tail(self, ch, w):
        b = self.rings[ch]; n = b.size; p = int(self.rpos[ch] % n)
        w = min(w, n)
        start = p - w
        if start >= 0:
            return b[start:p]                          # common case: no wrap, zero alloc
        return np.concatenate((b[start:], b[:p]))      # wrap: two slices, one alloc

    def run_task(self):
        try:
            while self.continue_running:
                # Apply a pending Reset Graph here, between blocks, where this
                # thread is the only one touching the ring.
                if self._reset_ring_pending:
                    self._reset_ring_pending = False
                    self.reset_ring_now()
                # Read each timing-domain task into its sub-frame, then scatter
                # the rows back into the combined frame at their global indices.
                for t in self.tasks:
                    t["reader"].read_many_sample(
                        t["frame"],
                        number_of_samples_per_channel=self.samples_per_frame,
                        timeout=1.0
                    )
                    sub = t["frame"]
                    for local_k, gi in enumerate(t["gidx"]):
                        self.frame[gi] = sub[local_k]
                block = self.frame

                # First frame: seed each filtered channel's state from its own
                # first reading so low/high-pass output begins at the real signal
                # level instead of ramping from zero. Uses the mean of the first
                # few samples to avoid seeding off a single noisy point. Channels
                # excluded by the filter mask, and median/unfiltered runs, are
                # left untouched (they carry no recursive state).
                if not self._filter_primed:
                    n_seed = min(block.shape[1], 16)
                    for ch in range(No_chans):
                        if not self._filter_mask[ch]:
                            continue
                        x0 = float(np.mean(block[ch][:n_seed]))
                        if self._zi_base_sos is not None:
                            self.zi[ch] = self._zi_base_sos * x0
                        elif self._zi_base_fir is not None:
                            self.zi[ch] = self._zi_base_fir * x0
                    self._filter_primed = True

                if self.fir_taps is not None:
                    for ch in range(No_chans):
                        if not self._filter_mask[ch]:
                            continue
                        y, self.zi[ch] = lfilter(self.fir_taps, 1.0, block[ch], zi=self.zi[ch])
                        block[ch] = y
                elif self.sos is not None:
                    for ch in range(No_chans):
                        if not self._filter_mask[ch]:
                            continue
                        y, self.zi[ch] = sosfilt(self.sos, block[ch], zi=self.zi[ch])
                        block[ch] = y
                elif self._filter_type == "median":
                    fo = int(filter_order) if filter_order else 3
                    kernel = max(3, fo | 1)   # must be odd
                    for ch in range(No_chans):
                        if not self._filter_mask[ch]:
                            continue
                        block[ch] = medfilt(block[ch], kernel_size=kernel)

                # Input channels — vectorized calibration, no pandas in the loop
                # block shape: (No_chans, samples_per_frame)
                # calib_mx/bias shape: (No_chans,) — broadcast across samples axis
                calibrated = (block[:No_chans].astype(np.float32)
                              * self.calib_mx[:, None]
                              + self.calib_bias[:, None])   # (No_chans, samples)
                transformed = list(calibrated)
                for index, y in enumerate(transformed):
                    self.rb_write(index, y)

                # Custom channels — index pairs resolved once at setup, no DataFrame lookup
                transformed_custom = []
                base_offset = No_chans
                for cidx, (f_ax, s_ax, op) in enumerate(self.custom_pairs):
                    f_buff = transformed[f_ax]; s_buff = transformed[s_ax]
                    if callable(op):
                        # Expression compiled at setup — A and B are numpy
                        # arrays, so ufuncs apply element-wise automatically.
                        try:
                            y = op(f_buff, s_buff)
                            if not isinstance(y, np.ndarray):
                                y = np.full_like(f_buff, float(y))
                        except Exception:
                            y = np.zeros_like(f_buff)
                    elif op == 'x':   y = f_buff * s_buff
                    elif op == '+':   y = f_buff + s_buff
                    elif op == '-':   y = f_buff - s_buff
                    else:             y = f_buff / np.where(s_buff == 0, 1.0, s_buff)
                    y = y.astype(np.float32, copy=False)
                    transformed_custom.append(y)
                    self.rb_write(base_offset + cidx, y)

                # Output channels — fill ring buffer from FG per-channel state.
                # _ch_states[oi].current_voltage is updated every 50 ms by the
                # FG tracking timer, independently for each AO channel.
                out_base = No_chans + No_chans_cust
                try:
                    fg = self.appsWidget.app_dialog('function_generator')
                    if fg and fg.is_running and hasattr(fg, '_ch_states'):
                        fg_voltages = [
                            float(fg._ch_states[oi].current_voltage)
                            if oi < len(fg._ch_states) else 0.0
                            for oi in range(No_chans_out)
                        ]
                    else:
                        fg_voltages = [0.0] * No_chans_out
                except Exception:
                    fg_voltages = [0.0] * No_chans_out

                # Direct AO writers (e.g. Flow Failure) publish the voltage they
                # are driving via notify_output_voltage(); that override wins so
                # the output channels display the actual commanded value (AO is
                # output-only — there is no hardware read-back).
                ovr = getattr(self, 'ao_overrides', None)
                if ovr:
                    out_chans = getattr(self, '_out_channels', None) or []
                    for oi in range(No_chans_out):
                        if oi < len(out_chans) and out_chans[oi] in ovr:
                            fg_voltages[oi] = ovr[out_chans[oi]]

                n_frames = block.shape[1]
                out_bufs = [np.full(n_frames, fg_voltages[oi], dtype=np.float32)
                            for oi in range(No_chans_out)]
                for oi, out_buf in enumerate(out_bufs):
                    self.rb_write(out_base + oi, out_buf)

                # Append to recording buffers (thread-safe)
                if self.is_recording:
                    with self.record_lock:
                        for i, y in enumerate(transformed):
                            self.record_buffers[i].append(y)
                        for i, y in enumerate(transformed_custom):
                            self.record_buffers[No_chans + i].append(y)
                        for oi, out_buf in enumerate(out_bufs):
                            self.record_buffers[No_chans + No_chans_cust + oi].append(out_buf)

                self.total_written += block.shape[1]
                self.signals.data_ready.emit(None)

        except Exception as e:
            print("Acquisition error:", e)
        finally:
            self._close_all_tasks()
            self.signals.task_stopped.emit()

    def update_data(self, _):
        self.plot_data()
        self.update_value_displays()
        # Once real data exists the per-unit Y axes have been created, so any
        # saved static lines can be safely re-attached to them. Done once per run.
        if getattr(self, '_static_lines_pending', False) and self.total_written > 0:
            self._static_lines_pending = False
            self._reapply_static_lines()

    def _reapply_static_lines(self):
        """Re-draw any static reference lines the user added in a previous run.

        The Static Line dialog keeps its line definitions across plot resets,
        so after the graph is rebuilt on Run we ask it to redraw them onto the
        fresh axes. No-op if the dialog was never opened or holds no lines."""
        aw = getattr(self, 'appsWidget', None)
        if aw is None:
            return
        dlg = aw.app_dialog('static_lines')
        if dlg is None or not getattr(dlg, '_line_defs', None):
            return
        try:
            dlg.reapply_lines()
        except Exception as e:
            print(f"Could not reapply static lines: {e}")

    def create_graph(self):
        ps = self.plotSettingsWidget
        if ps.apply_ranges_var.isChecked():
            x_range, y_ranges = ps.run_axis_ranges()
        else:
            # Each axis starts as the previous run left it, before clear_plots
            # takes those axes down.
            x_range, y_ranges = self.graphDataWidget.carried_ranges()
            if self.selected_x_axis != self._last_x_name:
                x_range = None      # a range for a different X quantity
            if x_range is None:
                x_range = ps.output_x_lock()
        self._last_x_name = self.selected_x_axis

        self.graphDataWidget.clear_plots()
        self.axes_plotting = []
        self.selected_count = 0
        for i, row in plot_options.iterrows():
            plot_check_box = getattr(self.plotSettingsWidget, f"plot_check_box{i}")
            if plot_check_box.isChecked():
                color = self.plot_colors[self.selected_count % len(self.plot_colors)]
                self.axes_plotting.append(
                    PlottedChannel(color, row['Device Name'], unit_key(row['Units']), i)
                )
                self.selected_count += 1
        graph_title = self.plotSettingsWidget.title_label_entry.text()
        self.graphDataWidget.set_graph_title(graph_title)
        # The Axis Ranges choices are written once here; changes made in the
        # plot during the run are the user's and are not carried to the next.
        # Kept so a held run is redrawn with the ranges it started with.
        self._run_axis_ranges = (x_range, y_ranges)
        self.graphDataWidget.set_run_ranges(x_range, y_ranges)
        self.apply_overlay()    # X may have changed; drawn as the axes appear

    @staticmethod
    def _decimate_minmax(x, y, max_points):
        """Reduce (x, y) to ~max_points using min/max (peak) decimation:
        split into buckets and keep each bucket's min and max in time order,
        so spikes survive. The full span of x is preserved — this thins
        resolution, it does not trim the window. Display only; never touches
        recorded data."""
        n = int(y.shape[0])
        if max_points is None or max_points <= 0 or n <= max_points or n < 4:
            return x, y
        n_buckets = max(1, int(max_points) // 2)
        bucket = n // n_buckets
        if bucket < 2:
            return x, y
        head = n_buckets * bucket
        yb = y[:head].reshape(n_buckets, bucket)
        xb = x[:head].reshape(n_buckets, bucket)
        rows = np.arange(n_buckets)
        imin = yb.argmin(axis=1)
        imax = yb.argmax(axis=1)
        lo_first = imin <= imax
        i1 = np.where(lo_first, imin, imax)
        i2 = np.where(lo_first, imax, imin)
        x_out = np.empty(n_buckets * 2, dtype=x.dtype)
        y_out = np.empty(n_buckets * 2, dtype=y.dtype)
        x_out[0::2] = xb[rows, i1]; x_out[1::2] = xb[rows, i2]
        y_out[0::2] = yb[rows, i1]; y_out[1::2] = yb[rows, i2]
        if head < n:        # keep the most-recent tail at full resolution
            x_out = np.concatenate([x_out, x[head:]])
            y_out = np.concatenate([y_out, y[head:]])
        return x_out, y_out

    def get_x_data(self, n):
        if self.selected_x_axis == "Samples":
            # Position at actual sample numbers in the recording
            start = max(0, self.total_written - n)
            return np.arange(start, start + n, dtype=np.int64)
        elif self.selected_x_axis == "Run Duration":
            # Position at actual elapsed time in the recording
            end_t = self.total_written / self.sample_rate
            start_t = max(0.0, end_t - n / self.sample_rate)
            return np.linspace(start_t, end_t, n, endpoint=False).astype(np.float32)
        else:
            match = combined_settings[combined_settings['Device Name'] == self.selected_x_axis].index
            if not match.empty:
                ch = int(match[0])
                xbuf = self.rb_tail(ch, min(self.samples_to_plot, self.rings[ch].size))
                return xbuf[-n:]
            else:
                start = max(0, self.total_written - n)
                return np.arange(start, start + n, dtype=np.int64)

    def plot_data(self):
        if not self.axes_plotting:
            return

        pencil_selected = self.channelSettingsWidget.pen_var.isChecked()
        # Only plot samples actually written — avoids showing pre-zero padding
        n_avail = min(self.samples_to_plot, self.total_written)
        if n_avail <= 0:
            return

        # Compute x-axis once — it is identical for every channel
        x = self.get_x_data(n_avail)

        for color, device_name, units, ch_index in self.axes_plotting:
            y = self.rb_tail(ch_index, n_avail)
            if y.size == 0:
                continue
            # x may be longer than y if a channel ring is smaller; trim to match
            xt = x[-y.size:]
            # Decimate to the display cap so the whole window stays in view at
            # lower resolution (instead of being truncated). Drawing only —
            # the recorded/saved data is never decimated.
            xt, yd = self._decimate_minmax(xt, y, max_plot_points)
            self.graphDataWidget.update_plot(device_name, xt, yd, color, pencil_selected, unit=units)

        self.graphDataWidget.plot_widget.setLabel('bottom', self._x_axis_label())

    def _x_axis_label(self, x_name=None):
        """X axis caption: the selection, with units when it is a data channel."""
        x_name = self.selected_x_axis if x_name is None else x_name
        x_units = ""
        if x_name not in ("Run Duration", "Samples"):
            x_match = combined_settings[combined_settings['Device Name'] == x_name]
            if not x_match.empty and 'Units' in x_match.columns:
                x_units = str(x_match['Units'].iloc[0])
                if x_units in ("nan", "None", ""):
                    x_units = "V"   # output channels default to volts
        return f"{x_name} [{x_units}]" if x_units else x_name

    def update_value_displays(self):
        """Push the latest single-sample reading for every channel (input,
        custom, output) into the CalibrationWindow, if it is open.
        Digital channels are polled separately by _poll_digital_channels."""
        cal_win = getattr(self.channelSettingsWidget, '_cal_window', None)
        if cal_win is None or not cal_win.isVisible():
            return
        # The three channel kinds sit consecutively in the ring buffer.
        base = 0
        for kind, count in (('input', No_chans),
                            ('custom', No_chans_cust),
                            ('output', No_chans_out)):
            for i in range(count):
                y = self.rb_tail(base + i, 1)
                cal_win.update_reading(kind, i, float(y[-1]) if y.size > 0 else float('nan'))
            base += count
        # Digital channels are polled by _poll_digital_channels (250 ms timer).
        # If DAQ is NOT running there is no update loop, so kick the poll here too.
        if not self.continue_running:
            self._poll_digital_channels()

    # ------------------------------------------------------------------
    # Digital channel state tracking + polling
    # ------------------------------------------------------------------
    # digital_states: {channel_str -> bool}  — single source of truth.
    # DI channels: updated by the poll timer via NI hardware read.
    # DO channels: updated by notify_digital_state() called after each write
    #              from CustomTriggers/_write_do and FlowFailure/_do_write.
    # ------------------------------------------------------------------

    def _start_dig_poll(self):
        """Start a 250 ms timer to poll DI channels and push all states to
        the CalibrationWindow."""
        if getattr(self, '_dig_poll_timer', None) is not None:
            return
        if len(default_settings_dig) == 0:
            return
        if not hasattr(self, 'digital_states'):
            self.digital_states = {}
        self._dig_poll_timer = QTimer(self)
        self._dig_poll_timer.setInterval(250)
        self._dig_poll_timer.timeout.connect(self._poll_digital_channels)
        self._dig_poll_timer.start()

    def _stop_dig_poll(self):
        t = getattr(self, '_dig_poll_timer', None)
        if t is not None:
            t.stop()
            t.deleteLater()
            self._dig_poll_timer = None

    def notify_digital_state(self, channel_str: str, state: bool):
        """Called by CT / FF after every DO write so the cal window stays live.
        channel_str is the bare channel string, e.g. 'port0/line0'."""
        if not hasattr(self, 'digital_states'):
            self.digital_states = {}
        self.digital_states[channel_str] = state
        # Push immediately to the cal window if open
        self._push_digital_to_cal_win()

    # ------------------------------------------------------------------
    # Manual digital outputs (CalibrationWindow On/Off toggles)
    # ------------------------------------------------------------------

    def register_manual_do(self, channel_str: str, state: bool):
        """Remember which lines were switched on by hand from the cal window.

        Only these are cleared on Stop — lines driven by Custom Triggers or Flow
        Failure belong to those apps and are left alone.
        """
        if not hasattr(self, '_manual_do_on'):
            self._manual_do_on = set()
        if state:
            self._manual_do_on.add(channel_str)
        else:
            self._manual_do_on.discard(channel_str)

    def _manual_digital_outputs_off(self):
        """Drive every hand-switched output line low. Called on Stop."""
        channels = getattr(self, '_manual_do_on', None)
        if not channels:
            return
        rows = {str(r['Channel']): r for _, r in default_settings_dig.iterrows()}
        for ch in sorted(channels):
            row = rows.get(ch)
            try:
                dig_daq = _row_device(row, selected_daq) if row is not None else selected_daq
                with nidaqmx.Task() as t:
                    t.do_channels.add_do_chan(f"{dig_daq}/{ch}")
                    t.write(False)
            except Exception as e:
                print(f"Stop: could not clear digital output {ch}: {e}")
            if not hasattr(self, 'digital_states'):
                self.digital_states = {}
            self.digital_states[ch] = False
        self._manual_do_on = set()
        self._push_digital_to_cal_win()
        cal_win = getattr(self.channelSettingsWidget, '_cal_window', None)
        if cal_win is not None and hasattr(cal_win, 'all_digital_outputs_off'):
            # Lines are already low — just resync the buttons.
            cal_win.all_digital_outputs_off(write=False)

    def notify_output_voltage(self, channel_str: str, voltage):
        """Called by Flow Failure after each AO write so the output channels
        display the voltage actually being driven. AO is output-only (no
        hardware read-back), so the output ring buffer normally mirrors the
        Function Generator's state; this override lets a direct writer feed it
        instead. Pass voltage=None to release the override and hand the display
        back to the Function Generator. channel_str is the bare AO channel,
        e.g. 'ao0'. Safe to call from a worker thread (simple dict update)."""
        if not hasattr(self, 'ao_overrides'):
            self.ao_overrides = {}
        if voltage is None:
            self.ao_overrides.pop(channel_str, None)
        else:
            self.ao_overrides[channel_str] = float(voltage)

    def _push_digital_to_cal_win(self):
        cal_win = getattr(self.channelSettingsWidget, '_cal_window', None)
        if cal_win is None or not cal_win.isVisible():
            return
        if not hasattr(self, 'digital_states'):
            return
        for i, row in default_settings_dig.iterrows():
            ch = str(row['Channel'])
            if ch in self.digital_states:
                val = 1.0 if self.digital_states[ch] else 0.0
                cal_win.update_reading('digital', i, val)

    def _poll_digital_channels(self):
        """Read DI channels via NI; use cached digital_states for DO channels."""
        cal_win = getattr(self.channelSettingsWidget, '_cal_window', None)
        if cal_win is None or not cal_win.isVisible():
            self._stop_dig_poll()
            return
        if not hasattr(self, 'digital_states'):
            self.digital_states = {}
        for i, row in default_settings_dig.iterrows():
            ch        = str(row['Channel'])
            direction = str(row.get('Direction', 'Input')).strip().lower()
            if direction == 'output':
                # DO: use the cached state written by CT/FF
                if ch in self.digital_states:
                    cal_win.update_reading('digital', i,
                                           1.0 if self.digital_states[ch] else 0.0)
                # else leave label as last value (no stale overwrite)
            else:
                # DI: live hardware read
                try:
                    dig_daq = _row_device(row, selected_daq)
                    with nidaqmx.Task() as t:
                        t.di_channels.add_di_chan(f"{dig_daq}/{ch}")
                        state = t.read()
                    if isinstance(state, (list, tuple)):
                        state = bool(state[0])
                    else:
                        state = bool(state)
                    self.digital_states[ch] = state
                    cal_win.update_reading('digital', i, 1.0 if state else 0.0)
                except Exception:
                    cal_win.update_reading('digital', i, float('nan'))

    def refresh_app_availability(self):
        """Re-evaluate every Apps-bar button, Run/Stop, and the cal window.

        The single place mutual exclusion is decided. Each app declares which
        resources it claims in its APP_SPEC and the registry works out the rest,
        so apps no longer disable each other by name — which is what made adding
        an app an O(n^2) edit and let a missed edit leave two apps fighting over
        the AO bus.
        """
        apps = getattr(self, 'appsWidget', None)
        if apps is None:
            return

        open_ids = apps.open_app_ids()

        # The Function Generator keeps the AO bus while generating even with its
        # window closed, so it is reported as holding its claims regardless.
        extra_held = set()
        fg = apps.app_dialog('function_generator')
        fg_running = fg is not None and getattr(fg, 'is_running', False)
        if fg_running:
            spec = app_registry.by_id('function_generator')
            if spec is not None:
                extra_held |= spec['claims']
                open_ids.add('function_generator')

        gating = app_registry.resolve_gating(
            open_ids,
            no_daq=NO_DAQ,
            daq_running=getattr(self, 'continue_running', False),
            extra_held=extra_held,
        )
        for app_id, (enabled, tip) in gating.items():
            btn = apps.app_button(app_id)
            if btn is not None:
                btn.setEnabled(enabled)
                btn.setToolTip(tip)

        # Run / Stop: locked while any app that drives them is open.
        run_locker = None
        for spec in app_registry.load_apps():
            if spec['id'] in open_ids and spec['locks_run_stop']:
                run_locker = spec
                break
        try:
            ow = self.outputSettingsWidget
            if getattr(self, 'continue_running', False):
                pass          # start_task()/disable_buttons owns this state
            else:
                tip = (f"Close {run_locker['label']} to use Run/Stop manually"
                       if run_locker else "")
                ow.start_button.setEnabled(not NO_DAQ and run_locker is None)
                ow.start_button.setToolTip(tip)
                ow.stop_button.setEnabled(run_locker is None)
                ow.stop_button.setToolTip(tip)
        except AttributeError:
            pass

        # The Function Generator's start/stop arrives here, and it decides
        # whether settings can be edited too.
        self._refresh_settings_actions()
        self._refresh_recording_length_controls()
        self._notify_cal_window_app_state()

    def _refresh_recording_length_controls(self):
        """Auto Stop and its duration — the one place they are decided.

        Unticked and greyed while an open app decides how long a recording
        lasts (APP_SPEC 'owns_recording_length', e.g. Custom Triggers' stopping
        event), so the Auto Stop timer cannot end its recording early. Greyed
        while recording, since the timer is set when a recording starts.
        """
        ps = getattr(self, 'plotSettingsWidget', None)
        apps = getattr(self, 'appsWidget', None)
        if ps is None or apps is None:
            return
        open_ids = apps.open_app_ids()
        owner = next((spec for spec in app_registry.load_apps()
                      if spec['id'] in open_ids
                      and spec.get('owns_recording_length')), None)
        if owner is not None:
            ps.auto_stop_var.setChecked(False)
        enabled = owner is None and not getattr(self, 'is_recording', False)
        for w in (ps.auto_stop_var, ps.recording_duration_entry,
                  ps.recording_duration_unit):
            w.setEnabled(enabled)
        tip = (f"Recording length is set by {owner['label']}"
               if owner is not None else "")
        ps.auto_stop_var.setToolTip(tip)
        ps.recording_duration_entry.setToolTip(tip)

    def _notify_cal_window_app_state(self):
        """Enable/disable Calibrate inside an open CalibrationWindow.

        Which apps block calibration comes from their APP_SPEC
        (blocks_calibration), so a new app is covered without editing this.
        """
        cal_win = getattr(self.channelSettingsWidget, '_cal_window', None)
        if cal_win is None or not cal_win.isVisible():
            return
        apps = getattr(self, 'appsWidget', None)
        blocker = app_registry.calibration_blocker(
            apps.open_app_ids() if apps is not None else set())
        if blocker:
            cal_win.set_calibrate_allowed(False, f"Close {blocker} before calibrating")
        else:
            cal_win.set_calibrate_allowed(True, "")

        # Manual digital-output toggles: only while acquiring, and never while an
        # app that drives the digital bus is open (which app that is comes from
        # APP_SPEC.blocks_digital_output, so a new app is covered without a
        # change here).
        if hasattr(cal_win, 'set_digital_output_allowed'):
            do_blocker = app_registry.digital_output_blocker(
                apps.open_app_ids() if apps is not None else set())
            if do_blocker:
                cal_win.set_digital_output_allowed(
                    False, f"Close {do_blocker} before switching digital outputs")
            elif not getattr(self, 'continue_running', False):
                cal_win.set_digital_output_allowed(
                    False, "Start the DAQ to switch digital outputs")
            else:
                cal_win.set_digital_output_allowed(True, "")

    def _refresh_record_button(self):
        """Start Recording: only while acquiring, not already recording, and
        with View / Calibrate closed.

        The one place this is decided. Calibrating changes Mx/b, the scaling of
        the very data being recorded, so a recording cannot start while that
        window is open — the reverse of start_recording() disabling the
        View / Calibrate button.
        """
        btn = self.outputSettingsWidget.record_button
        running = getattr(self, 'continue_running', False)
        cal_open = self.channelSettingsWidget.calibration_open()
        btn.setEnabled(running and not self.is_recording and not cal_open)
        btn.setToolTip("Close View / Calibrate before recording"
                       if running and not self.is_recording and cal_open else "")

    # ------------------------------------------------------------------
    # Overlay: a saved data file shown on Live Graph for reference only.
    # Drawn only if its X matches the live X, and each column only on the
    # live axis for its unit; anything else is left out and said so.
    # ------------------------------------------------------------------

    @staticmethod
    def _read_overlay_file(path):
        """{'path', 'x_header', 'x', 'columns': [(name, unit, y)]}; raises ValueError."""
        if path.lower().endswith('.csv'):
            try:
                df = pd.read_csv(path)
            except Exception as e:
                raise ValueError(f"The file could not be read.\n\nDetails: {e}")
        else:
            ok, msg = _validate_data_file(path)
            if not ok:
                raise ValueError(msg)
            df = pd.read_excel(path)
        if df.empty or len(df.columns) < 2:
            raise ValueError("A data file needs an X column and at least one channel column.")
        x = pd.to_numeric(df.iloc[:, 0], errors='coerce')
        if x.isna().any():
            raise ValueError(f"The first column ('{df.columns[0]}') is the X axis, "
                             "so every value in it must be a number.")
        x = x.to_numpy(dtype=np.float64)
        x_header = str(df.columns[0]).strip()
        if x_header == "Time [ms]":
            x, x_header = x / 1000.0, "Time [s]"
        columns = []
        for col in df.columns[1:]:
            m = re.match(r'^(.+)\s+\[(.*)\]$', str(col))
            name = m.group(1).strip() if m else str(col)
            unit = unit_key(m.group(2)) if m else ""
            y = pd.to_numeric(df[col], errors='coerce')
            if y.isna().all():
                continue
            columns.append((name, unit, y.to_numpy(dtype=np.float32)))
        return {'path': path, 'x_header': x_header, 'x': x, 'columns': columns}

    def _overlay_x_matches(self, x_header, x_name):
        """Whether an overlay with X column `x_header` fits a live X of `x_name`."""
        if x_name == "Run Duration":
            return x_header == "Time [s]"
        if x_name == "Samples":
            return x_header == "Samples"
        m_file = re.match(r'^.+\s+\[(.*)\]$', x_header)
        m_live = re.match(r'^.+\s+\[(.*)\]$', self._x_axis_label(x_name))
        return bool(m_file and m_live
                    and unit_key(m_file.group(1)) == unit_key(m_live.group(1)))

    def _sync_overlay_controls(self):
        ps = self.plotSettingsWidget
        loaded = self._overlay_file is not None
        ps.overlay_entry.setText(self._overlay_file['path'] if loaded else "")
        if not loaded:
            ps.overlay_show_var.setChecked(False)
        ps.overlay_show_var.setEnabled(loaded)
        ps.overlay_remove_button.setEnabled(loaded)

    def browse_overlay(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Overlay Data File", "", "Data files (*.csv *.xlsx)")
        if not path:
            return
        try:
            self._overlay_file = self._read_overlay_file(path)
        except ValueError as e:
            QMessageBox.warning(self, "Overlay", str(e))
            return
        self.plotSettingsWidget.overlay_show_var.setChecked(True)
        self._sync_overlay_controls()
        self.apply_overlay(report=True)

    def remove_overlay(self):
        self._overlay_file = None
        self._sync_overlay_controls()
        self.apply_overlay()

    def apply_overlay(self, report=False):
        """Put the overlay on Live Graph, or take it off.

        `report` shows what was left out in a message box (when the user has
        just loaded or shown it); otherwise it only goes to the log, as at
        every Run.
        """
        gd = self.graphDataWidget
        ps = self.plotSettingsWidget
        ov = self._overlay_file
        if ov is None or not ps.overlay_show_var.isChecked():
            gd.set_overlay([])
            return
        # Compared with what Live Graph shows: the X of the last Run, or the
        # selection if nothing has run yet.
        x_name = getattr(self, 'selected_x_axis', None) or ps.x_axis_selection.currentText()

        def tell(msg):
            print(f"Overlay: {msg}")
            if report:
                QMessageBox.warning(self, "Overlay", msg)

        if not self._overlay_x_matches(ov['x_header'], x_name):
            gd.set_overlay([])
            tell(f"its X axis is '{ov['x_header']}' but the live X axis is "
                 f"'{self._x_axis_label(x_name)}', so it is not drawn.")
            return
        curves = [(f"{name} (overlay)", unit, ov['x'], y,
                   self.plot_colors[i % len(self.plot_colors)])
                  for i, (name, unit, y) in enumerate(ov['columns'])]
        gd.set_overlay(curves)
        live_units = {unit_key(row['Units']) for i, row in plot_options.iterrows()
                      if getattr(ps, f"plot_check_box{i}").isChecked()}
        skipped = [f"{name} [{unit}]" for name, unit, _y in ov['columns']
                   if unit not in live_units]
        if skipped:
            tell("these columns have no matching axis on the live graph, so they "
                 "are not drawn: " + ", ".join(skipped) + ".")

    # ------------------------------------------------------------------
    # Held runs ("Hold Run to Session Memory")
    #
    # With Hold on, a recording is not saved at Stop: its chunk files move into
    # their own folder and it gets a tab, Run 1, Run 2, ... next to Live Graph.
    # Settings, channels and the X axis are locked while Hold is on, so every
    # run is made from the same channels. Selecting a run's tab turns Run/Stop
    # into Re-Run (record over that run) and Delete Run.
    # ------------------------------------------------------------------

    def hold_runs_on(self):
        return self.plotSettingsWidget.hold_runs_var.isChecked()

    def _held_plot_title(self, which):
        """Plot title for a held-run view: the shared title, then `which`."""
        return f"{self._held_title} ({which})" if self._held_title else which

    def _run_title(self, run_idx):
        return self._held_plot_title(f"Run {run_idx + 1}")

    def _run_tab_offset(self):
        """Tab index of Run 1: after Live Graph, and All Runs when it is there."""
        return 2 if self._all_runs_view is not None else 1

    def _selected_run_index(self):
        """Index into held_runs of the shown Run tab; None on Live Graph / All Runs."""
        i = self.graphTabs.currentIndex()
        offset = self._run_tab_offset()
        return i - offset if i >= offset else None

    def _apply_session_locks(self, running):
        """Everything locked by acquiring or by Hold, decided in one place.

        `running` is passed in because disable_buttons() runs before
        continue_running is set.
        """
        ps = self.plotSettingsWidget
        hold = self.hold_runs_on()
        ps.set_selection_locked(running or hold)
        # Hold can be turned off only while no runs depend on it.
        ps.hold_runs_var.setEnabled(not running and not self.held_runs)
        self._refresh_autosave_controls(running)
        self._refresh_settings_actions(running)
        self._refresh_run_controls(running)
        self.session_state_changed.emit()     # Autosave's enabled state

    def _refresh_autosave_controls(self, running=None):
        """Session Autosave's checkbox and Browse.

        It saves held runs, so it needs Hold, and is switched off when Hold
        goes off. Browse is greyed while it is on and holds runs: those runs
        are already in its file, so pointing it elsewhere would split the
        session. Delete the runs (or turn Autosave off) to choose another file.
        """
        if running is None:
            running = getattr(self, 'continue_running', False)
        ps = self.plotSettingsWidget
        hold = self.hold_runs_on()
        if not hold:
            ps.autosave_var.setChecked(False)
        ps.autosave_var.setEnabled(hold and not running)
        in_use = ps.autosave_var.isChecked() and bool(self.held_runs)
        ps.autosave_browse_button.setEnabled(hold and not running and not in_use)
        ps.autosave_browse_button.setToolTip(
            "Session Autosave already holds these runs \u2014 delete all runs "
            "to choose another file" if in_use else "")

    def _refresh_settings_actions(self, running=None):
        """System Settings / Load Settings: usable only while nothing is
        acquiring, the Function Generator is not generating, no app that takes
        over Run/Stop is open, and no runs are held. Hold on with no runs yet
        is fine — refresh_ui() keeps Hold on through the reload a settings
        change causes.

        An open Run/Stop app (APP_SPEC 'locks_run_stop': Flow Failure,
        Proportional Sweep, Custom Triggers) holds on to the main window's
        panels — the graph, the plot checkboxes, the axis controls — and a
        settings change rebuilds those panels underneath it.

        Called from _apply_session_locks (acquisition, Hold) and from
        refresh_app_availability (which the Function Generator's start/stop
        and every app opening or closing trigger), so each of those changes
        re-decides it.
        """
        if not hasattr(self, 'system_settings_action'):
            return
        if running is None:
            running = getattr(self, 'continue_running', False)
        apps = getattr(self, 'appsWidget', None)
        fg = apps.app_dialog('function_generator') if apps is not None else None
        fg_running = fg is not None and getattr(fg, 'is_running', False)
        open_ids = apps.open_app_ids() if apps is not None else set()
        run_app = next((spec for spec in app_registry.load_apps()
                        if spec['id'] in open_ids and spec['locks_run_stop']), None)
        runs_held = self.hold_runs_on() and bool(self.held_runs)
        if running:
            tip = ""
        elif fg_running:
            tip = "Stop the Function Generator before editing channel settings"
        elif run_app is not None:
            tip = f"Close {run_app['label']} to change settings"
        elif runs_held:
            tip = "Settings are locked while runs are held"
        else:
            tip = ""
        enabled = (not running and not fg_running and run_app is None
                   and not runs_held)
        for action, status in (
                (self.system_settings_action, "Edit channels, transducers and expert settings"),
                (self.load_settings_action, "Load a different channel-settings workbook")):
            action.setEnabled(enabled)
            action.setToolTip(tip)
            action.setStatusTip(tip or status)

    # ---- Session Autosave, for apps (see session_state_changed) --------
    def session_autosave_state(self):
        """{'hold', 'on', 'path', 'editable'} of Session Settings -> Autosave."""
        ps = self.plotSettingsWidget
        return {'hold': self.hold_runs_on(),
                'on': ps.autosave_var.isChecked(),
                'path': ps.autosave_path_entry.text(),
                'editable': ps.autosave_var.isEnabled()}

    def set_session_autosave(self, checked):
        """Turn Session Autosave on or off as if its checkbox were clicked —
        including asking for a workbook when none is chosen yet."""
        ps = self.plotSettingsWidget
        if not ps.autosave_var.isEnabled():
            return
        ps.autosave_var.setChecked(checked)
        self.on_autosave_clicked(checked)

    def _refresh_run_controls(self, running=None):
        """Run/Stop labels, the next-run field and the save actions."""
        if running is None:
            running = getattr(self, 'continue_running', False)
        on_run = self._selected_run_index() is not None and not running
        ow = self.outputSettingsWidget
        ow.start_button.setText("Re-Run" if on_run else "Run")
        ow.stop_button.setText("Delete Run" if on_run else "Stop")
        on_all = (self._all_runs_view is not None and not running
                  and self.graphTabs.currentIndex() == 1)
        ow.record_button.setVisible(not on_all)
        ow.delete_all_button.setVisible(on_all)
        ow.delete_all_button.setEnabled(bool(self.held_runs))

        if not self.hold_runs_on():
            label = ""
        elif self._rerun_index is not None:
            label = f"Run {self._rerun_index + 1} (overwrite)"
        else:
            label = f"Run {len(self.held_runs) + 1}"
        self.plotSettingsWidget.next_run_entry.setText(label)

        self.save_action.setEnabled(
            not running and (on_run or getattr(self, 'total_written', 0) > 0))
        self.save_all_runs_action.setEnabled(not running and bool(self.held_runs))
        self.save_session_action.setEnabled(not running and self.hold_runs_on())
        self.load_session_action.setEnabled(not running)

    def on_hold_clicked(self, checked):
        ps = self.plotSettingsWidget
        if checked:
            reply = QMessageBox.question(
                self, "Hold Run to Session Memory",
                "While this is on:\n\n"
                "  \u2022 Settings cannot be changed once a run is held.\n"
                "  \u2022 Channels cannot be added or removed and the X axis "
                "cannot be changed, so every run has the same channels.\n\n"
                "Each recording is kept as Run 1, Run 2, \u2026 instead of asking "
                "to save at Stop.",
                QMessageBox.Ok | QMessageBox.Cancel, QMessageBox.Cancel)
            if reply != QMessageBox.Ok:
                ps.hold_runs_var.setChecked(False)
                return
        # All Runs is there exactly while Hold is on.
        self._rebuild_run_tabs()
        self._apply_session_locks(getattr(self, 'continue_running', False))

    def on_title_edited(self):
        """Held runs share the title, so changing it while runs exist is confirmed.

        The title may be blank: a held run is then titled just "Run N"
        (see _held_plot_title)."""
        entry = self.plotSettingsWidget.title_label_entry
        text = entry.text().strip()
        if not self.held_runs or text == self._held_title:
            return
        # The message box takes focus from the entry, which would fire
        # editingFinished again and ask twice.
        entry.blockSignals(True)
        try:
            n = len(self.held_runs)
            change = (f"will change to \"{text}\"" if text else
                      "will be cleared; each run will be titled just \"Run N\"")
            reply = QMessageBox.question(
                self, "Change Graph Title",
                f"The title of all {n} held run(s) {change}.\n\nContinue?",
                QMessageBox.Ok | QMessageBox.Cancel, QMessageBox.Cancel)
            if reply != QMessageBox.Ok:
                entry.setText(self._held_title)
                return
            self._held_title = text
            self.graphDataWidget.set_graph_title(text)
            run_idx = self._selected_run_index()
            if self._shown_run_view is None:
                pass
            elif self._shown_run_view is self._all_runs_view:
                self._shown_run_view.set_graph_title(self._held_plot_title("All Runs"))
            elif run_idx is not None:
                self._shown_run_view.set_graph_title(self._run_title(run_idx))
        finally:
            entry.blockSignals(False)
        self._refresh_record_button()

    def on_run_button(self):
        """Run, or Re-Run when a held run's tab is shown."""
        self._rerun_index = self._selected_run_index()
        self._rerun_confirmed = False
        self.start_task()

    # ---- Runs started by apps (Custom Triggers) ------------------------
    def plotted_channels(self):
        """[(device name, unit, ring index)] of the channels selected for
        plotting: each ticked Y channel, then the X-axis channel when it is
        one (not Samples / Run Duration). The ring index is what rb_tail()
        takes, so an app can read the channel's live samples."""
        ps = self.plotSettingsWidget
        out, seen = [], set()
        for i, row in plot_options.iterrows():
            if getattr(ps, f"plot_check_box{i}").isChecked():
                out.append((str(row['Device Name']), unit_key(row['Units']), int(i)))
                seen.add(int(i))
        x_name = ps.x_axis_selection.currentText()
        if x_name not in ("Samples", "Run Duration"):
            match = combined_settings.index[combined_settings['Device Name'] == x_name]
            if len(match) and int(match[0]) not in seen:
                idx = int(match[0])
                out.append((x_name, unit_key(combined_settings.at[idx, 'Units']), idx))
        return out

    def selected_run_number(self):
        """Number of the held run whose tab is shown (1-based) while stopped —
        the run a start from that tab overwrites — else None."""
        if getattr(self, 'continue_running', False):
            return None
        idx = self._selected_run_index()
        return None if idx is None else idx + 1

    def start_run(self, overwrite_selected=False):
        """Start acquisition on behalf of an app.

        With overwrite_selected and a held run's tab shown, the recording
        replaces that run — the app has asked the user already, so Start
        Recording does not ask again (it may be triggered mid-event, with
        nobody watching). Otherwise a recording is added as a new run.
        """
        idx = self._selected_run_index() if overwrite_selected else None
        self._rerun_index = idx
        self._rerun_confirmed = idx is not None
        self.start_task()

    def on_stop_button(self):
        """Stop, or Delete Run when a held run's tab is shown while stopped."""
        run_idx = self._selected_run_index()
        if run_idx is not None and not getattr(self, 'continue_running', False):
            self._delete_run(run_idx)
        else:
            self.stop_task()

    def _on_graph_tab_changed(self, index):
        # Apps showing Start vs Re-Start follow the selected tab.
        self.session_state_changed.emit()
        # Only the shown run is drawn; leaving it drops its samples from memory.
        if self._shown_run_view is not None:
            self._shown_run_view.clear_plots()
            self._shown_run_view = None
        offset = self._run_tab_offset()
        run_idx = index - offset if index >= offset else None
        if self._all_runs_view is not None and index == 1:
            self._plot_all_runs(self._all_runs_view)
            self._shown_run_view = self._all_runs_view
        elif run_idx is not None and run_idx < len(self.held_runs):
            view = self._run_views[run_idx]
            self._plot_held_run(self.held_runs[run_idx], view, self._run_title(run_idx))
            self._shown_run_view = view
        self._sync_notes_field()
        self._refresh_run_controls()

    def _sync_notes_field(self):
        """Point the notes field at the shown tab: a run's notes, else Live's."""
        ps = self.plotSettingsWidget
        run_idx = self._selected_run_index()
        if run_idx is not None:
            text = self.held_runs[run_idx].notes
            hint = f"Notes for Run {run_idx + 1}; click Apply Notes to keep them"
        else:
            text, hint = self._live_notes, ps.LIVE_NOTES_HINT
        # Signals blocked: filling the field is not the user typing, and must
        # not reach Live Graph.
        ps.notes_entry.blockSignals(True)
        ps.notes_entry.setText(text)
        ps.notes_entry.blockSignals(False)
        ps.notes_entry.setPlaceholderText(hint)
        ps.apply_notes_button.setVisible(run_idx is not None)

    def on_notes_changed(self, text):
        """Typing goes straight to Live Graph, except on a Run tab (Apply Notes)."""
        if self._selected_run_index() is None:
            self._live_notes = text
            self.graphDataWidget.set_notes(text)

    def apply_run_notes(self):
        run_idx = self._selected_run_index()
        if run_idx is None:
            return
        text = self.plotSettingsWidget.notes_entry.text()
        self.held_runs[run_idx].notes = text
        if self._shown_run_view is not None:
            self._shown_run_view.set_notes(text)
        print(f"Notes applied to Run {run_idx + 1}.")

    def _rebuild_run_tabs(self, select=0):
        """Live Graph, then All Runs while Hold is on, then a tab per held run.

        Run tabs are numbered by position. `select` is the tab index to show.
        """
        tabs = self.graphTabs
        self._shown_run_view = None
        tabs.blockSignals(True)
        try:
            while tabs.count() > 1:
                view = tabs.widget(1)
                tabs.removeTab(1)
                view.deleteLater()
            self._all_runs_view = None
            if self.hold_runs_on():
                self._all_runs_view = GraphData(self)
                tabs.addTab(self._all_runs_view, "All Runs")
            self._run_views = []
            for k in range(len(self.held_runs)):
                view = GraphData(self)
                self._run_views.append(view)
                tabs.addTab(view, f"Run {k + 1}")
            tabs.setCurrentIndex(select)
        finally:
            tabs.blockSignals(False)
        self._on_graph_tab_changed(tabs.currentIndex())

    def _held_run_curves(self, run):
        """[(PlottedChannel, x, y)] for a held run's whole recording, read from disk."""
        arrays = self._load_ring_channels(run.chunk_files)

        def samples(idx):
            y = arrays[idx] if idx is not None and idx < len(arrays) else None
            if y is not None and run.max_samples:
                y = y[-run.max_samples:]
            return y

        curves = [(ch, samples(ch.ch_index)) for ch in run.channels]
        curves = [(ch, y) for ch, y in curves if y is not None]
        if run.x_index is not None:
            x = samples(run.x_index)
            if x is None:
                return []
        else:
            n = max((y.size for _, y in curves), default=0)
            x = np.arange(n, dtype=np.float64)
            if run.x_name == "Run Duration":
                x = x / run.rate
        out = []
        for ch, y in curves:
            m = min(x.size, y.size)
            out.append((ch, x[:m], y[:m]))
        return out

    def _plot_held_run(self, run, view, title):
        """Draw a held run's whole recording in `view`."""
        view.clear_plots()
        view.set_graph_title(title)
        view.set_run_ranges(run.x_range, run.y_ranges)
        for ch, x, y in self._held_run_curves(run):
            view.update_plot(ch.device_name, x, y, ch.color, unit=ch.units)
        view.plot_widget.setLabel('bottom', run.x_label)
        view.set_notes(run.notes)

    def _plot_all_runs(self, view):
        """Every held run overlaid in `view`, for comparing them.

        Each curve gets its own colour and is named "<channel> (Run N)". Every
        axis autoranges: the runs may each have started with different ranges.
        The runs share channels and X axis (Hold locks them), so they share
        axes. Notes are listed per run.
        """
        view.clear_plots()
        view.set_graph_title(self._held_plot_title("All Runs"))
        view.set_run_ranges(None, {})
        k = 0
        notes = []
        for number, run in enumerate(self.held_runs, 1):
            for ch, x, y in self._held_run_curves(run):
                color = self.plot_colors[k % len(self.plot_colors)]
                k += 1
                view.update_plot(f"{ch.device_name} (Run {number})", x, y, color,
                                 unit=ch.units)
            if run.notes:
                notes.append(f"Run {number}: {run.notes}")
        if self.held_runs:
            view.plot_widget.setLabel('bottom', self.held_runs[0].x_label)
        view.set_notes("\n".join(notes))

    def _hold_recording(self):
        """Keep the recording that just stopped as a held run (new, or Re-Run's)."""
        if not self.chunk_files:
            print("Nothing was recorded, so no run was held.")
            return
        folder = os.path.join(self.record_chunks_dir, f"held_{self._held_serial:04d}")
        self._held_serial += 1
        os.makedirs(folder, exist_ok=True)
        moved = [shutil.move(path, folder) for path in self.chunk_files]
        meta = os.path.join(self.record_chunks_dir, self._META_NAME)
        if os.path.exists(meta):
            shutil.move(meta, folder)
        # Moved, so _clear_chunks() must not go looking for them.
        self.chunk_files = []
        self.chunk_sample_counts = []

        ps = self.plotSettingsWidget
        x_name = self.selected_x_axis
        x_index = None
        if x_name not in ("Samples", "Run Duration"):
            match = combined_settings.index[combined_settings['Device Name'] == x_name]
            x_index = int(match[0]) if len(match) else None
        try:
            max_samples = int(ps.recording_duration_seconds() * self.sample_rate)
        except ValueError:
            max_samples = None
        x_range, y_ranges = self._run_axis_ranges
        if x_index is None:
            # Samples / Run Duration ranges describe the live window; a held
            # run shows its whole recording, so its X follows the data.
            x_range = None
        run = HeldRun(folder=folder, chunk_files=moved,
                      channels=list(self.axes_plotting),
                      x_name=x_name, x_index=x_index, x_label=self._x_axis_label(),
                      rate=self.sample_rate, max_samples=max_samples,
                      x_range=x_range, y_ranges=dict(y_ranges),
                      notes=self._live_notes)
        self._held_title = ps.title_label_entry.text().strip()

        target = self._rerun_index
        if target is not None and target < len(self.held_runs):
            shutil.rmtree(self.held_runs[target].folder, ignore_errors=True)
            self.held_runs[target] = run
            number = target + 1
        else:
            self.held_runs.append(run)
            number = len(self.held_runs)
        self._rebuild_run_tabs()
        print(f"Recording held as Run {number}.")
        self._autosave_runs()

    def _delete_run(self, run_idx):
        n = len(self.held_runs)
        number = run_idx + 1
        msg = f"Run {number} will be removed."
        if number < n:
            msg += (f" Run {number + 1}" + (f" to Run {n}" if number + 1 < n else "")
                    + " will be renamed one number lower.")
        if not self.held_runs[run_idx].saved:
            msg += "\n\nThis run has not been saved."
        reply = QMessageBox.question(self, "Delete Run", msg,
                                     QMessageBox.Ok | QMessageBox.Cancel,
                                     QMessageBox.Cancel)
        if reply != QMessageBox.Ok:
            return
        run = self.held_runs.pop(run_idx)
        shutil.rmtree(run.folder, ignore_errors=True)
        # Show the run that took its place, else the one before, else Live.
        offset = self._run_tab_offset()
        select = min(run_idx, len(self.held_runs) - 1) + offset if self.held_runs else 0
        self._rebuild_run_tabs(select)
        self._apply_session_locks(running=False)
        print(f"Run {number} deleted.")
        self._autosave_runs()

    def delete_all_runs(self):
        n = len(self.held_runs)
        if not n:
            return
        msg = f"This will permanently delete all {n} held run(s)."
        unsaved = sum(1 for run in self.held_runs if not run.saved)
        if unsaved:
            msg += f"\n\n{unsaved} of them have not been saved."
        reply = QMessageBox.warning(self, "Delete All Runs", msg,
                                    QMessageBox.Ok | QMessageBox.Cancel,
                                    QMessageBox.Cancel)
        if reply != QMessageBox.Ok:
            return
        self._discard_held_runs()
        self._rebuild_run_tabs(1)       # stay on All Runs, now empty
        self._apply_session_locks(running=False)
        print(f"All {n} run(s) deleted.")
        self._autosave_runs()

    def _save_held_run(self, run_idx):
        run = self.held_runs[run_idx]
        number = run_idx + 1
        filepath, _ = QFileDialog.getSaveFileName(
            self, f"Save Run {number}", "", "CSV Files (*.csv);;Excel Files (*.xlsx)")
        if not filepath:
            return False
        data = self._merge_recording(run.chunk_files, run.rate, run.max_samples,
                                     run.x_name, run.channels)
        if data is None:
            QMessageBox.warning(self, "No Data", f"Run {number} has no recorded data.")
            return False
        if self._write_data_file(data, filepath, f"Run {number}"):
            run.saved = True
            print(f"Run {number} saved → {os.path.basename(filepath)}")
            return True
        return False

    def _save_all_runs(self):
        """Every held run into one Excel file, a sheet each. True if written."""
        if not self.held_runs:
            return False
        filepath, _ = QFileDialog.getSaveFileName(
            self, "Save All Runs", "", "Excel Files (*.xlsx)")
        if not filepath:
            return False
        if not self._write_workbook(self._run_sheets(), filepath):
            return False
        for run in self.held_runs:
            run.saved = True
        print(f"{len(self.held_runs)} run(s) saved → {os.path.basename(filepath)}")
        return True

    def _run_sheets(self):
        """[(sheet name, columns)] for every held run, in tab order."""
        return [(f"Run {k}", self._merge_recording(run.chunk_files, run.rate,
                                                   run.max_samples,
                                                   run.x_name, run.channels))
                for k, run in enumerate(self.held_runs, 1)]

    # ---- Autosave ----------------------------------------------------
    def browse_autosave_path(self):
        """Pick the autosave workbook. True if one was chosen."""
        entry = self.plotSettingsWidget.autosave_path_entry
        path, _ = QFileDialog.getSaveFileName(
            self, "Session Autosave To", entry.text(), "DAQ Session (*.mcz)")
        if not path:
            return False
        if not path.lower().endswith('.mcz'):
            path += '.mcz'
        entry.setText(path)
        self._autosave_runs()
        return True

    def on_autosave_clicked(self, checked):
        ps = self.plotSettingsWidget
        if checked and not ps.autosave_path_entry.text():
            if not self.browse_autosave_path():
                ps.autosave_var.setChecked(False)
        elif checked:
            self._autosave_runs()       # bring the file up to the runs held now
        self._refresh_autosave_controls()

    def _autosave_runs(self):
        """With Session Autosave on, rewrite its session file to match the runs.

        The whole session every time (settings and every held run, the same
        .mcz Save Session writes), so a deleted or re-recorded run never
        lingers in it. An existing file is replaced without asking: choosing
        it for Session Autosave is the decision.
        """
        ps = self.plotSettingsWidget
        path = ps.autosave_path_entry.text()
        if not ps.autosave_var.isChecked() or not path:
            return
        try:
            self._write_session(path)
        except Exception as e:
            QMessageBox.warning(self, "Session Autosave Failed",
                                f"The session could not be written to\n{path}\n\n{e}")
            return
        print(f"Session autosaved: {len(self.held_runs)} run(s) \u2192 {os.path.basename(path)}")

    # ---- Sessions (.mcz) ---------------------------------------------
    # A session is a zip: the active settings workbook as settings.xlsx,
    # session.json (title, channels, X axis and each run's details), and each
    # run's chunk files under run_N/. Written read-only so it is not edited or
    # overwritten by accident; Save Session over an existing one (which the
    # save dialog has the user confirm) lifts that first.
    _SESSION_VERSION = 1

    def save_session(self):
        """Save the settings and every held run to a .mcz. True if written."""
        if not self.hold_runs_on() or getattr(self, 'continue_running', False):
            return False
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Session", "", "DAQ Session (*.mcz)")
        if not path:
            return False
        if not path.lower().endswith('.mcz'):
            path += '.mcz'
        try:
            self._write_session(path)
        except Exception as e:
            QMessageBox.critical(self, "Save Session Failed",
                                 f"The session could not be written.\n\n{e}")
            return False
        print(f"Session saved: {len(self.held_runs)} run(s) \u2192 {os.path.basename(path)}")
        return True

    def _write_session(self, path):
        """Write the settings and every held run to the .mcz at `path`.

        Shared by Save Session and Session Autosave, so the two cannot drift.
        Replaces an existing file (lifting its read-only flag); raises if it
        cannot be written, leaving any existing file as it was.
        """
        ps = self.plotSettingsWidget
        checked = [row['Device Name'] for i, row in plot_options.iterrows()
                   if getattr(ps, f"plot_check_box{i}").isChecked()]
        meta = {
            'version': self._SESSION_VERSION,
            'title': self._held_title or ps.title_label_entry.text().strip(),
            'x_name': ps.x_axis_selection.currentText(),
            'channels': checked,
            'runs': [{
                'dir': f"run_{k}",
                'x_name': run.x_name,
                'x_label': run.x_label,
                'rate': run.rate,
                'max_samples': run.max_samples,
                'x_range': run.x_range,
                'y_ranges': run.y_ranges,
                'notes': run.notes,
                'channels': [ch.device_name for ch in run.channels],
            } for k, run in enumerate(self.held_runs, 1)],
        }
        tmp = path + '.tmp'
        try:
            with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED) as z:
                z.write(_get_settings_path(), 'settings.xlsx')
                z.writestr('session.json', json.dumps(meta, indent=2))
                for k, run in enumerate(self.held_runs, 1):
                    for chunk in run.chunk_files:
                        z.write(chunk, f"run_{k}/{os.path.basename(chunk)}")
            if os.path.exists(path):
                os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
            os.replace(tmp, path)
            os.chmod(path, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
        except Exception:
            try:
                os.remove(tmp)
            except Exception:
                pass
            raise
        for run in self.held_runs:
            run.saved = True

    @staticmethod
    def _extract_session(path, dest):
        """Unzip a .mcz into `dest`; returns its session.json. Raises on a bad file."""
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            if 'session.json' not in names or 'settings.xlsx' not in names:
                raise ValueError("this is not a DAQ session file "
                                 "(session.json or settings.xlsx is missing)")
            root = os.path.realpath(dest)
            for name in names:
                # Refuse entries that would land outside `dest`.
                target = os.path.realpath(os.path.join(dest, name))
                if not target.startswith(root + os.sep):
                    raise ValueError(f"unsafe entry in session file: {name}")
            z.extractall(dest)
        with open(os.path.join(dest, 'session.json')) as f:
            meta = json.load(f)
        if meta.get('version') != VoltageContinuousInput._SESSION_VERSION:
            raise ValueError(f"unsupported session version {meta.get('version')!r}")
        return meta

    def load_session(self):
        """Apply a .mcz: its settings, Hold on, its channels, and all its runs."""
        if getattr(self, 'continue_running', False):
            QMessageBox.warning(self, "Stop First",
                                "Please stop the current acquisition before loading a session.")
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Session", "", "DAQ Session (*.mcz)")
        if not path:
            return

        tmpdir = tempfile.mkdtemp(prefix='daq_session_')
        try:
            try:
                meta = self._extract_session(path, tmpdir)
            except Exception as e:
                QMessageBox.critical(self, "Invalid Session File",
                                     f"This session could not be read.\n\n{e}")
                return
            settings_path = os.path.join(tmpdir, 'settings.xlsx')
            ok, msg = _validate_settings_file(settings_path)
            if not ok:
                QMessageBox.critical(self, "Invalid Session File", msg)
                return

            reply = QMessageBox.question(
                self, "Load Session",
                f"Load this session?\n\n{path}\n\n"
                f"Its settings become the active configuration and replace:\n"
                f"{_get_settings_path()}",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
            if reply != QMessageBox.Yes:
                return
            if self.held_runs:
                reply = QMessageBox.question(
                    self, "Replace Runs",
                    f"Loading a session replaces the {len(self.held_runs)} held "
                    "run(s).\n\nSave the current session first?",
                    QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel)
                if reply == QMessageBox.Cancel:
                    return
                if reply == QMessageBox.Yes and not self.save_session():
                    return

            self._discard_held_runs()
            self._rerun_index = None
            self._rerun_confirmed = False
            # Tabs follow at once, so a failure below cannot leave tabs for
            # runs that are gone.
            self._rebuild_run_tabs()
            self._apply_session_locks(running=False)
            try:
                _make_settings_active(settings_path, self)
            except Exception as e:
                QMessageBox.critical(self, "Load Session Failed",
                                     f"The session's settings could not be applied.\n\n{e}")
                return
            try:
                self.reset_device()
            except Exception as e:
                print(f"DAQ reset on session load: {e}")
            self._restore_session(meta, tmpdir)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        print(f"Session loaded: {len(self.held_runs)} run(s) from {os.path.basename(path)}")

    def _restore_session(self, meta, tmpdir):
        """Set the channels, title and Hold from `meta`, and hold its runs."""
        ps = self.plotSettingsWidget
        index_of = {row['Device Name']: i for i, row in plot_options.iterrows()}
        wanted = set(meta.get('channels', []))
        for name, i in index_of.items():
            getattr(ps, f"plot_check_box{i}").setChecked(name in wanted)
        ps.x_axis_selection.setCurrentText(meta.get('x_name', 'Samples'))
        title = meta.get('title', '')
        ps.title_label_entry.setText(title)
        ps.hold_runs_var.setChecked(True)      # set, not clicked: no prompt
        self._held_title = title

        def as_range(r):
            return None if r is None else (float(r[0]), float(r[1]))

        missing = set()
        for k, r in enumerate(meta.get('runs', []), 1):
            channels = []
            for name in r.get('channels', []):
                if name not in index_of:
                    missing.add(name)
                    continue
                row = plot_options.loc[index_of[name]]
                color = self.plot_colors[len(channels) % len(self.plot_colors)]
                channels.append(PlottedChannel(color, name, unit_key(row['Units']),
                                               index_of[name]))
            x_name = r.get('x_name', 'Samples')
            x_index = None
            if x_name not in ("Samples", "Run Duration"):
                match = combined_settings.index[combined_settings['Device Name'] == x_name]
                x_index = int(match[0]) if len(match) else None
            folder = os.path.join(self.record_chunks_dir, f"held_{self._held_serial:04d}")
            self._held_serial += 1
            os.makedirs(folder, exist_ok=True)
            src = os.path.join(tmpdir, r['dir'])
            chunks = [shutil.move(p, folder)
                      for p in sorted(glob.glob(os.path.join(src, 'chunk_*.npz')))]
            # So crash recovery can save this run by its plotted channels too.
            with open(os.path.join(folder, self._META_NAME), 'w') as f:
                json.dump({'x_name': x_name,
                           'channels': [[ch.device_name, ch.units, ch.ch_index]
                                        for ch in channels]}, f)
            self.held_runs.append(HeldRun(
                folder=folder, chunk_files=chunks, channels=channels,
                x_name=x_name, x_index=x_index, x_label=r.get('x_label', x_name),
                rate=int(r.get('rate') or self._settings_rate()),
                max_samples=r.get('max_samples'),
                x_range=as_range(r.get('x_range')),
                y_ranges={u: as_range(v) for u, v in (r.get('y_ranges') or {}).items()},
                notes=r.get('notes', ''), saved=True))
        if missing:
            QMessageBox.warning(
                self, "Load Session",
                "These channels are in the session's runs but not in its settings, "
                f"so they are left out: {', '.join(sorted(missing))}.")

        self._rebuild_run_tabs()
        self._apply_session_locks(running=False)

    def _write_workbook(self, sheets, filepath):
        """Write [(sheet name, data or None)] to one .xlsx. True if written."""
        if not filepath.lower().endswith('.xlsx'):
            filepath += '.xlsx'
        written, truncated, empty = 0, [], []
        try:
            with pd.ExcelWriter(filepath) as writer:
                for name, data in sheets:
                    if data is None:
                        empty.append(name)
                        continue
                    df = pd.DataFrame(data)
                    if len(df) > self._EXCEL_MAX_ROWS:
                        truncated.append(name)
                        df = df.iloc[:self._EXCEL_MAX_ROWS]
                    df.to_excel(writer, sheet_name=name, index=False)
                    written += 1
                if not written:
                    # An .xlsx needs at least one sheet.
                    pd.DataFrame().to_excel(writer, sheet_name="No Data", index=False)
        except Exception as e:
            QMessageBox.critical(self, "Save Failed",
                                 f"The workbook could not be written.\n\n{e}")
            return False
        notes = []
        if truncated:
            notes.append(f"{', '.join(truncated)} exceeded Excel's limit of "
                         f"{self._EXCEL_MAX_ROWS:,} rows and were truncated. "
                         "Save a run on its own as .csv to keep all of it.")
        if empty:
            notes.append(f"{', '.join(empty)} had no recorded data and were left out.")
        if notes:
            QMessageBox.warning(self, "Saved With Warnings", "\n\n".join(notes))
        return True

    def _discard_held_runs(self):
        for run in self.held_runs:
            shutil.rmtree(run.folder, ignore_errors=True)
        self.held_runs = []
        try:
            os.rmdir(self.record_chunks_dir)
        except Exception:
            pass   # not empty or doesn't exist — fine

    def enable_buttons(self):
        self.channelSettingsWidget.clear_button.setEnabled(True)
        self.channelSettingsWidget._set_clear_mode(False)
        self._refresh_record_button()
        self.load_action.setEnabled(True)
        self.reset_daq_action.setEnabled(not NO_DAQ)
        # View/Calibrate button is always accessible; the internal Calibrate
        # button inside CalibrationWindow is disabled when CT or FF is open
        # (handled by _notify_cal_window_app_state called from those dialogs).
        self.channelSettingsWidget.calibrate_button.setEnabled(True)
        self.channelSettingsWidget.calibrate_button.setToolTip(
            ChannelSettings.CALIBRATE_TIP)
        # Notify the cal window of current app state (disables internal cal btn if needed)
        self._notify_cal_window_app_state()
        # Apps bar, Run/Stop, Auto Stop and the cal window all come from the
        # registry now.
        self.refresh_app_availability()
        for i in range(1, self.graphTabs.count()):
            self.graphTabs.setTabEnabled(i, True)
        self._apply_session_locks(running=False)
        self.plotSettingsWidget.plot_duration_entry.setEnabled(True)
        self.plotSettingsWidget.plot_duration_unit.setEnabled(True)
        self.plotSettingsWidget.title_label_entry.setEnabled(True)
        self.plotSettingsWidget.apply_ranges_var.setEnabled(True)
        self.plotSettingsWidget.axis_ranges_button.setEnabled(
            self.plotSettingsWidget.apply_ranges_var.isChecked())

    def stop_task(self):
        self.continue_running = False

    def on_task_stopped(self):
        if self.is_recording:
            self._finish_recording()
        self._rerun_index = None
        self._rerun_confirmed = False
        # Notes belong to one run: a held run has taken its copy by now.
        self.plotSettingsWidget.notes_entry.clear()
        # Stopping the run drops any output the user switched on by hand, so the
        # rig is not left energised by a window they may have closed.
        self._manual_digital_outputs_off()
        self.enable_buttons()

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def _read_cache_settings(self):
        """Read Cache to Disk and Cache Interval from expert_settings in the xlsx.
        Returns (cache_enabled: bool, interval_seconds: float).
        Defaults: enabled=True, interval=900 s."""
        try:
            df = pd.read_excel(_get_settings_path(), sheet_name="expert_settings")
            def _get(label, default):
                ser = df.loc[df["Setting"] == label, "Value"]
                return default if ser.empty or pd.isna(ser.iloc[0]) else ser.iloc[0]
            enabled = str(_get("Cache to Disk", "1")).strip() != "0"
            try:
                interval = float(_get("Cache Interval (s)", 900))
            except (ValueError, TypeError):
                interval = 900.0
            return enabled, interval
        except Exception:
            return True, 900.0

    def start_recording(self):
        if (self.hold_runs_on() and self._rerun_index is not None
                and not self._rerun_confirmed):
            n = self._rerun_index + 1
            reply = QMessageBox.question(
                self, "Re-Run",
                f"This recording will replace Run {n} when you click Stop.\n\n"
                f"Record over Run {n}?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply != QMessageBox.Yes:
                return

        # Clear any leftover chunks from a previous session
        self._clear_chunks()

        # Fresh recording buffers and chunk counter
        n_total = No_chans + No_chans_cust + No_chans_out
        with self.record_lock:
            self.record_buffers = [[] for _ in range(n_total)]
        self.chunk_files = []
        self.chunk_sample_counts = []
        self.chunk_counter = 0

        os.makedirs(self.record_chunks_dir, exist_ok=True)
        self._write_recording_meta()
        self.is_recording = True

        # Gray out recording controls while session is active
        self._refresh_record_button()
        self.load_action.setEnabled(False)
        # Reset Graph is locked out for the duration of a recording.
        self.channelSettingsWidget.clear_button.setEnabled(False)
        self.channelSettingsWidget.clear_button.setToolTip(
            "Unavailable while recording — stop the recording first.")
        self._refresh_recording_length_controls()
        self.channelSettingsWidget.calibrate_button.setEnabled(False)

        # Read cache settings from expert_settings in the xlsx
        cache_enabled, cache_interval_s = self._read_cache_settings()

        # Start cache-to-disk timer if enabled
        if cache_enabled:
            self.chunk_timer = QTimer()
            self.chunk_timer.timeout.connect(self._autosave_chunk)
            self.chunk_timer.start(int(cache_interval_s * 1000))

        # Start autostop timer if checked — just stops the DAQ, no auto-save
        if self.plotSettingsWidget.auto_stop_var.isChecked():
            try:
                duration_s = self.plotSettingsWidget.recording_duration_seconds()
            except ValueError:
                duration_s = 60.0
            self.autostop_timer = QTimer()
            self.autostop_timer.setSingleShot(True)
            self.autostop_timer.timeout.connect(self._autostop_recording)
            self.autostop_timer.start(int(duration_s * 1000))

    def _drain_record_buffers(self):
        """Take everything buffered so far, leaving the buffers empty.

        Held under the lock because the acquisition thread appends to these.
        Returns one concatenated array per channel, in ring-index order.
        """
        with self.record_lock:
            snapshot = [np.concatenate(buf) if buf else np.array([], dtype=np.float32)
                        for buf in self.record_buffers]
            self.record_buffers = [[] for _ in range(len(self.record_buffers))]
        return snapshot

    def _write_chunk(self, snapshot, error_prefix):
        """Write one .npz chunk and register it. Returns its path, or None on
        failure (reported with `error_prefix`)."""
        chunk_path = os.path.join(self.record_chunks_dir,
                                  f"chunk_{self.chunk_counter:06d}.npz")
        self.chunk_counter += 1
        try:
            np.savez(chunk_path, **{f'ch_{i}': data for i, data in enumerate(snapshot)})
        except Exception as e:
            print(f"{error_prefix}: {e}")
            return None
        self.chunk_files.append(chunk_path)
        self.chunk_sample_counts.append(snapshot[0].size)
        return chunk_path

    def _autosave_chunk(self):
        """Periodically called by chunk_timer. Flushes RAM buffer to a .npz chunk on disk."""
        if not self.is_recording:
            return

        snapshot = self._drain_record_buffers()
        if all(s.size == 0 for s in snapshot):
            return

        chunk_path = self._write_chunk(snapshot, "Autosave failed")
        if chunk_path is None:
            return
        print(f"Autosaved chunk: {_short_chunk_path(chunk_path)} "
              f"({snapshot[0].size} samples)")

        self._trim_chunks()

    def _trim_chunks(self):
        """Remove oldest chunk files so total stored duration <= recording_duration.
           Always keeps at least 2 chunks as a safety net."""
        try:
            recording_duration = self.plotSettingsWidget.recording_duration_seconds()
            max_samples = int(recording_duration * self.sample_rate)
        except (ValueError, AttributeError):
            return

        if max_samples <= 0:
            return

        min_chunks = 2
        total = sum(self.chunk_sample_counts)

        while total > max_samples and len(self.chunk_files) > min_chunks:
            oldest_path = self.chunk_files.pop(0)
            oldest_count = self.chunk_sample_counts.pop(0)
            total -= oldest_count
            try:
                os.remove(oldest_path)
                print(f"Trimmed old chunk: {_short_chunk_path(oldest_path)}")
            except Exception as e:
                print(f"Could not remove chunk {oldest_path}: {e}")

    def _autostop_recording(self):
        """Fires when autostop timer expires — stops DAQ entirely.
           on_task_stopped → _finish_recording → _stop_recording handles the rest."""
        if not self.is_recording:
            return
        print("Autostop triggered — stopping acquisition.")
        self.stop_task()

    def _finish_recording(self):
        """Called by on_task_stopped when the DAQ stops while recording was active."""
        if not self.is_recording:
            return
        print("DAQ stopped — finishing recording.")
        self._stop_recording()

    def _stop_recording(self):
        """Common teardown: stop timers, flush final buffer, save, clear chunks."""
        self.is_recording = False

        # Reset Graph becomes available again; its label follows the run state.
        self.channelSettingsWidget._set_clear_mode(
            getattr(self, 'continue_running', False))
        self.channelSettingsWidget.clear_button.setEnabled(True)
        # start_recording() disabled Load File. Restore it now unless acquisition
        # is still running, in which case enable_buttons() owns that state and
        # will re-enable it when the task stops.
        if not getattr(self, 'continue_running', False):
            self.load_action.setEnabled(True)
            self._refresh_run_controls()

        if self.chunk_timer is not None:
            self.chunk_timer.stop()
            self.chunk_timer = None
        if self.autostop_timer is not None:
            self.autostop_timer.stop()
            self.autostop_timer = None

        # Flush remaining RAM buffer as the final chunk
        snapshot = self._drain_record_buffers()
        if any(s.size > 0 for s in snapshot):
            self._write_chunk(snapshot, "Could not flush final buffer")

        # Prompt user to save — unless CustomTriggers pre-set a path for autosave.
        # With Hold on, the recording becomes a run tab instead, and that
        # includes one a Custom Triggers autosave was aimed at.
        pending = getattr(self, '_pending_autosave_path', None)
        if self.hold_runs_on():
            self._pending_autosave_path = None
            self._hold_recording()
        elif pending:
            self._pending_autosave_path = None
            self._merge_and_save(pending)
        else:
            filepath, _ = QFileDialog.getSaveFileName(
                self, "Save Recording", "",
                "CSV Files (*.csv);;Excel Files (*.xlsx)"
            )
            if filepath:
                self._merge_and_save(filepath)

        self._clear_chunks()

    @staticmethod
    def _settings_rate():
        """The sample rate recordings are made at, from the loaded settings."""
        return int(float(sample_rate))

    def _load_ring_channels(self, chunk_files):
        """Each ring index's samples across `chunk_files`, concatenated; None if absent."""
        n_total = No_chans + No_chans_cust + No_chans_out
        parts = [[] for _ in range(n_total)]
        for chunk_path in chunk_files:
            try:
                with np.load(chunk_path) as data:
                    for i in range(n_total):
                        key = f'ch_{i}'
                        if key in data.files:
                            parts[i].append(data[key])
            except Exception as e:
                print(f"Error loading chunk {chunk_path}: {e}")
        return [np.concatenate(p) if p else None for p in parts]

    def _data_columns(self, x_name, channels, samples_of, rate, sample_start=0):
        """{column: values} for a save: the X axis first, then each plotted Y channel.

        The one rule every save follows. Column 1 is the X axis as plotted —
        "Time [s]" for Run Duration, "Samples", or the X channel's own data —
        which is also what Load File reads back as X. Then the Y channels that
        were plotted, in plot order. Nothing else: an output channel's
        commanded level appears only when it is the X axis.

        `samples_of(ring_index)` returns that channel's samples (or None);
        all are aligned at their start. None when there is nothing to save.
        """
        ys = [(ch, samples_of(ch.ch_index)) for ch in channels]
        ys = [(ch, y) for ch, y in ys if y is not None]
        x = None
        if x_name not in ("Run Duration", "Samples"):
            match = combined_settings.index[combined_settings['Device Name'] == x_name]
            x = samples_of(int(match[0])) if len(match) else None
            if x is None:
                return None
        lengths = [y.size for _, y in ys] + ([x.size] if x is not None else [])
        n = min(lengths) if lengths else 0
        if n <= 0:
            return None
        if x_name == "Run Duration":
            columns = {"Time [s]": np.arange(n, dtype=np.float64) / rate}
        elif x_name == "Samples":
            columns = {"Samples": np.arange(sample_start, sample_start + n, dtype=np.int64)}
        else:
            columns = {self._x_axis_label(x_name): x[:n].astype(np.float64, copy=False)}
        for ch, y in ys:
            header = f"{ch.device_name} [{ch.units}]" if ch.units else ch.device_name
            columns[header] = y[:n].astype(np.float64, copy=False)
        return columns

    def _merge_recording(self, chunk_files, rate, max_samples, x_name, channels):
        """A recording's columns (see _data_columns), read from its chunk files.

        `max_samples` keeps the last that many samples of each channel (None
        keeps all).
        """
        arrays = self._load_ring_channels(chunk_files)

        def samples_of(idx):
            y = arrays[idx] if idx < len(arrays) else None
            if y is not None and max_samples is not None:
                y = y[-max_samples:]
            return y
        return self._data_columns(x_name, channels, samples_of, rate)

    # A recording's X axis and plotted channels, written beside its chunk files
    # so the startup recovery can save it by the same rule after a crash.
    _META_NAME = 'recording_meta.json'

    def _write_recording_meta(self):
        meta = {'x_name': self.selected_x_axis,
                'channels': [[ch.device_name, ch.units, ch.ch_index]
                             for ch in self.axes_plotting]}
        try:
            with open(os.path.join(self.record_chunks_dir, self._META_NAME), 'w') as f:
                json.dump(meta, f)
        except Exception as e:
            print(f"Could not write recording info for crash recovery: {e}")

    def _read_recording_meta(self, folder):
        """(x_name, channels) saved with a recording, or None if absent/unreadable."""
        try:
            with open(os.path.join(folder, self._META_NAME)) as f:
                meta = json.load(f)
            channels = [PlottedChannel(None, name, units, int(idx))
                        for name, units, idx in meta['channels']]
            return meta['x_name'], channels
        except Exception:
            return None

    def _all_channels_meta(self):
        """Fallback for a recording with no saved info: every input and custom
        channel against time."""
        channels = [PlottedChannel(None, row['Device Name'], unit_key(row['Units']), i)
                    for i, row in plot_options.iterrows()]
        return "Run Duration", channels

    def _merge_and_save(self, filepath):
        """Merge the current recording's chunk files into one saved data file."""
        rate = self._settings_rate()
        try:
            max_samples = int(self.plotSettingsWidget.recording_duration_seconds() * rate)
        except (ValueError, AttributeError):
            max_samples = None
        data = self._merge_recording(self.chunk_files, rate, max_samples,
                                     self.selected_x_axis, self.axes_plotting)
        if data is None:
            QMessageBox.warning(self, "No Data", "No recorded data found to save.")
            return
        if self._write_data_file(data, filepath, "The recording"):
            print(f"Recording saved: {len(next(iter(data.values())))} samples → "
                  f"{os.path.basename(filepath)}")

    def _clear_chunks(self):
        """Delete all chunk files and reset tracking lists."""
        for path in self.chunk_files:
            try:
                os.remove(path)
            except Exception:
                pass
        self.chunk_files = []
        self.chunk_sample_counts = []
        try:
            os.remove(os.path.join(self.record_chunks_dir, self._META_NAME))
        except Exception:
            pass
        try:
            if os.path.exists(self.record_chunks_dir) and not os.listdir(self.record_chunks_dir):
                os.rmdir(self.record_chunks_dir)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Save (ring buffer snapshot — not full recording)
    # ------------------------------------------------------------------

    def load_file(self):
        filepath, _ = QFileDialog.getOpenFileName(
            self, "Load Data File", "", "Excel Files (*.xlsx)"
        )
        if not filepath:
            return

        ok, msg = _validate_data_file(filepath)
        if not ok:
            QMessageBox.critical(self, "Invalid Data File", msg)
            return

        try:
            df = pd.read_excel(filepath)
        except Exception as e:
            QMessageBox.warning(self, "Load Error", f"Could not read file:\n{e}")
            return

        # Validated above, so nothing below can fail on the file's shape. Only
        # now is the existing plot discarded — a rejected file used to wipe it.
        # A loaded file is drawn on Live Graph, so show it.
        self.graphTabs.setCurrentIndex(0)
        self.graphDataWidget.clear_plots()

        # First column is always the X axis
        x_col = df.columns[0]
        x_data = pd.to_numeric(df[x_col], errors="coerce").to_numpy(dtype=np.float64)

        if x_col == "Time [ms]":
            x_data = x_data / 1000.0
            x_label = "Time [s]"
        else:
            # "Time [s]", "Samples" and any channel-as-X header all plot as-is;
            # the header already carries the name and unit.
            x_label = x_col

        data_cols = list(df.columns[1:])

        skipped = []
        for i, col in enumerate(data_cols):
            m = re.match(r'^(.+)\s+\[(.+)\]$', col)
            name = m.group(1).strip() if m else col
            unit = m.group(2).strip() if m else ""
            y_num = pd.to_numeric(df[col], errors="coerce")
            # A single text column should not take the whole load down with it.
            if y_num.isna().all():
                skipped.append(str(col))
                continue
            y = y_num.to_numpy(dtype=np.float32)
            color = self.plot_colors[i % len(self.plot_colors)]
            self.graphDataWidget.update_plot(name, x_data, y, color, unit=unit)
        if skipped:
            print("Load: skipped non-numeric column(s): " + ", ".join(skipped))

        self.graphDataWidget.plot_widget.setLabel('bottom', x_label)
        self.graphDataWidget.set_graph_title(os.path.basename(filepath))
        print(f"Loaded: {os.path.basename(filepath)} "
              f"({len(x_data)} samples, {len(data_cols)} channels, X={x_label})")

    def save_data(self):
        """Save the shown held run, or else what is in the ring buffer.

        On a Run tab this is that run's full recording. On Live Graph it is the
        ring buffer, not the full recording.
        """
        run_idx = self._selected_run_index()
        if run_idx is not None:
            self._save_held_run(run_idx)
            return
        filepath, _ = QFileDialog.getSaveFileName(
            self, "Save Data", "",
            "CSV Files (*.csv);;Excel Files (*.xlsx)"
        )
        if not filepath:
            return

        length = min(self.total_written, self.rings[0].size)
        if length <= 0:
            QMessageBox.warning(self, "No Data", "No data has been acquired yet.")
            return

        data_output = self._data_columns(
            self.selected_x_axis, self.axes_plotting,
            lambda idx: self.rb_tail(idx, length), self.sample_rate,
            sample_start=max(0, self.total_written - length))
        if data_output is None:
            QMessageBox.warning(self, "No Data", "No plotted channels to save.")
            return
        self._write_data_file(data_output, filepath, "The data")

    _EXCEL_MAX_ROWS = 1_048_576

    def _write_data_file(self, data_output, filepath, what):
        """Write `data_output` to CSV or Excel, chosen by the file extension.

        Excel silently loses rows past its limit, so a too-long frame is
        truncated and the user told. `what` names the data in that warning.
        """
        df = pd.DataFrame(data_output)
        if filepath.lower().endswith('.csv'):
            df.to_csv(filepath, index=False)
            return True
        if len(df) > self._EXCEL_MAX_ROWS:
            QMessageBox.warning(
                self, "Row Limit Exceeded",
                f"{what} has {len(df):,} rows but Excel supports a maximum of "
                f"{self._EXCEL_MAX_ROWS:,}. Data will be truncated.\n\n"
                f"Tip: save as .csv to keep all data."
            )
            df = df.iloc[:self._EXCEL_MAX_ROWS]
        df.to_excel(filepath, index=False)
        return True

    def reset_device(self):
        _safe_reset_device(selected_daq)

    def closeEvent(self, event):
        # ── Gate 1: is anything actively driving hardware? ────────────────
        # Asked before anything is torn down, and before the recording question,
        # so a user who did not mean to close is not first asked whether to save.
        # Names come from each app's APP_SPEC, so a new app is covered here
        # without editing this method.
        busy = []
        aw = getattr(self, 'appsWidget', None)
        if aw is not None:
            for app_id, dlg in getattr(aw, '_app_dialogs', {}).items():
                if dlg is None:
                    continue
                spec = app_registry.by_id(app_id)
                label = spec['label'] if spec else app_id
                if getattr(dlg, '_is_running', False):
                    busy.append(f"{label} — test in progress")
                elif getattr(dlg, 'is_running', False):
                    # Function Generator: is_running is a property over its
                    # per-channel states, true whenever any channel generates.
                    busy.append(f"{label} — generating output")
        if busy:
            reply = QMessageBox.question(
                self, "Still Running",
                "The following are still running:\n\n  • "
                + "\n  • ".join(busy)
                + "\n\nClosing now will abort them and zero all analog outputs."
                  "\n\nClose anyway?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,          # default is the safe answer
            )
            if reply != QMessageBox.Yes:
                event.ignore()
                return

        if self.is_recording:
            reply = QMessageBox.question(
                self, "Recording Active",
                "A recording is in progress. Stop and save before closing?",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel
            )
            if reply == QMessageBox.Cancel:
                event.ignore()
                return
            if reply == QMessageBox.Yes:
                self._stop_recording()

        unsaved = [k + 1 for k, run in enumerate(self.held_runs) if not run.saved]
        if unsaved:
            names = ", ".join(f"Run {k}" for k in unsaved)
            reply = QMessageBox.question(
                self, "Unsaved Runs",
                f"These held runs have not been saved: {names}.\n\n"
                "Save all runs to one Excel file before closing?\n"
                "Choosing No discards them.",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel)
            if reply == QMessageBox.Cancel:
                event.ignore()
                return
            if reply == QMessageBox.Yes and not self._save_all_runs():
                event.ignore()
                return
        self._discard_held_runs()

        self.continue_running = False
        if hasattr(self, 'acquisition_thread') and self.acquisition_thread.is_alive():
            self.acquisition_thread.join(timeout=1.0)

        # Close all open child apps so their AO / DAQ tasks are released.
        # Generic over the registry: a new app is cleaned up without editing
        # this. Apps expose their own release hooks, which are used if present.
        aw = getattr(self, 'appsWidget', None)
        if aw is not None:
            for _app_id, dlg in list(getattr(aw, '_app_dialogs', {}).items()):
                if dlg is None:
                    continue
                # Release hardware first, while the dialog is still alive.
                for hook in ('safe_close_ao', '_safe_shutdown'):
                    fn = getattr(dlg, hook, None)
                    if callable(fn):
                        try:
                            fn()
                        except Exception:
                            print(f"{_app_id}.{hook}() failed during shutdown:\n"
                                  + traceback.format_exc())
                        break
                # Don't pop a modal mid-shutdown if a test is mid-run; those
                # workers are daemon threads and exit with the process.
                if getattr(dlg, '_is_running', False):
                    continue
                try:
                    dlg.close()
                except Exception:
                    pass
        csw = getattr(self, 'channelSettingsWidget', None)
        cal = getattr(csw, '_cal_window', None) if csw is not None else None
        if cal is not None:
            try:
                cal.close()
            except Exception:
                pass

        # Only now is the close certain, so the shortcut filter comes off here.
        # It used to be removed at the top of this method, which meant cancelling
        # the close left the app running with its keyboard shortcuts dead.
        if hasattr(self, '_key_filter'):
            QApplication.instance().removeEventFilter(self._key_filter)

        if hasattr(self, 'logWidget'):
            self.logWidget.restore_stdout()
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    # Recolour the chrome before any widget is built, so nothing is created
    # against one palette and then repainted.
    #
    # A palette and not a stylesheet: QSS rules matching a QCheckBox turn off
    # native drawing of its indicator, and they replace the style's control
    # shapes with flat ones. A palette supplies colours only, so Fusion keeps
    # drawing everything — checkbox indicators included — exactly as before.
    apply_app_palette()
    window = VoltageContinuousInput()
    window.show()
    # Run after the event loop starts so the main window is visible first
    QTimer.singleShot(200, _check_daq_mismatch_and_prompt)
    sys.exit(app.exec_())
