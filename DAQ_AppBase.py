
"""
DAQ_AppBase.py
--------------
Shared foundation for apps that drive analog output and run a test worker.

Extracted from DAQ_FlowFailure and DAQ_ProportionalSweep, which had grown
copies of the same helpers. The point is not line count — for two apps it is
roughly break-even — it is that a copy can silently drift from its original.
That already happened once: Proportional Sweep called a main-window method that
did not exist ('notify_analog_output' instead of 'notify_output_voltage') and,
because the call was guarded by hasattr, it failed silently and no AO value ever
reached the plot. With one implementation that class of bug cannot recur.

Members deliberately keep the leading-underscore names the apps already used,
so subclasses call them exactly as before.

Subclass hooks (all optional, all no-ops by default):
    _extra_lock()             extra widgets to disable when the app opens
    _extra_unlock()           undo of the above
    _extra_shutdown()         hardware to release beyond zeroing AO
    _release_blocked_workers()  wake a worker parked on an Event, for Force Stop
    _on_close_cleanup()       app-specific teardown inside closeEvent
"""

import threading
import traceback

import numpy as np
import pandas as pd
import nidaqmx
from nidaqmx.stream_writers import AnalogSingleChannelWriter

from PyQt5.QtWidgets import (
    QDialog, QGroupBox, QLabel, QFrame, QMessageBox
)
from PyQt5.QtCore import Qt, pyqtSignal, QObject

# DAQ_Settings owns the channel-type tables (which nidaqmx call opens a channel
# of each type, and what unit its range is in), so the dispatch lives there
# rather than being duplicated per app.
from DAQ_Settings import add_ao_channel, row_channel_type


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def write_dc(selected_daq, channel_str, value, min_v, max_v, type_label):
    """Write one clamped DC level to an AO channel and close the task.

    `type_label` is the row's 'Output Type' ('Voltage', 'Current'), which
    decides the nidaqmx call. `value`, min_v and max_v are all in that type's
    unit — volts for a voltage output, amps for a current one — so a 4-20 mA
    channel is driven in amps, not volts. It is a required argument rather than
    a defaulted one so a call site cannot silently keep driving volts into a
    current card.
    """
    v = float(np.clip(value, min_v, max_v))
    with nidaqmx.Task() as t:
        add_ao_channel(t, f"{selected_daq}/{channel_str}", type_label,
                       min_val=min_v, max_val=max_v)
        AnalogSingleChannelWriter(t.out_stream).write_one_sample(v)


def safe_str(val, fallback=''):
    """Convert a DataFrame cell to string, treating NaN/None as fallback."""
    if val is None:
        return fallback
    try:
        if pd.isna(val):
            return fallback
    except Exception:
        pass
    s = str(val).strip()
    return s if s else fallback


def hline():
    line = QFrame()
    line.setFrameShape(QFrame.HLine)
    line.setFrameShadow(QFrame.Sunken)
    return line


class GroupBox(QGroupBox):
    """QGroupBox that can float a bold label, or an arbitrary widget, to the
    right on its title line.

    set_corner_text shows the AO channel / device name (e.g. "ao0  (Supply)")
    aligned with the section title. set_corner_widget parks an interactive
    widget there instead.
    """

    def __init__(self, title, parent=None):
        super().__init__(title, parent)
        self._corner_label = QLabel("", self)
        self._corner_label.setStyleSheet("font-weight: bold;")
        self._corner_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._corner_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._corner_label.hide()
        self._corner_widget = None

    def set_corner_text(self, text):
        self._corner_label.setText(text or "")
        self._corner_label.setVisible(bool(text))
        self._reposition_corner()

    def set_corner_widget(self, widget):
        """Park an interactive widget on the title line, right aligned. The
        group's top margin is widened so it does not overlap the first row."""
        self._corner_widget = widget
        widget.setParent(self)
        widget.raise_()
        h = widget.sizeHint().height()
        lay = self.layout()
        if lay is not None:
            m = lay.contentsMargins()
            lay.setContentsMargins(m.left(), max(m.top(), h + 4),
                                   m.right(), m.bottom())
        self._reposition_corner()

    def _reposition_corner(self):
        margin = 10
        self._corner_label.adjustSize()
        x = self.width() - self._corner_label.width() - margin
        self._corner_label.move(max(margin, x), 0)
        self._corner_label.raise_()
        w = self._corner_widget
        if w is not None:
            w.adjustSize()
            x = self.width() - w.width() - margin
            w.move(max(margin, x), 0)
            w.raise_()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._reposition_corner()


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------

class AppSignals(QObject):
    """Signals every AO test dialog needs.

    Qt signals must exist on the class, so an app that needs more subclasses
    this and adds them:

        class _MySignals(AppSignals):
            my_extra_sig = pyqtSignal(bool)
    """
    status_changed  = pyqtSignal(str)
    enable_run      = pyqtSignal(bool)
    test_finished   = pyqtSignal()
    stop_daq_sig    = pyqtSignal()   # fires on UI thread to stop the DAQ task
    controls_unlock = pyqtSignal()   # fires on UI thread to re-enable controls


# ---------------------------------------------------------------------------
# Base dialog
# ---------------------------------------------------------------------------

class AOTestDialog(QDialog):
    """Base for dialogs that drive AO and run a background test worker."""

    SIGNALS_CLASS = AppSignals

    # Shown on the main window's Run/Stop while this dialog is open.
    APP_NAME = "this app"

    def __init__(self, main_window, default_settings_output, default_settings_input,
                 default_settings_dig, selected_daq, sample_rate, parent=None,
                 title=None):
        super().__init__(parent, Qt.Window)
        if title:
            self.setWindowTitle(title)
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)

        self.main_window             = main_window
        self.default_settings_output = default_settings_output
        self.default_settings_input  = default_settings_input
        self.default_settings_dig    = default_settings_dig
        self.selected_daq            = selected_daq
        self.sample_rate             = float(sample_rate) if sample_rate else 1000.0

        self._is_running    = False
        self._abort         = False
        self._worker_thread = None

        self._signals = self.SIGNALS_CLASS()
        self._signals.status_changed.connect(self._on_status_changed)
        self._signals.stop_daq_sig.connect(self._stop_daq)

    # ------------------------------------------------------------------
    # AO row helpers
    # ------------------------------------------------------------------

    def _ao_limits(self, row):
        """(min_val, max_val) for an AO row, with a sane fallback.

        The workbook's 'Min'/'Max' are in the unit named by its 'Range Unit',
        which is volts for a voltage output and amps for a current one. These
        feed nidaqmx min_val/max_val, which expect that same unit.
        """
        try:
            min_v = float(row.get('Min', 0.0))
            max_v = float(row.get('Max', 5.0))
        except (TypeError, ValueError):
            min_v, max_v = 0.0, 5.0
        if max_v <= min_v:
            min_v, max_v = 0.0, 5.0
        return min_v, max_v

    def _ao_type(self, row):
        """Output type a row names ('Voltage', 'Current'), else the default.

        Mirrors _ao_limits/_ao_device: the row is the authority, so nothing
        here assumes the card is a voltage card.
        """
        return row_channel_type(row, 'ao')

    def _ao_device(self, row):
        """Device/module a row lives on (its own 'DAQ'), else selected_daq."""
        try:
            v = row.get('DAQ')
            s = "" if v is None else str(v).strip()
            if s and s.lower() != 'nan':
                return s
        except Exception:
            pass
        return self.selected_daq

    def _notify_output(self, ch, voltage):
        """Publish the voltage being driven so the main window's output display
        and plot reflect it (AO is output-only — there is no read-back).

        voltage=None releases the override back to the Function Generator.
        Best effort; never fatal.
        """
        mw = getattr(self, 'main_window', None)
        if mw is not None and hasattr(mw, 'notify_output_voltage') and ch:
            try:
                mw.notify_output_voltage(ch, voltage)
            except Exception:
                pass

    def _read_gauge_from_rb(self, rb_idx, n_avg=20):
        """Mean of the latest n_avg samples for rb_idx from the main ring buffer.

        Read through the ring buffer rather than opening a task, because the
        main acquisition task already owns those channels and a second reader
        would fail with -50103.
        """
        mw = self.main_window
        if mw is None or not getattr(mw, 'continue_running', False):
            return None
        try:
            tail = mw.rb_tail(rb_idx, n_avg)
            if tail is None or len(tail) == 0:
                return None
            return float(np.mean(tail))
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Main window lock / unlock
    # ------------------------------------------------------------------

    def _lock_main_window(self):
        """Take over Run/Stop and let the registry lock out conflicting apps.

        Which other apps get disabled follows from this app's APP_SPEC claims,
        so no app is named here.
        """
        mw = self.main_window
        if mw is None:
            return
        if hasattr(mw, 'refresh_app_availability'):
            mw.refresh_app_availability()
        ow = getattr(mw, 'outputSettingsWidget', None)
        if ow:
            ow.start_button.setEnabled(False)
            ow.start_button.setToolTip(f"Close {self.APP_NAME} to use Run")
            ow.stop_button.setEnabled(False)
            ow.stop_button.setToolTip(f"Close {self.APP_NAME} to use Stop")
        self._extra_lock()
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
        self._extra_unlock()
        self._unlock_axis_controls(mw)

    # Hooks — override as needed.
    def _extra_lock(self):    pass
    def _extra_unlock(self):  pass
    def _lock_axis_controls(self, mw=None):   pass
    def _unlock_axis_controls(self, mw=None): pass

    # ------------------------------------------------------------------
    # DAQ task control
    # ------------------------------------------------------------------

    def _stop_daq(self):
        """Stop the main DAQ task. Runs on the UI thread via stop_daq_sig.

        stop_task() ends with enable_buttons(), which would hand the main
        window's controls back while this dialog is still open, so the locks are
        re-applied once task_stopped has been processed.
        """
        mw = self.main_window
        if mw is None:
            return
        try:
            mw.signals.task_stopped.connect(self._on_task_stopped_relock,
                                            type=Qt.SingleShotConnection)
        except AttributeError:
            # Qt older than 6.4 has no SingleShotConnection — emulate it.
            def _one_shot():
                self._on_task_stopped_relock()
                try:
                    mw.signals.task_stopped.disconnect(_one_shot)
                except Exception:
                    pass
            mw.signals.task_stopped.connect(_one_shot)
        except Exception:
            pass

        try:
            mw.stop_task()
        except AttributeError:
            pass

    def _on_task_stopped_relock(self):
        self._lock_main_window()
        self._lock_axis_controls()

    # ------------------------------------------------------------------
    # Status / abort / shutdown
    # ------------------------------------------------------------------

    def _on_status_changed(self, text):
        self._status_label.setText(text)

    def _force_stop(self):
        if not self._is_running:
            return
        self._abort = True
        # Wake anything parked on an Event, or Force Stop cannot reach the worker.
        self._release_blocked_workers()
        self._signals.status_changed.emit("Aborting… please wait")
        threading.Thread(target=self._safe_shutdown, daemon=True).start()
        self._signals.stop_daq_sig.emit()

    def _release_blocked_workers(self):
        pass

    def _safe_shutdown(self):
        """Zero every configured AO channel, then run app-specific teardown.

        Called on close AND on force-stop. Safe whether or not the worker is
        still alive — worst case is a brief task conflict, which is caught.
        """
        if self._worker_thread is not None and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=2.0)

        ao_df = self.default_settings_output
        if ao_df is not None:
            for _, row in ao_df.iterrows():
                ch = safe_str(row.get('Channel'), '')
                if not ch:
                    continue
                min_v, max_v = self._ao_limits(row)
                zero = float(np.clip(0.0, min_v, max_v))
                try:
                    write_dc(self._ao_device(row), ch, zero, min_v, max_v,
                             self._ao_type(row))
                    # Hand the display override back to the Function Generator.
                    self._notify_output(ch, None)
                except Exception:
                    print(f"{self.APP_NAME} shutdown: could not zero AO {ch}:\n"
                          + traceback.format_exc())

        self._extra_shutdown()

    def _extra_shutdown(self):
        pass

    # ------------------------------------------------------------------
    # Close
    # ------------------------------------------------------------------

    def closeEvent(self, event):
        """Refuse to close mid-test; otherwise release hardware and hide.

        These dialogs hide rather than destroy, so state survives reopening.
        """
        if self._is_running:
            QMessageBox.warning(
                self, "Test Running",
                "A test is in progress.\nUse Force Stop to abort, then close."
            )
            event.ignore()
            return
        self._safe_shutdown()
        self._on_close_cleanup()
        self._unlock_main_window()
        self.hide()
        event.ignore()

    def _on_close_cleanup(self):
        pass
