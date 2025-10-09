import sys
import os
import numpy as np
import collections
import pandas as pd
import subprocess
import threading
import time
from scipy.signal import butter, sosfilt, sosfilt_zi, medfilt
from scipy.ndimage import uniform_filter1d

import nidaqmx
from nidaqmx.constants import TerminalConfiguration
from nidaqmx.stream_readers import AnalogMultiChannelReader  # ★
from nidaqmx.stream_writers import AnalogSingleChannelWriter
import nidaqmx.constants as ni

from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, 
                             QGroupBox, QLabel, QLineEdit, QComboBox, QCheckBox, QPushButton,
                             QSpinBox, QDoubleSpinBox, QTabWidget, QFileDialog, QScrollArea,
                             QGridLayout, QSizePolicy, QDialog, QRadioButton, QButtonGroup)
from PyQt5.QtCore import Qt, QTimer, QObject, pyqtSignal  # ★
from PyQt5.QtGui import QColor

import pyqtgraph as pg
from pyqtgraph import PlotWidget, mkPen

# Initialize DAQ system
daqSys = nidaqmx.system.System()
daqList = daqSys.devices.device_names
daqObjects = [daqSys.devices[name] for name in daqList]

if len(daqList) == 0:
    raise ValueError('No DAQ detected, check connection.')


def read_defaults(xlsx_path='default_settings.xlsx'):
    def ensure_float_column(df, col, default=0.0):
        # Create the column if missing, then coerce to float
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
        """Return setting value or None. If numeric=True, coerce to float, NaN -> None."""
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

    # ---------- Channels ----------
    default_settings = read_sheet('default_settings')
    default_settings = filter_selected(default_settings)
    ensure_float_column(default_settings, 'Error', 0.0)

    default_settings_custom = read_sheet('custom_channel_settings')
    default_settings_custom = filter_selected(default_settings_custom)

    default_settings_output = read_sheet('output_settings')
    default_settings_output = filter_selected(default_settings_output)
    ensure_float_column(default_settings_output, 'Error', 0.0)

    # ---------- Expert settings ----------
    expert_settings = read_sheet('expert_settings')

    sample_rate     = get_setting(expert_settings, "Sample Rate (Hz)", numeric=True)
    refresh_rate    = get_setting(expert_settings, "Refresh Rate (Hz)", numeric=True)
    max_buffer_size = get_setting(expert_settings, "Max Buffer Size",   numeric=True)
    max_plot_points = get_setting(expert_settings, "Max Plot Points",   numeric=True)
    decimation_per  = get_setting(expert_settings, "Decimation %",      numeric=True)

    filter_type   = get_setting(expert_settings, "Filter Type",        numeric=False)
    filter_order  = get_setting(expert_settings, "Filter Order",       numeric=True)
    filter_cutoff = get_setting(expert_settings, "Filter Cutoff (Hz)", numeric=True)
    filter_fh     = get_setting(expert_settings, "Filter fh (Hz)",     numeric=True)

    # ---------- Convenience combos ----------
    combined_settings = pd.concat(
        [default_settings, default_settings_custom, default_settings_output],
        ignore_index=True
    )
    plot_options = pd.concat([default_settings, default_settings_custom], ignore_index=True)

    # Channel counts
    No_chans      = len(default_settings)
    No_chans_out  = len(default_settings_output)
    No_chans_cust = len(default_settings_custom)

    # Selected DAQ device (require at least one selected row among input/output)
    if len(default_settings) == 0 and len(default_settings_output) == 0:
        raise ValueError("No selected channels found in XLSX (Selected Channel == 1).")

    selected_daq = (
        default_settings['DAQ'].iloc[0]
        if len(default_settings) > 0
        else default_settings_output['DAQ'].iloc[0]
    )
    daq = nidaqmx.system.Device(selected_daq)

    return (
        default_settings, default_settings_output, default_settings_custom,
        combined_settings, plot_options, No_chans, No_chans_out, No_chans_cust,
        daq, selected_daq,
        sample_rate, refresh_rate, max_buffer_size, max_plot_points, decimation_per,
        filter_type, filter_order, filter_cutoff, filter_fh
    )

# Usage:
(
    default_settings, default_settings_output, default_settings_custom,
    combined_settings, plot_options, No_chans, No_chans_out, No_chans_cust,
    daq, selected_daq,
    sample_rate, refresh_rate, max_buffer_size, max_plot_points, decimation_per,
    filter_type, filter_order, filter_cutoff, filter_fh
) = read_defaults()




if daq.name in daqList:
    daq.reset_device()
else:
    try:
        subprocess.run(['python', 'DAQ_Settings.py'], check=True)
    except subprocess.CalledProcessError as e:
        print(f"Error running subprocess: {e}")
    if daqObjects and hasattr(daqObjects[0], "reset_device"):
        daqObjects[0].reset_device()
    else:
        print("Warning: No valid DAQ object found to reset.")

class DAQSignals(QObject):
    data_ready = pyqtSignal(object)  # ★
    task_stopped = pyqtSignal()      # ★

class GraphData(QGroupBox):
    def __init__(self, parent=None):
        super().__init__("Graph Data")
        self.parent = parent
        self.plot_items = {}   # channel_name -> PlotDataItem
        self.unit_axes = {}    # unit -> {"vb": ViewBox or None (for left), "axis": AxisItem or None}
        self.axis_order = []   # keep insertion order of units
        self.axis_right_offset_px = 50  # spacing between right-side axes
        self.create_widgets()

    def create_widgets(self):
        layout = QVBoxLayout(self)
        self.plot_widget = pg.PlotWidget()
        self.plot_widget.setBackground('w')
        self.plot_widget.showGrid(x=True, y=True)
        self.plot_widget.addLegend()
        layout.addWidget(self.plot_widget)

        # main (left) viewbox for the first unit
        self.main_vb = self.plot_widget.getViewBox()
        self.plot_widget.plotItem.showAxis('left', True)
        self.plot_widget.plotItem.showAxis('right', False)  # we'll add our own on the right

        # keep extra VBs sized with the main one
        self.main_vb.sigResized.connect(self._update_extra_views)

    def clear_plots(self):
        # Remove any curves from main view
        try:
            self.plot_widget.clear()
        except Exception:
            pass

        # Remove extra right-side axes and their viewboxes
        for unit, pack in list(self.unit_axes.items()):
            vb = pack.get('vb', None)
            ax = pack.get('axis', None)
            if vb is not None:
                # Remove all items from this VB
                for item in list(vb.addedItems.copy() if hasattr(vb, 'addedItems') else []):
                    try: vb.removeItem(item)
                    except Exception: pass
                try:
                    self.plot_widget.scene().removeItem(vb)
                except Exception:
                    pass
            if ax is not None:
                try:
                    self.plot_widget.plotItem.layout.removeItem(ax)
                except Exception:
                    pass

        # Reset our bookkeeping
        self.plot_items = {}
        self.unit_axes = {}
        self.axis_order = []

        # Reset base axes visibility
        try:
            self.plot_widget.plotItem.showAxis('left', True)
            self.plot_widget.plotItem.showAxis('right', False)
            self.plot_widget.plotItem.getAxis('left').setLabel("")  # no unit until first series added
        except Exception:
            pass

        # Clear legend entries (if present)
        if getattr(self.plot_widget.plotItem, 'legend', None) is not None:
            try:
                self.plot_widget.plotItem.legend.clear()
            except Exception:
                pass
            
    def _ensure_legend(self):
        """Ensure there's a single LegendItem we can add curves to, regardless of viewbox."""
        if getattr(self.plot_widget.plotItem, 'legend', None) is None:
            self.plot_widget.addLegend()  # returns a LegendItem attached to the plotItem
        return self.plot_widget.plotItem.legend


    # ---- multi-axis helpers ----
    def _ensure_axis_for_unit(self, unit: str, color=None):
        """
        Ensure an axis + viewbox exists for this unit.
        First unit uses LEFT axis on the main viewbox.
        Others get a RIGHT-side AxisItem + ViewBox with its own Y range.
        """
        if unit in self.unit_axes:
            ax = self.unit_axes[unit]['axis']
            if color is not None and ax is not None:
                try:
                    ax.setPen(pg.mkPen(color)); ax.setTextPen(pg.mkPen(color))
                except Exception:
                    pass
            return

        if not self.axis_order:
            # First unit: left axis
            left_axis = self.plot_widget.plotItem.getAxis('left')
            left_axis.setLabel(unit)
            if color is not None:
                try:
                    left_axis.setPen(pg.mkPen(color)); left_axis.setTextPen(pg.mkPen(color))
                except Exception:
                    pass
            self.unit_axes[unit] = {"vb": None, "axis": None}
            self.axis_order.append(unit)
            return

        # Create a new ViewBox + AxisItem on the right
        vb = pg.ViewBox()
        vb.enableAutoRange(axis=pg.ViewBox.YAxis, enable=True)
        vb.setXLink(self.main_vb)
        self.plot_widget.scene().addItem(vb)

        axis = pg.AxisItem('right')
        axis.setLabel(unit)
        if color is not None:
            try:
                axis.setPen(pg.mkPen(color)); axis.setTextPen(pg.mkPen(color))
            except Exception:
                pass

        # Place in a new layout column to the RIGHT (col index grows with each extra unit)
        # Base plotItem layout: row=2 holds the plot area; col=2 is central plot; col=3 is default right axis.
        # We'll append starting at col=4+
        existing_right_axes = sum(1 for u in self.axis_order if self.unit_axes[u]['axis'] is not None)
        col = 4 + existing_right_axes
        layout = self.plot_widget.plotItem.layout
        layout.addItem(axis, 2, col)

        # Give each extra axis a fixed width so labels are visible
        try:
            layout.setColumnFixedWidth(col, 60)
        except Exception:
            pass

        # Link axis <-> viewbox and align geometry to the main view
        axis.linkToView(vb)
        vb.setGeometry(self.main_vb.sceneBoundingRect())
        self._update_extra_views()  # force geometry sync now

        self.unit_axes[unit] = {"vb": vb, "axis": axis}
        self.axis_order.append(unit)


    def _update_extra_views(self):
        # keep all extra viewboxes aligned to the main one
        for unit, pack in self.unit_axes.items():
            vb = pack['vb']
            if vb is None:
                continue
            vb.setGeometry(self.main_vb.sceneBoundingRect())
            vb.linkedViewChanged(self.main_vb, vb.XAxis)

    # ---- plotting API ----
    def update_plot(self, channel_name, x_data, y_data, color, pen_mode=False, unit: str = None):
        """Create/update a curve routed to the axis for 'unit'."""
        if unit is None:
            unit = ""

        # make sure the unit axis exists before creating a curve
        self._ensure_axis_for_unit(unit, color=color)

        # choose destination ViewBox: None => main
        dest_vb = self.unit_axes[unit]['vb'] if unit in self.unit_axes else None
        if channel_name not in self.plot_items:
            pen = mkPen(color=color, width=2)

            # Create curve and attach to the correct viewbox
            if pen_mode and len(y_data) > 0:
                curve = pg.PlotDataItem([x_data[-1]], [y_data[-1]],
                                        pen=None, symbol='o', symbolSize=5,
                                        symbolBrush=color, name=channel_name)
            else:
                curve = pg.PlotDataItem(x_data, y_data, pen=pen, name=channel_name)

            # Add to main or secondary viewbox
            if dest_vb is None:
                # MAIN axis (left): add to the main PlotItem
                self.plot_widget.addItem(curve)
                # DO NOT add manually to legend here — pyqtgraph does it automatically
            else:
                # SECONDARY axis (right): add to its ViewBox
                dest_vb.addItem(curve)
                # Manually add to shared legend (secondary viewboxes are not auto-added)
                try:
                    self._ensure_legend().addItem(curve, channel_name)
                except Exception:
                    pass

            # speed-ups
            try:
                curve.setClipToView(True)
                curve.setDownsampling(auto=True, mode='peak')
            except Exception:
                pass

            self.plot_items[channel_name] = {"item": curve, "unit": unit}
        else:
            # if unit changed for an existing channel, move item to the correct vb
            rec = self.plot_items[channel_name]
            curve = rec["item"]
            old_unit = rec["unit"]
            if old_unit != unit:
                # remove from old vb and re-add to new
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

            # update data
            if pen_mode and len(y_data) > 0:
                curve.setData([x_data[-1]], [y_data[-1]])
            else:
                curve.setData(x_data, y_data)

class ChannelSettings(QGroupBox):
    def __init__(self, parent=None):
        super().__init__("Channels Settings")
        self.parent = parent
        self.create_widgets()
        self.popup = None
        
    def create_widgets(self):
        layout = QGridLayout(self)
        
        # Make col 0 (picker) and col 1 (reading) wide, col 2 (units) narrow
        layout.setColumnStretch(0, 4)   # Channel (pick)
        layout.setColumnStretch(1, 3)   # Reading
        layout.setColumnStretch(2, 1)   # Units (kept small)

        # Prevent the Units column from widening
        layout.setColumnMinimumWidth(2, 60)  # or 50 if your unit strings are short

        
        column_headers = ['Channel (pick)', 'Reading', 'Units']
        for i, header in enumerate(column_headers):
            layout.addWidget(QLabel(header), 0, i)
        self.display_names = plot_options["Device Name"].tolist()
        self.display_vars = []
        self.units_labels = []
        self.reading_labels = []
        default_three = (self.display_names + [""]*3)[:3]
        for slot in range(3):
            selector = QComboBox()
            selector.addItems(self.display_names)
            if slot < len(default_three):
                selector.setCurrentText(default_three[slot])
            layout.addWidget(selector, slot+1, 0)
            self.display_vars.append(selector)
            reading_label = QLabel("0.0000")
            reading_label.setFrameStyle(1)
            reading_label.setAlignment(Qt.AlignCenter)
            layout.addWidget(reading_label, slot+1, 1)
            setattr(self, f"reading_value_var{slot}", reading_label)
            self.reading_labels.append(reading_label)
            
            units_label = QLabel("")
            units_label.setAlignment(Qt.AlignCenter)
            units_label.setFixedWidth(70)  # tune as needed
            units_label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
            layout.addWidget(units_label, slot+1, 2)
            self.units_labels.append(units_label)
            
            selector.currentTextChanged.connect(lambda text, s=slot: self._on_selection_changed(s))
        self.settings_edit = QPushButton("Edit")
        self.settings_edit.clicked.connect(self.edit_settings)
        layout.addWidget(self.settings_edit, 4, 0)
        self.calibrate_button = QPushButton("Re-calibrate")
        self.calibrate_button.clicked.connect(self.open_calibration)
        layout.addWidget(self.calibrate_button, 4, 1, 1, 2)
        for slot in range(3):
            self._on_selection_changed(slot)
            
    def _find_index_by_device_name(self, name):
        if not name:
            return None
        match = plot_options.index[plot_options["Device Name"] == name]
        return int(match[0]) if len(match) else None
        
    def _on_selection_changed(self, slot):
        idx = self._find_index_by_device_name(self.display_vars[slot].currentText())
        if idx is None:
            self.units_labels[slot].setText("")
        else:
            self.units_labels[slot].setText(str(plot_options.loc[idx, "Units"]))
            
    def open_calibration(self):
        if self.popup is not None and self.popup.isVisible():
            return
        self.popup = QDialog(self)
        self.popup.setWindowTitle("Channel Calibration")
        layout = QGridLayout(self.popup)
        column_headers = ['Channel', 'Error Val', '', '']
        for i, header in enumerate(column_headers):
            layout.addWidget(QLabel(header), 0, i)
        for i, row in plot_options.iterrows():
            layout.addWidget(QLabel(row['Device Name']), i+1, 0)
            error_label = QLabel(str(row['Error']))
            error_label.setFrameStyle(1)
            layout.addWidget(error_label, i+1, 1)
            cal_edit = QPushButton("Zero")
            layout.addWidget(cal_edit, i+1, 2)
            cal_reset = QPushButton("Reset")
            layout.addWidget(cal_reset, i+1, 3)
        self.popup.exec_()  # ★ PyQt5
        
    def edit_settings(self):
        try:
            subprocess.run(['python', 'DAQ_Settings.py'], check=True)
        except subprocess.CalledProcessError as e:
            print(f"Error running subprocess: {e}")
        else:
            global default_settings, default_settings_output, default_settings_custom, combined_settings, plot_options, No_chans, No_chans_out, No_chans_cust, daq, selected_daq, sample_rate, refresh_rate, max_buffer_size, max_plot_points, decimation_per, filter_type, filter_order, filter_cutoff, filter_fh
            (default_settings, default_settings_output, default_settings_custom,
             combined_settings, plot_options, No_chans, No_chans_out, No_chans_cust,
             daq, selected_daq, sample_rate, refresh_rate, max_buffer_size, max_plot_points,
             decimation_per, filter_type, filter_order, filter_cutoff, filter_fh) = read_defaults()
            if self.parent:
                self.parent.refresh_ui()

                
                

class PlotSettings(QGroupBox):
    def __init__(self, parent=None):
        super().__init__("Plot Settings")
        self.parent = parent
        self.create_widgets()
        
    def create_widgets(self):
        self.tabs = QTabWidget()
        layout = QVBoxLayout(self)
        layout.addWidget(self.tabs)
        
        # Tabs we keep
        self.plotSettings1 = QWidget()
        self.saveSettings = QWidget()
        
        self.tabs.addTab(self.plotSettings1, "Plot Settings")
        self.tabs.addTab(self.saveSettings, "Save Settings")
        
        self.setup_plot_settings1()
        self.setup_input_settings()
        
    def setup_plot_settings1(self):
        layout = QGridLayout(self.plotSettings1)
        
        # Y-axis selection
        layout.addWidget(QLabel("Select Y-Axis"), 0, 0, 1, 5)
        for i, row in plot_options.iterrows():
            plot_check_var = QCheckBox(row['Channel'])
            setattr(self, f"plot_check_var{i}", plot_check_var)
            setattr(self, f"plot_check_box{i}", plot_check_var)
            row_pos = 1 if i <= 4 else 2
            col_pos = i % 5
            layout.addWidget(plot_check_var, row_pos, col_pos)
        
        # X-axis selection
        layout.addWidget(QLabel("Select X-Axis"), 3, 0, 1, 3)
        self.Non_chan_list = ["Full Duration", "Short Duration", "Samples"]
        self.chan_list = default_settings["Device Name"].tolist()
        self.out_chan_list = default_settings_output["Device Name"].tolist()
        self.x_axis_list = self.Non_chan_list + self.chan_list + self.out_chan_list
        
        self.x_axis_selection = QComboBox()
        self.x_axis_selection.addItems(self.x_axis_list)
        self.x_axis_selection.setCurrentText("Samples")
        layout.addWidget(self.x_axis_selection, 4, 0, 1, 3)
        
        self.plot_duration_label = QLabel("Plot duration")
        layout.addWidget(self.plot_duration_label, 3, 3, 1, 2)
        
        self.plot_duration_entry = QLineEdit("1")
        layout.addWidget(self.plot_duration_entry, 4, 3, 1, 2)
        
        # Hide duration widgets initially
        self.plot_duration_label.hide()
        self.plot_duration_entry.hide()
        
        # Toggle duration controls when "Short Duration" is selected
        self.x_axis_selection.currentTextChanged.connect(self.on_x_axis_selection_change)
        
        # Graph title
        layout.addWidget(QLabel("Graph Title"), 5, 0, 1, 3)
        self.title_label_entry = QLineEdit()
        layout.addWidget(self.title_label_entry, 6, 0, 1, 3)
        
        # Pen mode
        self.pen_var = QCheckBox("Pen")
        layout.addWidget(self.pen_var, 6, 4)
        
    def on_x_axis_selection_change(self, text):
        for i, row in plot_options.iterrows():
            plot_check_box = getattr(self, f"plot_check_box{i}")
            device_name = row["Device Name"]
            if text == device_name:
                plot_check_box.setChecked(False)
                plot_check_box.setEnabled(False)
                self.plot_duration_label.hide()
                self.plot_duration_entry.hide()
            elif text == "Short Duration":
                plot_check_box.setEnabled(True)
                self.plot_duration_label.show()
                self.plot_duration_entry.show()
            else:
                plot_check_box.setEnabled(True)
                self.plot_duration_label.hide()
                self.plot_duration_entry.hide()
                
    def setup_input_settings(self):
        layout = QGridLayout(self.saveSettings)
        
        # Duration
        layout.addWidget(QLabel("Duration to Acquire (s)"), 1, 0, 1, 2)
        self.recording_duration_entry = QLineEdit("10")
        layout.addWidget(self.recording_duration_entry, 2, 0, 1, 2)
        
        # Autosave
        self.auto_save_var = QCheckBox("Autosave")
        layout.addWidget(self.auto_save_var, 3, 0)
        
        # Autosave
        self.auto_save_var = QCheckBox("Autostop")
        layout.addWidget(self.auto_save_var, 3, 1)


class OutputSettings(QGroupBox):
    def __init__(self, parent=None):
        super().__init__("Output")
        self.parent = parent
        self.create_widgets()
    
    
    def create_widgets(self):
        outer = QVBoxLayout(self)

        # ---- Top row: Run | Start Recording | Stop ----
        top_row = QHBoxLayout()
        top_row.setSpacing(8)

        self.start_button = QPushButton("Run")
        self.start_button.clicked.connect(self.start_task)

        self.record_button = QPushButton("Start Recording")
        self.record_button.setEnabled(False)

        self.stop_button = QPushButton("Stop")
        self.stop_button.clicked.connect(self.stop_task)

        # make them the same width
        for btn in (self.start_button, self.record_button, self.stop_button):
            btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            btn.setMinimumWidth(110)

        top_row.addWidget(self.start_button)
        top_row.addWidget(self.record_button)
        top_row.addWidget(self.stop_button)
        outer.addLayout(top_row)

        # ---- Remaining actions (stacked) ----
        self.clear_button = QPushButton("Clear Graph")
        self.clear_button.clicked.connect(self.clear_graph)
        outer.addWidget(self.clear_button)

        self.save_button = QPushButton("Save Data")
        self.save_button.clicked.connect(self.save_data)
        outer.addWidget(self.save_button)

        self.reset_button = QPushButton("Reset DAQ")
        self.reset_button.clicked.connect(self.reset_device)
        outer.addWidget(self.reset_button)

        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.close_app)
        outer.addWidget(self.close_button)

        # optional: tighter spacing
        outer.setSpacing(10)
        outer.setContentsMargins(8, 8, 8, 8)

    
    def start_task(self):
        if self.parent:
            self.parent.start_task()
    def stop_task(self):
        if self.parent:
            self.parent.stop_task()
    def clear_graph(self):
        if self.parent and hasattr(self.parent, 'graphDataWidget'):
            self.parent.graphDataWidget.clear_plots()
    def save_data(self):
        if self.parent:
            self.parent.save_data()
    def reset_device(self):
        if self.parent:
            self.parent.reset_device()
    def close_app(self):
        if self.parent:
            self.parent.close()


class Functions(QGroupBox):
    """
    Hardware-timed, continuous AO function generator with DAQmx regeneration.

    Notes:
    - Writes one buffer and lets the device loop it (ALLOW_REGENERATION).
    - Start/Stop are immediate and robust.
    - Uses the first configured output channel in default_settings_output.
    - If you want to live-update (e.g., change freq), call start_generation() again,
      which will rebuild & rewrite the buffer (stop/restart internally).
    """
    def __init__(self, parent=None):
        super().__init__("Functions")
        self.parent = parent
        self.create_widgets()

        # AO task state
        self.ao_task = None
        self.ao_writer = None
        self.is_running = False
        self.popup = None

    def create_widgets(self):
        layout = QVBoxLayout(self)
        self.start_gen_button = QPushButton("Function Generator")
        self.start_gen_button.clicked.connect(self.open_generator)
        layout.addWidget(self.start_gen_button)

    # ---------- UI ----------
    def open_generator(self):
        if self.popup is not None and self.popup.isVisible():
            return

        self.popup = QDialog(self)
        self.popup.setWindowTitle("Function Generator")
        self.popup.setFixedSize(520, 220)
        layout = QGridLayout(self.popup)

        # Waveform type
        layout.addWidget(QLabel("Select Function:"), 0, 0)
        self.waveform_type_var = QComboBox()
        self.waveform_type_var.addItems(["Sine", "Square", "Triangular", "Sawtooth", "DC"])
        layout.addWidget(self.waveform_type_var, 1, 0)

        # Amplitude / DC offset
        layout.addWidget(QLabel("Amplitude (V):"), 0, 1)
        self.amplitude_entry = QLineEdit("2.5")
        layout.addWidget(self.amplitude_entry, 1, 1)

        layout.addWidget(QLabel("DC Offset (V):"), 0, 2)
        self.dc_offset_entry = QLineEdit("2.5")
        layout.addWidget(self.dc_offset_entry, 1, 2)

        # Start / Stop
        self.start_button = QPushButton("Start Generation")
        self.start_button.clicked.connect(self.start_generation)
        layout.addWidget(self.start_button, 1, 3)

        self.stop_button = QPushButton("Stop Generation")
        self.stop_button.clicked.connect(self.stop_generation)
        self.stop_button.setEnabled(False)
        layout.addWidget(self.stop_button, 1, 4)

        # Frequency + multiplier
        layout.addWidget(QLabel("Frequency (Hz):"), 2, 0)
        self.frequency_entry = QLineEdit("0.5")
        layout.addWidget(self.frequency_entry, 3, 0)

        layout.addWidget(QLabel("Multiplier:"), 2, 1, 1, 3)
        self.multiplier_var = QButtonGroup(self.popup)
        multiplier_options = [("x1", 1), ("x10", 10), ("x100", 100), ("x1000", 1000)]
        for col, (text, value) in enumerate(multiplier_options, 1):
            radio = QRadioButton(text)
            self.multiplier_var.addButton(radio, value)
            layout.addWidget(radio, 3, col)
            if value == 10:
                radio.setChecked(True)

        # Close
        close_button = QPushButton("Close")
        close_button.clicked.connect(self.popup.close)
        layout.addWidget(close_button, 4, 0, 1, 5)

        self.waveform_type_var.currentTextChanged.connect(self.on_waveform_select)
        self.on_waveform_select(self.waveform_type_var.currentText())  # initialize enable/disable

        self.popup.exec_()

    def on_waveform_select(self, waveform: str):
        # For DC, amplitude has no effect; keep field disabled to avoid confusion.
        self.amplitude_entry.setEnabled(waveform != "DC")

    # ---------- AO control ----------
    def start_generation(self):
        """
        Build waveform buffer, configure AO for hardware-timed continuous regeneration,
        preload buffer, and start the task.
        """

        # If already running, stop first to reconfigure with new params
        if self.is_running:
            self.safe_close_ao()

        # Parse UI values
        try:
            base_freq = float(self.frequency_entry.text())
            multiplier = self.multiplier_var.checkedId() or 1
            freq = max(0.0, base_freq * multiplier)
            waveform_type = self.waveform_type_var.currentText()
            dc_offset = float(self.dc_offset_entry.text())
            amplitude = float(self.amplitude_entry.text()) if waveform_type != "DC" else 0.0
        except ValueError:
            print("Invalid input values for function generator.")
            return

        # Resolve the AO channel + safe voltage limits from defaults
        try:
            physical_channel_o = f"{selected_daq}/{default_settings_output['Channel'].iloc[0]}"
            min_v = float(default_settings_output['Min Voltage'].iloc[0])
            max_v = float(default_settings_output['Max Voltage'].iloc[0])
            if min_v >= max_v:
                raise ValueError("AO min voltage must be less than max voltage.")
        except Exception as e:
            print("Output settings missing or invalid:", e)
            return

        # Choose an AO sample rate:
        # - Use your acquisition sample_rate as a starting point but clamp to a sensible range for AO.
        # - Rule of thumb: at least ~10x the highest waveform frequency (for decent shape).
        try:
            base_sr = int(float(sample_rate))
        except Exception:
            base_sr = 10000
        ao_sample_rate = max(5000, min(100000, base_sr))

        # Buffer length:
        # - At least one period for AC waves. For DC, choose 1 second worth (arbitrary but fine).
        if waveform_type != "DC" and freq > 0.0:
            period_samples = max(int(ao_sample_rate / max(freq, 1e-6)), 1000)
        else:
            period_samples = ao_sample_rate
        buf_len = min(max(period_samples, 2048), 200_000)
        
        
        self._ao_buf_len = buf_len  # remember for stop()

        self.ao_task = nidaqmx.Task()
        chan = self.ao_task.ao_channels.add_ao_voltage_chan(
            self.physical_channel_o, min_val=self.min_v, max_val=self.max_v
        )
        

        # Timebase for one loop buffer
        t = np.arange(buf_len, dtype=np.float64) / ao_sample_rate

        # Build waveform
        if waveform_type == "Sine":
            wv = amplitude * np.sin(2 * np.pi * freq * t) + dc_offset
        elif waveform_type == "Square":
            # Note: sign(sin) gives 0 at exact zeros; that’s fine for most cases
            wv = amplitude * np.sign(np.sin(2 * np.pi * freq * t)) + dc_offset
        elif waveform_type == "Triangular":
            # Triangle via a sawtooth fold
            saw = (freq * t) - np.floor(0.5 + freq * t)
            wv = amplitude * (2 * np.abs(2 * saw) - 1) + dc_offset
        elif waveform_type == "Sawtooth":
            saw = (freq * t) - np.floor(0.5 + freq * t)
            wv = amplitude * saw + dc_offset
        else:  # DC
            wv = np.full(buf_len, dc_offset, dtype=np.float64)

        # Clamp to device range
        wv = np.clip(wv, min_v, max_v)

        # Create & configure AO task
        try:
            self.ao_task = nidaqmx.Task()
            self.ao_task.ao_channels.add_ao_voltage_chan(
                physical_channel_o, min_val=min_v, max_val=max_v
            )

            # Hardware-timed continuous generation w/ regeneration
            self.ao_task.timing.cfg_samp_clk_timing(
                rate=ao_sample_rate,
                sample_mode=ni.AcquisitionType.CONTINUOUS,
                samps_per_chan=buf_len
            )
            self.ao_task.out_stream.regen_mode = ni.RegenerationMode.ALLOW_REGENERATION

            # Preload buffer and start
            self.ao_writer = AnalogSingleChannelWriter(self.ao_task.out_stream)
            self.ao_writer.write_many_sample(wv)   # NOTE: no auto_start kwarg
            self.ao_task.start()

            self.is_running = True
            self.start_button.setEnabled(False)
            self.stop_button.setEnabled(True)
        except Exception as e:
            print("Failed to start AO task:", e)
            self.safe_close_ao()

    def stop_generation(self):
        self.stop_event.set()
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)


    def safe_close_ao(self):
        """Idempotent AO cleanup."""
        try:
            if self.ao_task is not None:
                try:
                    if not self.ao_task.is_task_done():
                        self.ao_task.stop()
                except Exception:
                    pass
                self.ao_task.close()
        except Exception:
            pass
        self.ao_task = None
        self.ao_writer = None
        self.is_running = False



class Apps(QGroupBox):
    def __init__(self, parent=None):
        super().__init__("Apps")
        self.parent = parent
        self.create_widgets()
        
    def create_widgets(self):
        layout = QHBoxLayout(self)
        self.sweep_button = QPushButton("App1"); layout.addWidget(self.sweep_button)
        self.response_button = QPushButton("App2"); layout.addWidget(self.response_button)
        self.triggers_button = QPushButton("App3"); layout.addWidget(self.triggers_button)
        
        layout.addWidget(self.sweep_button)
        layout.addWidget(self.response_button)
        layout.addWidget(self.triggers_button)

        # lock height to the layout’s size hint (buttons + margins)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.setFixedHeight(self.sizeHint().height())

class VoltageContinuousInput(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Analog Voltage Data Acquisition")
        self.setGeometry(100, 100, 1400, 825)
        self.run = False
        self.continue_running = False
        self.signals = DAQSignals()
        self.signals.data_ready.connect(self.update_data)
        self.signals.task_stopped.connect(self.on_task_stopped)
        self.create_widgets()
        self.setup_layout()
        self.total_written = 0  # ← how many samples we’ve actually produced so far (per channel)
        
        
        # ring-buffers & plotting state ★
        self.plot_colors = ['red', 'green', 'blue', 'black', 'magenta', 'purple', 'orange', 'brown']
        self.axes_plotting = []
        self.sample_axis = None
        self.time_axis = None
        self.rings = []
        self.rpos = None
        self.reader = None
        self.sos = None
        self.zi = None
        self.samples_per_frame = None
        
    def create_widgets(self):
        self.channelSettingsWidget = ChannelSettings(self)
        self.plotSettingsWidget = PlotSettings(self)
        self.outputSettingsWidget = OutputSettings(self)
        self.graphDataWidget = GraphData(self)
        self.functionsWidget = Functions(self)
        self.appsWidget = Apps(self)
        
    def setup_layout(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        main_layout = QHBoxLayout(central_widget)

        # ---- Left panel (fixed width) ----
        left_v = QVBoxLayout()
        left_v.addWidget(self.channelSettingsWidget)
        left_v.addWidget(self.plotSettingsWidget)
        left_v.addWidget(self.outputSettingsWidget)
        left_v.addWidget(self.functionsWidget)
        left_v.setStretch(0, 1)
        left_v.setStretch(1, 1)
        left_v.setStretch(2, 1)
        left_v.setStretch(3, 1)

        left_container = QWidget()
        left_container.setLayout(left_v)

        FIXED_LEFT_WIDTH = 400  # pick what you like
        left_container.setFixedWidth(FIXED_LEFT_WIDTH)
        left_container.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)

        # ---- Right panel (flex) ----
        right_v = QVBoxLayout()
        right_v.addWidget(self.graphDataWidget, 3)
        right_v.addWidget(self.appsWidget, 1)

        right_container = QWidget()
        right_container.setLayout(right_v)
        right_container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        # Add to main layout
        main_layout.addWidget(left_container)
        main_layout.addWidget(right_container)

        
    def refresh_ui(self):
        self.create_widgets()
        self.setup_layout()
        
    # ---------------------- Acquisition/processing setup (optimized) ---------------------- ★
    def start_task(self):
        self.disable_buttons()
        self.get_user_settings()
        self.create_graph()
        self.create_buffers()
        self.create_nidaq_task()
        self.continue_running = True
        self.acquisition_thread = threading.Thread(target=self.run_task, daemon=True)
        self.acquisition_thread.start()
        
    def disable_buttons(self):
        self.outputSettingsWidget.start_button.setEnabled(False)
        self.outputSettingsWidget.clear_button.setEnabled(False)
        self.outputSettingsWidget.record_button.setEnabled(True)
        self.outputSettingsWidget.save_button.setEnabled(False)
        self.outputSettingsWidget.reset_button.setEnabled(False)
        self.outputSettingsWidget.close_button.setEnabled(False)
        for i in range(0, (No_chans + No_chans_cust)):
            plot_check_box = getattr(self.plotSettingsWidget, f"plot_check_box{i}")
            plot_check_box.setEnabled(False)
        self.plotSettingsWidget.x_axis_selection.setEnabled(False)
        self.plotSettingsWidget.title_label_entry.setEnabled(False)
        
    def get_user_settings(self):
        self.sample_rate = int(float(sample_rate))
        self.recording_duration = int(float(self.plotSettingsWidget.recording_duration_entry.text()))
        self.number_of_samples = self.recording_duration * self.sample_rate
        
        self.refresh_rate = max(1, min(int(float(refresh_rate)), 30))  # cap UI FPS ★
        self.samples_per_frame = max(1, self.sample_rate // self.refresh_rate)  # ★ fixed frame
        self.selected_x_axis = self.plotSettingsWidget.x_axis_selection.currentText()


        # --- set plot_duration BEFORE using it ---
        if self.selected_x_axis == "Short Duration":
            try:
                self.plot_duration = float(self.plotSettingsWidget.plot_duration_entry.text())
            except ValueError:
                self.plot_duration = min(self.recording_duration, 10.0)  # fallback
        else:
            self.plot_duration = self.recording_duration
        
        
        self.samples_to_plot = int(min(self.plot_duration * self.sample_rate, max_plot_points))
        self.sample_axis = np.arange(self.samples_to_plot, dtype=np.int32)
        self.time_axis   = np.linspace(0.0, self.samples_to_plot/self.sample_rate, self.samples_to_plot, endpoint=False).astype(np.float32)
        
        

        # prebuild axes (will slice to needed length) ★
        self.sample_axis = np.arange(self.samples_to_plot, dtype=np.int32)
        self.time_axis = np.linspace(0.0, self.samples_to_plot / self.sample_rate, self.samples_to_plot, endpoint=False).astype(np.float32)
        
        
        # filter config (causal) ★
        self.filter_order = 4
        self.selected_filter = 'low_pass'  # can switch to 'no_filter' for raw
        self.filter_cuttoff = 2.0
        self.filter_band = None
        
        # filter design/state ★
        nyq = 0.5 * self.sample_rate
        if self.selected_filter == 'low_pass':
            self.sos = butter(self.filter_order, self.filter_cuttoff/nyq, btype='low', output='sos')
            self.zi = [np.zeros((self.sos.shape[0], 2)) for _ in range(No_chans)]
        else:
            self.sos = None
            self.zi = None
            
    def create_nidaq_task(self):
        self.task = nidaqmx.Task()
        for _, row in default_settings.iterrows():
            physical_channel = f"{selected_daq}/{row['Channel']}"
            min_voltage = float(row['Min Voltage'])
            max_voltage = float(row['Max Voltage'])
            terminal_config = getattr(TerminalConfiguration, str(row['Terminal Configuration']).upper())
            self.task.ai_channels.add_ai_voltage_chan(
                physical_channel, min_val=min_voltage, max_val=max_voltage, terminal_config=terminal_config)
        
        # driver buffer ~ 3x duration to avoid overflows ★
        driver_buf = int(max(self.samples_per_frame * 6, max_buffer_size))
        
        self.task.timing.cfg_samp_clk_timing(
            self.sample_rate,
            sample_mode=nidaqmx.constants.AcquisitionType.CONTINUOUS,
            samps_per_chan=int(driver_buf)
        )
        self.reader = AnalogMultiChannelReader(self.task.in_stream)  # ★
        self.frame = np.empty((No_chans, int(self.samples_per_frame)), dtype=np.float64) # ★
        self.task.start()
        
    # --- ring buffers (values & plot) ★ ---
    def create_buffers(self):
        n_total = No_chans + No_chans_cust + No_chans_out
        N = max(self.samples_to_plot, self.samples_per_frame * 6)
        self.rings = [np.zeros(N, dtype=np.float32) for _ in range(n_total)]
        self.rpos = np.zeros(n_total, dtype=np.int64)
        
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
        idx = (np.arange(p - w, p, dtype=np.int64) % n)
        return np.take(b, idx)

    # --- main acquisition loop (fixed-size, deterministic-ish) ★ ---
    def run_task(self):
        try:
            while self.continue_running:
                # read exactly samples_per_frame; blocks up to timeout ★
                self.reader.read_many_sample(self.frame, number_of_samples_per_channel=self.samples_per_frame, timeout=1.0)
                block = self.frame  # shape: (No_chans, samples_per_frame)

                # causal filtering per-channel (optional) ★
                if self.sos is not None:
                    for ch in range(No_chans):
                        y, self.zi[ch] = sosfilt(self.sos, block[ch], zi=self.zi[ch])
                        block[ch] = y

                # calibration/transform for default channels ★
                transformed = []
                for index, row in default_settings.iterrows():
                    mx = float(row['Mx']); b = float(row['b']); err = float(row['Error'])
                    y = (block[index].astype(np.float32) * mx + b - err).astype(np.float32, copy=False)
                    transformed.append(y)
                    self.rb_write(index, y)

                # custom channels ★
                base_offset = No_chans
                for cidx, row in default_settings_custom.iterrows():
                    f_chan_name = row['First Channel']; s_chan_name = row['Second Channel']; op = row['Operation']
                    f_ax = default_settings[default_settings["Channel"] == f_chan_name].index[0]
                    s_ax = default_settings[default_settings["Channel"] == s_chan_name].index[0]
                    f_buff = transformed[f_ax]; s_buff = transformed[s_ax]
                    if op == 'x': y = f_buff * s_buff
                    elif op == '+': y = f_buff + s_buff
                    elif op == '-': y = f_buff - s_buff
                    else: y = f_buff / np.where(s_buff==0, 1.0, s_buff)
                    y = y.astype(np.float32, copy=False)
                    self.rb_write(base_offset + cidx, y)

                    
                self.total_written += block.shape[1]
                self.total_written = min(self.total_written, self.number_of_samples)  # clamp if you like
                self.signals.data_ready.emit(None)    
                    
                # output channels (if you read them; here we assume not part of AI read) ★
                # If outputs are measured on AI, map them here similarly.

                
                # Emit a lightweight signal: send just indices that changed (optional)
                # To keep API consistent with your code, emit a small object (None) and have UI pull from rings.
                self.signals.data_ready.emit(None)
                # sleep just enough to target refresh (not critical; read blocks anyway)
                # time.sleep(0.0)
        except Exception as e:
            print("Acquisition error:", e)
        finally:
            try:
                self.task.close()
            except Exception:
                pass
            self.signals.task_stopped.emit()
        
    # ---------------- UI update path ----------------
    def update_data(self, _):
        self.plot_data()
        self.update_value_displays()
        
    def create_graph(self):
        self.graphDataWidget.clear_plots()
        self.axes_plotting = []
        self.selected_count = 0
        for i, row in plot_options.iterrows():
            plot_check_var = getattr(self.plotSettingsWidget, f"plot_check_var{i}")
            if plot_check_var.isChecked():
                plot_buffer_attr = f'plot_buffer{i}'  # legacy label only
                device_name = row['Device Name']
                units = row['Units']
                color = self.plot_colors[self.selected_count % len(self.plot_colors)]
                self.axes_plotting.append((self.graphDataWidget.plot_widget, plot_buffer_attr, color, device_name, units, 'left', i))
                self.selected_count += 1
        graph_title = self.plotSettingsWidget.title_label_entry.text()
        self.graphDataWidget.plot_widget.setTitle(graph_title)
        
    def get_x_data(self, n):
        if self.selected_x_axis == "Samples":
            return self.sample_axis[:n]
        elif self.selected_x_axis in ("Full Duration", "Short Duration"):
            return self.time_axis[:n]
        else:
            # X = another channel’s Y. Use its ring buffer values (not time) ★
            match = combined_settings[combined_settings['Device Name'] == self.selected_x_axis].index
            if not match.empty:
                ch = int(match[0])
                xbuf = self.rb_tail(ch, min(self.samples_to_plot, self.rings[ch].size))
                return xbuf[-n:]
            else:
                return self.sample_axis[:n]
        
    def plot_data(self):
        if not self.axes_plotting:
            return

        pencil_selected = self.plotSettingsWidget.pen_var.isChecked()
        # number to plot is the smaller of window size and what we've collected
        n_avail = min(self.samples_to_plot, self.total_written)
        if n_avail <= 0:
            return

        for (_, _, color, device_name, units, _, ch_index) in self.axes_plotting:
            y = self.rb_tail(ch_index, n_avail)
            if y.size == 0:
                continue
            x = self.get_x_data(y.size)  # already slices [:n]
            self.graphDataWidget.update_plot(device_name, x, y, color, pencil_selected, unit=units)

        self.graphDataWidget.plot_widget.setLabel('bottom', self.selected_x_axis)
        self.graphDataWidget.plot_widget.autoRange()

        
    def update_value_displays(self):
        for slot in range(3):
            name = self.channelSettingsWidget.display_vars[slot].currentText() if hasattr(self.channelSettingsWidget, "display_vars") else ""
            label_widget = getattr(self.channelSettingsWidget, f"reading_value_var{slot}")
            if not name:
                label_widget.setText("—"); continue
            idx = self.channelSettingsWidget._find_index_by_device_name(name)
            if idx is None:
                label_widget.setText("—"); continue
            y = self.rb_tail(idx, 1)
            if y.size == 0:
                label_widget.setText("—")
            else:
                label_widget.setText("{:.4f}".format(float(y[-1])))
                
    def enable_buttons(self):
        self.outputSettingsWidget.start_button.setEnabled(True)
        self.outputSettingsWidget.clear_button.setEnabled(True)
        self.outputSettingsWidget.record_button.setEnabled(False)
        self.outputSettingsWidget.save_button.setEnabled(True)
        self.outputSettingsWidget.reset_button.setEnabled(True)
        self.outputSettingsWidget.close_button.setEnabled(True)
        for i, row in plot_options.iterrows():
            plot_check_box = getattr(self.plotSettingsWidget, f"plot_check_box{i}")
            plot_check_box.setEnabled(True)
        self.plotSettingsWidget.x_axis_selection.setEnabled(True)
        self.plotSettingsWidget.title_label_entry.setEnabled(True)
        
    def stop_task(self):
        self.continue_running = False
        # not clearing rings; keeps data for save
        
    def on_task_stopped(self):
        self.enable_buttons()
        
    def save_data(self):
        filepath, _ = QFileDialog.getSaveFileName(self, "Save Data", "", "Excel Files (*.xlsx)")
        if filepath:
            # Build time vector in ms for the full ring tail of channel 0 length (up to number_of_samples) ★
            length = min(self.number_of_samples, self.rings[0].size)
            y0 = self.rb_tail(0, length)
            time_vec = np.arange(y0.size, dtype=np.float64) * (1000.0 / self.sample_rate)
            data_output = {"Time [ms]": time_vec}
            for index, row in default_settings.iterrows():
                Device_Units = default_settings.loc[index, 'Units']
                Device_Name = default_settings.loc[index, 'Device Name']
                Label = f"{Device_Name} [{Device_Units}]"
                y = self.rb_tail(index, length).astype(np.float64, copy=False)
                data_output[Label] = y
            data_output = pd.DataFrame(data_output)
            data_output.to_excel(filepath, index=False)
            
    def reset_device(self):
        daq = nidaqmx.system.Device(selected_daq)
        daq.reset_device()
        
    def closeEvent(self, event):
        self.continue_running = False
        if hasattr(self, 'acquisition_thread') and self.acquisition_thread.is_alive():
            self.acquisition_thread.join(timeout=1.0)
        event.accept()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    window = VoltageContinuousInput()
    window.show()
    sys.exit(app.exec_())  # ★
