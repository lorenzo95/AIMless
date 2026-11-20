"""The application shell: tray, connect window, lifecycle and AimlessApp.

Extracted from gtkui.py. Owns the desktop-integration helpers (autostart,
desktop entry, app lock, excepthooks), the tray icon, the remote-connect window
and the top-level application object that ties the views together.
"""
import os
import shutil
import signal
import sys
import threading
import time
import traceback

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib

from . import appicon, logging, paths, pending
from .daemon import DaemonClient
from .dialogs import _error_dialog, ask_create_identity, ask_passphrase, ask_secret
from .identity import create_identity, restore_bundle
from .notify import desktop_notify, notify_init_async
from .peerdialog import ask_peers
from .prefs import load_prefs, pref, save_prefs
from .relay import is_tailcat, make_region_list
from .relayctl import apply_relay, relay_cfg_equal, reset_relay_config, restart_daemon
from .session import DaemonSupervisor, Session, read_pid
from .uibase import (APP_NAME, C, DETACHED_NOTICE_SECONDS, ISLAND_GRACE_SECONDS,
                     LINK_GRACE_SECONDS, PEER_CHECK_SECONDS, PENDING_CHECK_SECONDS,
                     TRAY_ADOPT_SECONDS, detached_gap_seconds, fmt_duration,
                     attachments_dir, identity_path, install_css_provider, run_async,
                     setup_app_icon, setup_application_identity, sock_path, tray_icon_name)
from .window import AimlessWindow


def _make_excepthook(gui_log):
    def hook(et, ev, tb):
        try:
            gui_log("uncaught: " + "".join(traceback.format_exception(et, ev, tb)))
        except Exception:
            pass
        sys.__excepthook__(et, ev, tb)
    return hook


def _make_thread_hook(gui_log):
    def hook(args):
        try:
            gui_log("thread crash: " + "".join(
                traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)))
        except Exception:
            pass
    return hook


def app_log_path():
    return paths.app_log_path()


def acquire_app_lock():
    """Single-instance guard. Returns (fh, None) when the lock was taken - hold the file
    handle for the process lifetime - or (None, holder_pid) when another instance runs."""
    import fcntl
    try:
        os.makedirs(paths.run_dir(), exist_ok=True)
        fh = open(paths.app_pid_path(), "a+")
    except OSError:
        return None, -1
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None, read_pid(paths.app_pid_path()) or -1
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()))
    fh.flush()
    return fh, None


def app_exec_line(mode="gui"):
    """The command line that starts the app, for the desktop entries.

    Prefers an installed `aimless` command, falls back to the .pyz next to
    it. Returns None if neither can be found.
    """
    aimless_bin = shutil.which("aimless") or os.path.expanduser("~/.local/bin/aimless")
    if os.path.exists(aimless_bin):
        return f"{aimless_bin} {mode}".strip()
    for d in (os.path.dirname(os.path.abspath(sys.argv[0])), os.getcwd(),
              os.path.expanduser("~/.local/bin")):
        pyz = os.path.join(d, "aimless.pyz")
        if os.path.exists(pyz):
            return f"{sys.executable} {pyz} {mode}".strip()
    return None


def install_desktop_entry():
    """Install a .desktop entry so the desktop can identify this app.

    Without one the taskbar has nothing to match the window against: it
    falls back to a generic icon and offers no application menu on
    right-click, because there is no application to build a menu from.
    StartupWMClass has to match the WM_CLASS we set ('aimless').
    """
    try:
        appicon.install()
    except Exception:
        pass
    exec_line = app_exec_line("gui")
    if not exec_line:
        return None
    data_home = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    path = os.path.join(data_home, "applications", "aimless.desktop")
    content = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=AIMless\n"
        "GenericName=Instant Messenger\n"
        "Comment=Serverless, end-to-end-encrypted instant messaging\n"
        f"Exec={exec_line}\n"
        f"Icon={appicon.ICON_NAME}\n"
        "Terminal=false\n"
        "StartupWMClass=aimless\n"
        "StartupNotify=true\n"
        "Categories=Network;InstantMessaging;\n"
        "Keywords=chat;im;aim;messaging;\n"
    )
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        old = None
        if os.path.exists(path):
            with open(path) as f:
                old = f.read()
        if old != content:
            with open(path, "w") as f:
                f.write(content)
        return path
    except OSError:
        return None


def install_autostart():
    desktop_path = paths.autostart_path()
    autostart_dir = os.path.dirname(desktop_path)
    # The entry points at our own icon, so make sure it is on disk first.
    try:
        appicon.install()
    except Exception:
        pass

    exec_line = app_exec_line("tray")
    if not exec_line:
        return None

    content = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=AIMless\n"
        "Comment=AIMless tray + daemon - messages are received in the background\n"
        f"Exec={exec_line}\n"
        f"Icon={appicon.ICON_NAME}\n"
        "Categories=Network;InstantMessaging;\n"
        "X-GNOME-Autostart-enabled=true\n"
        "X-XFCE-Autostart-Override=true\n"
        "Hidden=false\n"
    )
    os.makedirs(autostart_dir, exist_ok=True)
    with open(desktop_path, "w") as f:
        f.write(content)
    return desktop_path


class TrayIcon:
    # Class-level defaults so a TrayIcon built without __init__ (tests) still
    # has a sane state.
    unread = 0
    offline = False
    reason = ""
    locked = False
    warn = False
    warn_reason = ""

    def __init__(self, app):
        self.app = app
        self.have_tray = False
        self.unread = 0
        self.offline = False
        self.reason = ""
        self.locked = False
        self.warn = False
        self.warn_reason = ""
        self.menu = Gtk.Menu()
        mi_open = Gtk.MenuItem(label="Open AIMless")
        mi_open.connect("activate", self.on_open)
        self.menu.append(mi_open)
        self.menu.append(Gtk.SeparatorMenuItem())
        # A status line, so the state is visible in the menu too - not just
        # in the tooltip, which only shows on hover.
        self.status_item = Gtk.MenuItem(label="Status: running")
        self.status_item.set_sensitive(False)
        self.menu.append(self.status_item)
        self.menu.append(Gtk.SeparatorMenuItem())
        mi_quit = Gtk.MenuItem(label="Quit - shuts down AIMless")
        mi_quit.connect("activate", self.on_quit)
        self.menu.append(mi_quit)
        self.menu.show_all()
        try:
            self.icon = Gtk.StatusIcon()
            self.icon.set_title(APP_NAME)
            self.icon.connect("activate", self.on_open)
            self.icon.connect("popup-menu", self.on_popup)
            self.icon.set_visible(True)
            self.have_tray = True
            self._apply()
        except Exception as e:
            app.log(f"tray icon unavailable ({e!r}) - running as a plain window app")

    def set_unread(self, n, locked=False):
        """Show unread messages on the tray icon itself, not just the tooltip.

        The tooltip alone is invisible until you hover, so a new message left
        no trace on the icon. The badge variant makes it obvious at a glance.

        ``locked`` marks the count as "messages are waiting, the app is not
        unlocked yet" - the app cannot read them, so the wording says so.
        """
        self.unread = int(n or 0)
        self.locked = bool(locked)
        self._apply()

    def set_locked(self, locked):
        """The app was unlocked (or locked again) - the wording changes, the
        count does not: the same messages are still unread."""
        self.locked = bool(locked)
        self._apply()

    def set_offline(self, offline, reason=""):
        """Mark the client as not attached to the daemon.

        This is the state your buddies see as "client offline - messages will
        be delivered", so it has to be impossible to miss: the running man
        turns red.
        """
        self.offline = bool(offline)
        self.reason = reason or ""
        self._apply()

    def set_warn(self, warn, reason=""):
        """Mark the node as one peer away from being unreachable.

        Still on the network, but with a single link left: if that peer goes,
        the node is cut off. The running man turns amber - different from
        yellow (fine) and red (gone).
        """
        self.warn = bool(warn)
        self.warn_reason = reason or ""
        self._apply()

    def _tooltip(self):
        # Offline wins over at risk: the node is cut off, not fragile.
        if self.offline:
            head = f"{APP_NAME} - OFFLINE"
            if self.unread:
                head += f" - {self.unread} unread"
            lines = [head]
            if self.reason:
                lines.append(self.reason)
            lines.append("Left-click to open Messages")
            return "\n".join(lines)
        if self.warn:
            head = f"{APP_NAME} - AT RISK"
            if self.unread:
                head += f" - {self.unread} unread"
            lines = [head]
            if self.warn_reason:
                lines.append(self.warn_reason)
            lines.append("Left-click to open Messages")
            return "\n".join(lines)
        if self.unread:
            if self.locked:
                return (f"{APP_NAME} - {self.unread} new message(s) waiting\n"
                        "Click to unlock AIMless and read them")
            return f"{APP_NAME} - {self.unread} unread\nLeft-click to open Messages"
        if self.locked:
            return f"{APP_NAME} - locked\nClick to unlock AIMless"
        return f"{APP_NAME} - running\nLeft-click to open Messages"

    def _status_line(self):
        if self.offline:
            return f"Status: offline - {self.reason}" if self.reason else "Status: offline"
        if self.warn:
            return (f"Status: at risk - {self.warn_reason}" if self.warn_reason
                    else "Status: at risk")
        if self.unread:
            if self.locked:
                return f"Status: locked - {self.unread} message(s) waiting"
            return f"Status: running - {self.unread} unread"
        if self.locked:
            return "Status: locked"
        return "Status: running"

    def _apply(self):
        """Put the current state on the icon, the tooltip and the menu."""
        if not getattr(self, "have_tray", False):
            return
        try:
            self.icon.set_from_icon_name(tray_icon_name(self.unread > 0, self.offline,
                                                        self.warn))
            self.icon.set_tooltip_text(self._tooltip())
        except Exception:
            pass
        item = getattr(self, "status_item", None)
        if item is not None:
            try:
                item.set_label(self._status_line())
            except Exception:
                pass

    def is_embedded(self):
        """True only when a real system-tray host adopted the icon.

        Gtk.StatusIcon() does not raise when no tray host exists (X11 simply shows
        nothing), so `have_tray` alone would make a headless/container run believe
        it owns a tray and hide away to a tray that is not there.
        """
        return self.have_tray and self.icon.is_embedded()

    def on_open(self, *_):
        try:
            self.app.open_window()
        except Exception as e:
            self.app.log(f"open failed: {e!r}")

    def on_quit(self, *_):
        try:
            self.app.quit()
        except Exception as e:
            self.app.log(f"shutdown error: {e!r}")

    def on_popup(self, icon, button, t):
        self.menu.popup(None, None, Gtk.StatusIcon.position_menu, icon, button, t)


class ConnectWindow(Gtk.Window):
    """Small 'connecting…' window shown when the always-on daemon isn't reachable
    at startup, so the user can see whether ssh or the daemon is the problem."""

    SSH_TEXT = {
        "connecting": ("away", "●  ssh: connecting to {host}…"),
        "connected": ("online", "●  ssh: connected ({host})"),
        "failed": ("danger", "●  ssh: failed - retrying ({host})…"),
    }
    DAEMON_TEXT = {
        "waiting": ("muted", "●  daemon: waiting for the tunnel…"),
        "connecting": ("away", "●  daemon: connecting…"),
        "connected": ("online", "●  daemon: connected"),
        "unreachable": ("danger", "●  daemon: unreachable - retrying…"),
    }

    def __init__(self, remote, on_quit):
        super().__init__(title=f"{APP_NAME} - connecting")
        self.get_style_context().add_class("aimless-window")
        setup_app_icon(self)
        self.remote = remote or {}
        self._on_quit = on_quit
        self.set_default_size(460, -1)
        self.connect("delete-event", self._on_delete)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_border_width(14)
        self.ssh_label = Gtk.Label(xalign=0.0)
        self.daemon_label = Gtk.Label(xalign=0.0)
        box.pack_start(self.ssh_label, False, False, 0)
        box.pack_start(self.daemon_label, False, False, 0)
        box.pack_start(Gtk.Separator(), False, False, 0)
        cfg = Gtk.Label(xalign=0.0)
        cfg.set_line_wrap(True)
        cfg.set_selectable(True)  # read-only, copyable
        cfg.get_style_context().add_class("muted")
        cfg.set_text(
            "ssh            {host}\n"
            "remote socket  {remote}\n"
            "local socket   {local}".format(
                host=self.remote.get("host", ""),
                remote=self.remote.get("socket", ""),
                local=self.remote.get("local_socket", "")))
        box.pack_start(cfg, False, False, 0)
        quit_btn = Gtk.Button(label="Quit")
        quit_btn.set_halign(Gtk.Align.END)
        quit_btn.connect("clicked", lambda *_: self._on_quit())
        box.pack_start(quit_btn, False, False, 0)
        self.add(box)
        self.update("connecting", "waiting")

    def _on_delete(self, *_):
        self._on_quit()
        return False

    def update(self, ssh, daemon):
        color, text = self.SSH_TEXT.get(ssh, self.SSH_TEXT["connecting"])
        text = text.format(host=self.remote.get("host", ""))
        self.ssh_label.set_markup(
            f"<span foreground='{C[color]}'>{GLib.markup_escape_text(text)}</span>")
        color, text = self.DAEMON_TEXT.get(daemon, self.DAEMON_TEXT["waiting"])
        self.daemon_label.set_markup(
            f"<span foreground='{C[color]}'>{GLib.markup_escape_text(text)}</span>")


class AimlessApp:
    """One process: messages window + tray icon + the aimlessd daemon."""

    def __init__(self):
        # How long we were detached before this start - read the log before
        # this run writes its first line into it.
        self._detached_gap = detached_gap_seconds()
        self.log = logging.log_fn(app_log_path())
        self.supervisor = DaemonSupervisor()
        self.session = None
        self.passphrase = None
        self.window = None
        self.tray = None
        self.lock_fh = None
        self.quitting = False
        self._unlocking = False
        self._tunnel_fail = 0
        self._tunnel_restarting = False
        self._daemon_down_logged = False
        self._awaiting_connect = False
        self._connect_busy = False
        self._connect_show = False
        self._connect_state = {"ssh": "connecting", "daemon": "waiting"}
        self.connect_window = None
        # Link health: is our client traffic actually landing on the daemon?
        # If it is not, the daemon advertises us as "client offline" and the
        # buddies see us as gone. The tray has to say so.
        self._link_ok = True
        self._link_reason = ""
        self._link_timer = None
        # Island: the daemon answers but has no Yggdrasil peer up, so the
        # node cannot reach the overlay network and nobody can reach us.
        self._island = False
        self._island_reason = ""
        self._island_timer = None
        # None means "not told the tray yet" - the first reading always
        # applies, so the tray never keeps a stale state.
        self._peer_risk = None
        self._offline_notified = False
        # Locked state: the app runs at login without the passphrase, so it
        # cannot decrypt anything - but the daemon keeps journaling what
        # arrives, so the tray can still say "a message is waiting".
        self._pending_timer = None
        self._pending_busy = False
        self._pending_count = 0
        self._pending_error = ""
        # Put the app icon in the user's icon theme before anything asks for
        # it - the tray looks the name up when it is created.
        try:
            if not appicon.install():
                self.log("could not install the app icon - the drawing stack "
                         "(python3-cairo) is missing, the taskbar will show a "
                         "generic icon")
        except Exception as e:
            self.log(f"could not install the app icon: {e!r}")
        setup_application_identity()
        # Register the app with the desktop so the taskbar can match the
        # window to it and show a real icon (and an application menu).
        try:
            install_desktop_entry()
        except Exception as e:
            self.log(f"could not install the desktop entry: {e!r}")

    def start(self, open_window):
        rc = self._setup(open_window)
        if rc is not None:
            return rc
        Gtk.main()
        return 0

    def _setup(self, open_window):
        sys.excepthook = _make_excepthook(self.log)
        threading.excepthook = _make_thread_hook(self.log)

        paths.ensure_dirs()
        install_css_provider()

        self.lock_fh, holder = acquire_app_lock()
        if self.lock_fh is None:
            self.log(f"another instance is running (pid {holder}) - presenting its window")
            if holder and holder > 0:
                try:
                    os.kill(holder, signal.SIGUSR1)
                except OSError:
                    pass
            return 0

        if not self.supervisor.external:
            while True:
                try:
                    self.supervisor.ensure(log=self.log)
                    break
                except RuntimeError as e:
                    dlg = Gtk.MessageDialog(
                        message_type=Gtk.MessageType.ERROR,
                        buttons=Gtk.ButtonsType.NONE,
                        text="The daemon could not start.")
                    dlg.format_secondary_text(
                        f"{e}\n\nIf you changed the relay settings, they may be invalid. "
                        "Reset the relay to the default public network and try again?")
                    dlg.add_button("Quit", Gtk.ResponseType.CANCEL)
                    dlg.add_button("Reset relay & retry", Gtk.ResponseType.OK)
                    resp = dlg.run()
                    dlg.destroy()
                    if resp != Gtk.ResponseType.OK or not reset_relay_config():
                        return 1
                    self.log("relay config reset to the default public network - retrying")

        self.tray = TrayIcon(self)
        # The timers go up BEFORE the window. The unlock dialog is modal and
        # blocks until the passphrase is typed; with the heartbeat only
        # started afterwards the daemon saw no client at all while we sat at
        # the prompt, and the buddies showed us as "client offline" for as
        # long as the dialog was up.
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR1, self.on_open_signal)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, self.quit)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, self.quit)
        GLib.timeout_add_seconds(5, self.poll)
        GLib.timeout_add_seconds(10, self.heartbeat)
        GLib.timeout_add_seconds(PEER_CHECK_SECONDS, self.check_peers)
        # Count the messages that arrive while the app is locked. Once the
        # identity is unlocked the real unread counters take over.
        self._pending_timer = GLib.timeout_add_seconds(PENDING_CHECK_SECONDS,
                                                       self._check_pending)
        if self.supervisor.external:
            self.log(f"external daemon mode - tunnel to {self.supervisor.tunnel.host}")
            GLib.timeout_add_seconds(20, self.probe_tunnel)
        # Connect to the session bus off the main thread - it can block for
        # a while when the bus is not up yet.
        notify_init_async()
        # Say how long we were away - the buddies saw us as offline for that
        # whole time. A moment later, so the notification has a bus to go
        # out on.
        GLib.timeout_add_seconds(3, self._announce_detached_gap)
        # Tell the daemon a client is attached now, not in ten seconds.
        self.heartbeat()

        if self.supervisor.external:
            # Never block startup on the remote daemon: connect in the
            # background and show a status window if it doesn't come up promptly.
            if self.supervisor._local_daemon_running():
                err = Gtk.MessageDialog(
                    message_type=Gtk.MessageType.ERROR, buttons=Gtk.ButtonsType.CLOSE,
                    text="a local aimlessd is running - run `aimless stop` before using the remote daemon")
                err.run()
                err.destroy()
                return 1
            self._begin_connect(open_window or not self.tray.have_tray)
        elif open_window or not self.tray.have_tray:
            self.open_window()
        if self._no_window_headless():
            # The tray host may simply not have adopted the icon yet - the
            # handshake needs the main loop to run. Give it a moment before
            # deciding there is no tray at all.
            self._wait_for_tray()
        if self._no_window_headless():
            self.log("no window after setup (no usable tray) - exiting for the supervisor to restart")
            return 0

        self.log(f"app started (pid {os.getpid()}, tray={self.tray.have_tray})")
        return None

    # -- first connection (external mode) --------------------------------
    def _begin_connect(self, show_window=True):
        self._awaiting_connect = True
        self._connect_show = show_window
        self._connect_busy = False
        self._try_connect()
        if show_window:
            GLib.timeout_add(1000, self._maybe_show_connect)

    def _maybe_show_connect(self):
        if self._awaiting_connect and self.connect_window is None:
            self._show_connect_window()
        return False

    def _show_connect_window(self):
        self.connect_window = ConnectWindow(self.supervisor.remote, self.quit)
        self.connect_window.show_all()
        self._update_connect_window()

    def _update_connect_window(self):
        if self.connect_window is not None:
            self.connect_window.update(self._connect_state["ssh"], self._connect_state["daemon"])

    def _set_connect_state(self, ssh, daemon):
        self._connect_state = {"ssh": ssh, "daemon": daemon}
        self._update_connect_window()

    def _try_connect(self):
        if not self._awaiting_connect or self._connect_busy:
            return
        self._connect_busy = True
        self._set_connect_state("connecting", "waiting")

        def worker():
            try:
                self.supervisor.ensure(log=self.log)
                return None
            except RuntimeError as e:
                return str(e)

        def done(err):
            self._connect_busy = False
            if not self._awaiting_connect:
                return
            if err is None and self.supervisor._reachable():
                self._on_connected()
                return
            # classify: ssh down vs daemon unreachable
            ssh_ok = self.supervisor.tunnel.is_running()
            self._set_connect_state(
                "connected" if ssh_ok else "failed",
                "unreachable" if ssh_ok else "waiting")
            GLib.timeout_add_seconds(3, self._retry_connect)

        run_async(worker, on_done=done, on_error=lambda _e: done("error"))

    def _retry_connect(self):
        if self._awaiting_connect:
            self._try_connect()
        return False

    def _on_connected(self):
        self._awaiting_connect = False
        self._set_connect_state("connected", "connected")
        if self.connect_window is not None:
            win, self.connect_window = self.connect_window, None
            win.destroy()
        self.log(f"connected to remote daemon ({self.supervisor.tunnel.host})")
        # The tray was red while we were connecting - clear it now rather
        # than waiting for the next heartbeat.
        self._probe_link()
        if self._connect_show or not self.tray.have_tray:
            self.open_window()

    # -- SSH tunnel (remote mode) ------------------------------------------
    def probe_tunnel(self):
        """20s liveness probe. Only a *tunnel* failure (ssh/socket gone) triggers
        a restart; a reachable tunnel with an unreachable daemon is just reported
        (restarting ssh wouldn't help)."""
        if self.quitting or not self.supervisor.external or getattr(self, "_awaiting_connect", False):
            return False
        if self._tunnel_restarting:
            return True
        t = self.supervisor.tunnel
        if not t.is_running():
            self._tunnel_fail += 1
            if self._tunnel_fail == 1:
                self.log("tunnel: down (1/2) - confirming")
                GLib.timeout_add(1000, self._confirm_tunnel)
            else:
                self._tunnel_fail = 0
                self._schedule_tunnel_restart()
            return True
        if t.probe():
            if self._tunnel_fail:
                self.log("tunnel: healthy again")
            self._tunnel_fail = 0
            if self._daemon_down_logged:
                self._daemon_down_logged = False
                self.log("remote daemon reachable again")
            return True
        # tunnel is up, remote daemon isn't answering
        self._tunnel_fail = 0
        if not self._daemon_down_logged:
            self._daemon_down_logged = True
            self.log(f"tunnel: up, but remote daemon unreachable ({t.host})")
        if self.window is not None:
            self.window.poll_status()
        return True

    def _confirm_tunnel(self):
        if self.quitting or self._tunnel_restarting:
            return False
        t = self.supervisor.tunnel
        if t.is_running():
            self._tunnel_fail = 0
            self.log("tunnel: recovered")
            return False
        self._tunnel_fail = 0
        self._schedule_tunnel_restart()
        return False

    def _schedule_tunnel_restart(self):
        if self._tunnel_restarting or self.quitting:
            return
        self._tunnel_restarting = True
        delay = self.supervisor.tunnel.next_backoff()
        self.log(f"tunnel: restarting in {delay}s ({self.supervisor.tunnel.host})")
        GLib.timeout_add_seconds(delay, self._do_tunnel_restart)

    def _do_tunnel_restart(self):
        if self.quitting:
            self._tunnel_restarting = False
            return False
        t = self.supervisor.tunnel
        ok = t.restart(log=self.log)
        self._tunnel_restarting = False
        if ok:
            t.reset_backoff()
            self.rewatch()
        else:
            self._schedule_tunnel_restart()
        if self.window is not None:
            self.window.poll_status()
        return False


    # -- peers & relay UI ---------------------------------------------------
    def on_open_signal(self, *_):
        self.open_window()
        return GLib.SOURCE_REMOVE

    def _apply_peer_list(self, peers):
        """Write a peer list to the daemon.

        The daemon validates it, saves it to its config.json and applies it, so
        the list survives a restart and the file never disagrees with what the
        daemon is doing.
        """
        d = None
        try:
            d = DaemonClient(self.supervisor.sock)
            resp = d.request("setpeers", peer_list=list(peers), timeout=20)
        except Exception as e:
            self.log(f"could not set the peer list: {e!r}")
            return False
        finally:
            if d is not None:
                d.close()
        for uri, why in (resp.get("failed") or {}).items():
            self.log(f"peer refused: {uri} - {why}")
        added = resp.get("added") or []
        removed = resp.get("removed") or []
        if added:
            self.log(f"peers added: {', '.join(added)}")
        if removed:
            self.log(f"peers removed: {', '.join(removed)}")
        return True

    def show_peers(self, parent=None):
        """The network manager: Yggdrasil shows the editable peer list; tailcat
        shows the relay panel (the relay is baked into the address)."""
        st = self.supervisor.status()
        if st is None:
            _error_dialog(parent, "The daemon is not answering, so its network "
                                  "state is unknown. Start it and try again.")
            return
        if is_tailcat(st):
            self._relay_dialog(parent, st)
            return
        current = st.get("peer_list") or []
        details = st.get("peer_details") or []
        peers = ask_peers(parent, current, live=True, details=details,
                          defaults=st.get("default_peers") or [])
        if peers is None:
            return
        if peers != current:
            self._apply_peer_list(peers)

    def _first_run_peers(self, imported=False):
        """The network step of the setup wizard. Yggdrasil: the built-in peer
        list works out of the box, so this is about adding your own. tailcat:
        the public DERP relays work out of the box, so this is informational."""
        st = self.supervisor.status()
        if st is None:
            self.log("daemon not answering - skipping the network step")
            return
        if is_tailcat(st):
            relay = st.get("relay") or {}
            if imported and (relay.get("region_id") or relay.get("hostname")):
                # Region/relay restored from the backup - already part of the
                # address, so don't re-prompt. A brand-new identity still picks.
                return
            self._relay_dialog(None, st, first_run=True)
            return
        current = st.get("peer_list") or []
        peers = ask_peers(None, current, defaults=st.get("default_peers") or [])
        if peers is None:
            return
        if peers != current:
            self._apply_peer_list(peers)

    def _relay_dialog(self, parent, st, first_run=False):
        """The tailcat relay panel: pick your DERP region (it becomes part of your
        address), or point the daemon at a self-hosted relay. Pick the region
        before sharing invites; changing it later means buddies need your new
        invite."""
        title = "AIMless - step 2 of 2: relay" if first_run else "AIMless - relay"
        dlg = Gtk.Dialog(title=title, transient_for=parent, modal=True)
        setup_app_icon(dlg)
        save_btn = dlg.add_button("Save", Gtk.ResponseType.APPLY)
        close_btn = dlg.add_button("Finish" if first_run else "Close", Gtk.ResponseType.OK)
        dlg.set_default_size(560, -1)
        box = dlg.get_content_area()
        box.set_spacing(10)
        box.set_border_width(14)
        head = Gtk.Label(label="AIMless is using the tailcat transport.")
        head.set_xalign(0)
        box.pack_start(head, False, False, 0)
        info = Gtk.Label(label=(
            "tailcat connects you directly to each buddy when the network allows, "
            "and falls back to a DERP relay when it does not. Pick the relay region "
            "nearest you - it becomes part of your address."))
        info.set_xalign(0)
        info.set_line_wrap(True)
        box.pack_start(info, False, False, 0)

        relay = st.get("relay") or {}
        regions = st.get("regions") or []
        current = st.get("region") or {}

        rl = Gtk.Label(label="Relay region (part of your address):")
        rl.set_xalign(0)
        box.pack_start(rl, False, False, 0)
        if regions:
            region_scroll, region_view = make_region_list(regions, current.get("id"))
            box.pack_start(region_scroll, False, False, 0)
        else:
            region_view = None
            unavail = Gtk.Label(label="(regions unavailable - daemon offline?)")
            unavail.set_xalign(0)
            box.pack_start(unavail, False, False, 0)

        hl = Gtk.Label(label="...or a self-hosted relay hostname (overrides the region):")
        hl.set_xalign(0)
        box.pack_start(hl, False, False, 0)
        host_entry = Gtk.Entry()
        host_entry.set_text(relay.get("hostname", ""))
        host_entry.set_placeholder_text("derp.example.com")
        box.pack_start(host_entry, False, False, 0)

        ml = Gtk.Label(label="DERP map URL (advanced - blank = default public map):")
        ml.set_xalign(0)
        box.pack_start(ml, False, False, 0)
        map_entry = Gtk.Entry()
        map_entry.set_text(relay.get("derpmap_url", ""))
        map_entry.set_placeholder_text("https://login.tailscale.com/derpmap/default")
        box.pack_start(map_entry, False, False, 0)

        addr = st.get("address", "")
        al = Gtk.Label(label="Your tailcat address (share it as an invite):")
        al.set_xalign(0)
        box.pack_start(al, False, False, 0)
        addr_entry = Gtk.Entry()
        addr_entry.set_text(addr)
        addr_entry.set_editable(False)
        box.pack_start(addr_entry, False, False, 0)

        apply_note = (
            "Saving stores the change; restart the remote daemon to apply."
            if self.supervisor.external else
            "Saving applies the change by restarting the daemon."
        )
        note = Gtk.Label(label=(
            "Your region is part of your address, so pick it before sharing invites. "
            "Changing it later changes your address; active buddies pick up the new "
            "one automatically from your status. " + apply_note))
        note.set_xalign(0)
        note.set_line_wrap(True)
        note.set_opacity(0.7)
        box.pack_start(note, False, False, 0)
        status = Gtk.Label(label="")
        status.set_xalign(0)
        box.pack_start(status, False, False, 0)

        def pending_cfg():
            hostname = host_entry.get_text().strip()
            cfg = {"hostname": hostname}
            map_url = map_entry.get_text().strip()
            if map_url:
                cfg["derpmap_url"] = map_url
            if not hostname and region_view is not None:
                model, it = region_view.get_selection().get_selected()
                if it is not None:
                    cfg["region_id"] = model.get_value(it, 1)
            return cfg

        def refresh_save(*_):
            save_btn.set_sensitive(not relay_cfg_equal(pending_cfg(), relay))

        host_entry.connect("changed", refresh_save)
        map_entry.connect("changed", refresh_save)
        if region_view is not None:
            region_view.get_selection().connect("changed", refresh_save)
        refresh_save()

        dlg.show_all()
        while True:
            resp = dlg.run()
            if resp != Gtk.ResponseType.APPLY:
                break
            cfg = pending_cfg()
            save_btn.set_sensitive(False)
            close_btn.set_sensitive(False)
            status.set_text("Saving …" if self.supervisor.external
                            else "Restarting daemon to apply …")
            while Gtk.events_pending():
                Gtk.main_iteration()
            try:
                msg, new = apply_relay(self.supervisor, cfg, relay,
                                       getattr(self, "log", None))
                status.set_text(msg)
                if new is not None:
                    addr = new.get("address", addr)
                    addr_entry.set_text(addr)
                    relay = new.get("relay") or {}
            except Exception as e:
                status.set_text(f"error: {e}")
            finally:
                close_btn.set_sensitive(True)
                refresh_save()
        dlg.destroy()

    # -- window lifecycle & startup -----------------------------------------
    def open_window(self):
        if getattr(self, "_awaiting_connect", False):
            if self.connect_window is not None:
                self.connect_window.present()
            return
        if self.window:
            self.window.deiconify()
            self.window.present()
            # Re-map can make the WM apply its own placement, and the debounced
            # configure handler would then save that wrong spot. Re-assert the
            # remembered geometry on every tray reopen.
            self.window.restore_geometry()
            return
        if self._unlocking:
            return
        self._unlocking = True
        try:
            self._open_window_unlocked()
        finally:
            self._unlocking = False

    def _open_window_unlocked(self):
        session = None
        passphrase = self.passphrase
        fresh_identity = False
        imported_identity = False
        if not os.path.exists(identity_path()):
            for _create in range(3):
                created = ask_create_identity(None)
                if created is None:
                    self._cancel_or_quit()
                    return
                if created[0] == "__import__":
                    imported = self._import_backup_on_init()
                    if imported:
                        passphrase = imported
                        try:
                            session = Session(passphrase)
                            fresh_identity = True
                            imported_identity = True
                            break
                        except (ValueError, OSError) as e:
                            _error_dialog(None, f"imported identity could not be opened: {e}")
                    continue
                if created[0] == "__mismatch__":
                    err = Gtk.MessageDialog(message_type=Gtk.MessageType.ERROR, buttons=Gtk.ButtonsType.OK,
                                            text="passphrases do not match - try again")
                    err.run()
                    err.destroy()
                    continue
                new_pw, screen = created
                try:
                    create_identity(new_pw, screen)
                    passphrase = new_pw
                    session = Session(passphrase)
                    fresh_identity = True
                    break
                except OSError as e:
                    err = Gtk.MessageDialog(message_type=Gtk.MessageType.ERROR, buttons=Gtk.ButtonsType.CLOSE,
                                            text=f"could not create identity: {e}")
                    err.run()
                    err.destroy()
                    return
            else:
                return
        else:
            if passphrase:
                try:
                    session = Session(passphrase)
                except ValueError:
                    passphrase = None
                except OSError as e:
                    session = None
                    self.log(f"daemon unreachable during unlock ({e}) - retrying")
            if not passphrase:
                for _attempt in range(3):
                    passphrase = ask_passphrase(None)
                    if not passphrase:
                        self._cancel_or_quit()
                        return
                    try:
                        session = Session(passphrase)
                        break
                    except ValueError:
                        err = Gtk.MessageDialog(message_type=Gtk.MessageType.ERROR, buttons=Gtk.ButtonsType.OK,
                                                text="wrong passphrase or corrupted identity - try again")
                        err.run()
                        err.destroy()
                    except OSError as e:
                        err = Gtk.MessageDialog(message_type=Gtk.MessageType.ERROR, buttons=Gtk.ButtonsType.CLOSE,
                                                text=f"daemon not reachable: {e}\nretry after starting aimless")
                        err.run()
                        err.destroy()
                        return
                else:
                    return
        if fresh_identity:
            # Step 2 of the setup wizard: the peers.
            self._first_run_peers(imported=imported_identity)
        self.passphrase = passphrase
        if session.cache_recovered:
            self.log(f"cache recovery: {session.cache_recovered}")
        self.session = session
        self._prune_history()
        # The app is unlocked now: the real unread counters take over from the
        # locked-state count (same messages, different wording). The pending
        # timer removes itself on its next tick.
        tray = getattr(self, "tray", None)
        if tray is not None:
            try:
                tray.set_locked(False)
            except Exception:
                pass
        self.window = AimlessWindow(session, self.supervisor, app_ref=self)
        self.window.restore_geometry()
        self.window.show_all()

    def _prune_history(self, force=False):
        """Apply age-based history retention once the store is unlocked.

        Runs at unlock and then at most once a day (the poll loop calls it too,
        which is just a timestamp check), so an always-on tray session does not
        grow without bound either. ``force`` skips the daily throttle: the
        Preferences dialog uses it after a change or a Prune now click.
        Pruning only removes stored history and its attachment files - never
        undelivered outgoing mail. Returns what went, or {} when nothing ran.
        """
        if self.session is None:
            return {}  # still locked - nothing to prune yet
        prefs = load_prefs()
        try:
            days = int(pref(prefs, "history_days") or 0)
        except (TypeError, ValueError):
            return {}  # a hand-edited prefs.json must not break the poll loop
        try:
            last = float(prefs.get("history_pruned_at") or 0)
        except (TypeError, ValueError):
            last = 0.0  # unreadable watermark: run the pass now
        if days <= 0:
            return {}
        now = time.time()
        if not force and now - last < 86400:
            return {}
        try:
            gone = self.session.store.prune_history(
                days, attachments_root=attachments_dir())
        except Exception as e:
            self.log(f"history retention skipped: {e}")
            return {}
        prefs["history_pruned_at"] = now
        save_prefs(prefs)
        if gone["messages"]:
            extra = (f" and {gone['attachments']} attachment file(s)"
                     if gone["attachments"] else "")
            self.log(f"history retention: removed {gone['messages']} message(s) "
                     f"older than {days} days{extra}")
            self._refresh_after_prune()
        return gone

    def _refresh_after_prune(self):
        """An open thread may have lost bubbles - redraw it from the store."""
        messages = getattr(getattr(self, "window", None), "messages", None)
        selected = getattr(messages, "selected", None)
        if selected is None:
            return
        try:
            messages.refresh_conversation(selected["conv"])
        except Exception:
            pass

    def _cancel_or_quit(self):
        """User cancelled and there is no usable tray: log it so the caller (a
        supervisor/container) is expected to restart us. With a real tray the
        desktop behaviour is preserved: keep running, hidden in the tray."""
        if self.tray is not None and self.tray.is_embedded():
            self.log("cancel - keeping app in the system tray")
            return
        self.log("cancel - no usable tray (headless/container) - nothing to show")

    def _import_backup_on_init(self):
        """First-run import: pick a bundle, restore it, reload the daemon.

        Returns the new app passphrase on success, else None."""
        chooser = Gtk.FileChooserDialog(title="Import an aimless backup",
                                        action=Gtk.FileChooserAction.OPEN)
        chooser.add_buttons("Cancel", Gtk.ResponseType.CANCEL,
                            "Open", Gtk.ResponseType.OK)
        chooser.set_default_response(Gtk.ResponseType.OK)
        filt = Gtk.FileFilter()
        filt.set_name("aimless backup (*.json)")
        filt.add_pattern("*.json")
        chooser.add_filter(filt)
        if chooser.run() != Gtk.ResponseType.OK:
            chooser.destroy()
            return None
        path = chooser.get_filename()
        chooser.destroy()
        if not path:
            return None
        bundle_pw = ask_secret(None, "Backup passphrase")
        if not bundle_pw:
            return None
        app_pw = ask_secret(None, "Choose a passphrase for this machine", confirm=True)
        if not app_pw:
            return None
        try:
            restore_bundle(path, bundle_pw, app_pw)
        except (ValueError, OSError, KeyError) as e:
            _error_dialog(None, f"Import failed: {e}")
            return None
        # The daemon booted with a fresh node key; reload the imported one.
        restart_daemon(self.supervisor, self.log)
        if not self.supervisor.is_running():
            _error_dialog(None, "Imported, but the daemon did not restart. "
                          "Quit aimless and reopen.")
            return None
        self.log("imported identity from backup on the init screen")
        return app_pw

    def _no_window_headless(self):
        """True right after setup when there is no window and no usable tray -
        the app has nothing to show and (in a container) must exit so the
        supervisor restarts it, instead of lingering on a black screen."""
        if getattr(self, "_awaiting_connect", False):
            return False
        return self.window is None and not (self.tray is not None and self.tray.is_embedded())

    def _wait_for_tray(self, timeout=TRAY_ADOPT_SECONDS):
        """Give the tray host time to adopt the icon.

        The XEmbed handshake only completes while the GTK main loop runs, so
        is_embedded() is false the instant the StatusIcon is created - the
        app used to read that as "no tray here" and exit right there. On a
        fresh login the panel is still starting, so the autostart entry hit
        that every single time and the tray never came up.

        Returns True as soon as a host adopts the icon.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            while Gtk.events_pending():
                Gtk.main_iteration_do(False)
            if self.tray is not None and self.tray.is_embedded():
                return True
            time.sleep(0.05)
        return False

    # -- main loop ----------------------------------------------------------
    def poll(self):
        if self.quitting:
            return GLib.SOURCE_REMOVE
        self._prune_history()
        if self.supervisor.external:
            if getattr(self, "_awaiting_connect", False):
                return GLib.SOURCE_CONTINUE
            t = self.supervisor.tunnel
            if t is not None and not self._tunnel_restarting and not t.is_running():
                self.log("tunnel: ssh process gone - restarting")
                # Without the tunnel our traffic never reaches the daemon, so
                # the buddies see us as offline.
                self._set_link(False, "the ssh tunnel is down")
                self._schedule_tunnel_restart()
            return GLib.SOURCE_CONTINUE
        if not self.supervisor.is_running():
            self.log("aimlessd died - restarting")
            # The daemon is gone: our client traffic is not landing, so the
            # buddies will see us as offline. Say so right away.
            self._set_link(False, "the daemon died - restarting")
            try:
                self.supervisor.ensure(log=self.log)
            except RuntimeError as e:
                self.log(f"restart failed: {e}")
                return GLib.SOURCE_CONTINUE
            self.rewatch()
            self._probe_link()
        return GLib.SOURCE_CONTINUE

    def heartbeat(self):
        """Tell our daemon a client is attached, even with no window open. The
        daemon advertises presence to buddies only while this keeps arriving, so
        a suspended/quit client shows as offline to them.

        The outcome is our link health too: if this stops landing, the daemon
        drops us to "client offline" within half a minute - and the tray has
        to say so before the buddies notice."""
        if self.quitting:
            return GLib.SOURCE_REMOVE

        def worker():
            d = None
            try:
                d = DaemonClient(sock_path())
                d.request("whoami", timeout=3)
                return True
            except Exception:
                return False
            finally:
                if d is not None:
                    try:
                        d.close()
                    except Exception:
                        pass

        def done(ok):
            if ok:
                self._set_link(True)
            elif getattr(self, "_awaiting_connect", False):
                # Still bringing the tunnel up - the connect window says so,
                # so do not cry wolf before we even tried.
                self._set_link(False, "connecting to the remote daemon")
            else:
                self._set_link(False, "the daemon is not answering")

        run_async(worker, on_done=done)
        return GLib.SOURCE_CONTINUE

    # -- link health ------------------------------------------------------
    def offline_reason(self):
        """Why the buddies cannot reach us right now ("" when they can)."""
        if not self._link_ok:
            return self._link_reason or "the daemon is not answering"
        if self._island:
            return self._island_reason or "the daemon cannot reach the network"
        return ""

    def _set_link(self, ok, reason=""):
        """Record whether our client traffic is landing on the daemon.

        Not ok means the daemon is not hearing us: it will advertise us as
        "client offline" within 30s, which is what the buddies see. The tray
        turns red right away; the notification waits out the grace period, so
        a daemon that restarts in a second does not make noise.
        """
        changed = (ok != self._link_ok) or (reason != self._link_reason)
        self._link_ok = bool(ok)
        self._link_reason = "" if ok else reason
        if ok:
            self._cancel_link_timer()
            if not self._island:
                self._announce_online()
        else:
            # A daemon we cannot talk to says nothing about its peers - drop
            # the island state, the louder problem wins.
            self._set_island(False)
            # While the tunnel is still coming up we are not offline, we are
            # connecting - the connect window says so.
            if (self._link_timer is None
                    and not getattr(self, "_awaiting_connect", False)):
                self._link_timer = GLib.timeout_add_seconds(LINK_GRACE_SECONDS,
                                                            self._link_grace_expired)
        if changed:
            self._push_tray_state()

    def _set_island(self, island, reason=""):
        """The daemon answers, but it has no Yggdrasil peer up.

        That is an island: the node cannot reach the overlay network, so
        nobody can reach it either. The buddies see us as offline even though
        the daemon is right there answering.
        """
        changed = (island != self._island) or (reason != self._island_reason)
        self._island = bool(island)
        self._island_reason = reason if island else ""
        if not island:
            self._cancel_island_timer()
        if island:
            self._announce_offline(self._island_reason)
        elif self._link_ok:
            self._announce_online()
        if changed:
            self._push_tray_state()

    def _announce_offline(self, reason):
        """Say it once: the buddies cannot reach us."""
        if self._offline_notified:
            return
        self._offline_notified = True
        self.log(f"offline - {reason}")
        self._notify("AIMless is offline",
                     f"{reason} - your buddies see you as offline")

    def _announce_online(self):
        """The other half of the announcement: we are reachable again."""
        if not self._offline_notified:
            return
        self._offline_notified = False
        self._notify("AIMless is back online",
                     "the daemon is answering again - your buddies can see you")

    def _cancel_link_timer(self):
        if self._link_timer is not None:
            try:
                GLib.source_remove(self._link_timer)
            except Exception:
                pass
            self._link_timer = None

    def _link_grace_expired(self):
        """The daemon stayed silent past the grace period - call it offline."""
        self._link_timer = None
        if self._link_ok:
            return GLib.SOURCE_REMOVE
        if getattr(self, "_awaiting_connect", False):
            # Still connecting to the remote daemon - not an outage yet.
            return GLib.SOURCE_REMOVE
        self._announce_offline(self._link_reason or "the daemon is not answering")
        return GLib.SOURCE_REMOVE

    def _cancel_island_timer(self):
        if self._island_timer is not None:
            try:
                GLib.source_remove(self._island_timer)
            except Exception:
                pass
            self._island_timer = None

    def _island_grace_expired(self):
        """Re-check the peers before crying wolf.

        The daemon may have come back - with its peers - while the timer ran.
        A fresh status request decides, not the old reading.
        """
        self._island_timer = None
        if not self._link_ok:
            return GLib.SOURCE_REMOVE
        self._fetch_peers(force=True)
        return GLib.SOURCE_REMOVE

    def check_peers(self):
        """Watch the daemon's Yggdrasil peers.

        The daemon answering says nothing about whether the node is on the
        network: a daemon with no peer up cannot reach the overlay, so the
        buddies cannot reach us - while everything on this side looks fine.
        That is the "no peers" failure, and the tray has to show it.
        """
        if self.quitting:
            return GLib.SOURCE_REMOVE
        if not self._link_ok:
            # The daemon is not answering at all - that is the louder problem.
            return GLib.SOURCE_CONTINUE
        self._fetch_peers()
        return GLib.SOURCE_CONTINUE

    def _fetch_peers(self, force=False):
        """Ask the daemon how its peers are doing."""

        def worker():
            return self.supervisor.status()

        def done(st):
            self._apply_peers(st, force)

        run_async(worker, on_done=done)

    def _apply_peers(self, st, force=False):
        """Turn a peer reading into the island state."""
        if not st:
            # The status request itself failed - the link check owns that
            # case, and guessing here would clear a real island.
            return
        win = getattr(self, "window", None)
        if win is not None:
            win._update_peers_label(st)
        peers = st.get("peers_total", 0)
        up = st.get("peers_up", 0)
        if up > 0:
            self._cancel_island_timer()
            self._set_island(False)
            self._set_peer_risk(up, peers, st)
            return
        # No peer is up - so the node cannot reach the overlay and the
        # buddies cannot reach us. With no peers configured at all there is
        # nothing to wait for either: the node is simply cut off, and the
        # tray has to say so (the window already says "connecting").
        if is_tailcat(st):
            reason = "relay not reachable - AIMless cannot be reached"
        else:
            reason = ("no peers configured - AIMless cannot reach the network"
                      if peers <= 0 else
                      "no Yggdrasil peers - the daemon is on an island")
        # Give the daemon a moment before crying wolf: a restart comes up
        # with no peers for a second or two.
        if force:
            self._set_island(True, reason)
        elif self._island_timer is None and not self._island:
            self._island_timer = GLib.timeout_add_seconds(ISLAND_GRACE_SECONDS,
                                                          self._island_grace_expired)

    def _set_peer_risk(self, up, total, st=None):
        """Say something when only one peer is left.

        Losing a few peers is not worth a warning: the node is still on the
        network and still has more than one way in and out. But when exactly
        one link is left, that link is the only way through - one failure away
        from being cut off - so the tray says so (amber), and says it once.

        A daemon that was asked for a single peer is not warned about: it has
        all the redundancy it asked for.
        """
        at_risk = (not is_tailcat(st)) and (up == 1 and total > 1)
        reason = ("only one peer is up - if it drops, AIMless cannot be reached"
                  if at_risk else "")
        if at_risk == self._peer_risk:
            return
        self._peer_risk = at_risk
        tray = getattr(self, "tray", None)
        if tray is not None:
            try:
                tray.set_warn(at_risk, reason)
            except Exception:
                pass
        if at_risk:
            self._notify(APP_NAME, "only one Yggdrasil peer is up - if it drops, "
                                  "AIMless cannot be reached")

    def _push_tray_state(self):
        tray = getattr(self, "tray", None)
        if tray is None or not getattr(tray, "have_tray", False):
            return
        reason = self.offline_reason()
        try:
            tray.set_offline(bool(reason), reason)
        except Exception:
            pass

    def _probe_link(self, reason="the daemon is not answering"):
        """Ask in the background whether the daemon still hears us."""

        def worker():
            return self.supervisor._reachable()

        def done(ok):
            self._set_link(bool(ok), reason)

        run_async(worker, on_done=done)

    def _notify(self, title, body):
        icon = appicon.ICON_NAME if not self.offline_reason() else appicon.OFFLINE_ICON_NAME
        try:
            desktop_notify(title, body, icon)
        except Exception:
            pass

    # -- messages waiting while the app is locked --------------------------
    def _check_pending(self):
        """Count the messages that arrived while the app was locked.

        The app cannot decrypt anything before the passphrase is typed, but the
        daemon journals every message it receives - so the tray can still say
        "a message is waiting". The count is what the daemon journaled past our
        own watermark; the watermark is not touched, so unlocking still counts
        the same messages as unread.

        Once a session exists the real unread counters own the badge and this
        stops.
        """
        if self.quitting:
            return GLib.SOURCE_REMOVE
        if self.session is not None:
            self._pending_timer = None
            return GLib.SOURCE_REMOVE
        if self._pending_busy:
            return GLib.SOURCE_CONTINUE
        self._pending_busy = True

        def worker():
            d = DaemonClient(sock_path())
            try:
                return pending.pending_count(d, paths.contacts_path(), paths.cache_path())
            finally:
                try:
                    d.close()
                except Exception:
                    pass

        def done(n):
            self._pending_busy = False
            self._pending_error = ""
            self._apply_pending(n)

        def fail(e):
            self._pending_busy = False
            # Log a changed error once, not every round: a dead daemon already
            # has the tray saying so.
            msg = repr(e)
            if msg != self._pending_error:
                self._pending_error = msg
                self.log(f"pending count failed: {msg}")

        run_async(worker, on_done=done, on_error=fail)
        return GLib.SOURCE_CONTINUE

    def _apply_pending(self, n):
        """Put the waiting-message count on the tray (locked app only)."""
        if self.session is not None:
            return
        n = int(n or 0)
        if n == self._pending_count:
            return
        grew = n > self._pending_count
        self._pending_count = n
        if grew or n == 0:
            self.log(f"{n} message(s) waiting (app is locked)")
        tray = getattr(self, "tray", None)
        if tray is not None:
            try:
                tray.set_unread(n, locked=True)
            except Exception:
                pass
        if grew and n:
            self._notify(APP_NAME,
                         f"{n} new message(s) waiting - click the tray icon to unlock "
                         "AIMless and read them")

    def _announce_detached_gap(self):
        """Tell the user how long they were away, if it was long enough to
        matter: the buddies saw "client offline" for that whole time.

        Runs from a timer, so it returns False to fire only once.
        """
        gap = self._detached_gap
        if not gap or gap < 60:
            return False
        self.log(f"was detached for {fmt_duration(gap)} before this start")
        if gap >= DETACHED_NOTICE_SECONDS:
            self._notify(
                "AIMless was offline",
                f"you were detached for {fmt_duration(gap)} - your buddies saw you "
                "as offline that whole time")
        return False

    def rewatch(self):
        if not self.session:
            return
        recipients = self._status_recipients()

        def worker():
            for info in recipients:
                try:
                    self.session.client.add_contact(info["node"], info.get("addr", ""))
                except Exception:
                    pass

        run_async(worker)

    def quit(self, *_):
        if self.quitting:
            return GLib.SOURCE_REMOVE
        self.quitting = True
        self._awaiting_connect = False
        self.log("shutting down - stopping aimlessd")
        if self.window:
            try:
                self.window.save_geometry()
            except Exception:
                pass
        try:
            self.supervisor.stop()
        except Exception:
            pass
        if self.lock_fh:
            try:
                self.lock_fh.close()
            except Exception:
                pass
        try:
            os.remove(paths.app_pid_path())
        except OSError:
            pass
        Gtk.main_quit()
        return GLib.SOURCE_REMOVE
