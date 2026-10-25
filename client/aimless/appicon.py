"""The AIMless application icon.

The repo carries no binary image assets, and plenty of machines have no SVG
pixbuf loader, so the icon is drawn with Cairo and written out as PNGs. They
go into the standard per-user icon theme directory, which means the window,
the tray and the desktop shell can all find them by name.

Two variants are produced:

``aimless``          the plain running man
``aimless-unread``   the same running man with a badge in the corner

The tray swaps between them, so an unread message is visible without having
to hover for the tooltip.

Everything here degrades quietly: if Cairo or Gdk is missing the app still
starts, it just falls back to the generic theme icons.
"""

import os

ICON_NAME = "aimless"
UNREAD_ICON_NAME = "aimless-unread"

# Sizes rendered into the icon theme. The window asks for 48, the tray for
# 22/24, but the theme picks the closest match for whatever the desktop asks.
SIZES = (16, 22, 24, 32, 48, 64, 128, 256)

# Colours. The blue is the AIM-ish blue of the tile, the figure is the
# classic yellow, and the badge red is the standard "unread" red rather than
# the theme's softer "danger" pink, so it stays legible against the blue.
_BLUE_TOP = (0.29, 0.66, 0.93)
_BLUE_BOTTOM = (0.05, 0.40, 0.72)
_FIGURE = (1.0, 0.85, 0.24)
_BADGE = (0.90, 0.28, 0.24)

_cache = {}


def _imports():
    """Return (cairo, Gdk) or (None, None) if the drawing stack is missing."""
    try:
        import cairo
        import gi
        gi.require_version("Gdk", "3.0")
        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import Gdk
        return cairo, Gdk
    except Exception:
        return None, None


def _png_bytes(pixbuf):
    """Encode a pixbuf as PNG bytes.

    save_to_bufferv returns (ok, buffer); older bindings return the buffer on
    its own. Getting this wrong writes an empty file, and an empty file in
    the theme still satisfies has_icon() but fails to load - which leaves the
    taskbar blank again.
    """
    try:
        result = pixbuf.save_to_bufferv("png", [], [])
    except Exception:
        return None
    data = result[1] if isinstance(result, tuple) else result
    if isinstance(data, (bytes, bytearray)) and data:
        return bytes(data)
    return None


def _rounded_rect(ctx, x, y, w, h, r):
    ctx.new_sub_path()
    ctx.arc(x + w - r, y + r, r, -1.5707963, 0)
    ctx.arc(x + w - r, y + h - r, r, 0, 1.5707963)
    ctx.arc(x + r, y + h - r, r, 1.5707963, 3.1415926)
    ctx.arc(x + r, y + r, r, 3.1415926, 4.7123890)
    ctx.close_path()


def _running_man(ctx, size):
    """The AIM 'running man': a round head, a body leaning forward, arms and
    legs mid-stride.

    Drawn from scratch rather than traced from the AOL artwork - the pose and
    the yellow are what make it read as AIM, the exact shape is ours.
    """
    import cairo

    def p(x, y):
        return x * size, y * size

    limb = size * 0.088

    ctx.set_line_width(limb)
    ctx.set_line_cap(cairo.LINE_CAP_ROUND)
    ctx.set_line_join(cairo.LINE_JOIN_ROUND)

    # Back leg, extended behind.
    ctx.move_to(*p(0.50, 0.62))
    ctx.curve_to(*p(0.44, 0.74), *p(0.36, 0.84), *p(0.24, 0.88))
    ctx.stroke()

    # Back arm, swung back.
    ctx.move_to(*p(0.44, 0.47))
    ctx.curve_to(*p(0.34, 0.50), *p(0.27, 0.56), *p(0.22, 0.63))
    ctx.stroke()

    # Torso, leaning into the run.
    ctx.move_to(*p(0.43, 0.42))
    ctx.curve_to(*p(0.47, 0.49), *p(0.50, 0.55), *p(0.50, 0.63))
    ctx.stroke()

    # Front leg, driving forward and up.
    ctx.move_to(*p(0.50, 0.62))
    ctx.curve_to(*p(0.60, 0.66), *p(0.70, 0.68), *p(0.76, 0.62))
    ctx.stroke()

    # Front arm, punched forward.
    ctx.move_to(*p(0.45, 0.46))
    ctx.curve_to(*p(0.54, 0.42), *p(0.62, 0.38), *p(0.68, 0.31))
    ctx.stroke()

    # Head - deliberately big, the way the classic mascot had it.
    ctx.new_sub_path()
    ctx.arc(*p(0.35, 0.27), size * 0.150, 0, 6.2831853)
    ctx.fill()


def render(size=48, unread=False):
    """Draw the icon and return a GdkPixbuf (or None if the stack is missing)."""
    cairo, Gdk = _imports()
    if cairo is None:
        return None
    key = (size, unread)
    if key in _cache:
        return _cache[key]

    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    ctx = cairo.Context(surface)

    # Rounded blue tile.
    grad = cairo.LinearGradient(0, 0, 0, size)
    grad.add_color_stop_rgb(0, *_BLUE_TOP)
    grad.add_color_stop_rgb(1, *_BLUE_BOTTOM)
    _rounded_rect(ctx, 0, 0, size, size, size * 0.22)
    ctx.set_source(grad)
    ctx.fill()

    # The running man. Scaled inside the tile so it keeps a blue margin, and
    # nudged left so the top-right corner stays free for the unread badge.
    ctx.set_source_rgb(*_FIGURE)
    ctx.save()
    ctx.translate(size * 0.04, size * 0.05)
    ctx.scale(0.92, 0.92)
    _running_man(ctx, size)
    ctx.restore()

    if unread:
        # Red badge, ringed in the tile colour so it reads on any backdrop.
        r = size * 0.20
        cx, cy = size * 0.82, size * 0.19
        ctx.set_source_rgb(*_BLUE_BOTTOM)
        ctx.arc(cx, cy, r + size * 0.05, 0, 6.2831853)
        ctx.fill()
        ctx.set_source_rgb(*_BADGE)
        ctx.arc(cx, cy, r, 0, 6.2831853)
        ctx.fill()

    pixbuf = Gdk.pixbuf_get_from_surface(surface, 0, 0, size, size)
    _cache[key] = pixbuf
    return pixbuf


def icon_name(unread=False):
    """The icon-theme name for the current state."""
    return UNREAD_ICON_NAME if unread else ICON_NAME


def _xdg_icon_root():
    data_home = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(data_home, "icons", "hicolor")


def theme_dirs():
    """Directories to render the icons into.

    The XDG location is the one that matters - it is what the desktop shell
    and the window manager search. The copy inside the app root is a
    fallback for a read-only or unusual HOME.
    """
    dirs = [_xdg_icon_root()]
    try:
        from . import paths
        dirs.append(os.path.join(paths.client_dir(), "icons", "hicolor"))
    except Exception:
        pass
    return dirs


def install():
    """Write the PNGs into the icon theme and register the directories.

    Returns True if the icons can be looked up by name afterwards. Files are
    only rewritten when their bytes actually change, so a normal start does
    no disk writes.
    """
    cairo, Gdk = _imports()
    if cairo is None:
        return False

    for root in theme_dirs():
        for size in SIZES:
            for unread, name in ((False, ICON_NAME), (True, UNREAD_ICON_NAME)):
                pixbuf = render(size, unread)
                if pixbuf is None:
                    continue
                path = os.path.join(root, f"{size}x{size}", "apps", f"{name}.png")
                try:
                    data = _png_bytes(pixbuf)
                    if data is None:
                        continue
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    old = None
                    if os.path.exists(path):
                        with open(path, "rb") as f:
                            old = f.read()
                    if old != data:
                        with open(path, "wb") as f:
                            f.write(data)
                except Exception:
                    continue

    register()
    return True


def register():
    """Add the icon directories to the default GTK icon theme."""
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk
        theme = Gtk.IconTheme.get_default()
        for root in theme_dirs():
            path = os.path.join(root, "")
            try:
                if path not in theme.get_search_path():
                    theme.append_search_path(path)
            except Exception:
                continue
        try:
            # The PNGs were written a moment ago; the theme caches what it
            # saw when it was first scanned, so ask it to look again.
            theme.rescan_if_needed()
        except Exception:
            pass
        return True
    except Exception:
        return False


def has_icon(name=None):
    """True if the default icon theme can resolve the name and load it.

    has_icon() only checks that a file exists, so a truncated or empty file
    would pass and then blow up at draw time. Load it to be sure.
    """
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk
        theme = Gtk.IconTheme.get_default()
        target = name or ICON_NAME
        if not theme.has_icon(target):
            return False
        theme.load_icon(target, 48, 0)
        return True
    except Exception:
        return False
