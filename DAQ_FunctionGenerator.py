
import sys
import time
import threading
import warnings
import numpy as np

from PyQt5.QtWidgets import QApplication
if __name__ == "__main__":
   _standalone_app = QApplication.instance() or QApplication(sys.argv)

import nidaqmx
from nidaqmx.stream_writers import AnalogSingleChannelWriter, AnalogMultiChannelWriter
import nidaqmx.constants as ni

# DAQ_Settings owns the channel-type tables, so the per-type nidaqmx call is
# looked up there instead of assuming every AO channel is a voltage output.
from DAQ_Settings import (add_ao_channel, channel_unit, row_channel_type,
                          timing_group)


# Software-timed fallback: update rate, and how many updates one cycle needs to
# still resemble the requested shape. A QTimer cannot hold better than a few
# hundred Hz on a desktop OS, so anything faster than SW_RATE / SW_MIN_PTS is
# not the waveform the user asked for.
SW_RATE    = 200
SW_MIN_PTS = 10


class _HwTimingUnavailable(Exception):
    """Hardware-timed AO could not be started, so the software path should try.

    Raised only for a driver refusal (DaqError). ao_samp_clk_supported says the
    card HAS a sample clock, not that the clock is free: on a cDAQ chassis one
    AO timing engine is shared by every module, so another app holding it makes
    the build fail even though the capability query passed.
    """
try:
    from nidaqmx.errors import DaqError, DaqWarning
except ImportError:      # older nidaqmx layouts
    DaqWarning = Warning

    class DaqError(Exception):
        """Placeholder that nothing raises.

        Deliberately not `Exception`: DaqError gates the fall back from
        hardware- to software-timed output, and aliasing it to Exception would
        make every bug in the build path look like a driver refusal and get
        silently retried on the slow path.
        """

from PyQt5.QtWidgets import (QDialog, QGridLayout, QHBoxLayout, QVBoxLayout, QLabel, QLineEdit,
                            QPushButton, QComboBox, QRadioButton, QButtonGroup, QWidget,
                            QGroupBox, QTableWidget, QTableWidgetItem, QHeaderView,
                            QDialogButtonBox, QMessageBox, QCheckBox, QSlider)
from PyQt5.QtCore import QTimer, pyqtSignal, Qt


def _fg_settings_path():
    """Resolve the working default_settings.xlsx path (same logic as the apps)."""
    import os
    if getattr(sys, 'frozen', False):
        base = os.path.dirname(sys.executable)
    elif '__file__' in globals():
        base = os.path.dirname(os.path.abspath(__file__))
    else:
        base = os.getcwd()
    return os.path.join(base, 'default_settings.xlsx')


def _fg_math_ns():
    """numpy-backed namespace for evaluating FG math expressions in t and f."""
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


def _fg_parse_points(text):
    """Parse 'x:y, x:y, …' into a list of (x, y) floats, or None if invalid."""
    text = (text or '').strip()
    if not text:
        return None
    import re
    pts = []
    for p in [s for s in re.split(r'[;,]', text) if s.strip()]:
        if ':' not in p:
            return None
        a, b = p.split(':', 1)
        try:
            pts.append((float(a), float(b)))
        except ValueError:
            return None
    return pts if len(pts) >= 2 else None


# ---------------------------------------------------------------------------
# Per-channel settings  (UI values only — no task handles here)
# ---------------------------------------------------------------------------

class _ChannelState:
   """Stores the waveform settings for one AO channel and its current voltage
   for ring-buffer tracking.  Task handles live on the dialog, not here."""

   def __init__(self):
       self.waveform_type   = "Sine"
       self.amplitude       = "2.5"
       self.dc_offset       = "2.5"
       self.duty_cycle      = "50"
       self.frequency       = "0.5"
       self.multiplier      = 1        # 1 | 10 | 100 | 1000
       self.custom_points   = None     # list[(x, y)] for the "Custom" waveform
       self.custom_x_unit   = "cycle"   # cycle | % | degree | time  (X axis unit)
       # Frequency-sweep (chirp) mode — applies to Sine/Square/Triangular/Sawtooth
       self.sweep_enabled   = False
       self.sweep_log       = False     # False = linear, True = logarithmic
       self.sweep_f1        = "1"       # start frequency (Hz)
       self.sweep_f2        = "100"     # end frequency (Hz)
       self.sweep_time      = "1.0"     # seconds for one F1→F2 pass
       self.sweep_mode      = "Repeat"  # Repeat | Up-Down | Once
       self.is_running      = False    # True while this channel is active
       self.current_voltage = 0.0     # updated by the tracking timer


# ---------------------------------------------------------------------------
# Dialog
# ---------------------------------------------------------------------------

class FunctionGeneratorDialog(QDialog):
   """Multi-channel function generator.

   NI-DAQmx constraint: all AO channels on the same device must share a
   single hardware-timed task.  The dialog therefore manages ONE shared task
   that is rebuilt whenever the set of active channels changes (start /
   stop individual channels or use Start All / Stop All).

   For software-timed (on-demand) devices the same rule holds: a single
   task is used for all active channels, stepped by one QTimer.

   Signals
   -------
   generation_started  emitted when the first channel goes active
   generation_stopped  emitted when the last channel stops
   """

   # Sweep behaviour modes. The "(stop)" variants generate a single pass from
   # a FINITE task and then stop themselves; the others regenerate until Stop
   # is pressed. Plain "Once" is the pre-stop approximation of stopping: it
   # sweeps then holds at F2, because a regenerating buffer had no way to end.
   _SWEEP_MODES      = ("Repeat", "Up-Down", "Once", "Up-Down (stop)", "Once (stop)")
   _SWEEP_STOP_MODES = ("Up-Down (stop)", "Once (stop)")

   generation_started = pyqtSignal()
   generation_stopped = pyqtSignal()
   # Fired from the DAQmx done-event callback, which runs on a driver thread.
   # Qt objects must not be touched there, so the burst teardown is marshalled
   # onto the UI thread through this signal rather than done in the callback.
   burst_finished     = pyqtSignal()

   def __init__(self, selected_daq, default_settings_output, sample_rate,
                ao_sample_rate=None, parent=None):
       super().__init__(parent)
       self.selected_daq            = selected_daq
       self.default_settings_output = default_settings_output
       self.sample_rate             = sample_rate
       self._ao_sample_rate         = ao_sample_rate

       self.setWindowTitle("Function Generator")

       n = max(1, len(default_settings_output))
       self._ch_states: list[_ChannelState] = [_ChannelState() for _ in range(n)]
       self._current_ch = 0
       self._custom_funcs = {}     # name -> {"kind": "waveform"/"math", ...}

       # ---- shared task state ----
       # AO channels may live on different devices/modules, and a single
       # NI-DAQmx AO task cannot span multiple devices, so we build one task
       # per device. _ao_groups holds them; each entry is a dict:
       #   {task, writer, start, count, single, daq}
       # where start/count index into the flat _active_indices / *_waveforms
       # lists (which are ordered by device group).
       self._ao_groups    = []
       self._ao_task      = None   # legacy alias (first group's task or None)
       self._ao_writer    = None   # legacy alias
       self._sw_mode      = False  # True when using software-timed (on-demand) path
       self._sw_timer     = None   # QTimer for software-timed writes
       self._sw_waveforms = []     # list of 1-D arrays, one per active channel
       self._sw_idx       = 0
       self._sw_pass_len  = 0      # samples in one run-once pass (sw path)
       self._hw_waveforms = []     # same structure, for hw-timed tracking
       self._hw_start_time     = 0.0
       self._hw_ao_sample_rate = 10000
       self._hw_track_timer    = None
       self._active_indices: list[int] = []  # channel indices currently in the task

       self._ct_locked = False   # True while Custom Triggers owns generation

       # ---- burst (run-once) state ----
       # When armed, generation plays the buffer exactly once and stops itself
       # instead of regenerating. The flag is captured at start; toggling the
       # checkbox mid-run must not change what the running task does.
       self._burst_armed   = False
       self._burst_active  = False
       self._burst_done_at = 0.0
       # Devices each get their own task, so a burst spanning two DAQs has two
       # done events. Tearing down on the FIRST would stop the other device
       # mid-generation, so completions are counted and the teardown waits for
       # all of them. The counter is touched from DAQmx driver threads.
       self._burst_lock    = threading.Lock()
       self._burst_pending = 0

       # ---- DC slider state ----
       # The slider is just a nicer way to type into the DC entry; the level is
       # baked into the AO buffer when generation starts, so the slider is
       # disabled while the channel is running.
       # Slider counts per engineering unit. Recomputed per channel by
       # _update_dc_slider_range: a fixed counts-per-volt figure gives a 4-20 mA
       # channel a 20-count slider, so the scale follows the channel's span
       # instead (_DC_SLIDER_COUNTS steps across it, whatever the unit).
       self._dc_slider_scale = 1000.0
       self._dc_sync_guard   = False    # blocks slider <-> entry feedback loops

       self._setup_ui()
       self._load_channel_to_ui(0)

   # ------------------------------------------------------------------
   # UI
   # ------------------------------------------------------------------

   def _setup_ui(self):
       n_ch = len(self._ch_states)
       dialog_h = 268
       self.setFixedSize(560, dialog_h)

       layout = QGridLayout(self)
       layout.setVerticalSpacing(4)

       # Row 0 — AO channel selector (multi-channel only)
       if n_ch > 1:
           ch_group = QGroupBox()   # border preserved; no title text
           ch_group_layout = QHBoxLayout(ch_group)
           ch_group_layout.setContentsMargins(4, 2, 4, 2)
           ch_group_layout.setSpacing(12)

           self._ch_radio_group = QButtonGroup(self)
           self._ch_radios: list[QRadioButton] = []

           ch_group_layout.addWidget(QLabel("Channel:"))
           ch_group_layout.addStretch()

           for i in range(n_ch):
               rb = QRadioButton(self._ch_display_name(i))
               rb.setChecked(i == 0)
               self._ch_radio_group.addButton(rb, i)
               ch_group_layout.addWidget(rb)
               self._ch_radios.append(rb)

           self._ch_radio_group.buttonClicked[int].connect(self._on_channel_selected)

           layout.addWidget(ch_group, 0, 0, 1, 4)

           self.start_all_button = None
           self.stop_all_button  = None
       else:
           self._ch_radio_group = None
           self._ch_radios = []
           self.start_all_button = None
           self.stop_all_button  = None

       # Rows 1-2 — waveform + amplitude + DC + duty cycle
       r = 1
       layout.addWidget(QLabel("Select Function:"), r, 0)
       self.waveform_type_var = QComboBox()
       self._BASE_WAVEFORMS = ["Sine", "Square", "Triangular", "Sawtooth", "DC", "Custom"]
       self._custom_funcs = self._load_custom_functions()
       self.waveform_type_var.addItems(self._BASE_WAVEFORMS + list(self._custom_funcs.keys()))
       layout.addWidget(self.waveform_type_var, r + 1, 0)

       self._amplitude_label = QLabel(f"Amplitude ({self._chan_unit()}):")
       layout.addWidget(self._amplitude_label, r, 1)
       self.amplitude_entry = QLineEdit("2.5")
       layout.addWidget(self.amplitude_entry, r + 1, 1)

       # "Edit Points…" button shares the amplitude cell; shown only for Custom
       # (where amplitude is meaningless — the points are absolute volts).
       self.edit_points_btn = QPushButton("Edit Points…")
       self.edit_points_btn.clicked.connect(self._open_custom_editor)
       self.edit_points_btn.setVisible(False)
       layout.addWidget(self.edit_points_btn, r + 1, 1)

       self._dc_offset_label = QLabel(f"DC Offset ({self._chan_unit()}):")
       layout.addWidget(self._dc_offset_label, r, 2)
       self.dc_offset_entry = QLineEdit("2.5")
       layout.addWidget(self.dc_offset_entry, r + 1, 2)

       self.duty_cycle_label = QLabel("Duty Cycle (%):")
       layout.addWidget(self.duty_cycle_label, r, 3)
       self.duty_cycle_entry = QLineEdit("50")
       layout.addWidget(self.duty_cycle_entry, r + 1, 3)

       # Rows 3-4 — frequency + multiplier (normal) OR sweep controls.
       # Wrapped in a fixed-height container so hiding them doesn't collapse
       # the grid row and shift the waveform/button rows.
       freq_outer = QWidget()
       freq_vbox  = QVBoxLayout(freq_outer)
       freq_vbox.setContentsMargins(0, 0, 0, 0)
       freq_vbox.setSpacing(2)

       # Sweep enable checkbox
       self.sweep_checkbox = QCheckBox("Sweep mode (F1 → F2)")
       self.sweep_checkbox.toggled.connect(self._on_sweep_toggled)
       freq_vbox.addWidget(self.sweep_checkbox)

       # ---- Normal frequency + multiplier (shown when sweep is OFF) ----
       self._normal_freq_widget = QWidget()
       freq_grid  = QGridLayout(self._normal_freq_widget)
       freq_grid.setContentsMargins(0, 0, 0, 0)
       freq_grid.setVerticalSpacing(4)
       freq_grid.setColumnStretch(0, 1)
       freq_grid.setColumnStretch(1, 3)

       self._frequency_label = QLabel("Frequency (Hz):")
       freq_grid.addWidget(self._frequency_label, 0, 0)
       self.frequency_entry = QLineEdit("0.5")
       freq_grid.addWidget(self.frequency_entry, 1, 0)

       self._multiplier_label = QLabel("Multiplier:")
       freq_grid.addWidget(self._multiplier_label, 0, 1)
       self.multiplier_var = QButtonGroup(self)
       radio_container = QWidget()
       radio_layout = QHBoxLayout(radio_container)
       radio_layout.setContentsMargins(0, 0, 0, 0)
       for text, value in [("x1", 1), ("x10", 10), ("x100", 100), ("x1000", 1000)]:
           radio = QRadioButton(text)
           self.multiplier_var.addButton(radio, value)
           radio_layout.addWidget(radio)
           if value == 1:
               radio.setChecked(True)
       self._multiplier_container = radio_container
       freq_grid.addWidget(radio_container, 1, 1)
       freq_vbox.addWidget(self._normal_freq_widget)

       # ---- Sweep controls (shown when sweep is ON) ----
       self._sweep_widget = QWidget()
       sweep_grid = QGridLayout(self._sweep_widget)
       sweep_grid.setContentsMargins(0, 0, 0, 0)
       sweep_grid.setVerticalSpacing(4)

       self.sweep_type_group = QButtonGroup(self)
       self._sweep_lin_radio = QRadioButton("Linear")
       self._sweep_log_radio = QRadioButton("Log")
       self._sweep_lin_radio.setChecked(True)
       self.sweep_type_group.addButton(self._sweep_lin_radio, 0)
       self.sweep_type_group.addButton(self._sweep_log_radio, 1)
       sweep_grid.addWidget(QLabel("Sweep:"), 0, 0)
       lin_log = QHBoxLayout()
       lin_log.setContentsMargins(0, 0, 0, 0)
       lin_log.addWidget(self._sweep_lin_radio)
       lin_log.addWidget(self._sweep_log_radio)
       lin_log_w = QWidget(); lin_log_w.setLayout(lin_log)
       sweep_grid.addWidget(lin_log_w, 1, 0)

       sweep_grid.addWidget(QLabel("Freq 1 (Hz):"), 0, 1)
       self.sweep_f1_entry = QLineEdit("1")
       sweep_grid.addWidget(self.sweep_f1_entry, 1, 1)
       sweep_grid.addWidget(QLabel("Freq 2 (Hz):"), 0, 2)
       self.sweep_f2_entry = QLineEdit("100")
       sweep_grid.addWidget(self.sweep_f2_entry, 1, 2)
       sweep_grid.addWidget(QLabel("Duration (s):"), 0, 3)
       self.sweep_time_entry = QLineEdit("1.0")
       sweep_grid.addWidget(self.sweep_time_entry, 1, 3)
       sweep_grid.addWidget(QLabel("Mode:"), 0, 4)
       self.sweep_mode_combo = QComboBox()
       self.sweep_mode_combo.addItems(
           ["Repeat", "Up-Down", "Once", "Up-Down (stop)", "Once (stop)"])
       self.sweep_mode_combo.setToolTip(
           "Continuous — runs until Stop is pressed:\n"
           "  Repeat:   F1→F2 then snap back to F1 and repeat.\n"
           "  Up-Down:  F1→F2→F1 continuously (no snap-back).\n"
           "  Once:     sweep F1→F2 then hold, oscillating at F2.\n"
           "\n"
           "Run-once — generates a single pass, then stops and drops to 0:\n"
           "  Up-Down (stop): one F1→F2→F1 pass.\n"
           "  Once (stop):    one F1→F2 pass.\n"
           "\n"
           "With several channels started together the pass is set by the\n"
           "\"(stop)\" channel, and every channel on that device stops with it."
       )
       sweep_grid.addWidget(self.sweep_mode_combo, 1, 4)
       self._sweep_widget.setVisible(False)
       freq_vbox.addWidget(self._sweep_widget)

       # ---- DC level slider (shown only for the DC waveform) ----
       # Lives in the space the frequency controls occupy, which is free when
       # DC is selected, so showing it doesn't resize the dialog.
       self._dc_slider_widget = QWidget()
       dc_hbox = QHBoxLayout(self._dc_slider_widget)
       dc_hbox.setContentsMargins(0, 0, 0, 0)
       dc_hbox.setSpacing(6)
       self._dc_slider_min_label = QLabel("")
       self._dc_slider_max_label = QLabel("")
       self.dc_slider = QSlider(Qt.Horizontal)
       self.dc_slider.setTracking(True)
       self.dc_slider.setToolTip(
           "Drag to set the DC output level.\n"
           "Disabled while the channel is generating — stop it to change level."
       )
       dc_hbox.addWidget(self._dc_slider_min_label)
       dc_hbox.addWidget(self.dc_slider, 1)
       dc_hbox.addWidget(self._dc_slider_max_label)
       self._dc_slider_widget.setVisible(False)
       freq_vbox.addWidget(self._dc_slider_widget)

       # Span 2 grid rows (r+2 and r+3) so the container fills the same space
       layout.addWidget(freq_outer, r + 2, 0, 2, 4)
       self._freq_outer = freq_outer

       # Row 5 — Start / Stop / Close
       self.start_button = QPushButton("▶ Start")
       self.start_button.clicked.connect(self._start_current)

       self.stop_button = QPushButton("■ Stop")
       self.stop_button.clicked.connect(self._stop_current)
       self.stop_button.setEnabled(False)

       close_button = QPushButton("Close")
       close_button.clicked.connect(self.close)

       btn_container = QWidget()
       btn_layout = QHBoxLayout(btn_container)
       btn_layout.setContentsMargins(0, 0, 0, 0)
       btn_layout.addWidget(self.start_button)
       btn_layout.addWidget(self.stop_button)
       btn_layout.addWidget(close_button)
       layout.addWidget(btn_container, r + 4, 0, 1, 4)

       # DC entry <-> slider two-way sync
       self.dc_slider.valueChanged.connect(self._on_dc_slider_changed)
       self.dc_offset_entry.textChanged.connect(self._on_dc_text_changed)
       self.dc_offset_entry.editingFinished.connect(self._on_dc_editing_finished)
       self._update_dc_slider_range()

       self.waveform_type_var.currentTextChanged.connect(self.on_waveform_select)
       self.on_waveform_select(self.waveform_type_var.currentText())

       self.burst_finished.connect(self._on_burst_finished)

   # ------------------------------------------------------------------
   # Channel switching
   # ------------------------------------------------------------------

   def _save_ui_to_channel(self, ch_idx: int):
       if ch_idx < 0 or ch_idx >= len(self._ch_states):
           return
       s = self._ch_states[ch_idx]
       s.waveform_type = self.waveform_type_var.currentText()
       s.amplitude     = self.amplitude_entry.text()
       s.dc_offset     = self.dc_offset_entry.text()
       s.duty_cycle    = self.duty_cycle_entry.text()
       s.frequency     = self.frequency_entry.text()
       s.multiplier    = self.multiplier_var.checkedId() or 1
       s.sweep_enabled = self.sweep_checkbox.isChecked()
       s.sweep_log     = self._sweep_log_radio.isChecked()
       s.sweep_f1      = self.sweep_f1_entry.text()
       s.sweep_f2      = self.sweep_f2_entry.text()
       s.sweep_time    = self.sweep_time_entry.text()
       s.sweep_mode    = self.sweep_mode_combo.currentText()

   def _load_channel_to_ui(self, ch_idx: int):
       if ch_idx < 0 or ch_idx >= len(self._ch_states):
           return
       s = self._ch_states[ch_idx]
       self.waveform_type_var.blockSignals(True)
       self.waveform_type_var.setCurrentText(s.waveform_type)
       self.waveform_type_var.blockSignals(False)
       self.amplitude_entry.setText(s.amplitude)
       self.dc_offset_entry.setText(s.dc_offset)
       self.duty_cycle_entry.setText(s.duty_cycle)
       self.frequency_entry.setText(s.frequency)
       for btn in self.multiplier_var.buttons():
           if self.multiplier_var.id(btn) == s.multiplier:
               btn.setChecked(True)
               break
       # Sweep settings
       self.sweep_checkbox.blockSignals(True)
       self.sweep_checkbox.setChecked(bool(s.sweep_enabled))
       self.sweep_checkbox.blockSignals(False)
       (self._sweep_log_radio if s.sweep_log else self._sweep_lin_radio).setChecked(True)
       self.sweep_f1_entry.setText(s.sweep_f1)
       self.sweep_f2_entry.setText(s.sweep_f2)
       self.sweep_time_entry.setText(s.sweep_time)
       if s.sweep_mode in self._SWEEP_MODES:
           self.sweep_mode_combo.setCurrentText(s.sweep_mode)
       self.on_waveform_select(s.waveform_type)
       self._refresh_ui_buttons()

   def _on_channel_selected(self, new_idx: int):
       if new_idx == self._current_ch:
           return
       self._save_ui_to_channel(self._current_ch)
       self._current_ch = new_idx
       self._load_channel_to_ui(new_idx)
       self._update_dc_slider_range()

   def _refresh_ui_buttons(self):
       """Sync Start/Stop and the DC slider with live state, respecting the CT
       lock (while Custom Triggers owns generation, nothing here is usable)."""
       running = self._ch_states[self._current_ch].is_running
       self.start_button.setEnabled(not self._ct_locked and not running)
       self.stop_button.setEnabled(not self._ct_locked and running)
       # The DC level is written into the AO buffer at start, so it can only be
       # changed while this channel is stopped.
       self.dc_slider.setEnabled(not self._ct_locked and not running)

   # ------------------------------------------------------------------
   # Waveform helpers
   # ------------------------------------------------------------------

   def on_waveform_select(self, waveform: str):
       spec = getattr(self, "_custom_funcs", {}).get(waveform)
       is_custom_func = spec is not None   # a saved point-waveform or math function

       is_dc     = (waveform == "DC")
       is_custom = (waveform == "Custom")
       is_duty   = (waveform in ("Square", "Sawtooth"))
       is_freq   = (waveform != "DC")

       # Amplitude — hidden for DC, the built-in Custom, and any saved custom
       # function (custom waveforms use absolute-volt points; math functions
       # define volts directly via their expression, so Amplitude doesn't apply).
       amp_visible = not is_dc and not is_custom and not is_custom_func
       self._amplitude_label.setVisible(amp_visible)
       self.amplitude_entry.setVisible(amp_visible)

       # Edit Points button — only for the built-in Custom (shares amplitude cell)
       self.edit_points_btn.setVisible(is_custom)

       # DC offset — meaningless for Custom and for saved custom functions
       # (their output is already absolute volts).
       dc_visible = not is_custom and not is_custom_func
       # For DC the field IS the output level, not an offset on a carrier.
       u = self._chan_unit()
       self._amplitude_label.setText(f"Amplitude ({u}):")
       self._dc_offset_label.setText(f"DC ({u}):" if is_dc else f"DC Offset ({u}):")
       self._dc_offset_label.setVisible(dc_visible)
       self.dc_offset_entry.setVisible(dc_visible)

       # DC level slider — only meaningful for the DC waveform
       self._dc_slider_widget.setVisible(is_dc)
       if is_dc:
           self._update_dc_slider_range()

       # Duty cycle — hidden unless Square / Sawtooth
       self.duty_cycle_label.setVisible(is_duty)
       self.duty_cycle_entry.setVisible(is_duty)

       # Sweep mode applies only to the periodic carrier waveforms.
       can_sweep = waveform in ("Sine", "Square", "Triangular", "Sawtooth")
       self.sweep_checkbox.setVisible(can_sweep)
       sweeping = can_sweep and self.sweep_checkbox.isChecked()

       # Frequency + Multiplier (normal) vs Sweep controls. Keep the outer
       # container visible so the grid row doesn't collapse and shift layout.
       show_normal_freq = is_freq and not sweeping
       self._frequency_label.setVisible(show_normal_freq)
       self.frequency_entry.setVisible(show_normal_freq)
       self._multiplier_label.setVisible(show_normal_freq)
       self._multiplier_container.setVisible(show_normal_freq)
       self._normal_freq_widget.setVisible(show_normal_freq)
       self._sweep_widget.setVisible(sweeping)
       self._freq_outer.setVisible(True)

       # Custom + time(ms): the period comes from the points → lock Frequency.
       self._sync_custom_freq_field()

   # ------------------------------------------------------------------
   # DC level: entry <-> slider sync (applied at next start)
   # ------------------------------------------------------------------

   _DC_SLIDER_COUNTS = 1000.0   # slider steps across a channel's full span

   def _chan_limits(self, idx):
       """(Min, Max) for AO row `idx`, defaulting to 0–5.

       In the unit given by the row's 'Range Unit' — volts for a voltage
       output, amps for a current one.
       """
       try:
           return (float(self.default_settings_output['Min'].iloc[idx]),
                   float(self.default_settings_output['Max'].iloc[idx]))
       except Exception:
           return 0.0, 5.0

   def _dc_channel_limits(self, ch_idx=None):
       """(low, high) for the DC slider, in the channel's own unit: 0 up to the
       channel's Max (raised to Min if that channel cannot reach 0)."""
       idx = self._current_ch if ch_idx is None else ch_idx
       mn, mx = self._chan_limits(idx)
       lo = max(0.0, mn)
       hi = mx
       if hi <= lo:
           hi = lo + 5.0
       return lo, hi

   def _update_dc_slider_range(self):
       """Re-range the slider for the currently selected channel.

       The scale is derived from the span so the handle has the same resolution
       and travel on a 0-5 V channel as on a 4-20 mA one; the step sizes are
       fractions of the span for the same reason.
       """
       lo, hi = self._dc_channel_limits()
       span = hi - lo
       sc = (self._DC_SLIDER_COUNTS / span) if span > 0 else 1.0
       self._dc_slider_scale = sc
       self._dc_sync_guard = True
       try:
           self.dc_slider.setRange(int(round(lo * sc)), int(round(hi * sc)))
           # 0.2% and 2% of full span per arrow key / page key.
           self.dc_slider.setSingleStep(max(1, int(round(0.002 * self._DC_SLIDER_COUNTS))))
           self.dc_slider.setPageStep(max(1, int(round(0.02 * self._DC_SLIDER_COUNTS))))
       finally:
           self._dc_sync_guard = False
       u = self._chan_unit()
       self._dc_slider_min_label.setText(f"{lo:g} {u}")
       self._dc_slider_max_label.setText(f"{hi:g} {u}")
       self._sync_dc_slider_to_entry()

   def _sync_dc_slider_to_entry(self):
       """Move the handle to match the typed level (no output change)."""
       try:
           v = float(self.dc_offset_entry.text())
       except (TypeError, ValueError):
           return
       lo, hi = self._dc_channel_limits()
       pos = int(round(float(np.clip(v, lo, hi)) * self._dc_slider_scale))
       if pos == self.dc_slider.value():
           return
       self._dc_sync_guard = True
       try:
           self.dc_slider.setValue(pos)
       finally:
           self._dc_sync_guard = False

   def _on_dc_text_changed(self, _text=None):
       """Typing a voltage repositions the slider as you type."""
       if self._dc_sync_guard:
           return
       self._sync_dc_slider_to_entry()

   def _on_dc_editing_finished(self):
       """Typed value committed (Enter / focus out) — clamp, store, apply."""
       if self._dc_sync_guard:
           return
       try:
           v = float(self.dc_offset_entry.text())
       except (TypeError, ValueError):
           return
       lo, hi = self._dc_channel_limits()
       self._set_dc_value(float(np.clip(v, lo, hi)))

   def _on_dc_slider_changed(self, pos):
       if self._dc_sync_guard:
           return
       self._set_dc_value(pos / self._dc_slider_scale)

   def _set_dc_value(self, value):
       """Write `value` to the entry, the slider and the channel state. Takes
       effect the next time this channel starts — see _refresh_ui_buttons.

       Formatted with %g rather than a fixed 3 decimals: 0.012 A is a normal
       level on a current output and would round to "0.012", but 20 uA of
       slider resolution would vanish entirely at 3 dp.
       """
       text = f"{value:.6g}" if value else "0"
       self._dc_sync_guard = True
       try:
           if self.dc_offset_entry.text() != text:
               self.dc_offset_entry.setText(text)
           pos = int(round(value * self._dc_slider_scale))
           if self.dc_slider.value() != pos:
               self.dc_slider.setValue(pos)
       finally:
           self._dc_sync_guard = False

       self._ch_states[self._current_ch].dc_offset = text

   def _on_sweep_toggled(self, _checked=None):
       """Sweep checkbox toggled — re-evaluate the frequency/sweep visibility."""
       self.on_waveform_select(self.waveform_type_var.currentText())

   def _sync_custom_freq_field(self):
       """For a Custom waveform in 'time (ms)' units the base cycle period is
       set by the points, so the Frequency field is locked (showing the derived
       base rate). The Multiplier stays active so the user can speed the cycle
       up (x1/x10/...) for testing."""
       s = self._ch_states[self._current_ch]
       is_time_custom = (self.waveform_type_var.currentText() == "Custom"
                         and getattr(s, 'custom_x_unit', 'cycle') == "time")
       if is_time_custom:
           pts = s.custom_points
           if pts and len(pts) >= 2:
               xsp = [float(p[0]) for p in pts]
               span_ms = max(xsp) - min(xsp)
               if span_ms > 0:
                   self.frequency_entry.blockSignals(True)
                   self.frequency_entry.setText(f"{1000.0 / span_ms:g}")
                   self.frequency_entry.blockSignals(False)
           self.frequency_entry.setEnabled(False)
           self.frequency_entry.setToolTip(
               "Base rate set from the time points (period = X span in ms).\n"
               "Use the Multiplier to speed it up."
           )
           self._multiplier_container.setEnabled(True)   # speed-up still allowed
       else:
           self.frequency_entry.setEnabled(True)
           self.frequency_entry.setToolTip("")
           self._multiplier_container.setEnabled(True)

   def _open_custom_editor(self):
       """Open the (x, y) point editor for the current channel's custom waveform."""
       s = self._ch_states[self._current_ch]
       mn, mx = self._chan_limits(self._current_ch)
       try:
           ch_name = str(self.default_settings_output['Channel'].iloc[self._current_ch])
       except Exception:
           ch_name = f"ao{self._current_ch}"

       dlg = _CustomFunctionDialog(s.custom_points,
                                   getattr(s, 'custom_x_unit', 'cycle'),
                                   mn, mx, ch_name, parent=self,
                                   y_unit=self._chan_unit())
       if dlg.exec_() == QDialog.Accepted:
           s.custom_points = dlg.get_points()
           s.custom_x_unit = dlg.get_unit()
           # Reflect time-mode locking of the Frequency field for this channel.
           self._sync_custom_freq_field()
           # If this channel is already generating, apply the new shape now.
           if s.is_running:
               try:
                   self._rebuild_task()
               except Exception as e:
                   print(f"Custom waveform apply failed: {e}")

   def _build_waveform(self, waveform_type, freq, amplitude, dc_offset,
                       min_v, max_v, ao_sample_rate, duty_cycle=50.0,
                       custom_points=None):
       if waveform_type != "DC" and freq > 0.0:
           period_samples = max(int(round(ao_sample_rate / max(freq, 1e-6))), 1)
           n_periods = max(1, int(np.ceil(2048 / period_samples)))
           buf_len = min(period_samples * n_periods, 200_000)
       else:
           # DC (or freq 0) — a constant needs no long buffer. Keeping it short
           # (~50 ms) means start/stop is responsive instead of lagging a full
           # second behind, and matches the periodic path's buffer size.
           buf_len = max(2048, min(ao_sample_rate // 20, 8192))

       t = np.arange(buf_len, dtype=np.float64) / ao_sample_rate

       if waveform_type == "Sine":
           wv = amplitude * np.sin(2 * np.pi * freq * t) + dc_offset
       elif waveform_type == "Square":
           if freq > 0.0 and period_samples > 1:
               high = max(1, min(period_samples - 1,
                                 int(round(period_samples * duty_cycle / 100.0))))
               one_period = np.empty(period_samples, dtype=np.float64)
               one_period[:high] = amplitude + dc_offset
               one_period[high:] = -amplitude + dc_offset
               wv = np.tile(one_period, n_periods)[:buf_len]
           else:
               wv = np.full(buf_len, dc_offset, dtype=np.float64)
       elif waveform_type == "Triangular":
           saw = (freq * t) - np.floor(0.5 + freq * t)
           wv = amplitude * (4 * np.abs(saw) - 1) + dc_offset
       elif waveform_type == "Sawtooth":
           dc_frac = np.clip(duty_cycle / 100.0, 1e-6, 1.0 - 1e-6)
           phase = (freq * t) % 1.0
           rise  = phase / dc_frac
           fall  = (1.0 - phase) / (1.0 - dc_frac)
           tri   = np.where(phase < dc_frac, rise, fall) * 2.0 - 1.0
           wv    = amplitude * tri + dc_offset
       elif waveform_type == "Custom":
           # User-defined points define ONE cycle, repeated at `freq`.
           # X values are normalized to [0, 1] over the cycle; Y is absolute
           # volts (interpolated linearly between points).
           xs, ys = self._normalized_custom_points(custom_points)
           if xs is not None and freq > 0.0:
               phase = (freq * t) % 1.0
               wv = np.interp(phase, xs, ys)
           elif xs is not None:
               wv = np.full(buf_len, float(ys[0]), dtype=np.float64)   # freq 0 → hold first point
           else:
               wv = np.full(buf_len, dc_offset, dtype=np.float64)      # no points → offset
       elif waveform_type in getattr(self, "_custom_funcs", {}):
           spec = self._custom_funcs[waveform_type]
           if spec["kind"] == "waveform":
               # Saved point list defines ONE cycle, repeated at `freq`; Y is
               # absolute volts (same handling as the built-in "Custom" type).
               xs, ys = self._normalized_custom_points(spec["points"])
               if xs is not None and freq > 0.0:
                   phase = (freq * t) % 1.0
                   wv = np.interp(phase, xs, ys)
               elif xs is not None:
                   wv = np.full(buf_len, float(ys[0]), dtype=np.float64)
               else:
                   wv = np.full(buf_len, dc_offset, dtype=np.float64)
           else:
               # Math function: expression in t (time) and f (frequency) defines
               # the output volts directly (Amplitude / DC offset don't apply).
               try:
                   ns = _fg_math_ns()
                   ns.update({'t': t, 'f': freq})
                   y = eval(spec["expr"].replace('^', '**'), {'__builtins__': {}}, ns)
                   y = np.asarray(y, dtype=np.float64)
                   if y.shape == ():
                       y = np.full(buf_len, float(y), dtype=np.float64)
                   if y.shape != t.shape:
                       y = np.full(buf_len, dc_offset, dtype=np.float64)
                   wv = y
               except Exception as e:
                   print(f"Math function '{waveform_type}' evaluation error: {e}")
                   wv = np.full(buf_len, dc_offset, dtype=np.float64)
       else:
           wv = np.full(buf_len, dc_offset, dtype=np.float64)

       return np.clip(wv, min_v, max_v)

   @staticmethod
   def _normalized_custom_points(custom_points):
       """Return (xs_norm, ys) sorted by x and normalized to [0, 1], or
       (None, None) if there are fewer than two usable points.

       Points that share the same x are kept (in entry order) and turned into
       an (almost) instantaneous vertical jump, so square / step shapes can be
       drawn by entering two points at the same x."""
       if not custom_points or len(custom_points) < 2:
           return None, None
       pts = np.array(custom_points, dtype=np.float64)
       xs, ys = pts[:, 0], pts[:, 1]
       order = np.argsort(xs, kind='stable')   # stable → equal-x keep entry order
       xs, ys = xs[order], ys[order]
       if xs[-1] <= xs[0]:
           return None, None
       # Normalize x to one cycle [0, 1].
       xs = (xs - xs[0]) / (xs[-1] - xs[0])
       # np.interp needs strictly increasing x; nudge any duplicate/decreasing
       # x up by a hair so coincident points become a near-vertical edge
       # instead of being collapsed.
       eps = 1e-9
       for i in range(1, len(xs)):
           if xs[i] <= xs[i - 1]:
               xs[i] = xs[i - 1] + eps
       if xs[-1] <= xs[0]:
           return None, None
       return xs, ys

   @staticmethod
   def _carrier(frac, waveform_type, duty_cycle=50.0):
       """Unit-amplitude carrier (-1..1) as a function of fractional cycle
       phase `frac` in [0,1). Used by the frequency sweep."""
       frac = np.asarray(frac, dtype=np.float64)
       if waveform_type == "Square":
           d = np.clip(duty_cycle / 100.0, 1e-6, 1.0 - 1e-6)
           return np.where(frac < d, 1.0, -1.0)
       if waveform_type == "Triangular":
           return 1.0 - 4.0 * np.abs(frac - 0.5)        # -1 at 0, +1 at 0.5
       if waveform_type == "Sawtooth":
           d = np.clip(duty_cycle / 100.0, 1e-6, 1.0 - 1e-6)
           tri = np.where(frac < d, frac / d, (1.0 - frac) / (1.0 - d))
           return tri * 2.0 - 1.0
       # default: Sine
       return np.sin(2.0 * np.pi * frac)

   def _build_sweep_waveform(self, waveform_type, f1, f2, sweep_time, log, mode,
                             amplitude, dc_offset, min_v, max_v,
                             ao_sample_rate, duty_cycle=50.0):
       """Continuous frequency sweep (chirp) of the carrier, F1→F2 over
       `sweep_time`, with a behavior mode:
         Repeat         — F1→F2 then snap back to F1 and repeat (sawtooth sweep).
         Up-Down        — F1→F2→F1 continuously (triangle sweep, no snap-back).
         Once           — sweep F1→F2 once then hold (oscillating) at F2.
         Up-Down (stop) — one F1→F2→F1 pass, played once by a finite task.
         Once (stop)    — one F1→F2 pass, played once by a finite task.

       The "(stop)" variants build exactly one pass and nothing more. Plain
       "Once" appends a long hold at F2 instead, because a continuously
       regenerating buffer cannot stop -- holding was the closest it could get.
       That hold must NOT be included when the task is finite, or "play this
       once" would play the pass and then sit on F2 for the length of the hold
       (20x the sweep time) before stopping.

       Linear or logarithmic frequency progression."""
       T  = max(float(sweep_time), 1e-3)
       f1 = max(float(f1), 1e-6)
       f2 = max(float(f2), 1e-6)
       sr = float(ao_sample_rate)
       n_up = int(min(max(round(T * sr), 2), 5_000_000))

       # Instantaneous frequency over one F1→F2 pass.
       tau = np.arange(n_up, dtype=np.float64) / n_up      # 0 .. ~1
       if log and abs(f2 - f1) > 1e-9:
           f_up = f1 * np.power(f2 / f1, tau)
       else:
           f_up = f1 + (f2 - f1) * tau

       if mode in ("Up-Down", "Up-Down (stop)"):
           f_inst = np.concatenate([f_up, f_up[::-1]])
       elif mode == "Once (stop)":
           # One pass, no hold -- the finite task provides the ending.
           f_inst = f_up
       elif mode == "Once":
           # Hold (keep oscillating) at F2 after the pass. Buffer regenerates,
           # so the hold is long but finite — it will re-sweep when it loops.
           hold_n = int(min(max(n_up * 20, int(sr * 5)), 5_000_000))
           f_inst = np.concatenate([f_up, np.full(hold_n, f2, dtype=np.float64)])
       else:  # Repeat
           f_inst = f_up

       # Cumulative phase in CYCLES (numerical integral of frequency, dt = 1/sr).
       phase = np.cumsum(f_inst) / sr
       frac = phase - np.floor(phase)
       wv = amplitude * self._carrier(frac, waveform_type, duty_cycle) + dc_offset
       return np.clip(wv, min_v, max_v)

   def _resolve_ao_sample_rate(self):
       if self._ao_sample_rate:
           return int(self._ao_sample_rate)
       try:
           base_sr = int(float(self.sample_rate))
       except Exception:
           base_sr = 10000
       return max(5000, min(100000, base_sr))

   # ------------------------------------------------------------------
   # Public start / stop  (single channel or all)
   # ------------------------------------------------------------------

   def _start_current(self):
       self._save_ui_to_channel(self._current_ch)
       self._set_channel_active(self._current_ch, True)

   def _stop_current(self):
       self._set_channel_active(self._current_ch, False)

   def _start_all(self):
       self._save_ui_to_channel(self._current_ch)
       for i in range(len(self._ch_states)):
           self._ch_states[i].is_running = True   # mark all; task built once below
       self._rebuild_task()
       self._refresh_ui_buttons()
       self._update_all_radio_labels()
       if any(s.is_running for s in self._ch_states):
           self.generation_started.emit()

   def _stop_all(self):
       self._teardown_task(zero_outputs=True)
       for s in self._ch_states:
           s.is_running = False
           s.current_voltage = 0.0
       self._refresh_ui_buttons()
       self._update_all_radio_labels()
       self.generation_stopped.emit()

   # ------------------------------------------------------------------
   # Core: mark one channel active/inactive then rebuild the shared task
   # ------------------------------------------------------------------

   def _set_channel_active(self, ch_idx: int, active: bool):
       """Toggle one channel and rebuild the shared task to match."""
       was_any_running = any(s.is_running for s in self._ch_states)

       self._ch_states[ch_idx].is_running = active
       if not active:
           self._ch_states[ch_idx].current_voltage = 0.0

       self._teardown_task(zero_outputs=not active)
       self._rebuild_task()

       self._refresh_ui_buttons()
       self._update_radio_label(ch_idx, active)

       now_any_running = any(s.is_running for s in self._ch_states)
       if not was_any_running and now_any_running:
           self.generation_started.emit()
       elif was_any_running and not now_any_running:
           self.generation_stopped.emit()

   # ------------------------------------------------------------------
   # Task lifecycle
   # ------------------------------------------------------------------

   def _add_ao_chans(self, task, indices):
       """Add AO row `indices` to `task`, each with its own type and limits.

       The type comes from the row's 'Output Type', so a current-output module
       gets a current channel and its Min/Max are read as amps.
       """
       for idx in indices:
           ch = self.default_settings_output['Channel'].iloc[idx]
           mn, mx = self._chan_limits(idx)
           add_ao_channel(task, f"{self._chan_device(idx)}/{ch}",
                          self._chan_type(idx), min_val=mn, max_val=mx)

   def _chan_type(self, idx):
       """Output type AO row `idx` names ('Voltage', 'Current'), else default."""
       try:
           return row_channel_type(self.default_settings_output.iloc[idx], 'ao')
       except Exception:
           return "Voltage"

   def _chan_unit(self, idx=None):
       """Unit AO row `idx` is commanded in — 'V' or 'A'.

       Derived from the row's output type rather than read from a 'Range Unit'
       cell, so it cannot disagree with the channel the task actually opens.
       Every field that sets a level (amplitude, DC, custom points) is labelled
       from this, because on a current output those numbers are amps.
       """
       return channel_unit(self._chan_type(self._current_ch if idx is None else idx),
                           'ao')

   def _chan_device(self, idx):
       """Device/module that AO row `idx` lives on. Prefers the row's own
       'DAQ' value (so outputs target e.g. cDAQ2Mod2 even when the inputs are
       on cDAQ2Mod1) and falls back to selected_daq for legacy files."""
       try:
           cols = getattr(self.default_settings_output, 'columns', [])
           if 'DAQ' in cols:
               v = self.default_settings_output['DAQ'].iloc[idx]
               s = "" if v is None else str(v).strip()
               if s and s.lower() != 'nan':
                   return s
       except Exception:
           pass
       return self.selected_daq

   def _timing_group(self, device_name):
       """Timing domain a device belongs to — see DAQ_Settings.timing_group.

       All AO modules in one chassis share that chassis' single AO timing
       engine, so they must go in ONE task; a standalone USB/PCIe box is its
       own group. Kept as a method so the call sites below read unchanged.
       """
       return timing_group(device_name)

   def _group_by_device(self, active, waveforms):
       """Reorder (active, waveforms) so channels are grouped by TIMING DOMAIN
       (chassis, not individual module), and return (flat_active, flat_waves,
       slices) where slices is a list of (group_key, start, count) into the
       flat lists. Order within a group, and first-seen group order, are
       preserved.

       NOTE: the group_key is the chassis/standalone-device name and is used
       only to label the task — each channel's physical string is still built
       from its own module via _chan_device(), because a single chassis AO task
       holds fully-qualified per-module channels (e.g. cDAQ2Mod2/ao0)."""
       order = {}
       for pos, idx in enumerate(active):
           key = self._timing_group(self._chan_device(idx))
           order.setdefault(key, []).append(pos)
       flat_active, flat_waves, slices = [], [], []
       for key, poslist in order.items():
           start = len(flat_active)
           for p in poslist:
               flat_active.append(active[p])
               flat_waves.append(waveforms[p])
           slices.append((key, start, len(poslist)))
       return flat_active, flat_waves, slices

   def _close_groups(self):
       """Stop and close every live AO task.

       Stopping a FINITE task that still has samples queued raises DAQmx
       warning 200010 ("stopped before the requested number of samples were
       generated"). Every stop reached from here is one we asked for -- a Stop
       press, a channel change, or teardown at close -- so for the run-once
       modes that warning is pure noise and is suppressed.

       The suppression is scoped to these two calls on purpose. A blanket
       filter would also hide a finite task ending early when nobody asked it
       to, which elsewhere means truncated data.
       """
       for g in self._ao_groups:
           try:
               with warnings.catch_warnings():
                   warnings.filterwarnings("ignore", category=DaqWarning)
                   g["task"].stop()
           except Exception:
               pass
           try:
               g["task"].close()
           except Exception:
               pass
       self._ao_groups = []
       self._ao_task = None
       self._ao_writer = None

   def _teardown_task(self, zero_outputs: bool = True):
       """Stop and close every per-device AO task, optionally writing 0 first."""
       # Stop tracking timers
       for attr in ('_hw_track_timer', '_sw_timer'):
           t = getattr(self, attr, None)
           if t is not None:
               try:
                   t.stop()
                   t.deleteLater()
               except Exception:
                   pass
               setattr(self, attr, None)

       if not self._ao_groups:
           self._ao_writer = None
           self._active_indices = []
           return

       active_snapshot = list(self._active_indices)
       try:
           # Stop and close all live tasks first (a hardware-timed task can't
           # switch to on-demand to write zeros, so we close then re-open).
           self._close_groups()

           if zero_outputs and active_snapshot:
               # Group the channels by device and write 0 V to each device's
               # outputs via a fresh on-demand task.
               by_dev = {}
               for idx in active_snapshot:
                   by_dev.setdefault(self._chan_device(idx), []).append(idx)
               for dev, idxs in by_dev.items():
                   try:
                       with nidaqmx.Task() as zt:
                           self._add_ao_chans(zt, idxs)
                           if len(idxs) == 1:
                               AnalogSingleChannelWriter(zt.out_stream).write_one_sample(0.0)
                           else:
                               zeros = np.zeros(len(idxs), dtype=np.float64)
                               AnalogMultiChannelWriter(zt.out_stream).write_one_sample(zeros)
                   except Exception as e:
                       print(f"Function generator: could not zero outputs on {dev}: {e}")
       except Exception as e:
           print(f"Function generator teardown error: {e}")
       finally:
           self._ao_groups    = []
           self._ao_task      = None
           self._ao_writer    = None
           self._sw_waveforms = []
           self._hw_waveforms = []
           self._sw_idx       = 0
           self._sw_pass_len  = 0
           self._burst_active = False
           with self._burst_lock:
               self._burst_pending = 0
           self._active_indices = []

   def _channel_stops(self, state):
       """True if this channel's sweep mode asks for a single pass."""
       return (bool(getattr(state, 'sweep_enabled', False))
               and getattr(state, 'sweep_mode', '') in self._SWEEP_STOP_MODES
               and getattr(state, 'waveform_type', '') in
               ("Sine", "Square", "Triangular", "Sawtooth"))

   def _burst_stop(self):
       """Stop everything after a run-once pass completes."""
       if not self._burst_active:
           return
       self._burst_active = False
       # A finite task holds its last sample when it ends, which for a sweep is
       # wherever the waveform happened to stop -- not 0 V. Zero explicitly so
       # the channel is left de-commanded rather than parked at some arbitrary
       # mid-waveform level.
       self._teardown_task(zero_outputs=True)
       for st in self._ch_states:
           st.is_running = False
           st.current_voltage = 0.0
       self._refresh_ui_buttons()
       self._update_all_radio_labels()
       self.generation_stopped.emit()

   def _on_burst_finished(self):
       """UI-thread handler for the done event / software-timed pass end."""
       self._burst_stop()

   def _rebuild_task(self):
       """Build a new shared task for all channels currently marked is_running."""
       active = [i for i, s in enumerate(self._ch_states) if s.is_running]
       if not active:
           return

       # A device gets ONE task, so run-once is all-or-nothing per task: if any
       # active channel asks to stop, the whole task is finite and every
       # channel on it ends together. Erring toward finite rather than
       # continuous keeps the explicit request ("(stop)") from being silently
       # ignored because some other channel happened to be free-running.
       self._burst_armed = any(self._channel_stops(self._ch_states[i])
                               for i in active)

       ao_sample_rate = self._resolve_ao_sample_rate()

       # Build per-channel waveforms (may have different lengths — pad to match)
       raw_waveforms = []
       for idx in active:
           s = self._ch_states[idx]
           try:
               freq       = max(0.0, float(s.frequency) * (s.multiplier or 1))
               wt         = s.waveform_type
               dc_offset  = float(s.dc_offset)
               amplitude  = float(s.amplitude) if wt != "DC" else 0.0
               duty_cycle = float(np.clip(float(s.duty_cycle), 1.0, 99.0)) \
                            if wt in ("Square", "Sawtooth") else 50.0
               mn, mx = self._chan_limits(idx)
               # Custom waveform in "time (ms)" mode: the entered points define
               # the cycle DURATION directly, so derive the repeat rate from
               # their span and ignore the Frequency field.
               if wt == "Custom" and getattr(s, 'custom_x_unit', 'cycle') == "time" \
                       and s.custom_points and len(s.custom_points) >= 2:
                   xspan = [float(p[0]) for p in s.custom_points]
                   span_ms = max(xspan) - min(xspan)
                   if span_ms > 0:
                       freq = (1000.0 / span_ms) * (s.multiplier or 1)
           except Exception as e:
               print(f"Channel {idx}: invalid settings, defaulting to 0 V DC: {e}")
               mn, mx = 0.0, 5.0
               wt, freq, amplitude, dc_offset, duty_cycle = "DC", 0.0, 0.0, 0.0, 50.0

           if (getattr(s, 'sweep_enabled', False)
                   and wt in ("Sine", "Square", "Triangular", "Sawtooth")):
               try:
                   f1 = float(s.sweep_f1); f2 = float(s.sweep_f2)
                   stime = float(s.sweep_time)
               except (TypeError, ValueError):
                   f1, f2, stime = 1.0, 100.0, 1.0
               wv = self._build_sweep_waveform(
                   wt, f1, f2, stime, bool(s.sweep_log), s.sweep_mode,
                   amplitude, dc_offset, mn, mx, ao_sample_rate, duty_cycle)
           else:
               wv = self._build_waveform(wt, freq, amplitude, dc_offset,
                                         mn, mx, ao_sample_rate, duty_cycle,
                                         custom_points=s.custom_points)
           raw_waveforms.append(wv)

       # All channels must share the same buffer length in a single task.
       # Pad shorter waveforms by tiling (preserves periodicity perfectly);
       # tiling also truncates anything longer than the target.
       if self._burst_armed:
           # The pass is defined by the channels that asked to stop. Using the
           # longest channel instead would let a slow free-running channel
           # stretch the buffer and make the sweep repeat several times before
           # the task ended -- the opposite of "one pass".
           max_len = max(len(w) for w, idx in zip(raw_waveforms, active)
                         if self._channel_stops(self._ch_states[idx]))
       else:
           max_len = max(len(w) for w in raw_waveforms)
       padded = []
       for wv in raw_waveforms:
           if len(wv) == max_len:
               padded.append(wv)
           else:
               reps = int(np.ceil(max_len / len(wv)))
               padded.append(np.tile(wv, reps)[:max_len])

       # Detect whether ALL active devices support hardware-timed AO. If any
       # one doesn't, fall back to the software-timed path for everything.
       hw_timed = True
       try:
           for dev in {self._chan_device(idx) for idx in active}:
               try:
                   if not bool(nidaqmx.system.Device(dev).ao_samp_clk_supported):
                       hw_timed = False
                       break
               except Exception:
                   pass  # assume hw-capable if the query fails
       except Exception:
           hw_timed = True

       if hw_timed:
           try:
               self._rebuild_hw(active, padded, ao_sample_rate, max_len)
               return
           except _HwTimingUnavailable as e:
               # The capability query passed but the driver refused. Fall
               # through rather than leaving the user with nothing.
               print(f"Function generator: hardware-timed AO unavailable "
                     f"({e}); falling back to software-timed.")
       self._rebuild_sw(active, padded, ao_sample_rate)

   def _rebuild_hw(self, active: list[int], waveforms: list[np.ndarray],
                   ao_sample_rate: int, buf_len: int):
       """Start one hardware-timed task per device for the given channels."""
       flat_active, flat_waves, slices = self._group_by_device(active, waveforms)
       with self._burst_lock:
           self._burst_pending = len(slices) if self._burst_armed else 0
       built = []
       task = None
       try:
           for grp_key, start, count in slices:
               task = nidaqmx.Task()
               self._add_ao_chans(task, flat_active[start:start + count])

               burst = bool(self._burst_armed)
               task.timing.cfg_samp_clk_timing(
                   rate=ao_sample_rate,
                   sample_mode=(ni.AcquisitionType.FINITE if burst
                                else ni.AcquisitionType.CONTINUOUS),
                   samps_per_chan=buf_len
               )
               task.out_stream.regen_mode = (
                   ni.RegenerationMode.DONT_ALLOW_REGENERATION if burst
                   else ni.RegenerationMode.ALLOW_REGENERATION)

               if burst:
                   # Runs on a DAQmx driver thread. Emitting a Qt signal is the
                   # only safe thing to do here -- touching tasks or widgets
                   # from this thread is what makes done-event handlers crash.
                   # The signal is emitted only once every device has finished.
                   def _done_cb(task_handle, status, callback_data):
                       with self._burst_lock:
                           if self._burst_pending <= 0:
                               # Already torn down (manual Stop, or every
                               # device already reported). Without this a late
                               # callback would drive the counter negative and
                               # emit a second, spurious completion.
                               return 0
                           self._burst_pending -= 1
                           last = self._burst_pending == 0
                       if last:
                           self.burst_finished.emit()
                       return 0
                   task.register_done_event(_done_cb)

               if count == 1:
                   writer = AnalogSingleChannelWriter(task.out_stream)
                   writer.write_many_sample(flat_waves[start])
               else:
                   writer = AnalogMultiChannelWriter(task.out_stream)
                   writer.write_many_sample(np.vstack(flat_waves[start:start + count]))

               task.start()
               built.append({"task": task, "writer": writer, "start": start,
                             "count": count, "single": count == 1, "daq": grp_key})
               task = None

           self._ao_groups         = built
           self._ao_task           = built[0]["task"] if built else None
           self._ao_writer         = built[0]["writer"] if built else None
           self._sw_mode           = False
           self._active_indices    = flat_active
           self._hw_waveforms      = flat_waves
           self._hw_start_time     = time.monotonic()
           self._hw_ao_sample_rate = ao_sample_rate
           self._burst_active      = bool(self._burst_armed)

           self._hw_track_timer = QTimer(self)
           self._hw_track_timer.setInterval(50)
           self._hw_track_timer.timeout.connect(self._hw_update_voltages)
           self._hw_track_timer.start()

           ch_labels = ", ".join(
               f"{self._chan_device(i)}/{self.default_settings_output['Channel'].iloc[i]}"
               for i in flat_active
           )
           print(f"Function generator started (hardware-timed, "
                 f"{ao_sample_rate:g} S/s) — {ch_labels}.")
       except Exception as e:
           # Release everything this attempt reserved before deciding what next:
           # a half-built AO task keeps the timing engine and would make the
           # software fallback fail too.
           if task is not None:
               try:
                   task.close()
               except Exception:
                   pass
           for g in built:
               try:
                   g["task"].close()
               except Exception:
                   pass
           self._ao_groups = []
           if isinstance(e, DaqError):
               # A refusal, not a bug — let the caller try the software path.
               # Channel run flags stay set; that attempt owns them now.
               raise _HwTimingUnavailable(str(e).strip().splitlines()[0]) from e
           print(f"Function generator: failed to start AO task: {e}")
           # Roll back running flags for all channels we tried to start
           for idx in active:
               self._ch_states[idx].is_running = False
           self._active_indices = []

   def _rebuild_sw(self, active: list[int], waveforms: list[np.ndarray],
                   ao_sample_rate: int):
       """Start a software-timed (on-demand) task for the given channels."""
       sw_rate   = min(SW_RATE, ao_sample_rate)
       SW_PERIOD = max(1, int(1000 / sw_rate))

       resampled = []
       for wv in waveforms:
           if ao_sample_rate != sw_rate and len(wv) > 1:
               indices = np.linspace(0, len(wv) - 1,
                                     max(int(len(wv) * sw_rate / ao_sample_rate), 2))
               wv = wv[np.round(indices).astype(int)]
           resampled.append(wv)

       flat_active, flat_waves, slices = self._group_by_device(active, resampled)
       built = []
       task = None
       try:
           for grp_key, start, count in slices:
               task = nidaqmx.Task()
               self._add_ao_chans(task, flat_active[start:start + count])
               writer = (AnalogSingleChannelWriter(task.out_stream) if count == 1
                         else AnalogMultiChannelWriter(task.out_stream))
               task.start()
               built.append({"task": task, "writer": writer, "start": start,
                             "count": count, "single": count == 1, "daq": grp_key})
               task = None

           self._ao_groups      = built
           self._ao_task        = built[0]["task"] if built else None
           self._ao_writer      = built[0]["writer"] if built else None
           self._sw_mode        = True
           self._active_indices = flat_active
           self._sw_waveforms   = flat_waves
           self._sw_idx         = 0
           # One pass == the longest resampled channel. Shorter channels wrap
           # inside it, matching the hardware path where every channel shares
           # one buffer length.
           self._sw_pass_len    = max((len(w) for w in flat_waves), default=0)
           self._burst_active   = bool(self._burst_armed)

           self._sw_timer = QTimer(self)
           self._sw_timer.setInterval(SW_PERIOD)
           self._sw_timer.timeout.connect(self._sw_write_sample)
           self._sw_timer.start()

           ch_labels = ", ".join(
               f"{self._chan_device(i)}/{self.default_settings_output['Channel'].iloc[i]}"
               for i in flat_active
           )
           # Worth stating the ceiling, not just the rate: above
           # sw_rate / SW_MIN_PTS the output is no longer the requested shape.
           print(f"Function generator started (software-timed, ~{sw_rate:g} S/s, "
                 f"faithful to about {sw_rate / SW_MIN_PTS:g} Hz) — {ch_labels}.")
       except Exception as e:
           print(f"Function generator: failed to start AO task (software-timed): {e}")
           if task is not None:
               try:
                   task.close()
               except Exception:
                   pass
           for g in built:
               try:
                   g["task"].close()
               except Exception:
                   pass
           self._ao_groups = []
           for idx in active:
               self._ch_states[idx].is_running = False
           self._active_indices = []

   # ------------------------------------------------------------------
   # Timer callbacks
   # ------------------------------------------------------------------

   def _hw_update_voltages(self):
       """Periodic callback — keep current_voltage in sync with the hw waveform."""
       elapsed = time.monotonic() - self._hw_start_time
       for list_pos, ch_idx in enumerate(self._active_indices):
           wv = self._hw_waveforms[list_pos]
           if wv is None or len(wv) == 0:
               continue
           raw = int(elapsed * self._hw_ao_sample_rate)
           # A finite (run-once) buffer holds its last sample when it ends;
           # a continuous one regenerates from the start.
           pos = min(raw, len(wv) - 1) if self._burst_active else raw % len(wv)
           self._ch_states[ch_idx].current_voltage = float(wv[pos])

   def _sw_write_sample(self):
       """Periodic callback — write one sample per active channel, to each
       device's on-demand task separately."""
       if not self._ao_groups:
           return
       try:
           for g in self._ao_groups:
               start, count = g["start"], g["count"]
               if g["single"]:
                   wv = self._sw_waveforms[start]
                   v  = float(wv[self._sw_idx % len(wv)])
                   g["writer"].write_one_sample(v)
                   self._ch_states[self._active_indices[start]].current_voltage = v
               else:
                   vals = np.empty(count, dtype=np.float64)
                   for k in range(count):
                       wv = self._sw_waveforms[start + k]
                       vals[k] = float(wv[self._sw_idx % len(wv)])
                   g["writer"].write_one_sample(vals)
                   for k in range(count):
                       self._ch_states[self._active_indices[start + k]].current_voltage = float(vals[k])
           self._sw_idx += 1
           # Run-once: the pass ends when the longest channel has played out.
           # Checked AFTER the write so the final sample is actually driven.
           if self._burst_active and self._sw_idx >= self._sw_pass_len:
               self._sw_timer.stop()
               self.burst_finished.emit()
       except Exception as e:
           print(f"Function generator: software-timed write error: {e}")
           self._teardown_task(zero_outputs=False)
           for s in self._ch_states:
               s.is_running = False
               s.current_voltage = 0.0
           self._refresh_ui_buttons()
           self._update_all_radio_labels()
           self.generation_stopped.emit()

   # ------------------------------------------------------------------
   # Radio label helpers
   # ------------------------------------------------------------------

   def _ch_display_name(self, idx):
       """Friendly name for an AO channel radio button: the channel's Device
       Name when set, otherwise the physical channel id (e.g. 'ao0')."""
       df = self.default_settings_output
       try:
           nm = df['Device Name'].iloc[idx]
           s = "" if nm is None else str(nm).strip()
           if s and s.lower() != 'nan':
               return s
       except Exception:
           pass
       try:
           return str(df['Channel'].iloc[idx])
       except Exception:
           return f"ao{idx}"

   def _load_custom_functions(self):
       """Read DEFINED FG custom functions from the settings file.

       Returns an ordered dict {name: spec}. Only entries that have a name AND
       usable content are included, so blank/unsaved slots never appear in the
       dropdown. spec is {"kind": "waveform", "points": [...], "x_unit": str}
       or {"kind": "math", "expr": str}."""
       funcs = {}
       try:
           import os
           import pandas as pd
           path = _fg_settings_path()
           if not os.path.isfile(path):
               return funcs
           xls = pd.ExcelFile(path)
           if "app_fg_functions" not in xls.sheet_names:
               return funcs
           df = pd.read_excel(xls, sheet_name="app_fg_functions").fillna("")
           for _, row in df.iterrows():
               name = str(row.get("Name", "")).strip()
               if not name or name in funcs:
                   continue
               kind = str(row.get("Type", "")).strip().lower()
               if kind == "waveform":
                   pts = _fg_parse_points(str(row.get("Points", "")))
                   if pts:
                       funcs[name] = {"kind": "waveform", "points": pts,
                                      "x_unit": str(row.get("X Unit", "")).strip()}
               elif kind == "math":
                   expr = str(row.get("Expression", "")).strip()
                   if expr:
                       funcs[name] = {"kind": "math", "expr": expr}
       except Exception as e:
           print(f"Function generator: could not load custom functions: {e}")
       return funcs

   def _reload_custom_functions_into_combo(self):
       """Re-read custom functions and refresh the dropdown, preserving the
       current selection when still available. Called when the dialog is shown
       so functions saved in Settings appear without restarting."""
       if not hasattr(self, "waveform_type_var"):
           return
       new = self._load_custom_functions()
       if new == self._custom_funcs:
           return
       self._custom_funcs = new
       cur = self.waveform_type_var.currentText()
       self.waveform_type_var.blockSignals(True)
       self.waveform_type_var.clear()
       self.waveform_type_var.addItems(self._BASE_WAVEFORMS + list(new.keys()))
       idx = self.waveform_type_var.findText(cur)
       self.waveform_type_var.setCurrentIndex(idx if idx >= 0 else 0)
       self.waveform_type_var.blockSignals(False)

   def showEvent(self, event):
       super().showEvent(event)
       self._reload_custom_functions_into_combo()

   def _update_radio_label(self, ch_idx: int, running: bool):
       if ch_idx >= len(self._ch_radios):
           return
       self._ch_radios[ch_idx].setText(("▶ " if running else "") + self._ch_display_name(ch_idx))

   def _update_all_radio_labels(self):
       for i, s in enumerate(self._ch_states):
           self._update_radio_label(i, s.is_running)

   # ------------------------------------------------------------------
   # Public-facing properties (backwards compatible with main app)
   # ------------------------------------------------------------------

   def lock_for_ct(self, locked: bool):
       """Called by CustomTriggers to take/release ownership of generation."""
       self._ct_locked = locked
       tip = "Custom Triggers controls generation" if locked else ""
       self._refresh_ui_buttons()
       self.start_button.setToolTip(tip)
       self.stop_button.setToolTip(tip)

   @property
   def is_running(self) -> bool:
       return any(s.is_running for s in self._ch_states)

   @property
   def current_voltage(self) -> float:
       """Voltage of the first running channel (legacy ring-buffer compat)."""
       for s in self._ch_states:
           if s.is_running:
               return s.current_voltage
       return 0.0

   def safe_close_ao(self):
       self._teardown_task(zero_outputs=True)
       for s in self._ch_states:
           s.is_running = False
           s.current_voltage = 0.0

   # ------------------------------------------------------------------
   # Close
   # ------------------------------------------------------------------

   def closeEvent(self, event):
       if self.parent() is None:
           self.safe_close_ao()
           QApplication.instance().quit()
           event.accept()
       else:
           self.hide()
           event.ignore()


# ---------------------------------------------------------------------------
# Custom-function point editor
# ---------------------------------------------------------------------------

class _CustomFunctionDialog(QDialog):
    """Editor for a custom periodic waveform defined by (x, y) points.

    X is the position within one cycle, entered in the chosen unit (cycle /
    % / degree / time-ms); Y is the output voltage. A live preview shows the
    interpolated cycle. Save returns the cleaned point list and the unit.
    """

    _DEFAULT = [(0.0, 0.0), (0.25, 2.5), (0.5, 5.0), (0.75, 2.5), (1.0, 0.0)]

    # unit key -> (axis label, hard X max for zoom; None = unbounded)
    _X_UNITS = {
        "cycle":  ("Cycle (0–1)",      1.0),
        "%":      ("Percent (0–100)",  100.0),
        "degree": ("Degrees (0–360)",  360.0),
        "time":   ("Time (ms)",        None),
    }

    def __init__(self, points, x_unit, min_v, max_v, ch_name, parent=None,
                 y_unit="V"):
        super().__init__(parent)
        self.setWindowTitle(f"Custom Function — {ch_name}")
        self._min_v = float(min_v)
        self._max_v = float(max_v)
        # The channel's own unit: the points are absolute output levels, so on a
        # current channel this column is amps, and labelling it volts would
        # invite values three orders of magnitude out.
        self._y_unit = str(y_unit or "V")
        self.resize(660, 400)

        pts = list(points) if points and len(points) >= 2 else list(self._DEFAULT)
        self._points = [(float(x), float(y)) for x, y in pts]

        outer   = QVBoxLayout(self)
        content = QHBoxLayout()
        outer.addLayout(content)

        # ── Left: unit selector + editable points table ──────────────────
        left = QVBoxLayout()

        unit_row = QHBoxLayout()
        unit_row.addWidget(QLabel("X unit:"))
        self.unit_combo = QComboBox()
        self.unit_combo.addItems(list(self._X_UNITS.keys()))
        if x_unit in self._X_UNITS:
            self.unit_combo.setCurrentText(x_unit)
        self.unit_combo.currentTextChanged.connect(lambda *_: self._on_unit_changed())
        unit_row.addWidget(self.unit_combo)
        unit_row.addStretch(1)
        left.addLayout(unit_row)

        left.addWidget(QLabel(
            "Points define one cycle. Two points at the same X make an instant\n"
            "jump (e.g. a square). For cycle/%/degree the cycle repeats at the\n"
            "Frequency setting; for time (ms) the points' span sets the period."
        ))
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["X", f"Y ({self._y_unit})"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setMinimumWidth(230)
        left.addWidget(self.table)

        row_btns = QHBoxLayout()
        add_btn = QPushButton("Add Row")
        del_btn = QPushButton("Remove Row")
        add_btn.clicked.connect(self._add_row)
        del_btn.clicked.connect(self._remove_row)
        row_btns.addWidget(add_btn)
        row_btns.addWidget(del_btn)
        left.addLayout(row_btns)

        rng = QLabel(f"Output is clipped to the channel range: "
                     f"{self._min_v:g} … {self._max_v:g} {self._y_unit}")
        rng.setStyleSheet("color: #666;")
        rng.setWordWrap(True)
        left.addWidget(rng)
        content.addLayout(left)

        # ── Right: live preview (pyqtgraph, light theme) ─────────────────
        self._pg = None
        try:
            import pyqtgraph as pg
            self._pg = pg
            self._plot = pg.PlotWidget()
            self._plot.setBackground('w')                 # light background
            self._plot.setLabel('left', self._y_unit)
            self._plot.showGrid(x=True, y=True, alpha=0.25)
            for ax_name in ('left', 'bottom'):
                ax = self._plot.getAxis(ax_name)
                ax.setPen('#444444')
                try:
                    ax.setTextPen('#444444')              # newer pyqtgraph
                except Exception:
                    pass
            self._plot.addLine(y=self._min_v, pen=pg.mkPen('#888', style=Qt.DashLine))
            self._plot.addLine(y=self._max_v, pen=pg.mkPen('#888', style=Qt.DashLine))
            self._curve = self._plot.plot([], [], pen=pg.mkPen('#2980b9', width=2))
            self._marks = self._plot.plot([], [], pen=None, symbol='o',
                                          symbolBrush='#e74c3c', symbolSize=7)
            content.addWidget(self._plot, 1)
        except Exception:
            self._pg = None
            content.addWidget(QLabel("Preview unavailable\n(pyqtgraph not found)"), 1)

        # ── Bottom: Save / Cancel ─────────────────────────────────────────
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._on_save)
        bb.rejected.connect(self.reject)
        outer.addWidget(bb)

        self._populate(self._points)
        self.table.itemChanged.connect(lambda *_: self._refresh_plot())
        self._apply_axis_limits()
        self._refresh_plot()

    # -- axis / unit ----------------------------------------------------
    def get_unit(self):
        return self.unit_combo.currentText()

    def _apply_axis_limits(self):
        """Set the X-axis label and clamp zoom: Y to the channel voltage range,
        X to the unit's max (cycle 1 / % 100 / degree 360); time is unbounded."""
        unit = self.unit_combo.currentText()
        label, xmax = self._X_UNITS.get(unit, ("X", None))
        self.table.setHorizontalHeaderLabels([f"X ({unit})",
                                              f"Y ({self._y_unit})"])
        if self._pg is None:
            return
        self._plot.setLabel('bottom', label)
        vb = self._plot.getViewBox()
        self._plot.setYRange(self._min_v, self._max_v, padding=0.05)
        if xmax is not None:
            self._plot.setXRange(0.0, xmax, padding=0.02)
            vb.setLimits(xMin=0.0, xMax=xmax, yMin=self._min_v, yMax=self._max_v)
        else:
            vb.setLimits(xMin=None, xMax=None, yMin=self._min_v, yMax=self._max_v)
            self._plot.enableAutoRange(axis='x')

    def _on_unit_changed(self):
        self._apply_axis_limits()
        self._refresh_plot()

    # -- table helpers --------------------------------------------------
    def _append_row(self, x=0.0, y=0.0):
        r = self.table.rowCount()
        self.table.insertRow(r)
        self.table.setItem(r, 0, QTableWidgetItem(f"{x:g}"))
        self.table.setItem(r, 1, QTableWidgetItem(f"{y:g}"))

    def _populate(self, pts):
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        for x, y in pts:
            self._append_row(x, y)
        self.table.blockSignals(False)

    def _add_row(self):
        n = self.table.rowCount()
        r = self.table.currentRow()
        if r < 0:
            r = n - 1                      # nothing selected → after the last row
        insert_at = r + 1

        def xval(i):
            it = self.table.item(i, 0) if 0 <= i < self.table.rowCount() else None
            try:
                return float(it.text())
            except (ValueError, AttributeError):
                return None

        x_sel  = xval(r)
        x_next = xval(insert_at)
        if x_sel is not None and x_next is not None:
            nx = (x_sel + x_next) / 2.0    # land between the two neighbours
        elif x_sel is not None:
            nx = x_sel + 0.1
        else:
            nx = 0.0

        self.table.blockSignals(True)
        self.table.insertRow(insert_at)
        self.table.setItem(insert_at, 0, QTableWidgetItem(f"{nx:g}"))
        self.table.setItem(insert_at, 1, QTableWidgetItem("0"))
        self.table.blockSignals(False)
        self.table.selectRow(insert_at)
        self._refresh_plot()

    def _remove_row(self):
        r = self.table.currentRow()
        if r < 0:
            r = self.table.rowCount() - 1
        if r >= 0:
            self.table.removeRow(r)
        self._refresh_plot()

    def _read_points(self):
        pts = []
        for r in range(self.table.rowCount()):
            xi = self.table.item(r, 0)
            yi = self.table.item(r, 1)
            if xi is None or yi is None:
                continue
            try:
                pts.append((float(xi.text()), float(yi.text())))
            except (ValueError, AttributeError):
                continue
        return pts

    # -- preview --------------------------------------------------------
    def _refresh_plot(self):
        if self._pg is None:
            return
        pts = self._read_points()
        if len(pts) < 2:
            self._curve.setData([], [])
            self._marks.setData([], [])
            return
        arr = np.array(pts, dtype=np.float64)
        arr = arr[np.argsort(arr[:, 0], kind='stable')]   # equal-x keep entry order
        xr, yr = arr[:, 0].copy(), arr[:, 1]
        if xr[-1] <= xr[0]:
            self._curve.setData([], [])
            self._marks.setData([], [])
            return
        # Nudge coincident X so interpolation renders a vertical edge.
        eps = max(xr[-1] - xr[0], 1.0) * 1e-9
        for i in range(1, len(xr)):
            if xr[i] <= xr[i - 1]:
                xr[i] = xr[i - 1] + eps
        xi = np.linspace(xr[0], xr[-1], 400)
        yi = np.clip(np.interp(xi, xr, yr), self._min_v, self._max_v)
        self._curve.setData(xi, yi)
        self._marks.setData(arr[:, 0], np.clip(yr, self._min_v, self._max_v))

    # -- save -----------------------------------------------------------
    def _on_save(self):
        pts = self._read_points()
        xs, _ = FunctionGeneratorDialog._normalized_custom_points(pts)
        if xs is None:
            QMessageBox.warning(
                self, "Invalid Points",
                "Enter at least two points spanning a range of X "
                "(points may share an X to make an instant jump)."
            )
            return
        # For bounded units (cycle/%/degree) the points define ONE cycle, so X
        # should stay within 0..max. Out-of-range values still work (they get
        # normalized into the single cycle) but won't all be visible on the
        # preview, so confirm before saving.
        unit = self.unit_combo.currentText()
        _, xmax = self._X_UNITS.get(unit, (None, None))
        if xmax is not None:
            raw = [p[0] for p in pts]
            if max(raw) > xmax + 1e-9 or min(raw) < -1e-9:
                resp = QMessageBox.question(
                    self, "X outside one cycle",
                    f"These points define ONE cycle, so X should be within "
                    f"0–{xmax:g} ({unit}).\n\nSome X values are outside that range. "
                    f"They'll be squeezed into the single cycle and may sit off the "
                    f"preview.\n\nSave anyway?",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
                if resp != QMessageBox.Yes:
                    return
        arr = np.array(pts, dtype=np.float64)
        order = np.argsort(arr[:, 0], kind='stable')   # keep equal-x entry order
        arr = arr[order]
        self._points = [(float(x), float(y)) for x, y in arr]
        self.accept()

    def get_points(self):
        return self._points


if __name__ == "__main__":
   import pandas as pd

   dummy_output = pd.DataFrame([
       {'Channel': 'ao0', 'Min': 0.0, 'Max': 5.0},
       {'Channel': 'ao1', 'Min': 0.0, 'Max': 5.0},
   ])

   dlg = FunctionGeneratorDialog(
       selected_daq='Dev1',
       default_settings_output=dummy_output,
       sample_rate=10000,
   )
   dlg.show()
   sys.exit(_standalone_app.exec_())


# ---------------------------------------------------------------------------
# Apps-bar registration (see DAQ_AppRegistry.py)
# ---------------------------------------------------------------------------

def build(ctx, parent):
    """Positional signature, unchanged — kept here rather than in the engine."""
    return FunctionGeneratorDialog(
        ctx.selected_daq, ctx.output_settings, ctx.sample_rate,
        ao_sample_rate=ctx.ao_sample_rate, parent=parent,
    )


APP_SPEC = {
    'id':    'function_generator',
    'label': 'Function Generator',
    'cls':   'FunctionGeneratorDialog',
    'build': build,
    'requires_daq_hw': True,
    # Deliberately NOT requires_daq_stopped: generating while acquiring is a
    # normal way to use this, and the original code left its button live during
    # a run.
    'requires_daq_stopped': False,
    'claims': {'ao'},
}
