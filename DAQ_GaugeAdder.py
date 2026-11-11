
"""
DAQ_GaugeAdder.py  —  Floating gauge windows for GioDAQ
========================================================

Provides up to 5 independent gauge windows, each showing one channel as either:
 • Line Chart  — scrolling time-series plot  (pyqtgraph PlotWidget)
 • Round Dial  — analog-style pressure/flow gauge  (custom QPainter widget)
 • Digital     — large numeric readout

Line Chart and Round Dial can also overlay saved constant limits (Y=c / X=c)
matching the channel's unit, via per-gauge checkboxes.

Each gauge can show its channel in another unit of the same quantity (psi as
bar, gpm as lpm, a flow as drop/min or Sun drops/min ...), picked beside its
Open button. Only the gauge's display
is converted: readings, limits and Min/Max shown in it. The recorded data and
everything else in the program stay in the channel's own unit.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
INTEGRATION
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

None. This module registers itself through the APP_SPEC at the bottom of the
file; DAQ_AppRegistry picks it up from APP_MODULES and the Apps bar builds the
button. Multichannel_DAQ.py needs no edits.
"""

import math
import numpy as np

from PyQt5.QtWidgets import (
   QDialog, QVBoxLayout, QHBoxLayout, QGridLayout,
   QLabel, QComboBox, QDoubleSpinBox, QPushButton,
   QScrollArea, QWidget, QSizePolicy, QSpinBox, QFrame,
   QGroupBox, QCheckBox,
)
from PyQt5.QtCore import Qt, QTimer, QRectF, QPointF, QPoint
from PyQt5.QtGui import QPainter, QPen, QColor, QFont, QFontMetricsF, QPolygon

import pyqtgraph as pg
from pyqtgraph import mkPen

from DAQ_Settings import unit_conversions, unit_affine, UNIT_CONVERSION_GROUPS


# ──────────────────────────────────────────────────────────────────────────────
# Saved constant limits (shared with the static-line tool)
# ──────────────────────────────────────────────────────────────────────────────

def _fmt_num(val):
   """Compact numeric formatting shared by gauges and limit labels."""
   try:
       val = float(val)
   except (TypeError, ValueError):
       return str(val)
   if val == int(val):
       return str(int(val))
   if abs(val) >= 100:
       return f"{val:.1f}"
   if abs(val) >= 10:
       return f"{val:.2f}"
   return f"{val:.3f}"


def _load_constant_limits():
   """Return saved CONSTANT plot-line limits as a list of dicts:
       {label, unit, axis, color, values: [(value, op), ...]}

   A Double limit keeps both constants in one entry (so one checkbox toggles
   both lines). Sloped lines (Y = m·x + b) are skipped, since they have no
   single value to show on a one-channel gauge. Reads the same `app_plot_lines`
   sheet the static-line tool writes; returns [] if it cannot be read."""
   out = []
   try:
       import os
       import pandas as pd
       from DAQ_StaticLines import _sl_settings_path
       path = _sl_settings_path()
       if not path or not os.path.isfile(path):
           return out
       xls = pd.ExcelFile(path)
       if "app_plot_lines" not in xls.sheet_names:
           return out
       df = pd.read_excel(xls, sheet_name="app_plot_lines").fillna("")
       for _, r in df.iterrows():
           label = str(r.get("Label", "")).strip()
           axis  = str(r.get("Axis", "Y")).strip().upper() or "Y"
           unit  = str(r.get("Unit", "")).strip()
           color = str(r.get("Color", "red")).strip() or "red"
           # Collect the row's constant value(s): a Single has one, a Double
           # has two — both kept under ONE entry so one checkbox toggles both.
           values = []
           for vk, ok in (("Value", "Op"), ("Value2", "Op2")):
               raw = str(r.get(vk, "")).strip()
               if not raw or "x" in raw.lower():
                   continue          # blank, or a sloped equation -> skip
               try:
                   value = float(raw)
               except ValueError:
                   continue
               op = str(r.get(ok, "")).strip()
               values.append((value, op))
           if not values:
               continue
           out.append({
               "label":  label or f"{axis}={_fmt_num(values[0][0])}",
               "unit":   unit,
               "axis":   axis,
               "color":  color,
               "values": values,   # [(value, op), ...]  1 (single) or 2 (double)
           })
   except Exception as e:
       print(f"Gauge adder: could not load limits: {e}")
   return out


# Drop counts offered for flow channels, on top of DAQ_Settings'
# UNIT_CONVERSION_GROUPS. They are not standard units, so they live here
# rather than in the shared unit registry: the Gauge Adder offers them, and
# nothing else in the program recognises them.
#   shown unit -> (standard unit it is counted from, drops per that unit)
_DROP_UNITS = {
   "drop/min":      ("ml/min",   1 / 0.05),   # 1 drop = 0.05 mL
   "Sun drops/min": ("in^3/min", 250.0),      # exactly 250 per in^3 (~15 per cc)
}


def _gauge_conversions(unit):
   """Units a gauge for a channel in `unit` can show: the shared list plus,
   for a volumetric flow, the drop counts."""
   targets = list(unit_conversions(unit))
   if targets:
       for shown, (base, _per) in _DROP_UNITS.items():
           try:
               unit_affine(unit, base)
           except Exception:
               continue        # not a volumetric flow
           targets.append(shown)
   return targets


def _gauge_affine(source, shown):
   """(scale, offset) from `source` to `shown`, drop counts included."""
   if shown in _DROP_UNITS:
       base, per = _DROP_UNITS[shown]
       scale, offset = unit_affine(source, base)
       return scale * per, offset * per
   return unit_affine(source, shown)


class _UnitView:
   """The unit a gauge shows its channel in.

   `source` is the channel's own unit, `shown` the unit on the gauge, and
   apply() takes a value (or numpy array) from source to shown. With no
   conversion, shown is source and apply() returns the value unchanged.
   """

   def __init__(self, source, shown=None, scale=1.0, offset=0.0):
       self.source = source
       self.shown  = source if shown is None else shown
       self.scale  = scale
       self.offset = offset

   def apply(self, value):
       return value * self.scale + self.offset


class _LimitCheckBar(QWidget):
   """A horizontal strip of checkboxes — one per saved constant limit whose
   unit matches the gauge's channel unit. Toggling a box calls
   ``on_toggle(limit_dict, checked)``.

   A gauge shows a single channel's *value*, so any matching constant (whether
   saved as Y=c or X=c) is treated as a value threshold at ``value``. Limits
   are saved in the channel's unit, so they are matched on `units.source` and
   shown converted to `units.shown`."""

   def __init__(self, units, on_toggle, parent=None):
       super().__init__(parent)
       self._on_toggle = on_toggle
       unit = (units.source or "").strip()
       self.limits = [L for L in _load_constant_limits()
                      if unit and L["unit"] == unit]

       lay = QHBoxLayout(self)
       lay.setContentsMargins(0, 0, 0, 0)
       lay.setSpacing(8)

       if not self.limits:
           note = QLabel("No saved limits for this unit")
           note.setStyleSheet("color: grey; font-size: 10px;")
           lay.addWidget(note)
           lay.addStretch()
           return

       title = QLabel("Limits:")
       title.setStyleSheet("color: #444; font-size: 10px;")
       lay.addWidget(title)
       for L in self.limits:
           parts = []
           for value, op in L["values"]:
               op_txt = f"{op} " if op in ("<", "<=", ">", ">=") else ""
               parts.append(f"{op_txt}{_fmt_num(units.apply(value))}")
           cb = QCheckBox(f"{L['label']} ({', '.join(parts)} {units.shown})")
           cb.setStyleSheet(f"color: {L['color']}; font-size: 10px;")
           cb.toggled.connect(
               lambda checked, _L=L: self._on_toggle(_L, checked))
           lay.addWidget(cb)
       lay.addStretch()


# ──────────────────────────────────────────────────────────────────────────────
# Round dial widget  — hydraulic gauge style
# Direct port of the bug-free reference design.
# ──────────────────────────────────────────────────────────────────────────────

class DialGaugeWidget(QWidget):
   """
   Hydraulic-style analog gauge.

   All geometry is in a fixed 300×300 unit space, auto-scaled to widget size.
   Angle convention: 0° = 12 o'clock, positive = clockwise.
   START_ANGLE=240 → 8 o'clock (lower-left, min value).
   END=480 (=120°) → 4 o'clock (lower-right, max value).
   240° clockwise sweep through 12 o'clock.

   Ticks and needle both use painter.rotate(); labels use qt_angle_to_xy()
   so every element shares exactly the same angle math — no misalignment.

   Public API: set_value(v), update_range(min_val, max_val).
   """

   START_ANGLE   = 240   # degrees: 0°=12 o'clock, CW positive
   SPAN_ANGLE    = 240   # degrees: total clockwise sweep
   DEFAULT_MAJOR_TICKS = 11   # one source for the dial, its window and the Ticks spinbox
   MINOR_PER_GAP = 4    # minor ticks between each pair of major ticks

   TICK_OUTER       = 125   # outer radius shared by all ticks
   MAJOR_TICK_INNER = 111   # major tick runs MAJOR_TICK_INNER → TICK_OUTER
   MINOR_TICK_INNER = 117   # minor tick runs MINOR_TICK_INNER → TICK_OUTER
   LABEL_GAP        = 4     # clearance between a label's box and the major ticks
   LIMIT_OUTER      = 135   # limit markers run TICK_OUTER → LIMIT_OUTER
   BAND_COLOR       = QColor(234, 234, 237)   # band behind the minor ticks
   MINOR_TICK_COLOR = QColor(120, 120, 120)   # minor ticks; also the unit text
   NEEDLE_LENGTH    = 115   # needle tip radius — reaches into the tick band

   def __init__(self, min_val: float, max_val: float,
                unit: str = "", major_ticks: int = DEFAULT_MAJOR_TICKS, parent=None):
       super().__init__(parent)
       self.min_val    = float(min_val)
       self.max_val    = float(max_val)
       self.unit       = unit
       self.MAJOR_TICKS = int(max(2, major_ticks))
       self._value     = float(min_val)
       self._limits    = []   # list of (value, color) markers to draw
       self.decimals   = 2    # readout decimal places (0–3), live-adjustable
       self.setMinimumSize(240, 240)

   # ── public API ────────────────────────────────────────────────────────────

   def set_value(self, v: float):
       self._value = float(v)
       self.update()

   def update_range(self, min_val: float, max_val: float):
       self.min_val = float(min_val)
       self.max_val = float(max_val)
       self.update()

   def set_limits(self, limits):
       """limits: iterable of (value, color) markers drawn on the dial face."""
       self._limits = list(limits)
       self.update()

   def set_decimals(self, n):
       """Decimal places (0–3) for the numeric readout below the needle."""
       self.decimals = max(0, min(3, int(n)))
       self.update()

   # ── painting ──────────────────────────────────────────────────────────────

   def paintEvent(self, _event):
       p = QPainter(self)
       p.setRenderHint(QPainter.Antialiasing)

       side = min(self.width(), self.height())
       p.translate(self.rect().center())    # origin → widget centre
       p.scale(side / 300.0, side / 300.0) # work in fixed 300-unit space

       self._draw_background(p)
       self._draw_ticks(p)
       self._draw_labels(p)
       self._draw_limits(p)
       self._draw_needle(p)
       self._draw_center(p)
       self._draw_value_text(p)

       p.end()

   # ── angle helpers — same convention for EVERYTHING ────────────────────────

   @staticmethod
   def _angle_to_xy(angle_deg: float, r: float):
       """
       Convert a gauge angle (0°=12 o'clock, CW positive) to (x, y)
       in the painter's coordinate space (y increases downward).
       Identical to the reference qt_angle_to_xy().
       """
       rad = math.radians(angle_deg)
       return r * math.sin(rad), -r * math.cos(rad)

   def _value_to_angle(self, value: float) -> float:
       """Map a data value to a gauge angle in degrees."""
       rng = self.max_val - self.min_val
       t   = (value - self.min_val) / rng if rng != 0 else 0.0
       t   = max(0.0, min(1.0, t))
       return self.START_ANGLE + t * self.SPAN_ANGLE

   LABEL_MAX_DECIMALS = 6   # hard ceiling for scale-number decimals

   @classmethod
   def _label_decimals(cls, values, step: float) -> int:
       """
       One decimal count shared by every scale number, set by tick spacing.

       Uses the fewest decimals that show every label exactly (0.25 steps →
       2; -12.5 start with 25 steps → 1). If the labels are never exact
       (100 / 9 = 11.11…), uses the decimals that show the step to two
       significant digits (11.11 → 0 → "11, 22, 33"; 1.111 → 1 → "1.1, 2.2").
       """
       tol = max(abs(step), max(abs(v) for v in values)) * 1e-12   # float noise only
       for d in range(cls.LABEL_MAX_DECIMALS + 1):
           if all(abs(round(v, d) - v) <= tol for v in values):
               return d
       # step > 0 here: equal labels (min == max) are always exact above
       cap = 1 - math.floor(math.log10(step))
       return max(0, min(cls.LABEL_MAX_DECIMALS, cap))

   # ── drawing ───────────────────────────────────────────────────────────────

   def _draw_background(self, p):
       # Flat light face with a thin rim — no bezel, no gradient.
       p.setPen(QPen(QColor(190, 190, 195), 2))
       p.setBrush(QColor(245, 245, 247))
       p.drawEllipse(-140, -140, 280, 280)

       # Light-gray band behind the minor ticks, only across the sweep.
       # drawArc() uses Qt angles (0° = 3 o'clock, CCW positive, 1/16°), so
       # gauge angle a → 90 − a, and the clockwise sweep is a negative span.
       r_in, r_out = self.MINOR_TICK_INNER, self.TICK_OUTER
       r_mid = (r_in + r_out) / 2
       band  = QPen(self.BAND_COLOR, r_out - r_in)
       band.setCapStyle(Qt.FlatCap)
       p.setPen(band)
       p.setBrush(Qt.NoBrush)
       p.drawArc(QRectF(-r_mid, -r_mid, 2 * r_mid, 2 * r_mid),
                 int((90 - self.START_ANGLE) * 16), int(-self.SPAN_ANGLE * 16))

       # Thin ring at the outer edge of the limit markers.
       p.setPen(QPen(QColor(190, 190, 195), 1))
       p.setBrush(Qt.NoBrush)
       r = self.LIMIT_OUTER
       p.drawEllipse(QRectF(-r, -r, 2 * r, 2 * r))

   def _draw_ticks(self, p):
       """
       MAJOR_TICKS major ticks, MINOR_PER_GAP minor ticks between each pair.
       Uses painter.rotate() — identical approach to the reference.
       """
       major_gap = self.SPAN_ANGLE / (self.MAJOR_TICKS - 1)
       minor_gap = major_gap / (self.MINOR_PER_GAP + 1)

       for i in range(self.MAJOR_TICKS):
           major_angle = self.START_ANGLE + major_gap * i

           # major tick
           p.save()
           p.rotate(major_angle)
           p.setPen(QPen(QColor(60, 60, 60), 2))
           p.drawLine(0, -self.MAJOR_TICK_INNER, 0, -self.TICK_OUTER)
           p.restore()

           # minor ticks before the next major
           if i < self.MAJOR_TICKS - 1:
               for j in range(1, self.MINOR_PER_GAP + 1):
                   p.save()
                   p.rotate(major_angle + minor_gap * j)
                   p.setPen(QPen(self.MINOR_TICK_COLOR, .75))
                   p.drawLine(0, -self.MINOR_TICK_INNER, 0, -self.TICK_OUTER)
                   p.restore()

   def _draw_labels(self, p):
       """
       One label per major tick, using _angle_to_xy() — same angle math as
       the ticks so they always align.

       Each label is pushed as far out as it can go: its box is placed so the
       edge facing the rim sits LABEL_GAP inside the major ticks. The radial
       extent of a w×h box along angle θ is |sin θ|·w/2 + |cos θ|·h/2, so wide
       labels at 3/9 o'clock come in further than short ones at 12 o'clock.
       """
       font = QFont("Arial", 7, QFont.DemiBold)
       fm   = QFontMetricsF(font, self)
       p.setPen(QColor(40, 40, 40))
       p.setFont(font)

       major_gap = self.SPAN_ANGLE / (self.MAJOR_TICKS - 1)
       step      = (self.max_val - self.min_val) / (self.MAJOR_TICKS - 1)
       values    = [self.min_val + step * i for i in range(self.MAJOR_TICKS)]
       decimals  = self._label_decimals(values, abs(step))
       edge      = self.MAJOR_TICK_INNER - self.LABEL_GAP
       h         = fm.height()

       for i, val in enumerate(values):
           angle = self.START_ANGLE + major_gap * i
           text  = f"{val:.{decimals}f}"
           w     = fm.horizontalAdvance(text)
           rad   = math.radians(angle)
           r     = edge - (abs(math.sin(rad)) * w / 2 + abs(math.cos(rad)) * h / 2)
           x, y  = self._angle_to_xy(angle, r)
           p.drawText(QRectF(x - w / 2, y - h / 2, w, h), Qt.AlignCenter, text)

   def _draw_limits(self, p):
       """Coloured markers at each limit value that falls within range."""
       lo, hi = min(self.min_val, self.max_val), max(self.min_val, self.max_val)
       for value, color in self._limits:
           if value < lo or value > hi:
               continue
           p.save()
           p.rotate(self._value_to_angle(value))
           p.setPen(QPen(QColor(color), 0.75))   # same weight as minor ticks
           p.drawLine(0, -self.TICK_OUTER, 0, -self.LIMIT_OUTER)
           p.restore()

   def _draw_needle(self, p):
       """Triangle needle, rotated with _value_to_angle() — same convention."""
       p.save()
       p.rotate(self._value_to_angle(self._value))

       needle = QPolygon([
           QPoint(-6,  10),
           QPoint( 0, -self.NEEDLE_LENGTH),
           QPoint( 6,  10),
       ])
       p.setPen(Qt.NoPen)
       p.setBrush(QColor(231, 76, 60))
       p.drawPolygon(needle)

       p.restore()

   def _draw_center(self, p):
       p.setPen(Qt.NoPen)
       p.setBrush(QColor(70, 70, 75))
       p.drawEllipse(-12, -12, 24, 24)

   VALUE_FONT_PT = 15   # readout value size
   UNIT_FONT_PT  = 14   # unit line, one size below the value
   UNIT_Y_SCALE  = 0.8  # unit text height factor (width unchanged)
   VALUE_MIN_PT  = 9    # smallest size used when shrinking a long line

   def _fit_font(self, text, start_pt, width):
       """Arial (normal weight) at start_pt, stepped down until text fits."""
       font = QFont("Arial", start_pt, QFont.Normal)
       while (font.pointSize() > self.VALUE_MIN_PT and
              QFontMetricsF(font, self).horizontalAdvance(text) > width):
           font.setPointSize(font.pointSize() - 1)
       return font

   def _draw_value_text(self, p):
       """Plain Arial readout, no box: the value, with the unit on its own
       line below it in MINOR_TICK_COLOR, squashed vertically by UNIT_Y_SCALE.
       Each line shrinks to fit on its own."""
       value_rect = QRectF(-90, 44, 180, 28)
       unit_rect  = QRectF(-90, 70, 180, 24)

       value_str = f"{self._value:.{self.decimals}f}"
       p.setPen(QColor(25, 25, 28))
       p.setFont(self._fit_font(value_str, self.VALUE_FONT_PT, value_rect.width()))
       p.drawText(value_rect, Qt.AlignCenter, value_str)

       unit_str = (self.unit or "").strip()
       if unit_str:
           p.save()
           # Scale about the line's centre so it stays in place, only shorter.
           c = unit_rect.center()
           p.translate(c)
           p.scale(1.0, self.UNIT_Y_SCALE)
           p.setPen(self.MINOR_TICK_COLOR)
           p.setFont(self._fit_font(unit_str, self.UNIT_FONT_PT, unit_rect.width()))
           p.drawText(unit_rect.translated(-c), Qt.AlignCenter, unit_str)
           p.restore()


# ──────────────────────────────────────────────────────────────────────────────
# Dial gauge window
# ──────────────────────────────────────────────────────────────────────────────

class DialGaugeWindow(QDialog):
   """
   Floating window that houses a DialGaugeWidget.
   Polls the ring buffer on a QTimer (~15 Hz) — lightweight, no signal spam.
   """

   def __init__(self, channel_name: str, ch_index: int,
                min_val: float, max_val: float, units: _UnitView,
                main_window, major_ticks: int = DialGaugeWidget.DEFAULT_MAJOR_TICKS,
                on_closed=None, parent=None):
       super().__init__(parent,
                        Qt.Window | Qt.WindowCloseButtonHint | Qt.WindowTitleHint)
       self.setWindowTitle(f"Dial — {channel_name}")
       self.ch_index    = ch_index
       self.units       = units
       self.main_window = main_window
       self._on_closed  = on_closed

       # ── UI ──
       lay = QVBoxLayout(self)
       lay.setContentsMargins(10, 8, 10, 8)
       lay.setSpacing(4)

       hdr_row = QHBoxLayout()
       hdr_row.addWidget(QLabel(f"<b>{channel_name}</b>"))
       hdr_row.addStretch()
       hdr_row.addWidget(QLabel("Decimals:"))
       self._dec_spin = QSpinBox()
       self._dec_spin.setRange(0, 3)
       self._dec_spin.setValue(2)
       self._dec_spin.setFixedWidth(42)
       self._dec_spin.setToolTip("Decimal places in the readout (0–3)")
       hdr_row.addWidget(self._dec_spin)
       lay.addLayout(hdr_row)

       self._dial = DialGaugeWidget(min_val, max_val, units.shown,
                                    major_ticks=major_ticks)
       self._dial.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
       lay.addWidget(self._dial)
       self._dec_spin.valueChanged.connect(self._dial.set_decimals)
       self._dial.set_decimals(self._dec_spin.value())

       # "No data" label (shown until DAQ runs)
       self._no_data_lbl = QLabel("Waiting for data…")
       self._no_data_lbl.setAlignment(Qt.AlignCenter)
       self._no_data_lbl.setStyleSheet("color: grey; font-size: 10px;")
       lay.addWidget(self._no_data_lbl)

       # Limit checkboxes (constant limits matching this channel's unit)
       self._active_limits = []
       self._limit_bar = _LimitCheckBar(units, self._on_limit_toggle)
       lay.addWidget(self._limit_bar)

       self.resize(290, 340)

       # ── poll timer ──
       self._timer = QTimer(self)
       self._timer.setInterval(66)         # ~15 Hz
       self._timer.timeout.connect(self._refresh)
       self._timer.start()

   # ── slots ─────────────────────────────────────────────────────────────────

   def _refresh(self):
       mw = self.main_window
       has_data = (hasattr(mw, 'rings') and
                   hasattr(mw, 'total_written') and
                   mw.total_written > 0 and
                   self.ch_index < len(mw.rings))
       self._no_data_lbl.setVisible(not has_data)
       if not has_data:
           return
       try:
           v = float(mw.rb_tail(self.ch_index, 1)[-1])
           self._dial.set_value(self.units.apply(v))
       except Exception:
           pass

   def _on_limit_toggle(self, limit, checked):
       color = limit["color"]
       keys  = [(self.units.apply(value), color) for value, _op in limit["values"]]
       if checked:
           for k in keys:
               if k not in self._active_limits:
                   self._active_limits.append(k)
       else:
           self._active_limits = [k for k in self._active_limits if k not in keys]
       self._dial.set_limits(self._active_limits)

   def closeEvent(self, event):
       self._timer.stop()
       if self._on_closed:
           self._on_closed()
       super().closeEvent(event)


# ──────────────────────────────────────────────────────────────────────────────
# Line-chart gauge window
# ──────────────────────────────────────────────────────────────────────────────

_CHART_COLORS = [
   '#e05252', '#4a9edd', '#4caf50', '#f5a623',
   '#9b59b6', '#1abc9c', '#e67e22', '#e91e63',
]


class LineChartGaugeWindow(QDialog):
   """
   Floating scrolling line chart for a single channel. Polls the shared ring
   buffer on a timer (like the dial/digital gauges) and decimates before
   plotting, so long windows stay responsive and nothing accumulates.
   """

   def __init__(self, channel_name: str, ch_index: int, units: _UnitView,
                window_s: int, main_window, on_closed=None, parent=None):
       super().__init__(parent,
                        Qt.Window | Qt.WindowCloseButtonHint | Qt.WindowTitleHint)
       self.setWindowTitle(f"Chart — {channel_name}")
       self.ch_index    = ch_index
       self.units       = units
       self.unit        = units.shown
       self.window_s    = window_s
       self.main_window = main_window
       self._on_closed  = on_closed
       self._color      = _CHART_COLORS[ch_index % len(_CHART_COLORS)]

       self._build_ui(channel_name)
       self.resize(500, 300)

       # Poll the shared ring buffer on a timer (like the dial/digital gauges)
       # rather than subscribing to data_ready: throttled, read-only, and torn
       # down cleanly on close, so it never piles up work or accumulates.
       self._timer = QTimer(self)
       self._timer.setInterval(66)        # ~15 Hz
       self._timer.timeout.connect(self._refresh)
       self._timer.start()

   # ── UI ────────────────────────────────────────────────────────────────────

   def _build_ui(self, name):
       lay = QVBoxLayout(self)
       lay.setContentsMargins(8, 6, 8, 6)
       lay.setSpacing(4)

       # Header row
       hdr = QHBoxLayout()
       hdr.addWidget(QLabel(f"<b>{name}</b>  [{self.unit}]"))
       hdr.addStretch()
       hdr.addWidget(QLabel("Window (s):"))
       self._win_spin = QSpinBox()
       self._win_spin.setRange(1, 600)
       self._win_spin.setValue(self.window_s)
       self._win_spin.setFixedWidth(62)
       self._win_spin.setToolTip("How many seconds of history to display")
       self._win_spin.valueChanged.connect(lambda v: setattr(self, 'window_s', v))
       hdr.addWidget(self._win_spin)
       lay.addLayout(hdr)

       # Plot widget
       self._plot = pg.PlotWidget(background='w')
       self._plot.showGrid(x=True, y=True, alpha=0.25)
       self._plot.setLabel('left',   self.unit)
       self._plot.setLabel('bottom', 'Time (s)')
       self._plot.getViewBox().enableAutoRange(axis=pg.ViewBox.YAxis, enable=True)

       self._curve = self._plot.plot(pen=mkPen(color=self._color, width=2))
       # Same large-data safeguards the main plot uses: render only what is
       # visible, downsampled — keeps long windows smooth. Wrapped because
       # the kwarg name varies across pyqtgraph versions; the stride cap in
       # _refresh() is the real crash guard, so this is only a bonus.
       try:
           self._curve.setClipToView(True)
           self._curve.setDownsampling(auto=True, method='peak')
       except Exception:
           pass
       lay.addWidget(self._plot)

       # "No data" overlay label
       self._no_data_lbl = QLabel("Waiting for data…")
       self._no_data_lbl.setAlignment(Qt.AlignCenter)
       self._no_data_lbl.setStyleSheet("color: grey; font-size: 10px;")
       lay.addWidget(self._no_data_lbl)

       # Limit checkboxes (constant limits matching this channel's unit)
       self._limit_lines = {}
       self._limit_bar = _LimitCheckBar(self.units, self._on_limit_toggle)
       lay.addWidget(self._limit_bar)

   # ── data polling ──────────────────────────────────────────────────────────

   _MAX_POINTS = 4000   # cap points handed to setData() so long windows stay fast

   def _refresh(self):
       mw = self.main_window
       has_data = (hasattr(mw, 'rings') and
                   hasattr(mw, 'total_written') and
                   mw.total_written > 0 and
                   self.ch_index < len(mw.rings))
       self._no_data_lbl.setVisible(not has_data)
       if not has_data:
           return
       try:
           sr = getattr(mw, 'sample_rate', 1000)
           n  = min(int(self.window_s * sr), mw.total_written,
                    mw.rings[self.ch_index].size)
           if n <= 0:
               return
           y = self.units.apply(mw.rb_tail(self.ch_index, n))
           end_t   = mw.total_written / sr
           start_t = end_t - n / sr
           # Decimate before setData so we never push millions of points.
           if n > self._MAX_POINTS:
               step = (n + self._MAX_POINTS - 1) // self._MAX_POINTS
               y = y[::step]
           x = np.linspace(start_t, end_t, len(y), endpoint=False, dtype=np.float32)
           self._curve.setData(x, y)
       except Exception:
           pass

   def _on_limit_toggle(self, limit, checked):
       key = id(limit)
       if checked:
           if key in self._limit_lines:
               return
           lines = []
           for value, op in limit["values"]:
               value = self.units.apply(value)
               op_txt = f"{op} " if op in ("<", "<=", ">", ">=") else ""
               ln = pg.InfiniteLine(
                   pos=value, angle=0,
                   pen=mkPen(color=limit["color"], width=1.0, style=Qt.DashLine),
                   label=f"{limit['label']} {op_txt}{_fmt_num(value)}",
                   labelOpts={"position": 0.05, "color": limit["color"]},
               )
               self._plot.addItem(ln)
               lines.append(ln)
           self._limit_lines[key] = lines
       else:
           lines = self._limit_lines.pop(key, None)
           if lines:
               for ln in lines:
                   self._plot.removeItem(ln)

   def closeEvent(self, event):
       self._timer.stop()
       if self._on_closed:
           self._on_closed()
       super().closeEvent(event)


# ──────────────────────────────────────────────────────────────────────────────
# Digital readout gauge window
# ──────────────────────────────────────────────────────────────────────────────

class DigitalGaugeWindow(QDialog):
   """Large numeric readout of a single channel's latest value."""

   def __init__(self, channel_name: str, ch_index: int, units: _UnitView,
                main_window, on_closed=None, parent=None):
       super().__init__(parent,
                        Qt.Window | Qt.WindowCloseButtonHint | Qt.WindowTitleHint)
       self.setWindowTitle(f"Digital — {channel_name}")
       self.ch_index    = ch_index
       self.units       = units
       self.unit        = units.shown
       self.main_window = main_window
       self._on_closed  = on_closed

       lay = QVBoxLayout(self)
       lay.setContentsMargins(16, 12, 16, 12)
       lay.setSpacing(2)

       name_lbl = QLabel(channel_name)
       name_lbl.setAlignment(Qt.AlignCenter)
       name_lbl.setStyleSheet("color: #555; font-size: 14px; font-weight: bold;")
       lay.addWidget(name_lbl)

       self._value_lbl = QLabel("—")
       self._value_lbl.setAlignment(Qt.AlignCenter)
       self._value_lbl.setStyleSheet(
           "color: #111; font-family: Consolas, monospace; "
           "font-size: 48px; font-weight: bold;")
       lay.addWidget(self._value_lbl, 1)

       unit_lbl = QLabel(units.shown)
       unit_lbl.setAlignment(Qt.AlignCenter)
       unit_lbl.setStyleSheet("color: #555; font-size: 18px;")
       lay.addWidget(unit_lbl)

       self._no_data_lbl = QLabel("Waiting for data…")
       self._no_data_lbl.setAlignment(Qt.AlignCenter)
       self._no_data_lbl.setStyleSheet("color: grey; font-size: 10px;")
       lay.addWidget(self._no_data_lbl)

       self.resize(260, 190)

       self._timer = QTimer(self)
       self._timer.setInterval(66)        # ~15 Hz
       self._timer.timeout.connect(self._refresh)
       self._timer.start()

   def _refresh(self):
       mw = self.main_window
       has_data = (hasattr(mw, 'rings') and
                   hasattr(mw, 'total_written') and
                   mw.total_written > 0 and
                   self.ch_index < len(mw.rings))
       self._no_data_lbl.setVisible(not has_data)
       if not has_data:
           return
       try:
           v = float(mw.rb_tail(self.ch_index, 1)[-1])
           self._value_lbl.setText(_fmt_num(self.units.apply(v)))
       except Exception:
           pass

   def closeEvent(self, event):
       self._timer.stop()
       if self._on_closed:
           self._on_closed()
       super().closeEvent(event)


# ──────────────────────────────────────────────────────────────────────────────
# One gauge slot (a row in the GaugeAdderDialog)
# ──────────────────────────────────────────────────────────────────────────────

# ── Row / header columns ──────────────────────────────────────────────────────
# The header and every gauge row are laid out from this one table, so they
# cannot drift apart. Each entry is (header text, width); every column is that
# fixed width except Channel, which is the only one that stretches —
# so when the window is widened the extra space goes to the same column in the
# header and in every row. The Unit width is measured, not guessed (see
# _unit_col_width).
_ROW_SPACING = 5
_COLUMNS = (
   ("#",        18),
   ("Type",    102),
   ("Channel", None),      # stretches; at least _CHANNEL_MIN_W
   ("",         26),       # "Min" label
   ("Min",      82),
   ("",         26),       # "Max" label
   ("Max",      82),
   ("Options", 130),
   ("Unit",     None),     # fixed, but measured: _unit_col_width()
   ("",         64),       # Open button
   ("",         28),       # Remove button
)
_CHANNEL_MIN_W = 120
_unit_w = None


def _unit_col_width():
   """Width of the Unit column: the widest entry it can ever hold ("No
   conversion" or any unit), measured as a combo box in the current style and
   font, so no entry is hidden behind the drop-down arrow."""
   global _unit_w
   if _unit_w is None:
       probe = QComboBox()
       probe.setSizeAdjustPolicy(QComboBox.AdjustToContents)
       probe.addItem(GaugeRowWidget.NO_CONVERSION)
       for group in UNIT_CONVERSION_GROUPS:
           probe.addItems(group)
       probe.addItems(list(_DROP_UNITS))
       _unit_w = probe.sizeHint().width()
       probe.deleteLater()
   return _unit_w


class GaugeRowWidget(QWidget):
   """
   Horizontal row containing:
     [#]  [Type ▾]  [Channel ▾]  Min [____]  Max [____]  Win [__]s  [Unit ▾]  [Open]  [✕]

   The History (s) controls are hidden when gauge type is "Round Dial".
   """

   def __init__(self, row_num: int, plot_options, default_settings,
                No_chans: int, main_window, on_remove, parent=None):
       super().__init__(parent)
       self._plot_options     = plot_options
       self._default_settings = default_settings
       self._No_chans         = No_chans
       self._main_window      = main_window
       self._on_remove        = on_remove
       self._gauge_window     = None
       # Gauge Min/Max, kept in the channel's own unit. The spinboxes show
       # them in the chosen unit; keeping the channel-unit values as the
       # source means switching units back and forth never compounds the
       # spinboxes' rounding.
       self._range_src        = (0.0, 100.0)
       self._showing_range    = False

       self._build_ui(row_num)

   # ── UI ────────────────────────────────────────────────────────────────────

   def _build_ui(self, row_num):
       # Every width here comes from _COLUMNS, which the header uses too.
       widths = [w for _h, w in _COLUMNS]
       lay = QHBoxLayout(self)
       lay.setContentsMargins(0, 2, 0, 2)
       lay.setSpacing(_ROW_SPACING)

       # Row number
       num_lbl = QLabel(str(row_num))
       num_lbl.setFixedWidth(widths[0])
       num_lbl.setAlignment(Qt.AlignCenter)
       num_lbl.setStyleSheet("color: grey; font-size: 10px;")
       lay.addWidget(num_lbl)

       # Gauge type
       self._type_combo = QComboBox()
       self._type_combo.addItems(["Line Chart", "Round Dial", "Digital"])
       self._type_combo.setFixedWidth(widths[1])
       self._type_combo.setToolTip("Gauge style")
       self._type_combo.currentIndexChanged.connect(self._on_type_changed)
       lay.addWidget(self._type_combo)

       # Channel
       self._chan_combo = QComboBox()
       po = self._plot_options
       names = po["Device Name"].tolist() if len(po) > 0 else ["—"]
       self._chan_combo.addItems(names)
       self._chan_combo.setMinimumWidth(_CHANNEL_MIN_W)
       self._chan_combo.setToolTip("Channel to display")
       self._chan_combo.currentIndexChanged.connect(self._on_channel_changed)
       lay.addWidget(self._chan_combo)

       # Min / Max spinboxes
       self._min_spin = QDoubleSpinBox()
       self._max_spin = QDoubleSpinBox()
       for sp, tip in [(self._min_spin, "Gauge minimum value"),
                       (self._max_spin, "Gauge maximum value")]:
           sp.setRange(-1e9, 1e9)
           sp.setDecimals(3)
           sp.setFixedWidth(widths[4])
           sp.setSingleStep(1.0)
           sp.setToolTip(tip)
       for text, spin, w in (("Min", self._min_spin, widths[3]),
                             ("Max", self._max_spin, widths[5])):
           lbl = QLabel(text)
           lbl.setFixedWidth(w)
           lay.addWidget(lbl)
           lay.addWidget(spin)
       self._min_spin.valueChanged.connect(self._on_range_edited)
       self._max_spin.valueChanged.connect(self._on_range_edited)

       # Shared fixed-width container — shows either "History (s)" (Line Chart)
       # or "Major ticks" (Round Dial). Fixed width prevents layout shift on swap.
       self._win_container = QWidget()
       self._win_container.setFixedWidth(widths[7])
       win_inner = QHBoxLayout(self._win_container)
       win_inner.setContentsMargins(0, 0, 0, 0)
       win_inner.setSpacing(4)

       self._win_lbl  = QLabel("History (s)")
       self._win_spin = QSpinBox()
       self._win_spin.setRange(1, 600)
       self._win_spin.setValue(30)
       self._win_spin.setFixedWidth(48)
       self._win_spin.setToolTip("How many seconds of data history to display")
       win_inner.addWidget(self._win_lbl)
       win_inner.addWidget(self._win_spin)

       self._ticks_lbl  = QLabel("Maj. ticks")
       self._ticks_spin = QSpinBox()
       self._ticks_spin.setRange(2, 20)
       self._ticks_spin.setValue(DialGaugeWidget.DEFAULT_MAJOR_TICKS)
       self._ticks_spin.setFixedWidth(48)
       self._ticks_spin.setToolTip("Number of major tick marks on the dial (2–20)")
       win_inner.addWidget(self._ticks_lbl)
       win_inner.addWidget(self._ticks_spin)
       # Dial controls hidden by default (Line Chart is the default type)
       self._ticks_lbl.setVisible(False)
       self._ticks_spin.setVisible(False)

       lay.addWidget(self._win_container)

       # Unit the gauge shows the channel in. Filled per channel by
       # _on_channel_changed; only units of the same quantity are offered.
       self._unit_combo = QComboBox()
       self._unit_combo.setFixedWidth(_unit_col_width())
       self._unit_combo.currentIndexChanged.connect(self._on_unit_changed)
       lay.addWidget(self._unit_combo)

       # Open / Close button
       self._open_btn = QPushButton("Open")
       self._open_btn.setFixedWidth(widths[9])
       self._open_btn.clicked.connect(self._toggle_gauge)
       lay.addWidget(self._open_btn)

       # Remove button
       rem_btn = QPushButton("✕")
       rem_btn.setFixedWidth(widths[10])
       rem_btn.setToolTip("Remove this gauge slot")
       rem_btn.clicked.connect(self._remove)
       lay.addWidget(rem_btn)

       # Initialise spinboxes from first channel in list
       self._on_channel_changed()

   # ── helpers ───────────────────────────────────────────────────────────────

   def set_row_number(self, n: int):
       """Update the displayed row number after a row above is removed."""
       lbl = self.layout().itemAt(0).widget()
       if isinstance(lbl, QLabel):
           lbl.setText(str(n))

   def _channel_meta(self):
       """
       Return (ring_buffer_index, min_eng, max_eng, unit) for the selected channel.
       Engineering min/max are derived from Min/Max × (Mx, b).

       NOTE: Min/Max are in the row's 'Range Unit'. Mx/b must have been
       calibrated against that same unit — a current channel reads in amps
       (0.004-0.02), not milliamps, so a volt-calibrated Mx gives a wrong scale.
       Falls back to (0.0, 100.0) for custom/virtual channels.
       """
       name  = self._chan_combo.currentText()
       po    = self._plot_options
       ds    = self._default_settings
       match = po.index[po["Device Name"] == name]
       if match.empty:
           return 0, 0.0, 100.0, ""
       idx  = int(match[0])
       unit = str(po.loc[idx, "Units"]) if "Units" in po.columns else ""

       # Hardware input channels carry Mx / b / voltage limits
       if idx < self._No_chans and idx < len(ds):
           row = ds.iloc[idx]
           try:
               mx   = float(row["Mx"])
               b    = float(row["b"])
               vmin = float(row["Min"])
               vmax = float(row["Max"])
               eng_min = vmin * mx + b
               eng_max = vmax * mx + b
               if eng_min == eng_max:
                   eng_max = eng_min + 1.0
               return idx, eng_min, eng_max, unit
           except Exception:
               pass

       return idx, 0.0, 100.0, unit

   NO_CONVERSION = "No conversion"

   def _on_channel_changed(self):
       idx, mn, mx, unit = self._channel_meta()
       if mn > mx:
           mn, mx = mx, mn       # handle inverted calibrations
       self._range_src = (mn, mx)

       # A new channel starts in its own unit.
       targets = _gauge_conversions(unit)
       self._unit_combo.blockSignals(True)
       self._unit_combo.clear()
       self._unit_combo.addItem(self.NO_CONVERSION)
       self._unit_combo.addItems(targets)
       self._unit_combo.blockSignals(False)
       self._unit_combo.setEnabled(bool(targets))
       self._unit_combo.setToolTip(
           f"Show this gauge in another unit (channel unit: {unit})" if targets
           else "No other units available for this channel's unit")
       self._show_range()

   def _units(self):
       """The _UnitView for the current channel and Unit choice."""
       source = self._channel_meta()[3]
       if self._unit_combo.currentIndex() <= 0:
           return _UnitView(source)
       shown = self._unit_combo.currentText()
       scale, offset = _gauge_affine(source, shown)
       return _UnitView(source, shown, scale, offset)

   def _show_range(self):
       """Put the channel-unit Min/Max into the spinboxes, in the chosen unit."""
       units = self._units()
       lo, hi = (units.apply(v) for v in self._range_src)
       self._showing_range = True
       try:
           self._min_spin.setValue(min(lo, hi))
           self._max_spin.setValue(max(lo, hi))
       finally:
           self._showing_range = False

   def _on_range_edited(self):
       """A Min/Max typed in the chosen unit, stored back in the channel unit."""
       if self._showing_range:
           return
       units = self._units()
       self._range_src = tuple((sp.value() - units.offset) / units.scale
                               for sp in (self._min_spin, self._max_spin))

   def _on_unit_changed(self):
       # Min/Max follow the unit, so a range typed in by hand is kept.
       self._show_range()
       # An open gauge shows the old unit; close it, as a Type change does.
       self._close_gauge()

   def _on_type_changed(self):
       kind     = self._type_combo.currentText()
       is_chart = kind == "Line Chart"
       is_dial  = kind == "Round Dial"
       self._win_lbl.setVisible(is_chart)
       self._win_spin.setVisible(is_chart)
       self._ticks_lbl.setVisible(is_dial)
       self._ticks_spin.setVisible(is_dial)
       self._close_gauge()

   # ── gauge window lifecycle ────────────────────────────────────────────────

   def _toggle_gauge(self):
       if self._gauge_window is None:
           self._open_gauge()
       else:
           self._close_gauge()

   def _open_gauge(self):
       self._close_gauge()   # ensure clean state first

       ch_idx = self._channel_meta()[0]
       units = self._units()
       name = self._chan_combo.currentText()
       kind = self._type_combo.currentText()
       mn   = self._min_spin.value()
       mx   = self._max_spin.value()

       if kind == "Round Dial":
           win = DialGaugeWindow(
               channel_name = name,
               ch_index     = ch_idx,
               min_val      = mn,
               max_val      = mx,
               units        = units,
               major_ticks  = self._ticks_spin.value(),
               main_window  = self._main_window,
               on_closed    = self._on_gauge_closed,
               parent       = None,
           )
       elif kind == "Digital":
           win = DigitalGaugeWindow(
               channel_name = name,
               ch_index     = ch_idx,
               units        = units,
               main_window  = self._main_window,
               on_closed    = self._on_gauge_closed,
               parent       = None,
           )
       else:
           win = LineChartGaugeWindow(
               channel_name = name,
               ch_index     = ch_idx,
               units        = units,
               window_s     = self._win_spin.value(),
               main_window  = self._main_window,
               on_closed    = self._on_gauge_closed,
               parent       = None,
           )

       win.show()
       self._gauge_window = win
       self._open_btn.setText("Close")
       self._open_btn.setToolTip("Close this gauge window")

   def _close_gauge(self):
       if self._gauge_window is not None:
           # Temporarily suppress the callback to avoid double-triggering
           try:
               self._gauge_window._on_closed = None
               self._gauge_window.close()
           except Exception:
               pass
           self._gauge_window = None
       self._open_btn.setText("Open")
       self._open_btn.setToolTip("Open this gauge window")

   def _on_gauge_closed(self):
       """Called when the user closes the gauge window with its own X button."""
       self._gauge_window = None
       self._open_btn.setText("Open")
       self._open_btn.setToolTip("Open this gauge window")

   def _remove(self):
       self._close_gauge()
       self._on_remove(self)


# ──────────────────────────────────────────────────────────────────────────────
# Main Gauge Adder dialog
# ──────────────────────────────────────────────────────────────────────────────

class GaugeAdderDialog(QDialog):
   """
   Floating dialog that manages up to MAX_GAUGES gauge slots.

   Parameters
   ----------
   main_window : VoltageContinuousInput
       The main DAQ window (provides ring buffers and data_ready signal).
   plot_options : pd.DataFrame
       Combined channel list (Device Name, Units, …).
   default_settings : pd.DataFrame
       Hardware input channel calibration (Mx, b, Min/Max, …).
   No_chans : int
       Count of hardware input channels (vs custom/virtual channels).
   """

   MAX_GAUGES = 5

   def __init__(self, main_window, plot_options, default_settings,
                No_chans: int, parent=None):
       super().__init__(parent,
                        Qt.Window |
                        Qt.WindowCloseButtonHint |
                        Qt.WindowMinimizeButtonHint |
                        Qt.WindowTitleHint)
       self.setWindowTitle("Gauge Adder")
       self._main_window      = main_window
       self._plot_options     = plot_options
       self._default_settings = default_settings
       self._No_chans         = No_chans
       self._rows: list       = []

       self._build_ui()
       # As narrow as the fixed columns allow; wider only stretches Channel.
       self.resize(self.minimumSizeHint().width(), 160)

   # ── UI ────────────────────────────────────────────────────────────────────

   def _build_ui(self):
       outer = QVBoxLayout(self)
       outer.setContentsMargins(10, 10, 10, 10)
       outer.setSpacing(8)

       # Column header row — laid out from the same _COLUMNS table as each
       # GaugeRowWidget, so the columns line up at any window width.
       hdr_widget = QWidget()
       hdr = QHBoxLayout(hdr_widget)
       hdr.setContentsMargins(0, 0, 0, 0)
       hdr.setSpacing(_ROW_SPACING)
       for i, (text, width) in enumerate(_COLUMNS):
           lbl = QLabel(text)
           lbl.setStyleSheet("font-weight: bold; font-size: 10px;")
           if text == "Unit":
               width = _unit_col_width()
           if width is None:
               lbl.setMinimumWidth(_CHANNEL_MIN_W)
               lbl.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
           else:
               lbl.setFixedWidth(width)
               lbl.setAlignment(Qt.AlignCenter)
           hdr.addWidget(lbl)
       outer.addWidget(hdr_widget)

       sep = QFrame()
       sep.setFrameShape(QFrame.HLine)
       sep.setFrameShadow(QFrame.Sunken)
       outer.addWidget(sep)

       # Scrollable area for rows
       self._rows_container = QWidget()
       self._rows_layout    = QVBoxLayout(self._rows_container)
       self._rows_layout.setContentsMargins(0, 0, 0, 0)
       self._rows_layout.setSpacing(2)
       self._rows_layout.addStretch(1)          # pushes rows to the top

       scroll = QScrollArea()
       scroll.setWidgetResizable(True)
       scroll.setWidget(self._rows_container)
       scroll.setFrameShape(QFrame.NoFrame)
       scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
       outer.addWidget(scroll, 1)

       # When the vertical scrollbar shows, the rows lose its width; give the
       # header the same right margin so its columns stay over theirs.
       vbar = scroll.verticalScrollBar()

       def _match_scrollbar(*_):
           right = vbar.sizeHint().width() if vbar.maximum() > 0 else 0
           hdr.setContentsMargins(0, 0, right, 0)
       vbar.rangeChanged.connect(_match_scrollbar)

       # Bottom toolbar
       bot = QHBoxLayout()
       self._add_btn = QPushButton("＋  Add Gauge")
       self._add_btn.setFixedWidth(120)
       self._add_btn.clicked.connect(self._add_row)
       bot.addWidget(self._add_btn)
       bot.addStretch()
       self._count_lbl = QLabel()
       self._count_lbl.setStyleSheet("color: grey; font-size: 10px;")
       bot.addWidget(self._count_lbl)
       outer.addLayout(bot)

       # Start with one gauge row ready to use
       self._add_row()

   # ── row management ────────────────────────────────────────────────────────

   def _add_row(self):
       if len(self._rows) >= self.MAX_GAUGES:
           return
       row = GaugeRowWidget(
           row_num          = len(self._rows) + 1,
           plot_options     = self._plot_options,
           default_settings = self._default_settings,
           No_chans         = self._No_chans,
           main_window      = self._main_window,
           on_remove        = self._remove_row,
           parent           = self._rows_container,
       )
       self._rows.append(row)
       # Insert before the trailing stretch item
       self._rows_layout.insertWidget(len(self._rows) - 1, row)
       self._update_toolbar()
       self._fit_height()

   def _remove_row(self, row: GaugeRowWidget):
       if row not in self._rows:
           return
       self._rows.remove(row)
       self._rows_layout.removeWidget(row)
       row.deleteLater()
       # Renumber remaining rows
       for i, r in enumerate(self._rows):
           r.set_row_number(i + 1)
       self._update_toolbar()
       self._fit_height()

   def _update_toolbar(self):
       n       = len(self._rows)
       at_max  = n >= self.MAX_GAUGES
       self._add_btn.setEnabled(not at_max)
       self._add_btn.setToolTip(
           f"Maximum of {self.MAX_GAUGES} gauges reached" if at_max else ""
       )
       self._count_lbl.setText(f"{n} / {self.MAX_GAUGES} gauges")

   def _fit_height(self):
       """Resize the dialog to neatly fit the current number of rows."""
       n     = max(1, len(self._rows))
       row_h = 42
       h     = min(120 + n * row_h, 420)
       self.resize(self.width(), h)

   # ── cleanup ───────────────────────────────────────────────────────────────

   def closeEvent(self, event):
       """Close all open gauge windows when the dialog is closed."""
       for row in self._rows:
           row._close_gauge()
       super().closeEvent(event)


# ---------------------------------------------------------------------------
# Apps-bar registration (see DAQ_AppRegistry.py)
# ---------------------------------------------------------------------------

def build(ctx, parent):
   return GaugeAdderDialog(
       main_window=ctx.main_window,
       plot_options=ctx.plot_options,
       default_settings=ctx.input_settings,
       No_chans=ctx.no_chans,
       parent=parent,
   )


APP_SPEC = {
   'id':    'gauge_adder',
   'label': 'Graph & Gauge Adder',
   'cls':   'GaugeAdderDialog',
   'build': build,
   'claims': set(),
}
