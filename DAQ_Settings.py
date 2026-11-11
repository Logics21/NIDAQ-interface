
"""DAQ settings editor.

Channel, transducer and application settings for the DAQ application, stored
in an Excel workbook (default_settings.xlsx) alongside the executable.

Requirements: pip install PyQt5 pandas openpyxl nidaqmx
Without nidaqmx a dummy "Dev1" with example channels is used, so the UI can
be run and edited without hardware attached.
"""

import functools
import re
import sys
import pandas as pd

try:
   import nidaqmx
   from nidaqmx.system import System, Device
except Exception:
   nidaqmx = None
   System = Device = None


def log_daq_versions():
   """Print the DAQmx driver and nidaqmx package versions, once, at startup.

   Which device attributes exist depends on the driver, and which enum values
   the package can map depends on the package, so a card detected wrongly in
   the field is usually a question about this pairing. Having both in the log
   means it can be answered from the log alone.
   """
   if nidaqmx is None:
      print("NI-DAQmx not available — running without hardware.")
      return
   pkg = getattr(nidaqmx, "__version__", "unknown")
   try:
      drv = System.local().driver_version
      drv = f"{drv.major_version}.{drv.minor_version}.{drv.update_version}"
   except Exception as e:
      drv = f"unreadable ({type(e).__name__}: {e})"
   print(f"NI-DAQmx driver {drv}, nidaqmx package {pkg}")

from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QLocale
from PyQt5.QtGui import QDoubleValidator
from PyQt5.QtWidgets import (
   QApplication, QWidget, QMainWindow, QVBoxLayout, QHBoxLayout, QGridLayout,
   QLabel, QComboBox, QLineEdit, QCheckBox, QPushButton, QTabWidget,
   QGroupBox, QMessageBox, QSpinBox, QScrollArea, QDialog, QDialogButtonBox,
   QFileDialog,
)

import os

# ---------------------------------------------------------------------------
# Path resolution — works in .py, Jupyter, and PyInstaller frozen .exe
# ---------------------------------------------------------------------------

def _get_settings_path():
   if getattr(sys, 'frozen', False):
       base = os.path.dirname(sys.executable)
   elif '__file__' in dir():
       base = os.path.dirname(os.path.abspath(__file__))
   else:
       base = os.getcwd()
   return os.path.join(base, 'default_settings.xlsx')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _scroll_wrap(inner_widget: QWidget) -> QScrollArea:
   """Return a QScrollArea that contains inner_widget."""
   scroll = QScrollArea()
   scroll.setWidgetResizable(True)
   scroll.setWidget(inner_widget)
   return scroll


def _clear_widget(widget: QWidget) -> None:
   """Remove and delete all children + detach layout so a fresh one can be set."""
   old_layout = widget.layout()
   if old_layout is not None:
       # drain items
       while old_layout.count():
           item = old_layout.takeAt(0)
           child = item.widget()
           if child:
               child.deleteLater()
       # reparent the layout to a temporary widget so Qt lets us set a new one
       QWidget().setLayout(old_layout)


# ---------------------------------------------------------------------------
# Main widget
# ---------------------------------------------------------------------------

# Allowed names inside a custom-channel expression.
# A = first channel (dropdown 1), B = second channel (dropdown 2).
_EXPR_ALLOWED = {'A', 'B', 'log', 'log10', 'sqrt', 'abs', 'exp', 'sin', 'cos', 'tan'}

import math as _math

# Theme definitions live in their own module so both the settings dialog and the
# main window read the same table.
import DAQ_Theme as _daq_theme
_EXPR_SAFE_NS = {
    '__builtins__': {},
    'log':   _math.log,
    'log10': _math.log10,
    'sqrt':  _math.sqrt,
    'abs':   abs,
    'exp':   _math.exp,
    'sin':   _math.sin,
    'cos':   _math.cos,
    'tan':   _math.tan,
    'A': 1.0, 'B': 1.0,
}

# ---------------------------------------------------------------------------
# Signal units — what an NI-DAQmx channel natively measures / generates
# ---------------------------------------------------------------------------
#
# Each entry maps a unit string to (dimension, factor-to-base-unit). Two units
# are interchangeable only when they share a dimension; the factor then
# converts a value into that dimension's base unit.
#
# degC / degF / K each get their own dimension on purpose: they are offset
# scales, not simple multiples, so they must never be silently converted.

_UNIT_TABLE = {
    # electrical signals a transducer can actually put on a wire
    "V":      ("voltage",      1.0),
    "mV":     ("voltage",      1e-3),
    "A":      ("current",      1.0),
    "mA":     ("current",      1e-3),
    "uA":     ("current",      1e-6),
    "Ohms":   ("resistance",   1.0),
    "kOhms":  ("resistance",   1e3),
    "mV/V":   ("ratio",        1.0),
    "C":      ("charge",       1.0),
    # engineering units a DAQmx channel can be configured to return directly
    "degC":   ("temp_C",       1.0),
    "degF":   ("temp_F",       1.0),
    "K":      ("temp_K",       1.0),
    "strain": ("strain",       1.0),
    "Hz":     ("frequency",    1.0),
    "kHz":    ("frequency",    1e3),
    "g":      ("acceleration", 1.0),
    "m/s":    ("velocity",     1.0),
    "m":      ("length",       1.0),
    "deg":    ("angle",        1.0),
    "N":      ("force",        1.0),
    "Pa":     ("pressure",     1.0),
    "Nm":     ("torque",       1.0),
    "W":      ("power",        1.0),
}

# Offered in the Transducers tab. A transducer wired to a DAQ card emits an
# electrical signal — volts, a 4-20 mA loop, a resistance (RTD element) or a
# bridge ratio. It never emits degrees or psi: those are what Mx + b turn the
# signal into, and they belong in the Engineering Units column.
_TRANSDUCER_UNITS = ["V", "mV", "mA", "A", "uA", "Ohms", "kOhms", "mV/V"]

# NI-DAQmx measurement / output type ->
#   (unit the channel's min/max is expressed in,
#    Device attribute reporting the supported ranges (None = not queryable),
#    dimension of the electrical signal the channel accepts)
#
# The last two differ for sensor types, which is the whole reason they are
# separate fields: a thermocouple channel is configured in degC because the
# driver linearises for you, but the wire carries millivolts, so a mV
# transducer is the correct thing to attach to it. An RTD channel is likewise
# degC on the range and Ohms on the wire.
#
# Enum member names are taken from nidaqmx.constants.UsageTypeAI / UsageTypeAO.
_AI_TYPE_INFO = {
    "VOLTAGE":                        ("V",      "ai_voltage_rngs",    "voltage"),
    "VOLTAGE_ACRMS":                  ("V",      "ai_voltage_rngs",    "voltage"),
    "VOLTAGE_CUSTOM_WITH_EXCITATION": ("V",      "ai_voltage_rngs",    "voltage"),
    "TEDS":                           ("V",      "ai_voltage_rngs",    "voltage"),
    "CURRENT":                        ("A",      "ai_current_rngs",    "current"),
    "CURRENT_ACRMS":                  ("A",      "ai_current_rngs",    "current"),
    "RESISTANCE":                     ("Ohms",   "ai_resistance_rngs", "resistance"),
    "BRIDGE":                         ("mV/V",   "ai_bridge_rngs",     "ratio"),
    "FREQUENCY_VOLTAGE":              ("Hz",     "ai_freq_rngs",       "voltage"),
    "CHARGE":                         ("C",      "ai_charge_rngs",     "charge"),
    "TEMPERATURE_THERMOCOUPLE":       ("degC",   None,                 "voltage"),
    "TEMPERATURE_RTD":                ("degC",   None,                 "resistance"),
    "TEMPERATURE_THERMISTOR":         ("degC",   None,                 "resistance"),
    "TEMPERATURE_BUILT_IN_SENSOR":    ("degC",   None,                 None),
    "STRAIN_STRAIN_GAGE":             ("strain", None,                 "ratio"),
    "ROSETTE_STRAIN_GAGE":            ("strain", None,                 "ratio"),
    "FORCE_BRIDGE":                   ("N",      "ai_bridge_rngs",     "ratio"),
    "PRESSURE_BRIDGE":                ("Pa",     "ai_bridge_rngs",     "ratio"),
    "TORQUE_BRIDGE":                  ("Nm",     "ai_bridge_rngs",     "ratio"),
    "FORCE_IEPE_SENSOR":              ("N",      None,                 "current"),
    "SOUND_PRESSURE_MICROPHONE":      ("Pa",     None,                 "current"),
    "VELOCITY_IEPE_SENSOR":           ("m/s",    None,                 "current"),
    "ACCELERATION_ACCELEROMETER_CURRENT_INPUT": ("g", None,            "current"),
    "ACCELERATION_CHARGE":            ("g",      "ai_charge_rngs",     "charge"),
    "ACCELERATION_4_WIRE_DC_VOLTAGE": ("g",      None,                 "voltage"),
    "POSITION_LINEAR_LVDT":           ("m",      None,                 "voltage"),
    "POSITION_ANGULAR_RVDT":          ("deg",    None,                 "voltage"),
    "POSITION_EDDY_CURRENT_PROX_PROBE": ("m",    None,                 "voltage"),
    "POWER":                          ("W",      None,                 None),
    "CALCULATED_POWER":               ("W",      None,                 None),
}
_AO_TYPE_INFO = {
    "VOLTAGE":             ("V", "ao_voltage_rngs", "voltage"),
    "CURRENT":             ("A", "ao_current_rngs", "current"),
    "FUNCTION_GENERATION": ("V", "ao_voltage_rngs", "voltage"),
}

# Used only when the driver reports nothing at all (no hardware attached).
_DEFAULT_TYPE = "Voltage"


def _pretty_type(name):
    """'AI.MeasurementType.TEMPERATURE_RTD' -> 'Temperature Rtd'."""
    key = str(name).split('.')[-1].split(':')[0].strip()
    return key.replace('_', ' ').title()


def _type_key(label):
    """Inverse of _pretty_type: 'Temperature Rtd' -> 'TEMPERATURE_RTD'."""
    return str(label or '').strip().replace(' ', '_').upper()


def _type_info(label, direction="ai"):
    table = _AI_TYPE_INFO if direction == "ai" else _AO_TYPE_INFO
    return table.get(_type_key(label), ("V", None, "voltage"))


def _unit_for_type(label, direction="ai"):
    """Unit that a measurement/output type's min-max range is given in."""
    return _type_info(label, direction)[0]


def _rng_attr_for_type(label, direction="ai"):
    """nidaqmx Device attribute holding the supported ranges for a type."""
    return _type_info(label, direction)[1]


def _signal_dim_for_type(label, direction="ai"):
    """Dimension of the electrical signal a channel of this type accepts.

    This is what a transducer's output must match — not the range unit, which
    for a thermocouple is degC while the wire carries millivolts.
    """
    return _type_info(label, direction)[2]


# ---------------------------------------------------------------------------
# Channel creation
# ---------------------------------------------------------------------------
#
# The nidaqmx call that opens a channel of each type. Kept next to the tables
# above because it is the same kind of knowledge — a property of the driver,
# not of the user's setup — and because every module that opens hardware needs
# it: the acquisition engine, the function generator and the AO test apps all
# read 'Input Type'/'Output Type' out of the workbook and have to turn it into
# a call. A type absent from these maps is one the app cannot configure yet;
# that is deliberate, so a missing entry fails loudly instead of silently
# opening the channel as a voltage and reading plausible-looking garbage.
_AI_ADD_CHAN = {
    "VOLTAGE": "add_ai_voltage_chan",
    "CURRENT": "add_ai_current_chan",
}
_AO_ADD_CHAN = {
    "VOLTAGE": "add_ao_voltage_chan",
    "CURRENT": "add_ao_current_chan",
}


class UnsupportedChannelType(Exception):
    """Raised when a workbook row names a type no add_*_chan call is mapped to.

    Carries the offending label so the caller can name the channel in its own
    error message rather than only reporting that something went wrong.
    """

    def __init__(self, label, direction, physical_channel=""):
        self.label = label
        self.direction = direction
        self.physical_channel = physical_channel
        supported = sorted(_AI_ADD_CHAN if direction == "ai" else _AO_ADD_CHAN)
        where = f" on {physical_channel}" if physical_channel else ""
        super().__init__(
            f"{'Input' if direction == 'ai' else 'Output'} type "
            f"{label!r}{where} is not supported yet. "
            f"Supported: {', '.join(t.title() for t in supported)}."
        )


def add_ai_channel(task, physical_channel, type_label, min_val, max_val,
                   terminal_config=None):
    """Add one analog INPUT channel to `task`, of the type the workbook names.

    `type_label` is the row's 'Input Type' as written by the settings editor
    (a _pretty_type label such as 'Voltage' or 'Current'). `min_val`/`max_val`
    are in the unit named by that row's 'Range Unit' — volts for a voltage
    channel, amps for a current one — which is the unit nidaqmx expects, so no
    conversion happens here.

    Raises UnsupportedChannelType for a type with no mapped call.
    """
    key = _type_key(type_label) or _type_key(_DEFAULT_TYPE)
    method = _AI_ADD_CHAN.get(key)
    if method is None:
        raise UnsupportedChannelType(type_label, "ai", physical_channel)
    kwargs = {"min_val": min_val, "max_val": max_val}
    if terminal_config is not None:
        kwargs["terminal_config"] = terminal_config
    return getattr(task.ai_channels, method)(physical_channel, **kwargs)


def add_ao_channel(task, physical_channel, type_label, min_val, max_val):
    """Add one analog OUTPUT channel to `task`, of the type the workbook names.

    As add_ai_channel, for 'Output Type'. There is no terminal configuration on
    an output. Raises UnsupportedChannelType for an unmapped type.
    """
    key = _type_key(type_label) or _type_key(_DEFAULT_TYPE)
    method = _AO_ADD_CHAN.get(key)
    if method is None:
        raise UnsupportedChannelType(type_label, "ao", physical_channel)
    return getattr(task.ao_channels, method)(
        physical_channel, min_val=min_val, max_val=max_val)


# Units a duration may be entered in, and what each is worth in seconds. Owned
# here because the expert defaults are written here, and imported by the main
# window so its Duration to Acquire / Duration to Plot dropdowns cannot drift
# out of step with the values saved in the workbook.
DURATION_UNITS   = ["ms", "s", "min", "hr"]
DURATION_FACTORS = {"ms": 0.001, "s": 1.0, "min": 60.0, "hr": 3600.0}


def supported_types(direction="ai"):
    """Type labels this app can actually open a channel for, best first.

    The task builders and the settings editor read the same list, so a type can
    never be offered in a dropdown that add_ai_channel/add_ao_channel would
    then refuse.
    """
    table = _AI_ADD_CHAN if direction == "ai" else _AO_ADD_CHAN
    return [_pretty_type(k) for k in table]


def channel_unit(type_label, direction="ai"):
    """Unit a channel of this type is commanded and measured in ('V', 'A').

    The unit its Min/Max are expressed in, and therefore the unit every UI
    field that sets a level on it should be labelled with.
    """
    return _unit_for_type(type_label, direction)


def timing_group(device_name):
    """Key identifying which hardware timing domain a device belongs to.

    NI-DAQmx lets one hardware-timed task span several modules that share a
    timing engine — all C-Series modules inside ONE CompactDAQ/CompactRIO
    chassis — but not channels from independent devices (two chassis, or a
    chassis plus a USB box). So channels are grouped into one task per domain:
      * a module in a chassis -> the chassis name, so modules of the same
        chassis land in one task and stay hardware-synchronized
      * a standalone device   -> its own name

    Applies to both directions. The AI engine groups acquisition tasks with it;
    the function generator groups AO tasks with it, because a chassis has a
    single AO timing engine shared across its modules.
    """
    name = str(device_name).strip()
    if not name:
        return name
    try:
        import nidaqmx as _ni
        dev = _ni.system.Device(name)
        for attr in ("compact_daq_chassis_device", "compact_rio_chassis_device"):
            try:
                chassis = getattr(dev, attr, None)
                if chassis is not None and getattr(chassis, "name", ""):
                    return chassis.name
            except Exception:
                pass
    except Exception:
        pass
    return name


def row_channel_type(row, direction="ai"):
    """The 'Input Type'/'Output Type' a settings row names, else the default.

    Reads through .get so it works for a pandas Series or a plain dict, and
    falls back to _DEFAULT_TYPE for a blank cell — the same fallback the
    settings editor writes when the driver reports no type at all.
    """
    col = "Input Type" if direction == "ai" else "Output Type"
    try:
        val = row.get(col)
    except AttributeError:
        val = None
    text = "" if val is None else str(val).strip()
    if not text or text.lower() == "nan":
        return _DEFAULT_TYPE
    return text


def read_channel_units(settings_path=None):
    """Every channel's plotted unit, read from a settings workbook.

    [(kind, device_name, unit)] in sheet order, kind being 'ai' (Input
    Channels), 'custom' (Custom Channels) or 'ao' (Output Channels). For use by
    code that reads the workbook directly instead of getting it from the main
    window, so the sheet layout stays known only to this module:
      ai      unit is 'Scaled Units' (the unit after Mx + b)
      custom  unit is 'Units'
      ao      unit follows from 'Output Type' (V for voltage, A for current)
    Rows with no unit are left out, as are output rows with no channel (empty
    sheet lines). A missing workbook or sheet gives no rows; a workbook that
    cannot be read raises.
    """
    if settings_path is None:
        settings_path = _get_settings_path()
    if not os.path.isfile(settings_path):
        return []
    xls = pd.ExcelFile(settings_path)

    def _rows(sheet):
        if sheet not in xls.sheet_names:
            return []
        df = pd.read_excel(xls, sheet_name=sheet)
        return [r for _, r in df.iterrows()]

    out = []
    for kind, sheet, col in (("ai", "input_settings", "Scaled Units"),
                             ("custom", "custom_channel_settings", "Units")):
        for r in _rows(sheet):
            unit = _cell(r, col, strip=True)
            if unit:
                out.append((kind, _cell(r, "Device Name", strip=True), unit))
    for r in _rows("output_settings"):
        if not _cell(r, "Channel", strip=True):
            continue
        unit = channel_unit(row_channel_type(r, "ao"), "ao")
        if unit:
            out.append(("ao", _cell(r, "Device Name", strip=True), unit))
    return out


def _unit_dim(unit):
    """Dimension of a unit string, or None if unknown."""
    entry = _UNIT_TABLE.get((unit or '').strip())
    return entry[0] if entry else None


def _units_compatible(u1, u2):
    """True when two unit strings measure the same physical quantity."""
    d1 = _UNIT_TABLE.get((u1 or '').strip())
    d2 = _UNIT_TABLE.get((u2 or '').strip())
    return bool(d1 and d2 and d1[0] == d2[0])


def _convert_unit(value, from_unit, to_unit):
    """Convert a number between two compatible units.

    Returns a float, or None if the units are not interchangeable or the value
    is not numeric — callers treat None as "leave it alone and let validation
    report it".
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if (from_unit or '').strip() == (to_unit or '').strip():
        return v
    if not _units_compatible(from_unit, to_unit):
        return None
    return v * _UNIT_TABLE[from_unit.strip()][1] / _UNIT_TABLE[to_unit.strip()][1]


# ---------------------------------------------------------------------------
# Scaled-unit recognition (pint)
# ---------------------------------------------------------------------------
#
# Scaled Units and Custom Channel units are free text. pint decides whether a
# text is a real unit (coloured green/red in the editor) and supplies the short
# spelling a unit is rewritten to when the user finishes editing, so "inch",
# "inches" and "in" do not end up as three different units on the graph.
#
# Recognition and spelling only — nothing here converts values, so pint's own
# meaning of a symbol does not matter as long as it is recognised. In
# particular "g" stays pint's gram: making it standard gravity would turn "kg"
# and "mg" into accelerations too.

_UNIT_OK_STYLE  = "color: #2e7d32;"
_UNIT_BAD_STYLE = "color: #c62828; font-weight: bold;"

# Units this app uses that pint does not define, or spells differently.
_PINT_EXTRA_DEFS = (
    "gpm = gallon / minute",
    "lpm = liter / minute",
    "ccm = cubic_centimeter / minute",
    "strain = []",
    "Nm = newton * meter",
    "@alias ohm = Ohms",          # _UNIT_TABLE's spelling; kOhms via prefix
)

# Quantity names shown on hover, each with a reference unit to take its
# dimensions from. Quantities that share dimensions (torque and energy are both
# force x length) are shown together, e.g. "Torque / Energy". Anything not
# listed shows its raw dimensions instead.
_QUANTITY_REFS = (
    ("Pressure",              "Pa"),
    ("Force",                 "N"),
    ("Mass",                  "kg"),
    ("Length",                "m"),
    ("Area",                  "m**2"),
    ("Volume",                "m**3"),
    ("Time",                  "s"),
    ("Velocity",              "m/s"),
    ("Acceleration",          "m/s**2"),
    ("Volumetric flow",       "m**3/s"),
    ("Mass flow",             "kg/s"),
    ("Temperature",           "K"),
    ("Torque",                "N*m"),
    ("Energy",                "J"),
    ("Power",                 "W"),
    ("Frequency",             "Hz"),
    ("Rotational speed",      "rpm"),
    ("Voltage",               "V"),
    ("Current",               "A"),
    ("Resistance",            "ohm"),
    ("Charge",                "C"),
    ("Dimensionless (ratio)", "percent"),
)

# One unit name inside a unit expression ("gallons" in "gallons/minute").
_UNIT_TOKEN_RE = re.compile(r"[A-Za-z_°µμΩ][A-Za-z0-9_°µμΩ]*")

_ureg = None
_ureg_error = None
_quantity_names = {}      # pint dimensionality -> "Pressure", "Torque / Energy", ...


def _unit_registry():
    """The shared pint registry, built on first use. None if unavailable."""
    global _ureg, _ureg_error
    if _ureg is None and _ureg_error is None:
        try:
            import pint
            ureg = pint.UnitRegistry()
            for d in _PINT_EXTRA_DEFS:
                ureg.define(d)
            names = {}
            for name, ref in _QUANTITY_REFS:
                names.setdefault(ureg.parse_units(ref).dimensionality, []).append(name)
            _quantity_names.update({dim: " / ".join(n) for dim, n in names.items()})
            _ureg = ureg
        except Exception as e:
            _ureg_error = f"{type(e).__name__}: {e}"
            print(f"Unit check unavailable ({_ureg_error}).")
    return _ureg


@functools.lru_cache(maxsize=512)
def _unit_check(text):
    """(state, short_spelling, quantity) for a unit text.

    state is 'empty', 'ok', 'bad' or 'unavailable'. quantity is what the unit
    measures ("Pressure"), or its raw dimensions when that is not one of
    _QUANTITY_REFS; '' unless state is 'ok'. short_spelling is the text
    with each unit name replaced by its symbol where that symbol is plain
    ASCII, shorter, and means the same unit ("inches/minute" -> "in/min").
    Operators, order and exponents are left as typed, and symbols such as
    °C, Ω or μ are not substituted, so "degC", "Ohms" and "uA" — the spellings
    the rest of the app compares against — are never rewritten.
    """
    text = (text or "").strip()
    if not text:
        return 'empty', text, ''
    ureg = _unit_registry()
    if ureg is None:
        return 'unavailable', text, ''
    try:
        dim = ureg.parse_units(text).dimensionality
    except Exception:
        return 'bad', text, ''
    quantity = _quantity_names.get(dim) or str(dim)

    def _short(m):
        tok = m.group(0)
        try:
            unit = ureg.parse_units(tok)
            sym = f"{unit:~}"
            if (sym.isascii() and " " not in sym and len(sym) < len(tok)
                    and ureg.parse_units(sym) == unit):
                return sym
        except Exception:
            pass
        return tok

    return 'ok', _UNIT_TOKEN_RE.sub(_short, text), quantity


# Units a channel can be converted to for display (Gauge Adder), grouped by
# quantity. A unit is offered the group that measures the same quantity as it
# (same pint dimensions), minus any unit that would leave the value unchanged.
UNIT_CONVERSION_GROUPS = (
    ("psi", "bar", "mbar", "kPa", "MPa", "Pa", "inHg"),
    ("gpm", "lpm", "ccm", "cm^3/min", "in^3/min"),
    ("degC", "degF", "K"),
    ("N", "lbf"),
    ("in", "mm", "m"),
    ("Nm", "ft*lbf", "in*lbf"),
)


def unit_affine(src, dst):
    """(scale, offset) with value_in_dst = value_in_src * scale + offset.

    Every unit conversion is of this form — offset only for temperatures —
    so it applies to single values and numpy arrays alike. Raises if pint is
    unavailable or the units are not convertible.
    """
    ureg = _unit_registry()
    if ureg is None:
        raise RuntimeError(f"unit conversion unavailable ({_ureg_error})")
    offset = ureg.Quantity(0.0, src).to(dst).magnitude
    scale = ureg.Quantity(1.0, src).to(dst).magnitude - offset
    return float(scale), float(offset)


def unit_quantity(unit):
    """What a unit measures, by name from _QUANTITY_REFS ("Pressure",
    "Volumetric flow", ...), or None for an unrecognised unit or one whose
    quantity is not named there. For picking channels by what they measure
    rather than by a list of spellings (kPa and psi are both "Pressure")."""
    state, _, quantity = _unit_check(unit or "")
    if state != 'ok' or quantity not in _quantity_names.values():
        return None
    return quantity


@functools.lru_cache(maxsize=256)
def unit_conversions(unit):
    """The units `unit` can be converted to, from UNIT_CONVERSION_GROUPS.

    [] for an unrecognised unit or one whose quantity has no group.
    """
    state, _, _ = _unit_check(unit)
    if state != 'ok':
        return []
    ureg = _unit_registry()
    dim = ureg.parse_units(unit.strip()).dimensionality
    for group in UNIT_CONVERSION_GROUPS:
        if ureg.parse_units(group[0]).dimensionality != dim:
            continue
        out = []
        for target in group:
            try:
                scale, offset = unit_affine(unit.strip(), target)
            except Exception:
                continue
            if not (_math.isclose(scale, 1.0, rel_tol=1e-12) and abs(offset) < 1e-12):
                out.append(target)
        return out
    return []


def _apply_unit_color(widget, text):
    """Colour `widget`'s text by whether `text` is a recognised unit."""
    state, _, quantity = _unit_check(text)
    if state == 'ok':
        widget.setStyleSheet(_UNIT_OK_STYLE)
        widget.setToolTip(f"Unit recognized — {quantity}")
    elif state == 'bad':
        widget.setStyleSheet(_UNIT_BAD_STYLE)
        widget.setToolTip("Unit not recognized")
    elif state == 'unavailable':
        widget.setStyleSheet("")
        widget.setToolTip(f"Unit check unavailable ({_ureg_error})")
    else:
        widget.setStyleSheet("")
        widget.setToolTip("")


def _respell_unit(line_edit):
    """On finishing an edit, rewrite a recognised unit to its short spelling."""
    state, short, _ = _unit_check(line_edit.text())
    if state == 'ok' and short != line_edit.text().strip():
        line_edit.setText(short)


def _wire_unit_field(line_edit, color_target=True):
    """Colour a unit field as it is typed in and respell it when done."""
    if color_target:
        line_edit.textChanged.connect(
            lambda t, w=line_edit: _apply_unit_color(w, t))
    line_edit.editingFinished.connect(
        lambda w=line_edit: _respell_unit(w))


def _fmt_num(v):
    """Format a converted value without float noise (0.019999999 -> 0.02)."""
    return "" if v is None else f"{float(v):.10g}"


def _set_widget_value(widget, value):
    """Put a string into whichever kind of settings widget this is.

    Used by both the defaults toggle and the Excel loader, which previously each
    carried their own copy of this dispatch.
    """
    widget.setEnabled(True)          # may be disabled; caller re-locks after
    if isinstance(widget, QComboBox):
        widget.setCurrentText(value)
    elif isinstance(widget, QSpinBox):
        try:
            widget.setValue(int(float(value)))
        except (TypeError, ValueError):
            pass
    else:
        widget.setText(value)


def _cell(row, col, strip=False):
    """One cell of a pandas row as a string, with NaN and missing as ''.

    Every sheet reader needs this, so it lives here instead of being redefined
    as a nested helper in each one.
    """
    v = row.get(col, "")
    if pd.isna(v):
        return ""
    return str(v).strip() if strip else str(v)


def _validate_custom_expression(expr: str):
    """Return an error string, or None if the expression is valid.

    Checks only A/B are used as variables, then does a dry-run eval with
    A=B=1.0 to catch syntax/math errors.
    """
    # ^ is a common user habit; normalise to ** before any check
    normalised = expr.replace('^', '**')
    # Scan identifiers — reject anything that isn't a known name
    identifiers = set(re.findall(r'[A-Za-z_][A-Za-z0-9_]*', normalised))
    unknown = identifiers - _EXPR_ALLOWED
    if unknown:
        return (f"Unknown variable(s): {', '.join(sorted(unknown))}. "
                f"Only A, B, and math functions "
                f"(log, log10, sqrt, abs, exp, sin, cos, tan) are allowed.")
    try:
        eval(normalised, dict(_EXPR_SAFE_NS))   # copy so ns isn't mutated
    except ZeroDivisionError:
        pass   # only triggers with specific values; not a syntax error
    except Exception as exc:
        return f"Expression error: {exc}"
    return None


def _fg_math_namespace():
    """Safe evaluation namespace for Function-Generator math expressions.
    Uses numpy so `t` can be an array."""
    import numpy as np
    return {
        'pi': np.pi, 'e': np.e, 'tau': 2 * np.pi,
        'sin': np.sin, 'cos': np.cos, 'tan': np.tan,
        'asin': np.arcsin, 'acos': np.arccos, 'atan': np.arctan, 'atan2': np.arctan2,
        'sinh': np.sinh, 'cosh': np.cosh, 'tanh': np.tanh,
        'exp': np.exp, 'log': np.log, 'log10': np.log10, 'log2': np.log2,
        'sqrt': np.sqrt, 'abs': np.abs, 'fabs': np.abs,
        'floor': np.floor, 'ceil': np.ceil, 'pow': np.power, 'power': np.power,
        'sign': np.sign, 'clip': np.clip, 'mod': np.mod,
    }


_FG_MATH_ALLOWED = set(_fg_math_namespace().keys()) | {'t', 'f'}


def _validate_fg_math_expr(expr):
    """Validate a Function-Generator math expression in variables t (time) and
    f (frequency).

    Returns (error_or_None, out_min, out_max). The output range is computed at
    f = 1 over t in [0, 1] — indicative only (the FG sets the real f / scaling),
    but useful for spotting a function that will overrun an AO channel's range."""
    expr = (expr or '').strip()
    if not expr:
        return (None, None, None)
    normalised = expr.replace('^', '**')
    idents = set(re.findall(r'[A-Za-z_][A-Za-z0-9_]*', normalised))
    unknown = idents - _FG_MATH_ALLOWED
    if unknown:
        return (f"Unknown name(s): {', '.join(sorted(unknown))}. "
                f"Allowed: t, f, pi, e and math functions "
                f"(sin, cos, tan, exp, log, sqrt, abs, …).", None, None)
    try:
        import numpy as np
        t = np.linspace(0.0, 1.0, 256)
        ns = _fg_math_namespace()
        ns.update({'t': t, 'f': 1.0})
        y = eval(normalised, {'__builtins__': {}}, ns)
        y = np.asarray(y, dtype=float)
        if y.shape == ():                      # constant expression
            y = np.full_like(t, float(y))
        if y.shape != t.shape:
            return ("Expression must evaluate to a single value per time point.",
                    None, None)
        if not np.all(np.isfinite(y)):
            return ("Produces non-finite values (inf/NaN) over t in [0, 1].",
                    None, None)
        return (None, float(np.min(y)), float(np.max(y)))
    except ZeroDivisionError:
        return (None, None, None)   # only at specific t; not a syntax error
    except Exception as exc:
        return (f"Expression error: {exc}", None, None)


def _parse_points(text):
    """Parse 'x:y, x:y, …' (pairs separated by comma or semicolon) into a list
    of (x, y) float tuples. Returns (points, error_or_None)."""
    text = (text or '').strip()
    if not text:
        return ([], None)
    pts = []
    for p in [s for s in re.split(r'[;,]', text) if s.strip()]:
        if ':' not in p:
            return ([], f"Each point must be 'x:y' (got '{p.strip()}').")
        a, b = p.split(':', 1)
        try:
            pts.append((float(a), float(b)))
        except ValueError:
            return ([], f"Non-numeric point '{p.strip()}'.")
    if len(pts) < 2:
        return (pts, "Need at least 2 points.")
    xs = [x for x, _ in pts]
    if any(x2 < x1 for x1, x2 in zip(xs, xs[1:])):
        return (pts, "X values should be non-decreasing.")
    return (pts, None)


# ---------------------------------------------------------------------------
# Expression -> LaTeX (for the live preview under the Custom Channels tab)
# ---------------------------------------------------------------------------

# math.log is the *natural* log, so we render "log" as \ln to match what the
# expression actually computes.
_FUNC_LATEX = {
    'sqrt':  (r'\sqrt{', '}'),
    'abs':   (r'\left|', r'\right|'),
    'sin':   (r'\sin\left(', r'\right)'),
    'cos':   (r'\cos\left(', r'\right)'),
    'tan':   (r'\tan\left(', r'\right)'),
    'log':   (r'\ln\left(', r'\right)'),
    'log10': (r'\log_{10}\left(', r'\right)'),
}


def _fmt_num(value):
    """Render a number without a pointless trailing '.0'."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _ast_to_latex(node):
    """Recursively turn an ast node into (latex_string, precedence).

    Precedence: +/- = 1, */ = 2, unary = 3, ** = 4, atom = 5.
    Parentheses are inserted only where a child binds more loosely than its
    parent, so the output stays clean.
    """
    import ast

    if isinstance(node, ast.BinOp):
        l, lp = _ast_to_latex(node.left)
        r, rp = _ast_to_latex(node.right)
        op = node.op
        if isinstance(op, ast.Add):
            return f"{l} + {r}", 1
        if isinstance(op, ast.Sub):
            if rp <= 1:
                r = rf"\left({r}\right)"
            return f"{l} - {r}", 1
        if isinstance(op, ast.Mult):
            if lp < 2:
                l = rf"\left({l}\right)"
            if rp < 2:
                r = rf"\left({r}\right)"
            both_numeric = (
                isinstance(node.left,  (ast.Num, ast.Constant)) and
                isinstance(node.right, (ast.Num, ast.Constant))
            )
            joiner = r" \cdot " if both_numeric else r"\,"
            return f"{l}{joiner}{r}", 2
        if isinstance(op, ast.Div):
            return rf"\frac{{{l}}}{{{r}}}", 5   # frac groups visually -> atom
        if isinstance(op, ast.Pow):
            if lp <= 3:
                l = rf"\left({l}\right)"
            return f"{l}^{{{r}}}", 4
        raise ValueError("unsupported operator")

    if isinstance(node, ast.UnaryOp):
        operand, p = _ast_to_latex(node.operand)
        if isinstance(node.op, ast.USub):
            if p < 2:
                operand = rf"\left({operand}\right)"
            return f"-{operand}", 3
        if isinstance(node.op, ast.UAdd):
            return f"+{operand}", 3
        raise ValueError("unsupported unary operator")

    if isinstance(node, ast.Call):
        fname = getattr(node.func, 'id', '')
        args = [_ast_to_latex(a)[0] for a in node.args]
        inner = args[0] if args else ''
        if fname == 'exp':
            return f"e^{{{inner}}}", 4
        wrap = _FUNC_LATEX.get(fname)
        if wrap:
            return f"{wrap[0]}{inner}{wrap[1]}", 5
        return rf"\mathrm{{{fname}}}\left({', '.join(args)}\right)", 5

    if isinstance(node, ast.Name):
        return node.id, 5
    if isinstance(node, ast.Num):            # < py3.8
        return _fmt_num(node.n), 5
    if isinstance(node, ast.Constant):       # >= py3.8
        return _fmt_num(node.value), 5

    raise ValueError("unsupported expression element")


def _expr_to_latex(expr: str):
    """Convert a custom-channel expression to a LaTeX string.

    Returns None if the expression can't be parsed (so the caller can show a
    'check syntax' hint instead of crashing).
    """
    import ast
    expr = (expr or '').strip()
    if not expr:
        return ''
    try:
        tree = ast.parse(expr.replace('^', '**'), mode='eval')
        latex, _ = _ast_to_latex(tree.body)
        return latex
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Combobox that shows each channel's Device Name as a hover tooltip
# ---------------------------------------------------------------------------

class ChannelComboBox(QComboBox):
    """A QComboBox whose drop-down items show the channel's Device Name on hover.

    name_lookup(channel_id) should return the friendly Device Name (or "").
    Tooltips are refreshed every time the popup opens, so renames done on the
    Input Channels tab show up immediately.
    """

    def __init__(self, name_lookup=None, parent=None):
        super().__init__(parent)
        self._name_lookup = name_lookup

    def showPopup(self):
        if self._name_lookup is not None:
            for i in range(self.count()):
                chan = self.itemText(i)
                tip = chan
                if chan and chan != "None":
                    try:
                        friendly = self._name_lookup(chan)
                    except Exception:
                        friendly = ""
                    if friendly:
                        tip = f"{chan} \u2014 {friendly}"
                self.setItemData(i, tip, Qt.ToolTipRole)
        super().showPopup()


class DAQSettingsWidget(QGroupBox):

   settings_saved = pyqtSignal()   # emitted after a successful save

   # ------------------------------------------------------------------
   # Construction
   # ------------------------------------------------------------------

   def __init__(self, parent=None, title="Channel Settings"):
       super().__init__(title, parent)
       self.setAlignment(Qt.AlignHCenter)

       self.entry_widgets: dict = {}
       self.check_vars: dict = {}

       self.Terminal_options_list = ["RSE", "NRSE", "DIFF"]
       self.Default_terminal = "RSE"
       self.no_of_cus_chans = 2
       self.no_of_transducers = 20
       self.chan_names: list = []

       # ── DIFF (differential) channel pairing state ─────────────────
       # A differential input uses two physical AI pins, so the pin that
       # serves as the negative input must be locked out of the UI.
       self._ai_row_info    = {}    # row -> (device, channel index)
       self._ai_dev_rows    = {}    # device -> {channel index: row}
       self._diff_locked_rows = set()   # rows currently reserved as a (-) input
       self._diff_lock_busy = False      # re-entrancy guard

       self.expert_defaults = {
           "Sample Rate (Hz)":    "1000",
           "Duration to Acquire":      "180",
           "Duration to Acquire Unit": "s",
           # Blank means "plot the whole recording"; the main window treats an
           # empty value that way, so the default is deliberately empty rather
           # than a number.
           "Duration to Plot":         "",
           "Duration to Plot Unit":    "s",
           "AO Sample Rate (Hz)": "1024",
           "Max Buffer Size":     "1024",
           "Refresh Rate (Hz)":   "10",
           "Max Plot Points":     "200000",
           "Filter Mode":         "FIR",
           "Filter Type":         "lowpass",
           "Filter Order":        "2",
           "Filter Cutoff (Hz)":  "2",
           "Filter fh (Hz)":      "0",
           "Filter Channels":     "All",
           "Theme":               "Light",
       }

       root = QVBoxLayout(self)

       # ── Tabs ──────────────────────────────────────────────────────
       self.tabs = QTabWidget()
       self.tab_ai     = QWidget()   # Input Channels
       self.tab_ao      = QWidget()   # Output Channels
       self.tab_custom  = QWidget()   # Custom Channels
       self.tab_digital = QWidget()   # Digital Channels
       self.tab_trans   = QWidget()   # Transducers
       self.tab_expert  = QWidget()   # Expert Settings

       self.tab_keymap  = QWidget()   # Key Mapping
       self.tab_app     = QWidget()   # App Settings

       self.tabs.addTab(self.tab_ai,      "Input Channels")
       self.tabs.addTab(self.tab_ao,      "Output Channels")
       self.tabs.addTab(self.tab_custom,  "Custom Channels")
       self.tabs.addTab(self.tab_digital, "Digital Channels")
       self.tabs.addTab(self.tab_trans,   "Transducers")
       self.tabs.addTab(self.tab_expert,  "Expert Settings")
       self.tabs.addTab(self.tab_keymap,  "Key Mapping")
       self.tabs.addTab(self.tab_app,     "App Settings")
       root.addWidget(self.tabs)

       # App Settings holds static content (not tied to hardware channels), so
       # build it once here — update_channels() rebuilds only the channel tabs.
       self._build_app_settings_tab()

       # Refresh transducer dropdowns whenever Input Channels tab is shown
       self.tabs.currentChanged.connect(self._on_tab_changed)

       # ── Buttons ───────────────────────────────────────────────────
       btn_row = QHBoxLayout()
       self.load_btn  = QPushButton("Load Settings…")
       self.save_btn  = QPushButton("Save Settings")
       self.save_as_btn = QPushButton("Save As…")
       self.close_btn = QPushButton("Close")
       btn_row.addStretch()
       btn_row.addWidget(self.load_btn)
       btn_row.addWidget(self.save_btn)
       btn_row.addWidget(self.save_as_btn)
       btn_row.addWidget(self.close_btn)
       root.addLayout(btn_row)

       # ── Signals ───────────────────────────────────────────────────
       self.load_btn.clicked.connect(self.load_settings_file)
       self.save_btn.clicked.connect(self.save_settings)
       self.save_as_btn.clicked.connect(self.save_settings_as)
       self.close_btn.clicked.connect(self.close_window)

       # ── Initial build ─────────────────────────────────────────────
       self.update_channels()

   # ------------------------------------------------------------------
   # DAQ helpers
   # ------------------------------------------------------------------

   def _row_device_name(self, prefix, row):
       """Return the DAQ/module name stored for a given row (read-only label
       written when the tab was built), or '' if not present."""
       w = self.entry_widgets.get(f"{prefix}{row}")
       if w is None:
           return ""
       try:
           return w.text().strip()
       except Exception:
           return ""

   def _get_daq_capabilities(self, dev_name=None):
       """Query a DAQ for supported ranges, measurement types and terminal configs.

       Returns None if the device cannot be opened, else a dict:
           'ai_ranges' / 'ao_ranges'          voltage ranges only, kept for
                                              callers that deal purely in volts
           'ai_ranges_by_unit'                {'V': [(min, max), …], 'A': […]}
           'ao_ranges_by_unit'                same, for output
           'ai_types' / 'ao_types'            supported measurement/output types
           'term_cfgs'                        supported terminal configurations

       Every attribute is read in its own try/except. A single-function module
       raises on the attributes for the direction it does not have — an AI-only
       NI-9203 has no ao_voltage_rngs, an AO-only NI-9265 has no ai_* at all —
       and a current module has no *_voltage_rngs. None of those failures may
       discard the data for the attributes that did work.
       """
       try:
           import nidaqmx
           if not dev_name:
               lst = self._get_daq_list()
               dev_name = lst[0] if lst else ""
           daq = nidaqmx.system.Device(dev_name)
       except Exception as e:
           print(f"Could not open DAQ '{dev_name}': {e}")
           return None

       def has_chans(attr):
           """True when the card actually has channels in this direction.

           Guards the inference below: probing an AO-only module for input
           ranges would waste reads, and more importantly an empty input type
           list is only worth complaining about on a card that has inputs.
           """
           try:
               return bool(list(getattr(daq, attr)))
           except Exception:
               return False

       has_ai = has_chans("ai_physical_chans")
       has_ao = has_chans("ao_physical_chans")

       def pairs(flat):
           flat = list(flat or [])
           return [(flat[i], flat[i + 1]) for i in range(0, len(flat) - 1, 2)]

       def ranges(attr):
           """Ranges from one Device attribute; [] if unsupported."""
           if not attr:
               return []
           try:
               return pairs(getattr(daq, attr))
           except Exception:
               return []

       def types(attr):
           """Type labels from one Device attribute, element by element.

           Reading the list as a whole meant one value nidaqmx could not map to
           an enum discarded every type on the card, which then silently became
           a voltage card. Each element is converted on its own so a stranger
           costs only itself, and what failed is logged rather than swallowed.

           Returns (labels, note) — note is "" on a clean read, else why it is
           short or empty, for the caller to log against the device name.
           """
           try:
               raw = getattr(daq, attr)
           except Exception as e:
               first = str(e).strip().splitlines()[0] if str(e).strip() else ""
               return [], f"{attr} unreadable ({type(e).__name__}: {first})"
           labels, bad = [], []
           try:
               iterator = list(raw)
           except Exception as e:
               return [], f"{attr} not a list ({type(e).__name__}: {e})"
           for t in iterator:
               try:
                   labels.append(_pretty_type(t))
               except Exception as e:
                   bad.append(f"{t!r} ({type(e).__name__})")
           note = (f"{attr}: skipped unmappable {', '.join(bad)}" if bad else "")
           if not labels and not bad:
               note = f"{attr} returned nothing"
           return labels, note

       def infer_types(direction):
           """Types a card supports, deduced from which range attrs answer.

           Used when the driver will not report the type list at all — an old
           DAQmx predates the ai_meas_types/ao_output_types attributes, so the
           read fails on hardware that is otherwise perfectly usable. The range
           attributes are much older and still answer, and _types_the_card_ranges
           already treats "reports a range" as the honest test for a type, so the
           same test is simply applied in reverse: probe each type this app can
           open and keep the ones the card gives a range for.

           A 9203 answers ai_current_rngs and not ai_voltage_rngs, so it comes
           back as Current. Nothing is guessed from the model name.
           """
           table = _AI_ADD_CHAN if direction == "ai" else _AO_ADD_CHAN
           found = []
           for key in table:
               label = _pretty_type(key)
               if ranges(_rng_attr_for_type(label, direction)):
                   found.append(label)
           return found

       ai_types, ai_note = types("ai_meas_types")
       ao_types, ao_note = types("ao_output_types")

       # How each direction's type list was arrived at, so a wrong answer in
       # the field can be traced from the log without a debugger.
       ai_src = ao_src = "driver"
       if not ai_types and has_ai:
           ai_types = infer_types("ai")
           ai_src = "inferred from ranges"
       if not ao_types and has_ao:
           ao_types = infer_types("ao")
           ao_src = "inferred from ranges"

       for direction, tps, src, note, present in (
               ("input",  ai_types, ai_src, ai_note, has_ai),
               ("output", ao_types, ao_src, ao_note, has_ao)):
           if not present:
               continue
           if note:
               print(f"DAQ '{dev_name}' {direction} types: {note}")
           if src != "driver":
               if tps:
                   print(f"DAQ '{dev_name}' {direction} types {tps} "
                         f"{src} (driver did not report them).")
               else:
                   # The loud version of what used to be a silent assumption.
                   print(f"DAQ '{dev_name}': could not determine {direction} "
                         f"type from the driver or from its ranges; assuming "
                         f"{_DEFAULT_TYPE}. If this card is not a voltage card, "
                         f"its Min/Max and unit will be wrong.")

       # One range list per unit, keyed by the unit the DAQ reports it in
       # (volts for voltage types, amps for current types, …).
       ai_by_unit, ao_by_unit = {}, {}
       for label in (ai_types or [_DEFAULT_TYPE]):
           rng = ranges(_rng_attr_for_type(label, "ai"))
           if rng:
               ai_by_unit.setdefault(_unit_for_type(label, "ai"), rng)
       for label in (ao_types or [_DEFAULT_TYPE]):
           rng = ranges(_rng_attr_for_type(label, "ao"))
           if rng:
               ao_by_unit.setdefault(_unit_for_type(label, "ao"), rng)

       # Terminal configs: device-level first, then per-channel, then default
       term_cfgs = []
       try:
           for t in daq.ai_term_cfgs:
               term_cfgs.append(str(t).split('.')[-1].split(':')[0].strip().upper())
       except Exception:
           try:
               chans = daq.ai_physical_chans
               if chans:
                   for t in chans[0].ai_term_cfgs:
                       term_cfgs.append(str(t).split('.')[-1].split(':')[0].strip().upper())
           except Exception:
               term_cfgs = []
       # Only fall back for a device that actually has inputs; an AO-only
       # module should report nothing here. The fallback is DEFAULT alone, not
       # the generic RSE/NRSE/DIFF list: if the driver would not say what the
       # card supports, offering three specific configurations invites the user
       # to pick one the card rejects (-200077 at task build). DEFAULT lets
       # DAQmx choose the card's natural configuration, which is also the only
       # sensible answer for a current module.
       if not term_cfgs and has_ai:
           term_cfgs = ['DEFAULT']

       return {
           'ai_ranges':         ai_by_unit.get('V', []),
           'ao_ranges':         ao_by_unit.get('V', []),
           'ai_ranges_by_unit': ai_by_unit,
           'ao_ranges_by_unit': ao_by_unit,
           'ai_types':          ai_types,
           'ao_types':          ao_types,
           'term_cfgs':         term_cfgs,
       }

   def _caps(self, dev_name):
       """Cached _get_daq_capabilities — never None, so callers can .get().

       The cache is cleared on every update_channels() rebuild, so hot-plugging
       a module and refreshing picks up the new hardware.
       """
       if not hasattr(self, "_caps_cache"):
           self._caps_cache = {}
       if dev_name not in self._caps_cache:
           self._caps_cache[dev_name] = self._get_daq_capabilities(dev_name) or {}
       return self._caps_cache[dev_name]

   # There is no global "current device": every channel row resolves its own
   # card via _row_dev(), so one screen can mix a USB DAQ with several C-Series
   # modules and each row gets its own types, units, ranges and clamp limits.
   # That is why capability lookups all take a device name rather than reading
   # some selected-device state.
   #
   # Everything that differs between the Input and Output tabs lives here, so
   # the logic that uses it can be written once and take a `direction`.
   _DIR = {
       'ai': {'suffix': '',        'type_key': 'ai_type',
              'daq': 'DAQ_entry',        'ranges': 'ai_ranges_by_unit',
              'types': 'ai_types',       'attr': 'ai_meas_types',
              'chans': 'ai_physical_chans', 'verb': 'accepts'},
       'ao': {'suffix': '_output', 'type_key': 'output_type',
              'daq': 'DAQ_entry_output', 'ranges': 'ao_ranges_by_unit',
              'types': 'ao_types',       'attr': 'ao_output_types',
              'chans': 'ao_physical_chans', 'verb': 'can generate'},
   }

   def _ranges_for(self, dev_name, direction, unit):
       """Supported ranges on `dev_name` for one direction ('ai'/'ao') in `unit`."""
       key = self._DIR[direction]['ranges']
       return self._caps(dev_name).get(key, {}).get(unit, [])

   def _types_for(self, dev_name, direction='ai'):
       """Input/output types this device offers, after filtering and ordering."""
       key = self._DIR[direction]['types']
       return self._resolve_types(
           dev_name, self._caps(dev_name).get(key) or [_DEFAULT_TYPE], direction)

   # Input types listed first when a card reports several. A multifunction
   # board (USB-6003) reports its plain analog modes alongside housekeeping
   # entries like the built-in temperature sensor and TEDS, in driver order —
   # so without an explicit preference the first item can be something no one
   # wants, and the derived unit then has no matching range.
   _TYPE_PREFERENCE = ("VOLTAGE", "CURRENT", "RESISTANCE", "BRIDGE",
                       "TEMPERATURE_THERMOCOUPLE", "TEMPERATURE_RTD")

   # Reported by ai_meas_types but never a choice about how a channel is wired.
   # A multifunction board like the USB-6003 lists its internal temperature
   # sensor and TEDS support alongside VOLTAGE; those are device features, not
   # signal types you can connect something to, and leaving them in produced a
   # dropdown on a card that only accepts voltage.
   _NON_WIRING_TYPES = frozenset({
       "TEDS",                         # sensor self-identification, not a signal
       "TEMPERATURE_BUILT_IN_SENSOR",  # the board's own die temperature
       "POWER", "CALCULATED_POWER",    # derived, not a physical input
       # These share a range attribute with plain voltage/current, so the range
       # test below cannot separate them, and each is the same physical
       # connection with a different computation layered on top — a measurement
       # mode rather than a wiring choice:
       #   *_ACRMS                       returns an RMS figure instead of a level
       #   VOLTAGE_CUSTOM_WITH_EXCITATION divides by a stated excitation voltage
       #                                  to give a ratio, for ratiometric
       #                                  sensors such as potentiometers
       # Delete an entry here if a channel ever needs to be configured that way.
       "VOLTAGE_ACRMS", "CURRENT_ACRMS", "VOLTAGE_CUSTOM_WITH_EXCITATION",
   })

   @classmethod
   def _order_types(cls, types):
       """Drop non-wiring entries, dedupe, put the likeliest choice first."""
       real = [t for t in dict.fromkeys(types)
               if _type_key(t) not in cls._NON_WIRING_TYPES]

       def rank(t):
           key = _type_key(t)
           return (cls._TYPE_PREFERENCE.index(key)
                   if key in cls._TYPE_PREFERENCE else len(cls._TYPE_PREFERENCE))
       return sorted(real, key=rank)

   def _types_the_card_ranges(self, dev, types, direction="ai"):
       """Keep only the types this card reports an actual range for.

       ai_meas_types is not a list of what the hardware natively measures — it
       is what DAQmx will let you *configure*, and the driver can synthesise
       thermocouple, strain, bridge and RTD channels from a plain voltage input
       by scaling in software. That is why a USB-6003 offers twenty-odd types
       when its front end only does volts.

       Reporting a range is the honest test of native support: the 6003 answers
       ai_voltage_rngs and raises on ai_current_rngs, so voltage survives and
       current does not. Types whose range is not queryable at all (thermocouple,
       strain) cannot be confirmed this way, so they are kept only when nothing
       else qualifies — which is what leaves a dedicated NI-9213 showing
       thermocouple rather than falling back to voltage.
       """
       key = 'ai_ranges_by_unit' if direction == 'ai' else 'ao_ranges_by_unit'
       by_unit = self._caps(dev).get(key, {})
       if not by_unit:
           return list(types)          # card told us nothing; do not second-guess
       confirmed, unconfirmable = [], []
       for t in types:
           if _rng_attr_for_type(t, direction) is None:
               unconfirmable.append(t)
           elif _unit_for_type(t, direction) in by_unit:
               confirmed.append(t)
       return confirmed or unconfirmable or list(types)

   def _chan_types(self, dev, chan, direction="ai"):
       """Input/output types supported by one physical channel, best first.

       Queried per channel because that is the finest granularity the driver
       exposes. Most modules report exactly one — an NI-9203 channel is CURRENT
       and nothing else — so there is normally no choice to present. A
       multifunction or universal board reports several, and only then does the
       user have to say what is actually wired.
       """
       if nidaqmx is None:
           return [_DEFAULT_TYPE]
       attr = self._DIR[direction]['attr']
       coll = self._DIR[direction]['chans']
       # `chan` may arrive bare ('ai0') or fully qualified ('Dev1/ai0'), and the
       # collection is indexable by name or position. Try each rather than
       # assuming, because a miss here silently falls through to the device list.
       bare = str(chan).split('/')[-1]
       try:
           chans = getattr(Device(dev), coll)
           for key in (f"{dev}/{bare}", bare):
               try:
                   types = [_pretty_type(t) for t in getattr(chans[key], attr)]
                   if types:
                       return self._resolve_types(dev, types, direction)
               except Exception:
                   continue
           for pc in chans:
               if str(getattr(pc, "name", "")).split('/')[-1] == bare:
                   types = [_pretty_type(t) for t in getattr(pc, attr)]
                   if types:
                       return self._resolve_types(dev, types, direction)
       except Exception:
           pass
       # Fall back to the device-wide list, then to Voltage.
       return self._types_for(dev, direction)

   def _resolve_types(self, dev, types, direction="ai"):
       """Full pipeline: drop non-wiring, keep only card-ranged, order, default.

       Output types are additionally narrowed to the ones the app can open a
       channel for (Voltage, Current). An analog output is a level the app
       commands, so anything else — the card's onboard function generator, say —
       is a mode nothing here drives, and offering it would only produce a row
       that add_ao_channel then refuses. Inputs are deliberately NOT filtered
       this way: a dedicated thermocouple module has no voltage mode to fall
       back to, and showing its real type and failing with a clear message
       beats showing Voltage and failing in the driver.
       """
       out = self._order_types(
           self._types_the_card_ranges(dev, self._order_types(types), direction))
       if direction == "ao":
           openable = set(_AO_ADD_CHAN)
           out = [t for t in out if _type_key(t) in openable]
       return out or [_DEFAULT_TYPE]

   def _w_text(self, key):
       """Text of a widget that may be a combobox or a read-only line edit."""
       w = self.entry_widgets.get(key)
       if w is None:
           return ""
       if isinstance(w, QComboBox):
           return w.currentText()
       try:
           return w.text()
       except Exception:
           return ""

   def _make_type_widget(self, types, tooltip):
       """Read-only field when the hardware supports one type, dropdown when
       it genuinely supports several.

       The type is a property of the card, so there is nothing to choose in the
       common case and a dropdown would only invite a wrong answer.
       """
       if len(types) <= 1:
           w = QLineEdit(types[0] if types else _DEFAULT_TYPE)
           w.setEnabled(False)
           w.setReadOnly(True)
           w.setStyleSheet("color: #666; background: #f0f0f0;")
           w.setToolTip(tooltip + "\nDetermined by the card — not editable.")
       else:
           w = QComboBox()
           w.addItems(types)
           w.setToolTip(tooltip + "\nThis module supports several; pick the one "
                                  "matching what is wired.")
       w.setMinimumWidth(115)
       return w

   def _on_ai_channel_selected(self, row, state):
       """When an input channel is checked, filter terminal config to DAQ-supported only.
       When unchecked, restore the full list."""
       cb_ter = self.entry_widgets.get(f"ter_selection_var{row}")
       if cb_ter is None:
           return
       current = cb_ter.currentText()
       if state == Qt.Checked:
           caps = self._caps(self._row_dev(row, 'ai'))
           terms = caps.get('term_cfgs') or self.Terminal_options_list
       else:
           terms = self.Terminal_options_list
       # Ticking a channel fills its Min/Max from the card, unless a transducer
       # already set a narrower span.
       if state == Qt.Checked and not self._w_text(f"transducer_sel{row}"):
           self._fill_ai_range_from_card(row)

       terms = self._terminal_options_for_row(row, terms)
       cb_ter.blockSignals(True)
       cb_ter.clear()
       cb_ter.addItems(terms)
       # Restore previous selection if still valid, otherwise pick first
       if current in terms:
           cb_ter.setCurrentText(current)
       elif self.Default_terminal in terms:
           cb_ter.setCurrentText(self.Default_terminal)
       cb_ter.blockSignals(False)
       self._refresh_diff_locks()

   # ------------------------------------------------------------------
   # Differential (DIFF) channel pairing
   # ------------------------------------------------------------------
   #
   # On a multiplexed NI device a differential channel is built from two AI
   # pins: ai<N> is the positive input and ai<N + offset> is the negative
   # input, where offset is half the device's AI pin count.
   #
   #   USB-6001/6002/6003  (8 AI pins)  ->  ai0/ai4  ai1/ai5  ai2/ai6  ai3/ai7
   #   USB-621x / 62xx     (16 AI pins) ->  ai0/ai8  …  ai7/ai15
   #
   # Consequences enforced below:
   #   * only the lower half of the pins may be set to DIFF;
   #   * selecting DIFF reserves the paired pin, so that row is unchecked,
   #     greyed out, and given a tooltip naming its owner.

   @staticmethod
   def _chan_index(chan_name):
       """Trailing number of a channel name ('ai12' -> 12), or None."""
       m = re.search(r'(\d+)\s*$', chan_name or '')
       return int(m.group(1)) if m else None

   def _dev_uses_diff_pin_pairing(self, dev):
       """True when a differential input on `dev` consumes a second AI pin.

       That is how multiplexed boards work (USB-6001, USB-621x…). A C-Series
       module in a cDAQ/cRIO chassis gives every channel its own dedicated pin
       pair, so nothing is consumed and the pairing rules must not apply —
       otherwise setting DIFF on an NI-9203's ai0 would needlessly lock out
       ai4, costing half the module's channels.
       """
       if nidaqmx is None:
           return True            # dummy device — keep the previous behaviour
       if not hasattr(self, "_diff_pairing_cache"):
           self._diff_pairing_cache = {}
       if dev in self._diff_pairing_cache:
           return self._diff_pairing_cache[dev]
       result = True
       try:
           cat = getattr(Device(dev), "product_category", None)
           name = getattr(cat, "name", str(cat or "")).upper()
           # Exact ProductCategory names from nidaqmx.constants.ProductCategory.
           if name in ("C_SERIES_MODULE", "TEST_SCALE_MODULE",
                       "SCXI_MODULE", "SCC_MODULE", "FIELD_DAQ"):
               result = False
       except Exception:
           result = True          # unknown hardware — don't loosen the rules
       self._diff_pairing_cache[dev] = result
       return result

   def _ai_pair_offset(self, dev):
       """Pin distance between the + and - halves for DIFF on `dev`
       (half the AI pin count). 0 when there is no pairing to enforce."""
       if not self._dev_uses_diff_pin_pairing(dev):
           return 0
       n = len(self._ai_dev_rows.get(dev, {}))
       return n // 2 if n >= 2 else 0

   def _ai_row_label(self, row):
       """'Dev1/ai0'-style label for messages and tooltips."""
       info = self._ai_row_info.get(row)
       if not info:
           w = self.entry_widgets.get(f"Channel_entry{row}")
           return w.text() if w else f"row {row}"
       dev, idx = info
       w = self.entry_widgets.get(f"Channel_entry{row}")
       chan = w.text() if w else f"ai{idx}"
       return f"{dev}/{chan}"

   def _row_can_be_diff(self, row):
       """True if this row's pin may be the + input of a DIFF pair."""
       info = self._ai_row_info.get(row)
       if not info:
           return True
       dev, idx = info
       off = self._ai_pair_offset(dev)
       if off <= 0 or idx is None:
           return True          # unknown layout — don't restrict the user
       return idx < off

   def _diff_partner_row(self, row):
       """Row of the pin used as the negative input when `row` is DIFF."""
       info = self._ai_row_info.get(row)
       if not info:
           return None
       dev, idx = info
       off = self._ai_pair_offset(dev)
       if off <= 0 or idx is None or idx >= off:
           return None
       return self._ai_dev_rows.get(dev, {}).get(idx + off)

   def _terminal_options_for_row(self, row, terms):
       """Drop DIFF from a row's terminal list when that pin can only ever
       be the negative half of someone else's differential pair."""
       if self._row_can_be_diff(row):
           return list(terms)
       return [t for t in terms if t.strip().upper() != "DIFF"]

   def _row_is_diff(self, row):
       ter = self.entry_widgets.get(f"ter_selection_var{row}")
       return bool(ter) and ter.currentText().strip().upper() == "DIFF"

   def _diff_reserved_rows(self):
       """Map row -> label of the DIFF channel that owns it as its (-) input."""
       reserved = {}
       for row in self._ai_row_info:
           chk = self.check_vars.get(f"Selected Channel{row}")
           if not chk or not chk.isChecked():
               continue
           if not self._row_is_diff(row):
               continue
           partner = self._diff_partner_row(row)
           if partner is not None:
               reserved[partner] = self._ai_row_label(row)
       return reserved

   def _ai_row_widgets(self, row):
       """Every widget belonging to one Input Channels row."""
       keys = (f"Channel_entry{row}", f"DAQ_entry{row}", f"ai_type{row}",
               f"transducer_sel{row}",
               f"min_entry{row}", f"max_entry{row}", f"unit_lbl{row}",
               f"ter_selection_var{row}", f"mx_plus_entry{row}",
               f"b_entry{row}", f"units_entry{row}", f"Device_name{row}",
               f"Selected Channel{row}")
       return [self.entry_widgets[k] for k in keys if k in self.entry_widgets]

   def _clear_ai_row(self, row):
       """Blank out everything the user entered in an Input row."""
       for key in (f"min_entry{row}", f"max_entry{row}",
                   f"mx_plus_entry{row}", f"b_entry{row}",
                   f"units_entry{row}", f"Device_name{row}"):
           le = self.entry_widgets.get(key)
           if le is None:
               continue
           was = le.isEnabled()
           le.setEnabled(True)        # a disabled QLineEdit still accepts setText,
           le.clear()                 # but keep this explicit for clarity
           le.setEnabled(was)

       cb = self.entry_widgets.get(f"transducer_sel{row}")
       if cb:
           cb.blockSignals(True)
           cb.setCurrentIndex(0 if cb.count() else -1)
           cb.blockSignals(False)

       ter = self.entry_widgets.get(f"ter_selection_var{row}")
       if ter:
           ter.blockSignals(True)
           ter.setCurrentText(self.Default_terminal)
           ter.blockSignals(False)

   def _lock_ai_row_for_diff(self, row, owner_label):
       """Reserve a row: untick it, clear it, grey it out, explain why on hover."""
       tip = (f"Not available — used by {owner_label} as its negative (\u2212) "
              f"input in DIFF mode.\n"
              f"Set {owner_label} back to RSE/NRSE to free this channel.")

       chk = self.check_vars.get(f"Selected Channel{row}")
       if chk and chk.isChecked():
           chk.blockSignals(True)
           chk.setChecked(False)
           chk.blockSignals(False)

       # The pin belongs to the DIFF pair now, so nothing entered for it as a
       # standalone channel is still meaningful — wipe it rather than hide it.
       self._clear_ai_row(row)

       for w in self._ai_row_widgets(row):
           w.setEnabled(False)
           w.setToolTip(tip)

       self._diff_locked_rows.add(row)

   def _unlock_ai_row(self, row):
       """Release a previously reserved row and restore its normal state."""
       if row not in self._diff_locked_rows:
           return
       self._diff_locked_rows.discard(row)

       for w in self._ai_row_widgets(row):
           w.setToolTip("")

       # Channel / DAQ columns are read-only by design — leave them disabled.
       for key in (f"transducer_sel{row}", f"Device_name{row}",
                   f"Selected Channel{row}"):
           w = self.entry_widgets.get(key)
           if w:
               w.setEnabled(True)

       self.set_row_field_state(row, True)

   def _refresh_diff_locks(self, *_):
       """Recompute which AI rows are consumed as differential (-) inputs."""
       if self._diff_lock_busy or not self._ai_row_info:
           return
       self._diff_lock_busy = True
       try:
           reserved = self._diff_reserved_rows()
           changed = False
           for row in sorted(self._ai_row_info):
               owner = reserved.get(row)
               was = row in self._diff_locked_rows
               if owner:
                   self._lock_ai_row_for_diff(row, owner)
               else:
                   self._unlock_ai_row(row)
               changed = changed or (was != (row in self._diff_locked_rows))
       finally:
           self._diff_lock_busy = False

       if changed:
           # Rows are ticked/unticked with signals blocked above, so refresh the
           # Custom Channels A/B pools explicitly.
           self._update_custom_dropdowns()

   def _on_output_channel_selected(self, row, state):
       """When an output channel is checked, auto-fill min/max from the DAQ's
       widest supported range for that row's output type (fields stay editable).
       When unchecked, no action needed — fields stay as-is.

       The range is looked up in the row's own unit, so a current output
       pre-fills its amp range rather than being left blank because the module
       reports no voltage range at all.
       """
       le_min = self.entry_widgets.get(f"min_entry_output{row}")
       le_max = self.entry_widgets.get(f"max_entry_output{row}")
       if le_min is None or le_max is None:
           return

       if state == Qt.Checked:
           dev = self._row_dev(row, 'ao')
           rngs = self._ranges_for(dev, 'ao', self._row_unit(row, 'ao'))
           if rngs:
               # Widest supported range as a starting point; the user can adjust
               # and the values are validated on save.
               le_min.setText(_fmt_num(min(mn for mn, mx in rngs)))
               le_max.setText(_fmt_num(max(mx for mn, mx in rngs)))

   @staticmethod
   def _chassis_modules(dev):
       """Return the list of module Devices for a CompactDAQ/CompactRIO
       chassis, else []. A bare chassis has no ai/ao channels of its own —
       its modules carry them."""
       try:
           cat = getattr(dev, "product_category", None)
           is_chassis = False
           if cat is not None:
               cat_name = getattr(cat, "name", str(cat))
               is_chassis = cat_name in ("COMPACT_DAQ_CHASSIS", "COMPACT_RIO_CHASSIS")
           mods = list(getattr(dev, "chassis_module_devices", []) or [])
           if is_chassis or mods:
               return mods
       except Exception:
           pass
       return []

   def _iter_devices(self):
       """Yield Device objects across the whole system, expanding any
       chassis into its C-Series modules (the modules are what own the
       physical channels).

       NI-DAQmx lists a chassis AND each of its modules as separate top-level
       entries in System.local().devices, so we must dedupe by name: a module
       is emitted once (via its chassis expansion or as a standalone), never
       twice.
       """
       if nidaqmx is None:
           return
       seen = set()
       try:
           for dev in System.local().devices:
               modules = self._chassis_modules(dev)
               if modules:
                   # This is a chassis — emit its modules, skip the chassis itself.
                   for m in modules:
                       if m.name not in seen:
                           seen.add(m.name)
                           yield m
               else:
                   # Standalone device, or a module reached directly. Emit only
                   # if a chassis expansion hasn't already produced it.
                   if dev.name not in seen:
                       seen.add(dev.name)
                       yield dev
       except Exception:
           return

   def _get_daq_list(self):
       """Flat list of every selectable device/module name."""
       if nidaqmx is None:
           return ["Dev1"]
       try:
           names = [d.name for d in self._iter_devices()]
           return names if names else ["Dev1"]
       except Exception:
           return ["Dev1"]

   def _get_all_ai_channels(self):
       """Return [(device_name, 'ai0'), ...] for every AI channel on every
       device/module in the system. Falls back to a dummy device when no
       hardware/driver is present."""
       pairs = []
       if nidaqmx is None:
           return [("Dev1", f"ai{i}") for i in range(4)]
       try:
           for dev in self._iter_devices():
               try:
                   for ch in dev.ai_physical_chans:
                       pairs.append((dev.name, ch.name.split("/")[-1]))
               except Exception:
                   continue
       except Exception:
           pass
       return pairs

   def _get_all_ao_channels(self):
       """Return [(device_name, 'ao0'), ...] for every AO channel system-wide."""
       pairs = []
       if nidaqmx is None:
           return [("Dev1", f"ao{i}") for i in range(2)]
       try:
           for dev in self._iter_devices():
               try:
                   for ch in dev.ao_physical_chans:
                       pairs.append((dev.name, ch.name.split("/")[-1]))
               except Exception:
                   continue
       except Exception:
           pass
       return pairs

   def _get_all_digital_lines(self):
       """Return [(device_name, 'port0/line0'), ...] for every DI/DO line
       system-wide, deduplicated per device."""
       pairs = []
       if nidaqmx is None:
           return [("Dev1", f"port0/line{i}") for i in range(8)]
       try:
           for dev in self._iter_devices():
               seen = {}
               try:
                   for src in (dev.di_lines, dev.do_lines):
                       for ch in src:
                           short = "/".join(ch.name.split("/")[1:])
                           seen[short] = True
               except Exception:
                   pass

               def _sort_key(s):
                   parts = s.replace("port", "").replace("line", "").split("/")
                   return tuple(int(p) if p.isdigit() else p for p in parts)

               for short in sorted(seen.keys(), key=_sort_key):
                   pairs.append((dev.name, short))
       except Exception:
           pass
       return pairs

   # ------------------------------------------------------------------
   # Utility
   # ------------------------------------------------------------------

   def strip_value(self, value):
       return value.strip() if isinstance(value, str) else value

   # ------------------------------------------------------------------
   # Transducer helpers
   # ------------------------------------------------------------------

   def get_transducer_names(self):
       """Non-empty Hardware entries from Transducers tab, deduplicated."""
       names, seen, uniq = [], set(), []
       for i in range(1, self.no_of_transducers + 1):
           w = self.entry_widgets.get(f"trans_hw{i}")
           if w:
               hw = self.strip_value(w.text())
               if hw:
                   names.append(hw)
       for n in names:
           if n not in seen:
               seen.add(n)
               uniq.append(n)
       return uniq

   def get_transducer_dict(self):
       """Build  name -> {Min Output Range, Output Unit, ...}  from the
       Transducers tab.

       'Output Unit' is the electrical signal the transducer presents to the
       card (V, mA, …) and is what must match what the channel accepts.
       'Scaled Units' is the unit the reading becomes after Mx + b, and
       'Min/Max Signal Input' is the physical span in those units.
       """
       d = {}
       for i in range(1, self.no_of_transducers + 1):
           hw_w = self.entry_widgets.get(f"trans_hw{i}")
           if not hw_w:
               continue
           name = self.strip_value(hw_w.text())
           if not name:
               continue
           d[name] = {
               "Hardware Type":          self.entry_widgets.get(f"trans_type{i}", QLineEdit()).text(),
               "Min Output Range":       self.entry_widgets.get(f"trans_minv{i}", QLineEdit()).text(),
               "Max Output Range":       self.entry_widgets.get(f"trans_maxv{i}", QLineEdit()).text(),
               "Output Unit":            self._trans_output_unit(i),
               "Min Signal Input":       self.entry_widgets.get(f"trans_min_sig{i}", QLineEdit()).text(),
               "Max Signal Input":       self.entry_widgets.get(f"trans_max_sig{i}", QLineEdit()).text(),
               "Terminal Configuration": self.entry_widgets.get(f"trans_term{i}", QComboBox()).currentText(),
               "Mx":                     self.entry_widgets.get(f"trans_mx{i}",   QLineEdit()).text(),
               "B":                      self.entry_widgets.get(f"trans_b{i}",    QLineEdit()).text(),
               "Scaled Units":           self.entry_widgets.get(f"trans_units{i}",QLineEdit()).text(),
           }
       return d

   def _trans_output_unit(self, i):
       """Output Unit for transducer row i, defaulting to volts."""
       cb = self.entry_widgets.get(f"trans_output_unit{i}")
       return (cb.currentText().strip() if cb else "") or "V"

   def _row_type(self, row, direction='ai'):
       """The input/output type selected or derived for one row."""
       return self._w_text(f"{self._DIR[direction]['type_key']}{row}") or _DEFAULT_TYPE

   def _row_unit(self, row, direction='ai'):
       """Unit a row's Min/Max are expressed in, from its type."""
       return _unit_for_type(self._row_type(row, direction), direction)

   def _row_signal_dim(self, row, direction='ai'):
       """Dimension of the signal a row accepts, for transducer matching."""
       return _signal_dim_for_type(self._row_type(row, direction), direction)

   def _row_dev(self, row, direction='ai'):
       """Device/module the row's channel lives on."""
       return self._row_device_name(self._DIR[direction]['daq'], row)

   def _card_range(self, row, direction='ai'):
       """(min, max) this channel supports in its own unit, or None if the card
       does not report one."""
       rngs = self._ranges_for(self._row_dev(row, direction), direction,
                               self._row_unit(row, direction))
       if not rngs:
           return None
       return (min(mn for mn, _ in rngs), max(mx for _, mx in rngs))

   def _refresh_row_unit(self, row, direction='ai'):
       """Update a row's unit labels and the range tooltip on its Min/Max.

       There is no Card Range column: the supported range is surfaced as a
       tooltip on the two fields it constrains, and enforced on edit and on save.
       """
       sfx  = self._DIR[direction]['suffix']
       unit = self._row_unit(row, direction)
       lbl = self.entry_widgets.get(f"unit_lbl{sfx}{row}")
       if lbl is not None:
           lbl.setText(f"({unit})")
       rng = self._card_range(row, direction)
       tip = (f"Card {self._DIR[direction]['verb']} {_fmt_num(rng[0])} to "
              f"{_fmt_num(rng[1])} {unit} on this channel." if rng else
              "Supported range not reported by this card.")
       for key in (f"min_entry{sfx}{row}", f"max_entry{sfx}{row}"):
           le = self.entry_widgets.get(key)
           if le is not None:
               le.setToolTip(tip)

   def _clamp_to_card(self, row, which, direction="ai"):
       """Failsafe: pull a typed Min/Max back inside what the card accepts.

       Save-time validation already refuses an out-of-range value, but that is
       late — this stops a bad number being entered at all, so a channel can
       never be configured to ask the card for a range it cannot deliver.
       Non-numeric text is left alone for validation to report.
       """
       suffix = self._DIR[direction]['suffix']
       le = self.entry_widgets.get(f"{which}_entry{suffix}{row}")
       if le is None or not le.text().strip():
           return
       rng = self._card_range(row, direction)
       if rng is None:
           return
       try:
           v = float(le.text().strip())
       except ValueError:
           return
       clamped = min(max(v, rng[0]), rng[1])
       if clamped != v:
           unit = self._row_unit(row, direction)
           le.setText(_fmt_num(clamped))
           self._flash_field(le, f"{_fmt_num(v)} {unit} is outside the range this "
                                 f"card accepts ({_fmt_num(rng[0])} to "
                                 f"{_fmt_num(rng[1])} {unit}) — clamped.")

   def _flash_field(self, widget, message):
       """Briefly mark a field and say why its value changed."""
       widget.setToolTip(message)
       prev = widget.styleSheet()
       widget.setStyleSheet("border: 1px solid #c0392b;")
       QTimer.singleShot(2000, lambda: widget.setStyleSheet(prev))
       print(message)

   def _fill_ai_range_from_card(self, row, force=False):
       """Seed an Input row's Min/Max with the channel's full supported range.

       This is the starting point when no transducer is linked: the card decides
       what the channel can accept. A linked transducer then narrows it to the
       span the sensor actually uses, which is what gets handed to DAQmx so the
       driver can pick the tightest gain.
       """
       le_min = self.entry_widgets.get(f"min_entry{row}")
       le_max = self.entry_widgets.get(f"max_entry{row}")
       if le_min is None or le_max is None:
           return
       if not force and (le_min.text().strip() or le_max.text().strip()):
           return                      # don't clobber loaded or typed values
       rngs = self._ranges_for(self._row_dev(row, 'ai'), 'ai',
                              self._row_unit(row, 'ai'))
       if not rngs:
           return
       for w, v in ((le_min, min(mn for mn, _ in rngs)),
                    (le_max, max(mx for _, mx in rngs))):
           was = w.isEnabled()
           w.setEnabled(True)
           w.setText(_fmt_num(v))
           w.setEnabled(was)

   def _on_ai_type_changed(self, row):
       """Input type changed: relabel the units and re-filter the transducer list.

       A transducer whose Output Unit no longer matches the channel is dropped,
       because its Min/Max could not be meaningfully converted into the new
       unit (0–10 V means nothing on a current input).
       """
       self._refresh_row_unit(row, 'ai')
       cb = self.entry_widgets.get(f"transducer_sel{row}")
       cur = cb.currentText() if cb else ""
       if cur:
           tdict = self.get_transducer_dict()
           t_unit = (tdict.get(cur, {}) or {}).get("Output Unit", "V")
           if _unit_dim(t_unit) != self._row_signal_dim(row, 'ai'):
               cb.blockSignals(True)
               cb.setCurrentText("")
               cb.blockSignals(False)
               self.set_row_field_state(row, True)
       self.refresh_transducer_dropdowns()
       if cb and cb.currentText():
           self.apply_transducer_to_row(row, cb.currentText())

   def _reapply_all_transducers(self):
       """Re-apply current Transducers-tab data to every Input row that has one selected.

       Called whenever a Transducers-tab field is edited AND at the start of
       save_settings, so the locked input fields are always up to date.
       """
       for i in range(1, len(self.chan_names) + 1):
           cb = self.entry_widgets.get(f"transducer_sel{i}")
           if cb and cb.currentText():
               self.apply_transducer_to_row(i, cb.currentText())

   def _refresh_trans_scaled_unit(self, i):
       """Show the row's Scaled Units beside its Signal Input fields."""
       le = self.entry_widgets.get(f"trans_units{i}")
       raw = le.text() if le else ""
       txt = f"({raw.strip()})" if raw.strip() else ""
       lbl = self.entry_widgets.get(f"trans_sig_unit{i}")
       if lbl is not None:
           lbl.setText(txt)
           _apply_unit_color(lbl, raw)

   def _on_trans_output_unit_changed(self, *_):
       """A transducer's Output Unit changed — re-filter the Input-tab dropdowns
       (it may no longer suit the channel it is linked to) and re-apply values,
       which converts them into each channel's unit."""
       self.refresh_transducer_dropdowns()
       self._reapply_all_transducers()

   def _on_hw_editing_finished(self, row_idx):
       """Handle a hardware-name field losing focus.

       - Rejects duplicates (restores the previous name).
       - Renames any Input Channels combobox that referenced the old name so
         the link is never broken by a rename.
       - Refreshes all transducer dropdowns and re-applies linked values.
       """
       hw_w = self.entry_widgets.get(f"trans_hw{row_idx}")
       if not hw_w:
           return

       new_name = self.strip_value(hw_w.text())
       old_name = hw_w.property("prev_name") or ""

       if new_name == old_name:
           return  # nothing changed — skip all work

       # ── Duplicate check ──────────────────────────────────────────
       if new_name:
           for i in range(1, self.no_of_transducers + 1):
               if i == row_idx:
                   continue
               other = self.entry_widgets.get(f"trans_hw{i}")
               if other and self.strip_value(other.text()) == new_name:
                   QMessageBox.warning(
                       self, "Duplicate Transducer Name",
                       f'A transducer named "{new_name}" already exists in row {i}.\n'
                       f'Hardware Model names must be unique.'
                   )
                   hw_w.setText(old_name)   # restore previous name
                   hw_w.setFocus()
                   return

       # ── Rename: update any Input-tab combobox that showed old_name ──
       if old_name and old_name != new_name:
           for i in range(1, len(self.chan_names) + 1):
               cb = self.entry_widgets.get(f"transducer_sel{i}")
               if cb and cb.currentText() == old_name:
                   cb.blockSignals(True)
                   idx = cb.findText(old_name)
                   if idx >= 0:
                       # Update the item text in-place so selection follows
                       cb.setItemText(idx, new_name if new_name else "")
                   cb.setCurrentText(new_name if new_name else "")
                   cb.blockSignals(False)

       # Commit new name as the baseline for future edits
       hw_w.setProperty("prev_name", new_name)

       # Refresh dropdown lists and reapply current transducer values
       self.refresh_transducer_dropdowns()
       self._reapply_all_transducers()

   def _sync_hw_prev_names(self):
       """Snapshot current hardware names into each widget's prev_name property.

       Must be called after loading from file so that the first real edit
       has a correct old-name baseline to compare against.
       """
       for i in range(1, self.no_of_transducers + 1):
           hw_w = self.entry_widgets.get(f"trans_hw{i}")
           if hw_w:
               hw_w.setProperty("prev_name", self.strip_value(hw_w.text()))

   def refresh_transducer_dropdowns(self):
       """Update every Transducer combobox in the Input Channels tab.

       A row offers a transducer only when both of these hold: its output signal
       is the kind the channel accepts (a 4–20 mA sensor appears on a current
       input, not a voltage one), and its output range fits inside the range the
       card accepts. mA and A count as the same signal — values are converted on
       apply.

       The comparison is against the channel's signal dimension, not its range
       unit, because those differ for sensor types: a thermocouple channel reads
       out in degC but accepts a millivolt signal, so mV transducers belong on
       it.

       An already-selected transducer that no longer fits is kept in the list
       rather than silently dropped; validation reports the mismatch.
       """
       names = self.get_transducer_names()
       tdict = self.get_transducer_dict()
       for i in range(1, len(self.chan_names) + 1):
           cb = self.entry_widgets.get(f"transducer_sel{i}")
           if not cb:
               continue
           row_dim  = self._row_signal_dim(i, 'ai')
           row_unit = self._row_unit(i, 'ai')
           card     = self._card_range(i, 'ai')
           opts = [""]
           for name in names:
               tv = tdict.get(name, {}) or {}
               t_unit = tv.get("Output Unit", "V")
               if row_dim is None or _unit_dim(t_unit) != row_dim:
                   continue
               # Its output must also fit inside what the card accepts: a
               # 0-20 V sensor is the right kind of signal for a +/-10 V input
               # but would saturate it, so it is not offered.
               if card is not None:
                   lo = _convert_unit(tv.get("Min Output Range", ""), t_unit, row_unit)
                   hi = _convert_unit(tv.get("Max Output Range", ""), t_unit, row_unit)
                   if lo is not None and hi is not None:
                       if min(lo, hi) < card[0] or max(lo, hi) > card[1]:
                           continue
               opts.append(name)
           cur = cb.currentText()
           if cur and cur not in opts:
               opts.append(cur)
           cb.blockSignals(True)
           cb.clear()
           cb.addItems(opts)
           cb.setCurrentText(cur if cur in opts else "")
           cb.blockSignals(False)

   def set_row_field_state(self, row_idx, enabled: bool):
       """Enable / disable the six editable data fields in an Input row."""
       # A row reserved as the negative input of a DIFF pair stays greyed out
       # no matter what else (e.g. a transducer change) asks for.
       if enabled and row_idx in getattr(self, "_diff_locked_rows", set()):
           enabled = False
       for key in (
           f"min_entry{row_idx}", f"max_entry{row_idx}",
           f"mx_plus_entry{row_idx}",     f"b_entry{row_idx}",
           f"units_entry{row_idx}",
       ):
           w = self.entry_widgets.get(key)
           if w:
               w.setEnabled(enabled)
       # Input Type is hardware-determined rather than transducer-driven, so it
       # only greys out when the whole row is reserved as a DIFF (-) input.
       ai_type = self.entry_widgets.get(f"ai_type{row_idx}")
       if ai_type:
           ai_type.setEnabled(row_idx not in getattr(self, "_diff_locked_rows", set()))
       ter = self.entry_widgets.get(f"ter_selection_var{row_idx}")
       if ter:
           ter.setEnabled(enabled)

   def apply_transducer_to_row(self, row_idx, transducer_name):
       """Copy transducer settings into an Input row, then lock/unlock fields."""
       if not transducer_name:
           # No transducer: the channel's own supported range applies again.
           self.set_row_field_state(row_idx, True)
           self._fill_ai_range_from_card(row_idx, force=True)
           return
       tdict = self.get_transducer_dict()
       if transducer_name not in tdict:
           self.set_row_field_state(row_idx, True)
           return
       vals = tdict[transducer_name]

       def _set_le(key, val):
           w = self.entry_widgets.get(key)
           if w:
               w.setEnabled(True)
               w.setText("" if val is None else str(val))
               w.setEnabled(False)

       # The transducer states its output range in its own unit (often mA); the
       # row shows the channel's unit (A for a current input), so convert here —
       # the one place a unit change happens.
       #
       # When the two are not directly convertible the transducer's span is left
       # out and the card range stands. That is the thermocouple case: the sensor
       # emits mV but the channel is configured in degC, so there is no honest
       # arithmetic between them and the temperature span has to come from the
       # Engineering Units side instead.
       row_unit = self._row_unit(row_idx, 'ai')
       t_unit   = (vals.get("Output Unit") or "V").strip()
       conv_min = _convert_unit(vals.get("Min Output Range", ""), t_unit, row_unit)
       conv_max = _convert_unit(vals.get("Max Output Range", ""), t_unit, row_unit)

       if conv_min is None or conv_max is None:
           self._fill_ai_range_from_card(row_idx, force=True)
       else:
           _set_le(f"min_entry{row_idx}", _fmt_num(conv_min))
           _set_le(f"max_entry{row_idx}", _fmt_num(conv_max))
       _set_le(f"mx_plus_entry{row_idx}",     vals.get("Mx", ""))
       _set_le(f"b_entry{row_idx}",           vals.get("B", ""))
       _set_le(f"units_entry{row_idx}",       vals.get("Scaled Units", ""))

       ter = self.entry_widgets.get(f"ter_selection_var{row_idx}")
       if ter:
           ter.setEnabled(True)
           ter.setCurrentText(vals.get("Terminal Configuration", self.Default_terminal))
           ter.setEnabled(False)

   # ------------------------------------------------------------------
   # Tab / DAQ change signals
   # ------------------------------------------------------------------

   def _on_tab_changed(self, idx):
       if self.tabs.tabText(idx) == "Input Channels":
           self.refresh_transducer_dropdowns()

   # ------------------------------------------------------------------
   # Full channel rebuild
   # ------------------------------------------------------------------

   def update_channels(self, *_, settings_path=None):
       self.entry_widgets.clear()
       self.check_vars.clear()

       # Drop cached hardware answers so a module plugged in since the last
       # build is queried fresh.
       self._caps_cache = {}
       self._diff_pairing_cache = {}

       for tab in (self.tab_ai, self.tab_ao, self.tab_custom, self.tab_digital,
                   self.tab_trans, self.tab_expert, self.tab_keymap):
           _clear_widget(tab)

       # Gather channels from EVERY connected DAQ / module. Each entry is a
       # (device_name, short_channel) pair so the same 'ai0' on two modules
       # stays distinct.
       ai_pairs  = self._get_all_ai_channels()
       ao_pairs  = self._get_all_ao_channels()
       dig_pairs = self._get_all_digital_lines()

       # chan_names is the unique key list used by custom channels /
       # transducers; qualify it as "device/channel" so it is unambiguous
       # across multiple DAQs.
       self.chan_names = [f"{dev}/{ch}" for dev, ch in ai_pairs]

       # Lookups so load-from-file can match a saved (DAQ, Channel) back to
       # the right UI row regardless of enumeration order.
       self._ai_row_lookup  = {(dev, ch): i for i, (dev, ch) in enumerate(ai_pairs,  start=1)}
       self._ao_row_lookup  = {(dev, ch): i for i, (dev, ch) in enumerate(ao_pairs,  start=1)}
       self._dig_row_lookup = {(dev, ch): i for i, (dev, ch) in enumerate(dig_pairs, start=1)}

       self._build_ai_tab(ai_pairs)
       self._build_ao_tab(ao_pairs)
       self._build_custom_tab()
       self._build_digital_tab(dig_pairs)
       self._build_trans_tab()
       self._build_expert_tab()
       self._build_keymap_tab()

       self.read_defaults_from_excel(settings_path or _get_settings_path())
       self.refresh_transducer_dropdowns()
       # Snapshot hw names so _on_hw_editing_finished knows what changed
       self._sync_hw_prev_names()

       # Apply any transducer that was loaded from file
       for i in range(1, len(self.chan_names) + 1):
           cb = self.entry_widgets.get(f"transducer_sel{i}")
           if cb and cb.currentText():
               self.apply_transducer_to_row(i, cb.currentText())

       # Loaded settings may have put a channel into DIFF — reserve its pair.
       self._refresh_diff_locks()

       # App Settings plot-line unit dropdowns and Axis Ranges rows follow the
       # configured channels.
       self._refresh_plot_line_units()
       self._refresh_axis_range_rows()
       self._refresh_app_settings_status()

   # ------------------------------------------------------------------
   # App Settings tab
   # ------------------------------------------------------------------
   _FG_XUNITS    = ["cycle", "%", "degree", "time"]
   _PLOT_COLORS  = ["red", "blue", "green", "black", "orange",
                    "purple", "cyan", "magenta", "gray"]
   _PLOT_STYLES  = ["Solid", "Dashed", "Dotted"]
   _N_WAVE       = 2
   _N_MATH       = 2
   _N_PLOT_LINES = 10
   _SINGLE_OPS   = ["=", "<", "<=", ">", ">="]
   # Axis Ranges modes, matching the main window's Axis Ranges dialog.
   # A unit only custom channels use has no card range behind it, so it gets
   # Auto/Custom only — as in the dialog.
   _RANGE_MODES         = ["Auto", "Widest Range", "Narrowest Range", "Custom"]
   _RANGE_MODES_NO_CARD = ["Auto", "Custom"]
   _DOUBLE_OPS   = ["<", "<=", ">", ">="]

   # ── App Bar: added (plugin) apps ─────────────────────────────────

   def _discover_plugin_rows(self):
       """One row per folder in the Plugins folder, merged with what was
       known before (the sheet's Enabled/Show, a loaded plugin's id/label)."""
       try:
           import DAQ_AppRegistry as _app_registry
           found = _app_registry.discover_plugins()
       except Exception as e:
           print(f"Plugin list unavailable ({e}).")
           return []
       old = {r['name']: r for r in getattr(self, '_plugin_rows', [])}
       rows = []
       for name, path, has_entry in found:
           prev = old.get(name, {})
           status, spec = _app_registry.plugin_status(name)
           rows.append({
               'name': name, 'path': path, 'has_entry': has_entry,
               'enabled': prev.get('enabled', False) and has_entry,
               'show': prev.get('show', True),
               'id': spec['id'] if status == 'loaded' else prev.get('id', ''),
               'label': spec['label'] if status == 'loaded' else prev.get('label', ''),
           })
       return rows

   def _refresh_plugin_rows(self):
       """Redraw the Plugins grid from self._plugin_rows."""
       grid = self._plugin_grid
       while grid.count():
           item = grid.takeAt(0)
           w = item.widget()
           if w is not None:
               w.deleteLater()

       if not self._plugin_rows:
           grid.addWidget(QLabel("No plugins found. Put each app in its own "
                                 "folder here, e.g. Plugins\\MyApp\\MyApp.py."), 0, 0)
           return

       import DAQ_AppRegistry as _app_registry
       at_start = _app_registry.enabled_at_start()
       for c, h in enumerate(["Plugin", "Enable", "Show on Apps bar", "Status"]):
           grid.addWidget(QLabel(h), 0, c)
       for i, row in enumerate(self._plugin_rows):
           r = i + 1
           status, detail = _app_registry.plugin_status(row['name'])
           name = QLabel(row['label'] or row['name'])
           name.setToolTip(row['path'])
           grid.addWidget(name, r, 0)

           en = QCheckBox()
           en.setChecked(row['enabled'])
           en.setEnabled(row['has_entry'])
           grid.addWidget(en, r, 1)

           show = QCheckBox()
           show.setChecked(bool(row['show']))
           show.setEnabled(row['enabled'])
           show.toggled.connect(lambda on, row=row: row.__setitem__('show', on))
           grid.addWidget(show, r, 2)

           st = QLabel()
           if not row['has_entry']:
               st.setText(f"Cannot load: no {row['name']}.py in the folder")
               st.setStyleSheet("color: #c0392b;")
           elif row['enabled'] and row['name'] not in at_start:
               st.setText("Loads after restart")
           elif not row['enabled'] and row['name'] in at_start:
               st.setText("Unloads after restart")
           elif status == 'loaded':
               st.setText("Loaded")
           elif status == 'failed':
               st.setText("Failed to load")
               st.setStyleSheet("color: #c0392b;")
               st.setToolTip(str(detail).strip())
           else:
               st.setText("Not loaded")
           grid.addWidget(st, r, 3)

           def _on_enable(on, row=row, show=show):
               row['enabled'] = on
               show.setEnabled(on)
               self._refresh_plugin_rows()     # status text follows
           en.toggled.connect(_on_enable)
       grid.setColumnStretch(3, 1)

   def _open_plugins_folder(self):
       try:
           import DAQ_AppRegistry as _app_registry
           folder = _app_registry.plugins_dir()
           os.makedirs(folder, exist_ok=True)
           os.startfile(folder)
       except Exception as e:
           QMessageBox.warning(self, "Plugins Folder", f"Could not open the folder:\n{e}")

   def _refresh_shortcut_rows(self):
       """Redraw the Shortcuts grid from self._shortcut_rows."""
       grid = self._shortcut_grid
       while grid.count():
           item = grid.takeAt(0)
           w = item.widget()
           if w is not None:
               w.deleteLater()

       if not self._shortcut_rows:
           grid.addWidget(QLabel("No shortcuts added."), 0, 0)
           return

       for c, h in enumerate(["Button name", "Show on Apps bar", "Program", ""]):
           grid.addWidget(QLabel(h), 0, c)
       for i, row in enumerate(self._shortcut_rows):
           r = i + 1
           name = QLineEdit(row['name'])
           name.setMaximumWidth(180)
           name.textChanged.connect(lambda t, row=row: row.__setitem__('name', t))
           grid.addWidget(name, r, 0)

           chk = QCheckBox()
           chk.setChecked(bool(row['show']))
           chk.toggled.connect(lambda on, row=row: row.__setitem__('show', on))
           grid.addWidget(chk, r, 1)

           path_lbl = QLabel(row['path'])
           path_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
           if os.path.isfile(row['path']):
               path_lbl.setToolTip(row['path'])
           else:
               path_lbl.setText(row['path'] + "  (not found)")
               path_lbl.setStyleSheet("color: #c0392b;")
               path_lbl.setToolTip("This program was not found. The button "
                                   "will say so when clicked.")
           grid.addWidget(path_lbl, r, 2)

           rm = QPushButton("Remove")
           rm.clicked.connect(lambda _=False, row=row: self._on_remove_shortcut(row))
           grid.addWidget(rm, r, 3)
       grid.setColumnStretch(2, 1)

   def _on_add_shortcut(self):
       start = os.path.dirname(self._shortcut_rows[-1]['path']) if self._shortcut_rows else ""
       path, _ = QFileDialog.getOpenFileName(
           self, "Add Shortcut — choose a program", start,
           "Programs (*.exe);;All files (*)")
       if not path:
           return
       path = os.path.normpath(path)
       try:
           import DAQ_AppRegistry as _app_registry
           key = _app_registry.plugin_key(path)
           if any(_app_registry.plugin_key(r['path']) == key for r in self._shortcut_rows):
               QMessageBox.information(self, "Add Shortcut",
                                       "There is already a shortcut to that program.")
               return
       except Exception:
           pass
       name = os.path.splitext(os.path.basename(path))[0]
       self._shortcut_rows.append({'name': name, 'path': path, 'show': True})
       self._refresh_shortcut_rows()

   def _on_remove_shortcut(self, row):
       self._shortcut_rows = [r for r in self._shortcut_rows if r is not row]
       self._refresh_shortcut_rows()

   def _shortcut_errors(self):
       """Save-time checks for the Shortcuts list."""
       errors = []
       for r in self._shortcut_rows:
           if not r['name'].strip():
               errors.append(f"• Shortcuts, {os.path.basename(r['path'])}: "
                             f"a button name is required.")
       return errors

   def _plugin_set_changed(self):
       """True if the enabled plugins differ from those enabled at startup."""
       try:
           import DAQ_AppRegistry as _app_registry
       except Exception:
           return False
       now = {r['name'] for r in self._plugin_rows if r['enabled']}
       return now != _app_registry.enabled_at_start()

   def _build_app_settings_tab(self):
       sub = QTabWidget()

       # ---- Sub-tab 1: App Bar ----
       ab_tab = QWidget()
       ab_outer = QVBoxLayout(ab_tab)
       ab_hint = QLabel("Choose which apps appear on the Apps bar of the main "
                        "window. Unticked apps are hidden from the bar. Changes "
                        "take effect after saving. Plugins are the folders in "
                        "the Plugins folder; only the ones ticked Enable are "
                        "loaded, from the next start. Shortcuts start an "
                        "outside program and take effect after saving.")
       ab_hint.setWordWrap(True)
       ab_outer.addWidget(ab_hint)

       # Built-in apps: bundled into the executable, listed by the registry.
       # Built here rather than at class definition, so importing this module
       # does not import every app (and every plugin folder) as a side effect.
       try:
           import DAQ_AppRegistry as _app_registry
           self._app_bar_builtin = _app_registry.builtin_app_bar_items()
       except Exception as e:
           print(f"App bar list unavailable ({e}); Apps section will be empty.")
           self._app_bar_builtin = []

       ab_grp = QGroupBox("Built-in apps")
       ag = QGridLayout(ab_grp)
       ag.addWidget(QLabel("App"),          0, 0)
       ag.addWidget(QLabel("Show on Apps bar"), 0, 1)
       self._app_bar_checks = {}
       for i, (key, label, default_on) in enumerate(self._app_bar_builtin):
           r = i + 1
           ag.addWidget(QLabel(label), r, 0)
           chk = QCheckBox()
           chk.setChecked(default_on)
           ag.addWidget(chk, r, 1)
           self._app_bar_checks[key] = chk
       ag.setColumnStretch(0, 1)
       ab_outer.addWidget(ab_grp)

       # Plugins: every folder in the Plugins folder is listed; only the
       # ones ticked Enable are loaded (at the next start). Listing runs no
       # plugin code.
       pl_grp = QGroupBox("Plugins")
       pl_outer = QVBoxLayout(pl_grp)
       pl_holder = QWidget()
       self._plugin_grid = QGridLayout(pl_holder)
       self._plugin_grid.setContentsMargins(0, 0, 0, 0)
       pl_outer.addWidget(pl_holder)
       folder_row = QHBoxLayout()
       try:
           import DAQ_AppRegistry as _app_registry
           folder_lbl = QLabel(_app_registry.plugins_dir())
       except Exception:
           folder_lbl = QLabel("")
       folder_lbl.setStyleSheet("color: #666;")
       folder_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
       open_btn = QPushButton("Open Plugins Folder")
       open_btn.clicked.connect(self._open_plugins_folder)
       folder_row.addWidget(open_btn)
       folder_row.addWidget(folder_lbl, 1)
       pl_outer.addLayout(folder_row)
       ab_outer.addWidget(pl_grp)
       self._plugin_rows = []
       self._plugin_rows = self._discover_plugin_rows()
       self._refresh_plugin_rows()

       # Shortcuts: a named button that starts an outside program. Same
       # model-and-redraw arrangement as the added apps.
       sc_grp = QGroupBox("Shortcuts")
       sc_outer = QVBoxLayout(sc_grp)
       sc_holder = QWidget()
       self._shortcut_grid = QGridLayout(sc_holder)
       self._shortcut_grid.setContentsMargins(0, 0, 0, 0)
       sc_outer.addWidget(sc_holder)
       sc_add_row = QHBoxLayout()
       sc_add_btn = QPushButton("Add Shortcut…")
       sc_add_btn.setToolTip("Choose a program. The button name defaults to "
                             "the file name and can be edited.")
       sc_add_btn.clicked.connect(self._on_add_shortcut)
       sc_add_row.addWidget(sc_add_btn)
       sc_add_row.addStretch()
       sc_outer.addLayout(sc_add_row)
       ab_outer.addWidget(sc_grp)
       self._shortcut_rows = []    # [{'name', 'path', 'show'}]
       self._refresh_shortcut_rows()

       ab_outer.addStretch()
       sub.addTab(ab_tab, "App Bar")

       # ---- Sub-tab 2: FG Custom Functions ----
       fg_tab = QWidget()
       fg_outer = QVBoxLayout(fg_tab)

       wave_grp = QGroupBox("Custom Waveforms (point lists)")
       wg = QGridLayout(wave_grp)
       for c, h in enumerate(["Name", "X Unit", "Points  (x:y, x:y, …)", ""]):
           wg.addWidget(QLabel(h), 0, c)
       self._fg_wave_rows = []
       for i in range(self._N_WAVE):
           r = i + 1
           name  = QLineEdit()
           xunit = QComboBox(); xunit.addItems(self._FG_XUNITS)
           pts   = QLineEdit(); pts.setPlaceholderText("e.g.  0:0, 0.25:2.5, 0.5:0, 0.75:-2.5, 1:0")
           status = QLabel("")
           wg.addWidget(name, r, 0); wg.addWidget(xunit, r, 1)
           wg.addWidget(pts, r, 2); wg.addWidget(status, r, 3)
           self._fg_wave_rows.append({"name": name, "xunit": xunit,
                                      "points": pts, "status": status})
           pts.textChanged.connect(lambda _t, idx=i: self._update_fg_wave_status(idx))
       wg.setColumnStretch(2, 1)
       fg_outer.addWidget(wave_grp)

       math_grp = QGroupBox("Math Functions  (expression in t = time, f = frequency)")
       mg = QGridLayout(math_grp)
       for c, h in enumerate(["Name", "Expression"]):
           mg.addWidget(QLabel(h), 0, c)
       self._fg_math_rows = []
       grid_row = 1
       for i in range(self._N_MATH):
           name = QLineEdit()
           expr = QLineEdit(); expr.setPlaceholderText("e.g.  sin(2*pi*f*t) + 0.5*sin(6*pi*f*t)")
           mg.addWidget(name, grid_row, 0); mg.addWidget(expr, grid_row, 1)
           # Preview (rendered LaTeX) + validation status on the next line.
           preview = QLabel("")
           preview.setTextInteractionFlags(Qt.TextSelectableByMouse)
           preview.setStyleSheet("padding: 2px 0 2px 4px;")
           status = QLabel("")
           status.setWordWrap(True)
           mg.addWidget(preview, grid_row + 1, 1)
           mg.addWidget(status,  grid_row + 2, 1)
           self._fg_math_rows.append({"name": name, "expr": expr,
                                      "preview": preview, "status": status})
           expr.textChanged.connect(lambda _t, idx=i: self._update_fg_math_preview(idx))
           grid_row += 3
       mg.setColumnStretch(1, 1)
       fg_outer.addWidget(math_grp)
       fg_outer.addStretch()
       sub.addTab(fg_tab, "FG Custom Functions")

       # ---- Sub-tab 3: Plot Lines ----
       pl_tab = QWidget()
       pl_outer = QVBoxLayout(pl_tab)
       hint = QLabel("Predefined reference lines for the plot. The Unit list is "
                     "drawn from the units of your selected channels.")
       hint.setWordWrap(True)
       pl_outer.addWidget(hint)

       pl_inner = QWidget()
       pg = QGridLayout(pl_inner)
       for c, h in enumerate(["On", "Label", "Axis", "Limit", "Op", "Value",
                              "Op", "Value", "Unit", "Color", "Style"]):
           pg.addWidget(QLabel(h), 0, c)
       self._plot_line_rows = []
       for i in range(self._N_PLOT_LINES):
           r = i + 1
           en     = QCheckBox()
           label  = QLineEdit()
           axis   = QComboBox(); axis.addItems(["Y", "X"])
           limit  = QComboBox(); limit.addItems(["Single", "Double"]); limit.setFixedWidth(78)
           op     = QComboBox(); op.addItems(self._SINGLE_OPS); op.setFixedWidth(56)
           value  = QLineEdit(); value.setMaximumWidth(90)
           op2    = QComboBox(); op2.addItems(self._DOUBLE_OPS); op2.setFixedWidth(56)
           value2 = QLineEdit(); value2.setMaximumWidth(90)
           unit   = QComboBox(); unit.setEditable(True); unit.setMinimumWidth(90)
           color  = QComboBox(); color.addItems(self._PLOT_COLORS)
           style  = QComboBox(); style.addItems(self._PLOT_STYLES)
           pg.addWidget(en, r, 0); pg.addWidget(label, r, 1); pg.addWidget(axis, r, 2)
           pg.addWidget(limit, r, 3); pg.addWidget(op, r, 4); pg.addWidget(value, r, 5)
           pg.addWidget(op2, r, 6); pg.addWidget(value2, r, 7)
           pg.addWidget(unit, r, 8); pg.addWidget(color, r, 9); pg.addWidget(style, r, 10)
           row = {"enable": en, "label": label, "axis": axis, "limit": limit,
                  "op": op, "value": value, "op2": op2, "value2": value2,
                  "unit": unit, "color": color, "style": style}
           self._plot_line_rows.append(row)
           limit.currentTextChanged.connect(
               lambda _t, rr=row: self._apply_plot_limit_mode(rr))
           op.currentTextChanged.connect(
               lambda _t, rr=row: self._sync_plot_ops(rr, "op"))
           op2.currentTextChanged.connect(
               lambda _t, rr=row: self._sync_plot_ops(rr, "op2"))
           axis.currentTextChanged.connect(
               lambda _t, rr=row: self._refresh_row_units(rr))
           self._apply_plot_limit_mode(row)
       pg.setColumnStretch(1, 1)
       pl_outer.addWidget(_scroll_wrap(pl_inner))
       sub.addTab(pl_tab, "Plot Lines")

       # ---- Sub-tab 4: Axis Ranges ----
       ar_tab = QWidget()
       ar_outer = QVBoxLayout(ar_tab)
       ar_hint = QLabel(
           "Default Y-axis range for each unit of the selected channels. The "
           "main window's Axis Ranges dialog starts from these. Widest and "
           "Narrowest Range are worked out there from the channels' scaled card "
           "ranges. A unit used only by custom channels can be Auto or Custom.")
       ar_hint.setWordWrap(True)
       ar_outer.addWidget(ar_hint)
       ar_inner = QWidget()
       self._axis_range_grid = QGridLayout(ar_inner)
       ar_outer.addWidget(_scroll_wrap(ar_inner))
       sub.addTab(ar_tab, "Axis Ranges")
       # Rows are one per unit, rebuilt as the channels change. Choices are
       # kept by unit in _axis_range_values, so a unit that drops out of the
       # selection and comes back keeps its choice within this session.
       self._axis_range_rows = {}
       self._axis_range_values = {}
       sub.currentChanged.connect(
           lambda i: self._refresh_axis_range_rows() if sub.widget(i) is ar_tab else None)

       outer = QVBoxLayout(self.tab_app)
       outer.addWidget(sub)
       self._refresh_plot_line_units()
       self._refresh_axis_range_rows()

   def _refresh_plot_line_units(self):
       """Repopulate every plot-line Unit dropdown for its current axis."""
       if not hasattr(self, "_plot_line_rows"):
           return
       for row in self._plot_line_rows:
           self._refresh_row_units(row)

   # ---- Axis Ranges helpers ----------------------------------------
   def _selected_units(self):
       """[(unit, has_input)] for the selected input and custom channels.

       `has_input` is whether a selected input channel carries the unit, i.e.
       whether there is a card range for Widest/Narrowest to come from.
       """
       units = {}
       def _add(u, is_input):
           u = (u or "").strip()
           if u:
               units[u] = units.get(u, False) or is_input
       for i in range(1, len(getattr(self, "chan_names", [])) + 1):
           chk = self.check_vars.get(f"Selected Channel{i}")
           w = self.entry_widgets.get(f"units_entry{i}")
           if chk is not None and chk.isChecked() and w is not None:
               _add(w.text(), True)
       i = 1
       while True:
           w = self.entry_widgets.get(f"cust_units_entry{i}")
           if w is None:
               break
           chk = self.check_vars.get(f"Selected Channel_Custom{i}")
           if chk is not None and chk.isChecked():
               _add(w.text(), False)
           i += 1
       return list(units.items())

   def _stash_axis_range_rows(self):
       """Copy the shown rows into _axis_range_values."""
       for unit, row in self._axis_range_rows.items():
           self._axis_range_values[unit] = (row["mode"].currentText(),
                                            row["min"].text().strip(),
                                            row["max"].text().strip())

   def _refresh_axis_range_rows(self):
       """Rebuild the Axis Ranges rows for the units now selected."""
       if not hasattr(self, "_axis_range_grid"):
           return
       self._stash_axis_range_rows()
       grid = self._axis_range_grid
       while grid.count():
           w = grid.takeAt(0).widget()
           if w is not None:
               # Detached now, deleted later. deleteLater() alone leaves the
               # widget a visible child of the tab until control returns to
               # the event loop it was called from. Settings opened from the
               # main window runs inside a modal exec_() started from the main
               # loop, so rows built while it was being set up stayed alive
               # behind the real ones for as long as it was open, at Qt's
               # default 640x480 size.
               w.setParent(None)
               w.deleteLater()
       self._axis_range_rows = {}
       for c, h in enumerate(["Unit", "Range", "Min", "Max"]):
           grid.addWidget(QLabel(h), 0, c)
       units = self._selected_units()
       if not units:
           grid.addWidget(QLabel("No channels with units are selected."), 1, 0, 1, 4)
       for r, (unit, has_input) in enumerate(units, start=1):
           modes = self._RANGE_MODES if has_input else self._RANGE_MODES_NO_CARD
           mode = QComboBox()
           mode.addItems(modes)
           lo, hi = QLineEdit(), QLineEdit()
           for edit in (lo, hi):
               v = QDoubleValidator(edit)
               v.setLocale(QLocale.c())        # '.' decimal, whatever the OS locale
               edit.setValidator(v)
               edit.setMaximumWidth(100)
           default = "Widest Range" if has_input else "Auto"
           saved_mode, saved_lo, saved_hi = self._axis_range_values.get(unit, (default, "", ""))
           mode.setCurrentText(saved_mode if saved_mode in modes else default)
           row = {"mode": mode, "min": lo, "max": hi}
           if mode.currentText() == "Custom":
               lo.setText(saved_lo)
               hi.setText(saved_hi)
           self._apply_axis_range_mode(row)
           mode.currentTextChanged.connect(
               lambda _t, rr=row: self._on_axis_range_mode(rr))
           grid.addWidget(QLabel(unit), r, 0)
           grid.addWidget(mode, r, 1)
           grid.addWidget(lo, r, 2)
           grid.addWidget(hi, r, 3)
           self._axis_range_rows[unit] = row
       grid.setColumnStretch(4, 1)
       grid.setRowStretch(len(units) + 2, 1)

   def _on_axis_range_mode(self, rr):
       # Switching to Custom starts blank, as in the main window's dialog.
       rr["min"].clear()
       rr["max"].clear()
       self._apply_axis_range_mode(rr)

   @staticmethod
   def _apply_axis_range_mode(rr):
       """Min/Max are for Custom only; other modes leave them blank."""
       custom = rr["mode"].currentText() == "Custom"
       for edit in (rr["min"], rr["max"]):
           edit.setEnabled(custom)
           if not custom:
               edit.clear()

   def _axis_range_errors(self):
       errors = []
       for unit, row in self._axis_range_rows.items():
           if row["mode"].currentText() != "Custom":
               continue
           try:
               lo, hi = float(row["min"].text()), float(row["max"].text())
           except ValueError:
               errors.append(f"• Axis Ranges, {unit}: Custom needs both a Min and a Max.")
               continue
           if lo >= hi:
               errors.append(f"• Axis Ranges, {unit}: Min must be less than Max.")
       return errors

   def _channel_units(self):
       """Units currently set on the input and custom channels."""
       units, seen = [], set()
       def _add(u):
           u = (u or "").strip()
           if u and u not in seen:
               seen.add(u); units.append(u)
       for i in range(1, len(getattr(self, "chan_names", [])) + 1):
           w = self.entry_widgets.get(f"units_entry{i}")
           if w is not None:
               _add(w.text())
       i = 1
       while True:
           w = self.entry_widgets.get(f"cust_units_entry{i}")
           if w is None:
               break
           _add(w.text()); i += 1
       return units

   def _refresh_row_units(self, rr):
       """Populate one row's Unit dropdown for its axis. A Y line uses the
       channel units; an X line additionally offers the X-axis units —
       'samples', 's' (Run Duration) and 'V' (AO) — which must match exactly
       what the live X axis reports for the line to draw. The combo stays
       editable, so any saved value is preserved."""
       combo = rr["unit"]
       cur = combo.currentText()
       if rr["axis"].currentText() == "X":
           units = ["samples", "s", "V"]
           for u in self._channel_units():
               if u not in units:
                   units.append(u)
       else:
           units = self._channel_units()
       combo.blockSignals(True)
       combo.clear()
       combo.addItems(units)
       combo.setCurrentText(cur)   # editable -> preserves prior/saved value
       combo.blockSignals(False)

   # ---- Plot-line limit helpers -------------------------------------
   @staticmethod
   def _flip_op(op):
       return {"<": ">", "<=": ">=", ">": "<", ">=": "<="}.get(op, op)

   def _apply_plot_limit_mode(self, rr):
       """Show/hide the 2nd operator+value and set the allowed operators to
       match a row's Limit (Single/Double). Single uses one operator; Double keeps
       the two operators pointing in opposite directions."""
       is_double = (rr["limit"].currentText() == "Double")
       op, op2, val2 = rr["op"], rr["op2"], rr["value2"]
       op2.setVisible(is_double)
       val2.setVisible(is_double)
       cur = op.currentText()
       op.blockSignals(True)
       op.clear()
       op.addItems(self._DOUBLE_OPS if is_double else self._SINGLE_OPS)
       if is_double:
           op.setCurrentText(cur if cur in self._DOUBLE_OPS else "<=")
       else:
           op.setCurrentText(cur if cur in self._SINGLE_OPS else "=")
       op.blockSignals(False)
       if is_double:
           op2.blockSignals(True)
           op2.setCurrentText(self._flip_op(op.currentText()))
           op2.blockSignals(False)

   def _sync_plot_ops(self, rr, which):
       """Keep the two double-limit operators complementary: changing one
       flips the other (< <-> >, <= <-> >=). No effect in single-limit mode.
       """
       if rr["limit"].currentText() != "Double":
           return
       src, dst = ((rr["op"], rr["op2"]) if which == "op"
                   else (rr["op2"], rr["op"]))
       want = self._flip_op(src.currentText())
       if dst.currentText() != want:
           dst.blockSignals(True)
           dst.setCurrentText(want)
           dst.blockSignals(False)

   def _update_fg_math_preview(self, idx):
       """Render the math expression as LaTeX and show a validity/range status."""
       if not hasattr(self, "_fg_math_rows") or idx >= len(self._fg_math_rows):
           return
       row = self._fg_math_rows[idx]
       text = row["expr"].text().strip()
       preview, status = row["preview"], row["status"]

       if not text:
           preview.clear(); preview.setText("")
           status.setText("")
           return

       err, ymin, ymax = _validate_fg_math_expr(text)

       # LaTeX preview (nice 'pi'); fall back to raw text if matplotlib missing.
       latex = _expr_to_latex(text)
       if latex:
           latex = re.sub(r'\bpi\b', r'\\pi', latex)
           latex = re.sub(r'\btau\b', r'\\tau', latex)
           pix = self._render_latex_pixmap(latex)
           if pix is not None:
               preview.setPixmap(pix)
           else:
               preview.setText(f"<code>{latex}</code>")
       else:
           preview.setText("<i>preview unavailable (check syntax)</i>")

       if err:
           status.setText(f"<span style='color:#c0392b;'>✗ {err}</span>")
       else:
           rng = ""
           if ymin is not None and ymax is not None:
               rng = (f" · approx output range at f=1: "
                      f"[{ymin:.3g}, {ymax:.3g}] (clipped to each AO channel's Min/Max)")
           status.setText(f"<span style='color:#1e8449;'>✓ valid</span>{rng}")

   def _update_fg_wave_status(self, idx):
       """Validate a waveform point list and show point count or an error."""
       if not hasattr(self, "_fg_wave_rows") or idx >= len(self._fg_wave_rows):
           return
       row = self._fg_wave_rows[idx]
       text = row["points"].text().strip()
       status = row["status"]
       if not text:
           status.setText("")
           return
       pts, err = _parse_points(text)
       if err:
           status.setText(f"<span style='color:#c0392b;'>✗ {err}</span>")
       else:
           ys = [y for _, y in pts]
           status.setText(f"<span style='color:#1e8449;'>✓ {len(pts)} pts</span> "
                          f"· y∈[{min(ys):.3g}, {max(ys):.3g}]")

   def _refresh_app_settings_status(self):
       """Refresh all FG preview/status labels (after a load)."""
       for i in range(len(getattr(self, "_fg_math_rows", []))):
           self._update_fg_math_preview(i)
       for i in range(len(getattr(self, "_fg_wave_rows", []))):
           self._update_fg_wave_status(i)

   def _invalid_app_functions(self):
       """Return a list of human-readable problems with non-empty FG functions,
       used to warn before saving."""
       problems = []
       for i, row in enumerate(getattr(self, "_fg_math_rows", [])):
           expr = row["expr"].text().strip()
           if not expr:
               continue
           err, _mn, _mx = _validate_fg_math_expr(expr)
           if err:
               nm = row["name"].text().strip() or f"Math function {i+1}"
               problems.append(f"{nm}: {err}")
       for i, row in enumerate(getattr(self, "_fg_wave_rows", [])):
           text = row["points"].text().strip()
           if not text:
               continue
           _pts, err = _parse_points(text)
           if err:
               nm = row["name"].text().strip() or f"Custom waveform {i+1}"
               problems.append(f"{nm}: {err}")
       return problems

   # ------------------------------------------------------------------
   # Tab builders
   # ------------------------------------------------------------------

   def _build_ai_tab(self, ai_channels):
       # Min and Max are always in the same unit (from the channel's Signal
       # Type), so it is shown once, after Max.
       labels = [
           "Channel", "DAQ", "Input Type", "Transducer",
           "Min", "Max", "Unit",
           "Terminal Configuration", "Mx", "b", "Scaled Units",
           "Device Name", "Selected",
       ]

       # Resolve every channel's input type first. When no channel offers a
       # choice — the normal case, since a card's front end does one thing — the
       # column is pure repetition, so it is left out entirely. The widgets are
       # still created and still drive the units and the saved value; they are
       # simply never added to the grid, and an empty grid column collapses to
       # zero width on its own.
       ai_types = {r: self._chan_types(dev, ch, "ai")
                   for r, (dev, ch) in enumerate(ai_channels, start=1)}
       self._show_ai_type_col = any(len(t) > 1 for t in ai_types.values())

       container = QWidget()
       grid = QGridLayout(container)

       for col, lab in enumerate(labels):
           if lab == "Input Type" and not self._show_ai_type_col:
               continue
           lbl = QLabel(lab)
           lbl.setWordWrap(True)
           grid.addWidget(lbl, 0, col)

       # Reset the pin-pairing maps for the rows about to be created.
       self._ai_row_info = {}
       self._ai_dev_rows = {}
       self._diff_locked_rows = set()
       for row, (dev, ch) in enumerate(ai_channels, start=1):
           idx = self._chan_index(ch)
           self._ai_row_info[row] = (dev, idx)
           if idx is not None:
               self._ai_dev_rows.setdefault(dev, {})[idx] = row

       for row, (dev, ch) in enumerate(ai_channels, start=1):
           r = row

           # Channel (read-only)
           le = QLineEdit(ch)
           le.setEnabled(False)
           le.setMaximumWidth(70)
           self.entry_widgets[f"Channel_entry{r}"] = le
           grid.addWidget(le, r, 0)

           # DAQ (read-only — which device/module this channel lives on)
           le_daq = QLineEdit(dev)
           le_daq.setEnabled(False)
           le_daq.setMinimumWidth(110)
           self.entry_widgets[f"DAQ_entry{r}"] = le_daq
           grid.addWidget(le_daq, r, 1)

           # Input Type — what kind of signal this channel accepts, read from the
           # card (NI-DAQmx ai_meas_types), so it is a label rather than a choice
           # unless the module genuinely supports several.
           type_w = self._make_type_widget(
               ai_types[r],
               "Input type this channel accepts (NI-DAQmx ai_meas_types).")
           self.entry_widgets[f"ai_type{r}"] = type_w
           if self._show_ai_type_col:
               grid.addWidget(type_w, r, 2)
           if isinstance(type_w, QComboBox):
               type_w.currentIndexChanged.connect(
                   lambda _idx, row_=r: self._on_ai_type_changed(row_)
               )

           # Transducer combobox
           trans_cb = QComboBox()
           trans_cb.addItem("")
           trans_cb.setMinimumWidth(130)
           self.entry_widgets[f"transducer_sel{r}"] = trans_cb
           grid.addWidget(trans_cb, r, 3)

           # Use default-argument capture to avoid closure-over-loop-variable bugs
           def _make_handler(row_idx):
               def _on_change(idx, ri=row_idx):
                   cb = self.entry_widgets.get(f"transducer_sel{ri}")
                   if cb:
                       self.apply_transducer_to_row(ri, cb.currentText())
               return _on_change

           trans_cb.currentIndexChanged.connect(_make_handler(r))

           # Min, Max and their shared unit
           le_min = QLineEdit()
           le_min.setMaximumWidth(60)
           self.entry_widgets[f"min_entry{r}"] = le_min
           grid.addWidget(le_min, r, 4)

           le_max = QLineEdit()
           le_max.setMaximumWidth(60)
           self.entry_widgets[f"max_entry{r}"] = le_max
           grid.addWidget(le_max, r, 5)

           lbl_u = QLabel("")
           lbl_u.setMinimumWidth(34)
           self.entry_widgets[f"unit_lbl{r}"] = lbl_u
           grid.addWidget(lbl_u, r, 6)

           # Failsafe on entry, so an out-of-range value never survives typing.
           le_min.editingFinished.connect(
               lambda row_=r: self._clamp_to_card(row_, "min", "ai"))
           le_max.editingFinished.connect(
               lambda row_=r: self._clamp_to_card(row_, "max", "ai"))

           self._refresh_row_unit(r, 'ai')

           # Terminal Configuration — full list when unselected, DAQ-filtered when
           # selected. DIFF is offered only on pins that can be a positive input.
           cb_ter = QComboBox()
           cb_ter.addItems(self._terminal_options_for_row(r, self.Terminal_options_list))
           cb_ter.setCurrentText(self.Default_terminal)
           if not self._row_can_be_diff(r):
               partner = self._ai_dev_rows.get(dev, {}).get(
                   (self._chan_index(ch) or 0) - self._ai_pair_offset(dev))
               owner = self._ai_row_label(partner) if partner else "the paired channel"
               cb_ter.setToolTip(
                   f"DIFF is not available on this pin — it is the negative "
                   f"(\u2212) half of the pair.\nSelect DIFF on {owner} instead."
               )
           cb_ter.currentIndexChanged.connect(
               lambda _idx, row_=r: self._refresh_diff_locks()
           )
           self.entry_widgets[f"ter_selection_var{r}"] = cb_ter
           grid.addWidget(cb_ter, r, 7)

           # Mx
           le_mx = QLineEdit()
           le_mx.setMaximumWidth(55)
           self.entry_widgets[f"mx_plus_entry{r}"] = le_mx
           grid.addWidget(le_mx, r, 8)

           # b
           le_b = QLineEdit()
           le_b.setMaximumWidth(55)
           self.entry_widgets[f"b_entry{r}"] = le_b
           grid.addWidget(le_b, r, 9)

           # Scaled Units — the unit after Mx + b is applied, free text
           le_units = QLineEdit()
           _wire_unit_field(le_units)
           self.entry_widgets[f"units_entry{r}"] = le_units
           grid.addWidget(le_units, r, 10)

           # Device Name
           le_name = QLineEdit()
           le_name.setMinimumWidth(220)
           self.entry_widgets[f"Device_name{r}"] = le_name
           grid.addWidget(le_name, r, 11)

           # Selected checkbox
           chk = QCheckBox()
           self.check_vars[f"Selected Channel{r}"] = chk
           chk.stateChanged.connect(self._update_custom_dropdowns)
           chk.stateChanged.connect(
               lambda state, row_=r: self._on_ai_channel_selected(row_, state)
           )
           chk.stateChanged.connect(self._refresh_diff_locks)
           self.entry_widgets[f"Selected Channel{r}"] = chk
           grid.addWidget(chk, r, 12)

       # Push all rows to the top; empty space grows at the bottom
       grid.setRowStretch(len(ai_channels) + 1, 1)
       # Let the Device Name column (11) absorb spare width; keep others tight.
       grid.setColumnStretch(11, 1)

       outer = QVBoxLayout(self.tab_ai)
       outer.addWidget(_scroll_wrap(container))

       # Apply any pairing implied by the freshly created rows.
       self._refresh_diff_locks()

   def _build_ao_tab(self, ao_channels):
       labels = ["Channel", "DAQ", "Min", "Max", "Unit",
                 "Output Type", "Device Name", "Selected"]

       # Same rule as the Input tab: hide the type column unless some channel
       # actually offers a choice.
       ao_output_types = {(dev, ch): self._chan_types(dev, ch, "ao")
                          for dev, ch in ao_channels}
       self._show_ao_type_col = any(len(t) > 1 for t in ao_output_types.values())

       container = QWidget()
       grid = QGridLayout(container)

       for col, lab in enumerate(labels):
           if lab == "Output Type" and not self._show_ao_type_col:
               continue
           grid.addWidget(QLabel(lab), 0, col)

       for row, (dev, ch) in enumerate(ao_channels, start=1):
           le = QLineEdit(ch)
           le.setEnabled(False)
           self.entry_widgets[f"Channel_entry_output{row}"] = le
           grid.addWidget(le, row, 0)

           # DAQ (read-only)
           le_daq = QLineEdit(dev)
           le_daq.setEnabled(False)
           le_daq.setMinimumWidth(110)
           self.entry_widgets[f"DAQ_entry_output{row}"] = le_daq
           grid.addWidget(le_daq, row, 1)

           le_min = QLineEdit()
           le_min.setMaximumWidth(70)
           self.entry_widgets[f"min_entry_output{row}"] = le_min
           grid.addWidget(le_min, row, 2)

           le_max = QLineEdit()
           le_max.setMaximumWidth(70)
           self.entry_widgets[f"max_entry_output{row}"] = le_max
           grid.addWidget(le_max, row, 3)

           # Min and Max share one unit, shown once after Max.
           lbl_u = QLabel("")
           lbl_u.setMinimumWidth(34)
           self.entry_widgets[f"unit_lbl_output{row}"] = lbl_u
           grid.addWidget(lbl_u, row, 4)

           le_min.editingFinished.connect(
               lambda r=row: self._clamp_to_card(r, "min", "ao"))
           le_max.editingFinished.connect(
               lambda r=row: self._clamp_to_card(r, "max", "ao"))

           # Output type — drives the Min/Max unit (Current -> A) whether or not
           # the column is shown.
           type_list = ao_output_types.get((dev, ch)) or [_DEFAULT_TYPE]
           w_type = self._make_type_widget(
               type_list, "Output type reported by this channel "
                          "(NI-DAQmx ao_output_types).")
           self.entry_widgets[f"output_type{row}"] = w_type
           if self._show_ao_type_col:
               grid.addWidget(w_type, row, 5)
           if isinstance(w_type, QComboBox):
               w_type.currentIndexChanged.connect(
                   lambda _idx, r=row: self._refresh_row_unit(r, 'ao')
               )
           self._refresh_row_unit(row, 'ao')

           le_name = QLineEdit()
           le_name.setMinimumWidth(200)
           self.entry_widgets[f"Device_name_output{row}"] = le_name
           grid.addWidget(le_name, row, 6)

           chk = QCheckBox()
           self.check_vars[f"Selected Channel_output{row}"] = chk
           self.entry_widgets[f"Selected Channel_output{row}"] = chk
           chk.stateChanged.connect(
               lambda state, r=row: self._on_output_channel_selected(r, state)
           )
           grid.addWidget(chk, row, 7)

       # Push all rows to the top; empty space grows at the bottom
       grid.setRowStretch(len(ao_channels) + 1, 1)
       grid.setColumnStretch(6, 1)

       outer = QVBoxLayout(self.tab_ao)
       outer.addWidget(_scroll_wrap(container))

   def _build_custom_tab(self):
       labels = ["Channel", "A =", "B =", "Expression *",
                 "Device Name", "Units", "Selected"]

       grid = QGridLayout()
       for col, lab in enumerate(labels):
           lbl = QLabel(lab)
           lbl.setWordWrap(True)
           grid.addWidget(lbl, 0, col)

       grid.setColumnStretch(3, 2)

       for row in range(1, self.no_of_cus_chans + 1):
           le = QLineEdit(f"cu{row - 1}")
           le.setEnabled(False)
           self.entry_widgets[f"Channel_entry_cust{row}"] = le
           grid.addWidget(le, row, 0)

           # A dropdown
           first_cb = ChannelComboBox(self._channel_friendly_name)
           first_cb.addItems(self.chan_names)
           self.entry_widgets[f"first_chan_sel{row}"] = first_cb
           grid.addWidget(first_cb, row, 1)

           # B dropdown -- "None" is the default; excludes whatever A picked
           sec_cb = ChannelComboBox(self._channel_friendly_name)
           sec_cb.addItem("None")
           sec_cb.addItems(self.chan_names)
           self.entry_widgets[f"sec_chan_sel{row}"] = sec_cb
           grid.addWidget(sec_cb, row, 2)

           expr_le = QLineEdit()
           expr_le.setPlaceholderText("e.g.  A + B   or   3*A**3 + 2*A**2 + A + 5")
           self.entry_widgets[f"operation_sel{row}"] = expr_le
           grid.addWidget(expr_le, row, 3)

           name_le = QLineEdit()
           self.entry_widgets[f"custom_chan_name{row}"] = name_le
           grid.addWidget(name_le, row, 4)

           units_le = QLineEdit()
           _wire_unit_field(units_le)
           self.entry_widgets[f"cust_units_entry{row}"] = units_le
           grid.addWidget(units_le, row, 5)

           chk = QCheckBox()
           self.check_vars[f"Selected Channel_Custom{row}"] = chk
           self.entry_widgets[f"Selected Channel_Custom{row}"] = chk
           chk.stateChanged.connect(
               lambda _state, r=row: self._update_single_custom_row(r))
           grid.addWidget(chk, row, 6)

           # When A changes, rebuild B to exclude that channel
           first_cb.currentTextChanged.connect(
               lambda _text, r=row: self._sync_b_dropdown(r))

           # Update the LaTeX preview live as the expression is typed.
           expr_le.textChanged.connect(
               lambda _text, r=row: self._update_expr_preview(r))

       grid.setRowStretch(self.no_of_cus_chans + 1, 1)

       container = QWidget()
       container.setLayout(grid)

       note = QLabel(
           "<b>* Expression reference:</b>&nbsp; "
           "<b>A</b> = channel selected in the A dropdown. &nbsp;"
           "<b>B</b> = channel selected in the B dropdown "
           "(set B to <i>None</i> if not needed). &nbsp;"
           "Operators: <b>+ &nbsp; - &nbsp; * &nbsp; / &nbsp; **</b> (or <b>^</b>). &nbsp;"
           "Functions: <b>log &nbsp; log10 &nbsp; sqrt &nbsp; abs &nbsp; "
           "exp &nbsp; sin &nbsp; cos &nbsp; tan</b>. &nbsp;"
           "Only <b>A</b> and <b>B</b> may appear as variable names "
           "\u2014 anything else is rejected on save. &nbsp;"
           "Examples: &nbsp;<i>A + B</i> &nbsp;|&nbsp; "
           "<i>3*A**3 + 2*A**2 + A + 5</i> &nbsp;|&nbsp; "
           "<i>log(A)</i> &nbsp;|&nbsp; <i>1/A</i> &nbsp;|&nbsp; <i>2*A</i>"
       )
       note.setWordWrap(True)

       # ── LaTeX preview panel ──────────────────────────────────────
       preview_group = QGroupBox("Expression preview (LaTeX)")
       pv = QVBoxLayout(preview_group)
       for row in range(1, self.no_of_cus_chans + 1):
           line = QHBoxLayout()
           tag = QLabel(f"cu{row - 1} =")
           tag.setMinimumWidth(55)
           line.addWidget(tag)
           prev = QLabel()
           prev.setTextInteractionFlags(Qt.TextSelectableByMouse)
           prev.setStyleSheet("color:#1a3e72;")
           self.entry_widgets[f"expr_preview{row}"] = prev
           line.addWidget(prev, 1)
           pv.addLayout(line)

       outer = QVBoxLayout(self.tab_custom)
       outer.addWidget(container, 1)
       outer.addWidget(preview_group)
       outer.addWidget(note)

       self._update_custom_dropdowns()
       for row in range(1, self.no_of_cus_chans + 1):
           self._update_expr_preview(row)


   def _get_digital_voltage_str(self, dev_name=None):
       """Query logic-level voltage info for the selected device's digital lines.
       No hardcoded values — everything comes from the NI-DAQmx API.
         1. di_input_low_threshold / di_input_high_threshold  (programmable-threshold devices)
         2. di_logic_family / do_logic_family enum            (fixed-threshold devices, e.g. USB-6003)
         3. N/A if device cannot be queried at all
       """
       def _fmt_family(family):
           """Convert a LogicFamily enum to a readable voltage string."""
           raw = str(family).split('.')[-1].split(':')[0].strip()
           # e.g. 'THREE_POINT_THREE_V' -> '3.3V', 'FIVE_V' -> '5V',
           #      'ONE_POINT_EIGHT_V' -> '1.8V', 'TWO_POINT_FIVE_V' -> '2.5V'
           _map = {
               'ONE_POINT_EIGHT_V':   '1.8V',
               'TWO_POINT_FIVE_V':    '2.5V',
               'THREE_POINT_THREE_V': '3.3V',
               'FIVE_V':              '5.0V',
           }
           label = _map.get(raw)
           if label:
               return label
           # Unknown enum value — still show what the device returned, just cleaned up
           return raw.replace('_V', 'V').replace('_POINT_', '.').replace('_', ' ')

       try:
           import nidaqmx
           if not dev_name:
               lst = self._get_daq_list()
               dev_name = lst[0] if lst else ""
           dev = nidaqmx.system.Device(dev_name)
           di_lines = list(dev.di_lines)
           do_lines = list(dev.do_lines)
           lines = di_lines or do_lines
           if not lines:
               return "N/A"

           # ── Approach 1: programmable threshold query (DI) ─────────
           if di_lines:
               try:
                   with nidaqmx.Task() as t:
                       ch = t.di_channels.add_di_chan(di_lines[0].name)
                       low  = getattr(ch, 'di_input_low_threshold',  None)
                       high = getattr(ch, 'di_input_high_threshold', None)
                       if low is not None and high is not None:
                           return f"Low: {float(low):g}V / High: {float(high):g}V"
               except Exception:
                   pass

           # ── Approach 2: logic family enum ─────────────────────────
           # Try DI first, fall back to DO
           for task_type, ch_lines, add_fn in [
               ('di', di_lines, lambda t, ln: t.di_channels.add_di_chan(ln)),
               ('do', do_lines, lambda t, ln: t.do_channels.add_do_chan(ln)),
           ]:
               if not ch_lines:
                   continue
               try:
                   with nidaqmx.Task() as t:
                       ch = add_fn(t, ch_lines[0].name)
                       prop = f'{task_type}_logic_family'
                       family = getattr(ch, prop, None)
                       if family is not None:
                           return _fmt_family(family)
               except Exception:
                   continue

       except Exception:
           pass
       return "N/A"


   def _build_digital_tab(self, digital_lines):
       labels = ["Channel", "DAQ", "Direction", "Device Name", "Logic Level", "Selected"]

       container = QWidget()
       grid = QGridLayout(container)

       for col, lab in enumerate(labels):
           lbl = QLabel(lab)
           lbl.setWordWrap(True)
           grid.addWidget(lbl, 0, col)

       _volt_cache = {}

       for row, (dev, ch) in enumerate(digital_lines, start=1):
           # Channel (read-only)
           le = QLineEdit(ch)
           le.setEnabled(False)
           self.entry_widgets[f"dig_channel_entry{row}"] = le
           grid.addWidget(le, row, 0)

           # DAQ (read-only)
           le_daq = QLineEdit(dev)
           le_daq.setEnabled(False)
           le_daq.setMinimumWidth(110)
           self.entry_widgets[f"dig_DAQ{row}"] = le_daq
           grid.addWidget(le_daq, row, 1)

           # Direction — Input or Output
           dir_cb = QComboBox()
           dir_cb.addItems(["Input", "Output"])
           self.entry_widgets[f"dig_direction_sel{row}"] = dir_cb
           grid.addWidget(dir_cb, row, 2)

           # Device Name
           le_name = QLineEdit()
           self.entry_widgets[f"dig_device_name{row}"] = le_name
           grid.addWidget(le_name, row, 3)

           # Voltage (read-only, grayed — device-wide logic level, per device)
           if dev not in _volt_cache:
               _volt_cache[dev] = self._get_digital_voltage_str(dev)
           le_volt = QLineEdit(_volt_cache[dev])
           le_volt.setEnabled(False)
           le_volt.setReadOnly(True)
           le_volt.setStyleSheet("color: #666; background: #f0f0f0;")
           le_volt.setMaximumWidth(90)
           grid.addWidget(le_volt, row, 4)

           # Selected checkbox
           chk = QCheckBox()
           self.check_vars[f"Selected Channel_Digital{row}"] = chk
           self.entry_widgets[f"Selected Channel_Digital{row}"] = chk
           grid.addWidget(chk, row, 5)

       # Push rows to top; empty space grows at bottom
       grid.setRowStretch(len(digital_lines) + 1, 1)

       outer = QVBoxLayout(self.tab_digital)
       outer.addWidget(_scroll_wrap(container))

   def _build_trans_tab(self):
       # Output Range / Output Unit describe the electrical signal the
       # transducer sends to the card. Signal Input is the physical span it
       # measures, in Scaled Units — the two ends of what Mx + b relates.
       trans_labels = ["Hardware Model", "Hardware Type",
                       "Min Output Range", "Max Output Range", "Output Unit",
                       "Terminal Configuration", "Mx", "B", "Scaled Units",
                       "Min Signal Input", "Max Signal Input", "Unit"]

       container = QWidget()
       grid = QGridLayout(container)

       for col, lab in enumerate(trans_labels):
           lbl = QLabel(lab)
           lbl.setWordWrap(True)
           grid.addWidget(lbl, 0, col)

       for i in range(1, self.no_of_transducers + 1):
           # Hardware Model — this is the name other tabs reference, so it
           # stays the unique key for a transducer.
           hw = QLineEdit()
           hw.setMinimumWidth(200)
           self.entry_widgets[f"trans_hw{i}"] = hw
           grid.addWidget(hw, i, 0)

           # Hardware Type — free-text category, purely descriptive.
           hw_type = QLineEdit()
           hw_type.setMinimumWidth(150)
           self.entry_widgets[f"trans_type{i}"] = hw_type
           grid.addWidget(hw_type, i, 1)

           minv = QLineEdit()
           self.entry_widgets[f"trans_minv{i}"] = minv
           grid.addWidget(minv, i, 2)

           maxv = QLineEdit()
           self.entry_widgets[f"trans_maxv{i}"] = maxv
           grid.addWidget(maxv, i, 3)

           # Output Unit — what this transducer puts on the wire (V, a 4-20 mA
           # loop, a resistance, a bridge ratio). Not what it measures: a
           # pressure transducer outputs mA, and 'psi' goes in Engineering Units.
           range_unit = QComboBox()
           range_unit.addItems(_TRANSDUCER_UNITS)
           range_unit.setCurrentText("V")
           range_unit.setMaximumWidth(80)
           range_unit.setToolTip(
               "Electrical output of this transducer — the signal it presents "
               "to the card.\nOnly channels that accept this kind of signal "
               "will offer it."
           )
           self.entry_widgets[f"trans_output_unit{i}"] = range_unit
           grid.addWidget(range_unit, i, 4)

           term_cb = QComboBox()
           term_cb.addItems(self.Terminal_options_list)
           term_cb.setCurrentText(self.Default_terminal)
           self.entry_widgets[f"trans_term{i}"] = term_cb
           grid.addWidget(term_cb, i, 5)

           mx = QLineEdit()
           mx.setMaximumWidth(55)
           self.entry_widgets[f"trans_mx{i}"] = mx
           grid.addWidget(mx, i, 6)

           b_w = QLineEdit()
           b_w.setMaximumWidth(55)
           self.entry_widgets[f"trans_b{i}"] = b_w
           grid.addWidget(b_w, i, 7)

           units = QLineEdit()
           units.setMaximumWidth(90)
           units.setToolTip("Unit the reading becomes after Mx + b (psi, degC, "
                            "L/min ...).")
           # Respelt when edited; its recognised/unrecognised colour is shown
           # on the Signal Input unit labels beside it.
           _wire_unit_field(units, color_target=False)
           self.entry_widgets[f"trans_units{i}"] = units
           grid.addWidget(units, i, 8)

           # Signal Input — the physical span this transducer measures, stated
           # in Scaled Units. A 4-20 mA / 0-100 psi sensor has an output range of
           # 4 to 20 mA and a signal input of 0 to 100 psi.
           min_sig = QLineEdit()
           min_sig.setMaximumWidth(70)
           self.entry_widgets[f"trans_min_sig{i}"] = min_sig
           grid.addWidget(min_sig, i, 9)

           max_sig = QLineEdit()
           max_sig.setMaximumWidth(70)
           self.entry_widgets[f"trans_max_sig{i}"] = max_sig
           grid.addWidget(max_sig, i, 10)

           # Min and Max Signal Input share one unit (Scaled Units), shown
           # once after Max.
           sig_u = QLabel("")
           sig_u.setMinimumWidth(46)
           self.entry_widgets[f"trans_sig_unit{i}"] = sig_u
           grid.addWidget(sig_u, i, 11)

           # Keep those unit labels following whatever Scaled Units says.
           units.textChanged.connect(
               lambda _t, r=i: self._refresh_trans_scaled_unit(r))
           self._refresh_trans_scaled_unit(i)

           # Any edit in the Transducers tab immediately re-applies to input
           # rows that reference this transducer, so locked fields stay in sync.
           for le in (minv, maxv, mx, b_w, units, min_sig, max_sig):
               le.editingFinished.connect(self._reapply_all_transducers)
           # Changing the unit changes which channels can use this transducer,
           # so the dropdowns are re-filtered as well as re-applied.
           range_unit.currentIndexChanged.connect(self._on_trans_output_unit_changed)
           # hw: set initial prev_name, then single handler covers
           # duplicate-check + rename propagation + reapply
           hw.setProperty("prev_name", "")
           hw.editingFinished.connect(
               lambda checked=False, r=i: self._on_hw_editing_finished(r))
           term_cb.currentIndexChanged.connect(self._reapply_all_transducers)

       # Push all rows to the top; empty space grows at the bottom
       grid.setRowStretch(self.no_of_transducers + 1, 1)
       # Let the Model / Type columns absorb spare width; keep Mx/B tight.
       grid.setColumnStretch(0, 2)
       grid.setColumnStretch(1, 1)

       outer = QVBoxLayout(self.tab_trans)
       outer.addWidget(_scroll_wrap(container))

   def _build_expert_tab(self):
       outer = QVBoxLayout(self.tab_expert)

       # "Use default" checkbox at top (spans all sections)
       self.use_default_chk = QCheckBox("Use default")
       self.use_default_chk.setChecked(True)
       outer.addWidget(self.use_default_chk)

       sections_row = QHBoxLayout()

       # ── Sample Settings ──────────────────────────────────────────
       sample_grp = QGroupBox("Sample Settings")
       sg = QGridLayout(sample_grp)

       sg.addWidget(QLabel("AI Sample Rate (Hz)"), 0, 0)
       self.sample_rate_entry = QLineEdit(self.expert_defaults["Sample Rate (Hz)"])
       self.sample_rate_entry.setToolTip(
           "Input (AI) sample clock rate for acquisition."
       )
       sg.addWidget(self.sample_rate_entry, 0, 1)

       sg.addWidget(QLabel("AO Sample Rate (Hz)"), 1, 0)
       self.ao_sample_rate_entry = QLineEdit(self.expert_defaults["AO Sample Rate (Hz)"])
       self.ao_sample_rate_entry.setToolTip(
           "Output clock rate for the function generator.\n"
           "Only applies to hardware-timed AO devices (e.g. USB-6211, PCIe-6321).\n"
           "Ignored on on-demand AO devices such as the USB-6009 and USB-6003."
       )
       sg.addWidget(self.ao_sample_rate_entry, 1, 1)

       # Max Buffer Size is an acquisition (driver input buffer) setting, so it
       # lives with Sample Settings rather than Plot Settings.
       sg.addWidget(QLabel("Max Buffer Size"), 2, 0)
       self.max_buffer_entry = QLineEdit(self.expert_defaults["Max Buffer Size"])
       self.max_buffer_entry.setToolTip(
           "NI-DAQmx driver input buffer (samps_per_chan, continuous mode).\n"
           "Larger values give more cushion against dropped samples if the app\n"
           "stalls briefly. Does not affect plotting or recorded data."
       )
       sg.addWidget(self.max_buffer_entry, 2, 1)

       sg.addWidget(QLabel("Duration to Acquire"), 3, 0)
       dur_acq_row = QHBoxLayout()
       self.duration_acquire_entry = QLineEdit(
           self.expert_defaults["Duration to Acquire"])
       self.duration_acquire_entry.setToolTip(
           "Default length of a recording, used to pre-fill Duration to\n"
           "Acquire in the main window. It can still be changed there per run;\n"
           "this only sets what it starts at."
       )
       dur_acq_row.addWidget(self.duration_acquire_entry)
       self.duration_acquire_unit = QComboBox()
       self.duration_acquire_unit.addItems(DURATION_UNITS)
       self.duration_acquire_unit.setCurrentText(
           self.expert_defaults["Duration to Acquire Unit"])
       dur_acq_row.addWidget(self.duration_acquire_unit)
       sg.addLayout(dur_acq_row, 3, 1)

       sections_row.addWidget(sample_grp)

       # ── Plot Settings ────────────────────────────────────────────
       plot_grp = QGroupBox("Plot Settings")
       pg = QGridLayout(plot_grp)

       pg.addWidget(QLabel("Refresh Rate (Hz)"), 0, 0)
       self.refresh_entry = QLineEdit(self.expert_defaults["Refresh Rate (Hz)"])
       pg.addWidget(self.refresh_entry, 0, 1)

       pg.addWidget(QLabel("Max Plot Points"), 1, 0)
       self.max_points_entry = QLineEdit(self.expert_defaults["Max Plot Points"])
       self.max_points_entry.setToolTip(
           "Caps how many points are drawn per channel — display only.\n"
           "With \"Decimate to fit\" ON, the whole plot-duration window is\n"
           "shown and decimated (min/max) down to this many points.\n"
           "With it OFF, only the most recent this-many samples are drawn,\n"
           "at full resolution. Higher = more detail but slower.\n"
           "Recorded/saved data is always full-rate and never affected."
       )
       pg.addWidget(self.max_points_entry, 1, 1)

       pg.addWidget(QLabel("Duration to Plot"), 2, 0)
       dur_plot_row = QHBoxLayout()
       self.duration_plot_entry = QLineEdit(
           self.expert_defaults["Duration to Plot"])
       self.duration_plot_entry.setPlaceholderText("Blank = full duration")
       self.duration_plot_entry.setToolTip(
           "Default width of the plot window, used to pre-fill Duration to\n"
           "Plot in the main window. Leave blank to plot the whole recording.\n"
           "It can still be changed there per run."
       )
       dur_plot_row.addWidget(self.duration_plot_entry)
       self.duration_plot_unit = QComboBox()
       self.duration_plot_unit.addItems(DURATION_UNITS)
       self.duration_plot_unit.setCurrentText(
           self.expert_defaults["Duration to Plot Unit"])
       dur_plot_row.addWidget(self.duration_plot_unit)
       pg.addLayout(dur_plot_row, 2, 1)

       self.decimate_chk = QCheckBox("Decimate to fit (keep full window)")
       self.decimate_chk.setChecked(False)
       self.decimate_chk.setToolTip(
           "ON  — show the whole plot-duration window; if it holds more\n"
           "      samples than Max Plot Points it is decimated (min/max, so\n"
           "      spikes survive) to fit, and older data stays in view at\n"
           "      lower resolution.\n"
           "OFF — show only the most recent Max Plot Points samples at full\n"
           "      resolution (older data scrolls off).\n"
           "Recorded/saved data is full-rate either way."
       )
       pg.addWidget(self.decimate_chk, 3, 0, 1, 2)

       # ── Appearance ────────────────────────────────────────────────
       # Last row of the group, below the acquisition-related controls, since it
       # is a display preference rather than something that changes the data.
       #
       # Deliberately NOT registered in all_expert_widgets and not touched by
       # on_use_default_toggle. Everything in that set is forced to its default
       # and greyed out while "Use default" is ticked, which is right for sample
       # rates and filters but wrong for a display preference — nobody should
       # have to take manual control of the acquisition settings just to change
       # the plot colours.
       pg.addWidget(QLabel("Theme"), 4, 0)
       self.theme_cb = QComboBox()
       self.theme_cb.addItems(_daq_theme.theme_names())
       self.theme_cb.setCurrentText(self.expert_defaults["Theme"])
       self.theme_cb.setToolTip(
           "Plot appearance. Takes effect when GioDAQ is restarted.\n\n"
           + "\n".join(
               f"{n} — {_daq_theme.get_theme(n)['description']}"
               for n in _daq_theme.theme_names()
           )
       )
       pg.addWidget(self.theme_cb, 4, 1)

       sections_row.addWidget(plot_grp)

       # ── Filters ──────────────────────────────────────────────────
       filter_grp = QGroupBox("Filters")
       fg = QGridLayout(filter_grp)

       fg.addWidget(QLabel("Filter Type"), 0, 0)
       self.filter_type_cb = QComboBox()
       self.filter_type_cb.addItems(
           ["off", "lowpass", "highpass", "bandpass", "bandstop", "median"])
       self.filter_type_cb.setCurrentText(self.expert_defaults["Filter Type"])
       fg.addWidget(self.filter_type_cb, 0, 1)

       fg.addWidget(QLabel("Filter Mode"), 1, 0)
       self.filter_mode_cb = QComboBox()
       self.filter_mode_cb.addItems(["IIR (sosfilt)", "FIR"])
       self.filter_mode_cb.setCurrentText(self.expert_defaults["Filter Mode"])
       fg.addWidget(self.filter_mode_cb, 1, 1)

       fg.addWidget(QLabel("Order"), 2, 0)
       self.filter_order_sp = QSpinBox()
       self.filter_order_sp.setRange(1, 12)
       self.filter_order_sp.setValue(int(self.expert_defaults["Filter Order"]))
       fg.addWidget(self.filter_order_sp, 2, 1)

       fg.addWidget(QLabel("Cutoff (Hz)"), 3, 0)
       self.filter_fc_entry = QLineEdit(self.expert_defaults["Filter Cutoff (Hz)"])
       fg.addWidget(self.filter_fc_entry, 3, 1)

       fg.addWidget(QLabel("fh (Hz)"), 4, 0)
       self.filter_fh_entry = QLineEdit(self.expert_defaults["Filter fh (Hz)"])
       fg.addWidget(self.filter_fh_entry, 4, 1)

       # Apply-to: choose which input channels the filter is applied to.
       # Selection is held in self._filter_channels as the sentinel "All" or a
       # list of input-channel Device Names. Persisted as the "Filter Channels"
       # expert setting (value "All" or a comma-separated list of names).
       fg.addWidget(QLabel("Apply to"), 5, 0)
       self.filter_channels_btn = QPushButton("All channels")
       self.filter_channels_btn.setToolTip(
           "Choose which input channels the filter is applied to.\n"
           "Channels not selected here are passed through unfiltered."
       )
       self.filter_channels_btn.clicked.connect(self._open_filter_channel_picker)
       fg.addWidget(self.filter_channels_btn, 5, 1)
       self._set_filter_channels(self.expert_defaults.get("Filter Channels", "All"))


       sections_row.addWidget(filter_grp)
       outer.addLayout(sections_row)

       # ── Data Recording Settings (below Sample Settings) ───────────
       rec_grp = QGroupBox("Data Recording Settings")
       rg = QGridLayout(rec_grp)

       self.cache_to_disk_chk = QCheckBox("Cache to Disk")
       self.cache_to_disk_chk.setChecked(True)
       rg.addWidget(self.cache_to_disk_chk, 0, 0, 1, 2)

       rg.addWidget(QLabel("Cache Interval (s)"), 1, 0)
       self.cache_interval_entry = QLineEdit("900")
       rg.addWidget(self.cache_interval_entry, 1, 1)

       self.cache_to_disk_chk.stateChanged.connect(self._on_cache_toggle)
       outer.addWidget(rec_grp)

       outer.addStretch()

       # Map used by on_use_default_toggle
       self.all_expert_widgets = {
           "Sample Rate (Hz)":    self.sample_rate_entry,
           "AO Sample Rate (Hz)": self.ao_sample_rate_entry,
           "Max Buffer Size":     self.max_buffer_entry,
           "Refresh Rate (Hz)":   self.refresh_entry,
           "Max Plot Points":     self.max_points_entry,
           "Duration to Acquire":      self.duration_acquire_entry,
           "Duration to Acquire Unit": self.duration_acquire_unit,
           "Duration to Plot":         self.duration_plot_entry,
           "Duration to Plot Unit":    self.duration_plot_unit,
           "Filter Mode":         self.filter_mode_cb,
           "Filter Type":         self.filter_type_cb,
           "Filter Order":        self.filter_order_sp,
           "Filter Cutoff (Hz)":  self.filter_fc_entry,
           "Filter fh (Hz)":      self.filter_fh_entry,
       }

       self.filter_type_cb.currentTextChanged.connect(self._update_filter_fields)
       self.use_default_chk.stateChanged.connect(self.on_use_default_toggle)

       self._update_filter_fields()
       self.on_use_default_toggle()

   # ------------------------------------------------------------------
   # Expert Settings logic
   # ------------------------------------------------------------------

   def _on_cache_toggle(self, state):
       self.cache_interval_entry.setEnabled(state == Qt.Checked)

   def _update_filter_fields(self, *_):
       """Enable only the filter sub-fields relevant to the current type.
       When type is 'off', Filter Mode is also grayed out (it has no effect).
       """
       if self.use_default_chk.isChecked():
           return  # on_use_default_toggle owns state when locked
       t = self.filter_type_cb.currentText().lower()

       # Filter Mode is meaningless when filtering is off
       self.filter_mode_cb.setEnabled(t != "off")

       self.filter_order_sp.setEnabled(False)
       self.filter_fc_entry.setEnabled(False)
       self.filter_fh_entry.setEnabled(False)

       if t == "median":
           self.filter_order_sp.setEnabled(True)
       elif t in ("lowpass", "highpass"):
           self.filter_order_sp.setEnabled(True)
           self.filter_fc_entry.setEnabled(True)
       elif t in ("bandpass", "bandstop"):
           self.filter_order_sp.setEnabled(True)
           self.filter_fc_entry.setEnabled(True)
           self.filter_fh_entry.setEnabled(True)

       # The channel picker is meaningless when filtering is off.
       if hasattr(self, "filter_channels_btn"):
           self.filter_channels_btn.setEnabled(t != "off")

   # ------------------------------------------------------------------
   # Filter "Apply to" channel selection
   # ------------------------------------------------------------------

   def _input_channel_labels(self):
       """Identifiers for the currently selected input channels, used by the
       filter 'Apply to' picker and by the acquisition loop to match channels.

       Uses the Device Name when set; otherwise falls back to the
       device-qualified physical id ('cDAQ2Mod1/ai0') so channels from
       different modules never collide."""
       labels = []
       for i in range(1, len(self.chan_names) + 1):
           chk = self.check_vars.get(f"Selected Channel{i}")
           if chk is not None and not chk.isChecked():
               continue
           name_w = self.entry_widgets.get(f"Device_name{i}")
           name = self.strip_value(name_w.text()) if name_w is not None else ""
           if not name:
               chan_w = self.entry_widgets.get(f"Channel_entry{i}")
               daq_w  = self.entry_widgets.get(f"DAQ_entry{i}")
               ch  = chan_w.text().strip() if chan_w is not None else f"ai{i-1}"
               dev = daq_w.text().strip()  if daq_w  is not None else ""
               name = f"{dev}/{ch}" if dev else ch
           if name:
               labels.append(name)
       return labels

   def _set_filter_channels(self, value):
       """Normalize and store the selection, then refresh the button label.
       `value` may be the sentinel string "All", a comma-separated string, or
       a list of channel names."""
       if isinstance(value, (list, tuple)):
           names = [str(v).strip() for v in value if str(v).strip()]
       else:
           sval = str(value).strip()
           if sval.lower() in ("", "all"):
               names = "All"
           else:
               names = [s.strip() for s in sval.split(",") if s.strip()]
       self._filter_channels = "All" if names == "All" else names
       if hasattr(self, "filter_channels_btn"):
           self.filter_channels_btn.setText(self._filter_channels_summary())

   def _filter_channels_summary(self):
       """Short text for the picker button."""
       if self._filter_channels == "All":
           return "All channels"
       if not self._filter_channels:
           return "None selected"
       total = len(self._input_channel_labels())
       n = len(self._filter_channels)
       if n <= 2:
           return ", ".join(self._filter_channels)
       return f"{n} of {total} channels"

   def _filter_channels_value(self):
       """Value to persist: the sentinel 'All' or a comma-separated name list."""
       if self._filter_channels == "All":
           return "All"
       return ",".join(self._filter_channels)

   def _open_filter_channel_picker(self):
       channels = self._input_channel_labels()
       if not channels:
           QMessageBox.information(
               self, "No input channels",
               "Select at least one input channel in the Input Channels tab "
               "before choosing which channels to filter.")
           return

       dlg = QDialog(self)
       dlg.setWindowTitle("Filter — Apply to channels")
       v = QVBoxLayout(dlg)

       all_chk = QCheckBox("All channels")
       v.addWidget(all_chk)

       # Keep a list (not a dict) so that if two rows share a label, BOTH
       # checkboxes are tracked and get enabled/disabled together. A dict
       # would drop all but the last, leaving the earlier ("top") boxes
       # ungrayed when "All channels" is ticked.
       box_items = []   # list of (name, QCheckBox)
       for name in channels:
           cb = QCheckBox(name)
           v.addWidget(cb)
           box_items.append((name, cb))

       # Seed current selection.
       if self._filter_channels == "All":
           all_chk.setChecked(True)
       else:
           for name, cb in box_items:
               cb.setChecked(name in self._filter_channels)

       def _on_all_toggled(checked):
           for _name, cb in box_items:
               cb.setEnabled(not checked)
       all_chk.toggled.connect(_on_all_toggled)
       _on_all_toggled(all_chk.isChecked())

       bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
       bb.accepted.connect(dlg.accept)
       bb.rejected.connect(dlg.reject)
       v.addWidget(bb)

       if dlg.exec_() != QDialog.Accepted:
           return

       if all_chk.isChecked():
           self._set_filter_channels("All")
       else:
           # Preserve order and dedupe while collecting picks.
           picked = []
           for name, cb in box_items:
               if cb.isChecked() and name not in picked:
                   picked.append(name)
           # No explicit picks behaves like "All" so the filter is never
           # silently disabled for every channel.
           self._set_filter_channels(picked if picked else "All")

   def on_use_default_toggle(self, *_):
       use_default = self.use_default_chk.isChecked()

       if use_default:
           # Fill from defaults then disable everything
           for name, widget in self.all_expert_widgets.items():
               _set_widget_value(widget, self.expert_defaults.get(name, ""))
               widget.setEnabled(False)
           # Filter channel picker is not a standard widget — reset + lock it.
           self._set_filter_channels(self.expert_defaults.get("Filter Channels", "All"))
           if hasattr(self, "filter_channels_btn"):
               self.filter_channels_btn.setEnabled(False)
           # Reset cache controls to defaults and gray them out
           self.cache_to_disk_chk.setChecked(True)
           self.cache_interval_entry.setText("900")
           self.cache_to_disk_chk.setEnabled(False)
           self.cache_interval_entry.setEnabled(False)
           # Decimate-to-fit: reset to default (off) and gray out
           self.decimate_chk.setChecked(False)
           self.decimate_chk.setEnabled(False)
       else:
           # Re-enable all, then apply filter-type rules
           for name, widget in self.all_expert_widgets.items():
               widget.setEnabled(True)
           self._update_filter_fields()
           # Restore cache controls — interval enabled only if checkbox is checked
           self.cache_to_disk_chk.setEnabled(True)
           self.cache_interval_entry.setEnabled(self.cache_to_disk_chk.isChecked())
           self.decimate_chk.setEnabled(True)

   # ------------------------------------------------------------------
   # Custom channel dropdowns
   # ------------------------------------------------------------------

   def _get_available_channels(self):
       """Return channels whose Selected checkbox is ticked in the Input tab."""
       return [
           name for i, name in enumerate(self.chan_names, start=1)
           if self.check_vars.get(f"Selected Channel{i}", QCheckBox()).isChecked()
       ]

   def _selectable_chan_names(self):
       """All input channels that can still be used as a data source, i.e.
       every channel except the pins consumed as a differential (-) input."""
       locked = getattr(self, "_diff_locked_rows", set())
       return [
           name for i, name in enumerate(self.chan_names, start=1)
           if i not in locked
       ]

   def _custom_row_pool(self, row):
       """Channels a custom row may draw on.

       A selected custom row is restricted to channels selected on the Input
       tab; an unselected one may point at any channel that is not consumed as a
       differential (-) input. Both A and B use this same pool.
       """
       chk = self.check_vars.get(f"Selected Channel_Custom{row}")
       if chk is not None and chk.isChecked():
           return self._get_available_channels()
       return self._selectable_chan_names()

   def _update_single_custom_row(self, row):
       """Repopulate A/B channel dropdowns for one custom row.

       - Custom row Selected unchecked -> all channels available
       - Custom row Selected checked   -> only Input-tab selected channels
       After repopulating A, _sync_b_dropdown is called so B never
       shows the same channel as A.
       """
       values = self._custom_row_pool(row)
       # Repopulate A dropdown
       a_cb = self.entry_widgets.get(f"first_chan_sel{row}")
       if a_cb:
           cur = a_cb.currentText()
           a_cb.blockSignals(True)
           a_cb.clear()
           a_cb.addItems(values)
           if cur in values:
               a_cb.setCurrentText(cur)
           else:
               # The channel A was pointing at is gone (deselected, or now the
               # negative half of a DIFF pair), or A was already blank. Keep the
               # box blank rather than quietly sliding A onto a different
               # channel; save flags the empty selection.
               a_cb.insertItem(0, "")
               a_cb.setCurrentIndex(0)
           a_cb.blockSignals(False)
       # Repopulate B dropdown (excludes A's selection, always has None)
       self._sync_b_dropdown(row)


   def _update_custom_dropdowns(self):
       """Refresh all custom rows (called when Input-tab checkboxes change)."""
       for i in range(1, self.no_of_cus_chans + 1):
           self._update_single_custom_row(i)

   def _sync_b_dropdown(self, row):
       """Rebuild B dropdown so it never shows the same channel as A.
       "None" is always present and stays selected if B was using the
       channel that A just took.
       """
       a_cb = self.entry_widgets.get(f"first_chan_sel{row}")
       b_cb = self.entry_widgets.get(f"sec_chan_sel{row}")
       if not a_cb or not b_cb:
           return
       a_val   = a_cb.currentText()
       b_cur   = b_cb.currentText()
       pool    = self._custom_row_pool(row)
       b_items = ["None"] + [ch for ch in pool if ch != a_val]
       b_cb.blockSignals(True)
       b_cb.clear()
       b_cb.addItems(b_items)
       # Restore previous B if still valid, else default to None
       b_cb.setCurrentText(b_cur if b_cur in b_items else "None")
       b_cb.blockSignals(False)

   def _channel_friendly_name(self, chan_id):
       """Return the Device Name assigned to a physical channel id on the
       Input Channels tab (used for the A/B dropdown hover tooltips).
       """
       try:
           idx = self.chan_names.index(chan_id) + 1
       except ValueError:
           return ""
       w = self.entry_widgets.get(f"Device_name{idx}")
       return self.strip_value(w.text()) if w else ""

   def _render_latex_pixmap(self, latex):
       """Render a LaTeX string to a QPixmap via matplotlib's mathtext.
       Returns None if matplotlib isn't available or rendering fails, in
       which case the caller falls back to showing the LaTeX source text.
       """
       if getattr(self, '_mpl_available', None) is False:
           return None
       try:
           import io
           import matplotlib
           matplotlib.use("Agg")
           from matplotlib.figure import Figure
           from matplotlib.backends.backend_agg import FigureCanvasAgg
           from PyQt5.QtGui import QPixmap
           self._mpl_available = True
           fig = Figure(figsize=(3, 0.5))
           fig.patch.set_alpha(0.0)
           fig.text(0.01, 0.5, f"${latex}$", fontsize=9, va='center')
           FigureCanvasAgg(fig)
           buf = io.BytesIO()
           fig.savefig(buf, format='png', bbox_inches='tight',
                       transparent=True, dpi=130)
           buf.seek(0)
           pix = QPixmap()
           pix.loadFromData(buf.getvalue(), 'PNG')
           return pix if not pix.isNull() else None
       except Exception:
           self._mpl_available = False
           return None

   def _update_expr_preview(self, row):
       """Refresh the LaTeX preview label for one custom row."""
       expr_w = self.entry_widgets.get(f"operation_sel{row}")
       lbl    = self.entry_widgets.get(f"expr_preview{row}")
       if not expr_w or lbl is None:
           return
       text = expr_w.text().strip()
       lbl.clear()   # resets any previous text or pixmap
       if not text:
           lbl.setToolTip("")
           return
       latex = _expr_to_latex(text)
       if latex is None:
           lbl.setText("<i>(can\u2019t parse \u2014 check syntax)</i>")
           lbl.setToolTip("")
           return
       pix = self._render_latex_pixmap(latex)
       if pix is not None:
           lbl.setPixmap(pix)
       else:
           lbl.setText(f"<code style='font-size:8px'>{latex}</code>")
       lbl.setToolTip(latex)

   # ------------------------------------------------------------------
   # Excel – read defaults
   # ------------------------------------------------------------------

   # Recognized sheets that mark a workbook as a DAQ settings file.
   _SETTINGS_SHEETS = (
       "input_settings", "output_settings", "custom_channel_settings",
       "digital_settings", "expert_settings", "transducers_settings",
       "key_mappings",
   )

   def _validate_settings_file(self, path):
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
       if not (sheets & set(self._SETTINGS_SHEETS)):
           found = ", ".join(sorted(sheets)) or "none"
           return False, ("This does not look like a DAQ settings file.\n\n"
                          "It is missing the expected sheets (e.g. "
                          "'input_settings', 'output_settings').\n\n"
                          f"Sheets found: {found}")
       # An input-channel sheet with the core columns is the minimum needed.
       if "input_settings" not in sheets:
           return False, ("This settings file has no input-channel sheet "
                          "('input_settings'), so it cannot be loaded.")
       try:
           df = pd.read_excel(xls, sheet_name="input_settings", nrows=1)
       except Exception as e:
           return False, f"The 'input_settings' sheet could not be read.\n\nDetails: {e}"
       required = {"Channel", "Selected Channel"}
       missing = required - set(str(c) for c in df.columns)
       if missing:
           return False, ("The 'input_settings' sheet is missing required "
                          f"column(s): {', '.join(sorted(missing))}.\n\n"
                          "This file is not a valid settings file.")
       return True, "OK"

   def load_settings_file(self):
       """Let the user pick a settings .xlsx, validate it, and load it into the
       editor. The user then reviews and clicks 'Save Settings' to make it the
       active configuration."""
       path, _ = QFileDialog.getOpenFileName(
           self, "Load Settings File", "",
           "Excel settings files (*.xlsx *.xls);;All files (*)")
       if not path:
           return

       ok, msg = self._validate_settings_file(path)
       if not ok:
           QMessageBox.critical(self, "Invalid Settings File", msg)
           return

       try:
           # Rebuild every tab from the current hardware, loading values from the
           # chosen file instead of the default. Channels whose device/channel
           # aren't present are simply left unselected (never cross-applied).
           self.update_channels(settings_path=path)
       except Exception as e:
           QMessageBox.critical(
               self, "Load Failed",
               f"The settings file looked valid but could not be applied.\n\nDetails: {e}")
           return

       QMessageBox.information(
           self, "Settings Loaded",
           "Loaded settings from:\n"
           f"{path}\n\n"
           "Review the channels and values, then click 'Save Settings' to make "
           "them the active configuration.")

   def read_defaults_from_excel(self, filename):
       try:
           xls = pd.ExcelFile(filename)

           has_input = "input_settings" in xls.sheet_names
           def _match_idx(lookup, srow):
               """Find the UI row index for a saved row by its exact
               (device, channel) pair, so a saved 'Dev2/ao1' never lands on a
               different device with an 'ao1' channel."""
               return lookup.get((_cell(srow, "DAQ", strip=True),
                                  _cell(srow, "Channel", strip=True)))

           if has_input:
               df = pd.read_excel(xls, sheet_name="input_settings")
               lk = getattr(self, "_ai_row_lookup", {})
               for i in range(len(df)):
                   idx = _match_idx(lk, df.iloc[i])
                   if idx is not None:
                       self._set_entry_values(idx, df.iloc[i], "input")

           if "output_settings" in xls.sheet_names:
               df = pd.read_excel(xls, sheet_name="output_settings")
               lk = getattr(self, "_ao_row_lookup", {})
               for i in range(len(df)):
                   idx = _match_idx(lk, df.iloc[i])
                   if idx is not None:
                       self._set_entry_values(idx, df.iloc[i], "output")

           if "custom_channel_settings" in xls.sheet_names:
               df = pd.read_excel(xls, sheet_name="custom_channel_settings")
               for i in range(len(df)):
                   self._set_entry_values(i + 1, df.iloc[i], "custom")

           if "digital_settings" in xls.sheet_names:
               df = pd.read_excel(xls, sheet_name="digital_settings")
               lk = getattr(self, "_dig_row_lookup", {})
               for i in range(len(df)):
                   idx = _match_idx(lk, df.iloc[i])
                   if idx is not None:
                       self._set_entry_values(idx, df.iloc[i], "digital")

           if "transducers_settings" in xls.sheet_names:
               df = pd.read_excel(xls, sheet_name="transducers_settings")
               for i in range(min(len(df), self.no_of_transducers)):
                   row = df.iloc[i]
                   def _tv(col):
                       return _cell(row, col)
                   self.entry_widgets[f"trans_hw{i+1}"].setText(_tv("Hardware Model"))
                   self.entry_widgets[f"trans_type{i+1}"].setText(_tv("Hardware Type"))
                   self.entry_widgets[f"trans_minv{i+1}"].setText(_tv("Min Output Range"))
                   self.entry_widgets[f"trans_maxv{i+1}"].setText(_tv("Max Output Range"))
                   self.entry_widgets[f"trans_min_sig{i+1}"].setText(_tv("Min Signal Input"))
                   self.entry_widgets[f"trans_max_sig{i+1}"].setText(_tv("Max Signal Input"))
                   # Absent in files written before this column existed; those
                   # were voltage-only, so the "V" default is correct.
                   ru = _tv("Output Unit").strip()
                   ru_cb = self.entry_widgets.get(f"trans_output_unit{i+1}")
                   if ru and ru_cb:
                       if ru_cb.findText(ru) == -1:
                           ru_cb.addItem(ru)
                       ru_cb.blockSignals(True)
                       ru_cb.setCurrentText(ru)
                       ru_cb.blockSignals(False)
                   tc = _tv("Terminal Configuration")
                   if tc:
                       self.entry_widgets[f"trans_term{i+1}"].setCurrentText(tc)
                   self.entry_widgets[f"trans_mx{i+1}"].setText(_tv("Mx"))
                   self.entry_widgets[f"trans_b{i+1}"].setText(_tv("B"))
                   self.entry_widgets[f"trans_units{i+1}"].setText(_tv("Scaled Units"))
                   self._refresh_trans_scaled_unit(i + 1)

           if "expert_settings" in xls.sheet_names:
               df = pd.read_excel(xls, sheet_name="expert_settings")

               def _load(label, widget):
                   ser = df.loc[df["Setting"] == label, "Value"]
                   if ser.empty:
                       return
                   val = "" if pd.isna(ser.iloc[0]) else str(ser.iloc[0])
                   _set_widget_value(widget, val)

               _load("Sample Rate (Hz)",    self.sample_rate_entry)
               _load("AO Sample Rate (Hz)", self.ao_sample_rate_entry)
               _load("Max Buffer Size",     self.max_buffer_entry)
               _load("Refresh Rate (Hz)",   self.refresh_entry)
               _load("Max Plot Points",     self.max_points_entry)
               _load("Duration to Acquire",      self.duration_acquire_entry)
               _load("Duration to Acquire Unit", self.duration_acquire_unit)
               _load("Duration to Plot",         self.duration_plot_entry)
               _load("Duration to Plot Unit",    self.duration_plot_unit)
               _load("Filter Mode",         self.filter_mode_cb)
               _load("Filter Type",         self.filter_type_cb)
               _load("Filter Order",        self.filter_order_sp)
               _load("Filter Cutoff (Hz)",  self.filter_fc_entry)
               _load("Filter fh (Hz)",      self.filter_fh_entry)

               # Filter "Apply to" channel selection (sentinel "All" or list)
               ser_fch = df.loc[df["Setting"] == "Filter Channels", "Value"]
               if not ser_fch.empty and not pd.isna(ser_fch.iloc[0]):
                   self._set_filter_channels(str(ser_fch.iloc[0]))

               # Cache to Disk settings (stored as "0"/"1" strings)
               ser_cache = df.loc[df["Setting"] == "Cache to Disk", "Value"]
               if not ser_cache.empty:
                   self.cache_to_disk_chk.setChecked(str(ser_cache.iloc[0]).strip() != "0")
               # Decimate To Fit (display-only; "0"/"1"). Missing -> stays ON.
               ser_decimate = df.loc[df["Setting"] == "Decimate To Fit", "Value"]
               if not ser_decimate.empty and not pd.isna(ser_decimate.iloc[0]):
                   self.decimate_chk.setChecked(str(ser_decimate.iloc[0]).strip() != "0")
               ser_theme = df.loc[df["Setting"] == "Theme", "Value"]
               if not ser_theme.empty and not pd.isna(ser_theme.iloc[0]):
                   name = str(ser_theme.iloc[0]).strip()
                   # Only accept a theme this build knows about, so a hand-edited
                   # or newer workbook cannot leave the combo showing something
                   # that does not exist.
                   if name in _daq_theme.theme_names():
                       self.theme_cb.setCurrentText(name)

               ser_interval = df.loc[df["Setting"] == "Cache Interval (s)", "Value"]
               if not ser_interval.empty and not pd.isna(ser_interval.iloc[0]):
                   self.cache_interval_entry.setText(str(ser_interval.iloc[0]))

               # Restore Use Default flag
               if "Use Default" in df.columns and not df.empty:
                   try:
                       flag = bool(int(df.iloc[0]["Use Default"]))
                   except Exception:
                       flag = True
                   self.use_default_chk.setChecked(flag)
                   self.on_use_default_toggle()

           if "key_mappings" in xls.sheet_names:
               df_keys = pd.read_excel(xls, sheet_name="key_mappings")
               if not hasattr(self, '_key_bindings'):
                   self._key_bindings = {a: "" for a in self.KEY_MAP_ACTIONS}
               for _, row in df_keys.iterrows():
                   action = str(row.get("Action", ""))
                   key    = "" if pd.isna(row.get("Key", "")) else str(row.get("Key", ""))
                   if action in self._key_bindings:
                       self._key_bindings[action] = key
               # Refresh display labels if the tab has been built already
               if hasattr(self, '_key_display'):
                   for action, disp in self._key_display.items():
                       k = self._key_bindings.get(action, "") or "None"
                       disp.setText(f"<b>{k}</b>")

           # ── App Settings: FG custom functions ────────────────────
           if "app_fg_functions" in xls.sheet_names and hasattr(self, "_fg_wave_rows"):
               df = pd.read_excel(xls, sheet_name="app_fg_functions").fillna("")
               wi = mi = 0
               for _, r in df.iterrows():
                   typ = str(r.get("Type", "")).strip().lower()
                   if typ == "waveform" and wi < len(self._fg_wave_rows):
                       w = self._fg_wave_rows[wi]; wi += 1
                       w["name"].setText(str(r.get("Name", "")))
                       xu = str(r.get("X Unit", "")).strip()
                       if xu:
                           w["xunit"].setCurrentText(xu)
                       w["points"].setText(str(r.get("Points", "")))
                   elif typ == "math" and mi < len(self._fg_math_rows):
                       m = self._fg_math_rows[mi]; mi += 1
                       m["name"].setText(str(r.get("Name", "")))
                       m["expr"].setText(str(r.get("Expression", "")))

           # ── App Settings: plot lines ─────────────────────────────
           if "app_plot_lines" in xls.sheet_names and hasattr(self, "_plot_line_rows"):
               df = pd.read_excel(xls, sheet_name="app_plot_lines").fillna("")
               for i, (_, r) in enumerate(df.iterrows()):
                   if i >= len(self._plot_line_rows):
                       break
                   row = self._plot_line_rows[i]
                   try:
                       row["enable"].setChecked(bool(int(r.get("Enabled", 0))))
                   except Exception:
                       row["enable"].setChecked(False)
                   row["label"].setText(str(r.get("Label", "")))
                   ax = str(r.get("Axis", "Y")).strip() or "Y"
                   row["axis"].setCurrentText(ax if ax in ("Y", "X") else "Y")
                   # Limit mode; blank or unknown defaults to Single
                   lim = str(r.get("Limit", "Single")).strip().title() or "Single"
                   if lim not in ("Single", "Double"):
                       lim = "Single"
                   row["limit"].blockSignals(True)
                   row["limit"].setCurrentText(lim)
                   row["limit"].blockSignals(False)
                   self._apply_plot_limit_mode(row)
                   op = str(r.get("Op", "")).strip()
                   row["op"].blockSignals(True)
                   if lim == "Double":
                       row["op"].setCurrentText(op if op in self._DOUBLE_OPS else "<=")
                   else:
                       row["op"].setCurrentText(op if op in self._SINGLE_OPS else "=")
                   row["op"].blockSignals(False)
                   row["value"].setText(str(r.get("Value", "")))
                   if lim == "Double":
                       op2 = str(r.get("Op2", "")).strip()
                       row["op2"].blockSignals(True)
                       row["op2"].setCurrentText(
                           op2 if op2 in self._DOUBLE_OPS
                           else self._flip_op(row["op"].currentText()))
                       row["op2"].blockSignals(False)
                       row["value2"].setText(str(r.get("Value2", "")))
                   u = str(r.get("Unit", "")).strip()
                   if u:
                       row["unit"].setCurrentText(u)
                   c = str(r.get("Color", "")).strip()
                   if c:
                       row["color"].setCurrentText(c)
                   s = str(r.get("Style", "")).strip()
                   if s:
                       row["style"].setCurrentText(s)

           # ── App Settings: axis ranges ────────────────────────────
           if "axis_ranges" in xls.sheet_names and hasattr(self, "_axis_range_grid"):
               df = pd.read_excel(xls, sheet_name="axis_ranges").fillna("")
               self._axis_range_rows = {}      # replaced, not merged into
               self._axis_range_values = {}
               for _, r in df.iterrows():
                   unit = str(r.get("Unit", "")).strip()
                   if unit:
                       self._axis_range_values[unit] = (
                           str(r.get("Range", "")).strip(),
                           str(r.get("Min", "")).strip(),
                           str(r.get("Max", "")).strip())
               self._refresh_axis_range_rows()

           # ── App Settings: App Bar visibility + plugins ─────
           if "app_bar" in xls.sheet_names:
               df = pd.read_excel(xls, sheet_name="app_bar")
               known = {}              # plugin folder name -> sheet values
               for _, r in df.iterrows():
                   app_id = _cell(r, "App", strip=True)
                   try:
                       show = bool(int(r["Show"]))
                   except Exception:
                       show = True
                   plugin = _cell(r, "Plugin", strip=True)
                   if plugin:
                       try:
                           enabled = bool(int(r["Enabled"]))
                       except Exception:
                           enabled = False
                       known[plugin] = {'enabled': enabled, 'show': show, 'id': app_id,
                                        'label': _cell(r, "Label", strip=True)}
                       continue
                   # A key for a built-in app that is not in APP_MODULES
                   # (removed, or disabled by commenting it out) is skipped
                   # rather than aborting the rest of the load. Apps absent
                   # from the sheet keep their default tick state.
                   chk = self._app_bar_checks.get(app_id)
                   if chk is not None:
                       chk.setChecked(show)
               # Replaced, not merged: a plugin not in this sheet starts off.
               self._plugin_rows = [dict(r, enabled=False, show=True)
                                    for r in self._discover_plugin_rows()]
               for row in self._plugin_rows:
                   k = known.get(row['name'])
                   if k:
                       row['enabled'] = k['enabled'] and row['has_entry']
                       row['show'] = k['show']
                       row['id'] = row['id'] or k['id']
                       row['label'] = row['label'] or k['label']
               self._refresh_plugin_rows()

           # ── App Settings: shortcuts ───────────────────────
           if "app_shortcuts" in xls.sheet_names:
               df = pd.read_excel(xls, sheet_name="app_shortcuts")
               shortcut_rows = []      # replaced, not merged into
               for _, r in df.iterrows():
                   path = _cell(r, "Path", strip=True)
                   if not path:
                       continue
                   try:
                       show = bool(int(r["Show"]))
                   except Exception:
                       show = True
                   shortcut_rows.append({'name': _cell(r, "Name", strip=True),
                                         'path': path, 'show': show})
               self._shortcut_rows = shortcut_rows
               self._refresh_shortcut_rows()

       except FileNotFoundError:
           print(f"{filename} not found. Using default settings.")
       except Exception as e:
           print(f"Error reading {filename}: {e}")

       self._update_custom_dropdowns()

   def _set_entry_values(self, index, row, ctype):
       def val(col):
           return _cell(row, col)

       def _set_selected(key):
           """Apply a saved Selected flag; a blank or malformed cell leaves the
           checkbox as built."""
           chk = self.check_vars.get(key)
           if chk is None:
               return
           try:
               chk.setChecked(bool(int(val("Selected Channel"))))
           except (TypeError, ValueError):
               pass

       w = self.entry_widgets

       if ctype == "input":
           if f"min_entry{index}" not in w:
               return
           # Input Type first: it sets the row's unit, which the Min/Max labels
           # and the transducer filter both read.
           # Input Type comes from the card, so a saved value is only applied
           # when the hardware still offers it; otherwise the live reading wins
           # and validation reports the discrepancy.
           it = val("Input Type")
           tw = w.get(f"ai_type{index}")
           if it and isinstance(tw, QComboBox) and tw.findText(it) != -1:
               tw.blockSignals(True)
               tw.setCurrentText(it)
               tw.blockSignals(False)
           self._refresh_row_unit(index, 'ai')
           w[f"min_entry{index}"].setText(val("Min"))
           w[f"max_entry{index}"].setText(val("Max"))
           tc = val("Terminal Configuration")
           w[f"ter_selection_var{index}"].setCurrentText(tc or self.Default_terminal)
           w[f"Device_name{index}"].setText(val("Device Name"))
           w[f"mx_plus_entry{index}"].setText(val("Mx"))
           w[f"b_entry{index}"].setText(val("b"))
           w[f"units_entry{index}"].setText(val("Scaled Units"))

           trans_val = val("Transducer")
           if trans_val:
               cb = w.get(f"transducer_sel{index}")
               if cb:
                   if cb.findText(trans_val) == -1:
                       cb.addItem(trans_val)
                   cb.blockSignals(True)
                   cb.setCurrentText(trans_val)
                   cb.blockSignals(False)

           _set_selected(f"Selected Channel{index}")

       elif ctype == "output":
           if f"min_entry_output{index}" not in w:
               return
           ot = val("Output Type")
           ow = w.get(f"output_type{index}")
           if ot and isinstance(ow, QComboBox) and ow.findText(ot) != -1:
               ow.blockSignals(True)
               ow.setCurrentText(ot)
               ow.blockSignals(False)
           self._refresh_row_unit(index, 'ao')
           w[f"min_entry_output{index}"].setText(val("Min"))
           w[f"max_entry_output{index}"].setText(val("Max"))
           w[f"Device_name_output{index}"].setText(val("Device Name"))
           _set_selected(f"Selected Channel_output{index}")

       elif ctype == "custom":
           if f"Channel_entry_cust{index}" not in w:
               return
           ch_val = val("Channel")
           if ch_val:
               w[f"Channel_entry_cust{index}"].setText(ch_val)
           w[f"first_chan_sel{index}"].setCurrentText(val("First Channel"))
           w[f"operation_sel{index}"].setText(val("Operation"))
           # Restore B: empty string saved as "None" means no second channel
           b_cb = w.get(f"sec_chan_sel{index}")
           if b_cb:
               b_saved = val("Second Channel")
               b_cb.blockSignals(True)
               b_cb.setCurrentText(b_saved if b_saved else "None")
               b_cb.blockSignals(False)
           w[f"custom_chan_name{index}"].setText(val("Device Name"))
           w[f"cust_units_entry{index}"].setText(val("Units"))
           _set_selected(f"Selected Channel_Custom{index}")

       elif ctype == "digital":
           if f"dig_channel_entry{index}" not in w:
               return
           direction = val("Direction")
           if direction:
               w[f"dig_direction_sel{index}"].setCurrentText(direction)
           w[f"dig_device_name{index}"].setText(val("Device Name"))
           _set_selected(f"Selected Channel_Digital{index}")

   def _validate_before_save(self):
       """Validate selected input channels before saving.
       Returns True if valid, False if the user should fix errors first.
       """
       ai_count = len(self.chan_names)
       errors = []
       device_names_seen = {}

       # Capabilities are queried per device (cached on the widget) so each row
       # is validated against the DAQ it actually belongs to.
       for i in range(1, ai_count + 1):
           selected = self.check_vars.get(f"Selected Channel{i}", QCheckBox()).isChecked()
           if not selected:
               continue

           chan_label = self.entry_widgets[f"Channel_entry{i}"].text() or f"Channel {i}"
           dev_i_name = self._row_dev(i, 'ai')
           unit = self._row_unit(i, 'ai')
           # Ranges for this row's measurement type, not just its voltage ranges.
           ai_ranges = self._ranges_for(dev_i_name, 'ai', unit)
           term_cfgs = self._caps(dev_i_name).get('term_cfgs', [])

           # ── Device Name ──────────────────────────────────────────
           device_name = self.strip_value(self.entry_widgets[f"Device_name{i}"].text())
           if not device_name:
               errors.append(f"• {chan_label}: Device Name is required.")
           else:
               if device_name in device_names_seen:
                   errors.append(
                       f"• {chan_label}: Device Name '{device_name}' is already "
                       f"used by {device_names_seen[device_name]}."
                   )
               else:
                   device_names_seen[device_name] = chan_label

           # ── Min / Max ────────────────────────────────────────────
           trans_name = self.entry_widgets[f"transducer_sel{i}"].currentText()
           live_trans = self.get_transducer_dict()
           using_transducer = trans_name and trans_name in live_trans

           if using_transducer:
               tv     = live_trans[trans_name]
               t_unit = (tv.get("Output Unit") or "V").strip()
               term   = str(tv.get("Terminal Configuration", "")).strip()
               src    = f"transducer '{trans_name}'"
               if _unit_dim(t_unit) != self._row_signal_dim(i, 'ai'):
                   errors.append(
                       f"• {chan_label}: transducer '{trans_name}' outputs "
                       f"{t_unit}, which this channel does not accept "
                       f"(it is a {self._w_text(f'ai_type{i}')} channel).\n"
                       f"  Pick a transducer whose output matches the wiring."
                   )
                   min_v = max_v = ""
               else:
                   # Compare in the channel's unit — a 0–20 mA transducer on an
                   # amp-ranged current input is 0–0.02 A.
                   min_v = _fmt_num(_convert_unit(
                       str(tv.get("Min Output Range", "")).strip(), t_unit, unit))
                   max_v = _fmt_num(_convert_unit(
                       str(tv.get("Max Output Range", "")).strip(), t_unit, unit))
                   if not min_v or not max_v:
                       errors.append(
                           f"• {chan_label}: transducer '{trans_name}' has a "
                           f"non-numeric Min/Max."
                       )
           else:
               min_v = self.entry_widgets[f"min_entry{i}"].text().strip()
               max_v = self.entry_widgets[f"max_entry{i}"].text().strip()
               term  = self.entry_widgets[f"ter_selection_var{i}"].currentText().strip()
               src   = chan_label

               if not min_v:
                   errors.append(f"• {chan_label}: Min is required.")
               if not max_v:
                   errors.append(f"• {chan_label}: Max is required.")

           min_f = max_f = None
           if min_v and max_v:
               try:
                   min_f, max_f = float(min_v), float(max_v)
                   if min_f >= max_f:
                       errors.append(
                           f"• {chan_label}: Min ({min_v} {unit}) must be "
                           f"less than Max ({max_v} {unit})."
                       )
               except ValueError:
                   errors.append(
                       f"• {chan_label}: Min/Max must be numeric values."
                   )

           # ── Check against DAQ supported ranges ───────────────────
           if ai_ranges and min_f is not None and max_f is not None:
               supported = any(
                   daq_min <= min_f and max_f <= daq_max
                   for daq_min, daq_max in ai_ranges
               )
               if not supported:
                   range_list = ", ".join(
                       f"[{mn} {unit}, {mx} {unit}]" for mn, mx in ai_ranges
                   )
                   errors.append(
                       f"• {chan_label}: Range [{min_v} {unit}, {max_v} {unit}] "
                       f"from {src} is not supported by this DAQ.\n"
                       f"  Supported ranges: {range_list}"
                   )

           # ── Check terminal configuration against DAQ ─────────────
           if term_cfgs and term:
               if term.strip().upper() not in [t.upper() for t in term_cfgs]:
                   errors.append(
                       f"• {chan_label}: Terminal configuration '{term}' from {src} "
                       f"is not supported by this DAQ.\n"
                       f"  Supported: {', '.join(term_cfgs)}"
                   )

           # ── DIFF uses two pins — check the pair is free ───────────
           if term.strip().upper() == "DIFF":
               dev_i = self._ai_row_info.get(i, ("", None))[0]
               off = self._ai_pair_offset(dev_i)
               if not self._row_can_be_diff(i):
                   errors.append(
                       f"• {chan_label}: cannot be DIFF — on {dev_i} it is the "
                       f"negative half of a differential pair. Use ai0…ai{off - 1} "
                       f"for differential channels."
                   )
               partner = self._diff_partner_row(i)
               if partner is not None:
                   p_chk = self.check_vars.get(f"Selected Channel{partner}")
                   if p_chk and p_chk.isChecked():
                       errors.append(
                           f"• {chan_label}: DIFF also uses "
                           f"{self._ai_row_label(partner)} as its negative input, "
                           f"so that channel cannot be selected separately."
                       )

       # ── Custom channels ──────────────────────────────────────────
       # A/B must still point at channels that are selected on the Input tab.
       # A pin consumed as a differential (-) input is never selectable, so
       # this also catches "cu0 uses ai4" after ai0 was switched to DIFF.
       live = set(self._get_available_channels())
       for i in range(1, self.no_of_cus_chans + 1):
           chk = self.check_vars.get(f"Selected Channel_Custom{i}")
           if not chk or not chk.isChecked():
               continue
           cu_label = self.entry_widgets[f"Channel_entry_cust{i}"].text() or f"cu{i - 1}"
           for slot, key in (("A", f"first_chan_sel{i}"), ("B", f"sec_chan_sel{i}")):
               w = self.entry_widgets.get(key)
               val = w.currentText().strip() if w else ""
               if slot == "B" and (not val or val == "None"):
                   continue          # B is optional
               if not val:
                   errors.append(f"• {cu_label}: no channel chosen for {slot}.")
               elif val not in live:
                   extra = ""
                   idx = self.chan_names.index(val) + 1 if val in self.chan_names else None
                   if idx is not None and idx in self._diff_locked_rows:
                       extra = (" — it is used as a differential (\u2212) input by "
                                f"{self._diff_reserved_rows().get(idx, 'another channel')}")
                   errors.append(
                       f"• {cu_label}: {slot} = '{val}' is not a selected input "
                       f"channel{extra}."
                   )

       # ── Output channels ──────────────────────────────────────────
       ao_count = sum(1 for k in self.entry_widgets if k.startswith("Channel_entry_output"))

       for i in range(1, ao_count + 1):
           selected = self.check_vars.get(f"Selected Channel_output{i}", QCheckBox()).isChecked()
           if not selected:
               continue

           unit = self._row_unit(i, 'ao')
           ao_ranges = self._ranges_for(
               self._row_dev(i, 'ao'), 'ao', unit)

           chan_label = self.entry_widgets.get(f"Channel_entry_output{i}")
           chan_label = chan_label.text() if chan_label else f"Output {i}"

           min_v = self.entry_widgets[f"min_entry_output{i}"].text().strip()
           max_v = self.entry_widgets[f"max_entry_output{i}"].text().strip()

           if not min_v:
               errors.append(f"• {chan_label}: Min is required.")
           if not max_v:
               errors.append(f"• {chan_label}: Max is required.")

           if min_v and max_v:
               try:
                   min_f, max_f = float(min_v), float(max_v)
                   if min_f >= max_f:
                       errors.append(
                           f"• {chan_label}: Min ({min_v} {unit}) must be "
                           f"less than Max ({max_v} {unit})."
                       )
                   elif ao_ranges:
                       supported = any(
                           daq_min <= min_f and max_f <= daq_max
                           for daq_min, daq_max in ao_ranges
                       )
                       if not supported:
                           range_list = ", ".join(
                               f"[{mn} {unit}, {mx} {unit}]" for mn, mx in ao_ranges
                           )
                           errors.append(
                               f"• {chan_label}: Range [{min_v} {unit}, "
                               f"{max_v} {unit}] is not supported by this DAQ.\n"
                               f"  Supported output ranges: {range_list}"
                           )
               except ValueError:
                   errors.append(
                       f"• {chan_label}: Min/Max must be numeric values."
                   )

       self._refresh_axis_range_rows()      # rows for the units being saved
       errors.extend(self._axis_range_errors())
       errors.extend(self._shortcut_errors())

       if errors:
           QMessageBox.warning(
               self, "Validation Errors",
               "Please fix the following issues before saving:\n\n" + "\n".join(errors)
           )
           return False
       return True

   def save_settings(self, *args, path=None):
       # `*args` absorbs the bool passed by QPushButton.clicked. `path` lets
       # Save As target an arbitrary file; default is the working settings file.
       # Validate selected input channels before saving
       if not self._validate_before_save():
           return

       # Warn about invalid App-Settings functions, but let the user save anyway
       # (they may be saving partial work).
       problems = self._invalid_app_functions()
       if problems:
           shown = "\n".join(f"  • {p}" for p in problems[:8])
           more = "" if len(problems) <= 8 else f"\n  …and {len(problems) - 8} more"
           reply = QMessageBox.warning(
               self, "Invalid App-Settings functions",
               "Some Function Generator entries in App Settings have problems:\n\n"
               f"{shown}{more}\n\nSave anyway?",
               QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
           if reply != QMessageBox.Yes:
               return

       # Ensure locked input fields reflect any edits made in the Transducers
       # tab since the last time a transducer was selected/applied.
       self._reapply_all_transducers()

       # A channel's range is 'Min'/'Max' in the unit named by 'Range Unit' —
       # never assumed to be volts. Whatever reads this workbook must use the
       # unit, not infer it.
       fieldnames_in  = ['Channel', 'DAQ', 'Input Type', 'Transducer',
                         'Min', 'Max', 'Range Unit',
                         'Terminal Configuration', 'Mx', 'b', 'Scaled Units',
                         'Device Name', 'Selected Channel']
       fieldnames_out = ['Channel', 'DAQ', 'Output Type',
                         'Min', 'Max', 'Range Unit',
                         'Device Name', 'Selected Channel']
       fieldnames_cus = ['Channel', 'First Channel', 'Operation', 'Second Channel',
                         'Device Name', 'Units', 'Selected Channel']
       fieldnames_dig = ['Channel', 'DAQ', 'Direction', 'Device Name', 'Selected Channel']
       trans_fields   = ["Hardware Model", "Hardware Type",
                         "Min Output Range", "Max Output Range", "Output Unit",
                         "Terminal Configuration", "Mx", "B", "Scaled Units",
                         "Min Signal Input", "Max Signal Input"]

       ai_count = len(self.chan_names)
       ao_count = sum(1 for k in self.entry_widgets if k.startswith("Channel_entry_output"))

       data_in, data_out, data_cus, data_dig, data_expert, data_trans = [], [], [], [], [], []

       # ── Input channels ───────────────────────────────────────────
       # Build the transducer dict once so we read live values (bypassing
       # the locked snapshot in the disabled input fields).
       live_trans = self.get_transducer_dict()

       for i in range(1, ai_count + 1):
           selected   = self.check_vars.get(f"Selected Channel{i}", QCheckBox()).isChecked()
           trans_name = self.entry_widgets[f"transducer_sel{i}"].currentText()

           unit = self._row_unit(i, 'ai')

           if trans_name and trans_name in live_trans:
               # Read directly from the transducer definition — this is always
               # current even if the user edited the Transducers tab and saved
               # without switching back to the Input Channels tab. The range is
               # converted into the channel's unit, matching what the row shows.
               tv     = live_trans[trans_name]
               t_unit = (tv.get("Output Unit") or "V").strip()
               conv_min = _convert_unit(tv.get("Min Output Range", ""), t_unit, unit)
               conv_max = _convert_unit(tv.get("Max Output Range", ""), t_unit, unit)
               min_v = _fmt_num(conv_min) if conv_min is not None else tv.get("Min Output Range", "")
               max_v = _fmt_num(conv_max) if conv_max is not None else tv.get("Max Output Range", "")
               term  = tv.get("Terminal Configuration", self.Default_terminal)
               mx    = tv.get("Mx", "")
               b_val = tv.get("B", "")
               units = self.strip_value(tv.get("Scaled Units", ""))
           else:
               # No transducer linked — read from the (user-editable) fields.
               min_v = self.entry_widgets[f"min_entry{i}"].text()
               max_v = self.entry_widgets[f"max_entry{i}"].text()
               term  = self.entry_widgets[f"ter_selection_var{i}"].currentText()
               mx    = self.entry_widgets[f"mx_plus_entry{i}"].text()
               b_val = self.entry_widgets[f"b_entry{i}"].text()
               units = self.strip_value(self.entry_widgets[f"units_entry{i}"].text())

           data_in.append({
               'Channel':                self.entry_widgets[f"Channel_entry{i}"].text(),
               'DAQ':                    self._row_dev(i, 'ai'),
               'Input Type':             self._w_text(f"ai_type{i}") or _DEFAULT_TYPE,
               'Transducer':             trans_name,
               'Min':                    min_v,
               'Max':                    max_v,
               'Range Unit':             unit,
               'Terminal Configuration': term,
               'Mx':                     mx,
               'b':                      b_val,
               'Scaled Units':           units,
               'Device Name':            self.strip_value(self.entry_widgets[f"Device_name{i}"].text()),
               'Selected Channel':       int(selected),
           })

       # ── Output channels ──────────────────────────────────────────
       for i in range(1, ao_count + 1):
           selected = self.check_vars.get(f"Selected Channel_output{i}", QCheckBox()).isChecked()
           o_min = self.entry_widgets[f"min_entry_output{i}"].text()
           o_max = self.entry_widgets[f"max_entry_output{i}"].text()
           data_out.append({
               'Channel':          self.entry_widgets[f"Channel_entry_output{i}"].text(),
               'DAQ':              self._row_dev(i, 'ao'),
               'Output Type':      self._w_text(f"output_type{i}") or _DEFAULT_TYPE,
               'Min':              o_min,
               'Max':              o_max,
               'Range Unit':       self._row_unit(i, 'ao'),
               'Device Name':      self.strip_value(self.entry_widgets[f"Device_name_output{i}"].text()),
               'Selected Channel': int(selected),
           })

       # ── Custom channels ──────────────────────────────────────────
       for i in range(1, self.no_of_cus_chans + 1):
           selected    = self.check_vars.get(f"Selected Channel_Custom{i}", QCheckBox()).isChecked()
           first_chan  = self.entry_widgets[f"first_chan_sel{i}"].currentText()
           operation   = self.entry_widgets[f"operation_sel{i}"].text().strip()
           # B dropdown: "None" means no second channel
           second_chan = self.entry_widgets[f"sec_chan_sel{i}"].currentText()
           if second_chan == "None":
               second_chan = ""
           name        = self.strip_value(self.entry_widgets[f"custom_chan_name{i}"].text())
           units       = self.strip_value(self.entry_widgets[f"cust_units_entry{i}"].text())

           if selected and not operation:
               QMessageBox.warning(self, "Missing Expression",
                                   f"Custom channel {i} is selected but has no expression.\n"
                                   f"Enter an expression or deselect the channel.")
               return
           elif selected and operation:
               err = _validate_custom_expression(operation)
               if err:
                   QMessageBox.warning(self, "Expression Error",
                                       f"Custom channel {i}: {err}")
                   return
               # B / expression consistency is checked here rather than while
               # typing, so B can be picked before the expression is written.
               _tokens = set(re.findall(r'[A-Za-z_][A-Za-z0-9_]*', operation))
               _uses_b = "B" in _tokens
               if _uses_b and not second_chan:
                   QMessageBox.warning(self, "Missing B Channel",
                                       f"Custom channel {i}: the expression "
                                       f"uses B but no B channel is selected.")
                   return
               if second_chan and not _uses_b:
                   # B was picked but the expression never uses it -> drop it
                   # back to None. This is the only place B is reset.
                   _b_cb = self.entry_widgets[f"sec_chan_sel{i}"]
                   _b_cb.blockSignals(True)
                   _b_cb.setCurrentText("None")
                   _b_cb.blockSignals(False)
                   second_chan = ""

           data_cus.append({
               'Channel':         self.entry_widgets[f"Channel_entry_cust{i}"].text(),
               'First Channel':   first_chan,
               'Operation':       operation,
               'Second Channel':  second_chan,
               'Device Name':     name,
               'Units':           units,
               'Selected Channel': int(selected),
           })

       # ── Digital channels ─────────────────────────────────────────
       dig_count = sum(1 for k in self.entry_widgets if k.startswith("dig_channel_entry"))
       for i in range(1, dig_count + 1):
           selected = self.check_vars.get(f"Selected Channel_Digital{i}", QCheckBox()).isChecked()
           data_dig.append({
               'Channel':          self.entry_widgets[f"dig_channel_entry{i}"].text(),
               'DAQ':              self._row_device_name("dig_DAQ", i),
               'Direction':        self.entry_widgets[f"dig_direction_sel{i}"].currentText(),
               'Device Name':      self.strip_value(self.entry_widgets[f"dig_device_name{i}"].text()),
               'Selected Channel': int(selected),
           })

       # ── Expert settings ──────────────────────────────────────────
       use_def = int(self.use_default_chk.isChecked())
       def _add(k, v):
           data_expert.append({"Setting": k, "Value": v, "Use Default": use_def})

       _add("Sample Rate (Hz)",    self.sample_rate_entry.text())
       _add("AO Sample Rate (Hz)", self.ao_sample_rate_entry.text())
       _add("Max Buffer Size",     self.max_buffer_entry.text())
       _add("Refresh Rate (Hz)",   self.refresh_entry.text())
       _add("Max Plot Points",     self.max_points_entry.text())
       _add("Duration to Acquire",      self.duration_acquire_entry.text())
       _add("Duration to Acquire Unit", self.duration_acquire_unit.currentText())
       _add("Duration to Plot",         self.duration_plot_entry.text())
       _add("Duration to Plot Unit",    self.duration_plot_unit.currentText())
       _add("Decimate To Fit",     "1" if self.decimate_chk.isChecked() else "0")
       _add("Filter Mode",         self.filter_mode_cb.currentText())
       _add("Filter Type",         self.filter_type_cb.currentText())
       _add("Filter Order",        str(self.filter_order_sp.value()))
       _add("Filter Cutoff (Hz)",  self.filter_fc_entry.text())
       _add("Filter fh (Hz)",      self.filter_fh_entry.text())
       _add("Filter Channels",     self._filter_channels_value())
       _add("Cache to Disk",       "1" if self.cache_to_disk_chk.isChecked() else "0")
       _add("Cache Interval (s)",  self.cache_interval_entry.text())
       _add("Theme",               self.theme_cb.currentText())

       # ── Transducers ──────────────────────────────────────────────
       for i in range(1, self.no_of_transducers + 1):
           data_trans.append({
               "Hardware Model":         self.strip_value(self.entry_widgets[f"trans_hw{i}"].text()),
               "Hardware Type":          self.strip_value(self.entry_widgets[f"trans_type{i}"].text()),
               "Min Output Range":       self.entry_widgets[f"trans_minv{i}"].text(),
               "Max Output Range":       self.entry_widgets[f"trans_maxv{i}"].text(),
               "Output Unit":            self._trans_output_unit(i),
               "Min Signal Input":       self.entry_widgets[f"trans_min_sig{i}"].text(),
               "Max Signal Input":       self.entry_widgets[f"trans_max_sig{i}"].text(),
               "Terminal Configuration": self.entry_widgets[f"trans_term{i}"].currentText(),
               "Mx":                     self.entry_widgets[f"trans_mx{i}"].text(),
               "B":                      self.entry_widgets[f"trans_b{i}"].text(),
               "Scaled Units":           self.strip_value(self.entry_widgets[f"trans_units{i}"].text()),
           })

       # ── Key mappings ─────────────────────────────────────────────
       data_keys = [
           {"Action": a, "Key": self._key_bindings.get(a, "")}
           for a in self.KEY_MAP_ACTIONS
       ]

       # ── App Settings: FG custom functions + plot lines ───────────
       fg_fields = ["Slot", "Type", "Name", "X Unit", "Points", "Expression"]
       data_fg = []
       for i, row in enumerate(getattr(self, "_fg_wave_rows", [])):
           data_fg.append({
               "Slot": f"wave{i+1}", "Type": "waveform",
               "Name": self.strip_value(row["name"].text()),
               "X Unit": row["xunit"].currentText(),
               "Points": row["points"].text().strip(),
               "Expression": "",
           })
       for i, row in enumerate(getattr(self, "_fg_math_rows", [])):
           data_fg.append({
               "Slot": f"math{i+1}", "Type": "math",
               "Name": self.strip_value(row["name"].text()),
               "X Unit": "", "Points": "",
               "Expression": row["expr"].text().strip(),
           })

       pl_fields = ["Enabled", "Label", "Axis", "Limit",
                    "Op", "Value", "Op2", "Value2",
                    "Unit", "Color", "Style"]
       data_pl = []
       for row in getattr(self, "_plot_line_rows", []):
           is_double = (row["limit"].currentText() == "Double")
           op1 = row["op"].currentText()
           op2 = row["op2"].currentText() if is_double else ""
           # Store '=' as blank: a leading '=' in an Excel cell is read back as a
           # formula and would be corrupted. Blank is reloaded as '='.
           if op1 == "=":
               op1 = ""
           if op2 == "=":
               op2 = ""
           data_pl.append({
               "Enabled": int(row["enable"].isChecked()),
               "Label":   self.strip_value(row["label"].text()),
               "Axis":    row["axis"].currentText(),
               "Limit":   row["limit"].currentText(),
               "Op":      op1,
               "Value":   row["value"].text().strip(),
               "Op2":     op2,
               "Value2":  row["value2"].text().strip() if is_double else "",
               "Unit":    row["unit"].currentText().strip(),
               "Color":   row["color"].currentText(),
               "Style":   row["style"].currentText(),
           })

       # ── App Settings: axis ranges ────────────────────────────────
       # One row per unit of the selected channels; Min/Max only for Custom.
       ar_fields = ["Unit", "Range", "Min", "Max"]
       data_ar = []
       for unit, row in getattr(self, "_axis_range_rows", {}).items():
           custom = row["mode"].currentText() == "Custom"
           data_ar.append({
               "Unit":  unit,
               "Range": row["mode"].currentText(),
               # Validated before save, so Custom's fields are numbers.
               "Min":   float(row["min"].text()) if custom else "",
               "Max":   float(row["max"].text()) if custom else "",
           })

       # ── App Settings: App Bar visibility + plugins ───────
       # Built-in rows have an empty Plugin; a plugin row names its folder in
       # the Plugins folder, and DAQ_AppRegistry loads it at startup only if
       # Enabled.
       ab_fields = ["App", "Label", "Show", "Plugin", "Enabled"]
       data_ab = [{"App": key, "Label": label,
                   "Show": int(self._app_bar_checks[key].isChecked()),
                   "Plugin": "", "Enabled": ""}
                  for key, label, _default_on in self._app_bar_builtin]
       data_ab += [{"App": r['id'], "Label": r['label'],
                    "Show": int(bool(r['show'])), "Plugin": r['name'],
                    "Enabled": int(bool(r['enabled']))}
                   for r in self._plugin_rows]

       # ── App Settings: shortcuts ─────────────────────────
       sc_fields = ["Name", "Path", "Show"]
       data_sc = [{"Name": r['name'].strip(), "Path": r['path'],
                   "Show": int(bool(r['show']))}
                  for r in self._shortcut_rows]

       # ── Write Excel ──────────────────────────────────────────────
       dest = path or _get_settings_path()
       try:
           is_default = os.path.abspath(dest) == os.path.abspath(_get_settings_path())
       except Exception:
           is_default = (path is None)
       try:
           with pd.ExcelWriter(dest) as writer:
               pd.DataFrame(data_in,     columns=fieldnames_in ).to_excel(writer, sheet_name="input_settings",         index=False)
               pd.DataFrame(data_out,    columns=fieldnames_out).to_excel(writer, sheet_name="output_settings",         index=False)
               pd.DataFrame(data_cus,    columns=fieldnames_cus).to_excel(writer, sheet_name="custom_channel_settings", index=False)
               pd.DataFrame(data_dig,    columns=fieldnames_dig).to_excel(writer, sheet_name="digital_settings",        index=False)
               pd.DataFrame(data_expert, columns=["Setting", "Value", "Use Default"]).to_excel(writer, sheet_name="expert_settings",  index=False)
               pd.DataFrame(data_trans,  columns=trans_fields  ).to_excel(writer, sheet_name="transducers_settings",    index=False)
               pd.DataFrame(data_keys,   columns=["Action", "Key"]).to_excel(writer, sheet_name="key_mappings",         index=False)
               pd.DataFrame(data_fg,     columns=fg_fields      ).to_excel(writer, sheet_name="app_fg_functions",       index=False)
               pd.DataFrame(data_pl,     columns=pl_fields      ).to_excel(writer, sheet_name="app_plot_lines",         index=False)
               pd.DataFrame(data_ab,     columns=ab_fields      ).to_excel(writer, sheet_name="app_bar",                index=False)
               pd.DataFrame(data_sc,     columns=sc_fields      ).to_excel(writer, sheet_name="app_shortcuts",          index=False)
               pd.DataFrame(data_ar,     columns=ar_fields      ).to_excel(writer, sheet_name="axis_ranges",            index=False)

           msg = f"Settings saved to {os.path.basename(dest)}"
           if is_default and self._plugin_set_changed():
               msg += "\n\nEnabled or disabled plugins take effect after restarting."
           QMessageBox.information(self, "Saved", msg)
           # Only a save to the active settings file changes the running config,
           # so only then do we tell the main app to reload. A 'Save As' copy to
           # another location leaves the active configuration untouched.
           if is_default:
               self.settings_saved.emit()
       except Exception as e:
           QMessageBox.critical(self, "Error", f"Failed to save settings:\n{e}")

   def save_settings_as(self):
       """Save the current settings to a user-chosen file/location (a copy).
       Does not change the active configuration — use 'Load Settings' to make a
       saved file active later."""
       if not self._validate_before_save():
           return
       start_dir = os.path.dirname(_get_settings_path()) or ""
       suggested = os.path.join(start_dir, "settings.xlsx")
       path, _ = QFileDialog.getSaveFileName(
           self, "Save Settings As", suggested, "Excel files (*.xlsx)")
       if not path:
           return
       if not path.lower().endswith(".xlsx"):
           path += ".xlsx"
       self.save_settings(path=path)

   # ------------------------------------------------------------------
   # Key Mapping tab
   # ------------------------------------------------------------------

   # Actions that can be bound to a key.  Order defines display order.
   KEY_MAP_ACTIONS = ["Pen", "Run", "Stop", "Clear Graph", "Save Data"]

   def _build_keymap_tab(self):
       """Build the Key Mapping tab."""
       from PyQt5.QtWidgets import QSizePolicy as QSP

       outer = QVBoxLayout(self.tab_keymap)
       outer.setAlignment(Qt.AlignTop)

       grid = QGridLayout()
       grid.setColumnStretch(1, 1)
       outer.addLayout(grid)

       headers = ["Action", "Assigned Key", "", ""]
       for col, h in enumerate(headers):
           lbl = QLabel(f"<b>{h}</b>")
           grid.addWidget(lbl, 0, col)

       # Storage: action -> current key string (e.g. "F5", "Space", "R")
       if not hasattr(self, '_key_bindings'):
           self._key_bindings = {a: "" for a in self.KEY_MAP_ACTIONS}

       self._key_display = {}   # action -> QLabel showing current key

       for row, action in enumerate(self.KEY_MAP_ACTIONS, start=1):
           # Action name
           grid.addWidget(QLabel(action), row, 0)

           # Key display
           cur = self._key_bindings.get(action, "") or "None"
           disp = QLabel(f"<b>{cur}</b>")
           disp.setMinimumWidth(80)
           disp.setFrameStyle(1)
           disp.setAlignment(Qt.AlignCenter)
           disp.setSizePolicy(QSP.Expanding, QSP.Fixed)
           grid.addWidget(disp, row, 1)
           self._key_display[action] = disp

           # Assign button
           assign_btn = QPushButton("Assign Key")
           assign_btn.setCheckable(True)   # stays pressed while listening
           assign_btn.clicked.connect(
               lambda checked, a=action, b=assign_btn: self._start_key_capture(a, b)
           )
           grid.addWidget(assign_btn, row, 2)

           # Clear button
           clear_btn = QPushButton("Clear")
           clear_btn.clicked.connect(lambda _, a=action: self._clear_key_binding(a))
           grid.addWidget(clear_btn, row, 3)

       outer.addStretch()

       note = QLabel(
           "Click <b>Assign Key</b>, then press any key on the keyboard to bind it to that action. "
           "Keys only fire when the corresponding button is enabled in the main window."
       )
       note.setWordWrap(True)
       outer.addWidget(note)

   def _start_key_capture(self, action, btn):
       """Install a temporary event filter on the QApplication to grab the next keypress.

       Filtering at the application level (rather than the window) means the
       filter sees the event before any focused child widget (button, combobox…)
       can swallow it, so keys like Space and Enter are capturable.
       """
       from PyQt5.QtCore import QObject, QEvent
       from PyQt5.QtWidgets import QApplication

       if not btn.isChecked():
           btn.setText("Assign Key")
           return

       btn.setText("Press a key…")

       class _KeyCatcher(QObject):
           def __init__(self, outer, action, btn, display):
               super().__init__(outer)
               self._outer   = outer
               self._action  = action
               self._btn     = btn
               self._display = display

           def eventFilter(self, obj, event):
               if event.type() == QEvent.KeyPress:
                   key_name = self._get_key_name(event)
                   if key_name:
                       # Check for conflicts
                       conflict = None
                       for a, k in self._outer._key_bindings.items():
                           if k == key_name and a != self._action:
                               conflict = a
                               break
                       if conflict:
                           QMessageBox.warning(
                               self._outer,
                               "Key Already Assigned",
                               f"<b>{key_name}</b> is already assigned to <b>{conflict}</b>.\n"
                               "Please choose a different key or clear that binding first."
                           )
                       else:
                           self._outer._key_bindings[self._action] = key_name
                           self._display.setText(f"<b>{key_name}</b>")

                   self._btn.setChecked(False)
                   self._btn.setText("Assign Key")
                   QApplication.instance().removeEventFilter(self)
                   self.deleteLater()
                   return True   # consume the event so it doesn't trigger a button etc.
               return False

           @staticmethod
           def _get_key_name(event):
               from PyQt5.QtCore import Qt as _Qt
               key = event.key()

               # Bare modifier keys are not valid bindings
               if key in (_Qt.Key_Control, _Qt.Key_Shift,
                          _Qt.Key_Alt, _Qt.Key_Meta):
                   return ""

               # Named special keys
               named = {
                   _Qt.Key_F1: "F1",   _Qt.Key_F2: "F2",   _Qt.Key_F3: "F3",
                   _Qt.Key_F4: "F4",   _Qt.Key_F5: "F5",   _Qt.Key_F6: "F6",
                   _Qt.Key_F7: "F7",   _Qt.Key_F8: "F8",   _Qt.Key_F9: "F9",
                   _Qt.Key_F10: "F10", _Qt.Key_F11: "F11", _Qt.Key_F12: "F12",
                   _Qt.Key_Space:     "Space",
                   _Qt.Key_Return:    "Return",
                   _Qt.Key_Enter:     "Enter",
                   _Qt.Key_Escape:    "Escape",
                   _Qt.Key_Tab:       "Tab",
                   _Qt.Key_Backspace: "Backspace",
                   _Qt.Key_Delete:    "Delete",
                   _Qt.Key_Insert:    "Insert",
                   _Qt.Key_Home:      "Home",
                   _Qt.Key_End:       "End",
                   _Qt.Key_PageUp:    "PageUp",
                   _Qt.Key_PageDown:  "PageDown",
                   _Qt.Key_Up:        "Up",
                   _Qt.Key_Down:      "Down",
                   _Qt.Key_Left:      "Left",
                   _Qt.Key_Right:     "Right",
               }
               if key in named:
                   return named[key]

               # Letters A-Z (no modifier prefix — Shift+A == A)
               if _Qt.Key_A <= key <= _Qt.Key_Z:
                   return chr(key)

               # Digits 0-9
               if _Qt.Key_0 <= key <= _Qt.Key_9:
                   return chr(key)

               return ""

       catcher = _KeyCatcher(self, action, btn, self._key_display[action])
       QApplication.instance().installEventFilter(catcher)

   def _clear_key_binding(self, action):
       self._key_bindings[action] = ""
       if action in self._key_display:
           self._key_display[action].setText("<b>None</b>")

   def get_key_bindings(self):
       """Return a copy of the current key-binding dict (action -> key string)."""
       return dict(self._key_bindings)

   # ------------------------------------------------------------------
   # Close
   # ------------------------------------------------------------------

   def close_window(self):
       self.window().close()


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class DAQSettingsWindow(QMainWindow):
   def __init__(self):
       super().__init__()
       self.setWindowTitle("DAQ Settings")
       self.resize(1000, 680)

       central = QWidget()
       lay = QVBoxLayout(central)
       self.cs = DAQSettingsWidget(central, "Channel Settings")
       lay.addWidget(self.cs)
       self.setCentralWidget(central)


def main():
   app = QApplication(sys.argv)
   win = DAQSettingsWindow()
   win.show()
   sys.exit(app.exec_())


if __name__ == "__main__":
   main()
