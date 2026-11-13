"""Theme palettes and CSS for the AIMless GTK UI.

Extracted from gtkui.py: the palette data (THEMES/THEME_ENTRIES), palette
resolution (get_theme/detect_system_palette) and the CSS builder (build_css).
The active-palette global (C) and the CSS-provider lifecycle stay in gtkui.
"""
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk


THEMES = {
    "dark": {
        "name": "Aimless Dark",
        "bg": "#191b22", "header": "#14161d", "surface": "#1e212b",
        "sidebar": "#21242e", "sidebar_hover": "#262a35", "sidebar_selected": "#323748",
        "button": "#262a35", "button_hover": "#2e3240", "button_active": "#33363f",
        "border": "#3a3e4a", "border_dark": "#0d0e13", "sep": "#2a2d37",
        "text": "#e8eaf0", "text2": "#dfe3ec", "text_bright": "#f2f4f8",
        "muted": "#9aa0ad", "muted2": "#aab0bd", "subtitle": "#8c8c8c",
        "bubble_in": "#31343d", "bubble_in_fg": "#e8eaf0",
        "bubble_out": "#8ab4f8", "bubble_out_fg": "#10131a", "out_link": "#0b3d91",
        "badge_bg": "#7fa8f0", "badge_fg": "#10131a", "focus": "#4a5060",
        "log_text": "#c8cdd8",
        "away_bg": "#3a3020", "away_border": "#5a4a28", "away_text": "#f5d78e",
        "away_icon": "#f5d36b", "chip": "#22252e", "chip_hover": "#2c313d",
        "online": "#a6e3a1", "away": "#fab387", "offline": "#6c7086",
        "danger": "#f38ba8", "link": "#8ab4f8",
    },
    "mocha": {
        "name": "Mocha", "mono": False,
        "bg": "#1e1e2e", "header": "#11111b", "surface": "#181825",
        "sidebar": "#181825", "sidebar_hover": "#1e1e2e", "sidebar_selected": "#313244",
        "button": "#313244", "button_hover": "#45475a", "button_active": "#45475a",
        "border": "#45475a", "border_dark": "#11111b", "sep": "#313244",
        "text": "#cdd6f4", "text2": "#cdd6f4", "text_bright": "#ffffff",
        "muted": "#7f849c", "muted2": "#6c7086", "subtitle": "#6c7086",
        "bubble_in": "#313244", "bubble_in_fg": "#cdd6f4",
        "bubble_out": "#89b4fa", "bubble_out_fg": "#1e1e2e", "out_link": "#11111b",
        "badge_bg": "#89b4fa", "badge_fg": "#11111b", "focus": "#6c7086",
        "log_text": "#cdd6f4",
        "away_bg": "#313244", "away_border": "#fab387", "away_text": "#f9e2af",
        "away_icon": "#f9e2af", "chip": "#313244", "chip_hover": "#45475a",
        "online": "#a6e3a1", "away": "#fab387", "offline": "#6c7086",
        "danger": "#f38ba8", "link": "#89b4fa",
    },
    "nord": {
        "name": "Nord", "mono": False,
        "bg": "#2e3440", "header": "#242933", "surface": "#2e3440",
        "sidebar": "#2e3440", "sidebar_hover": "#3b4252", "sidebar_selected": "#434c5e",
        "button": "#3b4252", "button_hover": "#434c5e", "button_active": "#4c566a",
        "border": "#4c566a", "border_dark": "#1b1f27", "sep": "#434c5e",
        "text": "#eceff4", "text2": "#e5e9f0", "text_bright": "#ffffff",
        "muted": "#7b8ea1", "muted2": "#9caabb", "subtitle": "#4c566a",
        "bubble_in": "#3b4252", "bubble_in_fg": "#eceff4",
        "bubble_out": "#88c0d0", "bubble_out_fg": "#2e3440", "out_link": "#2e3440",
        "badge_bg": "#88c0d0", "badge_fg": "#2e3440", "focus": "#4c566a",
        "log_text": "#d8dee9",
        "away_bg": "#3b4252", "away_border": "#d08770", "away_text": "#ebcb8b",
        "away_icon": "#ebcb8b", "chip": "#3b4252", "chip_hover": "#434c5e",
        "online": "#a3be8c", "away": "#ebcb8b", "offline": "#4c566a",
        "danger": "#bf616a", "link": "#88c0d0",
    },
    "tokyo": {
        "name": "Tokyo Night", "mono": False,
        "bg": "#1a1b26", "header": "#16161e", "surface": "#1f2335",
        "sidebar": "#1a1b26", "sidebar_hover": "#24283b", "sidebar_selected": "#292e42",
        "button": "#24283b", "button_hover": "#292e42", "button_active": "#3b4261",
        "border": "#3b4261", "border_dark": "#0f0f14", "sep": "#292e42",
        "text": "#c0caf5", "text2": "#c0caf5", "text_bright": "#ffffff",
        "muted": "#565f89", "muted2": "#737aa2", "subtitle": "#565f89",
        "bubble_in": "#24283b", "bubble_in_fg": "#c0caf5",
        "bubble_out": "#7aa2f7", "bubble_out_fg": "#1a1b26", "out_link": "#1a1b26",
        "badge_bg": "#7aa2f7", "badge_fg": "#1a1b26", "focus": "#565f89",
        "log_text": "#a9b1d6",
        "away_bg": "#24283b", "away_border": "#e0af68", "away_text": "#e0af68",
        "away_icon": "#e0af68", "chip": "#24283b", "chip_hover": "#292e42",
        "online": "#9ece6a", "away": "#e0af68", "offline": "#414868",
        "danger": "#f7768e", "link": "#7aa2f7",
    },
    "latte": {
        "name": "Latte (light)", "mono": False,
        "bg": "#eff1f5", "header": "#e6e9ef", "surface": "#ffffff",
        "sidebar": "#e6e9ef", "sidebar_hover": "#dce0e8", "sidebar_selected": "#ccd0da",
        "button": "#ccd0da", "button_hover": "#bcc0cc", "button_active": "#acb0be",
        "border": "#bcc0cc", "border_dark": "#dce0e8", "sep": "#ccd0da",
        "text": "#4c4f69", "text2": "#4c4f69", "text_bright": "#11111b",
        "muted": "#7c7f93", "muted2": "#9ca0b0", "subtitle": "#8c8fa1",
        "bubble_in": "#e6e9ef", "bubble_in_fg": "#4c4f69",
        "bubble_out": "#1e66f5", "bubble_out_fg": "#eff1f5", "out_link": "#eff1f5",
        "badge_bg": "#1e66f5", "badge_fg": "#eff1f5", "focus": "#9ca0b0",
        "log_text": "#4c4f69",
        "away_bg": "#f2e9de", "away_border": "#df8e1d", "away_text": "#df8e1d",
        "away_icon": "#df8e1d", "chip": "#e6e9ef", "chip_hover": "#dce0e8",
        "online": "#40a02b", "away": "#df8e1d", "offline": "#9ca0b0",
        "danger": "#d20f39", "link": "#1e66f5",
    },
    "dawn": {
        "name": "Dawn (light)", "mono": False,
        "bg": "#faf4ed", "header": "#f2e9de", "surface": "#fffaf3",
        "sidebar": "#f2e9de", "sidebar_hover": "#e4dfde", "sidebar_selected": "#dfdad9",
        "button": "#e4dfde", "button_hover": "#dfdad9", "button_active": "#cecacd",
        "border": "#cecacd", "border_dark": "#e4dfde", "sep": "#dfdad9",
        "text": "#575279", "text2": "#575279", "text_bright": "#26233a",
        "muted": "#9893a5", "muted2": "#797593", "subtitle": "#9893a5",
        "bubble_in": "#f2e9de", "bubble_in_fg": "#575279",
        "bubble_out": "#907aa9", "bubble_out_fg": "#faf4ed", "out_link": "#faf4ed",
        "badge_bg": "#907aa9", "badge_fg": "#faf4ed", "focus": "#9893a5",
        "log_text": "#575279",
        "away_bg": "#f4ede8", "away_border": "#ea9d34", "away_text": "#ea9d34",
        "away_icon": "#ea9d34", "chip": "#f2e9de", "chip_hover": "#e4dfde",
        "online": "#56949f", "away": "#ea9d34", "offline": "#9893a5",
        "danger": "#b4637a", "link": "#907aa9",
    },
    "matrix": {
        "name": "Matrix", "mono": True,
        "bg": "#000000", "header": "#001100", "surface": "#001a0d",
        "sidebar": "#001100", "sidebar_hover": "#032006", "sidebar_selected": "#063a0d",
        "button": "#03310f", "button_hover": "#064a15", "button_active": "#0a5a1e",
        "border": "#0a5a1e", "border_dark": "#001100", "sep": "#03310f",
        "text": "#b8ffb8", "text2": "#b8ffb8", "text_bright": "#e8ffe8",
        "muted": "#0a8a2a", "muted2": "#0a8a2a", "subtitle": "#0a8a2a",
        "bubble_in": "#071f0d", "bubble_in_fg": "#b8ffb8",
        "bubble_out": "#00ff41", "bubble_out_fg": "#001100", "out_link": "#00260a",
        "badge_bg": "#00ff41", "badge_fg": "#001100", "focus": "#0a8a2a",
        "log_text": "#00c22e",
        "away_bg": "#1a1200", "away_border": "#ffb000", "away_text": "#ffb000",
        "away_icon": "#ffb000", "chip": "#03310f", "chip_hover": "#064a15",
        "online": "#00ff41", "away": "#ffb000", "offline": "#1f5a1f",
        "danger": "#ff2a2a", "link": "#39ff88",
    },
    "amber": {
        "name": "Amber CRT", "mono": True,
        "bg": "#0a0700", "header": "#120d00", "surface": "#1a1200",
        "sidebar": "#120d00", "sidebar_hover": "#241a00", "sidebar_selected": "#332500",
        "button": "#241a00", "button_hover": "#332500", "button_active": "#4a3600",
        "border": "#5a4200", "border_dark": "#120d00", "sep": "#332500",
        "text": "#ffc95c", "text2": "#ffc95c", "text_bright": "#ffe9b0",
        "muted": "#a8791f", "muted2": "#a8791f", "subtitle": "#a8791f",
        "bubble_in": "#241a00", "bubble_in_fg": "#ffc95c",
        "bubble_out": "#ffb000", "bubble_out_fg": "#1a1200", "out_link": "#4a3000",
        "badge_bg": "#ffb000", "badge_fg": "#1a1200", "focus": "#a8791f",
        "log_text": "#d9a23a",
        "away_bg": "#241a00", "away_border": "#ffb000", "away_text": "#ffe9b0",
        "away_icon": "#ffb000", "chip": "#241a00", "chip_hover": "#332500",
        "online": "#ffb000", "away": "#ff8c00", "offline": "#5a4200",
        "danger": "#ff5544", "link": "#ffd27f",
    },
    "dracula": {
        "name": "Dracula", "mono": False,
        "bg": "#282a36", "header": "#21222c", "surface": "#343746",
        "sidebar": "#21222c", "sidebar_hover": "#343746", "sidebar_selected": "#44475a",
        "button": "#44475a", "button_hover": "#4d5066", "button_active": "#565a75",
        "border": "#44475a", "border_dark": "#191a21", "sep": "#343746",
        "text": "#f8f8f2", "text2": "#f8f8f2", "text_bright": "#ffffff",
        "muted": "#6272a4", "muted2": "#8892bf", "subtitle": "#6272a4",
        "bubble_in": "#44475a", "bubble_in_fg": "#f8f8f2",
        "bubble_out": "#bd93f9", "bubble_out_fg": "#282a36", "out_link": "#191a21",
        "badge_bg": "#bd93f9", "badge_fg": "#282a36", "focus": "#6272a4",
        "log_text": "#f8f8f2",
        "away_bg": "#44475a", "away_border": "#ffb86c", "away_text": "#ffb86c",
        "away_icon": "#ffb86c", "chip": "#44475a", "chip_hover": "#4d5066",
        "online": "#50fa7b", "away": "#ffb86c", "offline": "#6272a4",
        "danger": "#ff5555", "link": "#8be9fd",
    },
}

THEME_ENTRIES = [
    ("system", "System", "Match the desktop"),
    ("dark", "Aimless Dark", "Default"),
    ("mocha", "Mocha", "Catppuccin · dark"),
    ("nord", "Nord", "Arctic · dark"),
    ("tokyo", "Tokyo Night", "Deep blue · dark"),
    ("latte", "Latte", "Catppuccin · light"),
    ("dawn", "Dawn", "Rosé Pine · light"),
    ("matrix", "Matrix", "Hacker green · monospace"),
    ("amber", "Amber CRT", "Phosphor amber · monospace"),
    ("dracula", "Dracula", "Vampire dark · purple"),
]


def detect_system_palette():
    """Desktop dark/light -> the palette 'System' resolves to."""
    try:
        s = Gtk.Settings.get_default()
        name = (s.get_property("gtk-theme-name") or "").lower()
        dark = bool(s.get_property("gtk-application-prefer-dark-theme")) or any(
            k in name for k in ("dark", "black", "night", "midnight"))
        return "dark" if dark else "latte"
    except Exception:
        return "dark"


def get_theme(name):
    if name == "system" or name not in THEMES:
        name = detect_system_palette()
    return THEMES[name]


def build_css(C):
    mono = "font-family: monospace;" if C.get("mono") else ""
    return f"""
* {{ {mono} }}
headerbar {{
    background-image: none;
    background-color: {C['header']};
    color: {C['text']};
    border-bottom: 1px solid {C['border_dark']};
    min-height: 40px;
}}

.aimless-window {{ background-image: none; background-color: {C['bg']}; color: {C['text']}; }}
.aimless-window label {{ color: {C['text']}; }}
.aimless-window .muted {{ color: {C['muted']}; }}

.aimless-window button {{
    background-image: none;
    background-color: {C['button']};
    color: {C['text2']};
    border: 1px solid {C['border']};
    border-radius: 8px;
}}
.aimless-window button:hover {{ background-color: {C['button_hover']}; }}
.aimless-window button:active {{ background-color: {C['button_active']}; }}
.aimless-window button:checked {{ background-color: {C['button_active']}; }}
.aimless-window button:disabled {{ opacity: 0.5; }}

.aimless-send {{ padding: 10px 20px; }}

stackswitcher {{ background-color: {C['surface']}; border-radius: 8px; }}
stackswitcher > button {{
    background-image: none; background-color: transparent; border: none; box-shadow: none;
    color: {C['muted2']}; padding: 5px 14px; margin: 2px; border-radius: 6px; outline: none;
}}
stackswitcher > button:checked {{ background-color: {C['button_active']}; color: {C['text_bright']}; }}

menu {{ background-color: {C['surface']}; color: {C['text2']}; border: 1px solid {C['border']}; border-radius: 6px; }}
menuitem {{ color: {C['text2']}; }}
menuitem:hover {{ background-color: {C['button_active']}; }}

.muted {{ color: {C['muted']}; font-size: 90%; }}

.aimless-sidebar scrolledwindow,
.aimless-sidebar list,
.aimless-sidebar row {{ background-color: {C['sidebar']}; }}
.aimless-sidebar row:hover {{ background-color: {C['sidebar_hover']}; }}
.aimless-sidebar row:selected {{ background-color: {C['sidebar_selected']}; }}
.aimless-sidebar row label {{ color: {C['text2']}; }}

.aimless-chat row label {{ color: {C['text']}; }}
.aimless-chat,
.aimless-chat stack,
.aimless-chat scrolledwindow,
.aimless-chat list,
.aimless-chat row {{ background-color: {C['bg']}; }}
.aimless-chat separator {{ background-color: {C['sep']}; min-height: 1px; }}

.aimless-bubble {{ padding: 8px 12px; border-radius: 14px; }}
.aimless-bubble-in {{ background-color: {C['bubble_in']}; }}
.aimless-bubble-out {{ background-color: {C['bubble_out']}; }}
.aimless-chat row .aimless-bubble-in {{ color: {C['bubble_in_fg']}; }}
.aimless-chat row .aimless-bubble-out {{ color: {C['bubble_out_fg']}; }}
.aimless-bubble-out link, .aimless-bubble-out link:visited {{ color: {C['out_link']}; }}
.aimless-bubble-in link, .aimless-bubble-in link:visited {{ color: {C['link']}; }}

.aimless-badge {{
    background-color: {C['badge_bg']}; color: {C['badge_fg']};
    border-radius: 10px; padding: 0 8px; font-size: 85%;
}}

.aimless-composer-frame {{ background-color: {C['surface']}; border: 1px solid {C['border']}; border-radius: 6px; }}
.aimless-composer-frame textview,
.aimless-composer-frame textview text {{ background-color: transparent; color: {C['text']}; caret-color: {C['text']}; }}

.aimless-window entry {{
    background-color: {C['surface']}; color: {C['text']};
    border: 1px solid {C['border']}; border-radius: 6px; padding: 6px 10px;
}}
.aimless-window entry:focus {{ border-color: {C['focus']}; }}

.aimless-log text,
.aimless-log textview,
.aimless-log textview text {{ background-color: {C['header']}; color: {C['log_text']}; }}

.aimless-away-banner {{
    background-color: mix({C['away_border']}, {C['bg']}, 0.45);
    border-top: 1px solid {C['away_border']};
    border-bottom: 1px solid {C['away_border']};
    color: {C['text']};
}}
.aimless-away-banner image {{ color: {C['away_icon']}; }}
.aimless-away-banner button {{
    background-image: none;
    background-color: transparent;
    border: 1px solid {C['away_border']};
    color: {C['text']};
}}
.aimless-away-banner button:hover {{ background-color: alpha({C['away_border']}, 0.25); }}

.aimless-route-bar {{ background-color: {C['header']}; border-top: 1px solid {C['sep']}; color: {C['muted2']}; }}
.aimless-route-bar image {{ color: {C['muted2']}; }}

.aimless-onboarding {{
    background-color: {C['surface']};
    border-bottom: 1px solid {C['border']};
}}
.aimless-onboarding label {{ color: {C['text']}; }}

.aimless-contacts frame {{
    background-color: {C['surface']};
    border: 1px solid {C['border']};
    border-radius: 6px;
}}
.aimless-contacts frame > label {{
    color: {C['muted2']};
    background-color: {C['bg']};
    padding: 0 4px;
}}
.aimless-contacts scrolledwindow,
.aimless-contacts list,
.aimless-contacts row {{ background-color: {C['surface']}; }}
.aimless-contacts row:hover {{ background-color: {C['button_hover']}; }}
.aimless-contacts row label {{ color: {C['text2']}; }}
.aimless-muted {{ opacity: 0.55; }}

.aimless-chip {{ padding: 2px 8px; margin: 1px; border-radius: 11px; background-color: {C['chip']}; }}
.aimless-chip:hover {{ background-color: {C['chip_hover']}; }}

.aimless-jump {{
    background-color: {C['badge_bg']}; color: {C['badge_fg']};
    border-radius: 14px; padding: 4px 12px;
}}
"""
