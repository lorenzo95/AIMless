"""Shared UI layer for the AIMless client: constants, theme state, path
wrappers, small GTK helpers, presence/markup formatting and run_async.

Extracted from gtkui.py so the view modules can use it without importing gtkui
(which would be circular). Depends only on lower-level aimless modules.

The palette dict C is mutated in place by set_theme, so a module may hold a
reference to it and still see theme switches. CURRENT_THEME is rebinding, so
read it as uibase.CURRENT_THEME (or gtkui.CURRENT_THEME, which forwards).
"""
import os
import re
import shutil
import sys
import threading
from datetime import datetime, timedelta

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib, Gdk

from . import appicon, paths, protocol, tunnel
from .daemon import presence_state
from .relay import is_tailcat
from .theme import THEMES, get_theme, build_css


APP_NAME = "AIMless"
STATUS_REASSERT_SECONDS = 60
# How often the locked app asks the daemon how many messages are waiting.
PENDING_CHECK_SECONDS = 15


def prefs_file():
    return paths.prefs_path()


def attachments_dir():
    return paths.attachments_dir()


def sock_path():
    return tunnel.client_socket()


def contacts_path():
    return paths.contacts_path()


def identity_path():
    return paths.identity_path()


def cache_path():
    return paths.cache_path()


_ENV_THEME = os.environ.get("AIMLESS_THEME")
CURRENT_THEME = _ENV_THEME if (_ENV_THEME in THEMES or _ENV_THEME == "system") else "system"
# Own dict, not an alias: get_theme returns the live THEMES entry, and set_theme
# mutates C in place - aliasing would corrupt the theme registry.
C = dict(get_theme(CURRENT_THEME))
CSS_PROVIDER = None


def install_css_provider():
    """Create the single app CSS provider and load the current palette."""
    global CSS_PROVIDER
    CSS_PROVIDER = Gtk.CssProvider()
    CSS_PROVIDER.load_from_data(build_css(C).encode())
    Gtk.StyleContext.add_provider_for_screen(
        Gdk.Screen.get_default(), CSS_PROVIDER, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


def set_theme(name):
    """Swap the active palette and reload the CSS in place (no re-add needed)."""
    global CURRENT_THEME
    CURRENT_THEME = name
    # Mutate in place: other modules hold a reference to this dict, so rebinding
    # it would leave them painting with the previous palette.
    new = get_theme(name)
    C.clear()
    C.update(new)
    if CSS_PROVIDER is not None:
        CSS_PROVIDER.load_from_data(build_css(C).encode())
    return C


def format_time(ts, time_format="24h"):
    dt = datetime.fromtimestamp(ts / 1000)
    if time_format == "12h":
        return dt.strftime("%I:%M %p").lstrip("0")
    return dt.strftime("%H:%M")


def date_label(ts, now=None):
    """Divider label for a message timestamp: Today / Yesterday / date."""
    day = datetime.fromtimestamp(ts / 1000).date()
    today = (now or datetime.now()).date()
    if day == today:
        return "Today"
    if day == today - timedelta(days=1):
        return "Yesterday"
    if day.year == today.year:
        return day.strftime("%A, %d %b")
    return day.strftime("%d %b %Y")


def clamp_to_workarea(x, y, w, h, areas, margin=40):
    """Nudge a saved top-left so some of the window is visible.

    ``areas`` is an iterable of monitor workareas (ax, ay, aw, ah). If the point
    already lands on a monitor it is kept; otherwise it is clamped into the
    first (primary) workarea so a window closed on a since-removed monitor
    reopens on-screen."""
    areas = list(areas)
    if not areas:
        return x, y
    for ax, ay, aw, ah in areas:
        if (ax - w + margin <= x <= ax + aw - margin
                and ay - h + margin <= y <= ay + ah - margin):
            return x, y
    ax, ay, aw, ah = areas[0]
    return (min(max(x, ax), ax + max(0, aw - w)),
            min(max(y, ay), ay + max(0, ah - h)))


def first_icon(*names):
    theme = Gtk.IconTheme.get_default()
    for name in names:
        if theme.has_icon(name):
            return name
    return names[-1]


# How long the daemon has to stop answering before we call it offline. The
# daemon forgets a client after 30s, so 15s warns us before our buddies see
# "client offline - messages will be delivered".
LINK_GRACE_SECONDS = 15

# How long the daemon may have no Yggdrasil peer up before we call the node
# an island. A restart comes up with no peers for a second or two, so this
# has to be generous.
ISLAND_GRACE_SECONDS = 30

# How often we ask the daemon how its peers are doing. Slower than the link
# check: it is a full status request, and over an ssh tunnel that is worth
# being gentle with.
PEER_CHECK_SECONDS = 15

# How long the app waits for a tray host to adopt the icon before it decides
# there is no tray. The handshake needs the main loop to run, and on a fresh
# login the panel is usually not up yet when the autostart entry fires.
TRAY_ADOPT_SECONDS = 15


# A gap longer than this between two app starts means we were detached for a
# while - worth telling the user, because that is what the buddies saw.
DETACHED_NOTICE_SECONDS = 900


def fmt_duration(seconds):
    """'42s', '12m 05s', '2h 30m' - for the offline notices."""
    s = int(max(0, seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {(s % 3600) // 60:02d}m"


def detached_gap_seconds(log_path=None, now=None):
    """How long the app was not running before this start.

    The app logs 'shutting down' when it stops, so the time since that line
    is the time we were detached - exactly the window in which the buddies
    saw us as offline. A run that never logged a shutdown (killed, crashed)
    leaves no answer, and we say nothing rather than guess.
    """
    path = log_path or paths.app_log_path()
    try:
        with open(path) as f:
            lines = f.readlines()
    except OSError:
        return None
    stamp = None
    for line in reversed(lines[-400:]):
        if "shutting down" not in line:
            continue
        m = re.match(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]", line)
        if m:
            stamp = m.group(1)
            break
    if not stamp:
        return None
    try:
        then = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    now = now or datetime.now()
    return max(0.0, (now - then).total_seconds())


def set_net_wm_icon_name(window, name):
    """Publish the icon *name* on the X window.

    GTK leaves _NET_WM_ICON_NAME holding the window title, so a panel that
    resolves the taskbar icon by name looks up 'AIMless' - which does not
    exist - and falls back to a generic placeholder. The property is set
    once at realize and does not track the title, so this sticks even when
    the title changes to 'AIMless (3)'.
    """
    try:
        gdk_window = window.get_window()
        if gdk_window is None:
            return
        gi.require_version("GdkX11", "3.0")
        from gi.repository import GdkX11
        if isinstance(gdk_window, GdkX11.X11Window):
            gdk_window.set_utf8_property("_NET_WM_ICON_NAME", name)
    except Exception:
        # Wayland has no _NET_WM_ICON_NAME; the desktop entry covers it there.
        pass


def setup_app_icon(window):
    """Give a window a real application icon and a usable WM_CLASS.

    GTK derives WM_CLASS from the program name, which for us is the .pyz
    filename - the desktop then looks up an icon called 'aimless.pyz', finds
    nothing, and the taskbar entry shows up blank or generic. Setting the
    class to 'aimless' plus an icon the theme can actually resolve is what
    puts a proper icon in the taskbar.
    """
    try:
        window.set_wmclass("aimless", APP_NAME)
    except Exception:
        pass
    try:
        if appicon.has_icon():
            window.set_icon_name(appicon.ICON_NAME)
            window.set_default_icon_name(appicon.ICON_NAME)
            window.connect("realize",
                           lambda w: set_net_wm_icon_name(w, appicon.ICON_NAME))
        else:
            # No icon theme entry (first run before the icons are written, or
            # a read-only home): fall back to a pixbuf, then to a theme icon.
            pixbuf = appicon.render(48)
            if pixbuf is not None:
                window.set_default_icon(pixbuf)
            else:
                window.set_default_icon_name(
                    first_icon("user-available-symbolic", "phone", "applications-internet"))
    except Exception:
        pass


def setup_application_identity():
    """Name the process and the application the way the desktop expects.

    GTK takes WM_CLASS from the program name, which for us is the .pyz
    filename. Windows we do not touch ourselves - the tray icon's own
    window, the unlock dialog, message dialogs - would then be filed under
    'aimless.pyz' and show up without an icon. Naming the process 'aimless'
    fixes all of them at once.
    """
    try:
        GLib.set_prgname("aimless")
        GLib.set_application_name(APP_NAME)
        # GDK derives the WM_CLASS class half from the program name; without
        # this it stays 'Aimless.pyz' on windows we do not set explicitly.
        Gdk.set_program_class(APP_NAME)
    except Exception:
        pass
    try:
        settings = Gtk.Settings.get_default()
        if settings is not None and appicon.has_icon():
            settings.set_property("gtk-application-icon-name", appicon.ICON_NAME)
    except Exception:
        pass
    try:
        # Process-wide default: every window we do not touch ourselves - the
        # unlock dialog, message dialogs, GTK's own file chooser - picks the
        # app icon up from here. Without it those windows carry the theme's
        # generic placeholder, which is what the taskbar showed.
        if appicon.has_icon():
            Gtk.Window.set_default_icon_name(appicon.ICON_NAME)
    except Exception:
        pass


def tray_icon_name(unread=False, offline=False, warn=False):
    """Icon name for the tray: the app icon, badged when there are unread,
    amber when one peer is left and red when the client is not attached."""
    name = appicon.icon_name(unread, offline, warn)
    if appicon.has_icon(name):
        return name
    # No app icon in the theme (first run, or a read-only home). Fall back to
    # theme icons - but keep the states distinct whatever the theme has,
    # otherwise the unread badge or the offline warning silently disappears.
    if offline:
        return first_icon("network-offline-symbolic",
                          "network-wireless-signal-none-symbolic", "dialog-error")
    if warn:
        return first_icon("network-wireless-signal-weak-symbolic",
                          "network-wireless-signal-excellent-symbolic")
    if unread:
        return first_icon("mail-unread-symbolic", "mail-message-new-symbolic")
    return first_icon("user-available-symbolic", "phone")


def scroll_to_bottom(scrolled):
    state = {"upper": None}

    def _settle():
        try:
            adj = scrolled.get_vadjustment()
            if adj is None:
                return False
            upper = adj.get_upper()
            adj.set_value(upper - adj.get_page_size())
            # A row added just before this call is allocated by the frame clock,
            # which fires a display tick AFTER this timeout runs - so the first
            # read of `upper` is one row behind. Re-settle on a tick boundary
            # until the size stops changing and the view sits at the true bottom.
            if upper == state["upper"]:
                return False
            state["upper"] = upper
        except Exception:
            return False
        return True

    GLib.timeout_add(30, _settle)
def clear_children(container):
    container.foreach(lambda w: w.destroy())


def daemon_binary():
    # Model A transport switch: point the supervisor at a specific daemon
    # binary (e.g. aimlessd-tailcat) via AIMLESS_DAEMON_BIN. Use a separate
    # AIMLESS_HOME per transport so keys/contacts/addresses stay distinct.
    override = os.environ.get("AIMLESS_DAEMON_BIN")
    if override:
        return override
    # Yggdrasil first (the default); the tailcat binary is also searched so it
    # works without AIMLESS_DAEMON_BIN when it is the only one present.
    names = ("aimlessd", "aimlessd-linux-amd64",
             "aimlessd-tailcat", "aimlessd-tailcat-linux-amd64")
    dirs = [
        os.path.dirname(os.path.abspath(sys.argv[0])),
        os.getcwd(),
        os.path.expanduser("~/.local/bin"),
    ]
    seen = set()
    for d in dirs:
        if not d or d in seen:
            continue
        seen.add(d)
        for name in names:
            p = os.path.join(d, name)
            if os.path.exists(p):
                return p
    for name in ("aimlessd", "aimlessd-tailcat"):
        found = shutil.which(name)
        if found:
            return found
    return None


# The presence state is shared with the command line, so the two can never
# disagree about the same buddy.
_presence_state = presence_state


def _self_state(st):
    """(colour, text) for how our own node is doing.

    One place, because the route bar and the activity line used to colour the
    same state differently. Yggdrasil counts peers; tailcat reports its relay.
    """
    up = st.get("peers_up", 0)
    total = st.get("peers_total", 0)
    if is_tailcat(st):
        if up <= 0:
            return C["away"], "relay not reachable"
        return C["online"], "online"
    if up <= 0:
        return C["away"], "connecting - no Yggdrasil peers yet"
    if up == 1 and total > 1:
        # One link left: still on the network, but one failure away from
        # being cut off.
        return C["away"], "at risk - one peer left"
    return C["online"], "online"


def _presence_color(online, away):
    """The colour of a presence dot.

    Away wins over online. A buddy who set themselves away is still
    reachable, but not at the keyboard - and that has to look different from
    "here I am". The offline case stays grey, the away case is amber.
    """
    if away:
        return C["away"]
    return C["online"] if online else C["offline"]


def _room_dots_markup(members, presence_by_node, exclude):
    """One presence dot per room member (sorted like the title), excluding self."""
    parts = []
    for screen, node in sorted((m.get("screen") or n[:8], n) for n, m in members.items()
                               if n != exclude):
        p = presence_by_node.get(node, {})
        color = _presence_color(p.get("online"), p.get("away"))
        parts.append(f"<span foreground='{color}'>●</span>")
    return "".join(parts)


def _sidebar_title_markup(thread, self_node):
    """Sidebar row title. Rooms: one liveness dot + online count (scales to any room
    size); per-member dots live in the conversation header instead. DMs: single dot."""
    esc = GLib.markup_escape_text
    if thread.get("is_room"):
        pb = thread.get("presence_by_node", {})
        others = [n for n in thread.get("members", {}) if n != self_node]
        online = sum(1 for n in others if pb.get(n, {}).get("online"))
        # "Available" is reachable and actually there: not away, and the
        # buddy's client is attached. Somebody who is away is still reachable,
        # but not at the keyboard - and the count and the dot have to agree.
        available = sum(1 for n in others
                        if pb.get(n, {}).get("online") and not pb.get(n, {}).get("away")
                        and not pb.get(n, {}).get("client_offline"))
        away = sum(1 for n in others if pb.get(n, {}).get("away"))
        dot_color = C["online"] if available else (C["away"] if online else C["offline"])
        return (f"<span foreground='{dot_color}'>●</span>  "
                f"<span size='small' foreground='{C['subtitle']}'>{available}/{len(others)}</span>  "
                f"<b>{esc(thread['screen'])}</b>")
    dot_color = _presence_color(thread["online"], thread["away"])
    return f"<span foreground='{dot_color}'>●</span>  <b>{esc(thread['screen'])}</b>"


def _room_header_markup(thread, self_node):
    dots = _room_dots_markup(thread.get("members", {}), thread.get("presence_by_node", {}), self_node)
    others = [n for n in thread.get("members", {}) if n != self_node]
    pb = thread.get("presence_by_node", {})
    available = sum(1 for n in others
                    if pb.get(n, {}).get("online") and not pb.get(n, {}).get("away")
                    and not pb.get(n, {}).get("client_offline"))
    return (f"<big><b>{GLib.markup_escape_text(thread['screen'])}</b></big>  {dots}  "
            f"<span size='small' foreground='{C['subtitle']}'>{available}/{len(others)} available</span>")


def _free_petname(contacts, base):
    petname, i = base, 2
    while petname in contacts:
        petname = f"{base} {i}"
        i += 1
    return petname


def _add_contact_from_roster(node, pubkey, screen, addr=""):
    contacts = protocol.load_contacts(contacts_path())
    petname = _free_petname(contacts, screen or node[:8])
    contacts[petname] = {"pubkey": pubkey, "node": node, "screen": screen or node[:8], "addr": addr}
    protocol.save_contacts(contacts_path(), contacts)
    return petname


def run_async(fn, on_done=None, on_error=None):
    def worker():
        try:
            result = fn()
        except Exception as exc:
            err = exc

            def deliver_error():
                on_error(err)
                return False

            if on_error:
                GLib.idle_add(deliver_error)
        else:
            if on_done:
                def deliver_done():
                    on_done(result)
                    return False

                GLib.idle_add(deliver_done)
    threading.Thread(target=worker, daemon=True).start()

def current_theme():
    """The active theme name. A function because CURRENT_THEME is rebinding."""
    return CURRENT_THEME
