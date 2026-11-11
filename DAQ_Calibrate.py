
"""
DAQ_Calibrate.py — Channel Viewer / Calibration Window
=======================================================
Opened from the main app via "View / Calibrate Channels", or run standalone.

Layout (always visible):
    Ch | Channel Name | Reading | Units | [section header rows]

A single "Calibrate" button at the bottom toggles calibration mode for all
input channels simultaneously.  In calibration mode each input row gains:
    Error Value | Zero | Reset

"Save Calibration" applies all offsets live (even while DAQ is running) and
returns to view mode.

Custom, output, and analog-input-side digital channels are read-only — live
readings only. Digital channels whose Direction is "output" get an On/Off
toggle that drives the line directly; it is live only while the main app is
acquiring, and greyed out while Custom Triggers, Flow Failure or Proportional
Sweep hold the digital bus.
"""

import sys
import os
import numpy as np
import pandas as pd

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QGridLayout, QHBoxLayout,
    QVBoxLayout, QLabel, QPushButton, QMessageBox, QFrame, QSizePolicy
)
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFont

import nidaqmx
from nidaqmx.constants import TerminalConfiguration
from nidaqmx.stream_readers import AnalogMultiChannelReader


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _get_settings_path():
    if getattr(sys, 'frozen', False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, 'default_settings.xlsx')


def _clean_device_names(names):
    result = []
    unnamed = 0
    for name in names:
        if pd.isna(name) or str(name).strip() == "":
            result.append("unnamed" if unnamed == 0 else f"unnamed{unnamed}")
            unnamed += 1
        else:
            result.append(str(name))
    return result


def _shorten_channel(ch_str):
    """Convert NI-style digital channel names to a compact form.
    'port0/line0' -> 'P0.0',  'port1/line3' -> 'P1.3'.
    Anything not matching that pattern is returned as-is."""
    import re
    m = re.match('port([0-9]+)/line([0-9]+)', str(ch_str).strip(), re.IGNORECASE)
    if m:
        return f"P{m.group(1)}.{m.group(2)}"
    return str(ch_str)


def _read_settings():
    path = _get_settings_path()
    df = pd.read_excel(path, sheet_name='input_settings')
    if 'Selected Channel' in df.columns:
        df = df[df['Selected Channel'] == 1].reset_index(drop=True)
    for col, default in [('Error', 0.0), ('Mx', 1.0), ('b', 0.0)]:
        if col not in df.columns:
            df[col] = default
        df[col] = pd.to_numeric(df[col], errors='coerce').fillna(default).astype(float)
    df["Device Name"] = _clean_device_names(df["Device Name"].tolist())
    selected_daq = df['DAQ'].iloc[0] if len(df) > 0 else ''

    def _load_sheet(sheet, sel=True):
        try:
            s = pd.read_excel(path, sheet_name=sheet)
            if sel and 'Selected Channel' in s.columns:
                s = s[s['Selected Channel'] == 1].reset_index(drop=True)
            s["Device Name"] = _clean_device_names(s["Device Name"].tolist())
            return s
        except Exception:
            return pd.DataFrame(columns=['Channel', 'Device Name', 'Units', 'Selected Channel'])

    custom_df  = _load_sheet('custom_channel_settings')
    output_df  = _load_sheet('output_settings')
    digital_df = _load_sheet('digital_settings')
    if 'Units' not in output_df.columns:
        output_df['Units'] = 'V'

    return df, selected_daq, custom_df, output_df, digital_df


def _row_device(row, fallback):
    """Device/module a settings row lives on (its own 'DAQ'), else fallback."""
    try:
        v = row.get('DAQ') if hasattr(row, 'get') else row['DAQ']
        s = "" if v is None else str(v).strip()
        if s and s.lower() != 'nan':
            return s
    except Exception:
        pass
    return fallback


def _timing_group(device_name):
    """Timing domain of a device: its chassis for a cDAQ/cRIO module, else the
    device itself. Modules of one chassis share one AI timing engine and so must
    be read in a single task; independent devices each get their own task."""
    name = str(device_name).strip()
    if not name:
        return name
    try:
        dev = nidaqmx.system.Device(name)
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


def _read_live(settings_df, selected_daq, n_avg=200):
    """Quick burst read — returns a list of per-channel float means in
    settings_df row order.

    Channels are grouped by timing domain (chassis) so a single task never spans
    independent devices (which NI-DAQmx forbids); modules of one chassis are read
    together in one synchronized task. Each channel's physical string uses its
    own 'DAQ' (falling back to selected_daq for legacy single-device files)."""
    n_ch = len(settings_df)
    results = [0.0] * n_ch

    rows = list(settings_df.iterrows())
    groups = {}   # timing-group key -> list of global row positions
    for pos, (_, row) in enumerate(rows):
        key = _timing_group(_row_device(row, selected_daq))
        groups.setdefault(key, []).append(pos)

    for _key, positions in groups.items():
        with nidaqmx.Task() as task:
            for pos in positions:
                _, row = rows[pos]
                dev = _row_device(row, selected_daq)
                tc = getattr(TerminalConfiguration,
                             str(row['Terminal Configuration']).upper())
                task.ai_channels.add_ai_voltage_chan(
                    f"{dev}/{row['Channel']}",
                    min_val=float(row['Min']),
                    max_val=float(row['Max']),
                    terminal_config=tc)
            task.timing.cfg_samp_clk_timing(
                1000,
                sample_mode=nidaqmx.constants.AcquisitionType.FINITE,
                samps_per_chan=n_avg)
            buf = np.zeros((len(positions), n_avg), dtype=np.float64)
            AnalogMultiChannelReader(task.in_stream).read_many_sample(
                buf, number_of_samples_per_channel=n_avg, timeout=5.0)
        for k, pos in enumerate(positions):
            results[pos] = float(np.mean(buf[k]))
    return results


# ---------------------------------------------------------------------------
# CalibrationWindow
# ---------------------------------------------------------------------------

class CalibrationWindow(QMainWindow):
    def __init__(self, settings_df, selected_daq, main_app=None,
                 custom_df=None, output_df=None, digital_df=None, parent=None):
        super().__init__(parent)
        self.settings_df  = settings_df.copy()
        self.selected_daq = selected_daq
        self.main_app     = main_app
        self.custom_df    = custom_df  if custom_df  is not None else pd.DataFrame()
        self.output_df    = output_df  if output_df  is not None else pd.DataFrame()
        self.digital_df   = digital_df if digital_df is not None else pd.DataFrame()

        self.No_input   = len(self.settings_df)
        self.No_custom  = len(self.custom_df)
        self.No_output  = len(self.output_df)
        self.No_digital = len(self.digital_df)

        # Calibration state (input channels only)
        self._errors     = {i: float(settings_df.loc[i, 'Error'])
                            for i in range(self.No_input)}
        self._calibrated = {i: (self._errors[i] != 0.0)
                            for i in range(self.No_input)}
        self._cal_mode   = False   # True = calibration columns visible

        # Widget refs
        self._reading_labels = {}   # ('input'|'custom'|'output'|'digital', idx) -> QLabel
        self._error_labels   = {}   # idx -> QLabel  (input channels in cal mode)
        self._cal_col_widgets = []  # all widgets shown only in cal mode
        self._cal_marks      = {}   # idx -> QLabel "✔"
        self._cal_header_labels = []  # header labels that appear in cal mode

        # Digital-output toggles: idx -> QPushButton, plus the state we believe
        # each line is in. State is refreshed from the live reading pushed in by
        # the main app, so an external writer (CT / FF) can't leave the button
        # lying about the line.
        self._do_buttons = {}
        self._do_states  = {}
        self._do_allowed = False
        self._do_tooltip = "Start the DAQ to switch digital outputs"

        self.setWindowTitle("Channels — View / Calibrate")
        self._build_ui()
        self.adjustSize()

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def _section_label(self, text):
        lbl = QLabel(text)
        # Neutral grey rather than the pale blue-grey it used to be, and the same
        # in every theme so the section bands read identically in Light and
        # Instrument. Text is near-black because #2c3e50 on this grey is only
        # about 3.8:1, which is under the 4.5:1 usually wanted for body text.
        lbl.setStyleSheet(
            "background:#c1c1c1; color:#101010; font-weight:bold; padding:2px 4px;" 
        )
        lbl.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        return lbl

    def _header_label(self, text):
        lbl = QLabel(f"<b>{text}</b>")
        lbl.setAlignment(Qt.AlignCenter)
        return lbl

    def _reading_label(self):
        lbl = QLabel("—")
        lbl.setFrameStyle(QFrame.Box)
        lbl.setAlignment(Qt.AlignCenter)
        lbl.setMinimumWidth(80)
        return lbl

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setSpacing(4)
        outer.setContentsMargins(8, 8, 8, 8)

        # ── scrollable grid area ───────────────────────────────────────
        grid_widget = QWidget()
        self._grid = QGridLayout(grid_widget)
        self._grid.setHorizontalSpacing(6)
        self._grid.setVerticalSpacing(3)

        # Column stretch
        self._grid.setColumnStretch(0, 0)   # Ch
        self._grid.setColumnStretch(1, 3)   # Name
        self._grid.setColumnStretch(2, 2)   # Reading
        self._grid.setColumnStretch(3, 1)   # Units
        self._grid.setColumnStretch(4, 0)   # Cal mark (view) / Error value (cal)
        self._grid.setColumnStretch(5, 0)   # Zero  (cal only)
        self._grid.setColumnStretch(6, 0)   # Reset (cal only)

        row = [0]   # mutable counter

        def add_header_row():
            for col, txt in enumerate(['', '', '', '', '', '', '']):
                self._grid.addWidget(self._header_label(txt), row[0], col)
            row[0] += 1

        # ── Input channels ─────────────────────────────────────────────
        if self.No_input > 0:
            sec = self._section_label(f"Input Channels  ({self.No_input})")
            self._grid.addWidget(sec, row[0], 0, 1, 7)
            row[0] += 1
            add_header_row()
            # "Error Value" header — cal mode only
            err_hdr = self._header_label("Error Value")
            err_hdr.hide()
            self._grid.addWidget(err_hdr, row[0] - 1, 4)
            self._cal_header_labels.append(err_hdr)

            for i, r in self.settings_df.iterrows():
                self._build_input_row(i, r, row[0])
                row[0] += 1

        # ── Custom channels ────────────────────────────────────────────
        if self.No_custom > 0:
            sec = self._section_label(f"Custom Channels  ({self.No_custom})")
            self._grid.addWidget(sec, row[0], 0, 1, 7)
            row[0] += 1
            add_header_row()
            for i, r in self.custom_df.iterrows():
                self._build_readonly_row('custom', i, r, row[0])
                row[0] += 1

        # ── Output channels ────────────────────────────────────────────
        if self.No_output > 0:
            sec = self._section_label(f"Output Channels  ({self.No_output})")
            self._grid.addWidget(sec, row[0], 0, 1, 7)
            row[0] += 1
            add_header_row()
            for i, r in self.output_df.iterrows():
                self._build_readonly_row('output', i, r, row[0])
                row[0] += 1

        # ── Digital channels ───────────────────────────────────────────
        if self.No_digital > 0:
            sec = self._section_label(f"Digital Channels  ({self.No_digital})")
            self._grid.addWidget(sec, row[0], 0, 1, 7)
            row[0] += 1
            add_header_row()
            for i, r in self.digital_df.iterrows():
                self._build_digital_row(i, r, row[0])
                row[0] += 1

        outer.addWidget(grid_widget)

        # ── Bottom button ──────────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        # "Reset All" — visible only in calibration mode, left of Save Calibration
        self._reset_all_btn = QPushButton("Reset All")
        self._reset_all_btn.setMinimumWidth(120)
        self._reset_all_btn.setFixedHeight(34)
        self._reset_all_btn.clicked.connect(self._do_reset_all)
        self._reset_all_btn.hide()
        btn_row.addWidget(self._reset_all_btn)
        self._cal_btn = QPushButton("Calibrate")
        self._cal_btn.setMinimumWidth(160)
        self._cal_btn.setFixedHeight(34)
        if self.No_input == 0:
            self._cal_btn.setEnabled(False)
            self._cal_btn.setToolTip("No input channels to calibrate")
        self._cal_btn.clicked.connect(self._toggle_cal_mode)
        btn_row.addWidget(self._cal_btn)
        btn_row.addStretch()
        outer.addLayout(btn_row)

    def _build_input_row(self, i, row, grid_row):
        g = self._grid

        ch_lbl = QLabel(str(row.get('Channel', '')))
        ch_lbl.setAlignment(Qt.AlignCenter)
        ch_lbl.setStyleSheet("color:#555; font-family:monospace;")
        g.addWidget(ch_lbl, grid_row, 0)

        g.addWidget(QLabel(str(row['Device Name'])), grid_row, 1)

        read_lbl = self._reading_label()
        g.addWidget(read_lbl, grid_row, 2)
        self._reading_labels[('input', i)] = read_lbl

        g.addWidget(QLabel(str(row.get('Units', ''))), grid_row, 3)

        # Col 4: calibrated mark (view) stacked with error label (cal mode)
        cal_mark = QLabel("✔" if self._calibrated.get(i) else "")
        cal_mark.setAlignment(Qt.AlignCenter)
        cal_mark.setStyleSheet("color:#27ae60; font-weight:bold; font-size:14px;")
        cal_mark.setToolTip("Calibrated this session" if self._calibrated.get(i) else "")
        g.addWidget(cal_mark, grid_row, 4)
        self._cal_marks[i] = cal_mark

        err_lbl = QLabel(f"{self._errors.get(i, 0.0):.6f}")
        err_lbl.setFrameStyle(QFrame.Box)
        err_lbl.setAlignment(Qt.AlignCenter)
        err_lbl.setMinimumWidth(100)
        err_lbl.hide()
        g.addWidget(err_lbl, grid_row, 4)
        self._error_labels[i] = err_lbl

        zero_btn = QPushButton("Zero")
        zero_btn.clicked.connect(lambda _, idx=i: self._do_zero(idx))
        zero_btn.hide()
        g.addWidget(zero_btn, grid_row, 5)

        reset_btn = QPushButton("Reset")
        reset_btn.clicked.connect(lambda _, idx=i: self._do_reset(idx))
        reset_btn.hide()
        g.addWidget(reset_btn, grid_row, 6)

        self._cal_col_widgets.extend([err_lbl, zero_btn, reset_btn])

    def _build_readonly_row(self, kind, i, row, grid_row):
        g = self._grid

        ch_lbl = QLabel(str(row.get('Channel', row.get('First Channel', ''))))
        ch_lbl.setAlignment(Qt.AlignCenter)
        ch_lbl.setStyleSheet("color:#555; font-family:monospace;")
        g.addWidget(ch_lbl, grid_row, 0)

        g.addWidget(QLabel(str(row['Device Name'])), grid_row, 1)

        read_lbl = self._reading_label()
        g.addWidget(read_lbl, grid_row, 2)
        self._reading_labels[(kind, i)] = read_lbl

        units = str(row.get('Units', 'V'))
        g.addWidget(QLabel(units), grid_row, 3)

    def _build_digital_row(self, i, row, grid_row):
        g = self._grid

        ch_lbl = QLabel(_shorten_channel(row.get('Channel', '')))
        ch_lbl.setAlignment(Qt.AlignCenter)
        ch_lbl.setStyleSheet("color:#555; font-family:monospace;")
        ch_lbl.setToolTip(str(row.get('Channel', '')))   # full name on hover
        g.addWidget(ch_lbl, grid_row, 0)

        name_lbl = QLabel(str(row['Device Name']))
        g.addWidget(name_lbl, grid_row, 1)

        read_lbl = self._reading_label()
        g.addWidget(read_lbl, grid_row, 2)
        self._reading_labels[('digital', i)] = read_lbl

        direction = str(row.get('Direction', ''))
        g.addWidget(QLabel(direction), grid_row, 3)

        if direction.strip().lower() == 'output':
            btn = QPushButton(self._do_glyph())
            btn.setCheckable(True)
            btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            btn.clicked.connect(lambda _, idx=i: self._toggle_digital_output(idx))
            # Left-aligned so shrinking the box doesn't leave it floating in the
            # middle of a wide cell.
            g.addWidget(btn, grid_row, 4, Qt.AlignLeft | Qt.AlignVCenter)
            self._do_buttons[i] = btn
            self._do_states[i]  = False
            self._refresh_do_button(i)

    # ------------------------------------------------------------------
    # Digital output toggles
    # ------------------------------------------------------------------

    # Bottom padding lifts the glyph: Qt centres the font's ascent/descent box,
    # not the visible ink, and the power symbol has no descender. Change the 2px
    # if it looks off-centre in your font.
    _DO_CSS = ("margin:0px; border:1px solid #999; border-radius:3px; "
               "background:transparent; font-weight:bold; padding:0px 0px 2px 0px;")
    _DO_ON_COLOR  = "#27ae60"   # green  = line high
    _DO_OFF_COLOR = "#c0392b"   # red    = line low
    _DO_OFF_GRAY  = "#9a9a9a"   # grey   = not switchable right now
    _do_glyph_cache = None

    @classmethod
    def _do_glyph(cls):
        """The power symbol, or the best substitute the installed fonts have.

        U+23FB is the IEC power glyph but it is missing from some Windows font
        stacks, where it renders as a tofu box. Checking with the actual font
        avoids that, rather than assuming.
        """
        if cls._do_glyph_cache is None:
            from PyQt5.QtGui import QFontMetrics
            fm = QFontMetrics(QApplication.font())
            cls._do_glyph_cache = "\u25cf"          # ● filled circle fallback
            for ch in ("\u23fb", "\u23fc", "\u2b58"):   # ⏻  ⏼  ⭘
                try:
                    if fm.inFont(ch):
                        cls._do_glyph_cache = ch
                        break
                except Exception:
                    pass
        return cls._do_glyph_cache

    def _size_do_buttons(self):
        """Fit each toggle inside its own row, measured after layout.

        Sized against the Reading field actually next to it rather than a probe
        widget: the laid-out height is what decides the row, and it differs from
        a fresh widget's sizeHint under some styles. One pixel under that height
        guarantees the button can never be the tallest thing in the row.
        """
        for idx, btn in self._do_buttons.items():
            ref = self._reading_labels.get(('digital', idx))
            h = ref.height() if ref is not None and ref.height() > 0 else 20
            side = max(12, h - 1)
            f = btn.font()
            f.setPixelSize(max(8, side - 7))   # glyph sits inside the box
            btn.setFont(f)
            btn.setFixedSize(side, side)
            self._refresh_do_button(idx)

    def _refresh_do_button(self, idx):
        """Colour, tooltip and enabled state from the line state + permission."""
        btn = self._do_buttons.get(idx)
        if btn is None:
            return
        on = bool(self._do_states.get(idx))
        btn.blockSignals(True)
        btn.setChecked(on)
        btn.blockSignals(False)
        btn.setEnabled(self._do_allowed)
        if not self._do_allowed:
            color, tip = self._DO_OFF_GRAY, self._do_tooltip
        else:
            color = self._DO_ON_COLOR if on else self._DO_OFF_COLOR
            tip = "On — click to switch off" if on else "Off — click to switch on"
        # setStyleSheet replaces the whole sheet, so the metrics have to be
        # re-stated with the colour.
        btn.setStyleSheet(f"{self._DO_CSS} color:{color};")
        btn.setToolTip(tip)

    def set_digital_output_allowed(self, allowed: bool, tooltip: str = ""):
        """Called by the main app to gate the digital-output toggles.

        Mirrors set_calibrate_allowed. The toggles are live only while the DAQ
        is acquiring and no app that drives the digital bus is open, because a
        second writer on the same line either fails with a reserved-resource
        error or fights the app that owns it.
        """
        self._do_allowed = bool(allowed)
        self._do_tooltip = tooltip or "Start the DAQ to switch digital outputs"
        for idx in self._do_buttons:
            self._refresh_do_button(idx)

    def _toggle_digital_output(self, idx):
        """Drive the line to the opposite of its current state."""
        if not self._do_allowed:
            return
        try:
            row = self.digital_df.loc[idx]
        except Exception:
            return
        target = not bool(self._do_states.get(idx))
        if self._write_digital_output(row, target):
            self._do_states[idx] = target
        self._refresh_do_button(idx)

    def _write_digital_output(self, row, state: bool) -> bool:
        """Write one DO line with a short-lived task — the same pattern the main
        app uses to poll DI lines, so nothing here holds a line open between
        clicks. Returns True on success."""
        ch = str(row.get('Channel', '')).strip()
        if not ch:
            return False
        dev = _row_device(row, self.selected_daq)
        try:
            with nidaqmx.Task() as t:
                t.do_channels.add_do_chan(f"{dev}/{ch}")
                t.write(bool(state))
        except Exception as e:
            QMessageBox.warning(self, "Digital Output Error",
                                f"Could not write {dev}/{ch}:\n{e}")
            return False

        # Keep the main app's cached state (and therefore this window's reading
        # label) in step, and let it remember the line so Stop can clear it.
        app = self.main_app
        if app is not None:
            for meth, args in (('notify_digital_state', (ch, bool(state))),
                               ('register_manual_do',   (ch, bool(state)))):
                fn = getattr(app, meth, None)
                if fn is not None:
                    try:
                        fn(*args)
                    except Exception:
                        pass
        return True

    def all_digital_outputs_off(self, write: bool = True):
        """Drive every output line low and reset the toggles.

        Called by the main app on Stop. `write=False` only resyncs the buttons,
        for when the caller has already cleared the lines itself.
        """
        for idx, row in self.digital_df.iterrows():
            if idx not in self._do_buttons:
                continue
            if write and self._do_states.get(idx):
                self._write_digital_output(row, False)
            self._do_states[idx] = False
            self._refresh_do_button(idx)

    def showEvent(self, event):
        super().showEvent(event)
        self._size_do_buttons()
        # Ask the main app to (re)apply gating now that this window is visible.
        # Its notifier ignores an invisible window, so anything it sent while the
        # window was still hidden never landed.
        app = self.main_app
        notify = getattr(app, '_notify_cal_window_app_state', None) if app else None
        if notify is not None:
            try:
                notify()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Calibration mode toggle
    # ------------------------------------------------------------------

    def _toggle_cal_mode(self):
        if not self._cal_mode:
            self._enter_cal_mode()
        else:
            self._save_all_calibrations()

    def _enter_cal_mode(self):
        self._cal_mode = True
        # Show error/zero/reset widgets for every input row
        for w in self._cal_col_widgets:
            w.show()
        # Hide the "✔" marks — error labels take that cell
        for i in range(self.No_input):
            self._cal_marks[i].hide()
        # Show the "Error Value" header
        for lbl in self._cal_header_labels:
            lbl.show()
        self._reset_all_btn.show()
        self._cal_btn.setText("Save Calibration")
        self.adjustSize()

    def _exit_cal_mode(self):
        self._cal_mode = False
        for w in self._cal_col_widgets:
            w.hide()
        for i in range(self.No_input):
            self._cal_marks[i].show()
        for lbl in self._cal_header_labels:
            lbl.hide()
        self._reset_all_btn.hide()
        self._cal_btn.setText("Calibrate")
        self.adjustSize()

    # ------------------------------------------------------------------
    # Calibration actions
    # ------------------------------------------------------------------

    def _do_zero(self, idx):
        val = self._get_current_reading(idx)
        if val is None:
            return
        self._errors[idx] = val
        self._error_labels[idx].setText(f"{val:.6f}")

    def _do_reset(self, idx):
        self._errors[idx] = 0.0
        self._error_labels[idx].setText("0.000000")

    def _do_reset_all(self):
        """Reset every input channel's error offset to zero."""
        for idx in range(self.No_input):
            self._do_reset(idx)

    def _get_current_reading(self, idx):
        app = self.main_app
        if app is not None and getattr(app, 'continue_running', False) \
                and getattr(app, 'total_written', 0) > 0:
            n_avg = min(100, app.total_written)
            return float(np.mean(app.rb_tail(idx, n_avg)))
        try:
            return _read_live(self.settings_df, self.selected_daq, n_avg=200)[idx]
        except Exception as e:
            QMessageBox.warning(self, "DAQ Error", str(e))
            return None

    def _save_all_calibrations(self):
        """Apply every pending error offset immediately and return to view mode."""
        app = self.main_app

        for idx in range(self.No_input):
            new_err = self._errors[idx]
            self._calibrated[idx] = (new_err != 0.0)
            mark = self._cal_marks[idx]
            mark.setText("✔" if self._calibrated[idx] else "")
            mark.setToolTip("Calibrated this session" if self._calibrated[idx] else "")

        # Push into main-app globals so the live run uses the new offsets
        if app is not None:
            try:
                import __main__ as _m
                for idx in range(self.No_input):
                    _m.default_settings.loc[idx, 'Error'] = self._errors[idx]
                    _m.plot_options.loc[idx, 'Error']      = self._errors[idx]
            except Exception:
                pass
            # Rebuild numpy calibration arrays (hot-path in run_task)
            if hasattr(app, 'calib_bias') and hasattr(app, 'calib_mx'):
                try:
                    import __main__ as _m
                    mx   = _m.default_settings['Mx'].to_numpy(dtype='float32')
                    b    = _m.default_settings['b'].to_numpy(dtype='float32')
                    err  = _m.default_settings['Error'].to_numpy(dtype='float32')
                    app.calib_mx   = mx
                    app.calib_bias = b - err
                except Exception:
                    pass

        self._exit_cal_mode()

    # ------------------------------------------------------------------
    # Live reading update — called by VoltageContinuousInput every frame
    # ------------------------------------------------------------------

    def set_calibrate_allowed(self, allowed: bool, tooltip: str = ""):
        """Called by the main app to gate the Calibrate button when CT/FF is open."""
        self._cal_btn.setEnabled(allowed and self.No_input > 0)
        self._cal_btn.setToolTip(tooltip)

    def update_reading(self, kind, idx, value):
        """kind: 'input' | 'custom' | 'output' | 'digital'"""
        lbl = self._reading_labels.get((kind, idx))
        if lbl is None:
            return
        is_nan = (value != value)   # fastest NaN check
        if kind == 'digital':
            # An output line can also be driven by CT / FF, so trust the reading
            # over what this window last set.
            if not is_nan and idx in self._do_buttons:
                on = value >= 0.5
                if on != bool(self._do_states.get(idx)):
                    self._do_states[idx] = on
                    self._refresh_do_button(idx)
            if is_nan:
                lbl.setText("—")
                lbl.setStyleSheet("color: #888;")
            elif value >= 0.5:
                lbl.setText("On")
                lbl.setStyleSheet("color: #27ae60; font-weight: bold;")
            else:
                lbl.setText("Off")
                lbl.setStyleSheet("color: #888; font-weight: normal;")
        else:
            lbl.setStyleSheet("")
            lbl.setText("—" if is_nan else f"{value:.4f}")


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle('Fusion')

    try:
        settings_df, selected_daq, custom_df, output_df, digital_df = _read_settings()
    except FileNotFoundError:
        QMessageBox.critical(None, "Settings Not Found",
                             "default_settings.xlsx not found.\n"
                             "Run the main application first.")
        sys.exit(1)
    except Exception as e:
        QMessageBox.critical(None, "Settings Error", str(e))
        sys.exit(1)

    try:
        daqList = nidaqmx.system.System().devices.device_names
    except Exception:
        daqList = []

    if not daqList:
        QMessageBox.warning(None, "No DAQ", "No NI-DAQ device detected.")
    else:
        # Check every device referenced by the input channels, not just the first.
        referenced = set()
        try:
            if 'DAQ' in settings_df.columns:
                for v in settings_df['DAQ'].tolist():
                    s = "" if v is None else str(v).strip()
                    if s and s.lower() != 'nan':
                        referenced.add(s)
        except Exception:
            pass
        if not referenced:
            referenced = {selected_daq}
        missing = sorted(d for d in referenced if d not in daqList)
        if missing:
            QMessageBox.warning(None, "DAQ Mismatch",
                                "Settings reference device(s) that are not connected:\n"
                                f"    {', '.join(missing)}\n\n"
                                f"Connected: {', '.join(daqList)}.")

    window = CalibrationWindow(settings_df, selected_daq,
                               main_app=None,
                               custom_df=custom_df,
                               output_df=output_df,
                               digital_df=digital_df)
    window.show()
    sys.exit(app.exec_())
