
"""
DAQ_ProportionalSweep.py
------------------------
Proportional Sweep — proportional valve characterization test
Part of GioDAQ.

Test stand pressure tuning, purge, triangular valve sweep and operator-judged
leakage check.
"""

import time
import threading
import traceback

import numpy as np
import pandas as pd
import nidaqmx
from nidaqmx.stream_writers import AnalogSingleChannelWriter

from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QLineEdit, QComboBox, QPushButton, QGroupBox,
    QMessageBox, QFrame, QSizePolicy, QRadioButton, QButtonGroup,
    QCheckBox
)
from PyQt5.QtCore import Qt, pyqtSignal

# Shared implementations, aliased to the names this module already used so no
# call site changes.
from DAQ_Settings import add_ao_channel, unit_quantity
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

class _PSSignals(AppSignals):
    """The common signals from AppSignals, plus this app's own."""
    tune_done_sig   = pyqtSignal(bool)
    clear_graph_sig = pyqtSignal(bool)   # fires on UI thread; arg = restart acq
    verdict_sig     = pyqtSignal(str)    # fires on UI thread to ask pass/fail
    plot_enable_sig = pyqtSignal(bool)   # fires on UI thread; freeze/thaw plot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _Aborted(Exception):
    """Raised inside the run worker to unwind cleanly on Force Stop."""


class _PersistentAO:
    """One long-lived on-demand AO task for fast software-timed output.

    _write_dc opens and closes a task per sample, which costs milliseconds and
    caps the achievable update rate in the low hundreds of Hz at best. The
    purge chirp runs to 20 Hz and needs a few hundred updates per second to
    keep its shape, so it holds a single task open and writes into it.

    On-demand (no sample clock) is used deliberately: a hardware-timed task
    would claim the device's AO timing engine, which on a cDAQ chassis is
    shared across every AO module and would collide with direct writes to the
    other AO channel.
    """

    def __init__(self, device, channel, min_v, max_v, type_label):
        self.min_v, self.max_v = min_v, max_v
        self._task = nidaqmx.Task()
        add_ao_channel(self._task, f"{device}/{channel}", type_label,
                       min_val=min_v, max_val=max_v)
        self._writer = AnalogSingleChannelWriter(self._task.out_stream)

    def write(self, value):
        v = float(np.clip(value, self.min_v, self.max_v))
        self._writer.write_one_sample(v)
        return v

    def close(self):
        try:
            self._task.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


# ---------------------------------------------------------------------------
# Dialog
# ---------------------------------------------------------------------------

class ProportionalSweepDialog(AOTestDialog):

    SIGNALS_CLASS   = _PSSignals
    APP_NAME        = "Proportional Sweep"
    _LABEL_COL_W    = 170       # fixed width of the left label column

    def __init__(self, main_window, default_settings_output, default_settings_input,
                 default_settings_dig, selected_daq, sample_rate, parent=None):
        super().__init__(main_window, default_settings_output, default_settings_input,
                         default_settings_dig, selected_daq, sample_rate,
                         parent=parent, title="Proportional Sweep")
        self.setMinimumWidth(620)

        # ── Test Stand Pressure tuning state (held in memory) ─────────────
        # Two curves, because the command→pressure relationship is not the same
        # going up as it is coming back down (valve/regulator hysteresis). The
        # triangular sweep 0 → max → 0 gives us both in one pass.
        self._ts_tuned    = False
        self._ts_up_v     = None   # np.ndarray — AO volts, ascending  (0 → max)
        self._ts_up_p     = None   # np.ndarray — test stand pressure at each
        self._ts_dn_v     = None   # np.ndarray — AO volts, descending (max → 0)
        self._ts_dn_p     = None   # np.ndarray — test stand pressure at each
        self._ts_sig      = None   # snapshot of the inputs the tune was valid for

        # ── Run-sequence state ────────────────────────────────────────────
        # Valve command → valve pressure, recorded during the run's triangular
        # sweep. {'up': (v, p, q), 'down': (v, p, q)}
        self._sweep_data   = {}
        self._verdict      = None
        self._verdict_done = threading.Event()
        self._clear_done   = threading.Event()
        self._saved_axes   = None   # main window's axes_plotting while frozen

        # status_changed and stop_daq_sig are wired by AOTestDialog.
        self._signals.enable_run.connect(self._run_btn_set_enabled)
        self._signals.test_finished.connect(self._on_test_finished)
        self._signals.controls_unlock.connect(lambda: self._set_controls_running(False))
        self._signals.tune_done_sig.connect(self._on_tune_done)
        self._signals.clear_graph_sig.connect(self._on_clear_graph)
        self._signals.verdict_sig.connect(self._on_verdict_prompt)
        self._signals.plot_enable_sig.connect(self._on_set_plotting)

        self._ao_count = len(default_settings_output) if default_settings_output is not None else 0

        self._build_ui()
        self._populate_channels()
        self._refresh_unit_labels()
        self._refresh_leak_resolved()
        self._refresh_tune_ui()
        self._lock_main_window()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(8)

        # ── Channels selection ────────────────────────────────────────────
        grp = _GroupBox("Channels selection")
        self._chan_grp = grp
        gl = QGridLayout(grp)
        gl.setColumnMinimumWidth(0, self._LABEL_COL_W)
        gl.setColumnStretch(1, 1)
        r = 0

        gl.addWidget(QLabel("Valve Type:"), r, 0)
        self._valve_no_nc = QComboBox()
        self._valve_no_nc.addItem("Normally Open")
        self._valve_no_nc.addItem("Normally Closed")
        # Starts blank. Valve polarity decides which way the Sweep AO is driven
        # to shut the valve, so it is left for the operator to state rather than
        # defaulted — a wrong guess here drives the valve the wrong way.
        self._valve_no_nc.setCurrentIndex(-1)
        self._valve_no_nc.setMinimumWidth(160)
        self._valve_no_nc.setToolTip(
            "Normally Open: 0 V command = flowing; rising command closes the valve.\n"
            "Normally Closed: 0 V command = shut; rising command opens the valve.\n\n"
            "Determines how the Sweep AO is parked during the Test Stand tune,\n"
            "so changing it invalidates the tune."
        )
        gl.addWidget(self._valve_no_nc, r, 1, 1, 2)
        self._valve_no_nc.currentIndexChanged.connect(self._on_valve_type_changed)
        r += 1

        # Test Stand Pressure Gauge (AI, pressure units only)
        gl.addWidget(QLabel("Test Stand Pressure Gauge:"), r, 0)
        self._ts_gauge_combo = QComboBox()
        self._ts_gauge_combo.setMinimumWidth(240)
        self._ts_gauge_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._ts_gauge_combo.setToolTip(
            "Supply / test stand pressure transducer.\n"
            "Only pressure channels (psi, bar, kPa, ...) are shown.\n"
            "This is the gauge read while tuning the Test Stand AO."
        )
        gl.addWidget(self._ts_gauge_combo, r, 1, 1, 2)
        self._ts_gauge_combo.currentIndexChanged.connect(self._on_ts_gauge_changed)
        r += 1

        # Valve Pressure Gauge (AI, pressure units only)
        gl.addWidget(QLabel("Valve Pressure Gauge:"), r, 0)
        self._valve_gauge_combo = QComboBox()
        self._valve_gauge_combo.setMinimumWidth(240)
        self._valve_gauge_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._valve_gauge_combo.setToolTip(
            "Pressure transducer at the valve under test.\n"
            "Only pressure channels (psi, bar, kPa, ...) are shown."
        )
        gl.addWidget(self._valve_gauge_combo, r, 1, 1, 2)
        self._valve_gauge_combo.currentIndexChanged.connect(self._on_valve_gauge_changed)
        r += 1

        # Flow Meter (AI, flow units only)
        gl.addWidget(QLabel("Flow Meter:"), r, 0)
        self._flow_combo = QComboBox()
        self._flow_combo.setMinimumWidth(240)
        self._flow_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._flow_combo.setToolTip(
            "Flow measurement channel.\n"
            "Only flow channels (gpm, lpm, L/min, ...) are shown."
        )
        gl.addWidget(self._flow_combo, r, 1, 1, 2)
        r += 1

        # Y-Axis — every configured AI channel, no unit filter.
        gl.addWidget(QLabel("Y-Axis:"), r, 0)
        self._y_combo = QComboBox()
        self._y_combo.setMinimumWidth(240)
        self._y_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._y_combo.setToolTip(
            "Analog input channel plotted on the Y axis.\n"
            "Also sets the Y-Axis in Multichannel_DAQ."
        )
        gl.addWidget(self._y_combo, r, 1, 1, 2)
        self._y_combo.currentIndexChanged.connect(self._on_y_changed)
        r += 1

        # Test Stand AO — drives supply pressure. Must differ from Sweep AO.
        gl.addWidget(QLabel("Test Stand AO:"), r, 0)
        self._ao_ts_combo = QComboBox()
        self._ao_ts_combo.setMinimumWidth(240)
        self._ao_ts_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._ao_ts_combo.setToolTip(
            "Analog output channel that commands test stand / supply pressure.\n"
            "Cannot be the same channel as the Sweep AO — selecting a channel\n"
            "already used as the Sweep AO clears the Sweep AO selection."
        )
        gl.addWidget(self._ao_ts_combo, r, 1, 1, 2)
        self._ao_ts_combo.currentIndexChanged.connect(self._on_ts_ao_changed)
        r += 1

        # Sweep AO — the proportional valve command.
        gl.addWidget(QLabel("Sweep AO:"), r, 0)
        self._ao_sweep_combo = QComboBox()
        self._ao_sweep_combo.setMinimumWidth(240)
        self._ao_sweep_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._ao_sweep_combo.setToolTip(
            "Analog output channel driving the proportional valve command.\n"
            "Cannot be the same channel as the Test Stand AO."
        )
        gl.addWidget(self._ao_sweep_combo, r, 1, 1, 2)
        self._ao_sweep_combo.currentIndexChanged.connect(self._on_sweep_ao_changed)

        root.addWidget(grp)

        # ── Test Stand Pressure tune ──────────────────────────────────────
        grp = _GroupBox("Test Stand Pressure")
        self._ts_grp = grp
        gl = QGridLayout(grp)
        gl.setColumnMinimumWidth(0, self._LABEL_COL_W)
        gl.setColumnStretch(6, 1)

        # Waveform parameters, named to match the Function Generator so the
        # numbers mean the same thing there and here. The tune drives the
        # Test Stand AO through a triangular ramp 0 → amplitude → 0.
        gl.addWidget(QLabel("Waveform:"), 0, 0)
        wf_row = QHBoxLayout()
        self._wave_label = QLabel("Triangular")
        self._wave_label.setStyleSheet("font-weight: bold;")
        wf_row.addWidget(self._wave_label)
        wf_row.addSpacing(16)
        wf_row.addWidget(QLabel("Amplitude (V)"))
        self._amplitude = QLineEdit("10"); self._amplitude.setMaximumWidth(60)
        self._amplitude.setToolTip(
            "Peak of the triangular ramp. The sweep runs 0 V → amplitude → 0 V.\n"
            "Clamped to the Test Stand AO channel's configured Min/Max."
        )
        wf_row.addWidget(self._amplitude)
        wf_row.addSpacing(12)
        wf_row.addWidget(QLabel("Frequency (Hz)"))
        self._frequency = QLineEdit("0.5"); self._frequency.setMaximumWidth(60)
        self._frequency.setToolTip(
            "Triangle frequency. One period = the full 0 → max → 0 excursion."
        )
        wf_row.addWidget(self._frequency)
        wf_row.addStretch(1)
        gl.addLayout(wf_row, 0, 1, 1, 6)

        # Sampling of the ramp. A pure 0.5 Hz triangle is a 2 s round trip,
        # which is far too fast for a pressure transducer to settle at each
        # command. Steps per ramp sets the resolution of the stored curve, and
        # Settle is the minimum dwell held at each step before reading. When
        # Settle is longer than the waveform timing implies, Settle wins and
        # the sweep simply takes longer than 1/frequency.
        gl.addWidget(QLabel("Sampling:"), 1, 0)
        gl.addWidget(QLabel("Steps per ramp"), 1, 1)
        self._tune_steps = QLineEdit("25"); self._tune_steps.setMaximumWidth(60)
        self._tune_steps.setToolTip(
            "Number of command points recorded on each leg of the triangle.\n"
            "25 gives a 0.4 V grid over a 10 V amplitude."
        )
        gl.addWidget(self._tune_steps, 1, 2)
        gl.addWidget(QLabel("Settle min / max (s)"), 1, 3)
        settle_row = QHBoxLayout()
        self._tune_settle = QLineEdit("0.25"); self._tune_settle.setMaximumWidth(55)
        self._tune_settle_max = QLineEdit("3.0"); self._tune_settle_max.setMaximumWidth(55)
        settle_row.addWidget(self._tune_settle)
        settle_row.addWidget(self._tune_settle_max)
        gl.addLayout(settle_row, 1, 4)
        self._tune_settle.setToolTip(
            "Minimum dwell at each command point before the gauge is read."
        )
        self._tune_settle_max.setToolTip(
            "Upper bound on the adaptive dwell. Once the gauge stops drifting the\n"
            "step is recorded immediately; this caps how long a slow-settling\n"
            "point is allowed to hold before it is recorded anyway."
        )
        gl.addWidget(QLabel("Stable slope (units/s)"), 2, 3)
        self._tune_slope_tol = QLineEdit("2.0"); self._tune_slope_tol.setMaximumWidth(55)
        self._tune_slope_tol.setToolTip(
            "A step counts as settled once the gauge's rate of change drops\n"
            "below this. Adaptive dwell removes the need to guess a single\n"
            "settle time that works across the whole pressure range."
        )
        gl.addWidget(self._tune_slope_tol, 2, 4)

        for w in (self._amplitude, self._frequency, self._tune_steps,
                  self._tune_settle, self._tune_settle_max, self._tune_slope_tol):
            w.textChanged.connect(self._on_tune_param_changed)

        # Tune button + read-only result summary.
        self._tune_btn = QPushButton("Tune Test Stand Pressure")
        self._tune_btn.setFixedHeight(26)
        self._tune_btn.clicked.connect(self._on_tune_test_stand)
        gl.addWidget(self._tune_btn, 2, 1, 1, 2)

        self._tune_summary = QLineEdit("")
        self._tune_summary.setReadOnly(True)
        self._tune_summary.setPlaceholderText("Not tuned")
        self._tune_summary.setStyleSheet("background: #e9e9e9; color: #555;")
        self._tune_summary.setToolTip(
            "Range covered by the stored command → pressure curves (read-only)."
        )
        gl.addWidget(self._tune_summary, 3, 1, 1, 6)

        root.addWidget(grp)

        # ── Leakage Test ──────────────────────────────────────────────────
        grp = _GroupBox("Leakage Test")
        self._leak_grp = grp
        gl = QGridLayout(grp)
        gl.setColumnMinimumWidth(0, self._LABEL_COL_W)
        gl.setColumnStretch(5, 1)

        self._leak_valve_lbl = QLabel("Valve Setting (units):")
        gl.addWidget(self._leak_valve_lbl, 0, 0)
        self._leak_valve_setting = QLineEdit("5000")
        self._leak_valve_setting.setMaximumWidth(80)
        self._leak_valve_setting.setToolTip(
            "Valve setting at which leakage is checked, in valve-gauge units.\n"
            "The Sweep AO command for this pressure is read off the valve sweep\n"
            "recorded earlier in the run, so the sweep has to happen first."
        )
        gl.addWidget(self._leak_valve_setting, 0, 1)

        # Test stand pressure for the leak check: an absolute value, or a
        # percentage of the valve setting above.
        gl.addWidget(QLabel("Test Stand Pressure:"), 1, 0)
        self._leak_fixed_radio   = QRadioButton("Fixed")
        self._leak_percent_radio = QRadioButton("Percent")
        self._leak_fixed_radio.setChecked(True)
        self._leak_mode_group = QButtonGroup(self)
        self._leak_mode_group.addButton(self._leak_fixed_radio)
        self._leak_mode_group.addButton(self._leak_percent_radio)

        gl.addWidget(self._leak_fixed_radio, 1, 1)
        self._leak_fixed = QLineEdit("4000"); self._leak_fixed.setMaximumWidth(80)
        self._leak_fixed.setToolTip("Absolute test stand pressure, in gauge units.")
        gl.addWidget(self._leak_fixed, 1, 2)

        gl.addWidget(self._leak_percent_radio, 1, 3)
        self._leak_percent = QLineEdit("85"); self._leak_percent.setMaximumWidth(60)
        self._leak_percent.setToolTip(
            "Percent of the Valve Setting above.\n"
            "85% of a 5000 setting resolves to 4250."
        )
        gl.addWidget(self._leak_percent, 1, 4)
        gl.addWidget(QLabel("%"), 1, 5, Qt.AlignLeft)

        self._leak_resolved = QLabel("")
        self._leak_resolved.setStyleSheet("color: #555; font-style: italic;")
        gl.addWidget(self._leak_resolved, 2, 1, 1, 5)

        for w in (self._leak_valve_setting, self._leak_fixed, self._leak_percent):
            w.textChanged.connect(self._refresh_leak_resolved)
        self._leak_fixed_radio.toggled.connect(self._refresh_leak_resolved)

        root.addWidget(grp)

        # ── Test Sequence ─────────────────────────────────────────────────
        grp = _GroupBox("Test Sequence")
        self._seq_grp = grp
        gl = QGridLayout(grp)
        gl.setColumnMinimumWidth(0, self._LABEL_COL_W)
        gl.setColumnStretch(8, 1)

        self._seq_setpoint_lbl = QLabel("Test Stand Setpoint (units):")
        gl.addWidget(self._seq_setpoint_lbl, 0, 0)
        self._seq_setpoint = QLineEdit("5000"); self._seq_setpoint.setMaximumWidth(80)
        self._seq_setpoint.setToolTip(
            "Pressure the test stand is brought to at the start of the run.\n"
            "Converted to a command through the tuned curve, then trimmed\n"
            "closed-loop against the test stand gauge."
        )
        gl.addWidget(self._seq_setpoint, 0, 1)
        gl.addWidget(QLabel("Tolerance"), 0, 2)
        self._seq_tol = QLineEdit("10"); self._seq_tol.setMaximumWidth(60)
        self._seq_tol.setToolTip(
            "Closed-loop trim stops once the gauge is within this much of the\n"
            "target, in gauge units."
        )
        gl.addWidget(self._seq_tol, 0, 3)

        gl.addWidget(QLabel("Purge:"), 1, 0)
        gl.addWidget(QLabel("Duration (s)"), 1, 1)
        self._purge_dur = QLineEdit("5"); self._purge_dur.setMaximumWidth(55)
        gl.addWidget(self._purge_dur, 1, 2)
        gl.addWidget(QLabel("From (Hz)"), 1, 3)
        self._purge_f1 = QLineEdit("0.5"); self._purge_f1.setMaximumWidth(55)
        gl.addWidget(self._purge_f1, 1, 4)
        gl.addWidget(QLabel("To (Hz)"), 1, 5)
        self._purge_f2 = QLineEdit("20"); self._purge_f2.setMaximumWidth(55)
        gl.addWidget(self._purge_f2, 1, 6)
        gl.addWidget(QLabel("Amp (V)"), 1, 7)
        self._purge_amp = QLineEdit("10"); self._purge_amp.setMaximumWidth(55)
        gl.addWidget(self._purge_amp, 1, 8, Qt.AlignLeft)
        self._purge_dur.setToolTip(
            "Total purge time. The chirp runs From → To over the first half and\n"
            "back To → From over the second."
        )

        gl.addWidget(QLabel("Valve Sweep:"), 2, 0)
        gl.addWidget(QLabel("Amp (V)"), 2, 1)
        self._sweep_amp = QLineEdit("10"); self._sweep_amp.setMaximumWidth(55)
        gl.addWidget(self._sweep_amp, 2, 2)
        gl.addWidget(QLabel("Steps"), 2, 3)
        self._sweep_steps = QLineEdit("40"); self._sweep_steps.setMaximumWidth(55)
        gl.addWidget(self._sweep_steps, 2, 4)
        gl.addWidget(QLabel("Settle (s)"), 2, 5)
        self._sweep_settle = QLineEdit("0.15"); self._sweep_settle.setMaximumWidth(55)
        gl.addWidget(self._sweep_settle, 2, 6)
        self._sweep_steps.setToolTip(
            "Points per leg of the 0 → Amp → 0 triangle on the Sweep AO.\n"
            "These points also become the valve command → valve pressure map\n"
            "used to place the valve at its leakage setting."
        )

        self._clear_chk = QCheckBox("Clear graph before sweep (restarts acquisition)")
        self._clear_chk.setChecked(True)
        self._clear_chk.setToolTip(
            "The plot always redraws the tail of the ring buffer, so clearing the\n"
            "curves alone refills instantly with purge history. Restarting\n"
            "acquisition zeroes the buffer, which is what actually leaves the\n"
            "sweep alone on the graph.\n\n"
            "Untick to clear the curves only and keep acquisition running."
        )
        gl.addWidget(self._clear_chk, 3, 0, 1, 9)

        root.addWidget(grp)

        root.addWidget(_hline())

        self._status_label = QLabel("Status: NOT TUNED  ❌")
        self._status_label.setAlignment(Qt.AlignCenter)
        self._status_label.setStyleSheet("font-weight: bold; padding: 4px;")
        root.addWidget(self._status_label)

        btn_row = QHBoxLayout()
        self._run_btn        = QPushButton("Run Test")
        self._force_stop_btn = QPushButton("Force Stop")
        self._close_btn      = QPushButton("Close")
        self._run_btn.setEnabled(False)
        self._run_btn.setToolTip("Tune Test Stand Pressure first to enable this.")
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
        ao_df = self.default_settings_output

        # ── AO selectors — one entry per configured output row ────────────
        for combo in (self._ao_ts_combo, self._ao_sweep_combo):
            combo.blockSignals(True)
            combo.clear()
            if ao_df is not None and len(ao_df) > 0:
                for idx in range(len(ao_df)):
                    ch = _safe_str(ao_df.iloc[idx].get('Channel'), f'ao{idx}')
                    nm = _safe_str(ao_df.iloc[idx].get('Device Name'), ch)
                    label = f"{ch}  —  {nm}" if nm and nm != ch else ch
                    combo.addItem(label, userData=idx)
            if combo.count() == 0:
                combo.addItem("(none configured)", userData=None)

        # Both start blank — nothing is preselected, so the operator has to
        # name each channel explicitly before a run.
        self._ao_ts_combo.setCurrentIndex(-1)
        self._ao_sweep_combo.setCurrentIndex(-1)
        for combo in (self._ao_ts_combo, self._ao_sweep_combo):
            combo.blockSignals(False)

        # ── AI selectors ─────────────────────────────────────────────────
        # userData stores (ring_buffer_index, device_name)
        for combo in (self._ts_gauge_combo, self._valve_gauge_combo,
                      self._flow_combo, self._y_combo):
            combo.blockSignals(True)
            combo.clear()

        ai_df = self.default_settings_input
        if ai_df is not None and len(ai_df) > 0:
            for idx, row in ai_df.iterrows():
                ch    = _safe_str(row.get('Channel'), f'ai{idx}')
                nm    = _safe_str(row.get('Device Name'), ch)
                units = _safe_str(row.get('Units'), '').strip()
                label = f"{ch}  —  {nm}" if nm and nm != ch else ch
                user_data = (int(idx), nm)
                # Picked by what the unit measures (DAQ_Settings.unit_quantity),
                # not by a list of spellings, so kPa or L/min gauges are
                # offered too. Everything here works in each gauge's own unit.
                quantity = unit_quantity(units)

                # Pressure gauges — both the test stand and the valve dropdown.
                if quantity == "Pressure":
                    self._ts_gauge_combo.addItem(label, userData=user_data)
                    self._valve_gauge_combo.addItem(label, userData=user_data)

                # Flow meters.
                if quantity == "Volumetric flow":
                    self._flow_combo.addItem(label, userData=user_data)

                # Y-Axis — every AI channel, units appended for clarity.
                y_label = f"{label}  ({units})" if units else label
                self._y_combo.addItem(y_label, userData=user_data)

        if self._ts_gauge_combo.count() == 0:
            self._ts_gauge_combo.addItem("(no pressure channels)", userData=None)
        if self._valve_gauge_combo.count() == 0:
            self._valve_gauge_combo.addItem("(no pressure channels)", userData=None)
        if self._flow_combo.count() == 0:
            self._flow_combo.addItem("(no flow channels)", userData=None)
        if self._y_combo.count() == 0:
            self._y_combo.addItem("(no AI channels configured)", userData=None)

        # All start blank rather than preselected.
        for combo in (self._ts_gauge_combo, self._valve_gauge_combo,
                      self._flow_combo, self._y_combo):
            combo.setCurrentIndex(-1)

        for combo in (self._ts_gauge_combo, self._valve_gauge_combo,
                      self._flow_combo, self._y_combo):
            combo.blockSignals(False)

        self._update_ao_corner_text()
        self._on_y_changed()

    # ------------------------------------------------------------------
    # Selection callbacks
    # ------------------------------------------------------------------

    def _on_valve_type_changed(self):
        """Valve polarity decides how the Sweep AO is parked during the tune,
        so a change invalidates the stored curves."""
        self._invalidate_tune()

    def _on_valve_gauge_changed(self):
        """Valve gauge drives the Valve Setting units and the resolved line."""
        self._refresh_unit_labels()
        self._refresh_leak_resolved()

    def _on_ts_gauge_changed(self):
        """The tune is a curve against one specific gauge — changing it voids."""
        self._refresh_unit_labels()
        self._refresh_leak_resolved()
        self._invalidate_tune()

    def _refresh_unit_labels(self):
        """Label each pressure entry with the units of the gauge it refers to.

        Valve Setting is read against the Valve Pressure Gauge; the Test Stand
        Setpoint against the Test Stand Pressure Gauge.
        """
        vu = self._valve_gauge_units() or "units"
        tu = self._ts_gauge_units() or "units"
        self._leak_valve_lbl.setText(f"Valve Setting ({vu}):")
        self._seq_setpoint_lbl.setText(f"Test Stand Setpoint ({tu}):")

    def _on_tune_param_changed(self, *_):
        """Amplitude / frequency / step count changes void the stored curves."""
        self._invalidate_tune()

    def _on_ts_ao_changed(self, *_):
        """Test Stand AO must differ from Sweep AO — clear Sweep AO on collision."""
        ts = self._ao_ts_combo.currentData()
        sw = self._ao_sweep_combo.currentData()
        if ts is not None and sw is not None and ts == sw:
            self._ao_sweep_combo.blockSignals(True)
            self._ao_sweep_combo.setCurrentIndex(-1)
            self._ao_sweep_combo.blockSignals(False)
        self._sync_ao_exclusion()
        self._update_ao_corner_text()
        self._invalidate_tune()

    def _on_sweep_ao_changed(self, *_):
        """Mirror of _on_ts_ao_changed — clear Test Stand AO on collision."""
        ts = self._ao_ts_combo.currentData()
        sw = self._ao_sweep_combo.currentData()
        if ts is not None and sw is not None and ts == sw:
            self._ao_ts_combo.blockSignals(True)
            self._ao_ts_combo.setCurrentIndex(-1)
            self._ao_ts_combo.blockSignals(False)
        self._sync_ao_exclusion()
        self._update_ao_corner_text()
        self._invalidate_tune()

    def _sync_ao_exclusion(self):
        """Gray out, in each AO dropdown, the row the other one has claimed.

        Belt-and-braces on top of the clear-on-collision above: the disabled
        item can't be picked in the first place, so the user sees why rather
        than watching their other selection silently vanish.
        """
        for combo, other in ((self._ao_ts_combo,    self._ao_sweep_combo),
                             (self._ao_sweep_combo, self._ao_ts_combo)):
            taken = other.currentData()
            model = combo.model()
            for i in range(combo.count()):
                item = model.item(i) if hasattr(model, 'item') else None
                if item is None:
                    continue
                data = combo.itemData(i)
                blocked = (taken is not None and data is not None and data == taken)
                item.setEnabled(not blocked)

    def _on_y_changed(self):
        """Set and lock the Y-axis checkbox in Multichannel_DAQ."""
        data = self._y_combo.currentData()
        if data is None:
            return
        _rb_idx, dev_name = data

        mw = self.main_window
        if mw is None or not hasattr(mw, 'plotSettingsWidget'):
            return
        ps = mw.plotSettingsWidget
        try:
            import Multichannel_DAQ as _mdaq
            for i, row in _mdaq.plot_options.iterrows():
                cb = getattr(ps, f'plot_check_box{i}', None)
                if cb is None:
                    continue
                if row.get('Device Name') == dev_name:
                    cb.setChecked(True)
                    cb.setEnabled(False)
                    cb.setToolTip("Controlled by Proportional Sweep — close it to change")
                else:
                    cb.setEnabled(True)
                    cb.setToolTip("")
        except Exception:
            pass

    def _update_ao_corner_text(self):
        grp = getattr(self, '_chan_grp', None)
        if grp is None:
            return
        parts = []
        for lbl, row in (("Stand", self._get_ao_row('test_stand')),
                         ("Sweep", self._get_ao_row('sweep'))):
            if row is None:
                parts.append(f"{lbl}: —")
            else:
                parts.append(f"{lbl}: {_safe_str(row.get('Channel'), '?')}")
        grp.set_corner_text("   ".join(parts))

    # ------------------------------------------------------------------
    # Channel / device resolution
    # ------------------------------------------------------------------

    def _get_ao_row(self, role):
        """Return the output settings row for an AO role.

        role: 'test_stand' → Test Stand AO, 'sweep' → Sweep AO.
        Returns None when nothing is selected (the blanked state after a
        collision) or when no outputs are configured.
        """
        df = self.default_settings_output
        if df is None or len(df) == 0:
            return None
        combo = {'test_stand': self._ao_ts_combo,
                 'sweep':      self._ao_sweep_combo}.get(role)
        if combo is None:
            return None
        try:
            idx = int(combo.currentData())
        except (TypeError, ValueError):
            return None
        return df.iloc[idx] if 0 <= idx < len(df) else None

    def _get_ts_gauge_data(self):
        """Return (rb_idx, device_name) for the test stand gauge, or None."""
        return self._ts_gauge_combo.currentData()

    def _get_valve_gauge_data(self):
        return self._valve_gauge_combo.currentData()

    def _get_flow_data(self):
        return self._flow_combo.currentData()

    def _get_y_data(self):
        return self._y_combo.currentData()

    def _valve_type(self):
        """'NC', 'NO', or None when the operator has not chosen yet."""
        txt = self._valve_no_nc.currentText()
        if txt == "Normally Closed":
            return 'NC'
        if txt == "Normally Open":
            return 'NO'
        return None

    def _is_normally_closed(self):
        return self._valve_type() == 'NC'

    def _ao_write(self, row, voltage):
        """Write a DC level to an AO row and keep the main display in sync."""
        if row is None:
            return
        ch = _safe_str(row.get('Channel'), '')
        if not ch:
            return
        min_v, max_v = self._ao_limits(row)
        _write_dc(self._ao_device(row), ch, voltage, min_v, max_v,
                  self._ao_type(row))
        self._notify_output(ch, float(np.clip(voltage, min_v, max_v)))

    # ------------------------------------------------------------------
    # Sweep AO parking during the tune
    # ------------------------------------------------------------------

    def _sweep_park_voltage(self):
        """Command to hold the Sweep AO at while the test stand is characterised.

        The valve under test must be out of the way so the test stand gauge
        sees clean supply pressure:
          Normally Closed → 0 V (de-energized, valve shut).
          Normally Open   → full-scale command (drives the valve shut), since
                            0 V would leave a NO valve flowing.
        """
        row = self._get_ao_row('sweep')
        if row is None:
            return 0.0
        vt = self._valve_type()
        if vt is None:
            # Callers validate first; refuse to guess rather than drive a
            # normally-open valve wide open on a blank selection.
            raise RuntimeError("Valve Type has not been selected.")
        min_v, max_v = self._ao_limits(row)
        return float(np.clip(0.0, min_v, max_v)) if vt == 'NC' else max_v

    # ------------------------------------------------------------------
    # Tune parameters
    # ------------------------------------------------------------------

    def _missing_selections(self, need_flow=False):
        """Names of required dropdowns still blank, in display order."""
        missing = []
        if self._valve_type() is None:
            missing.append("Valve Type")
        if self._get_ts_gauge_data() is None:
            missing.append("Test Stand Pressure Gauge")
        if self._get_valve_gauge_data() is None:
            missing.append("Valve Pressure Gauge")
        if need_flow and self._get_flow_data() is None:
            missing.append("Flow Meter")
        if self._get_ao_row('test_stand') is None:
            missing.append("Test Stand AO")
        if self._get_ao_row('sweep') is None:
            missing.append("Sweep AO")
        return missing

    def _tune_params(self):
        """Parse and validate the waveform fields.

        Returns (amplitude, freq_hz, steps, dwell_s, settle_max, slope_tol) or
        None after showing a message box.
        """
        try:
            amp = float(self._amplitude.text())
        except ValueError:
            QMessageBox.warning(self, "Tune Test Stand Pressure",
                                "Amplitude must be a number.")
            return None
        try:
            freq = float(self._frequency.text())
        except ValueError:
            QMessageBox.warning(self, "Tune Test Stand Pressure",
                                "Frequency must be a number.")
            return None
        try:
            steps = int(float(self._tune_steps.text()))
        except ValueError:
            QMessageBox.warning(self, "Tune Test Stand Pressure",
                                "Steps per ramp must be a whole number.")
            return None
        try:
            settle = float(self._tune_settle.text())
            settle_max = float(self._tune_settle_max.text())
            slope_tol = float(self._tune_slope_tol.text())
        except ValueError:
            QMessageBox.warning(self, "Tune Test Stand Pressure",
                                "Settle and slope fields must be numbers.")
            return None

        if amp <= 0:
            QMessageBox.warning(self, "Tune Test Stand Pressure",
                                "Amplitude must be greater than zero.")
            return None
        if freq <= 0:
            QMessageBox.warning(self, "Tune Test Stand Pressure",
                                "Frequency must be greater than zero.")
            return None
        if steps < 2:
            QMessageBox.warning(self, "Tune Test Stand Pressure",
                                "Steps per ramp must be at least 2.")
            return None
        if settle < 0 or settle_max < settle:
            QMessageBox.warning(
                self, "Tune Test Stand Pressure",
                "Settle must be >= 0 and Settle max must be >= Settle min."
            )
            return None

        # Waveform timing: one triangle period covers both legs, so each leg
        # gets half a period spread across `steps` points. The settle floor
        # wins when the transducer needs longer than the waveform implies —
        # the sweep slows down rather than recording lagged readings.
        period     = 1.0 / freq
        wave_dwell = period / (2.0 * steps)
        dwell      = max(wave_dwell, settle)

        return amp, freq, steps, dwell, settle_max, slope_tol

    def _current_tune_sig(self):
        """Snapshot of everything the stored curves depend on.

        Used to decide whether an existing tune is still valid, so we only
        invalidate on a real change rather than on any stray signal.
        """
        ts_row = self._get_ao_row('test_stand')
        ts_ch  = _safe_str(ts_row.get('Channel'), '') if ts_row is not None else ''
        gauge  = self._ts_gauge_combo.currentData()
        return (
            ts_ch,
            gauge[0] if gauge is not None else None,
            self._valve_no_nc.currentText(),
            self._amplitude.text().strip(),
            self._frequency.text().strip(),
            self._tune_steps.text().strip(),
        )

    def _invalidate_tune(self):
        """Drop the stored curves if the inputs they were built from changed."""
        if not self._ts_tuned:
            self._refresh_tune_ui()
            return
        if self._ts_sig == self._current_tune_sig():
            return          # nothing meaningful changed — keep the tune
        self._ts_tuned = False
        self._ts_up_v = self._ts_up_p = None
        self._ts_dn_v = self._ts_dn_p = None
        self._ts_sig  = None
        self._refresh_tune_ui()
        self._signals.status_changed.emit("Status: NOT TUNED  ❌")

    def _refresh_tune_ui(self):
        """Button label, Run gating and summary follow the tune state."""
        self._tune_btn.setText(
            "Re-tune Test Stand Pressure" if self._ts_tuned
            else "Tune Test Stand Pressure"
        )
        if self._is_running:
            return
        self._run_btn.setEnabled(self._ts_tuned)
        self._run_btn.setToolTip(
            "" if self._ts_tuned else "Tune Test Stand Pressure first to enable this."
        )
        if not self._ts_tuned:
            self._tune_summary.setText("")

    # ------------------------------------------------------------------
    # Tune Test Stand Pressure
    # ------------------------------------------------------------------

    def _on_tune_test_stand(self):
        """Characterise Test Stand AO command → test stand pressure.

        Drives the Test Stand AO through a triangular staircase, 0 V up to the
        amplitude and back down to 0 V, reading the test stand gauge at every
        step. The two legs are stored separately because the command → pressure
        relationship differs by direction (regulator/valve hysteresis), so a
        later lookup has to say which way it is coming from.

        A staircase is used rather than a continuously generated triangle for
        the same reason Flow Failure uses one: each point needs a settled,
        averaged gauge reading tied to a known command, which a free-running
        waveform can't give us.
        """
        if self._is_running:
            return

        # Valve Type and the Sweep AO matter even though the tune drives the
        # Test Stand AO: the valve under test has to be commanded shut so the
        # test stand gauge reads clean supply pressure.
        missing = [m for m in self._missing_selections()
                   if m != "Valve Pressure Gauge"]
        if missing:
            QMessageBox.warning(
                self, "Tune Test Stand Pressure",
                "Select the following first:\n\n  • " + "\n  • ".join(missing)
            )
            return

        gauge = self._get_ts_gauge_data()
        ts_rb_idx, _ = gauge
        ts_row    = self._get_ao_row('test_stand')
        sweep_row = self._get_ao_row('sweep')

        params = self._tune_params()
        if params is None:
            return
        amp, freq, steps, dwell, settle_max, slope_tol = params

        min_v, max_v = self._ao_limits(ts_row)
        peak = float(np.clip(amp, min_v, max_v))
        if peak < amp:
            QMessageBox.information(
                self, "Tune Test Stand Pressure",
                f"Amplitude {amp:g} V exceeds the Test Stand AO range "
                f"({min_v:g} to {max_v:g} V).\nThe sweep will peak at {peak:g} V."
            )
        base = float(np.clip(0.0, min_v, max_v))

        park = self._sweep_park_voltage()

        self._set_controls_running(True)
        self._signals.status_changed.emit("Tuning test stand pressure…")

        # The gauge is read out of the main ring buffer, so acquisition has to
        # be live. Same auto-start as Flow Failure's tune.
        mw = self.main_window
        if mw and not getattr(mw, 'continue_running', False):
            try:
                mw.start_task()
            except Exception:
                print("Proportional Sweep — could not auto-start DAQ:\n"
                      + traceback.format_exc())

        def _worker():
            ok = False
            try:
                # Park the valve under test out of the way, then start from base.
                self._ao_write(sweep_row, park)
                self._ao_write(ts_row, base)
                time.sleep(max(dwell, 0.2))

                up_v = np.linspace(base, peak, steps)
                dn_v = np.linspace(peak, base, steps)
                up_p = np.full(steps, np.nan, dtype=np.float64)
                dn_p = np.full(steps, np.nan, dtype=np.float64)

                for leg_name, volts, press in (("up", up_v, up_p),
                                               ("down", dn_v, dn_p)):
                    arrow = "↑" if leg_name == "up" else "↓"
                    for i, v in enumerate(volts):
                        if self._abort:
                            break
                        self._ao_write(ts_row, float(v))
                        reading = self._adaptive_settle(
                            ts_rb_idx, dwell, settle_max, slope_tol)
                        if reading is None:
                            if self._abort:
                                break
                            raise RuntimeError(
                                "Could not read the test stand gauge — ensure the "
                                "main DAQ is running before tuning."
                            )
                        press[i] = reading
                        self._signals.status_changed.emit(
                            f"Tuning {arrow}  {i+1}/{steps}   "
                            f"({v:.3f} V → {reading:.1f})"
                        )
                    if self._abort:
                        break

                # Return both channels to a safe state.
                self._ao_write(ts_row, base)
                self._ao_write(sweep_row, float(np.clip(0.0, *self._ao_limits(sweep_row))))

                if self._abort:
                    self._signals.status_changed.emit("Tune aborted  ⚠")
                else:
                    gu = ~np.isnan(up_p)
                    gd = ~np.isnan(dn_p)
                    if gu.sum() >= 2 and gd.sum() >= 2:
                        self._ts_up_v = up_v[gu]
                        self._ts_up_p = up_p[gu]
                        self._ts_dn_v = dn_v[gd]
                        self._ts_dn_p = dn_p[gd]
                        ok = True

            except Exception:
                print("Proportional Sweep — tune worker error:\n"
                      + traceback.format_exc())
                self._signals.status_changed.emit("Tune ERROR — see log  ❌")
            finally:
                self._is_running = False
                self._abort      = False
                self._signals.tune_done_sig.emit(ok)
                self._signals.controls_unlock.emit()
                self._signals.stop_daq_sig.emit()

        self._is_running = True
        self._abort      = False
        self._worker_thread = threading.Thread(target=_worker, daemon=True)
        self._worker_thread.start()

    def _on_tune_done(self, ok: bool):
        """Commit or discard the tune result on the UI thread."""
        if not ok:
            self._ts_tuned = False
            self._ts_sig   = None
            self._refresh_tune_ui()
            return

        self._ts_tuned = True
        self._ts_sig   = self._current_tune_sig()

        p_all = np.concatenate([self._ts_up_p, self._ts_dn_p])
        v_all = np.concatenate([self._ts_up_v, self._ts_dn_v])
        self._tune_summary.setText(
            f"{len(self._ts_up_v)} up / {len(self._ts_dn_v)} down   |   "
            f"{v_all.min():.2f}–{v_all.max():.2f} V   →   "
            f"{p_all.min():.1f}–{p_all.max():.1f} {self._ts_gauge_units()}"
        )
        self._refresh_tune_ui()
        self._signals.status_changed.emit("Status: TUNED  ✅")

    def _gauge_units(self, combo):
        """Units string of the channel selected in `combo`, for labels."""
        data = combo.currentData() if combo is not None else None
        ai_df = self.default_settings_input
        if data is None or ai_df is None:
            return ""
        try:
            return _safe_str(ai_df.loc[data[0]].get('Units'), '')
        except Exception:
            return ""

    def _ts_gauge_units(self):
        """Units of the Test Stand Pressure Gauge."""
        return self._gauge_units(self._ts_gauge_combo)

    def _valve_gauge_units(self):
        """Units of the Valve Pressure Gauge.

        Kept separate from the test stand units: the two dropdowns can point at
        transducers scaled differently, so the Valve Setting and the Test Stand
        Setpoint are not necessarily in the same unit.
        """
        return self._gauge_units(self._valve_gauge_combo)

    # ------------------------------------------------------------------
    # Stored curve lookups
    # ------------------------------------------------------------------

    def _ts_curve(self, direction):
        """(volts, pressures) for a direction, ordered for np.interp.

        direction: 'up' for rising command (low → high pressure), 'down' for
        falling. Returns None when untuned.
        """
        if not self._ts_tuned:
            return None
        if direction == 'up':
            return self._ts_up_v, self._ts_up_p
        if direction == 'down':
            # Stored high → low; np.interp needs an ascending x, so flip.
            return self._ts_dn_v[::-1], self._ts_dn_p[::-1]
        return None

    def ts_voltage_for_pressure(self, pressure, direction='up'):
        """Test Stand AO command that produces `pressure` on the given leg.

        Returns None when untuned or the curve isn't monotonic enough to invert.
        """
        curve = self._ts_curve(direction)
        if curve is None:
            return None
        v, p = curve
        order = np.argsort(p)
        p_s, v_s = p[order], v[order]
        if p_s[-1] - p_s[0] <= 0:
            return None
        return float(np.interp(float(pressure), p_s, v_s))

    def ts_pressure_for_voltage(self, voltage, direction='up'):
        """Test stand pressure expected at a command, on the given leg."""
        curve = self._ts_curve(direction)
        if curve is None:
            return None
        v, p = curve
        return float(np.interp(float(voltage), v, p))

    # ------------------------------------------------------------------
    # Settling
    # ------------------------------------------------------------------

    def _adaptive_settle(self, rb_idx, min_s, max_s, slope_tol, n_avg=20):
        """Hold, then return the gauge reading once it stops drifting.

        Waits min_s, then samples the gauge repeatedly and returns as soon as
        the rate of change falls under slope_tol (gauge units per second),
        giving up at max_s and returning the last reading regardless.

        This replaces a single guessed dwell. The rig's time constant is not
        constant across the pressure range, so any fixed settle is either too
        slow at one end or recording lagged values at the other.
        """
        time.sleep(max(0.0, min_s))
        prev = self._read_gauge_from_rb(rb_idx, n_avg)
        if prev is None:
            return None

        waited   = max(0.0, min_s)
        interval = 0.1
        while waited < max_s:
            if self._abort:
                return prev
            time.sleep(interval)
            waited += interval
            cur = self._read_gauge_from_rb(rb_idx, n_avg)
            if cur is None:
                return prev
            if abs(cur - prev) / interval <= slope_tol:
                return cur
            prev = cur
        return prev

    # ------------------------------------------------------------------
    # Closed-loop test stand pressure
    # ------------------------------------------------------------------

    def _local_dv_dp(self, direction, pressure, span=None):
        """Local slope of the tuned curve, in volts per gauge unit.

        Used to convert a pressure error into a command correction. Taken
        locally rather than as one global gain because the curve is not linear.
        """
        curve = self._ts_curve(direction)
        if curve is None:
            return None
        v, p = curve
        order = np.argsort(p)
        p_s, v_s = p[order], v[order]
        if p_s[-1] - p_s[0] <= 0:
            return None
        if span is None:
            span = max(1e-9, (p_s[-1] - p_s[0]) * 0.05)
        lo = float(np.interp(pressure - span, p_s, v_s))
        hi = float(np.interp(pressure + span, p_s, v_s))
        dp = 2.0 * span
        return (hi - lo) / dp if dp else None

    def _settle_test_stand_pressure(self, target, direction, tol,
                                    dwell, settle_max, slope_tol,
                                    max_iter=6):
        """Bring the test stand to `target` and return the achieved pressure.

        The tuned curve supplies the opening command; the gauge then closes the
        loop. Feedforward alone is not trustworthy here: hysteresis loops are
        nested, so a small reversal (5000 down to 4250) does not retrace the
        major down-curve that was measured from full scale — the true command
        sits between the two legs. Trimming against the gauge sidesteps that,
        along with supply drift and a stale tune.
        """
        ts_row = self._get_ao_row('test_stand')
        gauge  = self._get_ts_gauge_data()
        if ts_row is None or gauge is None:
            return None
        rb_idx, _ = gauge
        min_v, max_v = self._ao_limits(ts_row)

        v = self.ts_voltage_for_pressure(target, direction)
        if v is None:
            return None

        achieved = None
        for _ in range(max_iter):
            if self._abort:
                return achieved
            v = float(np.clip(v, min_v, max_v))
            self._ao_write(ts_row, v)
            achieved = self._adaptive_settle(rb_idx, dwell, settle_max, slope_tol)
            if achieved is None:
                return None
            err = float(target) - achieved
            self._signals.status_changed.emit(
                f"Test stand {achieved:.1f} / {float(target):.1f}  "
                f"(err {err:+.1f})   {v:.3f} V"
            )
            if abs(err) <= tol:
                return achieved
            slope = self._local_dv_dp(direction, achieved)
            if slope is None or slope == 0:
                return achieved
            step = err * slope
            # Cap a single correction so a bad local slope cannot bolt the
            # command across the whole range in one jump.
            limit = 0.25 * (max_v - min_v)
            v += float(np.clip(step, -limit, limit))
        return achieved

    # ------------------------------------------------------------------
    # Purge
    # ------------------------------------------------------------------

    def _run_purge(self, duration, f1, f2, amp):
        """Chirp the Sweep AO f1 → f2 → f1 over `duration` seconds.

        Frequency is swept up over the first half and back down over the
        second. Phase is integrated rather than computed from f*t, which would
        put a discontinuity at every frequency change and jolt the valve.

        The output rides 0 → amp (a raised cosine) so it starts and ends at
        0 V rather than mid-stroke.
        """
        row = self._get_ao_row('sweep')
        if row is None:
            return
        ch = _safe_str(row.get('Channel'), '')
        if not ch:
            return
        min_v, max_v = self._ao_limits(row)
        peak = float(np.clip(amp, min_v, max_v))

        # ~40 updates per cycle at the top frequency, so the fastest part of
        # the chirp still has a recognisable shape.
        rate     = max(200.0, 40.0 * max(f1, f2))
        interval = 1.0 / rate
        half     = duration / 2.0

        with _PersistentAO(self._ao_device(row), ch, min_v, max_v,
                           self._ao_type(row)) as ao:
            phase = 0.0
            t0    = time.perf_counter()
            last_notify = 0.0
            while True:
                t = time.perf_counter() - t0
                if t >= duration or self._abort:
                    break
                # Linear chirp up then back down.
                frac = t / half if t < half else (duration - t) / half
                f    = f1 + (f2 - f1) * float(np.clip(frac, 0.0, 1.0))
                phase += 2.0 * np.pi * f * interval
                # Taper the amplitude to zero over the last 10%. The integrated
                # phase will not happen to land on a multiple of 2*pi, so
                # without this the chirp stops mid-stroke and the zero write
                # below becomes a step change on a pressurised rig.
                env = 1.0
                tail = 0.1 * duration
                if tail > 0 and t > duration - tail:
                    env = max(0.0, (duration - t) / tail)
                v = 0.5 * peak * env * (1.0 - np.cos(phase))
                driven = ao.write(v)
                # Publishing every sample would hammer the override dict for no
                # visual gain; ~20 Hz is plenty for the display.
                if t - last_notify >= 0.05:
                    self._notify_output(ch, driven)
                    last_notify = t
                    self._signals.status_changed.emit(
                        f"Purging…  {t:0.1f}/{duration:0.1f} s   {f:0.1f} Hz"
                    )
                time.sleep(interval)

            ao.write(float(np.clip(0.0, min_v, max_v)))
        self._notify_output(ch, float(np.clip(0.0, min_v, max_v)))

    # ------------------------------------------------------------------
    # Valve sweep
    # ------------------------------------------------------------------

    def _run_valve_sweep(self, amp, steps, settle, settle_max, slope_tol):
        """Triangular staircase on the Sweep AO, 0 → amp → 0.

        Records the valve pressure gauge (and flow, when selected) at every
        command point, returning up/down legs. Those legs double as the
        command → valve pressure map used to place the valve at its leakage
        setting, which is why this has to run before the leak check.
        """
        row = self._get_ao_row('sweep')
        gauge = self._get_valve_gauge_data()
        if row is None or gauge is None:
            return None
        v_rb_idx, _ = gauge
        flow = self._get_flow_data()
        f_rb_idx = flow[0] if flow is not None else None

        min_v, max_v = self._ao_limits(row)
        peak = float(np.clip(amp, min_v, max_v))
        base = float(np.clip(0.0, min_v, max_v))

        out = {}
        for leg, volts in (('up',   np.linspace(base, peak, steps)),
                           ('down', np.linspace(peak, base, steps))):
            arrow = '↑' if leg == 'up' else '↓'
            v_arr, p_arr, q_arr = [], [], []
            for i, v in enumerate(volts):
                if self._abort:
                    break
                self._ao_write(row, float(v))
                reading = self._adaptive_settle(v_rb_idx, settle, settle_max, slope_tol)
                if reading is None:
                    raise RuntimeError(
                        "Could not read the valve pressure gauge — ensure the "
                        "main DAQ is running."
                    )
                v_arr.append(float(v))
                p_arr.append(reading)
                if f_rb_idx is not None:
                    q = self._read_gauge_from_rb(f_rb_idx)
                    q_arr.append(q if q is not None else np.nan)
                self._signals.status_changed.emit(
                    f"Valve sweep {arrow}  {i+1}/{steps}   "
                    f"({v:.3f} V → {reading:.1f})"
                )
            out[leg] = (np.asarray(v_arr), np.asarray(p_arr), np.asarray(q_arr))
            if self._abort:
                break
        return out

    def _valve_voltage_for_pressure(self, pressure, direction='up'):
        """Sweep AO command for a valve pressure, from the recorded sweep."""
        if not self._sweep_data or direction not in self._sweep_data:
            return None
        v, p, _q = self._sweep_data[direction]
        if v.size < 2:
            return None
        order = np.argsort(p)
        p_s, v_s = p[order], v[order]
        if p_s[-1] - p_s[0] <= 0:
            return None
        return float(np.interp(float(pressure), p_s, v_s))

    # ------------------------------------------------------------------
    # Leakage target resolution
    # ------------------------------------------------------------------

    def _leak_targets(self):
        """(valve_setting, test_stand_pressure) for the leak check, or None."""
        try:
            valve_set = float(self._leak_valve_setting.text())
        except ValueError:
            return None
        if self._leak_fixed_radio.isChecked():
            try:
                ts = float(self._leak_fixed.text())
            except ValueError:
                return None
        else:
            try:
                pct = float(self._leak_percent.text())
            except ValueError:
                return None
            ts = valve_set * pct / 100.0
        return valve_set, ts

    def _refresh_leak_resolved(self, *_):
        vu = self._valve_gauge_units()
        tu = self._ts_gauge_units()
        t = self._leak_targets()
        if t is None:
            self._leak_resolved.setText("—")
            return
        valve_set, ts = t
        mode = "fixed" if self._leak_fixed_radio.isChecked() else \
               f"{self._leak_percent.text().strip()}% of {valve_set:g}"
        note = ""
        if (not self._leak_fixed_radio.isChecked()
                and vu and tu and vu.lower() != tu.lower()):
            # A percentage of a valve-gauge value applied as a test stand
            # pressure only means something if both gauges read the same unit.
            note = f"   ⚠ percent taken across {vu} → {tu}"
        self._leak_resolved.setText(
            f"Valve at {valve_set:g} {vu}, test stand at {ts:g} {tu}  ({mode}){note}"
        )

    # ------------------------------------------------------------------
    # Graph clear / verdict prompt (UI thread)
    # ------------------------------------------------------------------

    def _on_clear_graph(self, restart):
        """Clear the plot, optionally restarting acquisition for a true reset.

        clear_plots only drops the curve items; plot_data immediately redraws
        the ring-buffer tail, so purge history reappears on the next frame.
        Restarting acquisition zeroes total_written, which is the only way to
        actually leave the graph empty going into the sweep.
        """
        mw = self.main_window
        try:
            if mw is not None and hasattr(mw, 'graphDataWidget'):
                mw.graphDataWidget.clear_plots()
        except Exception:
            print("Proportional Sweep: clear_plots failed:\n" + traceback.format_exc())

        if restart and mw is not None and getattr(mw, 'continue_running', False):
            try:
                mw.stop_task()
                mw.start_task()
                # start_task re-enables main-window controls; take them back.
                self._lock_main_window()
            except Exception:
                print("Proportional Sweep: acquisition restart failed:\n"
                      + traceback.format_exc())
        self._clear_done.set()

    def _on_set_plotting(self, enabled):
        """Freeze or thaw the graph without touching acquisition.

        plot_data() returns immediately when axes_plotting is empty, so
        emptying it stops the curves updating while the acquisition thread,
        the ring buffer and the numeric value displays all carry on. That
        matters here: the leak check still has to read the gauges to close the
        loop, so stopping the DAQ outright is not an option.

        The sweep therefore stays on screen as the final picture instead of
        being overwritten by leak-check data.
        """
        mw = self.main_window
        if mw is None:
            return
        try:
            if not enabled:
                if self._saved_axes is None:
                    self._saved_axes = list(getattr(mw, 'axes_plotting', []))
                mw.axes_plotting = []
            else:
                if self._saved_axes is not None:
                    # Only restore if the main window has not rebuilt the list
                    # itself in the meantime (start_task calls create_graph).
                    if not getattr(mw, 'axes_plotting', []):
                        mw.axes_plotting = self._saved_axes
                    self._saved_axes = None
        except Exception:
            print("Proportional Sweep: could not toggle plotting:\n"
                  + traceback.format_exc())

    def _on_verdict_prompt(self, message):
        """Ask the operator to pass or fail the leak check."""
        box = QMessageBox(self)
        box.setWindowTitle("Leakage Test")
        box.setIcon(QMessageBox.Question)
        box.setText(message)
        passed = box.addButton("Passed", QMessageBox.AcceptRole)
        failed = box.addButton("Failed", QMessageBox.RejectRole)
        box.exec_()
        self._verdict = "PASSED" if box.clickedButton() is passed else "FAILED"
        self._verdict_done.set()

    # ------------------------------------------------------------------
    # Base-class hooks
    # ------------------------------------------------------------------

    def _release_blocked_workers(self):
        """Wake a worker parked on the verdict prompt or the graph clear, so
        Force Stop can reach it."""
        self._verdict_done.set()
        self._clear_done.set()

    def _on_close_cleanup(self):
        self._on_set_plotting(True)     # never leave the graph frozen

    # ------------------------------------------------------------------
    # Main window lock / unlock
    # ------------------------------------------------------------------

    def _lock_main_window(self):
        mw = self.main_window
        if mw is None:
            return
        # Locking other apps out is the registry's job, driven by APP_SPEC
        # claims — this app never names another one.
        if hasattr(mw, 'refresh_app_availability'):
            mw.refresh_app_availability()
        ow = getattr(mw, 'outputSettingsWidget', None)
        if ow:
            ow.start_button.setEnabled(False)
            ow.start_button.setToolTip("Close Proportional Sweep to use Run")
            ow.stop_button.setEnabled(False)
            ow.stop_button.setToolTip("Close Proportional Sweep to use Stop")
        self._lock_axis_controls(mw)

    def _unlock_main_window(self):
        mw = self.main_window
        if mw is None:
            return
        daq_running = getattr(mw, 'continue_running', False)
        if hasattr(mw, 'refresh_app_availability'):
            mw.refresh_app_availability()
        ow = getattr(mw, 'outputSettingsWidget', None)
        if ow:
            ow.stop_button.setEnabled(True)
            ow.stop_button.setToolTip("")
            if not daq_running:
                ow.start_button.setEnabled(True)
                ow.start_button.setToolTip("")
        self._unlock_axis_controls(mw)

    def _lock_axis_controls(self, mw=None):
        """Disable the Y-axis checkbox this app has set, so it can't be cleared."""
        if mw is None:
            mw = self.main_window
        if mw is None or not hasattr(mw, 'plotSettingsWidget'):
            return
        ps = mw.plotSettingsWidget
        y_data = self._y_combo.currentData() if hasattr(self, '_y_combo') else None
        if y_data is None:
            return
        _rb_idx, dev_name = y_data
        try:
            import Multichannel_DAQ as _mdaq
            for i, row in _mdaq.plot_options.iterrows():
                cb = getattr(ps, f'plot_check_box{i}', None)
                if cb and row.get('Device Name') == dev_name:
                    cb.setEnabled(False)
                    cb.setToolTip("Controlled by Proportional Sweep — close it to change")
        except Exception:
            pass

    def _unlock_axis_controls(self, mw=None):
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
                    cb.setEnabled(True)
                    cb.setToolTip("")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Control state
    # ------------------------------------------------------------------

    def _set_controls_running(self, running: bool):
        """Lock/unlock controls during a worker. Run button managed separately."""
        self._force_stop_btn.setEnabled(running)
        self._close_btn.setEnabled(not running)
        self._tune_btn.setEnabled(not running)
        for w in (self._valve_no_nc, self._ts_gauge_combo, self._valve_gauge_combo,
                  self._flow_combo, self._y_combo,
                  self._ao_ts_combo, self._ao_sweep_combo,
                  self._amplitude, self._frequency, self._tune_steps,
                  self._tune_settle, self._tune_settle_max, self._tune_slope_tol,
                  self._leak_valve_setting, self._leak_fixed, self._leak_percent,
                  self._leak_fixed_radio, self._leak_percent_radio,
                  self._seq_setpoint, self._seq_tol,
                  self._purge_dur, self._purge_f1, self._purge_f2, self._purge_amp,
                  self._sweep_amp, self._sweep_steps, self._sweep_settle,
                  self._clear_chk):
            w.setEnabled(not running)

        if running:
            self._run_btn.setEnabled(False)
        else:
            # Run stays gated on the tune, not merely on "not busy".
            self._refresh_tune_ui()

    def _run_btn_set_enabled(self, enabled: bool):
        self._run_btn.setEnabled(enabled and self._ts_tuned)

    # ------------------------------------------------------------------
    # Signal handlers (UI thread)
    # ------------------------------------------------------------------

    def _on_test_finished(self):
        # Leave a verdict or an abort/error message in place — only announce
        # completion when the sequence ran without one being set.
        if self._verdict:
            self._status_label.setText(f"Leakage test {self._verdict}")
        elif "abort" not in self._status_label.text().lower() \
                and "ERROR" not in self._status_label.text():
            self._signals.status_changed.emit("Test complete  ✅")

    # ------------------------------------------------------------------
    # Run / stop
    # ------------------------------------------------------------------

    def _run_test(self):
        """Run the full proportional sweep / leakage sequence.

        1. Bring the test stand to its setpoint (curve seed + closed-loop trim).
        2. Purge: chirp the Sweep AO up and back down.
        3. Clear the graph.
        4. Triangular sweep of the Sweep AO, 0 → amp → 0, plotted and recorded.
        5. Park the valve at its leakage setting, using the sweep just recorded.
        6. Bring the test stand to the leak-check pressure.
        7. Ask the operator to pass or fail, then zero the outputs.
        """
        if self._is_running:
            return
        if not self._ts_tuned:
            QMessageBox.warning(
                self, "Not Tuned",
                "Run Tune Test Stand Pressure before running the test."
            )
            return

        missing = self._missing_selections()
        if missing:
            QMessageBox.warning(
                self, "Run Test",
                "Select the following first:\n\n  • " + "\n  • ".join(missing)
            )
            return

        ts_row    = self._get_ao_row('test_stand')
        sweep_row = self._get_ao_row('sweep')

        targets = self._leak_targets()
        if targets is None:
            QMessageBox.warning(self, "Run Test",
                                "Leakage Test values must be numbers.")
            return
        valve_set, leak_ts = targets

        tune = self._tune_params()
        if tune is None:
            return
        _amp, _freq, _steps, dwell, settle_max, slope_tol = tune

        try:
            setpoint   = float(self._seq_setpoint.text())
            tol        = float(self._seq_tol.text())
            purge_dur  = float(self._purge_dur.text())
            purge_f1   = float(self._purge_f1.text())
            purge_f2   = float(self._purge_f2.text())
            purge_amp  = float(self._purge_amp.text())
            sweep_amp  = float(self._sweep_amp.text())
            sweep_stp  = int(float(self._sweep_steps.text()))
            sweep_set  = float(self._sweep_settle.text())
        except ValueError:
            QMessageBox.warning(self, "Run Test",
                                "Test Sequence values must be numbers.")
            return
        if sweep_stp < 2:
            QMessageBox.warning(self, "Run Test", "Valve Sweep steps must be at least 2.")
            return
        if purge_dur <= 0 or purge_f1 <= 0 or purge_f2 <= 0:
            QMessageBox.warning(self, "Run Test",
                                "Purge duration and frequencies must be greater than zero.")
            return

        restart_on_clear = self._clear_chk.isChecked()

        self._sweep_data = {}
        self._verdict    = None
        self._set_controls_running(True)
        self._signals.status_changed.emit("Starting test…")

        # Everything reads through the ring buffer, so acquisition must be live.
        mw = self.main_window
        if mw and not getattr(mw, 'continue_running', False):
            try:
                mw.start_task()
            except Exception:
                print("Proportional Sweep — could not auto-start DAQ:\n"
                      + traceback.format_exc())

        def _worker():
            try:
                # 1 ── test stand up to setpoint (rising, so the up leg) ────
                self._signals.status_changed.emit("Setting test stand pressure…")
                got = self._settle_test_stand_pressure(
                    setpoint, 'up', tol, dwell, settle_max, slope_tol)
                if self._abort:
                    raise _Aborted()
                if got is None:
                    raise RuntimeError(
                        "Could not set test stand pressure — check the tune and "
                        "that the gauge is reading."
                    )

                # 2 ── purge ────────────────────────────────────────────────
                self._signals.status_changed.emit("Purging…")
                self._run_purge(purge_dur, purge_f1, purge_f2, purge_amp)
                if self._abort:
                    raise _Aborted()

                # 3 ── clear graph, then wait for the UI thread to finish ───
                self._signals.status_changed.emit("Clearing graph…")
                self._clear_done.clear()
                self._signals.clear_graph_sig.emit(restart_on_clear)
                if not self._clear_done.wait(timeout=15.0):
                    print("Proportional Sweep: graph clear timed out; continuing.")
                if restart_on_clear:
                    # Let the restarted acquisition put samples in the ring
                    # before anything tries to read it.
                    time.sleep(0.5)
                if self._abort:
                    raise _Aborted()

                # 4 ── triangular valve sweep ───────────────────────────────
                self._signals.status_changed.emit("Sweeping valve…")
                data = self._run_valve_sweep(
                    sweep_amp, sweep_stp, sweep_set, settle_max, slope_tol)
                if self._abort:
                    raise _Aborted()
                if not data:
                    raise RuntimeError("Valve sweep produced no data.")
                self._sweep_data = data

                # Freeze the graph so the sweep stays on screen — the leak
                # check that follows would otherwise scroll it away. Acquisition
                # keeps running because the trim below needs gauge reads.
                self._signals.plot_enable_sig.emit(False)

                # 5 ── park the valve at its leakage setting ────────────────
                # Approaching from 0 V after the triangle returned to base, so
                # the rising leg is the right map to invert.
                self._signals.status_changed.emit("Setting valve to leakage point…")
                v_cmd = self._valve_voltage_for_pressure(valve_set, 'up')
                if v_cmd is None:
                    raise RuntimeError(
                        f"Valve setting {valve_set:g} is not covered by the sweep "
                        f"just recorded — widen the sweep amplitude or lower the "
                        f"setting."
                    )
                self._ao_write(sweep_row, v_cmd)
                time.sleep(max(dwell, 0.2))
                if self._abort:
                    raise _Aborted()

                # 6 ── test stand to the leak-check pressure ────────────────
                # Direction is chosen from where we actually are, so a drop from
                # the setpoint seeds off the falling leg.
                self._signals.status_changed.emit("Setting leak-check pressure…")
                direction = 'down' if leak_ts < got else 'up'
                leak_got = self._settle_test_stand_pressure(
                    leak_ts, direction, tol, dwell, settle_max, slope_tol)
                if self._abort:
                    raise _Aborted()

                # 7 ── operator verdict ────────────────────────────────────
                vu = self._valve_gauge_units()
                tu = self._ts_gauge_units()
                msg = (
                    f"Valve at {valve_set:g} {vu}  ({v_cmd:.3f} V)\n"
                    f"Test stand target {leak_ts:g} {tu}"
                    + (f", reading {leak_got:.1f} {tu}" if leak_got is not None else "")
                    + "\n\nDid the valve pass the leakage check?"
                )
                self._verdict = None
                self._verdict_done.clear()
                self._signals.status_changed.emit("Awaiting operator verdict…")
                self._signals.verdict_sig.emit(msg)
                self._verdict_done.wait()

                self._signals.status_changed.emit(
                    f"Leakage test {self._verdict or 'no verdict'}"
                )

            except _Aborted:
                self._signals.status_changed.emit("Test aborted  ⚠")
            except Exception:
                print("Proportional Sweep — run worker error:\n"
                      + traceback.format_exc())
                self._signals.status_changed.emit("Test ERROR — see log  ❌")
            finally:
                # Always leave the rig at zero, verdict or not.
                try:
                    self._ao_write(sweep_row, float(np.clip(0.0, *self._ao_limits(sweep_row))))
                    self._ao_write(ts_row, float(np.clip(0.0, *self._ao_limits(ts_row))))
                except Exception:
                    print("Proportional Sweep: could not zero outputs:\n"
                          + traceback.format_exc())
                self._signals.plot_enable_sig.emit(True)
                self._is_running = False
                self._abort      = False
                self._signals.test_finished.emit()
                self._signals.controls_unlock.emit()
                self._signals.stop_daq_sig.emit()

        self._is_running = True
        self._abort      = False
        self._worker_thread = threading.Thread(target=_worker, daemon=True)
        self._worker_thread.start()

    # ------------------------------------------------------------------
    # Close
    # ------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Apps-bar registration (see DAQ_AppRegistry.py)
# ---------------------------------------------------------------------------

def build(ctx, parent):
    return ProportionalSweepDialog(
        main_window=ctx.main_window,
        default_settings_output=ctx.output_settings,
        default_settings_input=ctx.input_settings,
        default_settings_dig=ctx.dig_settings,
        selected_daq=ctx.selected_daq,
        sample_rate=ctx.sample_rate,
        parent=parent,
    )


APP_SPEC = {
    'id':    'proportional_sweep',
    'label': 'Proportional Sweep',
    'cls':   'ProportionalSweepDialog',
    'build': build,
    'color': '#1f618d',
    'hover': '#2980b9',
    'show_by_default': False,
    'requires_daq_hw': True,
    'requires_daq_stopped': True,
    'blocks_calibration': True,
    'locks_run_stop': True,
    'claims': {'ao'},
}
