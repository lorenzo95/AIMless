"""The AIMless application icon.

The repo carries no binary image assets, and plenty of machines have no SVG
pixbuf loader, so the icon is drawn with Cairo and written out as PNGs. They
go into the standard per-user icon theme directory, which means the window,
the tray and the desktop shell can all find them by name.

Two variants are produced:

``aimless``          the plain heart
``aimless-unread``   the same heart with a badge in the corner

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

# Colours. The blue is the app's own accent (theme "bubble_out"/"link"), the
# badge red is the standard "unread" red rather than the theme's softer
# "danger" pink, so it stays legible against the blue.
_BLUE_TOP = (0.62, 0.77, 1.00)
_BLUE_BOTTOM = (0.30, 0.52, 0.91)
_HEART = (1.0, 1.0, 1.0)
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


def _heart(ctx, size):
    """A heart in the middle of a size x size box, as a normalised path."""
    def p(x, y):
        return x * size, y * size

    ctx.move_to(*p(0.50, 0.88))
    ctx.curve_to(*p(0.50, 0.88), *p(0.06, 0.54), *p(0.06, 0.30))
    ctx.curve_to(*p(0.06, 0.12), *p(0.19, 0.03), *p(0.31, 0.03))
    ctx.curve_to(*p(0.41, 0.03), *p(0.47, 0.10), *p(0.50, 0.17))
    ctx.curve_to(*p(0.53, 0.10), *p(0.59, 0.03), *p(0.69, 0.03))
    ctx.curve_to(*p(0.81, 0.03), *p(0.94, 0.12), *p(0.94, 0.30))
    ctx.curve_to(*p(0.94, 0.54), *p(0.50, 0.88), *p(0.50, 0.88))
    ctx.close_path()


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

    # White heart. Scaled down inside the tile so it keeps a blue margin and
    # still reads as a heart at 16px, and nudged down to leave the top-right
    # corner free for the unread badge.
    ctx.set_source_rgb(*_HEART)
    ctx.save()
    ctx.translate(size * 0.10, size * 0.12)
    ctx.scale(0.80, 0.80)
    _heart(ctx, size)
    ctx.fill()
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
