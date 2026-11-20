"""The main window: tabs, route/status bars, identity and relay dialogs, tray.

Extracted from gtkui.py. Uses the shared UI layer (uibase) and the extracted
view/dialog/relay modules. ContactsView and ActivityView still live in gtkui and
are imported lazily in __init__ to avoid a circular import.
"""
import base64
import json
import os
import shutil
from datetime import datetime

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib, Gdk, Pango

from . import crypto, paths, protocol, uibase
from . import keys as keysmod
from . import __version__ as client_version
from .daemon import DaemonError
from .dialogs import ask_secret, ask_text
from .identity import restore_bundle
from .messages import MessagesView
from .views import ActivityView, ContactsView
from .notify import play_notification_sound, sound_enabled
from .prefs import load_prefs, pref, save_prefs
from .relay import is_tailcat, net_label, relay_tooltip, short_address
from .relayctl import restart_daemon
from .store import Store
from .theme import THEMES, THEME_ENTRIES
from .uibase import (APP_NAME, C, STATUS_REASSERT_SECONDS, _add_contact_from_roster,
                     _self_state, attachments_dir, clamp_to_workarea, contacts_path,
                     first_icon, identity_path, run_async, set_theme, setup_app_icon)


class AimlessWindow(Gtk.Window):
    def __init__(self, session, supervisor, app_ref=None):
        super().__init__(title=APP_NAME)
        self.session = session
        self.supervisor = supervisor
        self.app_ref = app_ref
        self.get_style_context().add_class("aimless-window")
        setup_app_icon(self)
        self.prefs = load_prefs()
        self.set_default_size(self.prefs.get("window_width", 1008), self.prefs.get("window_height", 723))
        self.connect("delete-event", self.on_delete)

        header_bar = Gtk.HeaderBar()
        header_bar.set_show_close_button(True)
        header_bar.set_title(APP_NAME)
        header_bar.set_subtitle(f"v{client_version}")
        self.set_titlebar(header_bar)

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)

        self.messages = MessagesView(self)
        self.stack.add_titled(self.messages, "messages", "Messages")

        self.contacts = ContactsView(self)
        self.stack.add_titled(self.contacts, "contacts", "Contacts")

        self.activity = ActivityView(self)
        self.stack.add_titled(self.activity, "activity", "Activity")

        switcher = Gtk.StackSwitcher()
        switcher.set_stack(self.stack)
        header_bar.set_custom_title(switcher)

        menu_button = Gtk.MenuButton()
        menu_button.set_image(Gtk.Image.new_from_icon_name("open-menu-symbolic", Gtk.IconSize.BUTTON))
        header_bar.pack_end(menu_button)

        self.away_banner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.away_banner.set_border_width(8)
        self.away_banner.get_style_context().add_class("aimless-away-banner")
        self.away_icon = Gtk.Image.new_from_icon_name("weather-clear-night-symbolic", Gtk.IconSize.MENU)
        self.away_banner.pack_start(self.away_icon, False, False, 0)
        self.away_label = Gtk.Label(label="")
        self.away_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.away_label.set_xalign(0.0)
        self.away_banner.pack_start(self.away_label, True, True, 0)
        away_back = Gtk.Button(label="I'm back")
        away_back.set_relief(Gtk.ReliefStyle.NONE)
        away_back.connect("clicked", lambda *_: self.set_away(None))
        self.away_banner.pack_start(away_back, False, False, 0)
        self.away_banner.set_no_show_all(True)
        self.away_banner.hide()

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        if not pref(self.prefs, "seen_onboarding") and not session.contacts():
            info = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            info.set_border_width(8)
            info.get_style_context().add_class("aimless-onboarding")
            hint = Gtk.Label(
                label="New to AIMless? Open Contacts to copy your invite and add a buddy.")
            hint.set_xalign(0.0)
            hint.set_line_wrap(True)
            info.pack_start(hint, True, True, 0)

            def dismiss_info(*_):
                info.hide()
                self.prefs["seen_onboarding"] = True
                save_prefs(self.prefs)

            go_btn = Gtk.Button(label="Go to Contacts")
            go_btn.connect("clicked", lambda *_: (dismiss_info(),
                                                  self.stack.set_visible_child_name("contacts")))
            dismiss_btn = Gtk.Button(label="Dismiss")
            dismiss_btn.connect("clicked", dismiss_info)
            info.pack_start(go_btn, False, False, 0)
            info.pack_start(dismiss_btn, False, False, 0)
            root.pack_start(info, False, False, 0)

        root.pack_start(self.away_banner, False, False, 0)
        root.pack_start(self.stack, True, True, 0)

        route_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        route_bar.set_border_width(6)
        route_bar.get_style_context().add_class("aimless-route-bar")
        self.route_icon = Gtk.Image.new_from_icon_name(
            first_icon("network-wireless-signal-excellent-symbolic", "applications-internet"), Gtk.IconSize.MENU)
        route_bar.pack_start(self.route_icon, False, False, 0)
        self.route_label = Gtk.Label(label="daemon: starting …")
        route_bar.pack_start(self.route_label, False, False, 0)
        # Two labels, not one: the name gets the normal text colour and the
        # key the muted one, and both come from the theme's CSS - so a theme
        # switch repaints them instead of leaving the old palette behind.
        # (Pango markup cannot carry a CSS class, so this cannot be one
        # label with two colours.)
        self.self_id_label = Gtk.Label()
        self.self_id_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.self_key_label = Gtk.Label()
        self.self_key_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.self_key_label.get_style_context().add_class("muted")
        route_bar.pack_end(self.self_key_label, False, False, 0)
        route_bar.pack_end(self.self_id_label, False, False, 0)
        root.pack_start(route_bar, False, False, 0)
        self._self_id = None

        self.add(root)

        accel = Gtk.AccelGroup()
        self.add_accel_group(accel)
        for key, view in (("1", "messages"), ("2", "contacts"), ("3", "activity")):
            accel.connect(Gdk.keyval_from_name(key), Gdk.ModifierType.CONTROL_MASK, Gtk.AccelFlags.VISIBLE,
                          lambda *_, v=view: self.stack.set_visible_child_name(v))

        options_menu = Gtk.Menu()
        away_item = Gtk.MenuItem(label="Set away …")
        away_item.connect("activate", self.on_set_away)
        options_menu.append(away_item)
        avail_item = Gtk.MenuItem(label="Available")
        avail_item.connect("activate", lambda *_: self.set_away(None))
        options_menu.append(avail_item)
        options_menu.append(Gtk.SeparatorMenuItem())
        keys_item = Gtk.MenuItem(label="Keys & Identity …")
        keys_item.connect("activate", self.on_keys)
        options_menu.append(keys_item)
        peers_item = Gtk.MenuItem(label="Peers …")
        self.peers_item = peers_item
        peers_item.connect("activate", self.on_peers)
        options_menu.append(peers_item)
        prefs_item = Gtk.MenuItem(label="Preferences …")
        prefs_item.connect("activate", self.on_preferences)
        options_menu.append(prefs_item)
        options_menu.append(Gtk.SeparatorMenuItem())
        quit_item = Gtk.MenuItem(label="Close window")
        quit_item.connect("activate", lambda *_: self.close())
        options_menu.append(quit_item)
        options_menu.show_all()
        menu_button.set_popup(options_menu)

        self.connect("destroy", self.on_destroy)
        self._geometry_restoring = False
        self._geometry_timer = 0
        self._want_geometry = False
        self.connect("configure-event", self.on_configure)
        self.connect("map-event", self._on_map)
        self.connect("notify::is-active", self._on_active_changed)
        self._presence_busy = False
        self._status_busy = False
        self._request_open = False
        self._daemon_user_stopped = False
        saved_away = self.prefs.get("away", "")
        if saved_away:
            self._apply_away_banner(saved_away)
        self.contacts.refresh()
        self.poll_status()

        self.stack.connect("notify::visible-child-name", self.on_view_changed)

        GLib.timeout_add(150, self.drain_events)
        GLib.timeout_add_seconds(3, self.poll_presence)
        GLib.timeout_add_seconds(5, self.poll_status)
        GLib.timeout_add(1200, self._catchup_once)
        GLib.timeout_add(2500, self._catchup_attachments_once)

        def watch_all():
            for info in self.session.contacts().values():
                try:
                    self.session.client.add_contact(info["node"])
                except DaemonError:
                    pass

        run_async(watch_all)

        self._push_status(self.prefs.get("away") or None)
        GLib.timeout_add_seconds(STATUS_REASSERT_SECONDS, self._reassert_status)
        GLib.idle_add(self.surface_pending_requests)

        self.apply_theme(os.environ.get("AIMLESS_THEME") or pref(self.prefs, "theme"))
        try:
            settings = Gtk.Settings.get_default()
            for prop in ("gtk-theme-name", "gtk-application-prefer-dark-theme"):
                settings.connect(f"notify::{prop}", self._on_system_theme)
        except Exception:
            pass

    def _ask_request(self, req):
        is_room = bool(req.get("conv"))
        if is_room:
            others = [m.get("screen") or m["node"][:8] for m in req.get("members", [])
                      if m["node"] != self.session.self_node]
            text = f"{req['screen']} invited you to a conversation with {', '.join(others)}"
        else:
            text = f"{req['screen']} wants to chat with you"
        dlg = Gtk.MessageDialog(transient_for=self, modal=True,
                                message_type=Gtk.MessageType.QUESTION,
                                buttons=Gtk.ButtonsType.NONE, text=text)
        dlg.format_secondary_text((req.get("text") or "")[:300])
        dlg.add_buttons("Deny", Gtk.ResponseType.REJECT,
                        "Accept", Gtk.ResponseType.ACCEPT,
                        "Block", Gtk.ResponseType.NO)
        dlg.set_default_response(Gtk.ResponseType.ACCEPT)
        resp = dlg.run()
        dlg.destroy()
        return resp

    def daemon_block_set(self, node, blocked, on_done=None):
        """Best-effort daemon-side block/unblock (fire-and-forget). Against a
        pre-0.3.4 daemon the op is unknown; fail quietly, leaving the client-side
        mute in charge."""
        if blocked:
            def worker():
                return self.session.client.block(node)
        else:
            def worker():
                return self.session.client.unblock(node)

        def fail(e):
            self.activity.log(f"daemon {'block' if blocked else 'unblock'} failed: {e} - client-side only")

        def done(_r):
            if on_done is not None:
                on_done()

        run_async(worker, on_done=done, on_error=fail)

    def surface_pending_requests(self):
        if getattr(self, "_request_open", False):
            return GLib.SOURCE_REMOVE
        req = self.session.cache.pending_pop()
        if req is None:
            return GLib.SOURCE_REMOVE
        self._request_open = True
        try:
            choice = self._ask_request(req)
        finally:
            self._request_open = False
        if choice == Gtk.ResponseType.ACCEPT:
            petname = _add_contact_from_roster(req["node"], req["pubkey"], req.get("screen"), req.get("addr", ""))
            self.session.cache.unmute(req["node"])
            self.daemon_block_set(req["node"], blocked=False)
            conv = req.get("conv") or req["node"]
            members = {m["node"]: {"node": m["node"], "pubkey": m["pubkey"],
                                   "screen": m.get("screen", ""), "addr": m.get("addr", "")}
                       for m in (req.get("members") or [])}
            if members:
                # The user accepted this request, so this roster is approved.
                # store.ensure_room never replaces the roster of a
                # conversation that already exists, so a request cannot
                # hijack a room we are in.
                self.session.cache.ensure_room(req["conv"], members)
            self.session.cache.add_recv(conv, req["node"],
                                        req.get("seq", 0), req.get("ts", 0), req.get("text", ""))
            self.contacts.refresh()
            self.messages.sync_sidebar()
            # The withheld message is now stored: redraw it if its conversation
            # is already open, otherwise it only appears after switching threads.
            self.messages.refresh_conversation(conv)
            self.activity.log(f"added {petname}")
        elif choice == Gtk.ResponseType.NO:
            self.session.cache.mute(req["node"])
            self.session.cache.set_blocked_screen(req["node"], req.get("screen") or req["node"][:8])
            self.daemon_block_set(req["node"], blocked=True, on_done=self.contacts.refresh)
            self.activity.log(f"blocked {req.get('screen') or req['node'][:8]}")
        else:
            # Deny is a one-time soft decline: this request is discarded and the
            # sender stays a stranger, so their next message re-prompts. Block is
            # the persistent option.
            self.activity.log(f"denied {req.get('screen') or req['node'][:8]}")
        GLib.idle_add(self.surface_pending_requests)
        return GLib.SOURCE_REMOVE

    def on_view_changed(self, stack, param):
        if stack.get_visible_child_name() == "contacts":
            self.contacts.refresh()

    def on_set_away(self, *_):
        away = ask_text(self, "Away message", "Away message (empty = available):")
        self.set_away(away.strip() if away and away.strip() else None)

    def set_away(self, away):
        self._apply_away_banner(away)
        self.prefs["away"] = away or ""
        save_prefs(self.prefs)
        self._push_status(away, log_status=True)

    def _status_recipients(self):
        """Everyone who should hear our status: buddies plus every room member.

        A room can include people who are not buddies. Status is sealed to each
        recipient's own key, so every one of them needs their own copy - the
        daemon cannot forward a payload sealed for somebody else. Deduped by
        node so a member who is also a buddy is not sent two.
        """
        by_node = {}
        for info in self.session.contacts().values():
            if info.get("node") and info.get("pubkey"):
                by_node[info["node"]] = info
        for thread in self.messages.threads.values():
            if not thread.get("is_room"):
                continue
            for node, m in thread.get("members", {}).items():
                if node == self.session.self_node or node in by_node:
                    continue
                if m.get("pubkey"):
                    by_node[node] = {"node": node, "pubkey": m["pubkey"],
                                     "screen": m.get("screen", ""), "addr": m.get("addr", "")}
        return list(by_node.values())

    def _push_status(self, away, log_status=False):
        recipients = self._status_recipients()

        def worker():
            errors = []
            try:
                addr = self.session.client.whoami().get("address", "")
            except Exception:
                addr = ""
            if not addr.startswith("tc"):
                addr = ""  # only tailcat dial addresses are worth propagating
            for info in recipients:
                try:
                    self.session.client.set_status(info["pubkey"], info["node"], away, addr=addr)
                except Exception as e:
                    errors.append(f"{info.get('screen', '?')}: {e}")
            return errors

        def done(errors):
            if errors:
                self.activity.log("setstatus failed: " + "; ".join(errors))
            if log_status:
                self.activity.log("away: " + away if away else "available")

        run_async(worker, on_done=done)

    def _reassert_status(self):
        """Status is ephemeral daemon RAM on both ends - re-announce the current
        status so buddies converge after any restart (ours or theirs)."""
        self._push_status(self.prefs.get("away") or None)
        return GLib.SOURCE_CONTINUE

    def _apply_away_banner(self, away):
        if away:
            self.away_icon.set_from_icon_name("weather-clear-night-symbolic", Gtk.IconSize.MENU)
            self.away_label.set_markup(
                f"<b>Away</b> - {GLib.markup_escape_text(away)}  "
                f"<span size='small'>(buddies see this as your away message)</span>")
            for child in self.away_banner.get_children():
                child.show()
            self.away_banner.show()
        else:
            self.away_banner.hide()

    def drain_events(self):
        try:
            while True:
                ev = self.session.daemon.next_event(timeout=0)
                if ev is None:
                    break
                if ev.get("op") == "recv":
                    self.messages.incoming(ev)
                elif ev.get("op") == "acked":
                    if not self.messages.note_acked(ev.get("to"), ev.get("seq")):
                        self.activity.log(f"delivered: seq {ev.get('seq')} → {str(ev.get('to'))[:16]}…")
        except Exception as e:
            try:
                self.activity.log(f"event error: {e}")
            except Exception:
                pass
        return True

    def _catchup_once(self):
        # Retry until the sweep actually runs: the text sweep fetches one
        # history per peer and can still be in flight when the timer fires.
        if not self.messages.catchup_unread():
            return GLib.SOURCE_CONTINUE
        return GLib.SOURCE_REMOVE

    def _catchup_attachments_once(self):
        # Same for the attachment sweep - it has its own busy flag now, so
        # the two sweeps no longer cancel each other.
        if not self.messages.catchup_attachments():
            return GLib.SOURCE_CONTINUE
        return GLib.SOURCE_REMOVE


    def poll_presence(self):
        if self._presence_busy:
            return True
        self._presence_busy = True

        def worker():
            presence = {p["key"]: p for p in self.session.client.presence(timeout=3)}
            known = {c["node"] for c in self.session.contacts().values()}
            self.session.client.sync_contact_addrs(presence, known_nodes=known)
            return presence

        def done(presence):
            self._presence_busy = False
            self.messages.refresh_presence(presence)
            self.contacts.refresh_presence(presence)

        def fail(_e):
            self._presence_busy = False
            presence = {}
            for t in self.messages.threads.values():
                t["online"] = False
            # Both tabs have to hear about it, or they disagree about the
            # same people: the sidebar greys out, the contacts tab keeps the
            # last green it had.
            self.messages.refresh_presence(presence)
            self.contacts.refresh_presence(presence)

        run_async(worker, on_done=done, on_error=fail)
        return True

    def poll_status(self):
        if self._status_busy:
            return True
        self._status_busy = True

        def worker():
            running = self.supervisor.is_running()
            return running, self.supervisor.status()

        def done(result):
            self._status_busy = False
            running, st = result
            self.activity.refresh_info(st)
            self.refresh_route(st)

        def fail(_e):
            self._status_busy = False

        run_async(worker, on_done=done, on_error=fail)
        return True

    def _set_route_icon(self, name):
        try:
            self.route_icon.set_from_icon_name(name, Gtk.IconSize.MENU)
        except Exception:
            pass

    def _tunnel_tooltip(self, t):
        lines = [
            f"SSH tunnel: {'up' if t['up'] else 'down'}",
            f"remote: {t['host']} {t['remote_socket']}",
            f"local:  {t['local_socket']}",
            f"restarts: {t['restarts']}",
        ]
        if t.get("last_error"):
            lines.append(f"last error: {t['last_error']}")
        return "\n".join(lines)

    def refresh_self_id(self):
        if not self.session:
            return
        screen = self.session.self_screen or "anonymous"
        node = self.session.self_node or ""
        if self._self_id == (screen, node):
            return
        self._self_id = (screen, node)
        short = f"{node[:16]}…" if node else "-"
        # No hardcoded colours here: the theme's CSS gives the name the normal
        # text colour and the key the muted one.
        self.self_id_label.set_text(f"you: {screen}")
        self.self_key_label.set_text(f"·  {short}")
        tip = f"node key: {node}"
        if self.session.pubkey_hex:
            tip += f"\nclient pubkey: {self.session.pubkey_hex}"
        self.self_id_label.set_tooltip_text(tip)
        self.self_key_label.set_tooltip_text(tip)

    def _update_peers_label(self, st):
        """Name the options-menu item for the transport: it opens the peer list
        (Yggdrasil) or the relay panel (tailcat). The item lives on the window."""
        if getattr(self, "peers_item", None) is not None:
            self.peers_item.set_label("Relay …" if is_tailcat(st) else "Peers …")

    def refresh_route(self, st):
        tunnel = self.supervisor.tunnel_state() if self.supervisor else None
        if tunnel is not None and not tunnel["up"]:
            host = GLib.markup_escape_text(tunnel["host"])
            self.route_label.set_markup(
                f"<span foreground='{C['danger']}'>●  tunnel down</span>  -  {host} · reconnecting…")
            self.route_label.set_tooltip_text(self._tunnel_tooltip(tunnel))
            self._set_route_icon(first_icon(
                "network-wireless-signal-none-symbolic", "network-offline-symbolic",
                "network-error-symbolic"))
        elif not st:
            if tunnel is not None:
                host = GLib.markup_escape_text(tunnel["host"])
                self.route_label.set_markup(
                    f"<span foreground='{C['away']}'>●  remote daemon unreachable</span>"
                    f"  -  tunnel {host}")
                self.route_label.set_tooltip_text(self._tunnel_tooltip(tunnel))
            else:
                self.route_label.set_markup(
                    f"<span foreground='{C['danger']}'>●  offline - daemon not reachable</span>")
                self.route_label.set_tooltip_text(None)
            self._set_route_icon(first_icon(
                "network-wireless-signal-none-symbolic", "network-offline-symbolic",
                "network-error-symbolic"))
        else:
            self._update_peers_label(st)
            tail = ""
            if tunnel is not None:
                tail = f"  ·  ↗ {GLib.markup_escape_text(tunnel['host'])}"
                self.route_label.set_tooltip_text(self._tunnel_tooltip(tunnel))
            elif not is_tailcat(st) and st.get("peers_up", 0) == 1 and st.get("peers_total", 0) > 1:
                self.route_label.set_tooltip_text(
                    "only one Yggdrasil peer is up - if it drops, AIMless cannot be reached")
            else:
                self.route_label.set_tooltip_text(relay_tooltip(st) if is_tailcat(st) else (st.get("address") or None))
            colour, text = _self_state(st)
            nl = net_label(st)
            net_part = f"  ·  {nl}" if nl else ""
            self.route_label.set_markup(
                f"<span foreground='{colour}'>●  {text}</span>  -  {short_address(st['address'])}{net_part}{tail}")
            if st.get("peers_up", 0) > 0:
                self._set_route_icon(first_icon(
                    "network-wireless-signal-excellent-symbolic", "applications-internet"))
            else:
                self._set_route_icon(first_icon(
                    "network-wireless-signal-weak-symbolic", "network-wireless-signal-ok-symbolic",
                    "network-idle-symbolic"))
        self.refresh_self_id()

    # -- Keys & Identity --------------------------------------------------
    def _info_dialog(self, title, text, secondary=None, parent=None):
        dlg = Gtk.MessageDialog(transient_for=parent or self, modal=True,
                                message_type=Gtk.MessageType.INFO,
                                text=title, buttons=Gtk.ButtonsType.OK)
        if secondary:
            dlg.format_secondary_text(secondary)
        dlg.run()
        dlg.destroy()

    def _confirm(self, title, secondary, parent=None):
        dlg = Gtk.MessageDialog(transient_for=parent or self, modal=True,
                                message_type=Gtk.MessageType.WARNING,
                                text=title, buttons=Gtk.ButtonsType.OK_CANCEL)
        dlg.format_secondary_text(secondary)
        dlg.set_default_response(Gtk.ResponseType.CANCEL)
        resp = dlg.run()
        dlg.destroy()
        return resp == Gtk.ResponseType.OK

    def _backup_payload(self):
        node_seed = None
        try:
            with open(os.path.join(paths.daemon_dir(), "node.key")) as f:
                node_seed = f.read().strip()
        except OSError:
            pass
        tailcat_key = keysmod.read_tailcat_key(paths.daemon_dir())
        relay = None
        try:
            with open(os.path.join(paths.daemon_dir(), "config.json")) as f:
                relay = (json.load(f) or {}).get("relay")
        except (OSError, ValueError):
            pass
        return {
            "kind": "identity",
            "identitySeed": bytes(self.session.identity).hex(),
            "nodeSeed": node_seed,
            "tailcatKey": tailcat_key,
            "relay": relay,
            "screen": self.session.self_screen,
            "pubkey": self.session.client.pubkey_hex,
            "contacts": protocol.load_contacts(contacts_path()),
        }

    def export_backup_to(self, path, passphrase):
        """Write the encrypted backup bundle to an explicit path (testable)."""
        keysmod.save_bundle(path, passphrase, self._backup_payload())

    def restore_backup_from(self, path, passphrase, app_passphrase):
        """Restore a bundle onto disk (no daemon restart). Returns the bundle.
        Raises ValueError on a bad passphrase/missing fields."""
        return restore_bundle(path, passphrase, app_passphrase)

    def export_data_backup_to(self, path, backup_pw, include_attachments=True):
        """Write an encrypted data backup (rooms + history, optionally attachment
        files). The store is re-keyed to the backup passphrase so it is portable
        across machines/passphrases. Returns {messages, rooms} counts."""
        import sqlite3
        import tempfile
        from .store import rekey_file
        app_pw = self.app_ref.passphrase if self.app_ref else None
        if not app_pw:
            raise ValueError("app passphrase unavailable")
        store = self.session.store
        adir = attachments_dir()
        tmpdir = tempfile.mkdtemp(prefix="aimless-data-")
        try:
            snap = os.path.join(tmpdir, "state.db")
            store.snapshot_to(snap)
            sconn = sqlite3.connect(snap)
            counts = {
                "messages": sconn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
                "rooms": sconn.execute(
                    "SELECT COUNT(*) FROM conversations WHERE dm=0 AND hidden=0").fetchone()[0],
            }
            sconn.close()
            attachments = {}
            if include_attachments:
                # Make attachment paths relative so they survive a different home.
                snap_store = Store(snap, app_pw)
                snap_store.map_attachment_paths(lambda p: os.path.relpath(p, adir))
                snap_store.close()
                if os.path.isdir(adir):
                    for root, _, files in os.walk(adir):
                        for name in files:
                            fp = os.path.join(root, name)
                            rel = os.path.relpath(fp, adir)
                            with open(fp, "rb") as f:
                                attachments[rel] = base64.b64encode(f.read()).decode("utf-8")
            rekey_file(snap, app_pw, backup_pw, os.urandom(16))
            with open(snap, "rb") as f:
                db_bytes = f.read()
            keysmod.save_data_bundle(path, backup_pw, db_bytes, attachments, counts)
            return counts
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def restore_data_backup_from(self, path, backup_pw, app_pw):
        """Merge a data backup into the current store (dedup; rosters kept) and
        restore attachment files. Returns the backup's counts."""
        import tempfile
        from .store import rekey_file
        data = keysmod.load_data_bundle(path, backup_pw)
        store = self.session.store
        adir = attachments_dir()
        tmpdir = tempfile.mkdtemp(prefix="aimless-restore-")
        try:
            tmp_db = os.path.join(tmpdir, "restore.db")
            with open(tmp_db, "wb") as f:
                f.write(base64.b64decode(data["db"]))
            rekey_file(tmp_db, backup_pw, app_pw, store.salt)
            if data.get("attachments"):
                rstore = Store(tmp_db, app_pw)
                rstore.map_attachment_paths(lambda p: os.path.join(adir, p))
                rstore.close()
            store.merge_from(tmp_db)
            for rel, b64 in (data.get("attachments") or {}).items():
                fp = os.path.normpath(os.path.join(adir, rel))
                if not fp.startswith(os.path.abspath(adir) + os.sep):
                    continue  # refuse to write outside the attachments dir
                os.makedirs(os.path.dirname(fp), exist_ok=True)
                if not os.path.exists(fp):
                    with open(fp, "wb") as f:
                        f.write(base64.b64decode(b64))
            return data.get("counts") or {}
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def _daemon_restart(self):
        restart_daemon(self.supervisor, self.app_ref.log if self.app_ref else None)

    def _apply_node_seed(self, seed):
        keysmod.write_node_key(paths.daemon_dir(), seed)
        self._daemon_restart()
        try:
            self.session.refresh_node()
        except Exception:
            pass
        self.messages.sync_sidebar()

    def on_peers(self, *_):
        """The peer manager: which nodes we reach the network through, and how
        each one is doing."""
        app = getattr(self, "app_ref", None)
        if app is None:
            return
        app.show_peers(self)

    def on_keys(self, *_):
        st = self.supervisor.status() or {}
        dlg = Gtk.Dialog(title="Keys & Identity", transient_for=self, modal=True)
        dlg.add_button("Close", Gtk.ResponseType.CLOSE)
        dlg.set_default_size(620, -1)
        box = dlg.get_content_area()
        box.set_spacing(10)
        box.set_border_width(14)

        def section(text):
            frame = Gtk.Frame(label=text)
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            inner.set_border_width(10)
            frame.add(inner)
            box.pack_start(frame, False, False, 0)
            return inner

        # --- Yggdrasil node key ---
        node = section("tailcat address" if is_tailcat(st) else "Yggdrasil node key - your address")
        node_status = Gtk.Label(label=f"current: {st.get('address', '?')}")
        node_status.set_xalign(0.0)
        node_status.set_line_wrap(True)
        node.pack_start(node_status, False, False, 0)
        node_entry = Gtk.Entry(placeholder_text="paste a 64- or 128-hex node key")
        node.pack_start(node_entry, False, False, 0)
        node_preview = Gtk.Label(label="")
        node_preview.set_xalign(0.0)
        node_preview.set_line_wrap(True)
        node_preview.get_style_context().add_class("muted")
        node.pack_start(node_preview, False, False, 0)

        def preview_node(*_):
            try:
                _seed, pub = keysmod.parse_node_key(node_entry.get_text())
            except ValueError as e:
                node_preview.set_text(f"error: {e}")
                return
            node_preview.set_text(f"address: {keysmod.yggdrasil_address(pub)}\n"
                                  f"pubkey: {pub.hex()}")

        def apply_node(*_):
            try:
                seed, pub = keysmod.parse_node_key(node_entry.get_text())
            except ValueError as e:
                node_preview.set_text(f"error: {e}")
                return
            addr = keysmod.yggdrasil_address(pub)
            if not self._confirm("Replace your Yggdrasil node key?",
                                 f"Your address becomes {addr}. Anyone who saved your old "
                                 "invite must add you again. The daemon restarts now.",
                                 parent=dlg):
                return
            try:
                self._apply_node_seed(seed)
            except OSError as e:
                self._info_dialog("Could not write node key", str(e), parent=dlg)
                return
            node_status.set_text(f"current: {addr}")
            self._info_dialog("Node key applied", f"Your address is now {addr}.", parent=dlg)

        def gen_node(*_):
            seed_hex, addr = keysmod.random_node_key()
            node_entry.set_text(seed_hex)
            node_preview.set_text(f"address: {addr}")

        def reset_node(*_):
            if not self._confirm("Reset to a fresh node key?",
                                 "Your current address is discarded and a new one is generated; "
                                 "old invites stop working. The daemon restarts now.",
                                 parent=dlg):
                return
            keysmod.remove_node_key(paths.daemon_dir())
            self._daemon_restart()
            try:
                self.session.refresh_node()
            except Exception:
                pass
            self.messages.sync_sidebar()
            node_status.set_text(
                f"current: {(self.supervisor.status() or {}).get('address', '?')}")

        nbtn = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        for label, cb in (("Preview", preview_node), ("Apply", apply_node),
                          ("Generate random", gen_node), ("Reset to default", reset_node)):
            b = Gtk.Button(label=label)
            b.connect("clicked", cb)
            nbtn.pack_start(b, False, False, 0)
        node.pack_start(nbtn, False, False, 0)
        if is_tailcat(st):
            # The Yggdrasil node-key controls do not apply to tailcat (the daemon
            # manages the WireGuard key); show the address and a note instead.
            for w in (node_entry, node_preview, nbtn):
                w.set_no_show_all(True)
                w.hide()
            tc_note = Gtk.Label(label="Your tailcat address is shared through your invite. "
                                      "Change relays in the relay panel, or regenerate a fresh "
                                      "identity below (old invites stop working).")
            tc_note.set_xalign(0.0)
            tc_note.set_line_wrap(True)
            tc_note.get_style_context().add_class("muted")
            node.pack_start(tc_note, False, False, 0)

            def regenerate_tailcat(*_):
                if not self._confirm(
                        "Regenerate your tailcat identity?",
                        "A new node key and address are generated; anyone who saved your old "
                        "invite must add you again. Your contacts and messages are kept. "
                        "The daemon restarts now.",
                        parent=dlg):
                    return
                try:
                    keysmod.remove_tailcat_key(paths.daemon_dir())
                except OSError:
                    pass
                self._daemon_restart()
                try:
                    self.session.refresh_node()
                except Exception:
                    pass
                self.messages.sync_sidebar()
                node_status.set_text(
                    f"current: {(self.supervisor.status() or {}).get('address', '?')}")

            regen = Gtk.Button(label="Regenerate identity")
            regen.connect("clicked", regenerate_tailcat)
            node.pack_start(regen, False, False, 0)

        # --- client identity ---
        ident = section("Client identity - your encryption key")
        id_status = Gtk.Label(label=f"current pubkey: {self.session.client.pubkey_hex}")
        id_status.set_xalign(0.0)
        id_status.set_line_wrap(True)
        ident.pack_start(id_status, False, False, 0)
        id_entry = Gtk.Entry(placeholder_text="paste a 64-hex identity seed to replace it")
        ident.pack_start(id_entry, False, False, 0)
        id_warn = Gtk.Label(
            label="Replacing your identity changes the key friends encrypt to; existing "
                  "conversations break and everyone must re-add your invite.")
        id_warn.set_xalign(0.0)
        id_warn.set_line_wrap(True)
        id_warn.get_style_context().add_class("muted")
        ident.pack_start(id_warn, False, False, 0)

        def import_identity(*_):
            try:
                seed = bytes.fromhex(id_entry.get_text().strip())
                if len(seed) != 32:
                    raise ValueError
            except ValueError:
                id_warn.set_text("error: expected a 64-hex identity seed")
                return
            if not self._confirm("Replace your client identity?",
                                 "This breaks existing conversations and cannot be undone here. "
                                 "Restart aimless afterwards to use it.",
                                 parent=dlg):
                return
            pw = self.app_ref.passphrase if self.app_ref else None
            if not pw:
                self._info_dialog("Passphrase unavailable",
                                  "Reopen aimless to import an identity.", parent=dlg)
                return
            crypto.save_identity(identity_path(), keysmod.signing_key(seed), pw)
            self._info_dialog("Identity written",
                              "Quit and reopen aimless to use the new identity.", parent=dlg)
        def regenerate_identity(*_):
            if not self._confirm(
                    "Regenerate your client identity?",
                    "This creates a NEW encryption key. All existing conversations break "
                    "and every contact must re-add your invite. This cannot be undone here. "
                    "Restart aimless afterwards to use it.",
                    parent=dlg):
                return
            pw = self.app_ref.passphrase if self.app_ref else None
            if not pw:
                self._info_dialog("Passphrase unavailable",
                                  "Reopen aimless to regenerate an identity.", parent=dlg)
                return
            crypto.save_identity(identity_path(), keysmod.signing_key(os.urandom(32)), pw)
            self._info_dialog("Identity regenerated",
                              "Quit and reopen aimless to use the new identity.", parent=dlg)

        idbtns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        ib = Gtk.Button(label="Import identity")
        ib.connect("clicked", import_identity)
        rb = Gtk.Button(label="Regenerate identity")
        rb.connect("clicked", regenerate_identity)
        idbtns.pack_start(ib, False, False, 0)
        idbtns.pack_start(rb, False, False, 0)
        ident.pack_start(idbtns, False, False, 0)

        # --- backup / restore ---
        bk = section("Backup & restore")
        bk_status = Gtk.Label(label="")
        bk_status.set_xalign(0.0)
        bk_status.set_line_wrap(True)
        bk_status.get_style_context().add_class("muted")
        bk.pack_start(bk_status, False, False, 0)

        def export_backup(*_):
            pw = ask_secret(dlg, "Backup passphrase", confirm=True)
            if pw is None:
                return
            chooser = Gtk.FileChooserDialog(
                title="Export backup", transient_for=dlg, action=Gtk.FileChooserAction.SAVE,
                buttons=("Cancel", Gtk.ResponseType.CANCEL, "Save", Gtk.ResponseType.OK))
            chooser.set_modal(True)
            chooser.set_current_name(f"aimless-backup-{datetime.now():%Y%m%d}.json")
            resp = chooser.run()
            path = chooser.get_filename()
            chooser.destroy()
            if resp != Gtk.ResponseType.OK or not path:
                return
            try:
                self.export_backup_to(path, pw)
                bk_status.set_text(f"backup written to {path}")
                self._info_dialog("Backup written", path, parent=dlg)
            except (OSError, ValueError) as e:
                bk_status.set_text(f"backup failed: {e}")
                self._info_dialog("Backup failed", str(e), parent=dlg)

        def export_data_backup(*_):
            adlg = Gtk.Dialog(title="Export data backup", transient_for=dlg, modal=True)
            adlg.add_button("Cancel", Gtk.ResponseType.CANCEL)
            adlg.add_button("Continue", Gtk.ResponseType.OK)
            abox = adlg.get_content_area()
            abox.set_spacing(8)
            abox.set_border_width(12)
            att_check = Gtk.CheckButton(label="Include attachment files")
            att_check.set_active(True)
            abox.pack_start(att_check, False, False, 0)
            anote = Gtk.Label(label="Backs up your rooms and message history (not your identity). "
                                    "You can restore it on this or another machine.")
            anote.set_xalign(0)
            anote.set_line_wrap(True)
            abox.pack_start(anote, False, False, 0)
            adlg.show_all()
            aresp = adlg.run()
            include_att = att_check.get_active()
            adlg.destroy()
            if aresp != Gtk.ResponseType.OK:
                return
            pw = ask_secret(dlg, "Backup passphrase", confirm=True)
            if pw is None:
                return
            chooser = Gtk.FileChooserDialog(
                title="Export data backup", transient_for=dlg, action=Gtk.FileChooserAction.SAVE,
                buttons=("Cancel", Gtk.ResponseType.CANCEL, "Save", Gtk.ResponseType.OK))
            chooser.set_modal(True)
            chooser.set_current_name(f"aimless-data-{datetime.now():%Y%m%d}.json")
            resp = chooser.run()
            path = chooser.get_filename()
            chooser.destroy()
            if resp != Gtk.ResponseType.OK or not path:
                return

            def worker():
                return self.export_data_backup_to(path, pw, include_att)

            def done(counts):
                bk_status.set_text(f"data backup written to {path}")
                self._info_dialog("Data backup written",
                                  f"{counts.get('messages', 0)} message(s), "
                                  f"{counts.get('rooms', 0)} room(s)\n{path}", parent=dlg)

            def fail(e):
                bk_status.set_text(f"data backup failed: {e}")
                self._info_dialog("Data backup failed", str(e), parent=dlg)

            run_async(worker, on_done=done, on_error=fail)

        def import_backup(*_):
            chooser = Gtk.FileChooserDialog(
                title="Import backup", transient_for=dlg, action=Gtk.FileChooserAction.OPEN,
                buttons=("Cancel", Gtk.ResponseType.CANCEL, "Open", Gtk.ResponseType.OK))
            chooser.set_modal(True)
            resp = chooser.run()
            path = chooser.get_filename()
            chooser.destroy()
            if resp != Gtk.ResponseType.OK or not path:
                return
            pw = ask_secret(dlg, "Backup passphrase")
            if pw is None:
                return
            try:
                data = keysmod.load_bundle(path, pw)
            except (ValueError, OSError, KeyError) as e:
                bk_status.set_text(f"import failed: {e}")
                self._info_dialog("Import failed", str(e), parent=dlg)
                return
            if data.get("kind") == "data":
                counts = data.get("counts") or {}
                n_att = len(data.get("attachments") or {})
                summary = (f"{counts.get('messages', 0)} message(s), "
                           f"{counts.get('rooms', 0)} room(s)")
                if n_att:
                    summary += f", {n_att} attachment file(s)"
                if not self._confirm(
                        "Restore this data backup?",
                        summary + "\n\nMerged into your current history (duplicates skipped).",
                        parent=dlg):
                    return
                app_pw = self.app_ref.passphrase if self.app_ref else None
                if not app_pw:
                    self._info_dialog("Passphrase unavailable",
                                      "Reopen aimless to import a backup.", parent=dlg)
                    return

                def data_worker():
                    return self.restore_data_backup_from(path, pw, app_pw)

                def data_done(_counts):
                    bk_status.set_text("data backup restored")
                    self._info_dialog("Data restored",
                                      "Your rooms and history were merged. Quit and reopen "
                                      "aimless to see them.", parent=dlg)

                def data_fail(e):
                    bk_status.set_text(f"restore failed: {e}")
                    self._info_dialog("Restore failed", str(e), parent=dlg)

                run_async(data_worker, on_done=data_done, on_error=data_fail)
                return
            try:
                seed = bytes.fromhex(data.get("identitySeed", ""))
                if len(seed) != 32:
                    raise ValueError("missing identity seed")
            except (ValueError, KeyError) as e:
                bk_status.set_text(f"import failed: {e}")
                self._info_dialog("Import failed", str(e), parent=dlg)
                return
            addr = "?"
            if data.get("nodeSeed"):
                try:
                    addr = keysmod.yggdrasil_address(
                        keysmod.parse_node_key(data["nodeSeed"])[1])
                except ValueError:
                    addr = "?"
            n = len([k for k in (data.get("contacts") or {}) if k != "_self"])
            if not self._confirm(
                    "Restore this backup?",
                    f"identity {data.get('pubkey', '?')[:16]}…\nnode address {addr}\n"
                    f"{n} contact(s)\n\nThis overwrites your current keys; restart aimless after.",
                    parent=dlg):
                return
            app_pw = self.app_ref.passphrase if self.app_ref else None
            if not app_pw:
                self._info_dialog("Passphrase unavailable",
                                  "Reopen aimless to import a backup.", parent=dlg)
                return
            try:
                self.restore_backup_from(path, pw, app_pw)
            except (ValueError, OSError) as e:
                bk_status.set_text(f"restore failed: {e}")
                self._info_dialog("Restore failed", str(e), parent=dlg)
                return
            self._daemon_restart()
            self._info_dialog("Backup restored",
                              "Quit and reopen aimless to use the restored identity.",
                              parent=dlg)

        hint = Gtk.Label(label="Identity: your keys and contacts (back up once). "
                               "Data: your rooms and message history (back up more often).")
        hint.set_xalign(0)
        hint.set_line_wrap(True)
        hint.get_style_context().add_class("muted")
        bk.pack_start(hint, False, False, 0)
        bbtn = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        eb = Gtk.Button(label="Export identity…")
        eb.connect("clicked", export_backup)
        db = Gtk.Button(label="Export data…")
        db.connect("clicked", export_data_backup)
        ib = Gtk.Button(label="Import backup…")
        ib.connect("clicked", import_backup)
        bbtn.pack_start(eb, False, False, 0)
        bbtn.pack_start(db, False, False, 0)
        bbtn.pack_start(ib, False, False, 0)
        bk.pack_start(bbtn, False, False, 0)

        box.show_all()
        dlg.run()
        dlg.destroy()

    def on_delete(self, *_):
        self.save_geometry()
        if self.app_ref is not None and self.app_ref.tray is not None and self.app_ref.tray.is_embedded():
            self.hide()
            return True
        return False

    def on_destroy(self, *_):
        self.save_geometry()
        if self.app_ref is not None:
            self.app_ref.quit()
        else:
            Gtk.main_quit()

    def on_configure(self, *_):
        # Debounced: configure-event fires continuously while dragging/resizing.
        if self._geometry_restoring:
            return False
        if not self._geometry_timer:
            self._geometry_timer = GLib.timeout_add(1000, self._save_geometry_now)
        return False

    def _save_geometry_now(self):
        self._geometry_timer = 0
        self.save_geometry()
        return False

    def _workareas(self):
        areas = []
        try:
            display = Gdk.Display.get_default()
            if display is not None:
                for i in range(display.get_n_monitors()):
                    geo = display.get_monitor(i).get_workarea()
                    areas.append((geo.x, geo.y, geo.width, geo.height))
        except Exception:
            pass
        if not areas:
            try:
                screen = Gdk.Screen.get_default()
                if screen is not None:
                    areas.append((0, 0, screen.get_width(), screen.get_height()))
            except Exception:
                pass
        return areas

    def save_geometry(self):
        self.prefs["window_width"], self.prefs["window_height"] = self.get_size()
        if pref(self.prefs, "remember_position"):
            try:
                gw = self.get_window()
                x, y = self.get_position()
                if (x, y) != (0, 0):  # (0,0) == "unknown" (Wayland)
                    self.prefs["window_x"], self.prefs["window_y"] = x, y
                if gw is not None:
                    self.prefs["window_maximized"] = bool(
                        gw.get_state() & Gdk.WindowState.MAXIMIZED)
            except Exception:
                pass
        save_prefs(self.prefs)

    def restore_geometry(self):
        if not pref(self.prefs, "remember_position"):
            return
        # Apply now (works as the initial-position hint on X11), and again once
        # the window is mapped - most window managers ignore pre-map move
        # requests and only honour one made after the window is shown.
        self._want_geometry = True
        self._apply_saved_position()

    def _apply_saved_position(self):
        x, y = self.prefs.get("window_x"), self.prefs.get("window_y")
        w, h = self.get_size()
        self._geometry_restoring = True
        try:
            if isinstance(x, int) and isinstance(y, int) and (x, y) != (0, 0):
                cx, cy = clamp_to_workarea(x, y, w, h, self._workareas())
                self.move(cx, cy)
            if self.prefs.get("window_maximized"):
                self.maximize()
        except Exception:
            pass
        finally:
            self._geometry_restoring = False

    def _on_map(self, *_):
        if getattr(self, "_want_geometry", False):
            self._want_geometry = False
            GLib.idle_add(self._apply_saved_position)
        return False

    def _on_active_changed(self, *_):
        # Reopening from the tray (or refocusing) makes the window active: the
        # conversation already on screen is now being read, so clear the unread
        # that piled up while it was hidden.
        if self.is_active() and getattr(self, "messages", None) is not None:
            self.messages.mark_selected_read()

    def reset_geometry(self):
        for k in ("window_x", "window_y", "window_maximized"):
            self.prefs.pop(k, None)
        save_prefs(self.prefs)

    def refresh_unread_indicator(self):
        messages = getattr(self, "messages", None)
        if messages is None:
            return
        n = sum(t.get("unread", 0) for t in messages.threads.values())
        self.set_title(f"{APP_NAME} ({n})" if n else APP_NAME)
        tray = getattr(self.app_ref, "tray", None) if self.app_ref else None
        if tray is not None and getattr(tray, "have_tray", False):
            tray.set_unread(n)

    def apply_theme(self, name):
        # AIMLESS_THEME is a session override and wins over the saved choice.
        env = os.environ.get("AIMLESS_THEME")
        if env in THEMES or env == "system":
            name = env
        if name != "system" and name not in THEMES:
            name = "system"
        set_theme(name)
        # Palette colours also live in Pango markup outside the CSS, so redraw
        # the widgets that embed them.
        try:
            self.messages.sync_sidebar()
            if self.messages.selected is not None:
                self.messages._render_messages(self.messages.selected["conv"],
                                               self.messages.selected)
            self.contacts.refresh()
            self.poll_status()
        except Exception:
            pass

    def _on_system_theme(self, *_):
        if uibase.CURRENT_THEME == "system":
            self.apply_theme("system")

    def on_preferences(self, *_):
        dlg = Gtk.Dialog(title="Preferences", transient_for=self, modal=True)
        dlg.add_button("Close", Gtk.ResponseType.CLOSE)
        dlg.set_default_size(440, -1)
        box = dlg.get_content_area()
        box.set_spacing(10)
        box.set_border_width(14)

        def row(label, widget):
            r = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            lbl = Gtk.Label(label=label)
            lbl.set_xalign(0.0)
            lbl.set_line_wrap(True)
            r.pack_start(lbl, True, True, 0)
            r.pack_end(widget, False, False, 0)
            box.pack_start(r, False, False, 0)
            return r

        def switch(key):
            sw = Gtk.Switch()
            sw.set_active(bool(pref(self.prefs, key)))
            sw.set_valign(Gtk.Align.CENTER)

            def on_toggle(w, _p, k=key):
                self.prefs[k] = w.get_active()
                if k == "remember_position" and not w.get_active():
                    self.reset_geometry()
                save_prefs(self.prefs)
            sw.connect("notify::active", on_toggle)
            return sw

        row("Remember window position and size", switch("remember_position"))
        reset_btn = Gtk.Button(label="Reset window position")
        reset_btn.set_halign(Gtk.Align.START)
        reset_btn.get_style_context().add_class("muted")
        reset_btn.connect("clicked", lambda *_: (self.reset_geometry(), self.unmaximize(),
                                                 self.activity.log("window position reset")))
        box.pack_start(reset_btn, False, False, 0)
        row("Desktop notifications for new messages", switch("notifications"))

        snd = Gtk.ComboBoxText()
        for value, label in (("off", "Off"), ("single", "Single beep"),
                             ("double", "Double beep"), ("triple", "Triple beep"),
                             ("long", "Long beep")):
            snd.append(value, label)
        snd.set_active_id(pref(self.prefs, "notification_sound"))

        def on_snd(w):
            self.prefs["notification_sound"] = w.get_active_id() or "single"
            save_prefs(self.prefs)
        snd.connect("changed", on_snd)
        snd_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        snd_box.pack_start(snd, False, False, 0)
        test_btn = Gtk.Button(label="Test")
        test_btn.get_style_context().add_class("muted")
        test_btn.connect("clicked", lambda *_: play_notification_sound(
            sound_enabled(self.prefs), force=True))
        snd_box.pack_start(test_btn, False, False, 0)
        row("Notification sound", snd_box)
        if os.environ.get("AIMLESS_SOUND"):
            note = Gtk.Label(label="AIMLESS_SOUND is set and overrides this choice.")
            note.set_xalign(0.0)
            note.get_style_context().add_class("muted")
            box.pack_start(note, False, False, 0)

        row("Press Enter to send (Shift+Enter for a new line)", switch("enter_to_send"))

        fmt = Gtk.ComboBoxText()
        fmt.append("24h", "24-hour")
        fmt.append("12h", "12-hour")
        fmt.set_active_id(pref(self.prefs, "time_format"))

        def on_fmt(w):
            self.prefs["time_format"] = w.get_active_id() or "24h"
            save_prefs(self.prefs)
            if self.messages.selected is not None:
                self.messages._history_loaded(self.messages.selected["conv"])
        fmt.connect("changed", on_fmt)
        row("Timestamp format", fmt)

        box.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 0)
        days_spin = Gtk.SpinButton.new_with_range(0, 3650, 1)
        days_spin.set_digits(0)
        days_spin.set_value(int(pref(self.prefs, "history_days") or 0))
        days_spin.set_tooltip_text("0 keeps history forever")

        def on_days(w):
            self.prefs["history_days"] = int(w.get_value())
            # A new window applies straight away, not at the next daily pass.
            self.prefs.pop("history_pruned_at", None)
            save_prefs(self.prefs)
        days_spin.connect("value-changed", on_days)
        days_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        days_box.pack_start(days_spin, False, False, 0)
        unit = Gtk.Label(label="days (0 = forever)")
        unit.get_style_context().add_class("muted")
        days_box.pack_start(unit, False, False, 0)
        row("Keep message history for", days_box)
        keep_note = Gtk.Label(label="Older messages and their attachment files are removed. "
                                    "Mail still waiting to be delivered is always kept.")
        keep_note.set_xalign(0.0)
        keep_note.set_line_wrap(True)
        keep_note.get_style_context().add_class("muted")
        box.pack_start(keep_note, False, False, 0)
        prune_btn = Gtk.Button(label="Prune now")
        prune_btn.set_halign(Gtk.Align.START)
        prune_btn.get_style_context().add_class("muted")

        def on_prune(*_):
            app = self.app_ref
            if app is None:
                return
            if not int(self.prefs.get("history_days") or 0):
                self.activity.log("history retention is off (0 = keep forever)")
                return
            gone = app._prune_history(force=True)
            n = gone.get("messages", 0)
            self.activity.log(f"history retention: removed {n} message(s)" if n
                              else "history retention: nothing to remove")
        prune_btn.connect("clicked", on_prune)
        box.pack_start(prune_btn, False, False, 0)

        box.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 0)
        theme_combo = Gtk.ComboBoxText()
        for tid, label, _desc in THEME_ENTRIES:
            theme_combo.append(tid, label)
        theme_combo.set_active_id(os.environ.get("AIMLESS_THEME") or pref(self.prefs, "theme"))

        def on_theme(w):
            name = w.get_active_id() or "system"
            self.prefs["theme"] = name
            save_prefs(self.prefs)
            self.apply_theme(name)
        theme_combo.connect("changed", on_theme)
        row("Theme", theme_combo)
        if os.environ.get("AIMLESS_THEME"):
            env_note = Gtk.Label(label="AIMLESS_THEME is set and overrides this choice.")
            env_note.set_xalign(0.0)
            env_note.get_style_context().add_class("muted")
            box.pack_start(env_note, False, False, 0)

        box.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 0)
        keys_btn = Gtk.Button(label="Keys & identity…")
        keys_btn.set_halign(Gtk.Align.START)
        keys_btn.connect("clicked", lambda *_: self.on_keys())
        box.pack_start(keys_btn, False, False, 0)

        box.show_all()
        dlg.run()
        dlg.destroy()
