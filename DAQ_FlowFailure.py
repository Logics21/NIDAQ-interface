
"""
DAQ_FlowFailure.py
------------------
Flow Failure — Valve Operating Envelope Test
Part of GioDAQ.
"""

import time
import threading
import traceback
import warnings
import pandas as pd

import numpy as np
import pyqtgraph as pg
from pyqtgraph import mkPen
import nidaqmx
import nidaqmx.constants as ni
from nidaqmx.stream_writers import AnalogSingleChannelWriter
try:
   from nidaqmx.errors import DaqWarning
except ImportError:      # older nidaqmx layouts
   DaqWarning = Warning

from PyQt5.QtWidgets import (
   QDialog, QVBoxLayout, QHBoxLayout, QGridLayout,
   QLabel, QLineEdit, QComboBox, QPushButton, QGroupBox,
   QCheckBox, QMessageBox, QSizePolicy,
   QRadioButton, QButtonGroup, QPlainTextEdit, QApplication
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal

# Shared implementation, aliased to the name this module already used so no
# call site changes. write_dc was extracted from this file into DAQ_AppBase,
# but this module was never rewired to it and kept running its own copy.
from DAQ_Settings import add_ao_channel, unit_quantity, unit_affine

# Shared implementations, aliased to the names this module already used so no
# call site changes. These were extracted FROM this file into DAQ_AppBase, but
# it was never rewired to them and kept running its own copies.
from DAQ_AppBase import (
    AOTestDialog, AppSignals,
    write_dc as _write_dc,
    safe_str as _safe_str,
    hline as _hline,
    GroupBox as _GroupBox,
)


# ---------------------------------------------------------------------------
# Signal bus
# ---------------------------------------------------------------------------

class _FFSignals(AppSignals):
   """The common signals from AppSignals, plus this app's own.

   status_changed, enable_run, test_finished, stop_daq_sig and controls_unlock
   all come from the base class.
   """
   point_recorded  = pyqtSignal(float, float, str)   # pressure, flow, label
   model_ready_sig = pyqtSignal()
   calib_done_sig  = pyqtSignal(bool)  # True = calibration succeeded (UI thread)


# ---------------------------------------------------------------------------
# Low-level DAQ helpers
# ---------------------------------------------------------------------------

def _format_do_channel(ch: str) -> str:
   """Convert NI-style digital channel string to compact P-dot notation.

   Examples:
       'port0/line0'  → 'P0.0'
       'port1/line3'  → 'P1.3'
       'port0/line12' → 'P0.12'

   Falls back to the original string if it doesn't match the pattern.
   """
   import re
   m = re.match(r'port(\d+)/line(\d+)', ch.strip().lower())
   if m:
       return f"P{m.group(1)}.{m.group(2)}"
   return ch


# ---------------------------------------------------------------------------
# Dialog
# ---------------------------------------------------------------------------

class FlowFailureDialog(AOTestDialog):

   _MODEL_COLOR  = '#2ecc71'   # dashed green  — threshold model curve
   _MODEL_DN_COLOR = '#9b59b6' # dashed purple — sweep-down threshold model curve
   _RESULT_COLOR = '#e74c3c'   # red circles   — envelope result points
   _ANCHOR_COLOR = '#f39c12'   # orange squares — (0, max_P) and (max_F, 0) anchors

   # AO update rate for the continuous tuning sweeps. 1 kHz is far finer than
   # any control valve responds to, so the command the rig sees is a smooth
   # ramp rather than a staircase; the cost is one short array per sweep.
   _SWEEP_AO_RATE = 1000.0
   _MODEL_REF_P_PSI = 1000.0   # dead-head pressure the ΔP/ΔQ model aims for, in psi
   _LABEL_COL_W   = 150        # fixed width of the left label column (alignment)

   SIGNALS_CLASS = _FFSignals
   APP_NAME      = "Flow Failure"

   def __init__(self, main_window, default_settings_output, default_settings_input,
                default_settings_dig, selected_daq, sample_rate, parent=None):
       # The base class holds the window flags, the settings frames, the
       # run/abort flags and the signal bus, and connects status_changed and
       # stop_daq_sig; only this app's own state and extra signals are below.
       super().__init__(main_window, default_settings_output,
                        default_settings_input, default_settings_dig,
                        selected_daq, sample_rate, parent=parent,
                        title="Flow Failure — Valve Envelope Test")
       self.setMinimumWidth(560)

       self._model           = {}      # active model (points to _model_up or _model_down)
       self._model_up        = {}      # gpm → pressure, AO1 swept UP   (0 → max)
       self._model_down      = {}      # gpm → pressure, AO1 swept DOWN (max → 0)
       self._model_ready     = False
       self._result_pressure = []
       self._result_flow     = []
       self._result_labels   = []

       # ── Pressure tuning-sweep state ───────────────────────────────────
       # Built by Pressure Tuning Sweep: one continuous 0 → max → 0 ramp of
       # the Fixed Test Point AO, driven from a finite hardware-timed task,
       # with the test valve closed and the flow valve held mid-range.
       #
       # Only the UP leg feeds the seed curve. The command→pressure map is a
       # hysteresis-bearing relationship, so mixing the two legs would give a
       # voltage that belongs to neither branch; the run always approaches a
       # setpoint from below, so the up leg is the branch that matches it.
       #
       # The seed is a starting guess only — the closed-loop ramp always
       # verifies against the gauge and never commands this value blind.
       self._swept           = False
       self._press_seed_v    = None   # np.ndarray — AO voltages (up leg)
       self._press_seed_p    = None   # np.ndarray — Y-gauge pressure at each
       self._max_p_reachable = None   # float — peak pressure seen (INFORMATIONAL)
       self._run_log         = []     # list[str] — TROUBLESHOOTING action log
       self._sweep_sig       = None   # snapshot of (Y gauge, valve type) at sweep time

       # ── Flow seed state ───────────────────────────────────────────────
       # Built during Generate ΔP/ΔQ Model by sweeping the Variable Test
       # Point AO channel (AO1) and reading the X-axis flow meter. Used only
       # to seed the closed-loop flow ramp. Taken from the UP sweep (the run
       # test always searches flow upward).
       self._flow_seeded     = False
       self._flow_seed_v     = None   # np.ndarray — AO1 voltages (ascending by flow)
       self._flow_seed_q     = None   # np.ndarray — flow read at each (ascending)
       self._max_q_reachable = None   # float — peak flow seen (INFORMATIONAL)

       # Pressure the ΔP/ΔQ model was actually built at. The model is only
       # interpretable relative to its reference, and the reference is now set
       # by commanding a voltage rather than by searching for a pressure, so
       # what the rig actually held has to be recorded rather than assumed.
       self._model_ref_p_meas = None
       self._model_leg_spread = None  # float — mean |up − down| across the grid

       self._model_item        = None   # pg.PlotDataItem — active threshold curve (legacy)
       self._model_item_up     = None   # pg.PlotDataItem — sweep-up model curve
       self._model_item_down   = None   # pg.PlotDataItem — sweep-down model curve
       self._result_item       = None   # pg.PlotDataItem — result scatter dots + line
       self._anchor_item       = None   # pg.PlotDataItem — (0,maxP) and (maxF,0) anchors
       self._envelope_line     = None   # pg.PlotDataItem — connecting line
       self._label_items       = []     # list of pg.TextItem — point value labels
       self._left_anchor_labeled = False
       self._n_anchor_labels   = 0

       self._expert_win = None   # QDialog — built in __init__ below

       # status_changed and stop_daq_sig are already connected by the base.
       self._signals.point_recorded.connect(self._on_point_recorded)
       self._signals.model_ready_sig.connect(self._on_model_ready)
       self._signals.enable_run.connect(self._run_btn_set_enabled)
       self._signals.test_finished.connect(self._on_test_finished)
       self._signals.controls_unlock.connect(lambda: self._set_controls_running(False))
       self._signals.calib_done_sig.connect(self._on_calib_done)

       self._ao_count = len(default_settings_output) if default_settings_output is not None else 0

       # Built BEFORE _build_ui: the expert window owns _ao_settle_flow,
       # _actuation_*, and the model-direction radios, and _build_ui →
       # _populate_channels reaches for those by name. Constructing it second
       # would leave them undefined during the first _set_controls_running /
       # _sync_active_model pass.
       self._expert_win = self._build_expert_window()

       self._build_ui()
       self._populate_channels()
       self._lock_main_window()
       self._install_clear_hook()

       if self._ao_count < 2:
           QTimer.singleShot(200, self._warn_ao_count)

   # ------------------------------------------------------------------
   # UI
   # ------------------------------------------------------------------

   def _build_ui(self):
       root = QVBoxLayout(self)
       root.setSpacing(8)

       # AO count warning banner
       self._ao_warn_label = QLabel(
           "⚠  Only one AO channel is configured in Settings.\n"
           "    Flow Failure requires two AO channels (Fixed + Variable)."
       )
       self._ao_warn_label.setStyleSheet(
           "color: white; background: #c0392b; padding: 6px; border-radius: 3px;"
       )
       self._ao_warn_label.setAlignment(Qt.AlignCenter)
       self._ao_warn_label.setVisible(self._ao_count < 2)
       root.addWidget(self._ao_warn_label)

       # ── Channels selection ────────────────────────────────────────────
       grp = QGroupBox("Channels selection")
       gl  = QGridLayout(grp)
       gl.setColumnMinimumWidth(0, self._LABEL_COL_W)
       gl.setColumnStretch(1, 1)

       gl.addWidget(QLabel("Valve Type:"), 0, 0)
       self._valve_no_nc = QComboBox()
       self._valve_no_nc.addItem("Normally Open")
       self._valve_no_nc.addItem("Normally Closed")
       self._valve_no_nc.setCurrentIndex(1)   # default: Normally Closed
       self._valve_no_nc.setMinimumWidth(160)
       self._valve_no_nc.setToolTip(
           "Normally Open: True = Close valve  |  Normally Closed: True = Open valve"
       )
       gl.addWidget(self._valve_no_nc, 0, 1, 1, 2)
       # Valve type affects the sweep (NC = de-energized, NO = energized), so
       # changing it requires re-running the pressure tuning sweep.
       self._valve_no_nc.currentIndexChanged.connect(self._on_valve_type_changed)

       gl.addWidget(QLabel("Test Valve Trigger:"), 1, 0)
       self._do_combo = QComboBox()
       self._do_combo.setMinimumWidth(260)
       gl.addWidget(self._do_combo, 1, 1, 1, 2)

       # Y-Axis pressure gauge (AI) — directly below the Test Valve Trigger row.
       gl.addWidget(QLabel("Y-Axis Pressure Gauge:"), 2, 0)
       self._y_gauge_combo = QComboBox()
       self._y_gauge_combo.setMinimumWidth(220)
       self._y_gauge_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
       self._y_gauge_combo.setToolTip(
           "Channel to read as the gauge measurement.\n"
           "Only pressure channels (psi, bar, kPa, ...) are shown.\n"
           "Also sets the Y-Axis in Multichannel_DAQ."
       )
       gl.addWidget(self._y_gauge_combo, 2, 1, 1, 2)
       self._y_gauge_combo.currentIndexChanged.connect(self._on_y_gauge_changed)

       # X-Axis flow meter (AI).
       gl.addWidget(QLabel("X-Axis Flow Meter:"), 3, 0)
       self._x_gauge_combo = QComboBox()
       self._x_gauge_combo.setMinimumWidth(220)
       self._x_gauge_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
       self._x_gauge_combo.setToolTip(
           "Channel to use as the flow measurement.\n"
           "Only flow channels (gpm, lpm, L/min, ...) are shown.\n"
           "Also sets the X-Axis in Multichannel_DAQ."
       )
       gl.addWidget(self._x_gauge_combo, 3, 1, 1, 2)
       self._x_gauge_combo.currentIndexChanged.connect(self._on_x_gauge_changed)

       # AO output channel selectors — the two AO roles are now user-chosen
       # instead of auto-assigned to output rows 0 and 1.
       gl.addWidget(QLabel("Fixed Test Point AO:"), 4, 0)
       self._ao_fixed_combo = QComboBox()
       self._ao_fixed_combo.setMinimumWidth(220)
       self._ao_fixed_combo.setToolTip(
           "AO channel driven to the fixed test-point (supply / pressure) voltage."
       )
       gl.addWidget(self._ao_fixed_combo, 4, 1, 1, 2)

       gl.addWidget(QLabel("Variable / Sweep AO:"), 5, 0)
       self._ao_var_combo = QComboBox()
       self._ao_var_combo.setMinimumWidth(220)
       self._ao_var_combo.setToolTip(
           "AO channel used for the variable / swept test signal."
       )
       gl.addWidget(self._ao_var_combo, 5, 1, 1, 2)

       # role -> combo lookup used by _get_ao_row()
       self._ao_role_combos = {0: self._ao_fixed_combo, 1: self._ao_var_combo}
       self._ao_fixed_combo.currentIndexChanged.connect(self._on_ao_selection_changed)
       self._ao_var_combo.currentIndexChanged.connect(self._on_ao_selection_changed)

       root.addWidget(grp)

       # ── Fixed Test Point ──────────────────────────────────────────────
       grp = _GroupBox("Fixed Test Point (Pressure / Supply)")
       self._fixed_grp = grp
       gl  = QGridLayout(grp)
       gl.setColumnMinimumWidth(0, self._LABEL_COL_W)
       gl.setColumnStretch(1, 1)

       # Test points are entered in the SELECTED Y-AXIS TRANSDUCER UNITS
       # (e.g. psi). The unit suffix in the label is updated live whenever the
       # Y-axis gauge selection changes (see _on_y_gauge_changed).
       self._test_points_label = QLabel("Test Points (comma-separated, units):")
       gl.addWidget(self._test_points_label, 0, 0, 1, 3)
       self._pressure_points = QLineEdit("")
       # Blank by default with the example as a placeholder: a pre-filled list
       # is a set of real pressures the rig will be driven to, and shipping one
       # invites running someone else's numbers by accident.
       self._pressure_points.setPlaceholderText("e.g.  1000, 2000, 3000, 4000, 5000")
       self._pressure_points.setToolTip(
           "Pressure setpoints in the selected Y-axis transducer units.\n\n"
           "Sorted ascending and de-duplicated automatically — the test always\n"
           "walks pressure upward, never up and back down.\n"
           "A point the rig cannot reach is recorded as unreached after the\n"
           "pressure ramp times out; nothing is dropped in advance."
       )
       # Normalise (sort ascending, de-duplicate) once editing finishes rather
       # than on every keystroke, so the field doesn't reorder mid-typing.
       self._pressure_points.editingFinished.connect(self._normalize_test_points)
       self._pressure_points.textChanged.connect(self._on_test_points_changed)
       gl.addWidget(self._pressure_points, 1, 0, 1, 2)

       # Capability sweep button sits next to the Test Points field. Its label
       # shows the selected AI channel; the suffix is refreshed live in
       # _refresh_units_label whenever the Y-axis gauge changes.
       self._sweep_btn = QPushButton("Pressure Tuning Sweep AO0 → AI")
       self._sweep_btn.setMaximumWidth(220)
       self._sweep_btn.setFixedHeight(24)
       self._sweep_btn.setToolTip(
           "One continuous 0 → max → 0 ramp of the Fixed Test Point AO with\n"
           "the test valve closed and the flow valve held mid-range.\n\n"
           "Produces the pre-tuned seed voltage for each pressure test point.\n"
           "The closed-loop ramp always verifies against the gauge, so these\n"
           "are starting guesses only — never the final command.\n\n"
           "Re-run only if you change the Y-axis gauge or the valve type."
       )
       self._sweep_btn.clicked.connect(self._on_pressure_tuning_sweep)
       gl.addWidget(self._sweep_btn, 1, 2, Qt.AlignRight | Qt.AlignVCenter)

       # Read-only pre-tuned seed voltage readout: the command each test point
       # will START its closed-loop ramp from, interpolated off the tuning
       # sweeps. Not the final command -- the ramp verifies against the gauge.
       self._range_label = QLabel("Pre-tuned voltage points:")
       gl.addWidget(self._range_label, 2, 0, 1, 3)
       self._range_field = QLineEdit("")
       self._range_field.setReadOnly(True)
       self._range_field.setPlaceholderText(
           "Run Pressure Tuning Sweep (pressure) and Generate \u0394P/\u0394Q Model (flow)")
       self._range_field.setStyleSheet("background: #e9e9e9; color: #555;")
       self._range_field.setToolTip(
           "Seed command voltage for each test point, read off the tuning\n"
           "sweeps (read-only).\n\n"
           "The closed-loop ramp starts one step below these and verifies\n"
           "against the gauge, so a seed that is slightly off costs a little\n"
           "time, not accuracy."
       )
       gl.addWidget(self._range_field, 3, 0, 1, 3)

       root.addWidget(grp)

       # ── Variable Test Point ───────────────────────────────────────────
       grp = _GroupBox("Variable Test Point (Flow / Control)")
       self._var_grp = grp
       gl  = QGridLayout(grp)
       gl.setColumnMinimumWidth(0, self._LABEL_COL_W)
       gl.setColumnStretch(1, 1)

       sweep = QHBoxLayout()
       self._flow_start = QLineEdit("0.0"); self._flow_start.setMaximumWidth(70)
       self._flow_step  = QLineEdit("");    self._flow_step.setMaximumWidth(70)
       self._flow_max   = QLineEdit("");    self._flow_max.setMaximumWidth(70)
       # Step and Max blank for the same reason as the pressure points. Start
       # keeps its 0.0: it is a floor rather than a value the rig is driven to,
       # and a blank there just means "from zero" anyway.
       self._flow_step.setPlaceholderText("e.g. 1")
       self._flow_max.setPlaceholderText("e.g. 10")
       # Labels carry the selected X-axis flow-meter units (gpm/lpm/ccm),
       # refreshed live in _refresh_flow_labels.
       self._flow_start_lbl = QLabel("Start (units)")
       self._flow_step_lbl  = QLabel("Step (units)")
       self._flow_max_lbl   = QLabel("Max (units)")
       for lbl, w in [(self._flow_start_lbl, self._flow_start),
                       (self._flow_step_lbl,  self._flow_step),
                       (self._flow_max_lbl,   self._flow_max)]:
           sweep.addWidget(lbl); sweep.addWidget(w)
       gl.addLayout(sweep, 0, 0, 1, 2)
       # Editing the flow sweep invalidates the model (it is built on this grid).
       for w in (self._flow_start, self._flow_step, self._flow_max):
           w.textChanged.connect(self._on_flow_settings_changed)

       vs_row = QHBoxLayout()
       self._var_start_chk = QCheckBox("Per-point start (units):")
       self._var_starts    = QLineEdit("")
       self._var_starts.setPlaceholderText("one per pressure point, e.g. 1, 1, 1")
       self._var_starts.setEnabled(False)
       self._var_start_chk.toggled.connect(self._on_var_start_toggled)
       self._var_starts.textChanged.connect(self._on_flow_settings_changed)
       vs_row.addWidget(self._var_start_chk); vs_row.addWidget(self._var_starts)
       gl.addLayout(vs_row, 1, 0, 1, 2)

       root.addWidget(grp)

       # ── Test Settings ─────────────────────────────────────────────────
       grp = QGroupBox("Test Settings")
       gl  = QGridLayout(grp)
       gl.setColumnMinimumWidth(0, self._LABEL_COL_W)
       gl.setColumnMinimumWidth(3, 24)   # gap between the two field pairs
       gl.setColumnStretch(6, 1)         # trailing slack on the right

       # Generate ΔP/ΔQ Model — parked on the button row below the threshold.
       self._gen_btn = QPushButton("Generate \u0394P/\u0394Q Model")
       self._gen_btn.setMaximumWidth(190)
       self._gen_btn.setFixedHeight(24)
       self._gen_btn.clicked.connect(self._generate_model)
       self._gen_btn.setEnabled(False)   # enabled only after the tuning sweep
       self._gen_btn.setToolTip("Run the Pressure Tuning Sweep first to enable this.")

       # ── Threshold ─────────────────────────────────────────────────────
       # Both thresholds are ABSOLUTE pressures in Y-axis units, but they are
       # referenced to different baselines because they test different states:
       #   Open  → compared against the ΔP/ΔQ model  (valve open, flowing)
       #   Close → compared against the latched setpoint (valve seated)
       gl.addWidget(QLabel("Threshold:"), 1, 0)
       self._open_label = QLabel("Open (pressure, units)")
       gl.addWidget(self._open_label, 1, 1)
       self._open_pressure = QLineEdit("5"); self._open_pressure.setMaximumWidth(70)
       self._open_pressure.setToolTip(
           "Open-check tolerance as an ABSOLUTE pressure (Y-axis units).\n"
           "With the valve open and flowing, the reading must not sit more than\n"
           "this far ABOVE the \u0394P/\u0394Q model's expected pressure at that flow:\n"
           "    reading <= model(flow) + tolerance\n"
           "Reading too high \u21d2 the valve did not open / did not reopen."
       )
       gl.addWidget(self._open_pressure, 1, 2)
       self._close_label = QLabel("Close (pressure, units)")
       gl.addWidget(self._close_label, 1, 4)
       self._close_pressure = QLineEdit("5"); self._close_pressure.setMaximumWidth(70)
       self._close_pressure.setToolTip(
           "Close-check tolerance as an ABSOLUTE pressure (Y-axis units).\n"
           "With the valve seated, the reading must recover to within this much\n"
           "of the latched setpoint:\n"
           "    reading >= setpoint - tolerance\n"
           "Reading too low \u21d2 the valve did not close / did not reseat.\n"
           "Default 1 accounts for gauge resolution/accuracy."
       )
       gl.addWidget(self._close_pressure, 1, 5)

       # ── Button row: Expert Settings (gear) + Generate model ───────────
       # Timing, actuation and the evaluation-model choice live in the Expert
       # Settings window rather than here: they are set once for a rig and then
       # left alone, so keeping them on the main page pushed the controls that
       # actually change between runs off the bottom.
       btn_row = QHBoxLayout()
       self._expert_btn = QPushButton("\u2699  Expert Settings\u2026")
       self._expert_btn.setFixedHeight(24)
       self._expert_btn.setMaximumWidth(170)
       self._expert_btn.setToolTip(
           "Open Expert Settings: valve timing delays, actuation cycling,\n"
           "evaluation model direction, and the closed-loop search parameters.\n\n"
           "Defaults suit most rigs — open this when a ramp overshoots, hunts,\n"
           "or times out, or to change the valve cycling."
       )
       self._expert_btn.clicked.connect(self._open_expert_window)
       btn_row.addWidget(self._expert_btn)
       btn_row.addStretch(1)
       btn_row.addWidget(self._gen_btn)
       gl.addLayout(btn_row, 2, 0, 1, 7)

       root.addWidget(grp)

       root.addWidget(_hline())

       self._status_label = QLabel("Status: NOT READY  ❌")
       self._status_label.setAlignment(Qt.AlignCenter)
       self._status_label.setStyleSheet("font-weight: bold; padding: 4px;")
       root.addWidget(self._status_label)

       btn_row = QHBoxLayout()
       self._run_btn        = QPushButton("Run Test")
       self._force_stop_btn = QPushButton("Force Stop")
       self._close_btn      = QPushButton("Close")
       self._run_btn.setEnabled(False)
       self._force_stop_btn.setEnabled(False)
       self._run_btn.clicked.connect(self._run_test)
       self._force_stop_btn.clicked.connect(self._force_stop)
       self._close_btn.clicked.connect(self.close)
       for b in (self._run_btn, self._force_stop_btn, self._close_btn):
           b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
           btn_row.addWidget(b)
       root.addLayout(btn_row)

   # ------------------------------------------------------------------
   # Populate channels
   # ------------------------------------------------------------------

   def _populate_channels(self):
       # AO output selectors — one entry per output row, default role 0 -> row 0,
       # role 1 -> row 1 (preserving the previous auto-assignment as the default).
       ao_df = self.default_settings_output
       for combo in (getattr(self, '_ao_fixed_combo', None),
                     getattr(self, '_ao_var_combo', None)):
           if combo is None:
               continue
           combo.blockSignals(True)
           combo.clear()
       if ao_df is not None and len(ao_df) > 0:
           for idx in range(len(ao_df)):
               ch = _safe_str(ao_df.iloc[idx].get('Channel'), f'ao{idx}')
               nm = _safe_str(ao_df.iloc[idx].get('Device Name'), ch)
               label = f"{ch}  —  {nm}" if nm and nm != ch else ch
               if getattr(self, '_ao_fixed_combo', None) is not None:
                   self._ao_fixed_combo.addItem(label, userData=idx)
               if getattr(self, '_ao_var_combo', None) is not None:
                   self._ao_var_combo.addItem(label, userData=idx)
       if getattr(self, '_ao_fixed_combo', None) is not None:
           if self._ao_fixed_combo.count() == 0:
               self._ao_fixed_combo.addItem("(none configured)", userData=None)
           self._ao_fixed_combo.setCurrentIndex(0)
           self._ao_fixed_combo.blockSignals(False)
       if getattr(self, '_ao_var_combo', None) is not None:
           if self._ao_var_combo.count() == 0:
               self._ao_var_combo.addItem("(none configured)", userData=None)
           # Default the variable role to the second output row when present.
           self._ao_var_combo.setCurrentIndex(1 if self._ao_var_combo.count() > 1 else 0)
           self._ao_var_combo.blockSignals(False)

       self._enforce_ao_exclusive(self._ao_fixed_combo)
       self._update_ao_corner_text()
       self._refresh_units_label()   # button text includes the AO name

       # DO dropdown — OUTPUT direction channels only
       # Channel label format: "P0.0  —  channel name"
       # Converts NI-style "port0/line0" → "P0.0", "port1/line3" → "P1.3", etc.
       self._do_combo.clear()
       do_df = self.default_settings_dig
       if do_df is not None and len(do_df) > 0:
           for _, row in do_df.iterrows():
               dirn = _safe_str(row.get('Direction'), '').strip().lower()
               # Skip anything that isn't an output
               if dirn and dirn != 'output':
                   continue
               ch  = _safe_str(row.get('Channel'), '')
               nm  = _safe_str(row.get('Device Name'), ch)
               # Convert "port0/line0" → "P0.0"
               ch_display = _format_do_channel(ch)
               label = f"{ch_display}  —  {nm}" if nm and nm != ch else ch_display
               self._do_combo.addItem(label, userData=ch)
       if self._do_combo.count() == 0:
           self._do_combo.addItem("(none configured)", userData=None)

       # AI gauge dropdowns — Y (fixed/pressure) and X (variable/flow)
       # Y-gauge: channels whose unit measures pressure (psi, bar, kPa, ...)
       # X-gauge: channels whose unit measures volumetric flow (gpm, lpm, ...)
       # Picked by quantity (DAQ_Settings.unit_quantity), not by a list of
       # spellings, so any pressure or flow unit the Settings unit check
       # recognises is offered. Everything here works in the gauge's own
       # unit, so no conversion is needed.
       # userData stores (ring_buffer_index, device_name)

       self._y_gauge_combo.clear()
       self._x_gauge_combo.clear()

       ai_df = self.default_settings_input
       if ai_df is not None and len(ai_df) > 0:
           for idx, row in ai_df.iterrows():
               ch    = _safe_str(row.get('Channel'), f'ai{idx}')
               nm    = _safe_str(row.get('Device Name'), ch)
               quantity = unit_quantity(_safe_str(row.get('Units'), '').strip())
               label = f"{ch}  —  {nm}" if nm and nm != ch else ch
               user_data = (int(idx), nm)

               # Y-gauge — pressure channels
               if quantity == "Pressure":
                   self._y_gauge_combo.addItem(label, userData=user_data)

               # X-gauge — flow channels
               if quantity == "Volumetric flow":
                   self._x_gauge_combo.addItem(label, userData=user_data)

       if self._y_gauge_combo.count() == 0:
           self._y_gauge_combo.addItem("(no pressure channels)", userData=None)
       if self._x_gauge_combo.count() == 0:
           self._x_gauge_combo.addItem("(no flow channels)", userData=None)

       # Apply initial axis selections to the main DAQ window
       self._on_y_gauge_changed()
       self._on_x_gauge_changed()

   # ------------------------------------------------------------------
   # Axis sync callbacks
   # ------------------------------------------------------------------

   def _on_y_gauge_changed(self):
       """Set and lock the Y-axis checkbox in Multichannel_DAQ.
       If the same channel is selected on the X combo, clear the X selection.
       """
       # Refresh the unit suffix on the labels and re-evaluate whether the
       # current calibration still applies (only gray out on a real change).
       self._refresh_units_label()
       self._sync_sweep_state_to_inputs()

       data = self._y_gauge_combo.currentData()
       if data is None:
           return
       rb_idx, dev_name = data

       # Mutual exclusion: if X combo has the same channel, reset it to blank
       x_data = self._x_gauge_combo.currentData()
       if x_data is not None and x_data[0] == rb_idx:
           self._x_gauge_combo.blockSignals(True)
           self._x_gauge_combo.setCurrentIndex(-1)
           self._x_gauge_combo.blockSignals(False)

       # One authority for which boxes are ticked and that they stay locked.
       # This handler used to tick the selected gauge but re-ENABLE every other
       # box, which is what let a competing unit back in mid-session.
       self._apply_axes_to_main_plot()
       self._lock_axis_controls()

   def _on_x_gauge_changed(self):
       """Set and lock the X-axis dropdown in Multichannel_DAQ.
       If the same channel is selected on the Y combo, clear the Y selection.
       """
       # Units may have changed → refresh the flow sweep labels. The flow
       # calibration is tied to a specific flow meter, so a change invalidates
       # both the flow calibration and the ΔP/ΔQ model.
       self._refresh_flow_labels()
       self._invalidate_flow_seed()

       data = self._x_gauge_combo.currentData()
       if data is None:
           return
       rb_idx, dev_name = data

       # Mutual exclusion: if Y combo has the same channel, reset it to blank
       y_data = self._y_gauge_combo.currentData()
       if y_data is not None and y_data[0] == rb_idx:
           self._y_gauge_combo.blockSignals(True)
           self._y_gauge_combo.setCurrentIndex(-1)
           self._y_gauge_combo.blockSignals(False)

       # Same single authority as the Y handler. Matters here too: the newly
       # chosen flow meter may currently be ticked as a Y trace, which would
       # make it a competing unit — _apply_axes_to_main_plot unticks it.
       self._apply_axes_to_main_plot()
       self._lock_axis_controls()

   # ------------------------------------------------------------------
   # Plot axis setup (applied on open, on gauge change, and per sequence)
   # ------------------------------------------------------------------

   def _apply_axes_to_main_plot(self):
       """Force the main DAQ plot onto the channels selected here.

       The pressure gauge is made the ONLY ticked trace, and every other box is
       unticked. That is not cosmetic -- it is what makes the overlay land on
       the right axis.

       GraphDataWidget._ensure_axis_for_unit gives the FIRST unit it sees the
       main ViewBox and the left axis ({'vb': None}); every later unit gets its
       own right-hand ViewBox. create_graph walks plot_options in row order, so
       "first" means whichever ticked channel sits earliest in that dataframe.
       This dialog's overlay items are added to plotItem, i.e. the MAIN
       ViewBox -- so if any other unit is ticked ahead of the pressure gauge,
       the DP/DQ model and the result points get drawn against a scale that
       belongs to a different unit. A 3000 psi curve rendered on a 0-10 gpm
       axis is simply off-screen, which looks exactly like the model vanishing.
       Whether it happened depended on which boxes were left ticked and their
       row order, so it came and went between runs.

       The flow meter is deliberately NOT ticked. x_axis_list is built from the
       channel lists independently of the checkboxes, and the X samples are
       pulled from combined_settings by device name, so the flow meter serves
       as the X axis without being a Y trace. Ticking it would plot flow
       against itself AND reintroduce the competing-unit problem above.

       Unlike the combo change handlers this does NOT invalidate the sweep or
       model, so it is safe to call at the start of a sequence.
       """
       mw = self.main_window
       if mw is None or not hasattr(mw, 'plotSettingsWidget'):
           return
       ps = mw.plotSettingsWidget

       # X axis → the selected flow meter (dropdown only; no checkbox needed)
       x_data = self._x_gauge_combo.currentData()
       if x_data is not None and hasattr(ps, 'x_axis_selection'):
           _, x_dev = x_data
           try:
               if ps.x_axis_selection.findText(x_dev) >= 0:
                   ps.x_axis_selection.setCurrentText(x_dev)
               ps.x_axis_selection.setEnabled(False)
               ps.x_axis_selection.setToolTip(
                   "Controlled by Flow Failure — close FF to change")
           except Exception:
               pass

       # Y axis → the pressure gauge, and nothing else.
       y_data = self._y_gauge_combo.currentData()
       if y_data is None:
           return
       _, y_dev = y_data
       try:
           import Multichannel_DAQ as _mdaq
           for i, row in _mdaq.plot_options.iterrows():
               cb = getattr(ps, f'plot_check_box{i}', None)
               if cb is None:
                   continue
               cb.setChecked(row.get('Device Name') == y_dev)
               cb.setEnabled(False)
               cb.setToolTip(
                   "Controlled by Flow Failure — close FF to change")
       except Exception:
           pass

   def _apply_plot_ranges(self):
       """Fix the plot window to 0 → max + 10% on both axes.

       X: 0 → Flow Max + 10%   (e.g. Max 10 gpm  →  0 – 11 gpm)
       Y: 0 → highest pressure test point + 10%
       Auto-range is disabled so the envelope is always read against the same
       fixed window instead of rescaling as points arrive.
       """
       pi = self._get_plot_item()
       if pi is None:
           return
       try:
           max_flow = float(self._flow_max.text())
       except (ValueError, TypeError):
           max_flow = 0.0
       try:
           max_p = max(float(x) for x in self._pressure_points.text().split(',')
                       if x.strip())
       except (ValueError, TypeError):
           max_p = 0.0
       if max_flow <= 0 or max_p <= 0:
           return
       try:
           pi.disableAutoRange()
           pi.setXRange(0.0, max_flow * 1.1, padding=0)
           pi.setYRange(0.0, max_p * 1.1, padding=0)
       except Exception:
           print(f"Flow Failure — could not set plot ranges:\n{traceback.format_exc()}")

   # ------------------------------------------------------------------
   # Capability sweep + closed-loop search
   # ------------------------------------------------------------------

   def _on_pressure_tuning_sweep(self):
       """Pressure tuning sweep of the Fixed Test Point AO channel.

       Drives the pressure AO through one continuous 0 -> max -> 0 triangle
       from a finite hardware-timed task, over the configured tuning duration,
       with the test article CLOSED and the flow control valve held at the
       expert mid-range command so pressure can actually build past the flow
       valve's deadband.

       Produces a coarse voltage->pressure curve (self._press_seed_v /
       _press_seed_p) used ONLY to seed the closed-loop ramp with a sensible
       starting voltage. It is never used as a final command; the ramp always
       verifies against the gauge.

       Only the UP leg is kept. Command->pressure carries valve hysteresis, so
       averaging the legs would land between two branches and match neither;
       the run always approaches from below, which is the up-leg branch.

       The peak pressure is recorded for display but does not gate anything --
       a fast sweep lags the rig and understates the peak, so using it to drop
       test points would silently refuse pressures the rig can actually hold.
       An unreachable point is better discovered by letting the ramp time out.
       """
       if self._is_running:
           return

       y_data = self._get_y_gauge_data()
       if y_data is None:
           QMessageBox.warning(self, "Pressure Tuning Sweep",
                               "No Y-axis gauge channel selected.")
           return
       y_rb_idx, _ = y_data

       ao_row = self._get_ao_row(0)   # Fixed Test Point (Pressure / Supply)
       if ao_row is None:
           QMessageBox.warning(self, "Pressure Tuning Sweep",
                               "Fixed Test Point AO channel not configured.")
           return

       if self._parse_test_points() is None:
           QMessageBox.warning(self, "Pressure Tuning Sweep",
                               "Test points must be numbers.")
           return

       lo, hi = self._ao_limits(ao_row)
       if hi <= lo:
           QMessageBox.warning(self, "Pressure Tuning Sweep",
               "Fixed Test Point AO Max must be greater than Min -- the "
               "pressure command cannot go negative.")
           return

       ao1   = self._get_ao_row(1)
       do_ch = self._get_do_channel()
       prm   = self._search_params()

       # Mid-range command for the flow valve, as a fraction of ITS OWN range.
       flow_mid_v = 0.0
       if ao1 is not None:
           flow_mid_v = self._ao_pct_to_voltage(ao1, prm['flow_mid_pct'])

       self._invalidate_sweep()
       self._set_controls_running(True)
       self._signals.status_changed.emit("Pressure tuning sweep\u2026")

       # Ensure DAQ is running so the ring buffer is live to read from.
       mw = self.main_window
       if mw and not getattr(mw, 'continue_running', False):
           try:
               mw.start_task()
           except Exception:
               print(f"Flow Failure -- could not auto-start DAQ:\n{traceback.format_exc()}")

       def _worker():
           ok = False
           try:
               ao_settle = float(self._ao_settle_flow.text())

               # Known de-commanded start state.
               self._ao_write(ao_row, lo)
               if ao1 is not None:
                   self._ao_write(ao1, 0.0)

               # Close the test article so the sweep sees dead-head pressure.
               if do_ch:
                   self._valve_close(do_ch)
                   self._signals.status_changed.emit("Test valve closed\u2026")
                   time.sleep(float(self._delay_valve_read.text()))

               # Flow valve to mid-range -- past deadband, not necessarily max.
               if ao1 is not None:
                   self._ao_write(ao1, flow_mid_v)
                   self._signals.status_changed.emit(
                       f"Flow valve \u2192 mid ({flow_mid_v:.3f} V)\u2026")
                   time.sleep(ao_settle)

               v_cmd, cols, apex = self._sweep_ao_continuous(
                   ao_row, [y_rb_idx], prm['p_sweep_secs'],
                   both_legs=True, status_prefix="Pressure tuning sweep\u2026")
               p_all = cols[0]

               # Return to a safe, de-commanded state.
               self._ao_write(ao_row, lo)
               if ao1 is not None:
                   self._ao_write(ao1, 0.0)
               if do_ch:
                   self._valve_close(do_ch)

               if self._abort:
                   self._signals.status_changed.emit("Sweep aborted  \u26a0")
               elif len(v_cmd) >= 2 and apex >= 2:
                   # UP leg only for the seed curve (see the docstring).
                   self._press_seed_v    = v_cmd[:apex]
                   self._press_seed_p    = p_all[:apex]
                   self._max_p_reachable = float(np.max(p_all))
                   ok = True

           except Exception:
               print(f"Flow Failure -- sweep worker error:\n{traceback.format_exc()}")
               self._signals.status_changed.emit("Sweep ERROR -- see log  \u274c")
           finally:
               self._is_running = False
               self._abort      = False
               self._signals.calib_done_sig.emit(ok)
               self._signals.controls_unlock.emit()
               self._signals.stop_daq_sig.emit()

       self._is_running = True
       self._abort      = False
       self._worker_thread = threading.Thread(target=_worker, daemon=True)
       self._worker_thread.start()

   # ------------------------------------------------------------------
   # Closed-loop search  (search, then LATCH -- no live control)
   # ------------------------------------------------------------------

   def _ao_pct_to_voltage(self, ao_row, pct):
       """Convert a percentage of an AO row's usable range into a voltage.

       The usable range is clamped at 0 on the low side: every command in this
       test (pressure valve, flow valve) is unipolar and must never be driven
       negative, even when the AO row itself is configured bipolar.
       """
       try:
           lo = float(ao_row.get('Min', 0.0))
           hi = float(ao_row.get('Max', 5.0))
       except (TypeError, ValueError):
           lo, hi = 0.0, 5.0
       lo = max(0.0, lo)
       if hi <= lo:
           return lo
       return lo + (hi - lo) * (float(pct) / 100.0)

   def _ao_limits(self, ao_row):
       """Return the (min, max) usable command voltages for an AO row, with the
       low side clamped at 0 for the same unipolar reason as above."""
       try:
           lo = float(ao_row.get('Min', 0.0))
           hi = float(ao_row.get('Max', 5.0))
       except (TypeError, ValueError):
           lo, hi = 0.0, 5.0
       lo = max(0.0, lo)
       if hi <= lo:
           hi = lo
       return lo, hi

   # ------------------------------------------------------------------
   # Continuous tuning sweep engine
   # ------------------------------------------------------------------

   def _sweep_sample_timing(self):
       """(sample interval, samples to average) for a sweep, derived from the
       main DAQ's frame rate.

       One sample per acquisition frame is the natural cadence: the ring
       buffer only gains new data once per frame, so polling faster just
       re-reads the same samples, and averaging one frame's worth is the
       widest average that does not blur two different commands together.
       """
       mw = self.main_window
       try:
           refresh = float(getattr(mw, 'refresh_rate', 0) or 0)
       except (TypeError, ValueError):
           refresh = 0.0
       if refresh <= 0:
           refresh = 10.0
       dt = 1.0 / refresh
       try:
           n_avg = int(float(getattr(mw, 'samples_per_frame', 0) or 0))
       except (TypeError, ValueError):
           n_avg = 0
       if n_avg <= 0:
           n_avg = max(1, int(self.sample_rate / refresh))
       return dt, n_avg

   def _build_ramp(self, lo, hi, duration_s, ao_rate, both_legs):
       """Triangle (0 → max → 0) or single ramp (0 → max) as an AO sample array.

       Triangular rather than sinusoidal: a triangle has constant dV/dt, so
       every command voltage gets the same dwell and the resulting curve is
       evenly resolved across the range. A sine loiters at both endpoints and
       rushes the middle, which is exactly where the test points live.
       """
       n = max(2, int(round(float(duration_s) * float(ao_rate))))
       if not both_legs:
           return np.linspace(lo, hi, n, dtype=np.float64)
       half = max(1, n // 2)
       up = np.linspace(lo, hi, half, endpoint=False, dtype=np.float64)
       dn = np.linspace(hi, lo, n - half, dtype=np.float64)
       return np.concatenate([up, dn])

   def _sweep_ao_continuous(self, ao_row, rb_indices, duration_s,
                            both_legs=True, status_prefix=""):
       """Drive one continuous ramp on `ao_row` and sample `rb_indices` under it.

       The command is written as a FINITE hardware-timed AO task, so exactly
       one pass is generated, it ends deterministically at the low end of the
       range, and there is no phase ambiguity about where it stopped. That is
       why this does not route through the Function Generator, whose waveforms
       regenerate continuously and whose task state lives on the UI thread
       while this runs on a worker.

       Devices with no AO sample clock (USB-6008 and friends) fall back to a
       software-paced write loop over the same array. The ramp is coarser in
       time but identical in shape.

       Returns (v_cmd, readings, apex) where `readings` is a list of arrays
       parallel to `rb_indices`, and `apex` is the index splitting the up leg
       from the down leg (== len(v_cmd) when both_legs is False).
       """
       lo, hi = self._ao_limits(ao_row)
       if hi <= lo:
           raise RuntimeError("AO range is empty -- check the AO Min/Max settings.")

       ao_rate = float(self._SWEEP_AO_RATE)
       ramp    = self._build_ramp(lo, hi, duration_s, ao_rate, both_legs)
       t_ramp  = np.arange(len(ramp), dtype=np.float64) / ao_rate
       total_s = float(len(ramp)) / ao_rate

       dev  = self._ao_device(ao_row)
       ch   = _safe_str(ao_row.get('Channel'), '')
       try:
           cfg_lo = float(ao_row.get('Min', 0.0))
           cfg_hi = float(ao_row.get('Max', 5.0))
       except (TypeError, ValueError):
           cfg_lo, cfg_hi = 0.0, 5.0

       hw_timed = True
       try:
           hw_timed = bool(nidaqmx.system.Device(dev).ao_samp_clk_supported)
       except Exception:
           hw_timed = True   # assume capable; the task build below will tell us

       dt, n_avg = self._sweep_sample_timing()
       t_samp, reads = [], []

       task = nidaqmx.Task()
       try:
           add_ao_channel(task, f"{dev}/{ch}", self._ao_type(ao_row),
                          min_val=cfg_lo, max_val=cfg_hi)
           writer = AnalogSingleChannelWriter(task.out_stream)

           if hw_timed:
               task.timing.cfg_samp_clk_timing(
                   rate=ao_rate,
                   sample_mode=ni.AcquisitionType.FINITE,
                   samps_per_chan=len(ramp))
               task.out_stream.regen_mode = \
                   ni.RegenerationMode.DONT_ALLOW_REGENERATION
               writer.write_many_sample(ramp)
               task.start()
           else:
               task.start()

           t0 = time.time()
           while True:
               el = time.time() - t0
               if el >= total_s or self._abort:
                   break
               v_now = float(ramp[min(len(ramp) - 1, int(el * ao_rate))])
               if not hw_timed:
                   writer.write_one_sample(v_now)
               # Publish the command so the main app's output channel display,
               # the plot trace and the calibration window all follow the ramp.
               # A hardware-timed task drives the AO without going through
               # _ao_write, so nothing else publishes during a sweep and the
               # output channel would sit flat at its last written value.
               self._notify_output(ch, v_now)
               vals = [self._read_gauge_from_rb(i, n_avg=n_avg)
                       for i in rb_indices]
               if any(v is None for v in vals):
                   raise RuntimeError(
                       "Could not read the gauge -- ensure the main DAQ is "
                       "running before sweeping.")
               t_samp.append(el)
               reads.append(vals)
               if status_prefix:
                   self._signals.status_changed.emit(
                       f"{status_prefix} {el:.1f}/{total_s:.1f}s")
               time.sleep(dt)

           if hw_timed and not self._abort:
               # NOT swallowed. If the generation did not finish, the ramp
               # never reached the top of its range, so the curve this sweep
               # returns is truncated -- and a truncated seed curve looks
               # exactly like a valid one to everything downstream. Failing
               # here is the only thing that stops bad seeds being used.
               try:
                   task.wait_until_done(timeout=max(1.0, total_s))
               except Exception as exc:
                   raise RuntimeError(
                       f"AO sweep did not finish generating ({exc}). The ramp "
                       f"was cut short, so the sweep data is incomplete."
                   ) from exc
       finally:
           try:
               if self._abort:
                   # Stopping a finite task mid-generation raises DAQmx
                   # warning 200010. On an abort that is exactly what we asked
                   # for, so it is suppressed here and nowhere else -- on a
                   # normal run the same warning would mean the ramp was cut
                   # short, which must stay visible.
                   with warnings.catch_warnings():
                       warnings.filterwarnings("ignore", category=DaqWarning)
                       task.stop()
               else:
                   task.stop()
           except Exception:
               pass
           try:
               task.close()
           except Exception:
               pass

       # Return the channel to a known de-commanded state. The finite task
       # ends at `lo`, but a software-paced run that was aborted mid-ramp may
       # not have, and an aborted hardware task stops wherever it was.
       self._ao_write(ao_row, lo)

       if len(t_samp) < 2:
           return (np.empty(0), [np.empty(0) for _ in rb_indices], 0)

       t_arr = np.asarray(t_samp, dtype=np.float64)
       # Each sample is the mean of the frame that ENDED when it was read, so
       # it represents the rig half a frame earlier. Correcting for that keeps
       # the command paired with the reading it actually produced.
       v_cmd = np.interp(np.clip(t_arr - 0.5 * dt, 0.0, t_ramp[-1]),
                         t_ramp, ramp)
       cols  = [np.asarray([r[k] for r in reads], dtype=np.float64)
                for k in range(len(rb_indices))]

       apex = len(v_cmd)
       if both_legs:
           apex = int(np.argmax(v_cmd)) + 1

       return v_cmd, cols, apex

   def _settled_read(self, rb_idx, poll, tol, max_dwell):
       """Read a gauge, waiting for it to stop moving before returning.

       A fixed sleep assumes the plant responds within one dwell. A pressure
       rig does not -- it takes seconds to build -- so a fixed 0.1 s settle
       returned a mid-ramp transient on every step, the ramp never saw the
       pressure it had already commanded, and it raced the AO to maximum before
       the gauge had meaningfully moved. That is what made a 4000 unit setpoint
       "fail" at 691 units in 0.2 s.

       Polls until two consecutive reads differ by less than `tol`, or
       `max_dwell` elapses. Fast plants return after the first poll, so this
       costs nothing where nothing is needed.
       """
       poll = max(0.02, float(poll))
       t0   = time.time()
       prev = self._read_gauge_from_rb(rb_idx)
       if prev is None:
           return None
       while time.time() - t0 < max_dwell:
           if self._abort:
               return prev
           time.sleep(poll)
           cur = self._read_gauge_from_rb(rb_idx)
           if cur is None:
               return prev
           if abs(cur - prev) <= tol:
               return cur
           prev = cur
       return prev

   def _search_and_latch(self, ao_row, rb_idx, target, seed_v,
                         step_pct, settle, band, timeout, what, units,
                         last_good_v=None, symmetric=False, stab_dwell=0.0,
                         max_halvings=6):
       """Ramp an AO upward until the measured value reaches `target`, then
       stop and return the voltage that got there.

       This is the ONLY place a loop is closed. Once this returns, the caller
       latches the returned voltage and holds it for the rest of the point --
       nothing re-corrects it. That is deliberate: a live controller chasing
       the setpoint during valve actuation would mask the very pressure drop
       the test is trying to measure, wind up against the cycling transients,
       and make results depend on loop timing rather than on the valve.

       SEEDING. The start command is

           clip(max(seed_v - step, last_good_v), lo, hi)

       where `last_good_v` is the command latched by the PREVIOUS point.
       Backing off one step keeps the approach from below even when the seed
       reads slightly high, which a swept seed curve tends to do -- the sweep
       lags the rig, so it understates the value at each voltage, and
       inverting it returns a voltage that is a little too high.

       The `max` against `last_good_v` is what makes a bad seed cheap. Test
       points ascend and the actuator is left where the previous point latched
       it, so the previous latch is a command already known to sit BELOW the
       new target -- and it is measured on this run rather than interpolated
       off a fast sweep. Falling back to it beats restarting at 0 V twice
       over: it skips re-climbing ground already covered, and it keeps the
       approach monotonic. Dropping to 0 V and climbing back is a different
       actuator history than continuing up, so the old restart did not
       preserve the hysteresis consistency it was written to protect.

       ACCEPTANCE. Pressure uses the one-sided test (`symmetric=False`):
       anything at or above `target - band` is a hit, because a setpoint that
       is overshot is still a setpoint that was reached, and pressure defines
       what the test means. A seed landing above the band is a bad seed rather
       than a hit, and drops back to `last_good_v` once.

       Flow uses the symmetric test (`symmetric=True`): the reading must land
       inside the band on BOTH sides. Overshoot halves the step and steps back
       down, up to `max_halvings` times, after which the closest reading is
       accepted -- flow points are not precision setpoints, the value recorded
       is the measured flow rather than the requested one, and bisecting
       forever would cost more than the extra precision is worth.

       Returns (voltage, measured, ok). On timeout ok is False and the last
       voltage/reading are returned so the caller can log and record.
       """
       lo, hi = self._ao_limits(ao_row)
       step   = max(1e-4, (hi - lo) * (float(step_pct) / 100.0))

       # Floor for the whole search: never command below the last known-good
       # point, so the approach only ever moves upward through fresh ground.
       floor_v = lo
       if last_good_v is not None and np.isfinite(last_good_v):
           floor_v = float(np.clip(last_good_v, lo, hi))

       v = floor_v
       seeded = False
       if seed_v is not None and np.isfinite(seed_v):
           v = float(np.clip(max(float(seed_v) - step, floor_v), lo, hi))
           seeded = True

       self._log(f"    search {what} \u2192 {target:g} {units}"
                 f"  (seed {v:.3f} V, floor {floor_v:.3f} V, "
                 f"step {step:.4f} V, band \u00b1{band:g})")

       t0            = time.time()
       reading       = None
       restarted     = False
       halvings      = 0
       at_max_logged = False
       while True:
           if self._abort:
               return v, reading, False

           self._ao_write(ao_row, v)
           if settle > 0:
               time.sleep(settle)
           # Wait for the gauge to stop moving rather than trusting one dwell.
           # tol is a quarter of the acceptance band: tight enough that a
           # settled reading is meaningful, loose enough that gauge noise does
           # not hold the poll open until max_dwell every time.
           reading = self._settled_read(rb_idx, settle, band / 4.0, stab_dwell)
           if reading is None:
               raise RuntimeError(
                   "DAQ ring buffer not available -- start the DAQ before running."
               )

           if symmetric:
               if abs(reading - target) <= band:
                   self._log(f"      found at {v:.3f} V  \u2192 {reading:.2f} {units}")
                   return v, reading, True
               if reading > target + band:
                   if halvings >= max_halvings or v <= floor_v + 1e-9:
                       # Out of halvings, or already back at the floor and
                       # still high. The measured flow is what gets recorded,
                       # so this is a usable point, not a failure.
                       self._log(f"      accepting {reading:.2f} {units} at "
                                 f"{v:.3f} V after {halvings} halving(s)")
                       return v, reading, True
                   halvings += 1
                   step = step / 2.0
                   v = max(floor_v, v - step)
                   self._log(f"      overshot ({reading:.2f} > "
                             f"{target + band:.2f}) -- step \u2192 {step:.4f} V, "
                             f"backing to {v:.3f} V")
                   continue
           else:
               # A seed that lands ABOVE the band is a bad seed, not a hit.
               # Accepting it would latch the actuator far past the setpoint
               # and still report success. Drop back to the last known-good
               # command and ramp from there. Only done once: if the reading
               # is still high from the floor, the pressure is not ours to
               # lower and the timeout below is the correct outcome.
               if seeded and not restarted and reading > target + band:
                   self._log(f"      seed overshot ({reading:.2f} {units} > "
                             f"{target + band:.2f}) -- discarding seed, "
                             f"restarting from {floor_v:.3f} V")
                   restarted = True
                   v = floor_v
                   continue

               if reading >= target - band:
                   self._log(f"      found at {v:.3f} V  \u2192 {reading:.2f} {units}")
                   return v, reading, True

           if time.time() - t0 > timeout:
               why = ("UNREACHABLE -- AO held at max for the full timeout"
                      if at_max_logged else "TIMEOUT")
               self._log(f"      {why} after {timeout:g}s at {v:.3f} V "
                         f"\u2192 {reading:.2f} {units} (wanted {target:g})")
               return v, reading, False

           if v >= hi - 1e-9:
               # Command exhausted is NOT the same as value unreachable. On a
               # slow plant, reaching max command means the pressure is still
               # climbing -- returning here reported "unreachable" a fifth of a
               # second into a build that takes seconds. Hold at max and keep
               # watching; only the timeout above decides it is truly out of
               # reach. `continue` re-enters the loop, which re-checks the
               # acceptance band and the timeout on every pass.
               if not at_max_logged:
                   self._log(f"      AO at max ({hi:.3f} V) \u2192 {reading:.2f} "
                             f"{units} -- holding, waiting for it to settle")
                   at_max_logged = True
               continue

           v = min(hi, v + step)

   def _band_for(self, target, pct, floor):
       """Acceptance band for a setpoint: a percentage of the setpoint, never
       tighter than the absolute floor. Percent alone would be unreasonably
       tight at low setpoints; a floor alone would be unreasonably loose at
       high ones."""
       return max(float(floor), abs(float(target)) * float(pct) / 100.0)

   def _seed_voltage(self, sweep_x, sweep_v, target):
       """Interpolate a seed command voltage for a target measurement from a
       recorded sweep. Returns None when no usable sweep exists.

       None means "no seed" -- the caller then starts its ramp from the bottom
       of the AO range. That is the safe fallback: the old code fell back to
       treating the target VALUE as a voltage, which silently commanded
       nonsense (e.g. 3000 psi -> 3000 V clamped to full scale).
       """
       if sweep_x is None or sweep_v is None or len(sweep_x) < 2:
           return None
       order = np.argsort(sweep_x)
       x_sorted = np.asarray(sweep_x, dtype=np.float64)[order]
       v_sorted = np.asarray(sweep_v, dtype=np.float64)[order]
       keep = np.concatenate(([True], np.diff(x_sorted) > 0))
       x_sorted = x_sorted[keep]
       v_sorted = v_sorted[keep]
       if len(x_sorted) < 2:
           return None
       return max(0.0, float(np.interp(float(target), x_sorted, v_sorted)))

   def _seed_voltage_for_pressure(self, pressure):
       """Seed voltage for a pressure setpoint, from the pressure tuning sweep."""
       return self._seed_voltage(self._press_seed_p, self._press_seed_v, pressure)

   def _seed_voltage_for_flow(self, flow):
       """Seed voltage for a flow setpoint, from the DP/DQ model sweep."""
       return self._seed_voltage(self._flow_seed_q, self._flow_seed_v, flow)

   def _active_model(self):
       """Return the model dict selected by the Sweep-up / Sweep-down radio."""
       if getattr(self, '_model_down_radio', None) is not None \
               and self._model_down_radio.isChecked():
           return self._model_down
       return self._model_up

   def _sync_active_model(self):
       """Point self._model at the currently selected up/down model."""
       self._model = self._active_model()

   def _on_model_dir_changed(self, _checked=None):
       """Sweep-up/Sweep-down selection changed — swap the active model and
       redraw the threshold curve. Only meaningful once a model exists."""
       if not (self._model_up or self._model_down):
           return
       self._sync_active_model()
       self._restore_static_plots()

   def _on_flow_settings_changed(self, _text=None):
       """Flow sweep grid (start/step/max or per-point starts) edited. The
       ΔP/ΔQ model is built on this grid, so any change makes it stale and
       the test must not run until the model is regenerated."""
       if self._model_ready:
           self._model_ready = False
           self._signals.enable_run.emit(False)
           self._signals.status_changed.emit(
               "Flow settings changed — click Generate \u0394P/\u0394Q Model to update  \u26a0"
           )

   # ------------------------------------------------------------------
   # Expert settings
   # ------------------------------------------------------------------

   def _build_expert_window(self):
       """Create the Expert Settings window and every widget it owns.

       A separate window rather than an inline block: these are rig-setup values
       that get dialled in once and then left alone, and inline they crowded out
       the controls that actually change between runs.

       The widgets themselves are still owned by THIS dialog (self._ao_settle_flow
       and friends), only their parent window differs. That matters because
       _set_controls_running, the worker threads and _search_params all reach for
       them by name, and because a QLineEdit's value survives the window being
       closed — nothing needs saving or reloading.
       """
       win = QDialog(self)
       win.setWindowTitle("Flow Failure — Expert Settings")
       win.setMinimumWidth(560)
       # Non-modal: the point is to watch the run log / plot while nudging a
       # search parameter, which a modal window would block.
       win.setModal(False)
       root = QVBoxLayout(win)

       note = QLabel(
           "Rig setup and closed-loop search parameters. Defaults suit most "
           "rigs — change these when a ramp overshoots, hunts or times out, or "
           "to alter valve cycling."
       )
       note.setWordWrap(True)
       note.setStyleSheet("color: #555; padding: 2px 0 6px 0;")
       root.addWidget(note)

       # ── Valve timing ──────────────────────────────────────────────────
       grp = QGroupBox("Valve Timing")
       gl  = QGridLayout(grp)
       gl.setColumnMinimumWidth(0, self._LABEL_COL_W)
       gl.setColumnStretch(6, 1)

       gl.addWidget(QLabel("Delay (s):"), 0, 0)
       gl.addWidget(QLabel("AO settle (flow)"), 0, 1)
       self._ao_settle_flow = QLineEdit("0.5"); self._ao_settle_flow.setMaximumWidth(70)
       self._ao_settle_flow.setToolTip(
           "Dwell after the FLOW control valve command is written, before the\n"
           "next action, so the flow valve reaches steady state rather than\n"
           "being acted on mid-transient.\n\n"
           "Applies where the flow command is set to mid-range before a\n"
           "pressure ramp, and where it is dropped back to 0 afterwards.\n"
           "The tuning sweeps do not use it — they are continuously paced.")
       gl.addWidget(self._ao_settle_flow, 0, 2)
       gl.addWidget(QLabel("Valve → Read"), 0, 4)
       self._delay_valve_read = QLineEdit("0.5"); self._delay_valve_read.setMaximumWidth(70)
       self._delay_valve_read.setToolTip(
           "Dwell after the valve moves before the gauge is read. Too short and "
           "a check sees the transient rather than the settled pressure.")
       gl.addWidget(self._delay_valve_read, 0, 5)

       # Pulse (chatter) the valve on each energize / de-energize transition
       # instead of a single switch.
       gl.addWidget(QLabel("Actuation:"), 1, 0)
       gl.addWidget(QLabel("Cycles"), 1, 1)
       self._actuation_cycles = QLineEdit("5"); self._actuation_cycles.setMaximumWidth(70)
       self._actuation_cycles.setToolTip(
           "Number of pulses applied to the valve at each energize / de-energize\n"
           "transition.\n"
           "  Energize   : on, off, on, off, … , on   (N on-pulses, ends energized)\n"
           "  De-energize: off, on, off, on, … , off  (N off-pulses, ends de-energized)\n"
           "1 = single switch (no chatter)."
       )
       gl.addWidget(self._actuation_cycles, 1, 2)
       gl.addWidget(QLabel("Frequency (Hz)"), 1, 4)
       self._actuation_freq = QLineEdit("2"); self._actuation_freq.setMaximumWidth(70)
       self._actuation_freq.setToolTip(
           "Rate at which the valve is toggled during a multi-cycle actuation.\n"
           "Each on/off state is held for half a period (1/(2·f) seconds).\n"
           "Ignored when Actuation cycles = 1."
       )
       gl.addWidget(self._actuation_freq, 1, 5)
       root.addWidget(grp)

       # ── Evaluation model ──────────────────────────────────────────────
       # Which threshold model the Run Test compares against — the sweep-up
       # model or the sweep-down (hysteresis) model. Both are built together
       # by Generate ΔP/ΔQ Model.
       grp = QGroupBox("Evaluation Model")
       dir_row = QHBoxLayout(grp)
       self._model_up_radio   = QRadioButton("Sweep-up")
       self._model_down_radio = QRadioButton("Sweep-down")
       self._model_up_radio.setChecked(True)
       self._model_dir_group  = QButtonGroup(self)
       self._model_dir_group.addButton(self._model_up_radio)
       self._model_dir_group.addButton(self._model_down_radio)
       self._model_up_radio.setToolTip(
           "Compare against the model built while AO1 was swept UP (0 → max)."
       )
       self._model_down_radio.setToolTip(
           "Compare against the model built while AO1 was swept DOWN (max → 0)."
       )
       self._model_up_radio.toggled.connect(self._on_model_dir_changed)
       dir_row.addWidget(self._model_up_radio)
       dir_row.addWidget(self._model_down_radio)
       dir_row.addStretch(1)
       root.addWidget(grp)

       # ── Closed-loop search ────────────────────────────────────────────
       grp = QGroupBox("Closed-Loop Search")
       eg  = QGridLayout(grp)
       eg.setColumnMinimumWidth(0, self._LABEL_COL_W + 110)
       eg.setColumnStretch(3, 1)

       def _expert_field(row, label, default, tip, col=0):
           lbl = QLabel(label)
           lbl.setToolTip(tip)
           eg.addWidget(lbl, row, col)
           fld = QLineEdit(default)
           fld.setMaximumWidth(70)
           fld.setToolTip(tip)
           eg.addWidget(fld, row, col + 1)
           return fld

       self._exp_flow_mid_pct = _expert_field(
           0, "Flow cmd during pressure set (% of AO range):", "50",
           "Command held on the flow control valve while the pressure setpoint\n"
           "is being found. Must be past the flow valve's deadband so pressure\n"
           "can actually build — it does NOT need to be full open.")
       self._exp_p_step_pct = _expert_field(
           1, "Pressure ramp step (% of AO range):", "2.0",
           "Increment applied to the pressure AO on each iteration of the\n"
           "closed-loop search. Expressed as % of AO range so it survives a\n"
           "change from 0–5 V to 0–10 V.")
       self._exp_p_settle = _expert_field(
           2, "Pressure poll interval (s):", "0.10",
           "How often the gauge is sampled while waiting for pressure to stop\n"
           "moving after each increment.")
       self._exp_p_stab = _expert_field(
           12, "Pressure stability max wait (s):", "3.0",
           "Longest the ramp waits for pressure to settle after one increment.\n"
           "A pressure rig takes seconds to build; without this the ramp reads\n"
           "a transient, never sees the pressure it commanded, and races the AO\n"
           "to maximum. Settles sooner when the rig responds sooner.")
       self._exp_p_band_pct = _expert_field(
           3, "Pressure acceptance band (% of setpoint):", "1.0",
           "The ramp stops once the reading is within this % of the setpoint.\n"
           "Percent rather than absolute so the band scales sensibly from a\n"
           "300 unit setpoint to a 5000 unit one.")
       self._exp_p_band_min = _expert_field(
           4, "Pressure acceptance band floor (units):", "5.0",
           "Absolute lower limit on the acceptance band, so a small setpoint\n"
           "does not demand a band finer than the gauge can resolve.")
       self._exp_p_timeout = _expert_field(
           5, "Pressure ramp timeout (s):", "15",
           "The search gives up after this long, logs a timeout, and moves on\n"
           "rather than stalling the run.")
       self._exp_q_step_pct = _expert_field(
           6, "Flow ramp step (% of AO range):", "5.0",
           "Increment applied to the flow AO on each iteration of the\n"
           "closed-loop search.")
       self._exp_q_settle = _expert_field(
           7, "Flow poll interval (s):", "0.05",
           "How often the flow meter is sampled while waiting for flow to stop\n"
           "moving after each increment.")
       self._exp_q_stab = _expert_field(
           13, "Flow stability max wait (s):", "0.5",
           "Longest the ramp waits for flow to settle after one increment.\n"
           "Much shorter than the pressure equivalent -- flow responds quickly,\n"
           "and flow points are not precision setpoints.")
       self._exp_q_band_pct = _expert_field(
           8, "Flow acceptance band (% of setpoint):", "10.0",
           "The ramp stops once the measured flow is within this % of the\n"
           "flow setpoint.")
       self._exp_q_band_min = _expert_field(
           9, "Flow acceptance band floor (units):", "0.25",
           "Absolute lower limit on the flow acceptance band.")
       self._exp_q_timeout = _expert_field(
           10, "Flow ramp timeout (s):", "10",
           "The flow search gives up after this long, logs a timeout, and the\n"
           "point is recorded as unreachable.")
       self._exp_p_sweep_secs = _expert_field(
           11, "Pressure tuning sweep duration (s):", "6.0",
           "Total time for the continuous 0 → max → 0 ramp of the pressure\n"
           "command. Longer is more accurate: the rig lags the command, so a\n"
           "fast sweep understates pressure at every voltage and the seed it\n"
           "produces reads high.")
       self._exp_q_sweep_secs = _expert_field(
           14, "Flow model sweep duration (s):", "6.0",
           "Total time for the continuous 0 → max → 0 ramp of the flow\n"
           "command that builds the ΔP/ΔQ model and the flow seeds.\n\n"
           "Watch the up/down leg spread reported when the model finishes: a\n"
           "large spread means this is too fast for the rig to keep up.")
       root.addWidget(grp)

       close_row = QHBoxLayout()
       close_row.addStretch(1)
       btn = QPushButton("Close")
       btn.clicked.connect(win.hide)
       close_row.addWidget(btn)
       root.addLayout(close_row)

       # hide() rather than close()/destroy on the window's own X button, so the
       # widgets and their values are never torn down while the dialog lives.
       win.setAttribute(Qt.WA_DeleteOnClose, False)
       return win

   def _open_expert_window(self):
       """Show the Expert Settings window, raising it if already open."""
       if self._expert_win is None:
           return
       self._expert_win.show()
       self._expert_win.raise_()
       self._expert_win.activateWindow()

   def _expert_val(self, field, default, lo=None, hi=None):
       """Read one expert field, falling back to its default on bad input and
       clamping to a sane range. Never raises — a mistyped expert value must
       not be able to abort a run."""
       try:
           v = float(field.text())
       except (ValueError, TypeError, AttributeError):
           return default
       if not np.isfinite(v):
           return default
       if lo is not None and v < lo:
           return lo
       if hi is not None and v > hi:
           return hi
       return v

   def _search_params(self):
       """Snapshot every closed-loop search parameter as a plain dict, read
       once on the UI thread before the worker starts. The worker must not
       touch the QLineEdits directly."""
       return {
           'flow_mid_pct': self._expert_val(self._exp_flow_mid_pct, 50.0, 0.0, 100.0),
           'p_step_pct':   self._expert_val(self._exp_p_step_pct,   2.0,  0.01, 50.0),
           'p_settle':     self._expert_val(self._exp_p_settle,     0.10, 0.0,  10.0),
           'p_band_pct':   self._expert_val(self._exp_p_band_pct,   1.0,  0.0,  50.0),
           'p_band_min':   self._expert_val(self._exp_p_band_min,   5.0,  0.0,  None),
           'p_timeout':    self._expert_val(self._exp_p_timeout,    15.0, 1.0,  600.0),
           'p_stab':       self._expert_val(self._exp_p_stab,       3.0,  0.0,  60.0),
           'q_step_pct':   self._expert_val(self._exp_q_step_pct,   5.0,  0.01, 50.0),
           'q_settle':     self._expert_val(self._exp_q_settle,     0.05, 0.0,  10.0),
           'q_band_pct':   self._expert_val(self._exp_q_band_pct,   10.0, 0.0,  50.0),
           'q_band_min':   self._expert_val(self._exp_q_band_min,   0.25, 0.0,  None),
           'q_timeout':    self._expert_val(self._exp_q_timeout,    10.0, 1.0,  600.0),
           'q_stab':       self._expert_val(self._exp_q_stab,       0.5,  0.0,  60.0),
           'p_sweep_secs': self._expert_val(self._exp_p_sweep_secs, 6.0,  0.5,  120.0),
           'q_sweep_secs': self._expert_val(self._exp_q_sweep_secs, 6.0,  0.5,  120.0),
       }

   # ------------------------------------------------------------------
   # Test point normalisation
   # ------------------------------------------------------------------

   def _parse_test_points(self):
       """Parse the pressure test point field. Returns list[float] or None on
       malformed input."""
       try:
           return [float(x.strip()) for x in
                   self._pressure_points.text().split(",") if x.strip()]
       except ValueError:
           return None

   def _normalize_test_points(self):
       """Sort the pressure test points ascending and drop duplicates.

       The test walks pressure upward only — a list like 3000,4000,3500,5000
       would otherwise drive the rig down and back up between points, which
       both wastes time and leaves the pressure control valve approaching each
       setpoint from an inconsistent direction.

       Per-point flow starts are positionally indexed against this list, so
       they are reordered as PAIRS with their pressure. Reordering the
       pressures alone would silently re-attach every start to the wrong
       pressure.
       """
       tps = self._parse_test_points()
       if not tps:
           return

       # Pair each pressure with its per-point start before sorting.
       try:
           starts = [float(x.strip()) for x in
                     self._var_starts.text().split(",") if x.strip()]
       except ValueError:
           starts = []
       try:
           default_start = float(self._flow_start.text())
       except (ValueError, TypeError):
           default_start = 0.0
       while len(starts) < len(tps):
           starts.append(default_start)
       starts = starts[:len(tps)]

       pairs = sorted(zip(tps, starts), key=lambda t: t[0])

       # De-duplicate on pressure, keeping the first occurrence (and its start).
       seen, uniq = set(), []
       for p, s in pairs:
           key = round(p, 6)
           if key in seen:
               continue
           seen.add(key)
           uniq.append((p, s))

       new_tp = ", ".join(f"{p:g}" for p, _ in uniq)
       new_vs = ", ".join(f"{s:g}" for _, s in uniq)

       if new_tp != self._pressure_points.text():
           self._pressure_points.blockSignals(True)
           self._pressure_points.setText(new_tp)
           self._pressure_points.blockSignals(False)
           self._on_test_points_changed()
       if new_vs != self._var_starts.text():
           self._var_starts.blockSignals(True)
           self._var_starts.setText(new_vs)
           self._var_starts.blockSignals(False)

   # ------------------------------------------------------------------
   # Capability sweep UI state
   # ------------------------------------------------------------------

   def _model_ref_p(self):
       """The model's reference dead-head pressure in the Y gauge's unit.

       Defined in psi (_MODEL_REF_P_PSI) and converted, so a gauge in bar or
       kPa aims for the same physical pressure — the number used to be taken
       as-is in the gauge's unit, which in bar meant 1000 bar (~14,500 psi).
       """
       units = self._get_y_units()
       try:
           scale, _offset = unit_affine("psi", units)
       except Exception as e:
           raise RuntimeError(f"cannot express the {self._MODEL_REF_P_PSI:g} psi "
                              f"model reference in the gauge's unit "
                              f"{units!r}: {e}") from None
       return float(self._MODEL_REF_P_PSI * scale)

   def _get_y_units(self):
       """Return the engineering units string for the selected Y-axis gauge
       (e.g. 'psi'), or '' if unavailable."""
       data = self._get_y_gauge_data()
       if data is None:
           return ''
       _, dev_name = data
       ai_df = self.default_settings_input
       if ai_df is None or ai_df.empty:
           return ''
       matches = ai_df[ai_df['Device Name'] == dev_name]
       if matches.empty:
           return ''
       return _safe_str(matches.iloc[0].get('Units'), '')

   def _get_x_units(self):
       """Return the engineering units string for the selected X-axis flow
       meter (e.g. 'gpm'), or '' if unavailable."""
       data = self._get_x_gauge_data()
       if data is None:
           return ''
       _, dev_name = data
       ai_df = self.default_settings_input
       if ai_df is None or ai_df.empty:
           return ''
       matches = ai_df[ai_df['Device Name'] == dev_name]
       if matches.empty:
           return ''
       return _safe_str(matches.iloc[0].get('Units'), '')

   def _get_y_channel_label(self):
       """Return a clean, upper-cased channel identifier for the selected
       Y-axis gauge, e.g. 'AI0', 'AI1', 'AI3'.

       Looks up the 'Channel' field in the AI settings dataframe and strips
       any device prefix (e.g. 'Dev1/ai2' → 'AI2').  Falls back to 'AI?'
       when the channel cannot be resolved.
       """
       data = self._get_y_gauge_data()
       if data is None:
           return 'AI?'
       _, dev_name = data
       ai_df = self.default_settings_input
       if ai_df is None or ai_df.empty:
           return 'AI?'
       matches = ai_df[ai_df['Device Name'] == dev_name]
       if matches.empty:
           return 'AI?'
       ch = _safe_str(matches.iloc[0].get('Channel'), '')
       # Strip device prefix: 'Dev1/ai2' → 'ai2'
       ch = ch.split('/')[-1].strip()
       return ch.upper() if ch else 'AI?'

   def _refresh_units_label(self):
       """Update the test-point and threshold labels to show the selected
       Y-axis transducer units, and update the sweep button to show the
       actual selected AI channel (e.g. 'Pressure Tuning Sweep AO0 → AI0')."""
       units = self._get_y_units() or 'units'
       if hasattr(self, '_test_points_label'):
           self._test_points_label.setText(f"Test Points (comma-separated, {units}):")
       if hasattr(self, '_open_label'):
           self._open_label.setText(f"Open (pressure, {units})")
       if hasattr(self, '_close_label'):
           self._close_label.setText(f"Close (pressure, {units})")
       if hasattr(self, '_sweep_btn'):
           self._sweep_btn.setText(
               f"Pressure Tuning Sweep AO0 → {self._get_y_channel_label()}")
       self._update_range_field()
       self._refresh_flow_labels()

   def _refresh_flow_labels(self):
       """Update the Variable Test Point sweep labels to show the selected
       X-axis flow-meter units (e.g. gpm)."""
       units = self._get_x_units() or 'units'
       if hasattr(self, '_flow_start_lbl'):
           self._flow_start_lbl.setText(f"Start ({units})")
           self._flow_step_lbl.setText(f"Step ({units})")
           self._flow_max_lbl.setText(f"Max ({units})")
       if hasattr(self, '_var_start_chk'):
           self._var_start_chk.setText(f"Per-point start ({units}):")

   def _current_sweep_sig(self):
       """Signature of the inputs that require a new hardware sweep:
         - Y-axis gauge  -- different channel = different units/Mx/b
         - Valve type    -- NC vs NO changes the valve state during the sweep

       Test points are NOT included: they are only compared against the
       reachable maximum and used as interpolation queries for the seed curve,
       so editing them never requires a re-sweep.
       """
       y = self._get_y_gauge_data()
       y_dev = y[1] if y else None
       valve = self._valve_no_nc.currentText() if hasattr(self, '_valve_no_nc') else None
       return (y_dev, valve)

   def _invalidate_sweep(self):
       """Hard reset -- forget the pressure tuning sweep entirely."""
       self._swept            = False
       self._press_seed_v     = None
       self._press_seed_p     = None
       self._max_p_reachable  = None
       self._sweep_sig        = None
       self._mark_unswept()

   def _invalidate_flow_seed(self):
       """Forget the flow seed and both DP/DQ models. Called when the X-axis
       flow meter changes (a different meter => different units/curve)."""
       self._flow_seeded      = False
       self._flow_seed_v      = None
       self._flow_seed_q      = None
       self._max_q_reachable  = None
       self._model_up         = {}
       self._model_down       = {}
       self._model            = {}
       self._model_ready      = False
       self._model_ref_p_meas = None
       self._model_leg_spread = None
       self._update_range_field()
       if hasattr(self, '_signals'):
           self._signals.enable_run.emit(False)

   def _mark_unswept(self):
       """Lock Generate DP/DQ Model, but RETAIN the sweep data so a revert can
       restore it."""
       self._swept       = False
       self._model_ready = False
       self._update_range_field()
       if hasattr(self, '_gen_btn'):
           self._gen_btn.setEnabled(False)
           self._gen_btn.setToolTip("Run the Pressure Tuning Sweep first to enable this.")
       self._signals.enable_run.emit(False)

   def _restore_swept_state(self):
       """Re-enable model generation from retained sweep data."""
       if self._press_seed_v is None or len(self._press_seed_v) < 2:
           return
       self._swept = True
       self._update_range_field()
       self._gen_btn.setEnabled(True)
       self._gen_btn.setToolTip("")

   def _sync_sweep_state_to_inputs(self):
       """Called when the Y-axis gauge or valve type changes. Only gray out
       Generate DP/DQ Model when the inputs actually differ from what was
       swept; if they are unchanged (or reverted back), restore instead of
       forcing a needless re-sweep."""
       if self._press_seed_v is None or self._sweep_sig is None:
           return
       if self._current_sweep_sig() == self._sweep_sig:
           if not self._swept:
               self._restore_swept_state()
       else:
           self._mark_unswept()

   def _on_valve_type_changed(self):
       """Valve type (NC/NO) changed.

       NC vs NO changes which DO state the tuning sweep holds, so the
       sweep data is no longer valid for the new setting. The DP/DQ model is
       stale for the same reason -- valve direction matters during the model
       sweep.
       """
       self._sync_sweep_state_to_inputs()
       if self._model_ready:
           self._model_ready = False
           self._signals.enable_run.emit(False)
           self._signals.status_changed.emit(
               "Valve type changed -- re-run Pressure Tuning Sweep then Generate "
               "\u0394P/\u0394Q Model  \u26a0"
           )

   def _on_test_points_changed(self, _text=None):
       """Test points edited.

       No per-point voltage to recompute any more -- the command is found by
       closed-loop ramp at run time. All that changes is the reachability
       warning and the staleness of the DP/DQ model (whose reference pressure
       is the highest test point).
       """
       if not hasattr(self, '_range_field'):
           return

       self._update_range_field()

       # Points are no longer screened against the swept peak -- a continuous
       # sweep lags the rig and understates it, so screening here would warn
       # about pressures the rig can actually hold.

       # NOTE: the model is built at a FIXED reference pressure and does not
       # depend on the test point list, so editing test points no longer makes
       # it stale. Nothing to invalidate here.

   def _max_test_point(self):
       """Return the greatest fixed test point pressure entered, or None."""
       tps = self._parse_test_points()
       return max(tps) if tps else None

   def _update_range_field(self):
       """Refresh the read-only pre-tuned seed voltage readout."""
       if not hasattr(self, '_range_field'):
           return
       y_units = self._get_y_units() or ''
       x_units = self._get_x_units() or ''
       parts = []

       tps = self._parse_test_points() or []
       if tps and self._press_seed_v is not None:
           seeds = []
           for tp in tps:
               v = self._seed_voltage_for_pressure(tp)
               if v is not None and np.isfinite(v):
                   seeds.append(f"{tp:g}{y_units}\u2192{v:.3f}V")
           if seeds:
               parts.append("P: " + ", ".join(seeds))

       if self._flow_seeded:
           try:
               flows = self._flow_range()
           except (ValueError, TypeError):
               flows = []
           seeds = []
           for f in flows:
               if f <= 1e-9:
                   continue
               v = self._seed_voltage_for_flow(f)
               if v is not None and np.isfinite(v):
                   seeds.append(f"{f:g}{x_units}\u2192{v:.3f}V")
           if seeds:
               parts.append("Q: " + ", ".join(seeds))

       if not parts:
           self._range_field.clear()
           self._range_field.setStyleSheet("background: #e9e9e9; color: #555;")
           return
       self._range_field.setStyleSheet("background: #e9e9e9; color: #555;")
       self._range_field.setText("   |   ".join(parts))

   def _on_calib_done(self, ok: bool):
       """UI-thread handler after a pressure tuning sweep completes."""
       if not ok:
           self._mark_unswept()
           self._signals.status_changed.emit("Sweep failed -- no usable data  \u274c")
           return

       # Snapshot the inputs this sweep is valid for.
       self._sweep_sig = self._current_sweep_sig()
       self._swept     = True
       self._gen_btn.setEnabled(True)
       self._gen_btn.setToolTip("")
       self._update_range_field()

       units = self._get_y_units()
       self._signals.status_changed.emit(
           f"Tuned \u2705  seeds ready   (peak seen {self._max_p_reachable:.1f} {units})"
       )

   # ------------------------------------------------------------------
   # Main-window locking / unlocking
   # ------------------------------------------------------------------

   def _extra_lock(self):
       """Beyond the base lock: force pen mode on, and point the plot at this
       app's channels.

       Applying the axes here rather than when a sequence starts means the plot
       is right as soon as the dialog opens. The base calls _lock_axis_controls
       straight after, which covers the paths where _apply_axes_to_main_plot
       returns early (no Y gauge picked yet, or plot_options unreadable).
       """
       mw = self.main_window
       ps = getattr(mw, 'plotSettingsWidget', None) if mw else None
       if ps and hasattr(ps, 'pen_var'):
           ps.pen_var.setChecked(True)
           ps.pen_var.setEnabled(False)
       self._apply_axes_to_main_plot()

   def _extra_unlock(self):
       mw = self.main_window
       ps = getattr(mw, 'plotSettingsWidget', None) if mw else None
       if ps and hasattr(ps, 'pen_var'):
           ps.pen_var.setEnabled(True)
           ps.pen_var.setChecked(False)

   def _lock_axis_controls(self, mw=None):
       """Disable EVERY Y-axis checkbox and the X-axis dropdown while FF is open.

       All of them, not just the pressure gauge's: ticking any other channel
       introduces a second unit, and if it sorts ahead of the pressure gauge in
       plot_options it takes the main ViewBox and pushes the overlay onto the
       wrong scale (see _apply_axes_to_main_plot). Leaving the other boxes live
       let the user silently break the plot mid-test.
       """
       if mw is None:
           mw = self.main_window
       if mw is None or not hasattr(mw, 'plotSettingsWidget'):
           return
       ps = mw.plotSettingsWidget

       try:
           import Multichannel_DAQ as _mdaq
           for i in _mdaq.plot_options.index:
               cb = getattr(ps, f'plot_check_box{i}', None)
               if cb:
                   cb.setEnabled(False)
                   cb.setToolTip("Controlled by Flow Failure — close FF to change")
       except Exception:
           pass

       # Lock X-axis dropdown
       if hasattr(ps, 'x_axis_selection'):
           ps.x_axis_selection.setEnabled(False)
           ps.x_axis_selection.setToolTip("Controlled by Flow Failure — close FF to change")

   def _unlock_axis_controls(self, mw=None):
       """Re-enable Y-axis checkbox and X-axis dropdown when FF closes."""
       if mw is None:
           mw = self.main_window
       if mw is None or not hasattr(mw, 'plotSettingsWidget'):
           return
       ps = mw.plotSettingsWidget

       # Re-enable all Y-axis checkboxes
       try:
           import Multichannel_DAQ as _mdaq
           for i in _mdaq.plot_options.index:
               cb = getattr(ps, f'plot_check_box{i}', None)
               if cb:
                   cb.setEnabled(True)
                   cb.setToolTip("")
       except Exception:
           pass

       # Re-enable X-axis dropdown
       if hasattr(ps, 'x_axis_selection'):
           ps.x_axis_selection.setEnabled(True)
           ps.x_axis_selection.setToolTip("")

   # ------------------------------------------------------------------
   # Static plot — clear hook + restore
   # ------------------------------------------------------------------

   def _install_clear_hook(self):
       mw = self.main_window
       if mw is None or not hasattr(mw, 'graphDataWidget'):
           return
       gw   = mw.graphDataWidget
       orig = gw.clear_plots
       dlg  = self

       def _patched_clear():
           orig()
           dlg._model_item         = None
           dlg._model_item_up      = None
           dlg._model_item_down    = None
           dlg._result_item        = None
           dlg._anchor_item        = None
           dlg._envelope_line      = None
           dlg._label_items        = []
           dlg._left_anchor_labeled = False
           dlg._n_anchor_labels    = 0
           dlg._restore_static_plots()

       gw.clear_plots   = _patched_clear
       self._orig_clear = orig

   def _uninstall_clear_hook(self):
       mw = self.main_window
       if mw and hasattr(mw, 'graphDataWidget') and hasattr(self, '_orig_clear'):
           mw.graphDataWidget.clear_plots = self._orig_clear

   def _get_plot_item(self):
       mw = self.main_window
       if mw and hasattr(mw, 'graphDataWidget'):
           return mw.graphDataWidget.plot_widget.plotItem
       return None

   def _ensure_on_plot(self, pi, item):
       """Guarantee `item` is actually attached to plot item `pi`.

       ROOT CAUSE of the disappearing model curves: this dialog's overlay items
       live on a plot owned by the main DAQ window, which detaches them
       whenever it rebuilds its own traces. Holding a non-None reference to an
       item therefore does NOT mean the item is still on the plot -- but the
       drawing code assumed it did, took the "already exists" branch, and only
       called setData() on an orphaned item that nothing was rendering.

       Re-adding an item that is already attached would stack duplicates, so
       membership is checked rather than blindly re-adding.
       """
       if pi is None or item is None:
           return
       try:
           if item not in pi.items:
               pi.addItem(item)
       except Exception:
           # Some pyqtgraph versions don't expose .items as a plain list;
           # fall back to the scene parentage check.
           try:
               if item.scene() is None:
                   pi.addItem(item)
           except Exception:
               pass

   def _restore_static_plots(self, final=False):
       """Add / update model curve, result scatter, connecting line, and labels.

       Called live after each point (final=False) and once more on test_finished
       (final=True) to add the bottom-right anchor (max_flow, 0).
       """
       pi = self._get_plot_item()
       if pi is None:
           return

       # Re-assert the fixed 0 → max+10% window: the live DAQ plot re-enables
       # auto-range as data streams in, which would rescale the envelope.
       if self._is_running:
           self._apply_plot_ranges()

       # ── Threshold model curves (both up and down shown) ───────────────
       # The Run Test compares against the model selected by the Sweep-up /
       # Sweep-down radio; that one is drawn emphasized, the other faint.
       active_down = (getattr(self, '_model_down_radio', None) is not None
                      and self._model_down_radio.isChecked())

       def _draw_model(model, item_attr, color, active):
           item = getattr(self, item_attr, None)
           if not model:
               return
           flows    = np.array(sorted(model.keys()),      dtype=np.float64)
           readings = np.array([model[f] for f in flows],  dtype=np.float64)
           width    = 2 if active else 1
           pen      = mkPen(color=color, width=width, style=Qt.DashLine)
           if item is None:
               item = pg.PlotDataItem(
                   flows, readings, pen=pen,
                   name=("FF: ΔP/ΔQ (up)" if 'up' in item_attr else "FF: ΔP/ΔQ (down)")
               )
               pi.addItem(item)
               setattr(self, item_attr, item)
           else:
               # The main window may have detached this item since last draw.
               self._ensure_on_plot(pi, item)
               item.setData(flows, readings)
               item.setPen(pen)

       _draw_model(self._model_up,   '_model_item_up',   self._MODEL_COLOR,    not active_down)
       _draw_model(self._model_down, '_model_item_down', self._MODEL_DN_COLOR, active_down)

       # ── Result points + connecting line + anchors + labels ────────────
       # Left anchor (0, maxP) is drawn as soon as we know max_pressure,
       # even before any measured points exist.
       try:
           max_pressure = float(max(
               float(x) for x in self._pressure_points.text().split(',') if x.strip()
           ))
       except Exception:
           max_pressure = float(max(self._result_pressure)) if self._result_pressure else 0.0
       try:
           max_flow = float(self._flow_max.text())
       except Exception:
           max_flow = float(max(self._result_flow)) if self._result_flow else 0.0

       # ── Left anchor: (0, maxP) — always shown once test starts ───────
       left_ax = np.array([0.0])
       left_ay = np.array([max_pressure])
       if self._anchor_item is None:
           self._anchor_item = pg.PlotDataItem(
               left_ax, left_ay,
               pen=None,
               symbol='s', symbolSize=10,
               symbolBrush=self._ANCHOR_COLOR,
               symbolPen=mkPen(color='#d68910', width=1),
               name="FF: Anchors"
           )
           pi.addItem(self._anchor_item)
           # Label the left anchor immediately
           txt = pg.TextItem(  # noqa: F841 (kept in _label_items)
               text=f"(0.00, {max_pressure:.2f})",
               color=self._ANCHOR_COLOR,
               anchor=(0.0, 1.0)
           )
           txt.setPos(0.0, max_pressure)
           pi.addItem(txt)
           self._label_items.append(txt)
           self._left_anchor_labeled = True
       else:
           self._ensure_on_plot(pi, self._anchor_item)

       # Labels can be detached too. Measured-point labels are rebuilt further
       # down anyway; re-attaching here is what keeps the anchor labels alive.
       for lbl in self._label_items:
           self._ensure_on_plot(pi, lbl)

       if not self._result_flow:
           return  # nothing more to draw until points arrive

       # Sort measured points by pressure ascending, carrying the failure
       # classification ("No Failure" / "Shift Failure" / "Spring Failure").
       labels_src = self._result_labels if len(self._result_labels) == len(self._result_flow) \
                    else [""] * len(self._result_flow)
       triples = sorted(zip(self._result_pressure, self._result_flow, labels_src))
       ry_meas = np.array([p for p, f, lb in triples], dtype=np.float64)
       rx_meas = np.array([f for p, f, lb in triples], dtype=np.float64)
       lb_meas = [lb for p, f, lb in triples]

       # ── Result scatter + line between measured points (live) ──────────
       if self._result_item is None:
           self._result_item = pg.PlotDataItem(
               rx_meas, ry_meas,
               pen=mkPen(color=self._RESULT_COLOR, width=2),
               symbol='o', symbolSize=10,
               symbolBrush=self._RESULT_COLOR,
               symbolPen=mkPen(color='#c0392b', width=1),
               name="FF: Envelope"
           )
           pi.addItem(self._result_item)
       else:
           self._ensure_on_plot(pi, self._result_item)
           self._result_item.setData(rx_meas, ry_meas)

       # ── Connecting line (live partial, updates each point) ────────────
       # Route: (0, maxP) → (flow@maxP, maxP) → pts desc → (flow@minP, minP)
       # On final call, extend to → (maxFlow, minP) → (maxFlow, 0)
       flow_at_max_p = float(rx_meas[-1])  # highest pressure point
       flow_at_min_p = float(rx_meas[0])   # lowest pressure point
       min_p         = float(ry_meas[0])
       rx_desc = rx_meas[::-1]
       ry_desc = ry_meas[::-1]

       if final:
           rx_full = np.concatenate([[0.0, flow_at_max_p], rx_desc,
                                     [flow_at_min_p, max_flow, max_flow]])
           ry_full = np.concatenate([[max_pressure, max_pressure], ry_desc,
                                     [min_p, min_p, 0.0]])
       else:
           # Partial line — no right anchor yet
           rx_full = np.concatenate([[0.0, flow_at_max_p], rx_desc])
           ry_full = np.concatenate([[max_pressure, max_pressure], ry_desc])

       existing_line = getattr(self, '_envelope_line', None)
       if existing_line is None:
           line_item = pg.PlotDataItem(
               rx_full, ry_full,
               pen=mkPen(color=self._RESULT_COLOR, width=1, style=Qt.DotLine),
               name=""
           )
           pi.addItem(line_item)
           self._envelope_line = line_item
       else:
           self._ensure_on_plot(pi, existing_line)
           existing_line.setData(rx_full, ry_full)

       # ── Right anchor: (maxFlow, 0) — only on final call ───────────────
       if final:
           # Update anchor item to show both anchors
           self._anchor_item.setData(
               np.array([0.0, max_flow]),
               np.array([max_pressure, 0.0])
           )
           # Label the right anchor
           txt = pg.TextItem(
               text=f"({max_flow:.2f}, 0.00)",
               color=self._ANCHOR_COLOR,
               anchor=(0.0, 1.0)
           )
           txt.setPos(max_flow, 0.0)
           pi.addItem(txt)
           self._label_items.append(txt)

       # ── Text labels for measured points — rebuild each update ─────────
       # Remove previous measured-point labels (keep anchor labels at end)
       n_anchor_labels = getattr(self, '_n_anchor_labels', 0)
       # Remove all non-anchor labels
       for lbl in self._label_items[n_anchor_labels:]:
           try: pi.removeItem(lbl)
           except Exception: pass
       self._label_items = self._label_items[:n_anchor_labels]

       # Color per classification for the point labels.
       # Shift Failure and Spring Failure are the two forms of flow failure.
       _LABEL_COLORS = {
           "No Failure":     "#27ae60",   # green
           "Shift Failure":  "#e74c3c",   # red
           "Spring Failure": "#e67e22",   # orange
           "Unreachable":    "#7f8c8d",   # gray — flow point never achieved
       }
       for x, y, lb in zip(rx_meas, ry_meas, lb_meas):
           text = f"{lb}\n({x:.2f}, {y:.2f})" if lb else f"({x:.2f}, {y:.2f})"
           txt = pg.TextItem(
               text=text,
               color=_LABEL_COLORS.get(lb, self._RESULT_COLOR),
               anchor=(0.0, 1.0)
           )
           txt.setPos(x, y)
           pi.addItem(txt)
           self._label_items.append(txt)

       if final:
           # Freeze the anchor label count so future calls don't remove them
           self._n_anchor_labels = len(self._label_items)

   def _remove_result_item(self):
       pi = self._get_plot_item()
       for attr in ('_result_item', '_anchor_item', '_envelope_line'):
           item = getattr(self, attr, None)
           if pi and item is not None:
               try: pi.removeItem(item)
               except Exception: pass
           setattr(self, attr, None)
       for lbl in self._label_items:
           if pi:
               try: pi.removeItem(lbl)
               except Exception: pass
       self._label_items         = []
       self._left_anchor_labeled = False
       self._n_anchor_labels     = 0

   # ------------------------------------------------------------------
   # Close guard
   # ------------------------------------------------------------------

   def _on_close_cleanup(self):
       """Teardown the base class does not know about.

       The expert window is a top-level QDialog, so it would otherwise stay on
       screen after Flow Failure is dismissed. hide(), not close(), so its field
       values survive for the next time the app is opened.
       """
       self._uninstall_clear_hook()
       if self._expert_win is not None:
           self._expert_win.hide()

   # ------------------------------------------------------------------
   # Helpers
   # ------------------------------------------------------------------

   def _warn_ao_count(self):
       QMessageBox.warning(
           self, "Insufficient AO Channels",
           "Flow Failure requires two Analog Output channels.\n\n"
           "Only one (or zero) AO channels are configured in Settings.\n"
           "Please add a second AO channel in DAQ Settings."
       )

   def _on_var_start_toggled(self, checked):
       self._var_starts.setEnabled(checked)
       self._flow_start.setEnabled(not checked)

   def _flow_range(self, start=None):
       """Flow setpoints to test, ascending.

       NOT truncated at the peak flow seen during the model sweep. That peak
       comes from a continuous ramp, which lags the rig and understates it, so
       truncating would silently drop flows the rig can actually deliver. A
       flow that really is out of reach is discovered by the ramp timing out.
       """
       s    = float(self._flow_start.text()) if start is None else start
       step = float(self._flow_step.text())
       maxv = float(self._flow_max.text())
       out, f = [], s
       while f <= maxv + 1e-9:
           out.append(round(f, 5))
           f += step
       return out

   def _closest_model_key(self, flow):
       return min(self._model.keys(), key=lambda k: abs(k - flow))

   def _get_ao_row(self, role):
       """Return the output settings row chosen for an AO role:
       role 0 = Fixed Test Point, role 1 = Variable / Sweep. Falls back to the
       positional row (legacy behavior) if the selector isn't available."""
       df = self.default_settings_output
       if df is None or len(df) == 0:
           return None
       idx = None
       combo = getattr(self, '_ao_role_combos', {}).get(role)
       if combo is not None:
           data = combo.currentData()
           if data is not None:
               try:
                   idx = int(data)
               except (TypeError, ValueError):
                   idx = None
       if idx is None:
           # A selector that exists but is blank means 'not chosen' -- do
           # NOT fall back to a positional row, or a cleared dropdown could
           # silently resolve to the channel the other role is using.
           if combo is not None and combo.count() > 0:
               return None
           idx = role   # legacy positional fallback (no selector present)
       return df.iloc[idx] if 0 <= idx < len(df) else None

   def _update_ao_corner_text(self):
       """Reflect the currently selected AO channels in each section's corner."""
       for role, grp in ((0, getattr(self, '_fixed_grp', None)),
                         (1, getattr(self, '_var_grp', None))):
           if grp is None:
               continue
           row = self._get_ao_row(role)
           if row is None:
               grp.set_corner_text("(not configured)")
           else:
               ch = _safe_str(row.get('Channel'), f'ao{role}')
               nm = _safe_str(row.get('Device Name'), ch)
               grp.set_corner_text(f"{ch}  ({nm})")

   def _on_ao_selection_changed(self, *_):
       # A single AO cannot drive two valves — keep the two role selections
       # mutually exclusive before anything else reads them.
       self._enforce_ao_exclusive(self.sender())
       self._update_ao_corner_text()
       self._refresh_units_label()

   def _enforce_ao_exclusive(self, changed_combo=None):
       """Never let the Fixed Test Point AO and the Variable / Sweep AO point
       at the same output row.

       Every channel stays selectable in both dropdowns. When a selection
       collides with the other role, the OTHER dropdown is cleared (left
       blank) so the user re-picks it deliberately -- same behaviour as the
       X / Y gauge dropdowns.
       """
       fixed = getattr(self, '_ao_fixed_combo', None)
       var   = getattr(self, '_ao_var_combo', None)
       if fixed is None or var is None:
           return
       if getattr(self, '_ao_exclusive_busy', False):
           return
       self._ao_exclusive_busy = True
       try:
           # Keep every item selectable in both dropdowns.
           for combo in (fixed, var):
               model = combo.model()
               if model is None or not hasattr(model, 'item'):
                   continue
               for i in range(combo.count()):
                   item = model.item(i)
                   if item is not None and not item.isEnabled():
                       item.setEnabled(True)

           # Whichever dropdown the user just touched wins; clear the other
           # one if it now points at the same output row.
           src, other = (var, fixed) if changed_combo is var else (fixed, var)
           src_data = src.currentData()
           if src_data is not None and other.currentData() == src_data:
               other.blockSignals(True)
               other.setCurrentIndex(-1)
               other.blockSignals(False)
       finally:
           self._ao_exclusive_busy = False

   def _do_device(self, channel_str):
       """Device/module a digital line lives on, resolved from the digital
       settings by channel, else selected_daq."""
       df = self.default_settings_dig
       try:
           if (df is not None and 'DAQ' in df.columns and 'Channel' in df.columns):
               m = df[df['Channel'].astype(str) == str(channel_str)]
               if not m.empty:
                   v = m.iloc[0].get('DAQ')
                   s = "" if v is None else str(v).strip()
                   if s and s.lower() != 'nan':
                       return s
       except Exception:
           pass
       return self.selected_daq

   def _get_y_gauge_data(self):
       """Return (rb_idx, device_name) for the Y-axis gauge, or None."""
       return self._y_gauge_combo.currentData()

   def _get_x_gauge_data(self):
       """Return (rb_idx, device_name) for the X-axis gauge, or None."""
       return self._x_gauge_combo.currentData()

   def _get_do_channel(self):
       idx = self._do_combo.currentIndex()
       return self._do_combo.itemData(idx) if idx >= 0 else None

   def _do_write(self, channel_str, state: bool):
       if not channel_str:
           return
       try:
           with nidaqmx.Task() as t:
               t.do_channels.add_do_chan(f"{self._do_device(channel_str)}/{channel_str}")
               t.write(state)
           # Notify main app so CalibrationWindow stays in sync
           mw = getattr(self, 'main_window', None)
           if mw is not None and hasattr(mw, 'notify_digital_state'):
               mw.notify_digital_state(channel_str, bool(state))
       except Exception:
           print(f"Flow Failure: DO write error ({channel_str} → {state}):\n"
                 + traceback.format_exc())

   def _valve_open(self, do_ch):
       """Energize the valve (open it).
       NC valve: write True  (energize solenoid → opens)
       NO valve: write False (de-energize solenoid → opens)
       """
       nc = self._valve_no_nc.currentText() == "Normally Closed"
       self._do_write(do_ch, True if nc else False)

   def _valve_close(self, do_ch):
       """De-energize the valve (close it).
       NC valve: write False (de-energize solenoid → closes)
       NO valve: write True  (energize solenoid → closes)
       """
       nc = self._valve_no_nc.currentText() == "Normally Closed"
       self._do_write(do_ch, False if nc else True)

   def _valve_pulse(self, do_ch, final_state: bool, cycles: int, freq_hz: float):
       """Chatter the valve solenoid `cycles` times, ending in `final_state`.

       Operates on the raw solenoid state (energized = DO True), so it is
       independent of NO/NC polarity — energizing always drives the line True
       and de-energizing always drives it False, exactly like the single-switch
       energize/de-energize transitions in the run worker.

       final_state True  (energize)   → on, off, on, off, … , on   (N on, N-1 off)
       final_state False (de-energize)→ off, on, off, on, … , off  (N off, N-1 on)

       Each intermediate on/off state is held for half a period (1/(2·f)). The
       final state is written and left applied — the caller's read delay covers
       settling before the measurement is taken.
       """
       try:
           n = max(1, int(cycles))
       except (TypeError, ValueError):
           n = 1
       try:
           f = float(freq_hz)
       except (TypeError, ValueError):
           f = 0.0
       half = (1.0 / (2.0 * f)) if f > 0 else 0.0

       # Sequence ends in final_state: [final, !final, final, … , final]
       # → 2n-1 states, n of them == final_state.
       total = 2 * n - 1
       for i in range(total):
           state = final_state if (i % 2 == 0) else (not final_state)
           self._do_write(do_ch, state)
           if self._abort:
               # Leave the valve in the requested final state on abort.
               if state is not final_state:
                   self._do_write(do_ch, final_state)
               return
           # Hold every state except the last; the last is held by the
           # caller's subsequent read delay.
           if half > 0 and i < total - 1:
               time.sleep(half)

   def _valve_energize(self, do_ch, cycles, freq_hz):
       """Energize the valve with `cycles` pulses, ending energized (on)."""
       self._valve_pulse(do_ch, True, cycles, freq_hz)

   def _valve_deenergize(self, do_ch, cycles, freq_hz):
       """De-energize the valve with `cycles` pulses, ending de-energized (off)."""
       self._valve_pulse(do_ch, False, cycles, freq_hz)

   def _ao_write(self, ao_row, voltage):
       ch    = _safe_str(ao_row.get('Channel'), '')
       min_v = float(ao_row.get('Min', 0.0))
       max_v = float(ao_row.get('Max', 5.0))
       _write_dc(self._ao_device(ao_row), ch, voltage, min_v, max_v,
                 self._ao_type(ao_row))
       # Publish the driven voltage so the main app's output channel display
       # reflects it (AO is output-only — no hardware read-back).
       self._notify_output(ch, float(np.clip(voltage, min_v, max_v)))

   # ------------------------------------------------------------------
   # UI state helpers
   # ------------------------------------------------------------------

   def _set_controls_running(self, running: bool):
       """Lock/unlock controls during a worker. Run button managed separately."""
       # Generate ΔP/ΔQ Model is available only when not running AND swept.
       if not running:
           self._gen_btn.setEnabled(self._swept)
           self._gen_btn.setToolTip(
               "" if self._swept else "Run the Pressure Tuning Sweep first to enable this."
           )
       else:
           self._gen_btn.setEnabled(False)

       self._force_stop_btn.setEnabled(running)
       self._close_btn.setEnabled(not running)
       for w in (self._pressure_points, self._flow_start, self._flow_step,
                 self._flow_max, self._var_starts, self._ao_settle_flow,
                 self._delay_valve_read, self._open_pressure, self._close_pressure,
                 self._actuation_cycles, self._actuation_freq,
                 self._y_gauge_combo, self._x_gauge_combo,
                 self._do_combo, self._valve_no_nc,
                 self._model_up_radio, self._model_down_radio,
                 self._sweep_btn,
                 self._exp_flow_mid_pct, self._exp_p_step_pct, self._exp_p_settle,
                 self._exp_p_band_pct, self._exp_p_band_min, self._exp_p_timeout,
                 self._exp_q_step_pct, self._exp_q_settle, self._exp_q_band_pct,
                 self._exp_q_band_min, self._exp_q_timeout,
                 self._exp_p_sweep_secs, self._exp_q_sweep_secs,
                 self._exp_p_stab, self._exp_q_stab):
           w.setEnabled(not running)

       if not running:
           # Per-point start field follows its checkbox, not the global unlock;
           # the global flow start is its mirror image.
           checked = self._var_start_chk.isChecked()
           self._var_starts.setEnabled(checked)
           self._flow_start.setEnabled(not checked)

   def _run_btn_set_enabled(self, enabled: bool):
       self._run_btn.setEnabled(enabled)

   # ------------------------------------------------------------------
   # Signal handlers (UI thread)
   # ------------------------------------------------------------------

   def _on_model_ready(self):
       self._restore_static_plots()
       # Publish the reachable range now that max flow is known.
       self._update_range_field()

   def _on_point_recorded(self, pressure, flow, label):
       self._restore_static_plots()

   # ------------------------------------------------------------------
   # TROUBLESHOOTING run log  (temporary — remove when no longer needed)
   # ------------------------------------------------------------------

   def _log(self, msg=""):
       """Append one line to the troubleshooting run log."""
       self._run_log.append(str(msg))

   def _show_run_log(self):
       """Pop up the full action/reading log from the last run."""
       if not self._run_log:
           return
       text = "\n".join(self._run_log)
       dlg = QDialog(self)
       dlg.setWindowTitle("Run Log (troubleshooting)")
       dlg.resize(760, 620)
       lay = QVBoxLayout(dlg)
       box = QPlainTextEdit(text)
       box.setReadOnly(True)
       box.setLineWrapMode(QPlainTextEdit.NoWrap)
       box.setStyleSheet("font-family: Consolas, monospace; font-size: 11px;")
       lay.addWidget(box)
       btns = QHBoxLayout()
       copy_btn = QPushButton("Copy to Clipboard")
       copy_btn.clicked.connect(
           lambda: QApplication.clipboard().setText(text))
       close_btn = QPushButton("Close")
       close_btn.clicked.connect(dlg.accept)
       btns.addWidget(copy_btn)
       btns.addStretch(1)
       btns.addWidget(close_btn)
       lay.addLayout(btns)
       dlg.exec_()

   def _on_test_finished(self):
       n = len(self._result_pressure)
       self._signals.status_changed.emit(f"Test complete — {n} point(s)  ✅")
       self._restore_static_plots(final=True)
       # TROUBLESHOOTING: show every action / reading from the run.
       self._show_run_log()

   # ------------------------------------------------------------------
   # Generate Threshold Model
   # ------------------------------------------------------------------

   def _generate_model(self):
       """Build the DP/DQ threshold model: pressure vs measured flow with the
       test valve OPEN, at a fixed reference inlet command.

       This is what the shift and spring checks compare against, and it also
       produces the seed voltages for the flow points.

       Unlike the pressure tuning sweep, this model's axes are BOTH readings
       -- pressure against measured flow, with the command nowhere in the
       relationship. That is what makes a swept model defensible: command
       lag and valve hysteresis move samples ALONG the curve (you land on a
       different flow than you would have) rather than lifting them OFF it.

       What does contaminate it is anything that makes pressure a function of
       flow's history rather than of flow: mismatched transducer time
       constants, line volume, compressibility. Those separate the up and
       down legs, so the spread between the legs is logged as a direct
       measure of how much of it is present. Legs that overlay mean the sweep
       duration is honest; legs that separate mean it is too fast (or the
       article itself is direction-dependent -- re-run at two durations to
       tell those apart, since only the dynamic component shrinks).
       """
       if not self._swept:
           QMessageBox.warning(self, "Not Ready",
                               "Run the Pressure Tuning Sweep before generating "
                               "the model.")
           return
       if self._ao_count < 2:
           self._warn_ao_count(); return

       ao0        = self._get_ao_row(0)
       ao1        = self._get_ao_row(1)
       y_data     = self._get_y_gauge_data()
       x_data     = self._get_x_gauge_data()
       do_ch      = self._get_do_channel()

       if ao0 is None or ao1 is None:
           QMessageBox.warning(self, "Error", "AO channels not configured."); return
       if y_data is None:
           QMessageBox.warning(self, "Error", "No Y-axis pressure gauge selected."); return
       if x_data is None:
           QMessageBox.warning(self, "Error", "No X-axis flow meter selected."); return

       y_rb_idx, _ = y_data
       x_rb_idx, _ = x_data

       # Reference inlet command.
       #
       # Set by VOLTAGE, not by searching for a pressure. The model is built
       # with the valve OPEN, where the pressure at the transducer is set by
       # the flow through the valve rather than by what was dialled in at the
       # inlet -- the inlet only has to be high enough to deliver flow. So
       # commanding the voltage that the tuning sweep associated with the
       # reference pressure is sufficient, and it removes an entire
       # closed-loop pressure search from every model run.
       #
       # If the rig cannot make the reference, the seed curve simply has no
       # voltage for it and this falls through to full command -- which is the
       # right answer anyway: use everything available.
       ao0_lo, ao0_hi = self._ao_limits(ao0)
       ref_p = self._model_ref_p()
       v_ref = self._seed_voltage_for_pressure(ref_p)
       if v_ref is None or not np.isfinite(v_ref):
           v_ref = ao0_hi
       v_ref = float(np.clip(min(v_ref, ao0_hi), ao0_lo, ao0_hi))

       ao1_min, ao1_max = self._ao_limits(ao1)
       if ao1_max <= ao1_min:
           QMessageBox.warning(self, "Error",
               "Variable / Sweep AO Max must be positive -- the flow command "
               "cannot go negative."); return

       # Flow sweep grid is defined in flow-meter units (e.g. gpm).
       try:
           step_q = float(self._flow_step.text())
           max_q  = float(self._flow_max.text())
       except ValueError:
           QMessageBox.warning(self, "Bad Input",
               "Enter Flow Step and Max (numbers) before generating the model."); return
       if step_q <= 0 or max_q <= 0:
           QMessageBox.warning(self, "Bad Input", "Flow Step and Max must be positive."); return

       # Model grid (flow units): 0 -> max in user steps.
       grid = []
       g = 0.0
       while g <= max_q + 1e-9:
           grid.append(round(g, 6))
           g += step_q

       # Clear any prior model curve AND result scatter -- old results are
       # invalid once a new model is generated.
       pi = self._get_plot_item()
       for attr in ('_model_item', '_model_item_up', '_model_item_down', '_result_item'):
           item = getattr(self, attr, None)
           if pi and item is not None:
               try: pi.removeItem(item)
               except Exception: pass
           setattr(self, attr, None)
       self._result_pressure = []
       self._result_flow     = []
       self._result_labels   = []

       self._model            = {}
       self._model_up         = {}
       self._model_down       = {}
       self._model_ready      = False
       self._flow_seeded      = False
       self._max_q_reachable  = None
       self._model_ref_p_meas = None
       self._model_leg_spread = None
       self._run_log          = []
       self._signals.enable_run.emit(False)
       self._set_controls_running(True)
       self._signals.status_changed.emit("Generating \u0394P/\u0394Q model\u2026")

       prm      = self._search_params()
       y_units  = self._get_y_units() or 'units'

       # Ensure DAQ is running so we can read from the ring buffer
       mw = self.main_window
       if mw and not getattr(mw, 'continue_running', False):
           try:
               mw.start_task()
           except Exception:
               print(f"Flow Failure -- could not auto-start DAQ:\n{traceback.format_exc()}")

       # Lock the plot onto the selected channels / fixed axis window.
       self._apply_axes_to_main_plot()
       self._apply_plot_ranges()

       def _resample(q_list, p_list, grid_pts):
           """Resample a (flow, pressure) sweep onto the flow grid -> {gpm: P}."""
           qa = np.asarray(q_list, dtype=np.float64)
           pa = np.asarray(p_list, dtype=np.float64)
           if qa.size < 2:
               return {}
           order = np.argsort(qa)
           qa, pa = qa[order], pa[order]
           keep = np.concatenate(([True], np.diff(qa) > 0))
           qa, pa = qa[keep], pa[keep]
           if qa.size < 2:
               return {}
           return {float(gp): float(np.interp(gp, qa, pa)) for gp in grid_pts}

       def _worker():
           try:
               ao_settle  = float(self._ao_settle_flow.text())
               delay_read = float(self._delay_valve_read.text())

               # Known de-commanded start state.
               self._ao_write(ao0, ao0_lo)
               self._ao_write(ao1, ao1_min)

               # ── Establish the reference inlet command ──────────────────
               self._valve_close(do_ch)
               time.sleep(delay_read)
               self._ao_write(ao0, v_ref)
               self._log(f"MODEL reference command {v_ref:.3f} V "
                         f"(target {ref_p:g} {y_units})")
               # Let the dead-head pressure build before recording what the
               # command actually produced. The model is only interpretable
               # relative to its reference, so this number is recorded rather
               # than assumed to be the reference.
               time.sleep(max(ao_settle, prm['p_stab']))
               p_ref = self._read_gauge_from_rb(y_rb_idx)
               if p_ref is None:
                   raise RuntimeError(
                       "Could not read the gauge -- ensure the main DAQ is "
                       "running before generating the model.")
               self._model_ref_p_meas = float(p_ref)
               self._log(f"MODEL reference measured {p_ref:.1f} {y_units}")

               if self._abort:
                   self._signals.status_changed.emit("Model generation aborted  \u26a0")
                   self._signals.enable_run.emit(False)
                   return

               # Open the valve for the whole sweep and hold the reference
               # command. AO0 is NOT touched again.
               self._valve_open(do_ch)
               time.sleep(delay_read)

               v_cmd, cols, apex = self._sweep_ao_continuous(
                   ao1, [x_rb_idx, y_rb_idx], prm['q_sweep_secs'],
                   both_legs=True, status_prefix="\u0394P/\u0394Q sweep\u2026")
               q_all, p_all = cols[0], cols[1]

               # Return valve to safe state and zero outputs
               self._valve_close(do_ch)
               self._ao_write(ao0, ao0_lo)
               self._ao_write(ao1, ao1_min)

               if self._abort:
                   self._signals.status_changed.emit("Model generation aborted  \u26a0")
                   self._signals.enable_run.emit(False)
                   return

               if len(v_cmd) < 4 or apex < 2:
                   self._signals.status_changed.emit(
                       "Model ERROR -- too few sweep points  \u274c")
                   self._signals.enable_run.emit(False)
                   return

               v_up, q_up, p_up = v_cmd[:apex], q_all[:apex], p_all[:apex]
               q_dn, p_dn       = q_all[apex:], p_all[apex:]

               # Flow seed (flow -> AO1 voltage) comes from the UP sweep, since
               # the run test always searches flow upward. This is a starting
               # guess for the closed-loop ramp, not a command.
               self._flow_seed_v = np.asarray(v_up, dtype=np.float64)
               self._flow_seed_q = np.asarray(q_up, dtype=np.float64)
               self._flow_seeded = True

               # Peak flow seen. INFORMATIONAL only -- it does not truncate the
               # flow grid, for the same reason the pressure peak no longer
               # drops test points: a swept peak understates a lagging rig.
               self._max_q_reachable = float(np.max(np.concatenate([q_up, q_dn]))) \
                   if q_dn.size else float(np.max(q_up))

               # Two DP/DQ models keyed on flow (gpm): pressure vs flow.
               self._model_up   = _resample(q_up, p_up, grid)
               self._model_down = _resample(q_dn, p_dn, grid) if q_dn.size \
                   else dict(self._model_up)

               # Spread between the legs -- the honesty check on sweep speed.
               shared = [k for k in self._model_up if k in self._model_down]
               if shared:
                   self._model_leg_spread = float(np.mean(
                       [abs(self._model_up[k] - self._model_down[k])
                        for k in shared]))
                   self._log(f"MODEL leg spread {self._model_leg_spread:.2f} "
                             f"{y_units} mean over {len(shared)} grid points")
                   self._log("  (large spread => sweep too fast for the rig, "
                             "or a direction-dependent article)")

               self._sync_active_model()
               self._model_ready = True
               x_units = self._get_x_units() or 'flow'
               spread_txt = (f"   spread {self._model_leg_spread:.1f} {y_units}"
                             if self._model_leg_spread is not None else "")
               self._signals.status_changed.emit(
                   f"READY \u2705  \u0394P/\u0394Q at {p_ref:.0f} {y_units}   "
                   f"up {len(self._model_up)} / down {len(self._model_down)} pts   "
                   f"max flow {self._max_q_reachable:.2f} {x_units}{spread_txt}"
               )
               self._signals.model_ready_sig.emit()
               self._signals.enable_run.emit(True)

           except Exception:
               print(f"Flow Failure -- model worker error:\n{traceback.format_exc()}")
               self._signals.status_changed.emit("Model ERROR -- see log  \u274c")
               self._signals.enable_run.emit(False)
           finally:
               self._is_running = False
               self._abort      = False
               self._signals.controls_unlock.emit()  # -> _set_controls_running(False)
               self._signals.stop_daq_sig.emit()     # -> mw.stop_task()

       self._is_running = True
       self._abort      = False
       self._worker_thread = threading.Thread(target=_worker, daemon=True)
       self._worker_thread.start()

   # ------------------------------------------------------------------
   # Run Test
   # ------------------------------------------------------------------

   def _run_test(self):
       if not self._swept:
           QMessageBox.warning(self, "Not Ready",
                               "Run the Pressure Tuning Sweep before running the test.")
           return
       if not self._model_ready:
           QMessageBox.warning(self, "Not Ready", "Generate the \u0394P/\u0394Q model first.")
           return
       if self._ao_count < 2:
           self._warn_ao_count(); return

       # Normalise the point list first: the test walks pressure upward only.
       self._normalize_test_points()

       try:
           pressures     = self._parse_test_points()
           if pressures is None:
               raise ValueError("Test points must be numbers.")
           open_tol      = float(self._open_pressure.text())    # absolute, Y-units
           close_tol     = float(self._close_pressure.text())   # absolute, Y-units
           default_start = float(self._flow_start.text())
           flow_units    = self._get_x_units() or 'flow'

           if self._var_start_chk.isChecked():
               raw_vs     = [x.strip() for x in self._var_starts.text().split(",") if x.strip()]
               var_starts = [float(v) for v in raw_vs]
               while len(var_starts) < len(pressures):
                   var_starts.append(default_start)
           else:
               var_starts = [default_start] * len(pressures)
       except ValueError as e:
           QMessageBox.warning(self, "Bad Input", str(e)); return

       if not pressures:
           QMessageBox.warning(self, "Bad Input",
               "Enter at least one pressure test point."); return

       # Test points are NOT screened against the swept peak. A continuous
       # sweep lags the rig and understates what it can hold, so screening
       # would silently refuse pressures that are actually reachable. An
       # out-of-reach point is left to the ramp, which times out and records
       # it as unreached -- slower, but it cannot produce a false negative.
       try:
           flow_max_req = float(self._flow_max.text())
       except (ValueError, TypeError):
           QMessageBox.warning(self, "Bad Input",
               "Enter Flow Max (a number) before running."); return
       for vs in var_starts:
           if vs > flow_max_req + 1e-9:
               QMessageBox.warning(self, "Bad Input",
                   f"Sweep start {vs:g} exceeds Flow Max "
                   f"({flow_max_req:g} {flow_units}).")
               return

       ao0    = self._get_ao_row(0)
       ao1    = self._get_ao_row(1)
       y_data = self._get_y_gauge_data()
       do_ch  = self._get_do_channel()

       if ao0 is None or ao1 is None or y_data is None:
           QMessageBox.warning(self, "Error", "AO/gauge channels not configured."); return

       # One AO cannot drive both valves.
       if _safe_str(ao0.get('Channel')) == _safe_str(ao1.get('Channel')):
           QMessageBox.warning(self, "Error",
               "Fixed Test Point AO and Variable / Sweep AO are the same channel.\n"
               "Assign them to two different output channels."); return

       if not self._flow_seeded:
           QMessageBox.warning(self, "Not Ready",
               "No flow data -- regenerate the \u0394P/\u0394Q model before running "
               "the test."); return

       x_data = self._get_x_gauge_data()
       if x_data is None:
           QMessageBox.warning(self, "Error", "No X-axis flow meter selected."); return
       x_rb_idx = x_data[0]

       # Lock in the model selected by the Sweep-up / Sweep-down radio.
       self._sync_active_model()
       if not self._model:
           QMessageBox.warning(self, "Not Ready",
               "Selected threshold model is empty -- regenerate the \u0394P/\u0394Q model.")
           return

       y_rb_idx, _ = y_data
       y_units     = self._get_y_units() or 'units'
       prm         = self._search_params()

       # Resolve the flow grid for every pressure point up front, on the UI
       # thread, so the worker never has to read a QLineEdit mid-run.
       try:
           flows_per_point = [self._flow_range(start=vs) for vs in var_starts]
       except (ValueError, TypeError):
           QMessageBox.warning(self, "Bad Input",
               "Enter Flow Start / Step / Max (numbers) before running."); return
       if not any(flows_per_point):
           QMessageBox.warning(self, "Nothing To Test",
               "No testable flow points. Check Start / Step / Max against the "
               "maximum reachable flow.")
           return

       self._result_pressure = []
       self._result_flow     = []
       self._result_labels   = []
       self._run_log         = []
       self._remove_result_item()

       self._signals.enable_run.emit(False)
       self._set_controls_running(True)
       self._signals.status_changed.emit("Running test\u2026")

       # Ensure DAQ is running (ring buffer must be live to read from it)
       mw = self.main_window
       if mw and not getattr(mw, 'continue_running', False):
           try:
               mw.start_task()
           except Exception:
               print(f"Flow Failure -- could not auto-start DAQ:\n{traceback.format_exc()}")

       self._apply_axes_to_main_plot()
       self._apply_plot_ranges()
       self._restore_static_plots()

       def _worker():
           try:
               ao_settle  = float(self._ao_settle_flow.text())
               delay_read = float(self._delay_valve_read.text())
               is_nc = self._valve_no_nc.currentText() == "Normally Closed"

               try:
                   act_cycles = max(1, int(float(self._actuation_cycles.text())))
               except (ValueError, TypeError):
                   act_cycles = 1
               try:
                   act_freq = float(self._actuation_freq.text())
               except (ValueError, TypeError):
                   act_freq = 0.0

               flow_mid = self._ao_pct_to_voltage(ao1, prm['flow_mid_pct'])

               # Known de-commanded start state.
               self._ao_write(ao0, 0.0)
               self._ao_write(ao1, 0.0)

               self._log(f"VALVE TYPE: {'Normally Closed' if is_nc else 'Normally Open'}")
               self._log(f"Open tolerance:  {open_tol:g} {y_units}  "
                         f"(reading <= model + tol, valve open)")
               self._log(f"Close tolerance: {close_tol:g} {y_units}  "
                         f"(reading >= setpoint - tol, valve seated)")
               self._log(f"Delays: AO settle (flow) {ao_settle:g}s   "
                         f"valve->read {delay_read:g}s")
               self._log(f"Cycling: {act_cycles} cycle(s) @ {act_freq:g} Hz")
               self._log(f"Flow cmd during pressure set: {prm['flow_mid_pct']:g}% "
                         f"of AO range ({flow_mid:.3f} V)")
               # Peaks seen during the tuning sweeps. Informational only --
               # they do not drop test points, because a swept peak understates
               # a rig that lags the command.
               if self._max_p_reachable is not None:
                   self._log(f"Sweep peak pressure: {self._max_p_reachable:.1f} {y_units} "
                             f"(not a limit)")
               if self._max_q_reachable is not None:
                   self._log(f"Sweep peak flow:     {self._max_q_reachable:.2f} {flow_units} "
                             f"(not a limit)")
               if self._model_ref_p_meas is not None:
                   self._log(f"Model reference:     {self._model_ref_p_meas:.1f} {y_units}")
               self._log("Commands are found by closed-loop ramp, then LATCHED "
                         "for the whole point -- no live control during checks.")
               self._log("")

               # Highest pressure command known to sit at or below target.
               # AO0 is never zeroed between pressure points, so this is where
               # the actuator physically is when the next ramp starts.
               last_p_v = None

               for idx, p in enumerate(pressures):
                   if self._abort:
                       break

                   self._log("=" * 68)
                   self._log(f"PRESSURE POINT {idx+1}/{len(pressures)}: {p:g} {y_units}")
                   self._signals.status_changed.emit(
                       f"Setting {p:g} {y_units}  ({idx+1}/{len(pressures)})")

                   # ══════════════════════════════════════════════════════
                   # PRESSURE SETTING SEQUENCE
                   #   1. close the test valve
                   #        NC -> de-energize   NO -> energize
                   #   2. flow command to mid-range (clears the flow valve
                   #      deadband so pressure can build)
                   #   3. ramp the pressure command until the setpoint is read
                   #   4. flow command back to 0
                   # ══════════════════════════════════════════════════════
                   self._valve_close(do_ch)
                   self._log(f"  1. valve CLOSED "
                             f"({'de-energized' if is_nc else 'energized'})")
                   time.sleep(delay_read)

                   self._ao_write(ao1, flow_mid)
                   self._log(f"  2. flow cmd -> mid {flow_mid:.3f} V")
                   time.sleep(ao_settle)

                   self._log("  3. ramping pressure command\u2026")
                   p_band = self._band_for(p, prm['p_band_pct'], prm['p_band_min'])
                   v_ao0, p_meas, p_ok = self._search_and_latch(
                       ao0, y_rb_idx, p, self._seed_voltage_for_pressure(p),
                       prm['p_step_pct'], prm['p_settle'], p_band,
                       prm['p_timeout'], "pressure", y_units,
                       last_good_v=last_p_v, stab_dwell=prm['p_stab'])

                   self._ao_write(ao1, 0.0)
                   self._log("  4. flow cmd -> 0.000 V")
                   time.sleep(ao_settle)

                   if self._abort:
                       break
                   if not p_ok:
                       # Could not establish the setpoint -- nothing measured at
                       # this pressure is meaningful, so it is logged and skipped
                       # rather than plotted as a result.
                       self._log(f"  PRESSURE NOT REACHED (got {p_meas:.1f} "
                                 f"{y_units}) -- point skipped")
                       self._signals.status_changed.emit(
                           f"\u26a0 {p:g} {y_units} not reached -- skipped")
                       continue

                   last_p_v = v_ao0
                   self._log(f"  PRESSURE LATCHED at {v_ao0:.3f} V "
                             f"(read {p_meas:.2f} {y_units}) -- held for this point")

                   # ══════════════════════════════════════════════════════
                   # FLOW LOOP
                   # ══════════════════════════════════════════════════════
                   flows = flows_per_point[idx]
                   if not flows:
                       self._log("  No testable flow points at this pressure.")
                       continue

                   # Open the valve to begin flowing.
                   #   NC -> energize    NO -> de-energize
                   self._valve_open(do_ch)
                   self._log(f"  valve OPEN "
                             f"({'energized' if is_nc else 'de-energized'}) "
                             f"-- starting flow loop")
                   time.sleep(delay_read)

                   # AO1 is driven to mid-range and back to 0 while the
                   # pressure is set, so nothing carries over from the previous
                   # pressure point -- the flow floor restarts here.
                   last_q_v     = None
                   record_flow  = 0.0
                   result_label = None
                   any_passed   = False

                   for j, f in enumerate(flows):
                       if self._abort:
                           break

                       # A commanded flow of 0 shuts the flow control valve, so
                       # no supply reaches the test article and neither check
                       # means anything. Skip to the first real step.
                       if f <= 1e-9 and len(flows) > 1:
                           self._log(f"  SKIP 0 {flow_units} step "
                                     f"(no flow => no pressure at test article)")
                           continue

                       is_last = (j == len(flows) - 1)

                       self._log("")
                       self._log(f"  FLOW POINT {j+1}/{len(flows)}: {f:g} {flow_units}"
                                 + ("  (last)" if is_last else ""))
                       self._signals.status_changed.emit(
                           f"{p:g} {y_units}  \u2192  seeking {f:g} {flow_units} "
                           f"({idx+1}/{len(pressures)})")

                       q_band = self._band_for(f, prm['q_band_pct'], prm['q_band_min'])
                       v_ao1, q_meas, q_ok = self._search_and_latch(
                           ao1, x_rb_idx, f, self._seed_voltage_for_flow(f),
                           prm['q_step_pct'], prm['q_settle'], q_band,
                           prm['q_timeout'], "flow", flow_units,
                           last_good_v=last_q_v, symmetric=True,
                           stab_dwell=prm['q_stab'])

                       if self._abort:
                           break

                       if not q_ok:
                           # Flow unreachable. Everything below it already
                           # passed, so this behaves like running out of flow
                           # points rather than like a failure.
                           self._log(f"    FLOW NOT REACHED (got {q_meas:.2f} "
                                     f"{flow_units})")
                           if any_passed:
                               result_label = "No Failure"
                           else:
                               result_label = "Unreachable"
                               record_flow  = q_meas
                           break

                       record_flow = q_meas
                       last_q_v    = v_ao1
                       self._log(f"    FLOW LATCHED at {v_ao1:.3f} V "
                                 f"(read {q_meas:.2f} {flow_units}) -- held")

                       expected = self._model.get(self._closest_model_key(f), None)

                       if is_nc:
                           # ══════════════════════════════════════════════
                           # NC: cycle -> de-energized, check SPRING RETURN
                           #     then cycle -> energized, check SHIFT
                           # ══════════════════════════════════════════════
                           self._valve_deenergize(do_ch, act_cycles, act_freq)
                           self._log("    valve cycle \u2192 DE-ENERGIZED (closed)")
                           time.sleep(delay_read)
                           reading = self._read_gauge_from_rb(y_rb_idx)
                           if reading is None:
                               if self._abort:
                                   break
                               raise RuntimeError(
                                   "DAQ ring buffer not available -- start the DAQ before running.")
                           # Baseline is the ACHIEVED pressure, not the
                           # requested one. The ramp steps in finite increments
                           # so it lands up to one step above the request
                           # (100 units at the default 2% step). Measuring
                           # against the request would hand the valve that
                           # entire overshoot as free margin -- a valve leaking
                           # 70 units would still pass a 5 unit tolerance.
                           limit     = p_meas - close_tol
                           spring_ok = reading >= limit
                           self._log(f"    SPRING RETURN check (close): "
                                     f"read {reading:.2f} {y_units}   "
                                     f"must be >= {limit:.2f} "
                                     f"(achieved {p_meas:.2f} - tol {close_tol:g})")
                           self._log(f"    {'PASS' if spring_ok else 'FAIL  <-- SPRING FAILURE'}")
                           if not spring_ok:
                               result_label = "Spring Failure"
                               break

                           self._valve_energize(do_ch, act_cycles, act_freq)
                           self._log("    valve cycle \u2192 ENERGIZED (open)")
                           time.sleep(delay_read)
                           reading = self._read_gauge_from_rb(y_rb_idx)
                           if reading is None:
                               if self._abort:
                                   break
                               raise RuntimeError(
                                   "DAQ ring buffer not available -- start the DAQ before running.")
                           if expected is None:
                               self._log("    SHIFT check SKIPPED -- no model value "
                                         f"at {f:g} {flow_units}")
                               shift_ok = True
                           else:
                               limit    = expected + open_tol
                               shift_ok = reading <= limit
                               self._log(f"    SHIFT check (open): "
                                         f"read {reading:.2f} {y_units}   "
                                         f"must be <= {limit:.2f} "
                                         f"(model {expected:.2f} + tol {open_tol:g})")
                               self._log(f"    {'PASS' if shift_ok else 'FAIL  <-- SHIFT FAILURE'}")
                           if not shift_ok:
                               result_label = "Shift Failure"
                               break

                       else:
                           # ══════════════════════════════════════════════
                           # NO: cycle -> energized, check SHIFT
                           #     then cycle -> de-energized, check SPRING
                           # ══════════════════════════════════════════════
                           self._valve_energize(do_ch, act_cycles, act_freq)
                           self._log("    valve cycle \u2192 ENERGIZED (closed)")
                           time.sleep(delay_read)
                           reading = self._read_gauge_from_rb(y_rb_idx)
                           if reading is None:
                               if self._abort:
                                   break
                               raise RuntimeError(
                                   "DAQ ring buffer not available -- start the DAQ before running.")
                           # Achieved, not requested — see the NC branch.
                           limit    = p_meas - close_tol
                           shift_ok = reading >= limit
                           self._log(f"    SHIFT check (close): "
                                     f"read {reading:.2f} {y_units}   "
                                     f"must be >= {limit:.2f} "
                                     f"(achieved {p_meas:.2f} - tol {close_tol:g})")
                           self._log(f"    {'PASS' if shift_ok else 'FAIL  <-- SHIFT FAILURE'}")
                           if not shift_ok:
                               result_label = "Shift Failure"
                               break

                           self._valve_deenergize(do_ch, act_cycles, act_freq)
                           self._log("    valve cycle \u2192 DE-ENERGIZED (open)")
                           time.sleep(delay_read)
                           reading = self._read_gauge_from_rb(y_rb_idx)
                           if reading is None:
                               if self._abort:
                                   break
                               raise RuntimeError(
                                   "DAQ ring buffer not available -- start the DAQ before running.")
                           if expected is None:
                               self._log("    SPRING RETURN check SKIPPED -- no model "
                                         f"value at {f:g} {flow_units}")
                               spring_ok = True
                           else:
                               limit     = expected + open_tol
                               spring_ok = reading <= limit
                               self._log(f"    SPRING RETURN check (open): "
                                         f"read {reading:.2f} {y_units}   "
                                         f"must be <= {limit:.2f} "
                                         f"(model {expected:.2f} + tol {open_tol:g})")
                               self._log(f"    {'PASS' if spring_ok else 'FAIL  <-- SPRING FAILURE'}")
                           if not spring_ok:
                               result_label = "Spring Failure"
                               break

                       # Both checks passed at this flow step.
                       any_passed = True
                       if is_last:
                           result_label = "No Failure"
                           break
                       # otherwise: on to the next flow point, pressure still latched

                   if self._abort:
                       break

                   if result_label is None:
                       # Loop ended without a verdict (e.g. every step was the
                       # skipped 0 step). Nothing was actually tested.
                       result_label = "Unreachable"

                   self._log("")
                   self._log(f"  RESULT: {result_label}  at {record_flow:g} {flow_units}")
                   self._result_pressure.append(p)
                   self._result_flow.append(record_flow)
                   self._result_labels.append(result_label)
                   self._signals.point_recorded.emit(p, record_flow, result_label)

                   # Return to a de-commanded state before the next pressure
                   # point re-runs the setting sequence from scratch.
                   self._ao_write(ao1, 0.0)
                   self._ao_write(ao0, 0.0)

               # Zero outputs when all pressure points are done
               self._valve_close(do_ch)
               self._ao_write(ao0, 0.0)
               self._ao_write(ao1, 0.0)

           except Exception:
               print(f"Flow Failure -- run worker error:\n{traceback.format_exc()}")
               self._signals.status_changed.emit("Test ERROR -- see log  \u274c")
           finally:
               self._is_running = False
               self._abort      = False
               self._signals.test_finished.emit()
               self._signals.enable_run.emit(True)
               self._signals.controls_unlock.emit()
               self._signals.stop_daq_sig.emit()

       self._is_running = True
       self._abort      = False
       self._worker_thread = threading.Thread(target=_worker, daemon=True)
       self._worker_thread.start()

   # ------------------------------------------------------------------
   # Stop DAQ helper
   # ------------------------------------------------------------------

   def _extra_shutdown(self):
       """De-energize every digital output, after the base has zeroed the AO.

       DI lines are inputs — there is nothing to "stop" on them hardware-wise,
       but any open task is released by letting the context managers close.
       """
       do_df = self.default_settings_dig
       if do_df is not None:
           for _, row in do_df.iterrows():
               dirn = _safe_str(row.get('Direction'), '').strip().lower()
               if dirn and dirn != 'output':
                   continue   # skip inputs
               ch = _safe_str(row.get('Channel'), '')
               if not ch:
                   continue
               try:
                   with nidaqmx.Task() as t:
                       dev = self._ao_device(row)  # same DAQ resolution (row carries 'DAQ')
                       t.do_channels.add_do_chan(f"{dev}/{ch}")
                       t.write(False)
               except Exception:
                   print(f"Flow Failure shutdown: could not de-energize DO {ch}:\n"
                         + traceback.format_exc())


# ---------------------------------------------------------------------------
# Apps-bar registration (see DAQ_AppRegistry.py)
# ---------------------------------------------------------------------------

def build(ctx, parent):
   return FlowFailureDialog(
       main_window=ctx.main_window,
       default_settings_output=ctx.output_settings,
       default_settings_input=ctx.input_settings,
       default_settings_dig=ctx.dig_settings,
       selected_daq=ctx.selected_daq,
       sample_rate=ctx.sample_rate,
       parent=parent,
   )


APP_SPEC = {
   'id':    'flow_failure',
   'label': 'Flow Failure',
   'cls':   'FlowFailureDialog',
   'build': build,
   'color': '#922b21',
   'hover': '#c0392b',
   'show_by_default': False,
   'requires_daq_hw': True,
   'requires_daq_stopped': True,
   'blocks_calibration': True,
   'locks_run_stop': True,
   'claims': {'ao'},
}
