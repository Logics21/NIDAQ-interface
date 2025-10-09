import tkinter as tk
from tkinter import ttk, filedialog

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure
from matplotlib.widgets import Cursor

import numpy as np
import collections
import pandas as pd
import subprocess

import nidaqmx
from nidaqmx.constants import TerminalConfiguration

import threading
from threading import Thread
import time


from scipy.signal import butter, filtfilt, medfilt, decimate
from scipy.ndimage import uniform_filter1d

daqSys = nidaqmx.system.System()
daqList = daqSys.devices.device_names

# Convert device names to actual device objects
daqObjects = [daqSys.devices[name] for name in daqList]



if len(daqList) == 0:
    raise ValueError('No DAQ detected, check connection.')
       
def read_defaults():
    # Read default settings and filter checked channels
    default_settings = pd.read_excel('default_settings.xlsx')
    default_settings = default_settings[default_settings['Selected Channel'] == 1]
    default_settings = default_settings.reset_index(drop=True)   
    default_settings['Error'] = 0 # Add error column with value 0 for all rows
    
    # Read custom channel settings and filter checked channels
    default_settings_custom = pd.read_excel('default_settings.xlsx', sheet_name='custom_channel_settings')
    default_settings_custom = default_settings_custom[default_settings_custom['Selected Channel'] == 1]
    default_settings_custom = default_settings_custom.reset_index(drop=True)
    
    
    # Read ouptut channel settings and filter checked channels
    default_settings_output = pd.read_excel('default_settings.xlsx', sheet_name='output_settings')
    default_settings_output = default_settings_output[default_settings_output['Selected Channel'] == 1]
    default_settings_output = default_settings_output.reset_index(drop=True)
    default_settings_output['Error'] = 0  # Add error column with value 0 for all rows
    
    
    combined_settings = pd.concat([default_settings, default_settings_custom, default_settings_output], ignore_index=True)
    plot_options = pd.concat([default_settings, default_settings_custom], ignore_index=True)

    
    # Number of channels
    No_chans = len(default_settings)
    No_chans_out = len(default_settings_output)
    No_chans_cust = len(default_settings_custom)

    # Selected DAQ device
    selected_daq = default_settings['DAQ'].iloc[0]
    daq = nidaqmx.system.Device(selected_daq)

    return default_settings, default_settings_output, default_settings_custom, combined_settings, plot_options,  No_chans, No_chans_out, No_chans_cust, daq, selected_daq

default_settings, default_settings_output, default_settings_custom, combined_settings, plot_options, No_chans, No_chans_out, No_chans_cust, daq, selected_daq = read_defaults()


if daq.name in daqList:  # Compare daq.name (string) to daqList
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


            
def refresh_frame(frame):
    for widget in frame.winfo_children():
        widget.destroy()
    frame.create_widgets()
    

class VoltageContinuousInput(tk.Frame):
        
    def __init__(self, master):
        super().__init__(master)
        self.master = master
        self.master.title("Analog Voltage Data Acquisition")
        self.master.geometry("1400x825")
        self.create_widgets()
        self.pack()
        self.run = False
        self.continue_running = False

    def create_widgets(self):
        # The main frame is made up of six subframes
        self.channelSettingsFrame = ChannelSettings(self, title="Channels Settings")
        self.channelSettingsFrame.grid(row=0, column=1, sticky="ew", pady=(15,0), padx=(10,20), ipady=10)
        
        self.plotSettingsFrame = PlotSettings(self, title="")  
        self.plotSettingsFrame.grid(row=1, column=1, sticky="ew", pady=(15,0), padx=(10,20), ipady=10)

        self.outputFrame = OutputSettings(self, title="Output")
        self.outputFrame.grid(row=2, column=1, sticky="ew", pady=(15,0), padx=(10,20), ipady=10)

        self.graphDataFrame = GraphData(self)
        self.graphDataFrame.grid(row=0, column=2, rowspan=3, pady=(15,0), padx=(10,20), ipady=10)
        
        self.FuncsFrame = Functions(self, title="Functions")
        self.FuncsFrame.grid(row=3, column=1, sticky="ew", pady=(0,0), padx=(10,20), ipady=10)
                
        self.appsFrame = Apps(self, title="Apps")
        self.appsFrame.grid(row=3, column=2, sticky="ew", pady=(0,0), padx=(10,20), ipady=10)
        
    def start_task(self):
        self.disable_buttons()
        self.get_user_settings()  # Call the standalone function
        self.create_graph()
        self.create_buffers()
        self.create_nidaq_task()
        self.continue_running = True  # Shared flag to alert task if it should stop
        self.master.after(10, self.run_task)       
               
    def disable_buttons(self):
        # Prevent the user from starting the task a second time
        self.outputFrame.start_button['state'] = 'disabled'
        self.outputFrame.clear_button['state'] = 'disabled'
        self.outputFrame.record_button['state'] = 'normal'
        self.outputFrame.save_button['state'] = 'disabled'
        self.outputFrame.reset_button['state'] = 'disabled'
        self.outputFrame.close_button['state'] = 'disabled'
        
        # Disable check boxes 
        for i in range(0, (No_chans + No_chans_cust)):
            plot_check_box = getattr(self.plotSettingsFrame, f"plot_check_box{i}")
            plot_check_box.config(state='disabled')   

        # Disable drop-down menus
        getattr(self.plotSettingsFrame, "x_axis_selection").config(state='disabled')  
        getattr(self.plotSettingsFrame, "title_label_entry").config(state='disabled') 

         
    def get_user_settings(self):
            # Get task settings from the user
            self.sample_rate = int(self.plotSettingsFrame.sample_rate_entry.get())
            self.recording_duration = int(self.plotSettingsFrame.recording_duration_entry.get())
            self.number_of_samples =  self.recording_duration * self.sample_rate 
            self.refresh_rate = int(self.plotSettingsFrame.refresh_rate_entry.get())      
            self.selected_x_axis = self.plotSettingsFrame.x_axis_var.get()
            
            self.filter_order = 10
            self.selected_filter = 'moving_average'
            self.filter_cuttoff = 2
            self.filter_band = None
                
            if self.selected_x_axis == "Short Duration":
                self.plot_duration = float(self.plotSettingsFrame.plot_duration_entry.get())
            else:
                self.plot_duration = self.recording_duration
            
            self.samples_to_plot = int(self.plot_duration * self.sample_rate)
    

    def apply_filter(self, data, filter_type, cutoff, fs, order, band, decimation_factor=1):
        nyquist = 0.5 * self.sample_rate
        
        filtered_data = []

        for channel_data in data:
            if filter_type == 'low_pass':
                normal_cutoff = cutoff / nyquist
                b, a = butter(order, normal_cutoff, btype='low', analog=False)
                channel_filtered = filtfilt(b, a, channel_data)

            elif filter_type == 'high_pass':
                normal_cutoff = cutoff / nyquist
                b, a = butter(order, normal_cutoff, btype='high', analog=False)
                channel_filtered = filtfilt(b, a, channel_data)

            elif filter_type == 'band_pass':
                if band is None:
                    raise ValueError("Band-pass filter requires a tuple (low_cutoff, high_cutoff).")
                low, high = band[0] / nyquist, band[1] / nyquist
                b, a = butter(order, [low, high], btype='band', analog=False)
                channel_filtered = filtfilt(b, a, channel_data)

            elif filter_type == 'band_stop':
                if band is None:
                    raise ValueError("Band-stop filter requires a tuple (low_cutoff, high_cutoff).")
                low, high = band[0] / nyquist, band[1] / nyquist
                b, a = butter(order, [low, high], btype='bandstop', analog=False)
                channel_filtered = filtfilt(b, a, channel_data)

            elif filter_type == 'VRM':
                channel_filtered = np.sqrt(np.mean(np.square(channel_data)))

            elif filter_type == 'median':
                channel_filtered = medfilt(channel_data, kernel_size=order)

            elif filter_type == 'moving_average':
                channel_filtered = uniform_filter1d(channel_data, size=order)

            elif filter_type == 'no_filter':
                channel_filtered = channel_data

            else:
                raise ValueError("Invalid filter type.")

                
            keep_percentage = 0.75  # Keep 75% of the original data
            decimation_factor = max(1, int(1 / (1 - keep_percentage)))  # Correct calculation  
            
            # Apply decimation
            if decimation_factor > 1 and not isinstance(channel_filtered, float):
                channel_filtered = channel_filtered[::decimation_factor]
                
            filtered_data.append(channel_filtered)

        return np.array(filtered_data)

        
    def create_nidaq_task(self):          
        self.task = nidaqmx.Task()  # Create and start task          
        for index, row in default_settings.iterrows():
            physical_channel = f"{selected_daq}/{row['Channel']}"
            min_voltage = row['Min Voltage']
            max_voltage = row['Max Voltage']
            terminal_config = getattr(TerminalConfiguration, row['Terminal Configuration'].upper())
                
            # Add the channel using the extracted values
            self.task.ai_channels.add_ai_voltage_chan(
                physical_channel, 
                min_val=min_voltage,
                max_val=max_voltage, 
                terminal_config=terminal_config)

            # Configure the timing of the task
        self.task.timing.cfg_samp_clk_timing(
            self.sample_rate, 
            sample_mode=nidaqmx.constants.AcquisitionType.CONTINUOUS, 
            samps_per_chan=self.number_of_samples * 3)
        
        self.task.start()
 
    def run_task(self):
        # Check if the task needs to update the data queue and plot
        samples_available = self.task._in_stream.avail_samp_per_chan
        samples_per_frame = int(self.sample_rate // self.refresh_rate)

        
        if samples_available >= samples_per_frame:
            temp_data = self.task.read(number_of_samples_per_channel=nidaqmx.constants.READ_ALL_AVAILABLE)            
            temp_data = np.array(temp_data)
            
            
            filtered_data = self.apply_filter(
                data=temp_data,
                filter_type=self.selected_filter,
                cutoff=self.filter_cuttoff,
                fs=self.sample_rate,
                order=self.filter_order,
                band=self.filter_band
            )
            
            #filtered_data = temp_data
            
            self.update_buffers(filtered_data)
            self.plot_data()
            self.update_value_displays()         
        
        if self.continue_running:  # Check if the task should sleep or stop
            self.master.after(10, self.run_task)
        else:
            self.task.close()
            self.reset_device()
            
    
    def create_buffers(self):
        for i in range(0, (No_chans + No_chans_cust + No_chans_out)):
            val_buff_name = f"val_buffer{i}"
            plot_buff_name = f"plot_buffer{i}"
            setattr(self, val_buff_name, collections.deque(maxlen=self.number_of_samples))
            setattr(self, plot_buff_name, collections.deque(maxlen=self.samples_to_plot))


    def update_buffers(self, temp_data):
        buffer_list = []
        # channel buffers
                  
        for index, row in default_settings.iterrows():
            mx = int(row['Mx'])
            b = int(row['b'])
            error_val = int(row['Error'])
            transformed_buff = np.array(temp_data[index]) * mx + b - error_val
            transformed_buff_name = f"transformed_buff{index}"
            setattr(self, transformed_buff_name, transformed_buff)
            buffer_list.append((index, transformed_buff))
            
        #output channel buffers
        for index, row in default_settings_output.iterrows():
            transformed_buff = temp_data[index + No_chans + No_chans_cust-1]
            transformed_buff_name = f"transformed_buff{index + No_chans + No_chans_cust -1}"
            setattr(self, transformed_buff_name, transformed_buff)
            buffer_list.append((index + No_chans + No_chans_cust, transformed_buff))
            
        #custom channel buffers
        for index, row in default_settings_custom.iterrows():
            f_chan_name = row['First Channel']
            s_chan_name = row['Second Channel']
            c_operation = row['Operation']
            transformed_buff_name = f"transformed_buff{index + No_chans}"

            # Find the index of f_chan_name and s_chan_name in default_settings
            f_ax = default_settings[default_settings["Channel"] == f_chan_name].index[0]
            s_ax = default_settings[default_settings["Channel"] == s_chan_name].index[0]
            
            # Extract the transformed_buff from the tuples
            f_buff = buffer_list[f_ax][1]
            s_buff = buffer_list[s_ax][1]
            
            if c_operation == 'x':
                transformed_buff = f_buff * s_buff
            elif c_operation == '+':
                transformed_buff = f_buff + s_buff
            elif c_operation == '-':
                transformed_buff = f_buff - s_buff
            elif c_operation == '÷':
                transformed_buff = f_buff / s_buff
                
            setattr(self, transformed_buff_name, transformed_buff)   
            buffer_list.append((index + No_chans, transformed_buff))
            
        # Update all buffers
        for i, val in buffer_list:
            val_buff_name = f"val_buffer{i}"
            plot_buff_name = f"plot_buffer{i}"
            
            # Extend the value and plot buffers with new data.
            # If `val` is a single np.float64 value (scalar), wrap it in a list to avoid errors with `.extend()`.
            # Otherwise, if `val` is an iterable (e.g., a NumPy array), extend the buffer normally.
            getattr(self, val_buff_name).extend([val] if isinstance(val, np.float64) else val)
            getattr(self, plot_buff_name).extend([val] if isinstance(val, np.float64) else val)

    def get_x_data(self, buffer_length):                      
        if self.selected_x_axis == "Samples":                
            x_data = range(buffer_length)
        elif self.selected_x_axis == "Full Duration" or self.selected_x_axis == "Short Duration":
            x_data = np.linspace(0, buffer_length/ self.sample_rate, buffer_length)
        else:
            matching_index = combined_settings[combined_settings['Device Name'] == self.selected_x_axis].index
            if not matching_index.empty:
                self.x_axis_row = matching_index[0]
                plot_buff_name = f"plot_buffer{self.x_axis_row}"
                x_data = getattr(self, plot_buff_name)
            else:
                x_data = range(buffer_length)
        return x_data

    def create_graph(self):
        refresh_frame(self.graphDataFrame)
        self.selected_count = 0  # Counter for selected checkboxes
        self.unique_ax_count = 0  # Counter for axis
        self.plot_color = ['red', 'green', 'blue', 'black', 'magenta', 'purple', 'orange', 'brown']
        self.axes_plotting = []
        unit_axes_mapping = {}  # Dictionary to map units to their corresponding axes

        def handle_plotting(i, plot_buffer_attr, device_name, units):
            plot_check_var = f"plot_check_var{i}"
            if getattr(self.plotSettingsFrame, plot_check_var).get():  # Plot only if selected
                if units in unit_axes_mapping:  # Check if units already plotted
                    ax = unit_axes_mapping[units]  # Use the existing axis
                else:
                    ax_name = f'self.ax{self.unique_ax_count + 1}'  # Create axis

                    if self.unique_ax_count == 0:
                        ax = self.graphDataFrame.ax1
                    else:
                        ax = self.graphDataFrame.ax1.twinx()
                        setattr(self.graphDataFrame, ax_name, ax)

                    unit_axes_mapping[units] = ax  # Map units to the current axis
                    self.unique_ax_count += 1

                color = self.plot_color[self.selected_count % len(self.plot_color)]
                self.selected_count += 1

                self.axes_plotting.append((ax, plot_buffer_attr, color, device_name, units, 'y'))

        for i, row in plot_options.iterrows():  # Iterate over rows
            plot_buffer_attr = f'plot_buffer{i}'
            device_name = row['Device Name']
            units = row['Units']
            handle_plotting(i, plot_buffer_attr, device_name, units)

        graph_title = self.plotSettingsFrame.title_label_entry.get()
        self.graphDataFrame.ax1.set_title(graph_title)

        
    def plot_data(self):
        
        updated_plots = []
        buffer_length  = 0
        for ax, plot_buffer_attr, color, device_name, units, axis in self.axes_plotting:
            buffer_data = getattr(self, plot_buffer_attr)
            updated_plots.append((ax, buffer_data, color, device_name, units, axis))
            buffer_length = len(buffer_data)
            
            
        # Get x_data and batch plot
        if buffer_length > 0:
            x_data = self.get_x_data(buffer_length)

        # Clear all axes before plotting
        self.clear_axes()
    
        pencil_selected = self.plotSettingsFrame.pen_var.get()
        
        # Iterate over updated_plots and plot on respective axes
        for ax, buffer, color, label, units, axis in updated_plots:
            if pencil_selected:               
                if len(buffer) > 0:
                    x_last = x_data[-1]
                    y_last = buffer[-1]
                    ax.scatter([x_last], [y_last], label=label, color=color, linewidth=.25, s=50)  # Plot the last point        
            else:    
                if len(x_data) > len(buffer):
                    x_data = x_data[:len(buffer)]
                ax.plot(x_data, buffer, label=label, color=color, linewidth=.25) # Plot the whole buffer
            ax.set_ylabel(units, color=color)
            ax.tick_params(axis=axis, labelcolor=color)
            
                

        for unit_name, unit_limits in self.plotSettingsFrame.unit_dic.items():
            min_val = getattr(self.plotSettingsFrame, unit_limits["min"]).get()
            max_val = getattr(self.plotSettingsFrame, unit_limits["max"]).get()

            # Ensure min_val and max_val are not empty
            if min_val and max_val:
                try:
                    min_val = float(min_val)
                    max_val = float(max_val)
                except ValueError:
                    continue  # Skip if conversion to float fails

                # Check if min_val and max_val are identical
                if min_val == max_val:
                    # Slightly adjust the values to avoid singular transformation
                    min_val -= 0.1
                    max_val += 0.1

                for ax, buffer_data, color, device_name, units, axis in updated_plots:
                    if units == unit_name:
                        ax.set_ylim(min_val, max_val)
         
        
        # set X Min and Max
        self.min_val_x = self.plotSettingsFrame.x_axis_unit_min.get()
        self.max_val_x = self.plotSettingsFrame.x_axis_unit_max.get()
        
        if self.min_val_x is not None and self.max_val_x is not None:
            try:
                self.min_val_x = float(self.min_val_x)
                self.max_val_x = float(self.max_val_x)
            except ValueError:
                pass  # Use pass instead of continue, as continue is invalid here

            if self.min_val_x != self.max_val_x:  
                ax.set_xlim(self.min_val_x, self.max_val_x)

        
                        
        # Collect unique axes based on their name
        unique_ax = {item[0] for item in updated_plots}
                      
        # Adjust legends for each unique axis and y-axis location
        for i, ax in enumerate(unique_ax):
            legend_offset = i * 0.15
            ax.legend(loc='upper right', bbox_to_anchor=(1, 1.0 - legend_offset))
            
            if i <= 1:
                continue
            else:
                ax_position = (i - 1) * 50
                ax.spines['right'].set_position(('outward', ax_position))  # Adjust the position of the y-axis

        self.graphDataFrame.ax1.set_xlabel(self.plotSettingsFrame.x_axis_var.get())
        self.graphDataFrame.graph.draw()
                  
    def clear_axes(self):
        for i in range(0, (No_chans +  No_chans_cust)):
            ax = getattr(self.graphDataFrame, f'ax{i+1}', None)
            if ax:
                ax.cla()
                    
    def clear_graph(self):
        # Clear the plot axes
        self.clear_axes()
            
        # Create the figure and main axis (ax1)
        self.graphDataFrame.ax1.set_title("Title")
        self.graphDataFrame.ax1.set_ylabel("Units")
        self.graphDataFrame.ax1.set_xlabel("Domain")

        # Redraw the graph
        self.graphDataFrame.graph.draw()

    def update_value_displays(self):
        # Loop over the three display slots, not all rows
        for slot in range(3):
            # Which device name is selected in this slot?
            name = self.channelSettingsFrame.display_vars[slot].get() if hasattr(self.channelSettingsFrame, "display_vars") else None
            if not name:
                # nothing selected; clear the text
                if hasattr(self.channelSettingsFrame, f"reading_value_var{slot}"):
                    getattr(self.channelSettingsFrame, f"reading_value_var{slot}").config(text="—")
                continue

            # Find index in plot_options for that device
            idx = self.channelSettingsFrame._find_index_by_device_name(name)
            if idx is None:
                getattr(self.channelSettingsFrame, f"reading_value_var{slot}").config(text="—")
                continue

            # Pull the transformed buffer for that index
            transformed_buff_name = f"transformed_buff{idx}"
            data_display = getattr(self, transformed_buff_name, None)

            # Get the label widget for this slot
            label_widget = getattr(self.channelSettingsFrame, f"reading_value_var{slot}")

            # Safely format value
            val = None
            if isinstance(data_display, (list, tuple, np.ndarray)) and len(data_display) > 0:
                val = float(data_display[0])
            elif isinstance(data_display, (int, float, np.floating)) and data_display is not None:
                val = float(data_display)
            # If nothing available yet, show dash
            if val is None:
                label_widget.config(text="—")
            else:
                label_widget.config(text="{:.4f}".format(val))


 
    def enable_buttons(self):          
        self.outputFrame.start_button['state'] = 'normal'
        self.outputFrame.clear_button['state'] = 'normal'
        self.outputFrame.record_button['state'] = 'disabled'
        self.outputFrame.save_button['state'] = 'enabled'
        self.outputFrame.reset_button['state'] = 'enabled'
        self.outputFrame.close_button['state'] = 'enabled'

        for i, row in plot_options.iterrows(): # enable boxes for all selected axis
            plot_check_box = getattr(self.plotSettingsFrame, f"plot_check_box{i}")
            plot_check_box.config(state='enabled')
                      
        getattr(self.plotSettingsFrame, "x_axis_selection").config(state='normal')
        getattr(self.plotSettingsFrame, "title_label_entry").config(state='normal')
        
    def stop_task(self):
        # Callback for the "stop task" button
        #self.continue_running = False
         
        for i in range(0, (No_chans +  No_chans_cust)):
            plot_buff_name = f"plot_buffer{i}"
            getattr(self, plot_buff_name).clear()
        
        self.continue_running = False

        self.enable_buttons()

    def save_data(self):
        filepath = filedialog.asksaveasfilename(defaultextension=".xlsx", filetypes=[("Excel files", "*.xlsx")])
        time_vec = np.arange(0, len(getattr(self, "val_buffer0")) / (self.sample_rate / 1000), 1000 / self.sample_rate)    
        data_output = {"Time [ms]": time_vec}

        for index, row in default_settings.iterrows():
            val_buff_name = f"val_buffer{index}"
            Device_Units = default_settings.loc[index, 'Units']
            Device_Name = default_settings.loc[index, 'Device Name']
            Label = f"{Device_Name} [{Device_Units}]"  
            data_output[Label] = getattr(self, val_buff_name) # Append to data_output

        data_output = pd.DataFrame(data_output)      
        data_output.to_excel(filepath, index=False)
        
    def reset_device(self):
        daq = nidaqmx.system.Device(selected_daq)
        daq.reset_device()
        


        
class ChannelSettings(tk.LabelFrame):

    def __init__(self, parent, title):
        super().__init__(parent, text=title, labelanchor='n')
        self.parent = parent
        self.x_padding = (10, 10)
        self.create_widgets()
        self.title = title
        self.popup = None  # Track the generator window
                
        
    def open_calibration(self):
        if self.popup is not None and tk.Toplevel.winfo_exists(self.popup):  # Check if window is already open
            return  # Prevent multiple windows

        self.popup = tk.Toplevel(self.parent)
        self.popup.title("Channel Calibration")
    
        column_headers = ['Channel', 'Error Val', '', '']
        
        
        for i, header in enumerate(column_headers):
            ttk.label = ttk.Label(self.popup, text=header)
            ttk.label.grid(row=2, column=i, sticky='w', padx=self.x_padding, pady=(5, 5))
                        
        self.x_disp_list = plot_options["Device Name"].tolist()

        # Create widgets dynamically based on default_settings DataFrame
        for i, row in plot_options.iterrows():
            # Channel name
            
            channel_name = ttk.Label(self.popup, text=row['Device Name'])
            channel_name.grid(row=i + 3, column=0, columnspan=1, sticky="w", padx=self.x_padding)

            error_label = ttk.Label(self.popup, width=7, text=row['Error'], borderwidth=1, relief="solid")
            error_label.grid(row=i + 3, column=1, sticky="w", padx=self.x_padding)
    
            cal_edit = ttk.Button(self.popup, text="Set", command="", width=10)
            cal_edit.grid(row=i + 3, column=2, sticky="w", padx=self.x_padding)
            
            cal_reset = ttk.Button(self.popup, text="reset", command="", width=10)
            cal_reset.grid(row=i + 3, column=3, sticky="w", padx=self.x_padding)

        
        # Update geometry to get required size
        self.popup.update_idletasks()

        # Get the required width and height, and add padding
        req_width = self.popup.winfo_reqwidth()
        req_height = self.popup.winfo_reqheight()
        padding = 40  # You can change this value

        self.popup.geometry(f"{req_width + padding}x{req_height + padding}")
        

    def create_widgets(self):
        # Headers
        column_headers = ['Channel (pick)', 'Reading', 'Units']
        for i, header in enumerate(column_headers):
            ttk.Label(self, text=header).grid(row=2, column=i, sticky='w', padx=self.x_padding, pady=(5, 5))

        # Available choices
        self.display_names = plot_options["Device Name"].tolist()

        # We'll build exactly 3 rows you can pick from
        self.display_vars = []      # StringVar for selected device per slot
        self.units_labels = []      # ttk.Label for units text per slot

        default_three = (self.display_names + [""]*3)[:3]  # pad if fewer than 3 available

        for slot in range(3):
            # selector
            var = tk.StringVar(value=default_three[slot])
            self.display_vars.append(var)

            selector = ttk.Combobox(self, textvariable=var, values=self.display_names, width=13, state='readonly')
            selector.grid(row=slot+3, column=0, sticky="w", padx=self.x_padding)

            # value label
            reading_value_var = f"reading_value_var{slot}"
            setattr(self, reading_value_var, ttk.Label(self, width=7, text="0.0000", borderwidth=1, relief="solid"))
            getattr(self, reading_value_var).grid(row=slot+3, column=1, sticky="w", padx=self.x_padding)

            # units label
            units_label = ttk.Label(self, width=5, text="")
            units_label.grid(row=slot+3, column=2, sticky="w", padx=self.x_padding)
            self.units_labels.append(units_label)

            # update units when selection changes
            selector.bind("<<ComboboxSelected>>", lambda e, s=slot: self._on_selection_changed(s))

        # Buttons row
        self.settings_edit = ttk.Button(self, text="Edit", command=self.edit_settings, width=10)
        self.settings_edit.grid(row=6, column=0, sticky='w', padx=self.x_padding, pady=(10, 0))

        self.calibrate_button = ttk.Button(self, text="Re-calibrate", command=self.open_calibration)
        self.calibrate_button.grid(row=6, column=1,  columnspan=3, sticky='w', padx=(8,5), pady=(10, 0))

        # Initialize units text for defaults
        for slot in range(3):
            self._on_selection_changed(slot)

    def _find_index_by_device_name(self, name):
        if not name:
            return None
        match = plot_options.index[plot_options["Device Name"] == name]
        return int(match[0]) if len(match) else None

    def _on_selection_changed(self, slot):
        """Update the units label when the selected device changes."""
        idx = self._find_index_by_device_name(self.display_vars[slot].get())
        if idx is None:
            self.units_labels[slot].config(text="")
        else:
            self.units_labels[slot].config(text=str(plot_options.loc[idx, "Units"]))


        
        
        
        
    def edit_settings(self):
        # Start the subprocess to run DAQ_Settings.py       
        try:
            subprocess.run(['python', 'DAQ_Settings.py'], check=True)
        except subprocess.CalledProcessError as e:
            print(f"Error running subprocess: {e}")
        
        else:
            # Subprocess ran successfully
            global default_settings, default_settings_output, default_settings_custom, combined_settings, plot_options, No_chans, No_chans_out, No_chans_cust, daq, selected_daq
            default_settings, default_settings_output, default_settings_custom, combined_settings, plot_options, No_chans, No_chans_out, No_chans_cust, daq, selected_daq = read_defaults()
            refresh_frame(self)
            refresh_frame(self.parent.graphDataFrame)
            refresh_frame(self.parent.plotSettingsFrame)


            
            
            
class PlotSettings(tk.LabelFrame):
    def __init__(self, parent, title):
        super().__init__(parent, text=title, labelanchor='n')
        self.parent = parent
        self.x_padding = (10, 10)
        self.create_widgets()
        
    def create_widgets(self):
                
        # Create a notebook widget
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill='both', expand=True)

        # Create frames for each tab
        self.plotSettings1 = ttk.Frame(self.notebook)
        self.plotSettings2 = ttk.Frame(self.notebook)
        self.OutputSettings = ttk.Frame(self.notebook)

        # Add frames to notebook as tabs
        self.notebook.add(self.plotSettings1, text='Plot Settings 1')
        self.notebook.add(self.plotSettings2, text='Plot Settings 2')
        self.notebook.add(self.OutputSettings, text='Input Settings')

        # Populate the first tab (plotSettings1)
        self.plot_label = ttk.Label(self.plotSettings1, text="Select Y-Axis")
        self.plot_label.grid(row=0, column=0, columnspan=3, sticky='w', padx=self.x_padding, pady=(5, 0))

        row_index = 0
        col_index = 0

        # First loop to create checkboxes for default_settings
        for i, row in plot_options.iterrows(): # create check boxes for all selected axis
            plot_check_var = f"plot_check_var{i}" # create name for box variables
            plot_check_box = f"plot_check_box{i}" # create name for box variables

            setattr(self, plot_check_var, tk.BooleanVar(value=False))  # create variables
            setattr(self, plot_check_box, ttk.Checkbutton(self.plotSettings1, variable=getattr(self, plot_check_var), text=row['Channel']))  # create variables
            row_index = 1 if i <= 4 else 2
            getattr(self, plot_check_box).grid(row=row_index, column=i % 5, sticky="w", padx=5, pady=5)
            col_index = i
                   
        #x- axis drop down menu   
        self.X_axis_label = ttk.Label(self.plotSettings1, text="Select X-Axis")
        self.X_axis_label.grid(row=3, column=0, columnspan=3, sticky='w', padx=self.x_padding, pady=(5, 0))

        self.plot_duration_label = ttk.Label(self.plotSettings1, text="Plot duration")
        self.plot_duration_label.grid(row=3, column=3, columnspan=2, sticky='w', padx=(0,0), pady=(10, 0))
        self.plot_duration_label.grid_remove()
                
        self.Non_chan_list = ["Full Duration", "Short Duration", "Samples"]
        self.chan_list = default_settings["Device Name"].tolist()
        self.out_chan_list = default_settings_output["Device Name"].tolist()
        self.x_axis_list = self.Non_chan_list + self.chan_list + self.out_chan_list
        
        self.x_axis_var = tk.StringVar(value="Samples") # default 

        self.x_axis_var.trace('w', self.on_x_axis_selection_change)
        self.x_axis_selection = ttk.OptionMenu(self.plotSettings1, self.x_axis_var, "", *self.x_axis_list)
        s = ttk.Style()
        s.configure("TMenubutton", background="white")
        self.x_axis_selection.grid(row=4, column=0, columnspan=3, sticky="w", padx=(5,0))
        
        self.plot_duration_entry = ttk.Entry(self.plotSettings1, width=15)
        self.plot_duration_entry.insert(0, "1")
        self.plot_duration_entry.grid(row=4, column=3, columnspan=2, sticky='w', padx=(0,0))
        self.plot_duration_entry.grid_remove()
        
        self.title_label = ttk.Label(self.plotSettings1, text="Graph Title")
        self.title_label.grid(row=5, column=0, columnspan=3, sticky='ew', padx=self.x_padding, pady=(5, 0))

        self.title_label_entry = ttk.Entry(self.plotSettings1, width=20)
        self.title_label_entry.grid(row=6, column=0, columnspan=3, sticky='ew', padx=(25, 0))
        
        self.pen_var = tk.BooleanVar(value=False)
        self.pen_box = ttk.Checkbutton(self.plotSettings1, variable=self.pen_var, text='Pen')
        self.pen_box.grid(row=6, column=4, sticky="w", padx=(0,0), pady=(0, 0))
       
        # Populate the second tab (plotSettings2)
        self.units_list = list(set(plot_options["Units"].tolist()))

        self.refresh_rate_label = ttk.Label(self.plotSettings2, text="Refresh Rate (Hz)")
        self.refresh_rate_label.grid(row=0, column=0, columnspan=3, sticky='w', padx=self.x_padding, pady=(10, 0))

        self.refresh_rate_entry = ttk.Entry(self.plotSettings2, width=15)
        self.refresh_rate_entry.insert(0, "10")
        self.refresh_rate_entry.grid(row=1, column=0, columnspan=2, sticky='w', padx=self.x_padding, pady=(0, 10))
               
        column_headers = ['Axis Unit', 'Min', 'Max']
        for i, header in enumerate(column_headers):
            label = ttk.Label(self.plotSettings2, text=header)
            label.grid(row=2, column=i, sticky='w', padx=self.x_padding, pady=(5, 5))
            
            
        self.unit_dic = {} 
        for i, unit_name in enumerate(self.units_list):
            # Channel name
            plot_unit_max = f"plot_unit_max{i}"
            plot_unit_min = f"plot_unit_min{i}"
            
            plot_unit_label = ttk.Label(self.plotSettings2, text=unit_name)
            plot_unit_label.grid(row=i + 3, column=0, columnspan=1, sticky="w", padx=self.x_padding)
            
            setattr(self, plot_unit_min, ttk.Entry(self.plotSettings2, width=7))
            getattr(self, plot_unit_min).grid(row=i + 3, column=1, sticky="w", padx=self.x_padding)
                 
            setattr(self, plot_unit_max, ttk.Entry(self.plotSettings2, width=7))
            getattr(self, plot_unit_max).grid(row=i + 3, column=2, sticky="w", padx=self.x_padding)
            
            # Update the dictionary
            self.unit_dic[unit_name] = {"min": plot_unit_min,"max": plot_unit_max}
            
        
        self.x_axis_unit_label = ttk.Label(self.plotSettings2, text="X-Axis Range")
        self.x_axis_unit_label.grid(row=len(self.units_list)+3, column=0, columnspan=1, sticky='w', padx=self.x_padding, pady=(10, 0))
        
        self.x_axis_unit_min = ttk.Entry(self.plotSettings2, width=7)
        self.x_axis_unit_min.grid(row=len(self.units_list)+3, column=1, columnspan=1, sticky="w", padx=self.x_padding, pady=(10, 0))
        
        self.x_axis_unit_max = ttk.Entry(self.plotSettings2, width=7)
        self.x_axis_unit_max.grid(row=len(self.units_list)+3, column=2, columnspan=1, sticky="w", padx=self.x_padding, pady=(10, 0))
        
        
        
            
        # Populate the third tab (Input settings)  
        self.sample_rate_label = ttk.Label(self.OutputSettings, text="Sample Rate (Hz)")
        self.sample_rate_label.grid(row=0, column=0, columnspan=2, sticky='w', padx=self.x_padding, pady=(10, 0))

        self.sample_rate_entry = ttk.Entry(self.OutputSettings)
        self.sample_rate_entry.insert(0, "1000")
        self.sample_rate_entry.grid(row=1, column=0, columnspan=2, sticky='ew', padx=self.x_padding)

        self.recording_duration_label = ttk.Label(self.OutputSettings, text="Duration to Acquire (s)")
        self.recording_duration_label.grid(row=2, column=0, columnspan=2, sticky='w', padx=self.x_padding, pady=(10, 0))

        self.recording_duration_entry = ttk.Entry(self.OutputSettings)
        self.recording_duration_entry.insert(0, "10")
        self.recording_duration_entry.grid(row=3, column=0, columnspan=2, sticky='ew', padx=self.x_padding)     
        
        
        self.auto_save_var = tk.BooleanVar(value=False)
        self.auto_save_box = ttk.Checkbutton(self.OutputSettings, variable=self.auto_save_var, text='Autosave')
        self.auto_save_box.grid(row=4, column=0, sticky="ew", padx=self.x_padding, pady=(10, 0))

                                   
    def on_x_axis_selection_change(self, *args):
        selected_x_axis = self.x_axis_var.get()

        # Iterate over available channels and update checkbox state accordingly
        for i, row in plot_options.iterrows():
            plot_check_var = getattr(self, f"plot_check_var{i}")
            plot_check_box = getattr(self, f"plot_check_box{i}")

            device_name = row["Device Name"] 

            if selected_x_axis == device_name:
                plot_check_var.set(False)
                plot_check_box.config(state='disabled')
                self.plot_duration_label.grid_remove()
                self.plot_duration_entry.grid_remove()
            elif selected_x_axis == "Short Duration": 
                plot_check_box.config(state='normal')
                self.plot_duration_label.grid() 
                self.plot_duration_entry.grid()
            else:
                plot_check_box.config(state='normal')
                self.plot_duration_label.grid_remove()
                self.plot_duration_entry.grid_remove()


                
                
class OutputSettings(tk.LabelFrame):

    def __init__(self, parent, title):
        super().__init__(parent, text=title, labelanchor='n')
        self.parent = parent
        self.x_padding = (30, 30)
        self.create_widgets()

    def create_widgets(self):       
        self.start_button = ttk.Button(self, text="Run", command=self.parent.start_task)
        self.start_button.grid(row=8, column=0, sticky='w', padx=(10,0), pady=(10, 0))
        
        self.is_recording = False  # State variable for the toggle button
        
        self.record_button = ttk.Button(self, text="Start Recording", state="disabled")
        self.record_button.grid(row=8, column=1, sticky='e', padx=(0,0), pady=(10, 0))

        self.stop_button = ttk.Button(self, text="Stop", command=self.parent.stop_task)
        self.stop_button.grid(row=8, column=2, sticky='e', padx=(0,0), pady=(10, 0))
        
        self.clear_button = ttk.Button(self, text="Clear Graph", command=self.parent.clear_graph)        
        self.clear_button.grid(row=9, column=0, columnspan=3, sticky='ew', padx=self.x_padding, pady=(10, 0))

        self.save_button = ttk.Button(self, text="Save Data", command=self.parent.save_data)
        self.save_button.grid(row=10, column=0, columnspan=3, sticky='ew', padx=(30, 30), pady=(10, 0))
        
        self.reset_button = ttk.Button(self, text="Reset DAQ", command=self.parent.reset_device)
        self.reset_button.grid(row=11, column=0, columnspan=3, sticky='ew', padx=self.x_padding, pady=(10, 0))

        self.close_button = ttk.Button(self, text="Close", command=root.destroy)
        self.close_button.grid(row=12, column=0, columnspan=3, sticky='ew', padx=self.x_padding, pady=(10, 0))
    
class GraphData(tk.LabelFrame):
    def __init__(self, parent):
        super().__init__(parent, text="Graph Data", labelanchor='n')
        self.parent = parent
        self.create_widgets()

    def create_widgets(self):
        self.figure = Figure(figsize=(10, 6), dpi=100)
        self.ax1 = self.figure.add_subplot(111)
        ax = self.ax1
            
        self.ax1.set_title("Title")
        self.ax1.set_ylabel("Units")
        self.ax1.set_xlabel("Domain")
        
        self.graph = FigureCanvasTkAgg(self.figure, self)
        self.graph.get_tk_widget().grid(row=0, column=0, sticky="nsew")

        # Adding interactive cursor
        self.cursor = Cursor(self.ax1, useblit=True, color='red', linewidth=1)
        
        self.toolbar_frame = tk.Frame(self)
        self.toolbar_frame.grid(row=1, column=0, sticky="nsew")
        self.toolbar = NavigationToolbar2Tk(self.graph, self.toolbar_frame)
        self.toolbar.update()


class Functions(tk.LabelFrame):
    def __init__(self, parent, title):
        super().__init__(parent, text=title, labelanchor='n')
        self.parent = parent
        self.create_widgets()
        self.generation_thread = None
        self.stop_event = threading.Event()
        self.popup = None  # Track the generator window
        
  
    def create_widgets(self):
        self.start_gen_button = ttk.Button(self, text="Function Generator", command=self.open_generator)
        self.start_gen_button.grid(row=1, column=1, columnspan=1, sticky="w", padx=(10, 5), pady=(3, 0))
        
    def open_generator(self):
        if self.popup is not None and tk.Toplevel.winfo_exists(self.popup):  # Check if window is already open
            return  # Prevent multiple windows

        self.start_gen_button.config(state="disabled")  # Disable button

        self.popup = tk.Toplevel(self.parent)
        self.popup.title("Function Generator")
        self.popup.geometry("500x200")

        # Re-enable the button when the window is closed
        self.popup.protocol("WM_DELETE_WINDOW", self.on_close)

        # Row 0: Labels
        ttk.Label(self.popup, text="Select Function:").grid(row=0, column=0, sticky='w', padx=(25,5), pady=(5, 0))
        ttk.Label(self.popup, text="Amplitude:").grid(row=0, column=1, sticky='w', padx=(5,5), pady=(5, 0))
        ttk.Label(self.popup, text="DC Offset:").grid(row=0, column=2, sticky='w', padx=(5,5), pady=(5, 0))

        # Row 1: Controls
        self.waveform_type_var = tk.StringVar(value="Sine")
        self.waveform_type_combobox = ttk.Combobox(
            self.popup, textvariable=self.waveform_type_var, values=["Sine", "Square", "Triangular", "Sawtooth", "DC"], width=7
        )
        self.waveform_type_combobox.grid(row=1, column=0, sticky="w", padx=(25,5))

        self.amplitude_entry = ttk.Entry(self.popup, width=7)
        self.amplitude_entry.insert(0, "2.5")
        self.amplitude_entry.grid(row=1, column=1, sticky="w", padx=(5,5))

        self.dc_offset_entry = ttk.Entry(self.popup, width=7)
        self.dc_offset_entry.insert(0, "2.5")
        self.dc_offset_entry.grid(row=1, column=2, sticky="w", padx=(5,5))

        self.waveform_type_var.trace_add('write', self.on_waveform_select)

        self.start_button = ttk.Button(self.popup, text="Start Generation", command=self.start_generation)
        self.start_button.grid(row=1, column=3, sticky="w", padx=(5,5))

        self.stop_button = ttk.Button(self.popup, text="Stop Generation", command=self.stop_generation, state='disabled')
        self.stop_button.grid(row=1, column=4, sticky="w", padx=(5,5))

        # Row 2: Frequency & Multiplier
        ttk.Label(self.popup, text="Frequency (Hz):").grid(row=2, column=0, sticky='w', padx=(25,5), pady=(5, 0))
        ttk.Label(self.popup, text="Multiplier:").grid(row=2, column=1, columnspan=2, sticky='ew', padx=(25,5), pady=(5, 0))

        # Row 3: Frequency Entry & Multiplier Buttons
        self.frequency_entry = ttk.Entry(self.popup, width=7)
        self.frequency_entry.insert(0, "0.5")
        self.frequency_entry.grid(row=3, column=0, sticky='w', padx=(25,5))

        self.multiplier_var = tk.IntVar(value=10)
        multiplier_options = [("x1", 1), ("x10", 10), ("x100", 100), ("x1000", 1000)]
        for index, (text, value) in enumerate(multiplier_options):
            ttk.Radiobutton(self.popup, text=text, variable=self.multiplier_var, value=value).grid(row=3, column=index+1, sticky='w', padx=(5,5))

        ttk.Button(self.popup, text="Close", command=self.on_close).grid(row=4, column=0, columnspan=5, pady=10)


    def on_close(self):
        """Re-enable the button when the generator window is closed."""
        self.stop_generation() # stop generations if thsi is running
        self.popup.destroy()
        self.start_gen_button.config(state="normal")  # Re-enable button
        self.popup = None  # Reset popup reference
  
        
    def on_waveform_select(self, *args):
        """Disables the amplitude entry if 'DC' is selected."""
        if self.waveform_type_var.get() == "DC":
            self.amplitude_entry.config(state='disabled')
        else:
            self.amplitude_entry.config(state='normal')
            
                   
    def start_generation(self):
        """Starts waveform generation in a separate thread."""
        if self.generation_thread and self.generation_thread.is_alive():
            return  # Prevent multiple threads from starting
        
        # Reset stop event
        self.stop_event.clear()

        try:
            frequency = float(self.frequency_entry.get())
            multiplier = self.multiplier_var.get()
            waveform_type = self.waveform_type_var.get()
            dc_offset = float(self.dc_offset_entry.get())
            amplitude = float(self.amplitude_entry.get()) if waveform_type != "DC" else 0
        except ValueError:
            print("Invalid input values")
            return
            

        physical_channel_o = f"{selected_daq}/{default_settings_output['Channel'][0]}"
        min_voltage_o = default_settings_output['Min Voltage'][0]
        max_voltage_o = default_settings_output['Max Voltage'][0]
          
        
        self.task2 = nidaqmx.Task()
        self.task2.ao_channels.add_ao_voltage_chan(physical_channel_o, min_val=min_voltage_o, max_val=max_voltage_o) 
        self.task2.start()

        
        # Ensure sample_rate is retrieved from user settings
        self.parent.get_user_settings()  # This ensures self.sample_rate is updated
        
        self.generation_thread = threading.Thread(
            target=self.generate_waveform,
            args=(self.task2, waveform_type, frequency, multiplier, amplitude, dc_offset, self.parent.sample_rate),
            daemon=True  # This prevents blocking issues
        )
        self.generation_thread.start()

        # Update button states
        self.start_button.config(state='disabled')
        self.stop_button.config(state='normal')
        

    def generate_waveform(self, task, waveform_type, frequency, multiplier, amplitude, dc_offset, sample_rate):
        """Generates and writes the waveform to the NI-DAQ device."""
        #sample_rate = 10000
        time_values = np.linspace(0, 1, sample_rate, endpoint=False)

        while not self.stop_event.is_set():
            if waveform_type == "Sine":
                waveform = (amplitude * np.sin(2 * np.pi * frequency * multiplier * time_values)) + dc_offset
            elif waveform_type == "Square":
                waveform = (amplitude * np.sign(np.sin(2 * np.pi * frequency * multiplier * time_values))) + dc_offset
            elif waveform_type == "Triangular":
                waveform = (amplitude * np.abs(2 * (frequency * multiplier * time_values - np.floor(0.5 + frequency * multiplier * time_values))) - 1) + dc_offset
            elif waveform_type == "Sawtooth":
                waveform = (amplitude * (frequency * multiplier * time_values - np.floor(0.5 + frequency * multiplier * time_values))) + dc_offset
            elif waveform_type == "DC":
                waveform = np.full(sample_rate, dc_offset)

            waveform = np.clip(waveform, 0, 5)  # Ensure voltage stays within range
            task.write(waveform, auto_start=True)

            # Check every small interval if we should stop
            time.sleep(0.01)

        # Stop waveform generation
        task.write(np.zeros(sample_rate), auto_start=True)
        task.close()
    


    # Function to stop waveform generation
    def stop_generation(self):
        """Stops waveform generation by setting the stop event."""
        
        #self.generation_thread.clear()
        if self.generation_thread and self.generation_thread.is_alive():
            self.stop_event.set()  # Signal the thread to stop
            self.generation_thread = None  # Remove reference to the thread
        
        # Reset button states
        self.start_button.config(state='normal')
        self.stop_button.config(state='disabled')

               
        
class Apps(tk.LabelFrame):
    def __init__(self, parent, title):
        super().__init__(parent, text=title, labelanchor='n')
        
        self.parent = parent
        self.create_widgets()

    def create_widgets(self):
        
        self.Sweep_button = ttk.Button(self, text="App1")
        self.Sweep_button.grid(row=1, column=1, columnspan=1, sticky="w", padx=(10,5), pady=(3, 0))
        
        self.Response_button = ttk.Button(self, text="App2")
        self.Response_button.grid(row=1, column=2, columnspan=1, sticky="w", padx=(10,5), pady=(3, 0))
        
        self.triggers_button = ttk.Button(self, text="App3")
        self.triggers_button.grid(row=1, column=3, columnspan=1, sticky="w", padx=(10,5), pady=(3, 0))

if __name__ == "__main__":
    root = tk.Tk()
    app = VoltageContinuousInput(master=root)
    
    app.functions_frame = Functions(root, "Function Controls")
    
    app.mainloop()
