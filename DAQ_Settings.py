import tkinter as tk
from tkinter import ttk
import nidaqmx
import pandas as pd
from tkinter import messagebox

class ChannelSettings(tk.LabelFrame):      
    def __init__(self, parent, title):  
        super().__init__(parent, text=title, labelanchor='n')
        self.parent = parent
        self.x_padding = (10, 10)
        self.entry_widgets = {}  # Dictionary to store entry widgets
        self.create_widgets()
        
    def create_widgets(self):
        daqSys = nidaqmx.system.System()
        daqList = daqSys.devices.device_names
        self.chosen_daq = tk.StringVar()
        #self.chosen_daq.set(daqList[0])
        
        self.checkbutton_vars = {}
        self.Terminal_options_list = ["RSE", "NRSE", "DIFFERENTIAL"]
        self.Default_terminal = "RSE"
        
        # DAQ selection
        self.daq_selection_label = ttk.Label(self, text="Select DAQ")
        self.daq_selection_label.grid(row=0, column=0, columnspan=1, sticky='w', padx=self.x_padding, pady=(5, 0))
        
        self.daq_selection_menu = ttk.OptionMenu(self, self.chosen_daq, daqList[0], *daqList)

        s = ttk.Style()
        s.configure("TMenubutton", background="white")
        self.daq_selection_menu.grid(row=1, column=0, columnspan=1, sticky="w", padx=self.x_padding, pady=10)

        # Create a tabbed frame
        self.tab_control = ttk.Notebook(self)
        self.tab_control.grid(row=2, column=0, columnspan=2, padx=self.x_padding)

        # Add tabs for input and output channels
        self.tab1 = ttk.Frame(self.tab_control)
        self.tab_control.add(self.tab1, text='Input Channels')
        
        self.tab2 = ttk.Frame(self.tab_control)
        self.tab_control.add(self.tab2, text='Output Channels')
        
        self.tab3 = ttk.Frame(self.tab_control)
        self.tab_control.add(self.tab3, text='Custom Channels')

        self.tab4 = ttk.Frame(self.tab_control)
        self.tab_control.add(self.tab4, text='Expert Settings')
        
        self.tab5 = ttk.Frame(self.tab_control)
        self.tab_control.add(self.tab5, text='Transducers')
        
        
        # ⬇️ Move "Expert Settings" to the end, i.e., after "Transducers" workaround to move expert settings to the end
        self.tab_control.insert('end', self.tab4)
        
        # Add a save button
        self.save_button = ttk.Button(self, text="Save Settings", command=self.save_settings)
        self.save_button.grid(row=3, column=0, columnspan=1, pady=10, sticky="e")

        self.close_button = ttk.Button(self, text="Close", command=self.close_window)
        self.close_button.grid(row=3, column=1, columnspan=1, pady=10, sticky="w")

        self.chosen_daq.trace_add('write', self.update_channels)  # Add a trace to update channels when selection changes
        self.update_channels()  # Initial channel update

        
    def get_transducer_names(self):
        """Non-empty 'Hardware' names from Transducers tab, de-duplicated (keep order)."""
        names = []
        for i in range(1, getattr(self, "no_of_transducers", 0) + 1):
            hw = self.strip_value(self.entry_widgets.get(f"trans_hw{i}", None).get()) if self.entry_widgets.get(f"trans_hw{i}") else ""
            if hw:
                names.append(hw)
        seen, uniq = set(), []
        for n in names:
            if n not in seen:
                seen.add(n)
                uniq.append(n)
        return uniq

    def get_transducer_dict(self):
        """
        Build a dict: name -> settings (Min V, Max V, Terminal Configuration, Mx, B, Units)
        from the Transducers tab.
        """
        d = {}
        for i in range(1, getattr(self, "no_of_transducers", 0) + 1):
            hw_widget = self.entry_widgets.get(f"trans_hw{i}")
            if not hw_widget:
                continue
            name = self.strip_value(hw_widget.get())
            if not name:
                continue
            d[name] = {
                "Min Voltage": self.entry_widgets[f"trans_minv{i}"].get() if self.entry_widgets.get(f"trans_minv{i}") else "",
                "Max Voltage": self.entry_widgets[f"trans_maxv{i}"].get() if self.entry_widgets.get(f"trans_maxv{i}") else "",
                "Terminal Configuration": self.entry_widgets[f"trans_term{i}"].get() if self.entry_widgets.get(f"trans_term{i}") else "",
                "Mx": self.entry_widgets[f"trans_mx{i}"].get() if self.entry_widgets.get(f"trans_mx{i}") else "",
                "B":  self.entry_widgets[f"trans_b{i}"].get() if self.entry_widgets.get(f"trans_b{i}") else "",
                "Units": self.entry_widgets[f"trans_units{i}"].get() if self.entry_widgets.get(f"trans_units{i}") else "",
            }
        return d

    
    def refresh_transducer_dropdowns(self):
        """Refresh all 'Transducer' comboboxes in the input channels grid."""
        options = self.get_transducer_names()
        for i in range(1, len(self.device.ai_physical_chans) + 1):
            cb = self.entry_widgets.get(f"transducer_sel{i}")
            if cb:
                cb['values'] = options
                
    def set_row_field_state(self, row_index, state):
        """Enable/disable the six fields in Input row 'row_index'."""
        # state: 'normal' or 'disabled'
        self.entry_widgets[f"min_voltage_entry{row_index}"].config(state=state)
        self.entry_widgets[f"max_voltage_entry{row_index}"].config(state=state)
        self.entry_widgets[f"ter_selection_var{row_index}"].config(state=state)  # Combobox
        self.entry_widgets[f"mx_plus_entry{row_index}"].config(state=state)
        self.entry_widgets[f"b_entry{row_index}"].config(state=state)
        self.entry_widgets[f"units_entry{row_index}"].config(state=state)

    def apply_transducer_to_row(self, row_index, transducer_name):
        """
        If transducer_name is a concrete choice, copy its settings into the row and disable those fields.
        If blank, enable the fields (do not overwrite).
        """
        if not transducer_name:
            # re-enable fields
            self.set_row_field_state(row_index, 'normal')
            return

        tdict = self.get_transducer_dict()
        if transducer_name not in tdict:
            # Not found — just enable fields so user can edit
            self.set_row_field_state(row_index, 'normal')
            return

        vals = tdict[transducer_name]

        # Fill values, then disable
        def _set(widget, value):
            widget.config(state='normal')
            widget.delete(0, tk.END)
            widget.insert(0, "" if value is None else str(value))
            widget.config(state='disabled')

        _set(self.entry_widgets[f"min_voltage_entry{row_index}"], vals.get("Min Voltage", ""))
        _set(self.entry_widgets[f"max_voltage_entry{row_index}"], vals.get("Max Voltage", ""))
        # Terminal config is a Combobox
        ter_cb = self.entry_widgets[f"ter_selection_var{row_index}"]
        ter_cb.config(state='normal')
        ter_cb.set(vals.get("Terminal Configuration", self.Default_terminal))
        ter_cb.config(state='disabled')
        _set(self.entry_widgets[f"mx_plus_entry{row_index}"], vals.get("Mx", ""))
        _set(self.entry_widgets[f"b_entry{row_index}"], vals.get("B", ""))
        _set(self.entry_widgets[f"units_entry{row_index}"], vals.get("Units", ""))

                
        
    def update_channels(self, *args):
        # Clear existing widgets
        for widget in self.tab1.winfo_children():
            widget.destroy()
        for widget in self.tab2.winfo_children():
            widget.destroy()
        for widget in self.tab3.winfo_children():
            widget.destroy()

        # Get the selected device
        selected_device_name = self.chosen_daq.get()
        self.device = nidaqmx.system.Device(selected_device_name)

        self.chan_names = []

        for channel in self.device.ai_physical_chans:
            channel_name = channel.name.split('/')[1]
            self.chan_names.append(channel_name)

        # Get a list of physical channels for the device
        physical_ai_channels = self.device.ai_physical_chans
        num_ai_channels = len(self.device.ai_physical_chans)

        physical_ao_channels = self.device.ao_physical_chans
        num_ao_channels = len(self.device.ao_physical_chans)

        # Define widget labels for input channels
        labels = ["Channel", "Transducer", "Min V", "Max V", "Terminal Configuration", "Mx", "b", "Units", "Device Name", "Selected Channel"]
        label_length = [8, 20, 7, 7, 13, 6, 6, 7, 20, 8]

        # Create labels for input channels
        for i, label in enumerate(labels):
            ttk.Label(self.tab1, text=label, width=label_length[i], wraplength=label_length[i]*8).grid(row=0, column=i, padx=5, pady=5)

        for i, channel in enumerate(physical_ai_channels):
            r = i + 1

            # Channel (disabled)
            self.entry_widgets[f"Channel_entry{r}"] = ttk.Entry(self.tab1, width=label_length[0])
            self.entry_widgets[f"Channel_entry{r}"].insert(0, channel.name.split('/')[1])
            self.entry_widgets[f"Channel_entry{r}"].grid(row=r, column=0, padx=5, pady=5, sticky='w')
            self.entry_widgets[f"Channel_entry{r}"]['state'] = 'disabled'

            self.entry_widgets[f"transducer_sel{r}"] = ttk.Combobox(self.tab1, values=[], width=label_length[1], state="readonly")
            self.entry_widgets[f"transducer_sel{r}"].grid(row=r, column=1, padx=5, pady=5, sticky='w')
            self.entry_widgets[f"transducer_sel{r}"].bind("<Button-1>", lambda e, cs=r: self.refresh_transducer_dropdowns())
            
            
            # Populate values on open / click
            def _on_transducer_click(event, row=r):
                opts = [""] + self.get_transducer_names()
                cb = self.entry_widgets[f"transducer_sel{row}"]
                cb['values'] = opts
            self.entry_widgets[f"transducer_sel{r}"].bind("<Button-1>", _on_transducer_click)

            # React to selection
            def _on_transducer_selected(event, row=r):
                chosen = self.entry_widgets[f"transducer_sel{row}"].get()
                self.apply_transducer_to_row(row, chosen)

            self.entry_widgets[f"transducer_sel{r}"].bind("<<ComboboxSelected>>", _on_transducer_selected)

            
            self.entry_widgets[f"min_voltage_entry{r}"] = ttk.Entry(self.tab1, width=label_length[2])
            self.entry_widgets[f"min_voltage_entry{r}"].grid(row=r, column=2, padx=5, pady=5, sticky='w')

            self.entry_widgets[f"max_voltage_entry{r}"] = ttk.Entry(self.tab1, width=label_length[3])
            self.entry_widgets[f"max_voltage_entry{r}"].grid(row=r, column=3, padx=5, pady=5, sticky='w')

            self.entry_widgets[f"ter_selection_var{r}"] = ttk.Combobox(self.tab1, values=self.Terminal_options_list, width=label_length[4])
            self.entry_widgets[f"ter_selection_var{r}"].set(self.Default_terminal)
            self.entry_widgets[f"ter_selection_var{r}"].grid(row=r, column=4, padx=5, pady=5, sticky='w')

            self.entry_widgets[f"mx_plus_entry{r}"] = ttk.Entry(self.tab1, width=label_length[5])
            self.entry_widgets[f"mx_plus_entry{r}"].grid(row=r, column=5, padx=5, pady=5, sticky='w')

            self.entry_widgets[f"b_entry{r}"] = ttk.Entry(self.tab1, width=label_length[6])
            self.entry_widgets[f"b_entry{r}"].grid(row=r, column=6, padx=5, pady=5, sticky='w')

            self.entry_widgets[f"units_entry{r}"] = ttk.Entry(self.tab1, width=label_length[7])
            self.entry_widgets[f"units_entry{r}"].grid(row=r, column=7, padx=5, pady=5, sticky='w')

            self.entry_widgets[f"Device_name{r}"] = ttk.Entry(self.tab1, width=label_length[8])
            self.entry_widgets[f"Device_name{r}"].grid(row=r, column=8, padx=5, pady=5, sticky='w')

            self.checkbutton_vars[f"Selected Channel{r}"] = tk.StringVar()
            self.checkbutton_vars[f"Selected Channel{r}"].set(0)
            self.entry_widgets[f"Selected Channel{r}"] = ttk.Checkbutton(self.tab1, width=7, variable=self.checkbutton_vars[f"Selected Channel{r}"])
            self.entry_widgets[f"Selected Channel{r}"].grid(row=r, column=9, padx=5, pady=5, sticky='w')
            self.entry_widgets[f"Selected Channel{r}"].bind("<Button-1>", self.clear_cust_dropdown)

        
        

        
        # Define widget labels for output channels
        labels_ao = ["Channel", "Min V", "Max V", "Terminal Configuration", "Device Name", "Selected Channel"]
        labels_ao_length = [8, 7, 7, 13, 20, 8]  # corrected the typo in 'label_length'
        
        # Create labels for output channels
        for i, label in enumerate(labels_ao):
            ttk.Label(self.tab2, text=label, width=labels_ao_length[i], wraplength=labels_ao_length[i]*8).grid(row=0, column=i, padx=5, pady=5, sticky='w')


        # Create entries for each output channel
        for i, channel in enumerate(physical_ao_channels):
            
            self.entry_widgets[f"Channel_entry_output{i+1}"] = ttk.Entry(self.tab2, width=labels_ao_length[0])
            self.entry_widgets[f"Channel_entry_output{i+1}"].insert(0, channel.name.split('/')[1])
            self.entry_widgets[f"Channel_entry_output{i+1}"].grid(row=i+1, column=0, padx=5, pady=5, sticky='w')
            self.entry_widgets[f"Channel_entry_output{i+1}"]['state'] = 'disabled'

            self.entry_widgets[f"min_voltage_entry_output{i+1}"] = ttk.Entry(self.tab2, width=labels_ao_length[1])
            self.entry_widgets[f"min_voltage_entry_output{i+1}"].grid(row=i+1, column=1, padx=5, pady=5, sticky='w')

            self.entry_widgets[f"max_voltage_entry_output{i+1}"] = ttk.Entry(self.tab2, width=labels_ao_length[2])
            self.entry_widgets[f"max_voltage_entry_output{i+1}"].grid(row=i+1, column=2, padx=5, pady=5, sticky='w')

            self.entry_widgets[f"ter_selection_var_output{i+1}"] = ttk.Combobox(self.tab2, values=self.Terminal_options_list, width=labels_ao_length[3])
            self.entry_widgets[f"ter_selection_var_output{i+1}"].set(self.Default_terminal)
            self.entry_widgets[f"ter_selection_var_output{i+1}"].grid(row=i+1, column=3, padx=5, pady=5, sticky='w')

            self.entry_widgets[f"Device_name_output{i+1}"] = ttk.Entry(self.tab2, width=labels_ao_length[4])
            self.entry_widgets[f"Device_name_output{i+1}"].grid(row=i+1, column=4, padx=5, pady=5, sticky='w')
            
            self.checkbutton_vars[f"Selected Channel_output{i+1}"] = tk.StringVar()
            self.checkbutton_vars[f"Selected Channel_output{i+1}"].set(0)
            self.entry_widgets[f"Selected Channel_output{i+1}"] = ttk.Checkbutton(self.tab2, width=7, variable=self.checkbutton_vars[f"Selected Channel_output{i+1}"])
            self.entry_widgets[f"Selected Channel_output{i+1}"].grid(row=i+1, column=5, padx=5, pady=5, sticky='w')
            

        
        # Define widget labels for Custom channels functions
        labels_co = ['Channel',"First Channel", "Operation", "Second Channel", "Device Name", "Units", "Selected Channel"]
        labels_co_length = [8, 13, 10, 15, 20, 7, 8]
        
        # Create labels for output channels
        for i, label in enumerate(labels_co):
            ttk.Label(self.tab3, text=label, width=labels_co_length[i], wraplength=labels_co_length[i]*8).grid(row=0, column=i, padx=2, pady=5) 
         
        self.no_of_cus_chans = 2 # number of custom operation channels to be created can be expanded       
        Opereation_list = ["+", "-", "x", "÷"]
    

        # Create entries for each custom channel
        for i in range(0, self.no_of_cus_chans):
            
            self.entry_widgets[f"Channel_entry_cust{i+1}"] = ttk.Entry(self.tab3, width=labels_co_length[0])
            self.entry_widgets[f"Channel_entry_cust{i+1}"].insert(0, f"cu{i}")
            self.entry_widgets[f"Channel_entry_cust{i+1}"].grid(row=i+1, column=0, padx=5, pady=5, sticky='w')
            self.entry_widgets[f"Channel_entry_cust{i+1}"]['state'] = 'disabled'
            
            
            self.entry_widgets[f"first_chan_sel{i+1}"] = ttk.Combobox(self.tab3, values=self.get_available_channels(), width=labels_co_length[1])
            self.entry_widgets[f"first_chan_sel{i+1}"].grid(row=i+1, column=1, padx=5, pady=5, sticky='w')
            self.entry_widgets[f"first_chan_sel{i+1}"].bind("<Button-1>", self.update_chan_sel)
            
            self.entry_widgets[f"operation_sel{i+1}"] = ttk.Combobox(self.tab3, values=Opereation_list, width=labels_co_length[2])
            self.entry_widgets[f"operation_sel{i+1}"].grid(row=i+1, column=2, padx=5, pady=5, sticky='w')

            self.entry_widgets[f"sec_chan_sel{i+1}"] = ttk.Combobox(self.tab3, values=self.get_available_channels(), width=labels_co_length[3])
            self.entry_widgets[f"sec_chan_sel{i+1}"].grid(row=i+1, column=3, padx=5, pady=5, sticky='w')
            self.entry_widgets[f"sec_chan_sel{i+1}"].bind("<Button-1>", self.update_chan_sel)

            self.entry_widgets[f"custom_chan_name{i+1}"] = ttk.Entry(self.tab3, width=labels_co_length[4])
            self.entry_widgets[f"custom_chan_name{i+1}"].grid(row=i+1, column=4, padx=5, pady=5, sticky='w')
            
            self.entry_widgets[f"cust_units_entry{i+1}"] = ttk.Entry(self.tab3, width=labels_co_length[5])       
            self.entry_widgets[f"cust_units_entry{i+1}"].grid(row=i+1, column=5, padx=5, pady=5, sticky='w')
            
            self.checkbutton_vars[f"Selected Channel_Custom{i+1}"] = tk.StringVar()
            self.checkbutton_vars[f"Selected Channel_Custom{i+1}"].set(0)
            self.entry_widgets[f"Selected Channel_Custom{i+1}"] = ttk.Checkbutton(self.tab3, width=7, variable=self.checkbutton_vars[f"Selected Channel_Custom{i+1}"])
            self.entry_widgets[f"Selected Channel_Custom{i+1}"].grid(row=i+1, column=6, padx=5, pady=5, sticky='w')
            
            
            
        # ---------------- Expert Settings layout: 3 sections side-by-side ----------------
        # Make tab4 use three columns so frames sit next to each other
        self.tab4.columnconfigure(0, weight=1)
        self.tab4.columnconfigure(1, weight=1)
        self.tab4.columnconfigure(2, weight=1)

        # "Use default" checkbox at the top spanning all columns
        self.use_default_var = tk.BooleanVar(value=True)
        self.use_default_chk = ttk.Checkbutton(
            self.tab4,
            text="Use default",
            variable=self.use_default_var,
            command=self.on_use_default_toggle
        )
        self.use_default_chk.grid(row=0, column=0, columnspan=3, padx=5, pady=(5, 10), sticky='w')

        # --- Defaults for everything in these sections ---
        self.expert_defaults = {
            # Sample Settings
            "Sample Rate (Hz)": "1000",
            "Decimation %": "0",

            # Plot Settings
            "Max Buffer Size": "1024",
            "Refresh Rate (Hz)": "10",
            "Max Plot Points": "200000",

            # Filters
            "Filter Type": "Bandstop",
            "Filter Order": "2",
            "Filter Cutoff (Hz)": "2",
            "Filter fh (Hz)": "0",
        }

        # ---------------- Sample Settings ----------------
        self.sample_frame = ttk.LabelFrame(self.tab4, text="Sample Settings")
        self.sample_frame.grid(row=1, column=0, padx=5, pady=5, sticky='nsew')

        ttk.Label(self.sample_frame, text="Sample Rate (Hz)").grid(row=0, column=0, padx=5, pady=5, sticky='w')
        self.sample_rate_entry = ttk.Entry(self.sample_frame, width=14)
        self.sample_rate_entry.grid(row=0, column=1, padx=5, pady=5, sticky='w')
        self.sample_rate_entry.insert(0, self.expert_defaults["Sample Rate (Hz)"])

        ttk.Label(self.sample_frame, text="Decimation %").grid(row=1, column=0, padx=5, pady=5, sticky='w')
        self.decimation_sp = tk.Spinbox(self.sample_frame, from_=0, to=100, width=6)   # 0–100%
        self.decimation_sp.grid(row=1, column=1, padx=5, pady=5, sticky='w')
        self.decimation_sp.delete(0, tk.END)
        self.decimation_sp.insert(0, self.expert_defaults["Decimation %"])

        self.sample_widgets = {
            "Sample Rate (Hz)": self.sample_rate_entry,
            "Decimation %": self.decimation_sp,
        }

        # ---------------- Plot Settings ----------------
        self.plot_frame = ttk.LabelFrame(self.tab4, text="Plot Settings")
        self.plot_frame.grid(row=1, column=1, padx=5, pady=5, sticky='nsew')

        ttk.Label(self.plot_frame, text="Max Buffer Size").grid(row=0, column=0, padx=5, pady=5, sticky='w')
        self.max_buffer_entry = ttk.Entry(self.plot_frame, width=14)
        self.max_buffer_entry.grid(row=0, column=1, padx=5, pady=5, sticky='w')
        self.max_buffer_entry.insert(0, self.expert_defaults["Max Buffer Size"])

        ttk.Label(self.plot_frame, text="Refresh Rate (Hz)").grid(row=1, column=0, padx=5, pady=5, sticky='w')
        self.refresh_entry = ttk.Entry(self.plot_frame, width=14)
        self.refresh_entry.grid(row=1, column=1, padx=5, pady=5, sticky='w')
        self.refresh_entry.insert(0, self.expert_defaults["Refresh Rate (Hz)"])

        ttk.Label(self.plot_frame, text="Max Plot Points").grid(row=2, column=0, padx=5, pady=5, sticky='w')
        self.max_points_entry = ttk.Entry(self.plot_frame, width=14)
        self.max_points_entry.grid(row=2, column=1, padx=5, pady=5, sticky='w')
        self.max_points_entry.insert(0, self.expert_defaults["Max Plot Points"])

        self.plot_widgets = {
            "Max Buffer Size": self.max_buffer_entry,
            "Refresh Rate (Hz)": self.refresh_entry,
            "Max Plot Points": self.max_points_entry,
        }

        # ---------------- Filters (as a section) ----------------
        self.filter_frame = ttk.LabelFrame(self.tab4, text="Filters")
        self.filter_frame.grid(row=1, column=2, padx=5, pady=5, sticky='nsew')

        self.filter_types = ["off", "lowpass", "highpass", "bandpass", "bandstop", "median"]

        ttk.Label(self.filter_frame, text="Filter Type").grid(row=0, column=0, padx=5, pady=5, sticky='w')
        self.filter_type_cb = ttk.Combobox(self.filter_frame, values=self.filter_types, state="readonly", width=12)
        self.filter_type_cb.grid(row=0, column=1, padx=5, pady=5, sticky='w')
        self.filter_type_cb.set(self.expert_defaults["Filter Type"])

        ttk.Label(self.filter_frame, text="Order").grid(row=1, column=0, padx=5, pady=5, sticky='w')
        self.filter_order_sp = tk.Spinbox(self.filter_frame, from_=1, to=12, width=6)
        self.filter_order_sp.grid(row=1, column=1, padx=5, pady=5, sticky='w')
        self.filter_order_sp.delete(0, tk.END)
        self.filter_order_sp.insert(0, self.expert_defaults["Filter Order"])

        ttk.Label(self.filter_frame, text="Cutoff (Hz)").grid(row=2, column=0, padx=5, pady=5, sticky='w')
        self.filter_fc_entry = ttk.Entry(self.filter_frame, width=12)
        self.filter_fc_entry.grid(row=2, column=1, padx=5, pady=5, sticky='w')
        self.filter_fc_entry.insert(0, self.expert_defaults["Filter Cutoff (Hz)"])

        ttk.Label(self.filter_frame, text="fh (Hz)").grid(row=3, column=0, padx=5, pady=5, sticky='w')
        self.filter_fh_entry = ttk.Entry(self.filter_frame, width=12)
        self.filter_fh_entry.grid(row=3, column=1, padx=5, pady=5, sticky='w')
        self.filter_fh_entry.insert(0, self.expert_defaults["Filter fh (Hz)"])

        self.filter_widgets = {
            "Filter Type": self.filter_type_cb,
            "Filter Order": self.filter_order_sp,
            "Filter Cutoff (Hz)": self.filter_fc_entry,
            "Filter fh (Hz)": self.filter_fh_entry,
        }

        # Common helper: enable/disable filter fields based on type
        def _update_filter_fields(*_):
            t = self.filter_type_cb.get().lower()
            def set_state(widget, state):
                try:
                    widget.config(state=state)
                except Exception:
                    widget.configure(state=state)

            # default all disabled except type
            set_state(self.filter_order_sp, 'disabled')
            set_state(self.filter_fc_entry, 'disabled')
            set_state(self.filter_fh_entry, 'disabled')

            if t == "median":
                set_state(self.filter_order_sp, 'normal')
            elif t in ("lowpass", "highpass"):
                set_state(self.filter_order_sp, 'normal')
                set_state(self.filter_fc_entry, 'normal')
            elif t in ("bandpass", "bandstop"):
                set_state(self.filter_order_sp, 'normal')
                set_state(self.filter_fc_entry, 'normal')
                set_state(self.filter_fh_entry, 'normal')

        self.filter_type_cb.bind("<<ComboboxSelected>>", _update_filter_fields)
        _update_filter_fields()

        # Unified map of all expert widgets to apply defaults/locks
        self.all_expert_widgets = {
            **self.sample_widgets,
            **self.plot_widgets,
            **self.filter_widgets,
        }

        # Apply initial "Use default" state to everything
        self.on_use_default_toggle()


        # Transducers table headers
        trans_labels = ["Hardware", "Min V", "Max V", "Terminal Configuration", "Mx", "B", "Units"]
        label_length_trans = [35, 7, 7, 13, 6, 6, 7]  # corrected the typo in 'label_length'

        # How many transducer rows to show (adjust as needed)
        self.no_of_transducers = 10
        


        # Create labels for input channels
        for i, label in enumerate(trans_labels):
            ttk.Label(self.tab5, text=label, width=label_length_trans[i], wraplength=label_length_trans[i]*8).grid(row=0, column=i, padx=5, pady=5)
            
            

        # Create entries for each transducer row
        for i in range(1, self.no_of_transducers + 1):
            # Hardware
            self.entry_widgets[f"trans_hw{i}"] = ttk.Entry(self.tab5, width=label_length_trans[0])
            self.entry_widgets[f"trans_hw{i}"].grid(row=i, column=0, padx=5, pady=1, sticky='w')

            # Min V
            self.entry_widgets[f"trans_minv{i}"] = ttk.Entry(self.tab5, width=label_length_trans[1])
            self.entry_widgets[f"trans_minv{i}"].grid(row=i, column=1, padx=5, pady=1, sticky='w')

            # Max V
            self.entry_widgets[f"trans_maxv{i}"] = ttk.Entry(self.tab5, width=label_length_trans[2])
            self.entry_widgets[f"trans_maxv{i}"].grid(row=i, column=2, padx=5, pady=1, sticky='w')

            # Terminal Configuration
            self.entry_widgets[f"trans_term{i}"] = ttk.Combobox(self.tab5, values=self.Terminal_options_list, width=label_length_trans[3])
            self.entry_widgets[f"trans_term{i}"].set(self.Default_terminal)
            self.entry_widgets[f"trans_term{i}"].grid(row=i, column=3, padx=5, pady=1, sticky='w')

            # Mx
            self.entry_widgets[f"trans_mx{i}"] = ttk.Entry(self.tab5, width=label_length_trans[4])
            self.entry_widgets[f"trans_mx{i}"].grid(row=i, column=4, padx=5, pady=1, sticky='w')

            # B
            self.entry_widgets[f"trans_b{i}"] = ttk.Entry(self.tab5, width=label_length_trans[5])
            self.entry_widgets[f"trans_b{i}"].grid(row=i, column=5, padx=5, pady=1, sticky='w')

            # Units
            self.entry_widgets[f"trans_units{i}"] = ttk.Entry(self.tab5, width=label_length_trans[6])
            self.entry_widgets[f"trans_units{i}"].grid(row=i, column=6, padx=5, pady=2, sticky='w')


        self.read_defaults_from_excel('default_settings.xlsx')
        self.refresh_transducer_dropdowns()
        
        # At the end of update_channels(), after read_defaults_from_excel(...)
        for i in range(1, len(self.device.ai_physical_chans) + 1):
            cb = self.entry_widgets.get(f"transducer_sel{i}")
            if cb:
                chosen = cb.get()
                if chosen:  # not "" (blank)
                    self.apply_transducer_to_row(i, chosen)
        
        
    def get_available_channels(self):
        available_channels = []
        for i, name in enumerate(self.chan_names):
            if self.checkbutton_vars.get(f"Selected Channel{i+1}", tk.StringVar()).get() == "1":
                available_channels.append(name)     
        return available_channels
    
    def update_chan_sel(self, event):
        combobox = event.widget
        available_channels = self.get_available_channels()
        combobox['values'] = available_channels
            
    def clear_cust_dropdown(self, event):
        for i in range(0, self.no_of_cus_chans):     
            first_chan_sel = self.entry_widgets[f"first_chan_sel{i+1}"]
            sec_chan_sel = self.entry_widgets[f"sec_chan_sel{i+1}"]
            available_channels = self.get_available_channels()

            if first_chan_sel.get() not in available_channels:
                first_chan_sel.set('')

            if sec_chan_sel.get() not in available_channels:
                sec_chan_sel.set('')
        
    
    
    def read_defaults_from_excel(self, filename):
        try:
            df1 = pd.read_excel(filename, sheet_name='default_settings')
            df2 = pd.read_excel(filename, sheet_name='output_settings')
            df3 = pd.read_excel(filename, sheet_name='custom_channel_settings')
            df4 = pd.read_excel(filename, sheet_name='expert_settings')
            df5 = pd.read_excel(filename, sheet_name='transducers_settings')
            
            # Set values for rows from 'default_settings' sheet
            for i in range(len(df1)):
                row = df1.iloc[i]
                self.set_entry_values(i + 1, row, "input")

            # Set values for rows from 'output_settings' sheet
            for i in range(len(df2)):
                row = df2.iloc[i]
                self.set_entry_values(i + 1, row, "output")
                
            # Set values for rows from 'custom_channel_settings' sheet
            for i in range(len(df3)):
                row = df3.iloc[i]
                self.set_entry_values(i + 1, row, "custom")
            
            # Set values for rows from 'transducers' sheet    
            max_rows = min(len(df5), self.no_of_transducers)
            for i in range(max_rows):
                row = df5.iloc[i]
                hw = row.get("Hardware", "")
                units = row.get("Units", "")
                
                self.entry_widgets[f"trans_hw{i+1}"].insert(0, "" if pd.isna(hw) else str(hw))
                
                self.entry_widgets[f"trans_minv{i+1}"].insert(0, "" if pd.isna(row.get("Min Voltage")) else row.get("Min Voltage"))
                self.entry_widgets[f"trans_maxv{i+1}"].insert(0, "" if pd.isna(row.get("Max Voltage")) else row.get("Max Voltage"))
                if not pd.isna(row.get("Terminal Configuration", "")):
                    self.entry_widgets[f"trans_term{i+1}"].set(row.get("Terminal Configuration"))
                self.entry_widgets[f"trans_mx{i+1}"].insert(0, "" if pd.isna(row.get("Mx")) else row.get("Mx"))
                self.entry_widgets[f"trans_b{i+1}"].insert(0, "" if pd.isna(row.get("B")) else row.get("B"))
                
                self.entry_widgets[f"trans_units{i+1}"].insert(0, "" if pd.isna(units) else str(units))

             # ----- Load Expert values (Sample, Plot, Filters) -----
            def _load_val(label, widget, is_combo=False):
                try:
                    series = df4.loc[df4["Setting"] == label, "Value"]
                    if not series.empty:
                        val = "" if pd.isna(series.iloc[0]) else str(series.iloc[0])
                        # temporarily enable to set
                        try:
                            widget.config(state='normal')
                        except Exception:
                            widget.configure(state='normal')
                        if is_combo:
                            widget.set(val)
                        else:
                            widget.delete(0, tk.END)
                            widget.insert(0, val)
                except Exception:
                    pass

            # Sample
            _load_val("Sample Rate (Hz)", self.sample_rate_entry)
            _load_val("Decimation %", self.decimation_sp)

            # Plot
            _load_val("Max Buffer Size", self.max_buffer_entry)
            _load_val("Refresh Rate (Hz)", self.refresh_entry)
            _load_val("Max Plot Points", self.max_points_entry)

            # Filters
            _load_val("Filter Type", self.filter_type_cb, is_combo=True)
            _load_val("Filter Order", self.filter_order_sp)
            _load_val("Filter Cutoff (Hz)", self.filter_fc_entry)
            _load_val("Filter fh (Hz)", self.filter_fh_entry)

            # Apply state rules for filters
            try:
                self.filter_type_cb.event_generate("<<ComboboxSelected>>")
            except Exception:
                pass

            # If 'Use Default' column exists, apply it (first row as source)
            if 'Use Default' in df4.columns and len(df4) > 0:
                try:
                    use_default_flag = bool(int(df4.iloc[0]['Use Default']))
                except Exception:
                    use_default_flag = False
                self.use_default_var.set(use_default_flag)
                self.on_use_default_toggle()
 
        except FileNotFoundError:
            print(f"{filename} not found. Using default settings.")
        except Exception as e:
            print(f"Error reading {filename}: {e}")  
            
    def set_entry_values(self, index, row, ctype):
        if ctype == "input":
            self.entry_widgets[f"min_voltage_entry{index}"].insert(0, row['Min Voltage'] if not pd.isna(row['Min Voltage']) else '')
            self.entry_widgets[f"max_voltage_entry{index}"].insert(0, row['Max Voltage'] if not pd.isna(row['Max Voltage']) else '')
            self.entry_widgets[f"ter_selection_var{index}"].set(row['Terminal Configuration'] if not pd.isna(row['Terminal Configuration']) else '')
            self.entry_widgets[f"Device_name{index}"].insert(0, row['Device Name'] if not pd.isna(row['Device Name']) else '')
            self.entry_widgets[f"mx_plus_entry{index}"].insert(0, row['Mx'] if not pd.isna(row['Mx']) else '')
            self.entry_widgets[f"b_entry{index}"].insert(0, row['b'] if not pd.isna(row['b']) else '')
            self.entry_widgets[f"units_entry{index}"].insert(0, row['Units'] if not pd.isna(row['Units']) else '')
            self.checkbutton_vars[f"Selected Channel{index}"].set(int(row['Selected Channel']))
            
            if 'Transducer' in row.index and not pd.isna(row['Transducer']):
                self.entry_widgets[f"transducer_sel{index}"].set(row['Transducer'])
            
        elif ctype == "output":
            self.entry_widgets[f"min_voltage_entry_output{index}"].insert(0, row['Min Voltage'])
            self.entry_widgets[f"max_voltage_entry_output{index}"].insert(0, row['Max Voltage'])
            self.entry_widgets[f"ter_selection_var_output{index}"].set(row['Terminal Configuration'])
            self.entry_widgets[f"Device_name_output{index}"].insert(0, row['Device Name'])
            self.checkbutton_vars[f"Selected Channel_output{index}"].set(int(row['Selected Channel']))
            
        elif ctype == "custom":
            self.entry_widgets[f"Channel_entry_cust{index}"].insert(0, row['Channel'] if not pd.isna(row['Channel']) else '')
            self.entry_widgets[f"first_chan_sel{index}"].set(row['First Channel'] if not pd.isna(row['First Channel']) else '')
            self.entry_widgets[f"operation_sel{index}"].set(row['Operation'] if not pd.isna(row['Operation']) else '')
            self.entry_widgets[f"sec_chan_sel{index}"].set(row['Second Channel'] if not pd.isna(row['Second Channel']) else '')
            self.entry_widgets[f"custom_chan_name{index}"].insert(0, row['Device Name'] if not pd.isna(row['Device Name']) else '')
            self.entry_widgets[f"cust_units_entry{index}"].insert(0, row['Units'] if not pd.isna(row['Units']) else '')
            self.checkbutton_vars[f"Selected Channel_Custom{index}"].set(int(row['Selected Channel']))   
            
            
    def on_use_default_toggle(self):
        """Apply defaults and lock fields when 'Use default' is checked; re-enable otherwise."""
        use_default = self.use_default_var.get()

        # Helper to (temporarily) make a widget editable for setting text
        def _temp_enable_set(widget, value, is_combo=False):
            try:
                widget.config(state='normal')
            except Exception:
                widget.configure(state='normal')
            if is_combo:
                try:
                    widget.set(value)
                except Exception:
                    pass
            else:
                try:
                    widget.delete(0, tk.END)
                    widget.insert(0, value)
                except Exception:
                    pass

        if use_default:
            # Fill ALL expert fields from defaults (if a default exists), then disable them
            for name, widget in getattr(self, "all_expert_widgets", {}).items():
                if name in self.expert_defaults:
                    _temp_enable_set(widget, self.expert_defaults[name],
                                     is_combo=isinstance(widget, ttk.Combobox))
                try:
                    widget.config(state='disabled')
                except Exception:
                    widget.configure(state='disabled')

            # Explicitly enforce defaults for Filters and gray out every control
            _temp_enable_set(self.filter_type_cb, self.expert_defaults.get("Filter Type", "bandstop"), is_combo=True)
            _temp_enable_set(self.filter_order_sp, self.expert_defaults.get("Filter Order", "2"))
            _temp_enable_set(self.filter_fc_entry, self.expert_defaults.get("Filter Cutoff (Hz)", "2"))
            _temp_enable_set(self.filter_fh_entry, self.expert_defaults.get("Filter fh (Hz)", "0"))

            # Now gray them out
            self.filter_type_cb.config(state='disabled')
            self.filter_order_sp.config(state='disabled')
            self.filter_fc_entry.config(state='disabled')
            self.filter_fh_entry.config(state='disabled')

            # Do NOT re-apply type logic—everything stays disabled under defaults
            return

        # When not using defaults: unlock all fields
        for name, widget in getattr(self, "all_expert_widgets", {}).items():
            try:
                if isinstance(widget, ttk.Combobox) and name == "Filter Type":
                    widget.config(state='readonly')   # combobox stays readonly
                else:
                    widget.config(state='normal')
            except Exception:
                widget.configure(state='normal')

        # Re-apply filter-type rules so Order/Cutoff/fh enable correctly per selection
        try:
            self.filter_type_cb.event_generate("<<ComboboxSelected>>")
        except Exception:
            pass


    
    #strip white spaces from begining and ending of text
    def strip_value(self, value):
        if isinstance(value, str):
            return value.strip()
        return value    
    
    def save_settings(self):
        fieldnames = ['Channel','DAQ','Transducer','Min Voltage','Max Voltage','Terminal Configuration','Mx','b','Units','Device Name','Selected Channel']
        fieldnames_out = ['Channel','DAQ','Min Voltage','Max Voltage','Terminal Configuration','Device Name','Selected Channel']
        fieldnames_cus =  ['Channel',"First Channel", "Operation", "Second Channel", "Device Name", "Units", 'Selected Channel']

        # Get a list of physical channels for the device
        physical_ai_channels = self.device.ai_physical_chans
        num_ai_channels = len(self.device.ai_physical_chans)
        physical_ao_channels = self.device.ao_physical_chans
        num_ao_channels = len(self.device.ao_physical_chans)
        
        data1 = []
        data2 = []
        data3 = []
        data4 = []
        data5 = []
        
        # Collect data for input channels
        for i in range(1, num_ai_channels + 1):
            channel = self.entry_widgets[f"Channel_entry{i}"].get()
            transducer = self.entry_widgets[f"transducer_sel{i}"].get()
            min_voltage = self.entry_widgets[f"min_voltage_entry{i}"].get()
            max_voltage = self.entry_widgets[f"max_voltage_entry{i}"].get()
            term_config = self.entry_widgets[f"ter_selection_var{i}"].get()
            mx_plus = self.entry_widgets[f"mx_plus_entry{i}"].get()
            b = self.entry_widgets[f"b_entry{i}"].get()
            name = self.strip_value(self.entry_widgets[f"Device_name{i}"].get())
            units = self.strip_value(self.entry_widgets[f"units_entry{i}"].get())
            selected = int(self.checkbutton_vars[f"Selected Channel{i}"].get())
            
            data1.append({
                'Channel': channel,
                'DAQ': self.chosen_daq.get(),
                'Transducer': transducer,  
                'Min Voltage': min_voltage,
                'Max Voltage': max_voltage, 
                'Terminal Configuration': term_config, 
                'Mx': mx_plus,
                'b': b, 
                'Units': units, 
                'Device Name': name,
                'Selected Channel': selected
                })

        # Collect data for output channels
        for i in range(1, num_ao_channels + 1):
            channel = self.entry_widgets[f"Channel_entry_output{i}"].get()
            min_voltage = self.entry_widgets[f"min_voltage_entry_output{i}"].get()
            max_voltage = self.entry_widgets[f"max_voltage_entry_output{i}"].get()
            term_config = self.entry_widgets[f"ter_selection_var_output{i}"].get()
            #name = self.entry_widgets[f"Device_name_output{i}"].get()
            name = self.strip_value(self.entry_widgets[f"Device_name_output{i}"].get())
            selected = int(self.checkbutton_vars[f"Selected Channel_output{i}"].get())
            
            data2.append({
                'Channel': channel, 
                'DAQ': self.chosen_daq.get(),
                'Min Voltage': min_voltage,
                'Max Voltage': max_voltage, 
                'Terminal Configuration': term_config, 
                'Device Name': name,
                'Selected Channel': selected
            })
            
        # Collect data for custom channels
        for i in range(1, self.no_of_cus_chans + 1):
            channel = self.entry_widgets[f"Channel_entry_cust{i}"].get()
            first_channel = self.entry_widgets[f"first_chan_sel{i}"].get()
            operation = self.entry_widgets[f"operation_sel{i}"].get()
            second_channel = self.entry_widgets[f"sec_chan_sel{i}"].get()
            #units = self.entry_widgets[f"cust_units_entry{i}"].get()
            #name = self.entry_widgets[f"custom_chan_name{i}"].get()
            name = self.strip_value(self.entry_widgets[f"custom_chan_name{i}"].get())
            units = self.strip_value(self.entry_widgets[f"cust_units_entry{i}"].get())

            selected = int(self.checkbutton_vars[f"Selected Channel_Custom{i}"].get())
            
            data3.append({
                'Channel': channel,
                'First Channel': first_channel, 
                'Operation': operation,
                'Second Channel': second_channel,
                'Device Name': name, 
                'Units': units,
                'Selected Channel': selected
            })
            
            # Check if any of the required fields are "n/a"
            if selected:
                if first_channel == '' or second_channel == '' or operation == '':
                    messagebox.showwarning("Missing Values", f"Warning: Selected custom channel {i} is missing values")

                    

            
                
        # Collect data for transducers
        trans_fieldnames = ["Hardware", "Min Voltage", "Max Voltage", "Terminal Configuration", "Mx", "B", "Units"]
        
        for i in range(1, self.no_of_transducers + 1):
            hw    = self.strip_value(self.entry_widgets[f"trans_hw{i}"].get())
            minv  = self.entry_widgets[f"trans_minv{i}"].get()
            maxv  = self.entry_widgets[f"trans_maxv{i}"].get()
            term  = self.entry_widgets[f"trans_term{i}"].get()
            mx    = self.entry_widgets[f"trans_mx{i}"].get()
            b     = self.entry_widgets[f"trans_b{i}"].get()
            units = self.strip_value(self.entry_widgets[f"trans_units{i}"].get())

            data5.append({
                "Hardware": hw,
                "Min Voltage": minv,
                "Max Voltage": maxv,
                "Terminal Configuration": term,
                "Mx": mx,
                "B": b,
                "Units": units,
            })
            
            
                  # Collect data for expert settings
        
        # Collect data for expert settings (Sample, Plot, Filters)
        def _add_setting(k, v):
            data4.append({"Setting": k, "Value": v, "Use Default": int(self.use_default_var.get())})

        # Sample
        _add_setting("Sample Rate (Hz)", self.sample_rate_entry.get())
        _add_setting("Decimation %", self.decimation_sp.get())

        # Plot
        _add_setting("Max Buffer Size", self.max_buffer_entry.get())
        _add_setting("Refresh Rate (Hz)", self.refresh_entry.get())
        _add_setting("Max Plot Points", self.max_points_entry.get())

        # Filters
        _add_setting("Filter Type", self.filter_type_cb.get())
        _add_setting("Filter Order", self.filter_order_sp.get())
        _add_setting("Filter Cutoff (Hz)", self.filter_fc_entry.get())
        _add_setting("Filter fh (Hz)", self.filter_fh_entry.get())

            
    
        # Create DataFrames
        df1 = pd.DataFrame(data1, columns=fieldnames)
        df2 = pd.DataFrame(data2, columns=fieldnames_out)
        df3 = pd.DataFrame(data3, columns=fieldnames_cus)
        df4 = pd.DataFrame(data4, columns=["Setting", "Value", "Use Default"])
        df5 = pd.DataFrame(data5, columns=trans_fieldnames)
        
        # Write to Excel file with multiple sheets
        with pd.ExcelWriter('default_settings.xlsx') as writer:
            df1.to_excel(writer, sheet_name='default_settings', index=False)
            df2.to_excel(writer, sheet_name='output_settings', index=False)
            df3.to_excel(writer, sheet_name='custom_channel_settings', index=False)
            df4.to_excel(writer, sheet_name='expert_settings', index=False)
            df5.to_excel(writer, sheet_name='transducers_settings', index=False)

    def close_window(self):    
        self.destroy()
        self.parent.destroy()

# Main Application
class App(tk.Tk):
    def __init__(self):
        super().__init__()

        self.title("DAQ Settings")
        self.geometry("900x500")

        self.channel_settings = ChannelSettings(self, "Channel Settings")
        self.channel_settings.pack(padx=10, pady=10, fill="both", expand=True)
        
        self.protocol("WM_DELETE_WINDOW", self.on_closing)
        
    def on_closing(self):
        self.destroy()

if __name__ == "__main__":
    set_app = App()
    set_app.mainloop()
