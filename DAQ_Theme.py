
"""
DAQ_Theme.py
------------
Plot appearance themes for GioDAQ.

Chosen in Settings → Expert Settings → Appearance. Applies on restart, matching
how the other expert settings are read (once, into module globals at import).

Scope, deliberately: the "Graph Data" panel — the group box, the plot canvas,
its axes, the title, the legend and the curve palette. The window chrome —
buttons, panels, dialogs — is untouched and stays in the system light style.
This is not a "dark mode"; it is the benchtop-instrument look, where a dark
measurement panel sits inside normal light chrome so traces read clearly
without the whole application going dark.

The Graph Data group box itself — its frame and caption — is chrome and stays on
the application palette. A theme colours the plot widget inside it: the axis
margin, the canvas, and the text drawn on them, which is specified per theme so
the graph title, axis numbers and legend stay legible on whichever background
that theme picks.

Adding a theme means one entry in THEMES. Every name in THEMES appears in the
Settings dropdown automatically.

Why the curve palette is per-theme and not shared
-------------------------------------------------
It is not cosmetic. The light palette contains 'black', which is invisible on a
dark canvas, and 'brown'/'purple' go muddy against it. A theme that changed only
the background would silently lose channels off the graph.
"""

DEFAULT_THEME = "Light"


THEMES = {
    # The existing look: white throughout, dark ink.
    "Light": {
        "label":       "Light",
        "description": "White plot canvas — the original appearance.",
        # Background of the whole plot widget, including the margin where the
        # axis numbers are drawn.
        "widget":      "w",
        # Interior of the plotting area only. None = same as `widget`.
        "canvas":      None,
        # "axis" is the pen: axis line, ticks AND grid lines, because pyqtgraph
        # draws the grid with the axis pen. "axis_text" is separate and colours
        # only the numbers and the unit label.
        "axis":        "#000000",
        "axis_text":   "#000000",
        "grid_alpha":  0.3,
        # Graph title, drawn on the plot widget's top margin. Stated explicitly
        # in every theme because pyqtgraph's own default is a light grey chosen
        # for its dark default background, which is invisible on white.
        "title":       "#000000",
        # Legend text. It is drawn on the canvas, so it follows the canvas.
        # Set rather than left to pyqtgraph, whose default is a mid grey
        # (#969696) that is faint on white — and invisible on black, which is
        # what made the legend disappear on the Instrument theme.
        "legend_text": "#101010",
        # Fill behind the legend labels. None = transparent, i.e. the canvas
        # shows through, which is fine on white.
        "legend_bg":     None,
        "curves": ['red', 'green', 'blue', 'black',
                   'magenta', 'purple', 'orange', 'brown'],
        # Log panel. None = leave at the system default.
        "log_bg":      None,
        "log_fg":      None,
        # Colour overrides for the application palette. None = do not touch the
        # palette at all, so the chrome is exactly what the style would draw.
        "palette":     None,
    },

    # Light chrome, black measurement panel: the benchtop-scope look.
    #
    # The plot widget is black throughout — axis margin and canvas alike — so it
    # reads as one instrument face set into the light window rather than a black
    # rectangle with a grey mat around it. The axis numbers therefore sit on black
    # and are light. `widget` and `canvas` stay separate keys because another
    # theme may want the margin and the canvas to differ.
    "Instrument": {
        "label":       "Instrument",
        "description": "Black measurement canvas with high-contrast traces, in "
                       "the style of a benchtop scope. Window chrome stays light.",
        "widget":      "#000000",
        "canvas":      "#000000",
        # Pen and text have to differ here. The pen also draws the grid, and a
        # near-black pen on a black canvas rendered zero visible grid pixels.
        # A mid grey at 50% gives roughly #464646 lines — dark grey, clearly
        # present without competing with the traces.
        "axis":        "#8c8c8c",
        # The numbers sit on the black margin now, not a grey one, so they are
        # light. Kept off pure white: at 9pt a full-white number set is louder
        # than the traces it is there to label.
        "axis_text":   "#e8e8e8",
        "grid_alpha":  0.5,
        "title":       "#f0f0f0",
        # The legend sits inside the canvas, over the traces. Light text alone
        # is not enough — a label with a curve running behind it disappears — so
        # it gets a near-opaque black fill behind it. No outline: the fill is
        # there to back the text, not to draw a box on the canvas.
        "legend_text":   "#f5f5f5",
        "legend_bg":     "#000000cc",
        # No black and no brown — see the module docstring.
        "curves": ['#ff2d2d',   # red
                   '#00e64d',   # green
                   '#3d9dff',   # blue
                   '#ffd400',   # amber
                   '#ff5ecb',   # pink
                   '#00e5e5',   # cyan
                   '#ff8c1a',   # orange
                   '#b478ff'],  # violet
        # The Log reads as a second instrument readout, so it matches the canvas.
        "log_bg":      "#000000",
        "log_fg":      "#e8e8e8",

        # Chrome is recoloured through the QPalette, NOT a stylesheet.
        #
        # A stylesheet was tried first and was the wrong tool twice over. Any QSS
        # rule matching a QCheckBox switches that widget off native drawing, and
        # the ::indicator sub-control then has to be redrawn by hand or it simply
        # is not painted — which is what made the channel checkboxes vanish. It
        # also replaced the style's own control shapes with flat square ones, so
        # the whole application took on a dated look nobody asked for.
        #
        # A palette only supplies colours. The Fusion style keeps drawing its own
        # controls, indicators included, so this shifts the greys without
        # changing how anything is rendered.
        "palette": {
            
            "Window":          "#d6d6d6",   # panel background
            "WindowText":      "#101010",
            
            # Off-white, not pure white: checkbox indicators and entry fields
            # (the graph-title box among them) use this, and pure white was the
            # brightest thing on screen.
            
            "Base":            "#e6e6e6",              
            "AlternateBase":   "#dedede",
            "Text":            "#101010",
                        
            "Button":          "#d6d6d6",
            "ButtonText":      "#101010",
            "ToolTipBase":     "#ffffe1",
            "ToolTipText":     "#101010",
            "Highlight":       "#2c6ba8",
            "HighlightedText": "#ffffff",
            
            # Greyed-out controls, so disabled still reads as disabled.
            "Disabled:WindowText": "#8f8f8f",
            "Disabled:Text":       "#8f8f8f",
            "Disabled:ButtonText": "#8f8f8f",
        },
    },
}


def title_color(name):
    """Colour for the graph title, falling back to the axis text colour."""
    t = get_theme(name)
    return t.get("title") or t.get("axis_text") or t.get("axis")


def legend_bg(name):
    """Fill behind the legend labels, or None for a transparent legend."""
    return get_theme(name).get("legend_bg")


def log_colors(name):
    """(background, foreground) for the Log panel; either may be None."""
    t = get_theme(name)
    return t.get("log_bg"), t.get("log_fg")


def qt_palette(name, base_palette):
    """Return a QPalette with this theme's colours applied, or None.

    None means "leave the application palette alone", which is how a theme opts
    out of touching the chrome entirely.

    `base_palette` is the style's current palette, copied and then overridden, so
    any role a theme does not mention keeps the style's own value.
    """
    spec = get_theme(name).get("palette")
    if not spec:
        return None

    from PyQt5.QtGui import QPalette, QColor

    pal = QPalette(base_palette)
    for key, value in spec.items():
        group = QPalette.All
        role_name = key
        if ":" in key:                       # e.g. "Disabled:Text"
            group_name, role_name = key.split(":", 1)
            group = getattr(QPalette, group_name, QPalette.All)
        role = getattr(QPalette, role_name, None)
        if role is None:
            print(f"Theme {name}: unknown palette role {role_name!r}; ignored.")
            continue
        pal.setColor(group, role, QColor(value))
    return pal


def widget_bg(name):
    return get_theme(name)["widget"]


def canvas_bg(name):
    """Interior colour for the plotting area, or None to match the widget."""
    return get_theme(name)["canvas"]


def axis_color(name):
    return get_theme(name)["axis"]


def grid_alpha(name):
    return get_theme(name)["grid_alpha"]


def theme_names():
    """Names for the Settings dropdown, with the default first."""
    names = [DEFAULT_THEME]
    names += [n for n in THEMES if n != DEFAULT_THEME]
    return names


def get_theme(name):
    """Return a theme dict, falling back to the default.

    Forgiving on purpose: the value arrives from a spreadsheet cell a user can
    edit by hand, and an unrecognised theme should not stop the program.
    """
    if name is None:
        return THEMES[DEFAULT_THEME]
    key = str(name).strip()
    if key in THEMES:
        return THEMES[key]
    for n in THEMES:                      # case-insensitive second chance
        if n.lower() == key.lower():
            return THEMES[n]
    if key:
        print(f"Unknown theme {key!r}; using {DEFAULT_THEME}.")
    return THEMES[DEFAULT_THEME]


def curve_colors(name):
    return list(get_theme(name)["curves"])
