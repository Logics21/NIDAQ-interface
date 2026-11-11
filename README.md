# GioDAQ — Multichannel DAQ

A Windows desktop application for live data acquisition with National Instruments DAQ hardware. It plots many analog channels at once, records runs, drives analog and digital outputs, and hosts test apps (built-in and plugins) on an Apps bar.

Built with PyQt5, pyqtgraph and NI-DAQmx. Ships as a standalone `.exe` built with PyInstaller.

---

## Features

- **Live multichannel plotting** — input, custom (calculated) and output channels, one Y axis per unit, any channel on the X axis.
- **Axis Ranges** — Auto, Widest, Narrowest or Custom per unit, with defaults saved in Settings.
- **Recording** — record to file, with Auto Stop after a set duration.
- **Held runs and sessions** — with *Hold Run to Session Memory* on, each recording becomes a run tab (Run 1, Run 2, … and All Runs). Select a run tab and press Run to re-record over it.
  - **Save Session / Load Session** — a `.mcz` file holding the settings and every held run.
  - **Session Autosave** — rewrites the session file whenever runs are added, re-recorded or deleted.
- **Calibration window** — live readouts, calibration and manual digital output.
- **Unit checking** — Scaled Units and custom-channel units are checked with [pint](https://pint.readthedocs.io/): green if recognised, red if not, and a tooltip saying what the unit measures (Pressure, Volumetric flow, …). Recognised units are rewritten to their short form when you finish editing (`inches` → `in`).
- **Apps bar** — built-in apps, plugin apps and shortcuts to outside programs.

## Apps

| App | Kind | What it does |
|---|---|---|
| Static Lines | built-in | Constant limit lines on the graph, by axis and unit |
| Function Generator | built-in | Waveforms and sweeps on the analog outputs |
| Gauge Adder | built-in | Digital, dial and chart gauges per channel, with optional unit conversion (psi ↔ bar, gpm ↔ lpm, drop/min, …) |
| Custom Triggers | built-in | Trigger and stop conditions on plotted channels or digital inputs; ordered events (digital outputs, generator starts, Start Recording, Wait); stop actions; pre- and post-trigger recording |
| Flow Failure | plugin | Flow-failure testing against a pressure gauge and flow meter |
| Proportional Sweep | plugin | Proportional valve sweep and leakage check |

Apps that would conflict (for example two apps driving the analog outputs) lock each other out automatically.

---

## Requirements

- Windows
- NI-DAQmx driver and a supported NI DAQ device. Without a device the app starts, but hardware features are disabled.
- Python 3.10 (Anaconda recommended). The build script expects a conda environment named `daq_build`.

Python packages:

```
pip install PyQt5 pyqtgraph numpy pandas scipy openpyxl matplotlib nidaqmx pint pyinstaller
```

## Running from source

```
conda activate daq_build
python Multichannel_DAQ.py
```

On first start, if there is no `default_settings.xlsx`, the app runs with defaults. Create one from **Edit → System Settings**.

### Notebook workflow

Each module is edited in a notebook of the same name (`Multichannel_DAQ.ipynb`, `DAQ_GaugeAdder.ipynb`, …). The notebook's `%%writefile` cell writes the `.py`. Run that cell after editing so the `.py` matches the notebook.

---

## Project layout

```
Multichannel_DAQ.py        Main window: acquisition, plotting, recording, runs, sessions
DAQ_Settings.py            Settings dialog, settings workbook, unit checking and conversion
DAQ_AppRegistry.py         Apps bar: built-in apps, plugin discovery, shortcuts, lockouts
DAQ_AppBase.py             Base dialog for analog-output test apps (used by plugins)
DAQ_Theme.py               Colour themes
DAQ_Calibrate.py           Calibration window
DAQ_StaticLines.py         Built-in app
DAQ_FunctionGenerator.py   Built-in app
DAQ_GaugeAdder.py          Built-in app
DAQ_CustomTriggers.py      Built-in app
Plugins/                   Plugin apps (not bundled into the .exe)
    DAQ_FlowFailure/DAQ_FlowFailure.py
    DAQ_ProportionalSweep/DAQ_ProportionalSweep.py
Wrap_Executable.bat        Build script
default_settings.xlsx      Working settings workbook (next to the .py or the .exe)
```

## Settings workbook

All configuration lives in `default_settings.xlsx`, next to the program. **Edit → System Settings** edits it, and saving reloads the main window. Its sheets include:

- Input, output, digital and custom channels
- Transducers
- Axis-range defaults
- Plot lines
- The Apps bar: built-in app visibility and plugin Enable/Show (`app_bar`)
- Shortcuts (`app_shortcuts`)

Settings can't be changed while acquisition or the Function Generator is running, while an app that takes over Run/Stop is open, or while runs are held.

---

## Building the executable

```
Wrap_Executable.bat
```

The script:

1. Activates `daq_build`.
2. Picks the next version and checks `APP_VERSION` in `Multichannel_DAQ.py` against it.
3. Checks that every built-in app in `APP_MODULES` (`DAQ_AppRegistry.py`) has a `--hidden-import` line, and refuses to build if one is missing.
4. Shows a confirmation screen listing every file it will create, change or delete.
5. Builds `dist\Multichannel_DAQ_vX.Y.Z\` with PyInstaller (one folder, windowed).
6. Copies `Plugins\` next to the new `.exe`, leaving out notebooks and caches.
7. Backs up the `.py` files used to `dist\SourceFiles_Multichannel_DAQ_vX.Y.Z\`.
8. Consolidates notebooks. For each module it keeps the newest of `<name>.ipynb` / `<name>-CopyN.ipynb` and **deletes the rest**, bypassing the Recycle Bin.

If PyInstaller fails, the script stops before the backup and before touching notebooks.

**Before the first build**, edit the `--add-binary` line in `Wrap_Executable.bat` so it points at `python310.dll` in your own `daq_build` environment.

---

## Plugins

Plugins are apps that live outside the `.exe`. They can be added or updated without rebuilding.

### Layout

One folder per app, inside `Plugins/` next to the `.exe` (or next to `Multichannel_DAQ.py` when running from source). The entry file is named after its folder:

```
Plugins/
    MyApp/
        MyApp.py        must define APP_SPEC
        helpers.py      optional — import with `from . import helpers`
```

Loose files directly in `Plugins/` are ignored, and so is any `__init__.py`.

### Enabling

**Settings → App Settings → App Bar → Plugins** lists every folder found, with a status for each. A plugin is **only loaded once Enable is ticked**: listing a folder runs none of its code. Enabling or disabling takes effect after a restart.

> **Security:** a loaded plugin runs inside GioDAQ with the user's Windows permissions. Keep the `Plugins` folder writable only by the people who maintain plugins, and enable only plugins you trust.

### `APP_SPEC`

```python
APP_SPEC = {
    'id':    'my_app',          # unique and stable — saved in the settings workbook
    'label': 'My App',          # button text
    'cls':   'MyAppDialog',     # dialog class in this module
    'build': build,             # callable(ctx, parent) -> QDialog
    'color': '#1f618d',         # optional button colour
    'hover': '#2980b9',         # optional hover colour
    'show_by_default':       True,
    'requires_daq_hw':       True,   # greyed out with no DAQ device
    'requires_daq_stopped':  False,  # greyed out while acquiring
    'blocks_calibration':    False,  # disables Calibrate while open
    'locks_run_stop':        False,  # takes over the main Run/Stop
    'owns_recording_length': False,  # main Auto Stop is unticked and greyed while open
    'claims': {'ao'},                # exclusive resources; apps sharing a claim lock each other out
}
```

A copy of an app needs a new `id`. A clash is reported, and the second app isn't loaded.

### What a plugin can import

A plugin can only import what is bundled into the `.exe`:

- the `DAQ_*` support modules, such as `DAQ_AppBase` and `DAQ_Settings`
- the libraries in the "plugin SDK" block of `Wrap_Executable.bat`

A plugin that needs anything else, or a change to a bundled module, requires adding it there and rebuilding.

---

## Shortcuts

**Settings → App Settings → App Bar → Shortcuts** adds a named button that starts any outside program. Shortcuts take part in no locking. Changes show as soon as you save.

## Version

The version is `APP_VERSION` in `Multichannel_DAQ.py`, shown in the window title. Each build goes to its own `dist\Multichannel_DAQ_vX.Y.Z\` folder.
