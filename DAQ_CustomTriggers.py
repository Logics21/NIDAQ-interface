
"""
DAQ_CustomTriggers.py
---------------------
Custom Triggers dialog for GioDAQ.

Flow when user clicks "Start Event"
────────────────────────────────────
1. Validate config + save filename (entered in this dialog).
2. Call mw.start_task() — starts DAQ hardware.
3. Wait for mw.total_written > 0 before starting trigger checks
   (avoids false-firing while hardware spins up).
4. Once trigger fires → execute Added Events.
   - If a "Start Recording" event exists it calls mw.start_recording()
     (with optional pre-trigger data injection).
   - If no "Start Recording" event exists, recording is NOT started.
5. Once stop condition → call mw.stop_task().
   Duration stop uses a local QTimer.  Level/edge stops are polled.

Hysteresis
──────────
An optional hysteresis band is applied to all analog threshold
comparisons (Rising/Falling/Above/Below).  For rising/above the
signal must exceed (threshold + hysteresis); for falling/below it
must go below (threshold - hysteresis).

Locking
───────
While dialog is open:
  - Run button in main window is disabled.
  - Function Generator button is disabled.
  - Flow Failure button is disabled.
Filename field in main window is NOT locked (we have our own field here).

Autosave and Hold:
  With Hold off, "File Autosave" saves the recording to this dialog's own file.
  With Hold on, a recording becomes a held run instead, and the checkbox here
  becomes "Session Autosave", mirroring Session Settings -> Session Autosave
  (which saves the whole session to its .mcz): ticking one ticks the other.
  When Hold goes off again this dialog's own File Autosave choice and filename
  come back.

Re-Start Event:
  With a held run's tab shown in the main window, Start Event becomes
  Re-Start Event: the event's recording replaces that run (after asking).
  Switch to Live Graph to add a new run instead.

Trigger and stop conditions:
  Each is [Source | Event | Value]. Sources are the channels plotted in the
  main window (Y or X axis), the digital inputs, and Time (trigger: elapsed
  since Start) or Duration (stop: time since the trigger). Conditions look
  only at samples that arrive after Start (the stop: after the trigger), so
  data already on the plot never fires them.

  Events: Rising/Falling Edge, Above/Below Level, and Enter/Leave/Inside/
  Outside Window (a low and a high value). An edge needs the opposite state
  first — a Rising Edge counts only after the signal was at or below the
  level — so a signal already past the level at Start does not fire it.
  Digital inputs are read each poll (100 ms): Rising/Falling Edge.

Added Events: + Add Event offers the events still available. Wait is an
  event that pauses before the events below it fire.

Stop Actions: run all at once (Waits are skipped), in their own order, which
  their arrows change. Stop Recording has a post-trigger time (0-3000 ms):
  the recording, and the acquisition, carry on that long after the stop.

Auto Stop:
  This app owns when a recording stops (APP_SPEC 'owns_recording_length'), so
  the main window unticks and greys Auto Stop while it is open.
"""

import os
import sys

import numpy as np
import pandas as pd

from PyQt5.QtWidgets import (
    QDialog, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QComboBox, QPushButton, QGroupBox,
    QSizePolicy, QMessageBox, QFrame, QCheckBox, QMenu,
)
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QFont, QDoubleValidator

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ANALOG_EVENTS   = ["Rising Edge", "Falling Edge", "Above Level", "Below Level", "Enter Window", "Leave Window", "Inside Window", "Outside Window"]
_DIGITAL_EVENTS  = ["Rising Edge", "Falling Edge"]


def _resolve_fg_channel_idx(fg, display: str) -> int:
    """Find the AO channel index in fg.default_settings_output that matches
    *display*, which may be either the friendly Device Name or the raw Channel
    string (e.g. 'ao0').  Falls back to parsing a trailing digit."""
    if hasattr(fg, 'default_settings_output'):
        df = fg.default_settings_output
        # Try Device Name first
        if 'Device Name' in df.columns:
            for i in range(len(df)):
                if str(df['Device Name'].iloc[i]).strip() == display:
                    return i
        # Try Channel string
        if 'Channel' in df.columns:
            for i in range(len(df)):
                if str(df['Channel'].iloc[i]).strip() == display:
                    return i
    # Last resort: parse trailing integer
    import re as _re
    m = _re.search(r'([0-9]+)$', display)
    return int(m.group(1)) if m else 0
_STOP_EVENTS     = _ANALOG_EVENTS          # the stop offers the same events
_WINDOW_EVENTS   = ("Enter Window", "Leave Window", "Inside Window", "Outside Window")
_POST_TRIGGER_MAX_MS = 3000                 # Stop Recording post-trigger limit

_SOURCE_TIME     = "Time"        # trigger source: elapsed time since Start
_SOURCE_DURATION = "Duration"    # stop source: time since the trigger
_EVT_START_REC = "Start Recording"   # sentinel for the recording-start event row
_EVT_WAIT      = "Wait"              # pause before the events below it fire


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hline():
    line = QFrame()
    line.setFrameShape(QFrame.HLine)
    line.setFrameShadow(QFrame.Sunken)
    return line


def _unit_field(placeholder="", width=110):
    """Return (container QWidget, QLineEdit, units QLabel)."""
    w = QWidget()
    h = QHBoxLayout(w)
    h.setContentsMargins(0, 0, 0, 0)
    h.setSpacing(4)
    le = QLineEdit()
    le.setPlaceholderText(placeholder)
    le.setFixedWidth(width)
    lbl = QLabel("")
    lbl.setFixedWidth(46)
    lbl.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
    lbl.setStyleSheet("color: grey;")
    h.addWidget(le)
    h.addWidget(lbl)
    h.addStretch()
    return w, le, lbl


# ---------------------------------------------------------------------------
# Dialog
# ---------------------------------------------------------------------------

class CustomTriggersDialog(QDialog):

    def __init__(self, main_window, default_settings, default_settings_dig,
                 sample_rate, parent=None):
        super().__init__(parent, Qt.Window)
        self.setWindowTitle("Custom Triggers")
        self.setMinimumWidth(500)
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)

        self.main_window          = main_window
        self.default_settings     = default_settings     if default_settings     is not None else pd.DataFrame()
        self.default_settings_dig = default_settings_dig if default_settings_dig is not None else pd.DataFrame()
        self.sample_rate          = float(sample_rate) if sample_rate else 1000.0

        # Engine state
        self._armed            = False
        self._triggered        = False
        self._trigger_sample   = None
        self._stop_is_duration = False
        self._autosave_filepath = None
        self._event_token      = 0      # bumped on disarm; cancels pending Waits
        self._seen_total       = None   # samples already checked (see _poll)
        self._primed           = {}     # edge state per condition, see _edge
        self._di_errors        = set()  # digital inputs already reported unreadable
        self._ending           = False  # stop actions done, post-trigger running
        # Session Autosave mirror (see _sync_from_main)
        self._mirroring        = False          # Hold on: Autosave is Session Autosave
        self._own_autosave     = (False, "")    # this dialog's choice, kept while mirroring
        self._syncing          = False          # set while copying the session state in
        self._duration_timer   = None   # owned here — no reliance on main window autostop
        self._event_rows       = []     # list of added trigger event dicts
        self._stop_order       = []     # the same rows, in Stop Actions order

        # Poll timer — checks conditions against live ring buffer
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(100)   # 100 ms is plenty; avoids hammering the UI thread
        self._poll_timer.timeout.connect(self._poll)

        self._build_channel_lists()
        self._build_ui()
        self._connect_signals()
        self._on_source_changed(self.source_cb.currentText())   # set initial state
        self._on_stop_source_changed(self.stop_source_cb.currentText())

        # Follow Hold / Session Autosave in the main window.
        try:
            main_window.session_state_changed.connect(self._sync_from_main)
        except AttributeError:
            pass
        self._sync_from_main()

        # Only disable Run in the main window; filename is managed here
        self._set_run_locked(True)

    # ------------------------------------------------------------------ channel lists

    def _load_plotted(self):
        """Analog sources: the channels plotted in the main window, on the Y
        or the X axis. name -> (unit, ring index for rb_tail)."""
        try:
            chans = self.main_window.plotted_channels()
        except AttributeError as e:
            # An older Multichannel_DAQ without plotted_channels(): say so
            # instead of silently offering no channels.
            print("Custom Triggers: the main window cannot list plotted "
                  f"channels ({e}). Update Multichannel_DAQ.py to the version "
                  "that matches DAQ_CustomTriggers.py.")
            chans = []
        self._plotted = {name: (unit or "V", idx) for name, unit, idx in chans}

    def _build_channel_lists(self):
        self._load_plotted()

        # Digital channels — split into DI (trigger source) and DO (add-event actions)
        self._digital_names        = []
        self._digital_input_names  = []   # DI only — shown in Triggering Source
        self._digital_output_names = []   # DO only — available as Add Event actions

        if not self.default_settings_dig.empty:
            name_col = 'Device Name' if 'Device Name' in self.default_settings_dig.columns else 'Channel'
            dir_col  = 'Direction'   if 'Direction'   in self.default_settings_dig.columns else None
            for _, row in self.default_settings_dig.iterrows():
                name = row.get(name_col, '')
                if not name or pd.isna(name):
                    continue
                name = str(name)
                self._digital_names.append(name)
                direction = str(row.get(dir_col, 'Input') if dir_col else 'Input').strip().lower()
                if direction == 'output':
                    self._digital_output_names.append(name)
                else:
                    self._digital_input_names.append(name)

    # ------------------------------------------------------------------ UI

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(10)
        root.setContentsMargins(14, 14, 14, 14)

        # Title
        title = QLabel("Custom Triggers")
        f = QFont(); f.setPointSize(12); f.setBold(True)
        title.setFont(f)
        root.addWidget(title)
        root.addWidget(_hline())

        # ── Save Filename ──────────────────────────────────────────────
        fname_group = QGroupBox("Save File")
        fname_layout = QHBoxLayout(fname_group)
        self.autosave_chk = QCheckBox("File Autosave")
        self.autosave_chk.setChecked(False)
        fname_layout.addWidget(self.autosave_chk)
        self.fname_le = QLineEdit()
        self.fname_le.setPlaceholderText("e.g. trigger_run_001.csv")
        self.fname_le.setEnabled(False)
        fname_layout.addWidget(self.fname_le)
        root.addWidget(fname_group)

        # ── Trigger Condition: [Source | Event | Value] in one row ────
        trig_group = QGroupBox("Trigger Condition")
        trig_outer = QVBoxLayout(trig_group)
        trig_outer.setSpacing(6)

        # [ source | event | value ]
        trig_row = QHBoxLayout()
        trig_row.setSpacing(6)

        self.source_cb = QComboBox()
        self.source_cb.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._fill_sources(self.source_cb, _SOURCE_TIME)
        trig_row.addWidget(self.source_cb, 3)

        self.trig_event_cb = QComboBox()
        self.trig_event_cb.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        trig_row.addWidget(self.trig_event_cb, 3)

        _w, self.trig_val_le, self.trig_val_unit = _unit_field("e.g. 50", width=70)
        trig_row.addWidget(_w, 2)
        # High edge of a window (Enter/Leave/Inside/Outside Window)
        self._trig_val2_w, self.trig_val2_le, self.trig_val2_unit = \
            _unit_field("high", width=70)
        self._trig_val2_w.setVisible(False)
        trig_row.addWidget(self._trig_val2_w, 2)

        trig_outer.addLayout(trig_row)

        # Trigger Hysteresis — only shown for analog level/edge triggers
        self._hyst_widget = QWidget()
        hyst_layout = QHBoxLayout(self._hyst_widget)
        hyst_layout.setContentsMargins(0, 0, 0, 0)
        hyst_layout.setSpacing(6)
        hyst_lbl = QLabel("Trigger Hysteresis:")
        hyst_lbl.setStyleSheet("color:#555;")
        hyst_layout.addWidget(hyst_lbl)
        _wh, self.hyst_le, self.hyst_unit = _unit_field("optional", width=70)
        hyst_layout.addWidget(_wh)
        hyst_layout.addStretch()
        self._hyst_widget.setVisible(False)   # hidden until an analog source is chosen
        trig_outer.addWidget(self._hyst_widget)

        root.addWidget(trig_group)

        # ── Added Events (actions fired when trigger fires) ────────────
        add_events_group = QGroupBox("Added Events")
        add_events_outer = QVBoxLayout(add_events_group)
        add_events_outer.setSpacing(4)

        self._events_container = QWidget()
        self._events_layout    = QVBoxLayout(self._events_container)
        self._events_layout.setContentsMargins(0, 0, 0, 0)
        self._events_layout.setSpacing(4)
        add_events_outer.addWidget(self._events_container)

        self._add_event_btn = QPushButton("+ Add Event")
        self._add_event_btn.setMaximumWidth(110)
        self._add_event_btn.setStyleSheet(
            "QPushButton { color:#2980b9; border:1px solid #2980b9; "
            "border-radius:3px; padding:2px 8px; background:transparent; }"
            "QPushButton:disabled { color:#aaa; border-color:#ccc; }"
            "QPushButton:hover:enabled { background:#eaf4fb; }"
        )
        self._add_event_btn.clicked.connect(self._show_add_event_menu)
        add_events_outer.addWidget(self._add_event_btn)
        root.addWidget(add_events_group)

        # ── Stopping Event: [Source | Event | Value], like the trigger ─
        stop_group = QGroupBox("Stopping Event")
        stop_outer = QVBoxLayout(stop_group)
        stop_outer.setSpacing(6)

        stop_row = QHBoxLayout()
        stop_row.setSpacing(6)
        self.stop_source_cb = QComboBox()
        self.stop_source_cb.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._fill_sources(self.stop_source_cb, _SOURCE_DURATION)
        stop_row.addWidget(self.stop_source_cb, 3)

        self.stop_event_cb = QComboBox()
        self.stop_event_cb.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        stop_row.addWidget(self.stop_event_cb, 3)

        _w2, self.stop_val_le, self.stop_val_unit = _unit_field("e.g. 20", width=70)
        stop_row.addWidget(_w2, 2)
        self._stop_val2_w, self.stop_val2_le, self.stop_val2_unit = \
            _unit_field("high", width=70)
        self._stop_val2_w.setVisible(False)
        stop_row.addWidget(self._stop_val2_w, 2)
        stop_outer.addLayout(stop_row)

        # Stop Hysteresis — only shown for an analog stop source
        self._stop_hyst_widget = QWidget()
        sh_layout = QHBoxLayout(self._stop_hyst_widget)
        sh_layout.setContentsMargins(0, 0, 0, 0)
        sh_layout.setSpacing(6)
        sh_lbl = QLabel("Stop Hysteresis:")
        sh_lbl.setStyleSheet("color:#555;")
        sh_layout.addWidget(sh_lbl)
        _wsh, self.stop_hyst_le, self.stop_hyst_unit = _unit_field("optional", width=70)
        sh_layout.addWidget(_wsh)
        sh_layout.addStretch()
        self._stop_hyst_widget.setVisible(False)
        stop_outer.addWidget(self._stop_hyst_widget)

        self._stop_note = QLabel("")
        self._stop_note.setStyleSheet("color: #c0392b; font-style: italic;")
        self._stop_note.setWordWrap(True)
        stop_outer.addWidget(self._stop_note)

        root.addWidget(stop_group)

        # ── Stop Actions (auto-populated, one per Added Event) ─────────
        self._stop_actions_group = QGroupBox("Stop Actions")
        self._stop_actions_group.setVisible(False)
        self._stop_actions_layout = QVBoxLayout(self._stop_actions_group)
        self._stop_actions_layout.setSpacing(2)
        root.addWidget(self._stop_actions_group)

        root.addWidget(_hline())

        # ── Start Event button ─────────────────────────────────────────
        self.start_btn = QPushButton("▶  Start Event")
        self.start_btn.setMinimumHeight(36)
        self.start_btn.setEnabled(False)
        bf = QFont(); bf.setBold(True); bf.setPointSize(10)
        self.start_btn.setFont(bf)
        self.start_btn.setStyleSheet(
            "QPushButton           { background:#27ae60; color:white; border-radius:4px; }"
            "QPushButton:disabled  { background:#bdc3c7; color:#7f8c8d; }"
            "QPushButton:hover:enabled { background:#2ecc71; }"
        )
        root.addWidget(self.start_btn)

        # ── Force Stop button ──────────────────────────────────────────
        self.force_stop_btn = QPushButton("■  Force Stop")
        self.force_stop_btn.setMinimumHeight(32)
        self.force_stop_btn.setEnabled(False)
        self.force_stop_btn.setFont(bf)
        self.force_stop_btn.setStyleSheet(
            "QPushButton           { background:#c0392b; color:white; border-radius:4px; }"
            "QPushButton:disabled  { background:#bdc3c7; color:#7f8c8d; }"
            "QPushButton:hover:enabled { background:#e74c3c; }"
        )
        root.addWidget(self.force_stop_btn)

        self.status_lbl = QLabel("")
        self.status_lbl.setAlignment(Qt.AlignCenter)
        self.status_lbl.setStyleSheet("color:#2980b9; font-style:italic;")
        root.addWidget(self.status_lbl)
        root.addStretch()

    # ------------------------------------------------------------------ signals

    def _connect_signals(self):
        self.source_cb.currentTextChanged.connect(self._on_source_changed)
        self.stop_source_cb.currentTextChanged.connect(self._on_stop_source_changed)
        self.trig_event_cb.currentTextChanged.connect(self._on_trig_event_changed)
        self.stop_event_cb.currentTextChanged.connect(self._on_stop_event_changed)
        for le in (self.trig_val2_le, self.stop_val2_le):
            le.textChanged.connect(self._refresh_start_btn)
        self.stop_hyst_le.textChanged.connect(self._refresh_start_btn)
        self.trig_val_le.textChanged.connect(self._refresh_start_btn)
        self.hyst_le.textChanged.connect(self._refresh_start_btn)
        self.stop_val_le.textChanged.connect(self._on_stop_val_changed)
        self.fname_le.textChanged.connect(self._refresh_start_btn)
        self.autosave_chk.stateChanged.connect(self._on_autosave_toggled)
        self.start_btn.clicked.connect(self._on_start_event)
        self.force_stop_btn.clicked.connect(self._on_force_stop)

    # ------------------------------------------------------------------ source / event logic

    def _on_source_changed(self, source):
        is_time  = (source == _SOURCE_TIME)
        is_dig   = (source in self._digital_input_names)

        # Triggering Event dropdown
        self.trig_event_cb.blockSignals(True)
        self.trig_event_cb.clear()
        if is_analog := not is_time and not is_dig:
            self.trig_event_cb.addItems(_ANALOG_EVENTS)
            self.trig_event_cb.setEnabled(True)
        elif is_dig:
            self.trig_event_cb.addItems(_DIGITAL_EVENTS)
            self.trig_event_cb.setEnabled(True)
        else:
            self.trig_event_cb.addItem("(elapsed time)")
            self.trig_event_cb.setEnabled(False)
        self.trig_event_cb.blockSignals(False)

        # Triggering Event Value + Trigger Hysteresis visibility
        if is_dig:
            self.trig_val_le.setEnabled(False)
            self.trig_val_le.setPlaceholderText("N/A for digital")
            self.trig_val_unit.setText("")
            self._hyst_widget.setVisible(False)
            self.hyst_le.clear()
        elif is_time:
            self.trig_val_le.setEnabled(True)
            self.trig_val_le.setPlaceholderText("delay (ms)")
            self.trig_val_unit.setText("ms")
            self._hyst_widget.setVisible(False)
            self.hyst_le.clear()
        else:
            unit = self._plotted.get(source, ("V", None))[0]
            self.trig_val_le.setEnabled(True)
            self.trig_val_le.setPlaceholderText("threshold")
            self.trig_val_unit.setText(unit)
            self._hyst_widget.setVisible(True)
            self.hyst_le.setEnabled(True)
            self.hyst_unit.setText(unit)

        self._on_trig_event_changed(self.trig_event_cb.currentText())
        self._refresh_start_btn()

    def _on_trig_event_changed(self, event):
        self._show_window_field(event, self.trig_val_le, self._trig_val2_w,
                                self.trig_val2_le, self.trig_val2_unit,
                                self.trig_val_unit.text())

    def _on_stop_event_changed(self, event):
        self._show_window_field(event, self.stop_val_le, self._stop_val2_w,
                                self.stop_val2_le, self.stop_val2_unit,
                                self.stop_val_unit.text())

    def _show_window_field(self, event, val_le, val2_w, val2_le, val2_unit, unit):
        """A window event takes a low and a high value; the others one."""
        is_win = event in _WINDOW_EVENTS
        val2_w.setVisible(is_win)
        val2_unit.setText(unit)
        if is_win:
            val_le.setPlaceholderText("low")
        elif val_le.isEnabled() and val_le.placeholderText() == "low":
            val_le.setPlaceholderText("threshold")
        if not is_win:
            val2_le.clear()
        self._refresh_start_btn()

    def _fill_sources(self, combo, fixed):
        """Fill a source dropdown: the plotted channels, the digital inputs,
        then `fixed` (Time for the trigger, Duration for the stop). Keeps the
        current choice when it is still offered, else falls back to `fixed`."""
        current = combo.currentText()
        items = list(self._plotted) + list(self._digital_input_names) + [fixed]
        combo.blockSignals(True)
        combo.clear()
        combo.addItems(items)
        combo.setCurrentText(current if current in items else fixed)
        combo.blockSignals(False)

    def _refresh_sources(self):
        """Re-read the plotted channels and refill both source dropdowns.
        Not while armed: the running event keeps the sources it started with."""
        if self._armed:
            return
        self._load_plotted()
        trig, stop = self.source_cb.currentText(), self.stop_source_cb.currentText()
        self._fill_sources(self.source_cb, _SOURCE_TIME)
        self._fill_sources(self.stop_source_cb, _SOURCE_DURATION)
        if self.source_cb.currentText() != trig:
            self._on_source_changed(self.source_cb.currentText())
        if self.stop_source_cb.currentText() != stop:
            self._on_stop_source_changed(self.stop_source_cb.currentText())

    def _on_stop_source_changed(self, source):
        is_dur = (source == _SOURCE_DURATION)
        is_dig = (source in self._digital_input_names)

        self.stop_event_cb.blockSignals(True)
        self.stop_event_cb.clear()
        if is_dur:
            self.stop_event_cb.addItem("(after the trigger)")
            self.stop_event_cb.setEnabled(False)
        else:
            self.stop_event_cb.addItems(_DIGITAL_EVENTS if is_dig else _STOP_EVENTS)
            self.stop_event_cb.setEnabled(True)
        self.stop_event_cb.blockSignals(False)

        if is_dig:
            self.stop_val_le.setEnabled(False)
            self.stop_val_le.setPlaceholderText("N/A for digital")
            self.stop_val_unit.setText("")
        elif is_dur:
            self.stop_val_le.setEnabled(True)
            self.stop_val_le.setPlaceholderText("duration (s)")
            self.stop_val_unit.setText("s")
        else:
            unit = self._plotted.get(source, ("V", None))[0]
            self.stop_val_le.setEnabled(True)
            self.stop_val_le.setPlaceholderText("threshold")
            self.stop_val_unit.setText(unit)
            self.stop_hyst_unit.setText(unit)
        self._stop_hyst_widget.setVisible(not is_dig and not is_dur)
        if is_dig or is_dur:
            self.stop_hyst_le.clear()

        self._on_stop_event_changed(self.stop_event_cb.currentText())
        self._on_stop_val_changed(self.stop_val_le.text())
        self._refresh_start_btn()

    def _on_stop_val_changed(self, text):
        """Show informational note when Duration stop value is entered."""
        self._stop_note.setText("")
        if self.stop_source_cb.currentText() != _SOURCE_DURATION:
            return
        try:
            dur = float(text.strip())
            self._stop_note.setText(
                f"ℹ  DAQ will acquire {dur} s after the trigger fires."
            )
            self._stop_note.setStyleSheet("color: #2980b9; font-style: italic;")
        except (ValueError, AttributeError):
            pass
        self._refresh_start_btn()

    # ------------------------------------------------------------------ autosave toggle

    def _on_autosave_toggled(self, state):
        if self._mirroring and not self._syncing:
            # Clicked while Hold is on: this is Session Autosave, so set that
            # (which may ask for a workbook), then take back whatever it ended
            # up as — the user can cancel the workbook dialog.
            try:
                self.main_window.set_session_autosave(state == Qt.Checked)
            except AttributeError:
                pass
            self._sync_from_main()
            return

        enabled = self.autosave_chk.isChecked()
        if not self._mirroring:
            self.fname_le.setEnabled(enabled)
            if not enabled and not self._syncing:
                self.fname_le.clear()

        if enabled:
            # Ensure a Start Recording row exists (add it if not present)
            if not self._has_start_recording_row():
                self._add_trigger_event(force_type=_EVT_START_REC, auto_added=True)
        else:
            # Remove the Start Recording row if it was auto-added
            # (only remove if it was the auto-added mandatory one)
            self._remove_start_recording_row_if_auto()

        self._refresh_required_rows()
        self._refresh_start_btn()

    def _sync_from_main(self):
        """Follow the main window's session state.

        Autosave: while Hold is on, show and control Session Settings ->
        Session Autosave here. With Hold on a recording becomes a held run
        rather than going to this dialog's file, and Session Autosave is what
        writes the session to disk, so that is what the checkbox means then.
        With Hold off it is this dialog's own File Autosave again, restored to
        how it was before Hold.

        Start button: Re-Start Event while a held run's tab is shown.
        """
        if not self._armed:
            self.start_btn.setText(self._idle_start_text())
            self._refresh_sources()     # plotted channels may have changed
        try:
            st = self.main_window.session_autosave_state()
        except AttributeError:
            return
        self._syncing = True
        try:
            if st['hold']:
                if not self._mirroring:
                    self._own_autosave = (self.autosave_chk.isChecked(),
                                          self.fname_le.text())
                    self._mirroring = True
                tip = ("Hold is on, so recordings become held runs. This is "
                       "Session Settings \u2192 Session Autosave, which saves "
                       "the whole session to its file.")
                self.autosave_chk.setText("Session Autosave")
                self.autosave_chk.setChecked(st['on'])
                self.autosave_chk.setEnabled(st['editable'])
                self.fname_le.setEnabled(False)
                self.fname_le.setText(st['path'])
                self.fname_le.setPlaceholderText(
                    "Session Settings \u2192 Session Autosave file")
                for w in (self.autosave_chk, self.fname_le):
                    w.setToolTip(tip)
            elif self._mirroring:
                self._mirroring = False
                checked, fname = self._own_autosave
                self.autosave_chk.setText("File Autosave")
                self.autosave_chk.setEnabled(True)
                self.fname_le.setPlaceholderText("e.g. trigger_run_001.csv")
                for w in (self.autosave_chk, self.fname_le):
                    w.setToolTip("")
                self.autosave_chk.setChecked(checked)
                self.fname_le.setText(fname)
                self.fname_le.setEnabled(checked)
        finally:
            self._syncing = False
        self._refresh_start_btn()

    def _rerun_number(self):
        """The held run a start would overwrite (its tab is shown), else None."""
        try:
            return self.main_window.selected_run_number()
        except AttributeError:
            return None

    def _idle_start_text(self):
        return ("\u25b6  Re-Start Event" if self._rerun_number() is not None
                else "\u25b6  Start Event")

    def showEvent(self, event):
        super().showEvent(event)
        self._sync_from_main()     # Hold may have changed while hidden

    def _has_start_recording_row(self):
        return any(r['type_cb'].currentText() == _EVT_START_REC
                   for r in self._event_rows)

    def _remove_start_recording_row_if_auto(self):
        """Remove the first Start Recording row that was auto-added (undeletable)."""
        for row in list(self._event_rows):
            if row['type_cb'].currentText() == _EVT_START_REC and row.get('auto_added'):
                self._remove_trigger_event(row)
                break

    # ------------------------------------------------------------------ validation

    def _config_valid(self):
        # Every Wait needs a time; Stop Recording's post-trigger must be
        # 0 to _POST_TRIGGER_MAX_MS ms.
        for r in self._event_rows:
            t = r['type_cb'].currentText()
            if t == _EVT_WAIT and self._wait_ms(r) is None:
                return False
            if t == _EVT_START_REC and self._post_ms(r) is None:
                return False

        # A window event needs a low below its high.
        for ev_cb, lo_le, hi_le in (
                (self.trig_event_cb, self.trig_val_le, self.trig_val2_le),
                (self.stop_event_cb, self.stop_val_le, self.stop_val2_le)):
            if ev_cb.currentText() in _WINDOW_EVENTS:
                try:
                    if not float(lo_le.text()) < float(hi_le.text()):
                        return False
                except ValueError:
                    return False

        # If this dialog's own autosave is on, a filename must be entered.
        # (While mirroring, Session Autosave cannot be on without a workbook.)
        if (self.autosave_chk.isChecked() and not self._mirroring
                and not self.fname_le.text().strip()):
            return False

        source      = self.source_cb.currentText()
        is_dig_src  = (source in self._digital_input_names)
        is_dig_stop = (self.stop_source_cb.currentText() in self._digital_input_names)

        # Trigger value: required unless digital source
        if not is_dig_src:
            t = self.trig_val_le.text().strip()
            if not t:
                return False
            try:
                float(t)
            except ValueError:
                return False

        # Stop value: required unless digital stop
        if not is_dig_stop:
            s = self.stop_val_le.text().strip()
            if not s:
                return False
            try:
                float(s)
            except ValueError:
                return False

        return True

    def _refresh_start_btn(self):
        self.start_btn.setEnabled(self._config_valid() and not self._armed)

    # ------------------------------------------------------------------ main window locking

    def _set_run_locked(self, locked: bool):
        mw = self.main_window
        if mw is None:
            return
        try:
            tip = "Close Custom Triggers to use Run/Stop manually" if locked else ""
            mw.outputSettingsWidget.start_button.setEnabled(not locked)
            mw.outputSettingsWidget.start_button.setToolTip(tip)
            mw.outputSettingsWidget.stop_button.setEnabled(not locked)
            mw.outputSettingsWidget.stop_button.setToolTip(tip)
        except AttributeError:
            pass
        # Auto Stop is not touched here: APP_SPEC 'owns_recording_length' has
        # the main window untick and grey it while this dialog is open.
        # Gray out the function generator (launch button + its own Start/Stop)
        # while Custom Triggers is open — generation is driven from here.
        self._lock_func_gen(locked)
        # Gray out the Flow Failure button while Custom Triggers is open.
        self._refresh_app_bar()
        # On unlock (dialog closing), re-enable the Custom Triggers launch
        # button itself. start_task()->disable_buttons() turns it off when an
        # event runs, and enable_buttons() only restores it when the dialog is
        # already closed — so without this it stays orphaned-disabled.
        # On unlock the bar is re-evaluated wholesale, which re-enables this
        # app's own button too. The old code poked that one button directly and
        # was wrapped in except AttributeError, so it failed silently.
        if not locked:
            self._refresh_app_bar()

    # ------------------------------------------------------------------ flow failure locking

    def _refresh_app_bar(self):
        """Ask the main window to re-evaluate every app button.

        Which apps get locked out follows from the 'ao' claim in this app's
        APP_SPEC, so no other app is named here.
        """
        mw = self.main_window
        if mw is not None:
            mw.refresh_app_availability()

    # ------------------------------------------------------------------ function generator locking

    def _lock_func_gen(self, locked: bool):
        """Disable the function generator's launch button in the main window and,
        if the dialog is open, its own Start/Stop buttons. Custom Triggers owns
        generation while this dialog is open."""
        mw = self.main_window
        apps = getattr(mw, 'appsWidget', None) if mw else None

        # The launch button itself is handled by refresh_app_availability via
        # the 'ao' claim; only the FG dialog's own Start/Stop needs doing here.
        if mw is not None and hasattr(mw, 'refresh_app_availability'):
            mw.refresh_app_availability()

        # The function generator dialog's own Start/Stop buttons
        fg = self._get_func_gen()
        if fg is not None:
            try:
                fg.lock_for_ct(locked)
            except AttributeError:
                # Fallback for older FG versions
                tip = "Custom Triggers controls generation" if locked else ""
                fg.start_button.setEnabled(not locked)
                fg.start_button.setToolTip(tip)
                fg.stop_button.setEnabled(not locked and getattr(fg, 'is_running', False))
                fg.stop_button.setToolTip(tip)

    def _open_and_lock_func_gen(self):
        """Open (or re-show) the function generator dialog and disable its own
        Start/Stop buttons. Called when a 'Start Generation' event is chosen."""
        mw = self.main_window
        apps = getattr(mw, 'appsWidget', None) if mw else None
        fg = self._get_func_gen()
        if fg is None:
            if apps is not None:
                try:
                    apps.open_app('function_generator')
                except Exception as e:
                    print(f"Custom Triggers: could not open Function Generator: {e}")
        else:
            # Dialog already exists but may be hidden — bring it back up
            try:
                fg.show()
                fg.raise_()
            except Exception:
                pass
        self._lock_func_gen(True)

    def _close_func_gen(self):
        """Close the function generator dialog. Called when no event row selects
        'Start Generation' anymore. Stops generation first if it is running."""
        fg = self._get_func_gen()
        if fg is None:
            return
        try:
            if getattr(fg, 'is_running', False):
                fg._stop_all()
        except Exception as e:
            print(f"Custom Triggers: stop before close failed: {e}")
        try:
            fg.close()
        except Exception:
            pass

    def _sync_func_gen_for_rows(self):
        """Keep the function generator in step with the event rows: open it when
        any row selects a generation event, close it when none do. Skipped while
        armed so an in-progress run isn't disturbed."""
        if self._armed:
            return
        has_sg = any(r['type_cb'].currentText().startswith("Start: ")
                     for r in self._event_rows)
        if has_sg:
            self._open_and_lock_func_gen()
        else:
            self._close_func_gen()

    # ------------------------------------------------------------------ function generator accessor

    def _get_func_gen(self):
        """The live Function Generator dialog, or None.

        Asks the Apps bar by app id. This used to guess among several possible
        attribute names, which broke silently the moment those names changed —
        _get_func_gen() would just return None and generation would quietly stop
        working rather than raising.
        """
        mw = self.main_window
        if mw is None:
            return None
        apps = getattr(mw, 'appsWidget', None)
        if apps is None:
            return None
        return apps.app_dialog('function_generator')

    # ------------------------------------------------------------------ added events

    def _available_event_types(self):
        types = [_EVT_START_REC]   # always available — recording is always possible
        # Offer per-channel "Start: aoX" entries for each AO output channel.
        mw = self.main_window
        apps = getattr(mw, 'appsWidget', None) if mw else None
        # Registry is the authority on whether the Function Generator exists in
        # this build, rather than probing the main window for a method name.
        try:
            import DAQ_AppRegistry as _reg
            _fg_available = _reg.by_id('function_generator') is not None
        except Exception:
            _fg_available = False
        can_gen = (apps is not None and _fg_available) \
                  or self._get_func_gen() is not None
        if can_gen:
            fg = self._get_func_gen()
            # Try to read channel names from the FG dialog's output settings
            output_df = None
            if fg is not None and hasattr(fg, 'default_settings_output'):
                output_df = fg.default_settings_output
            elif mw is not None:
                import __main__ as _m
                output_df = getattr(_m, 'default_settings_output', None)
            if output_df is not None and len(output_df) > 0:
                for i in range(len(output_df)):
                    try:
                        # Use Device Name (friendly) as the display label;
                        # fall back to Channel string if unavailable.
                        if 'Device Name' in output_df.columns:
                            display = str(output_df['Device Name'].iloc[i]).strip()
                            if not display or display.lower() in ('nan', ''):
                                display = str(output_df['Channel'].iloc[i])
                        else:
                            display = str(output_df['Channel'].iloc[i])
                    except Exception:
                        display = f"ao{i}"
                    types.append(f"Start: {display}")
            else:
                types.append("Start: ao0")
        types.extend(self._digital_output_names)
        types.append(_EVT_WAIT)    # may be used any number of times
        return types

    def _stop_label_for(self, event_type):
        if event_type == _EVT_WAIT:
            return ""              # stop actions are immediate; waits are skipped
        if event_type == _EVT_START_REC:
            return "→  Stop Recording"
        if event_type.startswith("Start: "):
            ch = event_type[len("Start: "):]
            return f"→  Stop: {ch}"
        return f"→  Clear  {event_type}  (write LOW)"

    def _selected_event_types(self, exclude_row=None):
        """Event types currently chosen in rows other than exclude_row."""
        return [
            row['type_cb'].currentText()
            for row in self._event_rows
            if row is not exclude_row
        ]

    def _refresh_event_options(self):
        """Keep each Added Event dropdown free of types already chosen in other
        rows, so the same action can't be selected twice."""
        all_types = self._available_event_types()
        for row in self._event_rows:
            cb      = row['type_cb']
            current = cb.currentText()
            taken   = self._selected_event_types(exclude_row=row)
            allowed = [t for t in all_types
                       if t not in taken or t == current or t == _EVT_WAIT]
            cb.blockSignals(True)
            cb.clear()
            cb.addItems(allowed)
            if current in allowed:
                cb.setCurrentText(current)
            cb.blockSignals(False)
        # The only limit is the number of distinct available event types —
        # the same action can't be chosen twice, so once all types are used
        # there's nothing left to add.
        taken_types = self._selected_event_types()
        remaining   = [t for t in all_types if t not in taken_types]
        self._add_event_btn.setEnabled(len(remaining) > 0)

    def _addable_event_types(self):
        """Event types that can still be added: each action once, Wait any
        number of times."""
        taken = self._selected_event_types()
        return [t for t in self._available_event_types()
                if t not in taken or t == _EVT_WAIT]

    def _show_add_event_menu(self):
        """+ Add Event: pick the event to add from the ones still available."""
        menu = QMenu(self)
        for t in self._addable_event_types():
            menu.addAction(t).triggered.connect(
                lambda _=False, t=t: self._add_trigger_event(chosen_type=t))
        btn = self._add_event_btn
        menu.exec_(btn.mapToGlobal(btn.rect().bottomLeft()))

    def _add_trigger_event(self, force_type=None, auto_added=False, chosen_type=None):
        """Add an event row.

        force_type  — if set, the row is locked to that event type.
        auto_added  — if True (autosave-injected Start Recording), the row
                      shows a lock icon instead of ✕ and cannot be removed
                      manually; it disappears when autosave is unchecked.
        chosen_type — the type picked from the + Add Event menu.
        """
        all_types = self._available_event_types()
        taken     = self._selected_event_types()

        if force_type:
            # If already present, don't add a duplicate
            if force_type in taken:
                return
            options      = [force_type]
            default_type = force_type
        elif chosen_type:
            options = self._addable_event_types()
            if chosen_type not in options:
                return
            default_type = chosen_type
        else:
            options = [t for t in all_types if t not in taken or t == _EVT_WAIT]
            if not options:
                return
            # Prefer a DO / recording default over generation events
            # so adding a row doesn't open the FG unless the user picks it;
            # Wait only once nothing else is left.
            default_type = next(
                (t for t in options
                 if not t.startswith("Start: ")
                 and t not in (_EVT_START_REC, _EVT_WAIT)),
                next((t for t in options if t != _EVT_WAIT), _EVT_WAIT)
            )

        row_widget = QWidget()
        row_layout = QHBoxLayout(row_widget)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(6)

        # Move up / down — shown once there are two or more events (see
        # _refresh_move_buttons). Events fire in list order.
        up_btn, down_btn = QPushButton("\u25b2"), QPushButton("\u25bc")
        for b, tip in ((up_btn, "Move this event up"),
                       (down_btn, "Move this event down")):
            b.setFixedWidth(22)
            b.setToolTip(tip)
            b.setStyleSheet("QPushButton { border:none; color:#2980b9; }"
                            "QPushButton:disabled { color:#ccc; }")
            row_layout.addWidget(b)

        type_cb = QComboBox()
        type_cb.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        type_cb.addItems(options)
        type_cb.setCurrentText(default_type)
        # Locked while it is Autosave's Start Recording row: _refresh_required_rows
        row_layout.addWidget(type_cb)

        # Pre-trigger field — only shown for Start Recording rows
        pre_trig_widget = QWidget()
        pre_trig_layout = QHBoxLayout(pre_trig_widget)
        pre_trig_layout.setContentsMargins(0, 0, 0, 0)
        pre_trig_layout.setSpacing(2)
        pre_lbl = QLabel("Pre-trigger:")
        pre_lbl.setStyleSheet("color:#555; font-size:11px;")
        pre_trig_layout.addWidget(pre_lbl)
        _wp, pre_trig_le, pre_trig_unit = _unit_field("ms", width=55)
        pre_trig_unit.setText("ms")
        pre_trig_layout.addWidget(_wp)
        pre_trig_widget.setVisible(default_type == _EVT_START_REC)
        row_layout.addWidget(pre_trig_widget)

        # Wait time — only shown for Wait rows
        wait_widget = QWidget()
        wait_layout = QHBoxLayout(wait_widget)
        wait_layout.setContentsMargins(0, 0, 0, 0)
        wait_layout.setSpacing(2)
        _ww, wait_le, wait_unit = _unit_field("ms", width=55)
        wait_unit.setText("ms")
        wait_le.setToolTip("How long to wait before the events below this one fire")
        wait_layout.addWidget(_ww)
        wait_widget.setVisible(default_type == _EVT_WAIT)
        row_layout.addWidget(wait_widget)
        wait_le.textChanged.connect(self._refresh_start_btn)

        # ✕, or 🔒 while this is the Start Recording row Autosave needs —
        # decided by _refresh_required_rows, for auto-added and user-added
        # rows alike.
        remove_btn = QPushButton("✕")
        remove_btn.setFixedWidth(28)
        row_layout.addWidget(remove_btn)

        # Its Stop Action: own arrows (Stop Actions have their own order),
        # the label, and a post-trigger time for Stop Recording.
        stop_widget = QWidget()
        stop_layout = QHBoxLayout(stop_widget)
        stop_layout.setContentsMargins(0, 0, 0, 0)
        stop_layout.setSpacing(6)
        s_up, s_down = QPushButton("\u25b2"), QPushButton("\u25bc")
        for b, tip in ((s_up, "Move this stop action up"),
                       (s_down, "Move this stop action down")):
            b.setFixedWidth(22)
            b.setToolTip(tip)
            b.setStyleSheet("QPushButton { border:none; color:#2980b9; }"
                            "QPushButton:disabled { color:#ccc; }")
            stop_layout.addWidget(b)
        stop_label = QLabel(self._stop_label_for(default_type))
        stop_label.setStyleSheet("color:#555; font-style:italic; padding:2px 4px;")
        stop_layout.addWidget(stop_label)
        post_widget = QWidget()
        post_layout = QHBoxLayout(post_widget)
        post_layout.setContentsMargins(0, 0, 0, 0)
        post_layout.setSpacing(2)
        post_lbl = QLabel("Post-trigger:")
        post_lbl.setStyleSheet("color:#555; font-size:11px;")
        post_layout.addWidget(post_lbl)
        _wpost, post_le, post_unit = _unit_field("0\u20133000", width=55)
        post_unit.setText("ms")
        post_le.setText("0")
        post_le.setToolTip(f"Keep recording this long after the stop condition "
                           f"(0 to {_POST_TRIGGER_MAX_MS} ms)")
        post_le.textChanged.connect(self._refresh_start_btn)
        post_layout.addWidget(_wpost)
        post_widget.setVisible(default_type == _EVT_START_REC)
        stop_layout.addWidget(post_widget)
        stop_layout.addStretch()
        stop_widget.setVisible(default_type != _EVT_WAIT)

        row_dict = {
            'trigger_widget':  row_widget,
            'type_cb':         type_cb,
            'stop_label':      stop_label,
            'stop_widget':     stop_widget,
            'stop_up':         s_up,
            'stop_down':       s_down,
            'post_le':         post_le,
            'post_widget':     post_widget,
            'pre_trig_le':     pre_trig_le,
            'pre_trig_widget': pre_trig_widget,
            'wait_le':         wait_le,
            'wait_widget':     wait_widget,
            'auto_added':      auto_added,
            'up_btn':          up_btn,
            'down_btn':        down_btn,
            'remove_btn':      remove_btn,
        }
        up_btn.clicked.connect(lambda _, d=row_dict: self._move_event(d, -1))
        down_btn.clicked.connect(lambda _, d=row_dict: self._move_event(d, +1))
        s_up.clicked.connect(lambda _, d=row_dict: self._move_stop(d, -1))
        s_down.clicked.connect(lambda _, d=row_dict: self._move_stop(d, +1))
        self._event_rows.append(row_dict)
        self._stop_order.append(row_dict)
        self._events_layout.addWidget(row_widget)
        self._stop_actions_layout.addWidget(stop_widget)
        self._refresh_stop_group()

        type_cb.currentTextChanged.connect(
            lambda text, d=row_dict: self._on_event_type_changed(d, text)
        )
        # 'activated' fires whenever the user picks an item from the dropdown —
        # including re-picking the one already selected. So if the user closed
        # the function generator and then selects its channel again, this
        # reopens it (currentTextChanged would NOT fire on an unchanged pick).
        type_cb.activated.connect(
            lambda _idx, d=row_dict: self._on_event_type_activated(d)
        )
        remove_btn.clicked.connect(lambda _, d=row_dict: self._remove_trigger_event(d))
        self._refresh_event_options()
        self._sync_func_gen_for_rows()
        self._refresh_move_buttons()
        self._refresh_required_rows()

    def _refresh_required_rows(self):
        """Lock the Start Recording row while Autosave is on.

        Autosave saves what was recorded, so with no Start Recording it would
        save nothing. Whichever row is the Start Recording one — added by
        Autosave or by the user — cannot be deleted or changed to another
        type while Autosave is on. Unticking Autosave unlocks a user-added
        row (and removes an auto-added one, see _on_autosave_toggled).
        """
        need = self.autosave_chk.isChecked()
        locked_one = False
        for r in self._event_rows:
            lock = (need and not locked_one
                    and r['type_cb'].currentText() == _EVT_START_REC)
            locked_one = locked_one or lock
            btn = r['remove_btn']
            if lock:
                btn.setText("🔒")
                btn.setEnabled(False)
                btn.setToolTip("Start Recording is required while Autosave is on")
                btn.setStyleSheet("QPushButton { border:none; color:#aaa; }")
            else:
                btn.setText("✕")
                btn.setEnabled(True)
                btn.setToolTip("Remove this event")
                btn.setStyleSheet(
                    "QPushButton { color:#c0392b; border:none; font-weight:bold; }"
                    "QPushButton:hover { color:#e74c3c; }")
            r['type_cb'].setEnabled(not lock)

    def _remove_trigger_event(self, row_dict):
        if row_dict not in self._event_rows:
            return
        self._event_rows.remove(row_dict)
        self._stop_order.remove(row_dict)
        row_dict['trigger_widget'].setParent(None)
        row_dict['trigger_widget'].deleteLater()
        row_dict['stop_widget'].setParent(None)
        row_dict['stop_widget'].deleteLater()
        self._refresh_stop_group()
        self._refresh_event_options()
        self._sync_func_gen_for_rows()
        self._refresh_move_buttons()

    # Arrows appear once there are more than this many events.
    _MOVE_ARROWS_ABOVE = 1

    def _move_event(self, row_dict, delta):
        """Move an event up (-1) or down (+1). Events fire in list order.
        Stop Actions keep their own order (see _move_stop)."""
        if self._armed or row_dict not in self._event_rows:
            return
        i = self._event_rows.index(row_dict)
        j = i + delta
        if not 0 <= j < len(self._event_rows):
            return
        rows = self._event_rows
        rows[i], rows[j] = rows[j], rows[i]
        # The layout holds only these rows, so re-adding them in list order
        # puts the widgets in that order.
        for r in rows:
            self._events_layout.removeWidget(r['trigger_widget'])
        for r in rows:
            self._events_layout.addWidget(r['trigger_widget'])
        self._refresh_move_buttons()

    def _stop_rows(self):
        """Rows that have a Stop Action (every event but Wait), in stop order."""
        return [r for r in self._stop_order
                if r['type_cb'].currentText() != _EVT_WAIT]

    def _move_stop(self, row_dict, delta):
        """Move a Stop Action up (-1) or down (+1) among the Stop Actions."""
        vis = self._stop_rows()
        if self._armed or row_dict not in vis:
            return
        i = vis.index(row_dict)
        j = i + delta
        if not 0 <= j < len(vis):
            return
        a, b = self._stop_order.index(vis[i]), self._stop_order.index(vis[j])
        self._stop_order[a], self._stop_order[b] = self._stop_order[b], self._stop_order[a]
        for r in self._stop_order:
            self._stop_actions_layout.removeWidget(r['stop_widget'])
        for r in self._stop_order:
            self._stop_actions_layout.addWidget(r['stop_widget'])
        self._refresh_move_buttons()

    def _refresh_stop_group(self):
        self._stop_actions_group.setVisible(bool(self._stop_rows()))
        self._refresh_move_buttons()

    def _refresh_move_buttons(self):
        """Show the arrows with more than _MOVE_ARROWS_ABOVE items, in Added
        Events and in Stop Actions; grey the ones that cannot move (top up,
        bottom down) and all of them while an event is running."""
        for rows, up, down in ((self._event_rows, 'up_btn', 'down_btn'),
                               (self._stop_rows(), 'stop_up', 'stop_down')):
            show = len(rows) > self._MOVE_ARROWS_ABOVE
            for k, r in enumerate(rows):
                r[up].setVisible(show)
                r[down].setVisible(show)
                r[up].setEnabled(not self._armed and k > 0)
                r[down].setEnabled(not self._armed and k < len(rows) - 1)

    def _on_event_type_activated(self, row_dict):
        """User actively chose an item in an event row's dropdown (fires even if
        the selection is unchanged). If it's a function-generator channel,
        (re)open the FG so its settings can be edited again after being closed."""
        if self._armed:
            return
        if row_dict['type_cb'].currentText().startswith("Start: "):
            self._open_and_lock_func_gen()

    def _on_event_type_changed(self, row_dict, event_type):
        row_dict['stop_label'].setText(self._stop_label_for(event_type))
        row_dict['stop_widget'].setVisible(event_type != _EVT_WAIT)
        row_dict['post_widget'].setVisible(event_type == _EVT_START_REC)
        self._refresh_stop_group()
        # Show pre-trigger field only for Start Recording rows, wait time only
        # for Wait rows
        row_dict['pre_trig_widget'].setVisible(event_type == _EVT_START_REC)
        row_dict['wait_widget'].setVisible(event_type == _EVT_WAIT)
        self._refresh_event_options()
        self._sync_func_gen_for_rows()

    def _execute_trigger_events(self, mw):
        """Fire the Added Events top to bottom. A Wait row pauses for its time
        before the rows below it fire; the rest are fired back to back."""
        self._run_events(list(self._event_rows), mw, self._event_token)

    def _run_events(self, rows, mw, token):
        # A disarm (stop, force stop, close) bumps the token, so events still
        # waiting behind a Wait are dropped rather than fired after the stop.
        if token != self._event_token or not self._armed:
            return
        for k, row in enumerate(rows):
            if row['type_cb'].currentText() == _EVT_WAIT:
                ms = self._wait_ms(row)
                if ms > 0:
                    rest = rows[k + 1:]
                    QTimer.singleShot(int(round(ms)),
                                      lambda r=rest: self._run_events(r, mw, token))
                    return
                continue
            self._fire_event(row, mw)

    @staticmethod
    def _post_ms(row):
        """Stop Recording's post-trigger time in ms, or None unless it is a
        number from 0 to _POST_TRIGGER_MAX_MS."""
        try:
            ms = float(row['post_le'].text().strip())
        except ValueError:
            return None
        return ms if 0 <= ms <= _POST_TRIGGER_MAX_MS else None

    @staticmethod
    def _wait_ms(row):
        """A Wait row's time in ms, or None if it is not a number >= 0."""
        try:
            ms = float(row['wait_le'].text().strip())
        except ValueError:
            return None
        return ms if ms >= 0 else None

    def _fire_event(self, row, mw):
        """Fire one Added Event (not a Wait)."""
        event_type = row['type_cb'].currentText()
        if event_type == _EVT_START_REC:
            # Inject pre-trigger data then start recording
            try:
                pre_ms = float(row['pre_trig_le'].text().strip() or "0")
            except (ValueError, KeyError):
                pre_ms = 0.0
            self._start_recording_with_pretrigger(mw, pre_ms)
        elif event_type.startswith("Start: "):
            display = event_type[len("Start: "):]
            fg = self._get_func_gen()
            if fg is not None:
                try:
                    ch_idx = _resolve_fg_channel_idx(fg, display)
                    fg._save_ui_to_channel(fg._current_ch)
                    fg._set_channel_active(ch_idx, True)
                except Exception as e:
                    print(f"Custom Triggers: Start generation '{display}' failed: {e}")
        else:
            self._write_do(event_type, True, mw)

    def _start_recording_with_pretrigger(self, mw, pre_ms: float):
        """Inject pre-trigger samples then start recording in the main window."""
        if pre_ms > 0:
            pre_samples = int(pre_ms / 1000.0 * self.sample_rate)
            if pre_samples > 0:
                try:
                    with mw.record_lock:
                        for ch_idx in range(len(mw.record_buffers)):
                            hist = mw.rb_tail(ch_idx, pre_samples)
                            if hist is not None and hist.size > 0:
                                mw.record_buffers[ch_idx].insert(0, hist.copy())
                except Exception:
                    pass
        try:
            if not mw.is_recording:
                mw.start_recording()
        except AttributeError:
            pass

    def _execute_stop_events(self, mw):
        # All at once, in Stop Actions order — Waits apply only when the events
        # fire. Stop Recording is not done here: it is stopping the acquisition,
        # which _end_event does (after the post-trigger time).
        for row in self._stop_rows():
            event_type = row['type_cb'].currentText()
            if event_type == _EVT_START_REC:
                # Recording will be stopped via stop_task; nothing extra needed
                pass
            elif event_type.startswith("Start: "):
                display = event_type[len("Start: "):]
                fg = self._get_func_gen()
                if fg is not None:
                    try:
                        ch_idx = _resolve_fg_channel_idx(fg, display)
                        fg._set_channel_active(ch_idx, False)
                    except Exception as e:
                        print(f"Custom Triggers: Stop generation '{display}' failed: {e}")
            else:
                self._write_do(event_type, False, mw)

    def _write_do(self, device_name, high, mw=None):
        path = self._dig_path(device_name, mw)
        if path is None:
            return
        try:
            import nidaqmx
            with nidaqmx.Task() as t:
                t.do_channels.add_do_chan(path)
                t.write([bool(high)])
            # Notify main app so CalibrationWindow stays in sync
            if mw is not None and hasattr(mw, 'notify_digital_state'):
                mw.notify_digital_state(path.rsplit('/', 1)[-1], bool(high))
        except Exception as e:
            label = 'HIGH' if high else 'LOW'
            print(f"Custom Triggers: DO write {device_name}={label} failed: {e}")

    def _on_start_event(self):
        if not self._config_valid():
            return

        mw = self.main_window
        if mw is None:
            QMessageBox.warning(self, "Error", "Main DAQ window is not available.")
            return

        # ── Re-Start: a held run's tab is shown, so its run is replaced ─
        rerun = self._rerun_number()
        if rerun is not None:
            reply = QMessageBox.question(
                self, "Re-Start Event",
                f"Run {rerun} will be overwritten by this event's recording.\n\n"
                "To add a new run instead, switch to the Live Graph tab.\n\n"
                f"Overwrite Run {rerun}?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply != QMessageBox.Yes:
                return

        # ── Resolve and validate save filename (autosave only) ─────────
        self._autosave_filepath = None
        # While mirroring, the recording becomes a held run and Session
        # Autosave saves it; there is no file of this dialog's own.
        if self.autosave_chk.isChecked() and not self._mirroring:
            fname = self.fname_le.text().strip()
            if not (fname.lower().endswith('.csv') or fname.lower().endswith('.xlsx')):
                fname += '.csv'
            # The program's folder: beside the .exe when built, beside this
            # file when run from source. (This was `'__file__' in dir()`,
            # which inside a method lists only local names, so it was always
            # False and files went to whatever the working directory was.)
            _app_dir = (os.path.dirname(sys.executable)
                        if getattr(sys, 'frozen', False)
                        else os.path.dirname(os.path.abspath(__file__)))
            fpath = os.path.join(_app_dir, fname)

            # Warn if file already exists
            if os.path.exists(fpath):
                reply = QMessageBox.question(
                    self, "File Already Exists",
                    f"<b>{fname}</b> already exists and will be overwritten.\n\n"
                    "Continue?",
                    QMessageBox.Ok | QMessageBox.Cancel,
                    QMessageBox.Cancel,
                )
                if reply != QMessageBox.Ok:
                    return

            self._autosave_filepath = fpath

        # ── Classify stop type ─────────────────────────────────────────
        self._stop_is_duration = (self.stop_source_cb.currentText() == _SOURCE_DURATION)

        # ── Arm state ──────────────────────────────────────────────────
        self._armed          = True
        self._triggered      = False
        self._trigger_sample = None
        self._primed         = {}
        self._ending         = False
        self._refresh_move_buttons()

        self.start_btn.setEnabled(False)
        self.start_btn.setText("⏳  Waiting for trigger…")
        self.force_stop_btn.setEnabled(True)
        self._set_status("Armed — waiting for trigger event", "#e67e22")

        # ── Start DAQ hardware ─────────────────────────────────────────
        # start_task() resets total_written to 0 and starts acquisition.
        # We must call it BEFORE starting the poll so total_written is a
        # reliable elapsed-sample counter from t=0.
        # Through start_run, so a Re-Start replaces the shown run (already
        # confirmed above) instead of always adding a new one.
        # Conditions are checked only on samples that arrive after Start, so
        # data already on the plot can never fire them. When this starts the
        # acquisition every sample is new (from 0); when it was already
        # running, the first poll takes the current count as the baseline.
        self._seen_total = None
        try:
            if not mw.continue_running:
                mw.start_run(overwrite_selected=rerun is not None)
                self._seen_total = 0
        except AttributeError:
            pass

        # ── Start poll ─────────────────────────────────────────────────
        # Small startup delay so the DAQ hardware is actually streaming
        # before we check conditions — avoids false "0 samples elapsed"
        # trigger on Time mode with very short delays.
        QTimer.singleShot(300, self._start_polling)

    def _start_polling(self):
        if self._armed:
            self._poll_timer.start()

    # ------------------------------------------------------------------ Poll

    def _poll(self):
        mw = self.main_window
        if mw is None or not self._armed:
            self._disarm()
            return
        if self._ending:
            return                      # post-trigger running; nothing to check

        # Safety net: if DAQ stopped externally while we were watching
        # (e.g. user clicked Stop in main window), clean up.
        if not mw.continue_running and self._triggered:
            self._set_status("■ Acquisition complete", "#27ae60")
            self._execute_stop_events(mw)
            self._do_autosave(mw)
            self._disarm()
            return

        try:
            total = int(mw.total_written)
        except AttributeError:
            return

        if total <= 0:
            return
        if self._seen_total is None or total < self._seen_total:
            self._seen_total = total        # baseline: only later samples count
            return

        # ── Phase 1: waiting for trigger ──────────────────────────────
        if not self._triggered:
            fired = self._check_trigger(total, mw)
            self._seen_total = total
            if not fired:
                return

            self._triggered      = True
            self._trigger_sample = total
            self._primed.pop('stop', None)  # the stop looks only after the trigger

            # Fire all added trigger events (Start Recording, Start Generation, DO outputs)
            # Start Recording rows call mw.start_recording() with their own pre-trigger time.
            # If no Start Recording row exists, recording is NOT started automatically.
            self._execute_trigger_events(mw)

            # Update status depending on whether recording was started
            has_rec = self._has_start_recording_row()
            if has_rec:
                self._set_status("✔ Triggered — recording…", "#27ae60")
                self.start_btn.setText("⏳  Recording…")
            else:
                self._set_status("✔ Triggered — waiting for stop…", "#27ae60")
                self.start_btn.setText("⏳  Triggered…")

            # Duration stop: start our own timer — no reliance on main window autostop
            if self._stop_is_duration:
                try:
                    dur_ms = int(float(self.stop_val_le.text().strip()) * 1000)
                except ValueError:
                    dur_ms = 3000
                self._duration_timer = QTimer(self)
                self._duration_timer.setSingleShot(True)
                self._duration_timer.timeout.connect(self._on_duration_elapsed)
                self._duration_timer.start(dur_ms)

            return

        # ── Phase 2: waiting for stop condition (non-Duration only) ────
        if self._stop_is_duration:
            # Duration timer owns the stop — nothing to poll for
            return

        stop_fired = self._check_stop(total, mw)
        self._seen_total = total
        if stop_fired:
            self._end_event(mw, "■ Stop condition met — stopping", "#c0392b")

    # ------------------------------------------------------------------ duration timer callback

    def _on_duration_elapsed(self):
        """Fired by _duration_timer when the recording duration is up."""
        if not self._armed:
            return
        mw = self.main_window
        self._end_event(mw, "■ Duration complete — stopping", "#27ae60")

    def _end_event(self, mw, status, colour):
        """The stopping event happened: run the Stop Actions now, then stop
        the acquisition — which stops the recording — after Stop Recording's
        post-trigger time, if the event is recording."""
        self._ending = True
        if self._duration_timer is not None:
            self._duration_timer.stop()
            self._duration_timer = None
        self._execute_stop_events(mw)
        self._do_autosave(mw)
        post = 0.0
        if getattr(mw, 'is_recording', False):
            rec = next((r for r in self._event_rows
                        if r['type_cb'].currentText() == _EVT_START_REC), None)
            post = (self._post_ms(rec) or 0.0) if rec is not None else 0.0
        if post > 0:
            self._set_status(f"{status} (recording {post:g} ms more)", colour)
            token = self._event_token
            QTimer.singleShot(int(round(post)),
                              lambda: self._finish_event(mw, token))
            return
        self._set_status(status, colour)
        self._finish_event(mw, self._event_token)

    def _finish_event(self, mw, token):
        """Disarm and stop the acquisition — unless a Force Stop already did."""
        if token != self._event_token:
            return
        self._disarm()
        try:
            mw.stop_task()
        except AttributeError:
            pass

    # ------------------------------------------------------------------ condition checkers

    @staticmethod
    def _hysteresis(le) -> float:
        """A hysteresis field's value, or 0.0 if blank/invalid."""
        try:
            return max(0.0, float(le.text().strip()))
        except (ValueError, AttributeError):
            return 0.0

    def _check_trigger(self, total, mw) -> bool:
        source = self.source_cb.currentText()
        if source == _SOURCE_TIME:
            try:
                wait_ms = float(self.trig_val_le.text())
                elapsed_ms = total / self.sample_rate * 1000.0
                return elapsed_ms >= wait_ms
            except (ValueError, AttributeError):
                return False
        return self._check_source('trigger', source, self.trig_event_cb.currentText(),
                                  self.trig_val_le, self.trig_val2_le, self.hyst_le,
                                  total, mw)

    def _check_stop(self, total, mw) -> bool:
        return self._check_source('stop', self.stop_source_cb.currentText(),
                                  self.stop_event_cb.currentText(),
                                  self.stop_val_le, self.stop_val2_le,
                                  self.stop_hyst_le, total, mw)

    def _check_source(self, key, source, event, val_le, val2_le, hyst_le,
                      total, mw) -> bool:
        """Whether one condition (the trigger's or the stop's) has happened.

        Analog: on the samples that arrived since the last check. Digital
        input: on the line's state read now (checked each poll, 100 ms). An
        edge — Rising/Falling, Enter/Leave Window — needs the opposite state
        first: a Rising Edge needs the signal at or below the level before it
        counts going above it. That "primed" state is kept between checks in
        self._primed[key], so an edge split across two checks still counts.
        """
        if source in self._digital_input_names:
            state = self._read_di(source, mw)
            if state is None:
                return False
            v = np.array([state])
            if event == "Rising Edge":
                return self._edge(key, ~v, v)
            if event == "Falling Edge":
                return self._edge(key, v, ~v)
            return False

        y = self._fresh_samples(source, total, mw)
        if y is None:
            return False
        try:
            a = float(val_le.text())
            b = float(val2_le.text()) if event in _WINDOW_EVENTS else a
        except ValueError:
            return False
        h = self._hysteresis(hyst_le)
        if event == "Above Level":
            return bool(np.any(y > a + h))
        if event == "Below Level":
            return bool(np.any(y < a - h))
        if event == "Rising Edge":
            return self._edge(key, y <= a - h, y > a + h)
        if event == "Falling Edge":
            return self._edge(key, y >= a + h, y < a - h)
        # Window a..b: inside / outside, each kept clear of the edges by h.
        inside  = (y >= a + h) & (y <= b - h)
        outside = (y < a - h) | (y > b + h)
        if event == "Inside Window":
            return bool(np.any(inside))
        if event == "Outside Window":
            return bool(np.any(outside))
        if event == "Enter Window":
            return self._edge(key, outside, inside)
        if event == "Leave Window":
            return self._edge(key, inside, outside)
        return False

    def _edge(self, key, prime, fire) -> bool:
        """An edge: `fire` true at a sample after `prime` was true (or after
        an earlier check left it primed). Records the primed state."""
        start = 0
        if not self._primed.get(key):
            p = np.flatnonzero(prime)
            if p.size == 0:
                return False
            start = int(p[0])
            self._primed[key] = True
        return bool(np.any(fire[start:]))

    def _fresh_samples(self, source, total, mw):
        """The source's samples since the last check, or None if nothing new
        or the source is not a plotted channel."""
        entry = self._plotted.get(source)
        if entry is None:
            return None
        idx = entry[1]
        n_new = total - (self._seen_total or 0)
        if n_new <= 0:
            return None
        try:
            n_new = min(n_new, mw.rings[idx].size)
        except Exception:
            pass
        try:
            y = mw.rb_tail(idx, n_new)
        except Exception:
            return None
        return y if (y is not None and y.size > 0) else None

    # ------------------------------------------------------------------ digital lines

    def _dig_path(self, device_name, mw=None):
        """'<device>/<line>' of a digital row named `device_name`, or None.

        Uses the line's own device (its 'DAQ' column) — the line lives on a
        digital module, generally not the input device. Falls back to the
        main window's selected DAQ, then the first input DAQ, for files with
        no DAQ column.
        """
        dig = self.default_settings_dig
        if dig.empty:
            return None
        name_col = 'Device Name' if 'Device Name' in dig.columns else 'Channel'
        if 'Channel' not in dig.columns:
            return None
        row = dig[dig[name_col] == device_name]
        if row.empty:
            return None
        channel = str(row.iloc[0]['Channel'])
        daq_name = ""
        if 'DAQ' in dig.columns:
            v = row.iloc[0].get('DAQ')
            text = "" if v is None else str(v).strip()
            if text and text.lower() != 'nan':
                daq_name = text
        if not daq_name:
            daq_name = str(getattr(mw, 'selected_daq', '') if mw else '')
        if not daq_name and not self.default_settings.empty \
                and 'DAQ' in self.default_settings.columns:
            daq_name = str(self.default_settings['DAQ'].iloc[0])
        return f"{daq_name}/{channel}" if daq_name else channel

    def _read_di(self, device_name, mw=None):
        """A digital input's state now (True = high), or None if it cannot be
        read. A short task per read, as the main window's own DI readout does,
        so the line is never held between checks."""
        path = self._dig_path(device_name, mw)
        if path is None:
            return None
        try:
            import nidaqmx
            with nidaqmx.Task() as t:
                t.di_channels.add_di_chan(path)
                v = t.read()
            if isinstance(v, (list, tuple)):
                v = v[0]
            return bool(v)
        except Exception as e:
            if device_name not in self._di_errors:
                self._di_errors.add(device_name)
                print(f"Custom Triggers: cannot read digital input "
                      f"{device_name} ({path}): {e}")
            return None

    # ------------------------------------------------------------------ status

    def _set_status(self, text, colour="#2980b9"):
        self.status_lbl.setText(text)
        self.status_lbl.setStyleSheet(f"color:{colour}; font-style:italic;")

    # ------------------------------------------------------------------ autosave

    def _do_autosave(self, mw):
        """If autosave is enabled, set the pending save path on the main window
        so _stop_recording saves there automatically without a dialog."""
        fpath = getattr(self, '_autosave_filepath', None)
        if fpath:
            try:
                mw._pending_autosave_path = fpath
            except AttributeError:
                pass
        self._autosave_filepath = None

    # ------------------------------------------------------------------ disarm

    def _disarm(self):
        self._event_token += 1          # drop events still behind a Wait
        self._armed     = False
        self._ending    = False
        self._triggered = False
        self._poll_timer.stop()
        if self._duration_timer is not None:
            self._duration_timer.stop()
            self._duration_timer = None
        self.start_btn.setText(self._idle_start_text())
        self.force_stop_btn.setEnabled(False)
        self._refresh_move_buttons()
        self._refresh_start_btn()

    # ------------------------------------------------------------------ force stop

    def _on_force_stop(self):
        """Immediately stop the event, auto-save if enabled, then disarm."""
        if not self._armed:
            return
        mw = self.main_window
        self._set_status("■ Force stopped", "#c0392b")
        if not self._ending:            # stop actions not yet run
            self._execute_stop_events(mw)
            self._do_autosave(mw)
        self._disarm()                  # also cancels a running post-trigger
        try:
            mw.stop_task()
        except AttributeError:
            pass

    # ------------------------------------------------------------------ close

    def closeEvent(self, event):
        # Don't allow the dialog to close while an event is running.
        if self._armed:
            QMessageBox.warning(
                self, "Event Running",
                "An event is currently running.\n\n"
                "Use Force Stop to halt it before closing Custom Triggers."
            )
            event.ignore()
            return
        self._disarm()
        self._set_run_locked(False)
        super().closeEvent(event)


# ---------------------------------------------------------------------------
# Apps-bar registration (see DAQ_AppRegistry.py)
# ---------------------------------------------------------------------------

def build(ctx, parent):
    return CustomTriggersDialog(
        main_window=ctx.main_window,
        default_settings=ctx.input_settings,
        default_settings_dig=ctx.dig_settings,
        sample_rate=ctx.sample_rate,
        parent=parent,
    )


APP_SPEC = {
    'id':    'custom_triggers',
    'label': 'Custom Triggers',
    'cls':   'CustomTriggersDialog',
    'build': build,
    'show_by_default': False,
    # Drives analog output, so it needs a device present. (The pre-registry
    # code was inconsistent here: the startup path disabled this button when no
    # DAQ was found but enable_buttons() would hand it back.)
    'requires_daq_hw': True,
    'requires_daq_stopped': True,
    'blocks_calibration': True,
    'locks_run_stop': True,
    'owns_recording_length': True,
    # Drives generation itself while open, so it owns the AO bus.
    'claims': {'ao'},
}
