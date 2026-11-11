
import sys
import re
import numpy as np

# QApplication must exist before pyqtgraph is imported when running standalone
from PyQt5.QtWidgets import QApplication
if __name__ == "__main__":
    _standalone_app = QApplication.instance() or QApplication(sys.argv)

import pyqtgraph as pg

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QGridLayout,
                             QLabel, QLineEdit, QComboBox, QPushButton,
                             QWidget, QMessageBox)


_SINGLE_OPS = ["=", "<", "<=", ">", ">="]
_DOUBLE_OPS = ["<", "<=", ">", ">="]
_MAX_LINES  = 10


def _flip_op(op):
    """Opposite-direction operator: < <-> >, <= <-> >= (others unchanged)."""
    return {"<": ">", "<=": ">=", ">": "<", ">=": "<="}.get(op, op)


def _sl_settings_path():
    """Resolve the working default_settings.xlsx path (same logic as the apps)."""
    import os
    if getattr(sys, 'frozen', False):
        base = os.path.dirname(sys.executable)
    elif '__file__' in globals():
        base = os.path.dirname(os.path.abspath(__file__))
    else:
        base = os.getcwd()
    return os.path.join(base, 'default_settings.xlsx')


def _load_plot_line_presets():
    """Return the ENABLED plot-line presets saved in App Settings as a list of
    dicts {Label, Axis, Value, Unit, Color, Style, Limit, Op, Op2, Value2}.
    Empty if none."""
    presets = []
    try:
        import os
        import pandas as pd
        path = _sl_settings_path()
        if not os.path.isfile(path):
            return presets
        xls = pd.ExcelFile(path)
        if "app_plot_lines" not in xls.sheet_names:
            return presets
        df = pd.read_excel(xls, sheet_name="app_plot_lines").fillna("")
        for _, r in df.iterrows():
            try:
                enabled = bool(int(r.get("Enabled", 0)))
            except (TypeError, ValueError):
                enabled = False
            if not enabled:
                continue
            presets.append({k: str(r.get(k, "")).strip()
                            for k in ["Label", "Axis", "Value", "Unit", "Color",
                                      "Style", "Limit", "Op", "Op2", "Value2"]})
    except Exception as e:
        print(f"Static lines: could not load saved plot lines: {e}")
    return presets


def _settings_channel_units():
    """[(kind, device_name, unit)] for every channel in Settings.

    Read through DAQ_Settings, which owns the workbook layout (the input sheet
    calls its unit 'Scaled Units', the custom sheet 'Units', and an output's
    unit follows from its Output Type). Reading the sheets here directly once
    looked for 'Units' on the input sheet and so missed every input unit.
    """
    try:
        from DAQ_Settings import read_channel_units
        return read_channel_units(_sl_settings_path())
    except Exception as e:
        print(f"Static lines: could not load channel units: {e}")
        return []


def _load_channel_units():
    """All distinct channel units defined in Settings (input + custom
    channels), regardless of whether each channel is selected for plotting."""
    units = []
    for kind, _name, unit in _settings_channel_units():
        if kind in ("ai", "custom") and unit not in units:
            units.append(unit)
    return units


def _load_x_axis_units():
    """Units selectable for an X-axis line: sample count, the time/duration
    unit, and the unit of every analog-output channel defined in Settings.

    An AO channel's unit comes from its 'Output Type' — volts for a voltage
    output, amps for a current one — so a current module offers 'A' rather than
    the 'V' this used to assume for every output.
    """
    units = ["samples", "s"]
    for kind, _name, unit in _settings_channel_units():
        if kind == "ao" and unit not in units:
            units.append(unit)
    return units


class StaticLineDialog(QDialog):
    """Lets the user add static reference lines to the live plot.

    Args:
        graph_widget : the GraphData instance (needs .plot_widget and .unit_axes)
        parent       : optional Qt parent widget
    """

    def __init__(self, graph_widget, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Static Line Adder")
        self.setMinimumWidth(420)
        self.graph_widget = graph_widget
        self.added_lines = []   # derived: (display, item, eq_str, dest_vb, cleanup_fn, unit)
        self._line_defs  = []   # source of truth; each defn carries a 'group' id
        self._group_seq  = 0    # increasing id; lines added together share a group
        self._setup_ui()
        # Saved lines belong to the plot, not to this window, so they are
        # imported as soon as the dialog exists rather than waiting for the
        # user to press 'Load Saved Lines'. That button stays for reloading
        # them after a manual removal.
        r = self.load_saved_lines()
        if r['added']:
            print(f"Static lines: loaded {r['added']} saved line(s).")

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        form = QGridLayout()

        # --- Axis selector + limit type ---
        form.addWidget(QLabel("Axis:"), 0, 0)
        self.axis_combo = QComboBox()
        self.axis_combo.addItems(["Y", "X"])
        self.axis_combo.setFixedWidth(60)
        form.addWidget(self.axis_combo, 0, 1)
        form.addWidget(QLabel("Limit:"), 0, 2)
        self.limit_combo = QComboBox()
        self.limit_combo.addItems(["Single", "Double"])
        self.limit_combo.setFixedWidth(90)
        form.addWidget(self.limit_combo, 0, 3)

        # --- Y fields (operator + equation, optional 2nd bound) ---
        self.y_eq_label = QLabel("Y")
        form.addWidget(self.y_eq_label, 1, 0)
        self.y_op = QComboBox()
        self.y_op.addItems(_SINGLE_OPS)
        self.y_op.setFixedWidth(60)
        form.addWidget(self.y_op, 1, 1)
        self.eq_entry = QLineEdit()
        self.eq_entry.setPlaceholderText("e.g.  2   3x+1   -0.5x-2")
        form.addWidget(self.eq_entry, 1, 2, 1, 2)

        self.y_eq_label2 = QLabel("Y")
        form.addWidget(self.y_eq_label2, 2, 0)
        self.y_op2 = QComboBox()
        self.y_op2.addItems(_DOUBLE_OPS)
        self.y_op2.setFixedWidth(60)
        form.addWidget(self.y_op2, 2, 1)
        self.eq_entry2 = QLineEdit()
        self.eq_entry2.setPlaceholderText("second Y limit")
        form.addWidget(self.eq_entry2, 2, 2, 1, 2)

        self.y_units_label = QLabel("Y-Axis Units:")
        form.addWidget(self.y_units_label, 3, 0)
        self.yaxis_combo = QComboBox()
        self.yaxis_combo.setMinimumWidth(120)
        form.addWidget(self.yaxis_combo, 3, 1, 1, 3)

        # --- X fields (operator + number, optional 2nd bound) ---
        self.x_val_label = QLabel("X")
        form.addWidget(self.x_val_label, 4, 0)
        self.x_op = QComboBox()
        self.x_op.addItems(_SINGLE_OPS)
        self.x_op.setFixedWidth(60)
        form.addWidget(self.x_op, 4, 1)
        self.x_entry = QLineEdit()
        self.x_entry.setPlaceholderText("e.g.  5   -3.2")
        form.addWidget(self.x_entry, 4, 2, 1, 2)

        self.x_val_label2 = QLabel("X")
        form.addWidget(self.x_val_label2, 5, 0)
        self.x_op2 = QComboBox()
        self.x_op2.addItems(_DOUBLE_OPS)
        self.x_op2.setFixedWidth(60)
        form.addWidget(self.x_op2, 5, 1)
        self.x_entry2 = QLineEdit()
        self.x_entry2.setPlaceholderText("second X limit")
        form.addWidget(self.x_entry2, 5, 2, 1, 2)

        self.x_units_label = QLabel("X-Axis Units:")
        form.addWidget(self.x_units_label, 6, 0)
        self.xaxis_combo = QComboBox()
        self.xaxis_combo.setMinimumWidth(120)
        form.addWidget(self.xaxis_combo, 6, 1, 1, 3)

        # --- Shared fields ---
        form.addWidget(QLabel("Label:"), 7, 0)
        self.label_entry = QLineEdit()
        self.label_entry.setPlaceholderText("Optional display name")
        form.addWidget(self.label_entry, 7, 1, 1, 3)

        form.addWidget(QLabel("Color:"), 8, 0)
        self.color_combo = QComboBox()
        self.color_combo.addItems(["red", "blue", "green", "black",
                                   "orange", "purple", "magenta", "brown", "gray"])
        form.addWidget(self.color_combo, 8, 1)

        form.addWidget(QLabel("Style:"), 8, 2)
        self.style_combo = QComboBox()
        self.style_combo.addItems(["Solid", "Dashed", "Dotted"])
        form.addWidget(self.style_combo, 8, 3)

        layout.addLayout(form)

        btn_row = QHBoxLayout()
        add_btn = QPushButton("Add Line")
        add_btn.clicked.connect(self._add_line)
        btn_row.addWidget(add_btn)
        load_btn = QPushButton("＋ Load Saved Lines")
        load_btn.setToolTip("Add the enabled plot lines defined in "
                            "Settings → App Settings → Plot Lines")
        load_btn.clicked.connect(self.add_preset_lines)
        btn_row.addWidget(load_btn)
        layout.addLayout(btn_row)

        layout.addWidget(QLabel("Added lines:"))
        self.lines_layout = QVBoxLayout()
        layout.addLayout(self.lines_layout)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)
        layout.addWidget(close_btn)

        self.axis_combo.currentTextChanged.connect(self._on_axis_changed)
        self.limit_combo.currentTextChanged.connect(self._on_limit_changed)
        self.y_op.currentTextChanged.connect(lambda _t: self._sync_dialog_ops("Y", "op"))
        self.y_op2.currentTextChanged.connect(lambda _t: self._sync_dialog_ops("Y", "op2"))
        self.x_op.currentTextChanged.connect(lambda _t: self._sync_dialog_ops("X", "op"))
        self.x_op2.currentTextChanged.connect(lambda _t: self._sync_dialog_ops("X", "op2"))
        self._apply_dialog_limit_mode()
        self._on_axis_changed(self.axis_combo.currentText())
        self._refresh_yaxis_combo()

    def _on_axis_changed(self, _axis=None):
        self._update_field_visibility()

    def _on_limit_changed(self, _t=None):
        self._apply_dialog_limit_mode()
        self._update_field_visibility()

    def _update_field_visibility(self):
        is_y = (self.axis_combo.currentText() == "Y")
        is_double = (self.limit_combo.currentText() == "Double")
        for w in (self.y_eq_label, self.y_op, self.eq_entry):
            w.setVisible(is_y)
        for w in (self.y_eq_label2, self.y_op2, self.eq_entry2):
            w.setVisible(is_y and is_double)
        for w in (self.y_units_label, self.yaxis_combo):
            w.setVisible(is_y)
        for w in (self.x_val_label, self.x_op, self.x_entry,
                  self.x_units_label, self.xaxis_combo):
            w.setVisible(not is_y)
        for w in (self.x_val_label2, self.x_op2, self.x_entry2):
            w.setVisible((not is_y) and is_double)

    def _apply_dialog_limit_mode(self):
        """Set the allowed operators + 2nd-bound visibility for the current
        Single/Double mode. Single uses one operator; Double keeps each axis's two
        operators pointing in opposite directions."""
        is_double = (self.limit_combo.currentText() == "Double")
        for op, op2 in ((self.y_op, self.y_op2), (self.x_op, self.x_op2)):
            cur = op.currentText()
            op.blockSignals(True)
            op.clear()
            op.addItems(_DOUBLE_OPS if is_double else _SINGLE_OPS)
            if is_double:
                op.setCurrentText(cur if cur in _DOUBLE_OPS else "<=")
            else:
                op.setCurrentText(cur if cur in _SINGLE_OPS else "=")
            op.blockSignals(False)
            if is_double:
                op2.blockSignals(True)
                op2.setCurrentText(_flip_op(op.currentText()))
                op2.blockSignals(False)

    def _sync_dialog_ops(self, axis, which):
        """In Double mode, keep an axis's two operators complementary:
        changing one flips the other (< <-> >, <= <-> >=)."""
        if self.limit_combo.currentText() != "Double":
            return
        op  = self.y_op  if axis == "Y" else self.x_op
        op2 = self.y_op2 if axis == "Y" else self.x_op2
        src, dst = (op, op2) if which == "op" else (op2, op)
        want = _flip_op(src.currentText())
        if dst.currentText() != want:
            dst.blockSignals(True)
            dst.setCurrentText(want)
            dst.blockSignals(False)

    def _next_group(self):
        self._group_seq += 1
        return self._group_seq

    def _refresh_yaxis_combo(self):
        """Y-axis units come from the (input/custom) channel units; X-axis units
        are samples / duration / AO-channel units. All read from Settings."""
        def _fill(combo, unit_list):
            cur = combo.currentText()
            combo.clear()
            if unit_list:
                combo.addItems(unit_list)
                if cur in unit_list:
                    combo.setCurrentText(cur)
            else:
                combo.addItem("(no units in settings)")
        _fill(self.yaxis_combo, _load_channel_units())
        _fill(self.xaxis_combo, _load_x_axis_units())

    def _current_x_unit(self):
        """Unit currently on the X axis, used only to decide whether an X line
        *draws* (never whether it is imported). Read from the live X-axis
        selector plus the settings file: 'Samples' -> 'samples', 'Run Duration'
        -> 's', an input/custom channel -> its unit, an output (AO) channel ->
        the unit of its Output Type. None if it can't be resolved (then X lines
        draw freely)."""
        try:
            sel = (self.graph_widget.parent
                   .plotSettingsWidget.x_axis_selection.currentText())
        except Exception:
            return None
        if not sel:
            return None
        if sel == "Samples":
            return "samples"
        if sel == "Run Duration":
            return "s"
        # Input and custom channels first, then outputs — the order the main
        # window resolves a Device Name in.
        rows = _settings_channel_units()
        for kinds in (("ai", "custom"), ("ao",)):
            for kind, name, unit in rows:
                if kind in kinds and name == sel.strip():
                    return unit
        return None

    # ------------------------------------------------------------------
    # Equation parsing
    # ------------------------------------------------------------------

    def _parse_y_equation(self, rhs_str):
        """Parse the right-hand side of Y = ...
        Returns ('H', y_val) for horizontal or ('L', slope, intercept) for sloped.
        """
        rhs = rhs_str.replace(' ', '').upper()

        # Try plain constant: 2, -3.5
        try:
            return ('H', float(rhs)), None
        except ValueError:
            pass

        # Try sloped: [±][coeff]X[±intercept]
        m = re.fullmatch(
            r'([+-]?(?:\d+\.?\d*|\.\d+)?)X([+-](?:\d+\.?\d*|\.\d+))?',
            rhs
        )
        if m:
            s, b = m.group(1), m.group(2)
            if s in ('', '+'):
                slope = 1.0
            elif s == '-':
                slope = -1.0
            else:
                try:
                    slope = float(s)
                except ValueError:
                    return None, f"Cannot parse slope: '{s}'"
            intercept = float(b) if b else 0.0
            return ('L', slope, intercept), None

        return None, (
            f"Cannot parse: '{rhs_str}'\n"
            "Valid forms:  2   -3.5   3x+1   -0.5x-2   x   -x+4"
        )

    # ------------------------------------------------------------------
    # Add / Remove lines
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Build / reapply helpers
    # ------------------------------------------------------------------

    def _build_item(self, defn):
        """Draw the live pyqtgraph item for a defn and attach its live handles
        (defn['_item'], defn['_dest_vb'], defn['_cleanup']). Returns the defn on
        success, or None if it couldn't be drawn (named unit not on the plot)."""
        color      = defn['color']
        style_map  = {"Solid": Qt.SolidLine, "Dashed": Qt.DashLine, "Dotted": Qt.DotLine}
        pen        = pg.mkPen(color=color, width=2, style=style_map[defn['style']])
        pw         = self.graph_widget.plot_widget
        axis       = defn['axis']
        display    = defn['display']
        selected_unit = defn['unit']
        cleanup_fn = None

        if axis == "X":
            # X lines live on the shared bottom axis. Draw only if the line's
            # unit matches the current X-axis unit; a blank "—" always draws
            # and an unknown X unit draws rather than hides. This affects only
            # *drawing* — a non-matching line is still imported/stored and will
            # draw once the X axis is set to its unit.
            x_unit = self._current_x_unit()
            if x_unit and selected_unit != "—" and selected_unit != x_unit:
                return None
            item = pg.InfiniteLine(
                pos=defn['x_val'], angle=90, pen=pen,
                label=display, labelOpts={'position': 0.9, 'color': color}
            )
            pw.addItem(item)
            dest_vb = None

        else:
            unit_axes = self.graph_widget.unit_axes
            # Strict: if axes exist but this specific named unit isn't among
            # them, skip rather than drawing on the default axis at a meaningless
            # scale. (Pre-Run, unit_axes is empty and we fall through to the
            # default-viewbox fallback; a blank unit "—" also uses the fallback.)
            if unit_axes and selected_unit != "—" and selected_unit not in unit_axes:
                return None
            unit_pack = unit_axes.get(selected_unit, {})
            dest_vb   = unit_pack.get('vb', None)

            if defn['line_type'] == 'H':
                item = pg.InfiniteLine(
                    pos=defn['y_val'], angle=0, pen=pen,
                    label=display, labelOpts={'position': 0.9, 'color': color}
                )
                if dest_vb is None:
                    pw.addItem(item)
                else:
                    dest_vb.addItem(item)

            else:   # sloped
                slope, intercept = defn['slope'], defn['intercept']
                x_range = pw.getViewBox().viewRange()[0]
                margin  = max(abs(x_range[1] - x_range[0]) * 0.1, 10)
                x_vals  = np.array([x_range[0] - margin, x_range[1] + margin])
                y_vals  = slope * x_vals + intercept
                item    = pg.PlotCurveItem(x_vals, y_vals, pen=pen)
                if dest_vb is None:
                    pw.addItem(item, ignoreBounds=True)
                else:
                    dest_vb.addItem(item, ignoreBounds=True)
                    dest_vb.enableAutoRange(axis=pg.ViewBox.YAxis, enable=True)

                def _update_sloped(vb, x_range,
                                   _item=item, _slope=slope, _intercept=intercept):
                    x = np.array([x_range[0], x_range[1]], dtype=np.float64)
                    _item.setData(x, _slope * x + _intercept)

                pw.getViewBox().sigXRangeChanged.connect(_update_sloped)
                cleanup_fn = lambda: pw.getViewBox().sigXRangeChanged.disconnect(_update_sloped)

        defn['_item']    = item
        defn['_dest_vb'] = dest_vb
        defn['_cleanup'] = cleanup_fn
        return defn

    def _clear_live_items(self):
        """Remove all live pyqtgraph items, leaving _line_defs (and their
        params) intact. Live handles are detached from each defn."""
        for d in self._line_defs:
            cl = d.get("_cleanup")
            if cl is not None:
                try:
                    cl()
                except Exception:
                    pass
            item = d.get("_item")
            if item is not None:
                dest_vb = d.get("_dest_vb")
                try:
                    if dest_vb is None:
                        self.graph_widget.plot_widget.removeItem(item)
                    else:
                        dest_vb.removeItem(item)
                except Exception:
                    pass
            d.pop("_item", None)
            d.pop("_dest_vb", None)
            d.pop("_cleanup", None)
        self.added_lines = []

    def reapply_lines(self):
        """Re-add every stored line to the (possibly reset) plot.

        Call this from the main app after a re-run / plot clear, e.g.::

            self.appsWidget.app_dialog('static_lines').reapply_lines()
        """
        self._clear_live_items()
        for d in list(self._line_defs):
            self._build_item(d)   # re-attaches handles; leaves undrawable ones
        self._sync_added_lines()
        self._refresh_list()

    # ------------------------------------------------------------------
    # Add line (from UI)
    # ------------------------------------------------------------------

    def _preset_to_defns(self, p):
        """Convert a saved plot-line preset into a LIST of line-definition dicts
        (same shape _add_line()/_build_item() use). A Single limit yields one
        defn; a Double limit yields two (high and low) with '+'/'-' appended to
        the shared label. Returns [] if nothing parseable."""
        axis  = (p.get("Axis")  or "Y").strip().upper()
        color = (p.get("Color") or "red").strip() or "red"
        style = (p.get("Style") or "Solid").strip() or "Solid"
        label = (p.get("Label") or "").strip()
        limit = (p.get("Limit") or "Single").strip().title() or "Single"
        unit  = (p.get("Unit")  or "").strip() or "—"

        if limit == "Double":
            bounds = []
            for val, op in ((p.get("Value"),  p.get("Op")),
                            (p.get("Value2"), p.get("Op2"))):
                d = self._single_defn(axis, val, op, color, style, label, unit)
                if d is not None:
                    bounds.append(d)
            if bounds:
                grp = self._next_group()
                for d in bounds:
                    d["group"] = grp
            return bounds

        d = self._single_defn(axis, p.get("Value"), p.get("Op"),
                              color, style, label, unit)
        if d is None:
            return []
        d["group"] = self._next_group()
        return [d]

    def _single_defn(self, axis, value, op, color, style, label, unit):
        """Build ONE line-definition dict from a value string, or None if it
        can't be parsed. 'op' is normalised to one of =, <, <=, >, >= (blank
        -> '='); the operator symbol is appended to the on-plot label
        (nothing is appended for '='). 'label_base' keeps the raw label for the
        list view."""
        value = (value or "").strip()
        op    = (op or "").strip()
        if op == "":
            op = "="
        if not value:
            return None

        def _disp(eq_str):
            if not label:
                return eq_str
            return label if op == "=" else f"{label} {op}"

        if axis == "X":
            try:
                x_val = float(value)
            except ValueError:
                return None
            eq_str = f"X={x_val}"
            return dict(axis="X", color=color, style=style,
                        display=_disp(eq_str), label_base=label, eq_str=eq_str,
                        unit=(unit or "—"), x_val=x_val, op=op, sort_val=x_val)

        # Y line — reuse the same equation parser as manual entry
        result, _error = self._parse_y_equation(value)
        if result is None:
            return None
        if result[0] == "H":
            y_val = result[1]
            eq_str = f"Y={y_val}"
            return dict(axis="Y", color=color, style=style, display=_disp(eq_str),
                        label_base=label, eq_str=eq_str, unit=unit, line_type="H",
                        y_val=y_val, op=op, sort_val=y_val)
        slope, intercept = result[1], result[2]
        sign = "+" if intercept >= 0 else "-"
        eq_str = f"Y={slope}x{sign}{abs(intercept)}"
        return dict(axis="Y", color=color, style=style, display=_disp(eq_str),
                    label_base=label, eq_str=eq_str, unit=unit, line_type="L",
                    slope=slope, intercept=intercept, op=op, sort_val=intercept)

    def load_saved_lines(self):
        """Import the enabled plot lines saved in App Settings.

        Silent by design: it does the work and returns a summary, leaving the
        caller to decide whether (and how) to report it. Presets already on the
        plot are skipped, matched by equation, so calling it repeatedly never
        duplicates a line.

        Returns a dict: {'presets': int   — how many enabled presets were found,
                         'added':   int   — lines imported by this call,
                         'pending': int   — imported but not drawable yet,
                         'capped':  bool  — stopped at the row limit,
                         'skipped': list  — labels that could not be parsed}.
        """
        presets = _load_plot_line_presets()
        result = {'presets': len(presets), 'added': 0, 'pending': 0,
                  'capped': False, 'skipped': []}
        if not presets:
            return result

        existing = {d.get("eq_str") for d in self._line_defs}
        for p in presets:
            if len({d.get("group") for d in self._line_defs}) >= _MAX_LINES:
                result['capped'] = True
                break
            defns = self._preset_to_defns(p)
            if not defns:
                result['skipped'].append(p.get("Label") or p.get("Value") or "?")
                continue
            for defn in defns:
                if defn["eq_str"] in existing:
                    continue   # already imported
                # Import every parseable line. _build_item draws it now if its
                # unit is on the plot; otherwise it stays stored and draws on a
                # later run once that unit/axis is present.
                if self._build_item(defn) is None:
                    result['pending'] += 1
                self._line_defs.append(defn)
                existing.add(defn["eq_str"])
                result['added'] += 1
        self._sync_added_lines()
        self._refresh_list()
        return result

    def add_preset_lines(self):
        """'Load Saved Lines' button: import the saved lines, then report what
        happened. Useful for pulling the lines back after removing them by
        hand, or after editing them in Settings."""
        r = self.load_saved_lines()
        if not r['presets']:
            QMessageBox.information(
                self, "No Saved Lines",
                "No enabled plot lines are saved.\n\n"
                "Define them in Settings → App Settings → Plot Lines "
                "(tick 'On' for each line you want).")
            return

        msg = f"Added {r['added']} saved line(s)."
        if r['pending']:
            msg += (f"\n{r['pending']} not shown yet — they'll appear once their "
                    "unit/axis is on the plot (e.g. after Run).")
        if r['capped']:
            msg += f"\nStopped at the {_MAX_LINES}-row limit."
        if r['skipped']:
            msg += "\nSkipped (couldn't parse): " + ", ".join(r['skipped']) + "."
        QMessageBox.information(self, "Saved Lines", msg)

    def _add_line(self):
        color = self.color_combo.currentText()
        style = self.style_combo.currentText()
        axis  = self.axis_combo.currentText()
        label = self.label_entry.text().strip()
        is_double = (self.limit_combo.currentText() == "Double")

        if axis == "X":
            unit = self.xaxis_combo.currentText()
            if not unit or unit.startswith("("):
                unit = "—"
            v1_w, op1_w = self.x_entry,  self.x_op
            v2_w, op2_w = self.x_entry2, self.x_op2
        else:
            unit = self.yaxis_combo.currentText()
            v1_w, op1_w = self.eq_entry,  self.y_op
            v2_w, op2_w = self.eq_entry2, self.y_op2

        specs = [(v1_w.text(), op1_w.currentText())]
        if is_double:
            specs.append((v2_w.text(), op2_w.currentText()))

        defns = []
        for val, op in specs:
            val = (val or "").strip()
            if not val:
                QMessageBox.warning(
                    self, "Missing Value",
                    "Please enter both limit values."
                    if is_double else "Please enter a value.")
                return
            d = self._single_defn(axis, val, op, color, style, label, unit)
            if d is None:
                QMessageBox.warning(self, "Parse Error",
                                    f"Couldn't parse '{val}'.")
                return
            defns.append(d)

        if len({d.get("group") for d in self._line_defs}) >= _MAX_LINES:
            QMessageBox.warning(
                self, "Line Limit Reached",
                f"You can have at most {_MAX_LINES} rows. A double limit counts "
                "as one row. Remove some before adding more.")
            return

        grp = self._next_group()
        drawn = 0
        for d in defns:
            d["group"] = grp
            if self._build_item(d) is not None:
                drawn += 1
            self._line_defs.append(d)   # import regardless; draws when applicable

        if drawn == 0:
            QMessageBox.information(
                self, "Line Stored",
                f"Added, but '{unit}' isn't on the plot right now, so it isn't "
                "drawn yet. It will appear once that unit/axis is on the plot "
                "(e.g. after Run).")

        self._sync_added_lines()
        self._refresh_list()

    def _remove_group(self, gid):
        """Remove every line in a group (both halves of a double limit) and
        tear down their live items."""
        keep = []
        for d in self._line_defs:
            if d.get("group") != gid:
                keep.append(d)
                continue
            cl = d.get("_cleanup")
            if cl is not None:
                try:
                    cl()
                except Exception:
                    pass
            item = d.get("_item")
            if item is not None:
                dest_vb = d.get("_dest_vb")
                try:
                    if dest_vb is None:
                        self.graph_widget.plot_widget.removeItem(item)
                    else:
                        dest_vb.removeItem(item)
                except Exception:
                    pass
        self._line_defs = keep
        self._sync_added_lines()
        self._refresh_list()

    def _remove_all(self):
        self._clear_live_items()
        self._line_defs = []
        self._refresh_list()

    def _sync_added_lines(self):
        """Rebuild the external-facing added_lines list from the drawn defns."""
        self.added_lines = [
            (d["display"], d["_item"], d["eq_str"], d.get("_dest_vb"),
             d.get("_cleanup"), d["unit"])
            for d in self._line_defs if d.get("_item") is not None
        ]

    def _bound_text(self, d):
        """One bound rendered as e.g. 'Y <= 80' (or the raw equation for a
        sloped line, where an operator has no simple position)."""
        op   = (d.get("op") or "").strip()
        axis = d.get("axis", "Y")
        if d.get("line_type") == "L":
            return d["eq_str"]
        val = d["x_val"] if axis == "X" else d["y_val"]
        if op and op != "=":
            return f"{axis} {op} {val}"
        return f"{axis}={val}"

    def _refresh_list(self):
        while self.lines_layout.count():
            child = self.lines_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

        # Group defns by their group id, preserving insertion order. A double
        # limit (two defns sharing a group) renders as a single row.
        order, groups = [], {}
        for d in self._line_defs:
            g = d.get("group")
            if g not in groups:
                groups[g] = []
                order.append(g)
            groups[g].append(d)

        for g in order:
            members = groups[g]
            unit = members[0].get("unit", "—")
            base = members[0].get("label_base", "")
            bounds = "  ,  ".join(self._bound_text(d) for d in members)
            text = (f"{base}  ({bounds})  [{unit}]" if base
                    else f"{bounds}  [{unit}]")
            if not all(d.get("_item") is not None for d in members):
                text += "   — not drawn (unit not on plot)"
            row_widget = QWidget()
            row = QHBoxLayout(row_widget)
            row.setContentsMargins(0, 0, 0, 0)
            row.addWidget(QLabel(text))
            rm = QPushButton("Remove")
            rm.setFixedWidth(70)
            rm.clicked.connect(lambda checked, gid=g: self._remove_group(gid))
            row.addWidget(rm)
            self.lines_layout.addWidget(row_widget)

        if self._line_defs:
            rm_all = QPushButton("Remove All")
            rm_all.clicked.connect(self._remove_all)
            self.lines_layout.addWidget(rm_all)

    def closeEvent(self, event):
        if self.parent() is None:
            # Running standalone — actually quit
            self._remove_all()
            QApplication.instance().quit()
            event.accept()
        else:
            # Embedded in a larger app — just hide
            self.hide()
            event.ignore()        


if __name__ == "__main__":
    class DummyGraphWidget:
        def __init__(self):
            self.plot_widget = pg.PlotWidget()
            self.unit_axes = {"bar": {"vb": None}, "mV": {"vb": None}}

    dummy = DummyGraphWidget()
    dummy.plot_widget.show()
    dlg = StaticLineDialog(dummy)
    dlg.show()
    sys.exit(_standalone_app.exec_())


# ---------------------------------------------------------------------------
# Apps-bar registration (see DAQ_AppRegistry.py)
# ---------------------------------------------------------------------------

def build(ctx, parent):
    """Static lines only decorate the graph, so it needs the graph widget."""
    return StaticLineDialog(ctx.main_window.graphDataWidget, parent=parent)


APP_SPEC = {
    'id':    'static_lines',
    'label': 'Static Lines',
    'cls':   'StaticLineDialog',
    'build': build,
    # Touches no hardware and holds no output, so it stays available at all
    # times and never locks another app out.
    'claims': set(),
}
