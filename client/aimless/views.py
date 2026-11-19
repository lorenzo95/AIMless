"""The secondary tabs: contacts and activity.

Extracted from gtkui.py. Depends only on the shared UI layer (uibase) and
lower-level modules, so window.py can import it without a cycle.
"""
from datetime import datetime

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib, Gdk

from . import MIN_DAEMON_BUILD, protocol
from . import __version__ as client_version
from .daemon import DaemonError
from .relay import net_label, short_address
from .uibase import (C, _presence_state, _self_state, clear_children, contacts_path,
                     run_async, scroll_to_bottom)


class ContactsView(Gtk.Box):
    def __init__(self, app):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.app = app
        self.set_border_width(14)
        self.get_style_context().add_class("aimless-contacts")

        invite_frame = Gtk.Frame(label="Your invite - send this to a friend")
        invite_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        invite_box.set_border_width(10)
        self.invite_entry = Gtk.Entry(editable=False)
        invite_box.pack_start(self.invite_entry, False, False, 0)
        copy_btn = Gtk.Button(label="Copy to clipboard")
        copy_btn.connect("clicked", self.on_copy_invite)
        invite_box.pack_start(copy_btn, False, False, 0)
        invite_frame.add(invite_box)
        self.pack_start(invite_frame, False, False, 0)

        add_frame = Gtk.Frame(label="Add a buddy - paste their invite")
        add_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        add_box.set_border_width(10)
        self.add_invite_entry = Gtk.Entry(placeholder_text="paste a buddy's invite (aimless1 / aimless2)")
        add_box.pack_start(self.add_invite_entry, False, False, 0)
        pet_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.add_petname_entry = Gtk.Entry(placeholder_text="Petname (optional)")
        pet_box.pack_start(self.add_petname_entry, True, True, 0)
        add_btn = Gtk.Button(label="Add buddy")
        add_btn.get_style_context().add_class("aimless-send")
        add_btn.connect("clicked", self.on_add)
        pet_box.pack_start(add_btn, False, False, 0)
        add_box.pack_start(pet_box, False, False, 0)
        self.add_status = Gtk.Label(label="")
        self.add_status.set_xalign(0.0)
        self.add_status.set_line_wrap(True)
        add_box.pack_start(self.add_status, False, False, 0)
        add_frame.add(add_box)
        self.pack_start(add_frame, False, False, 0)

        list_frame = Gtk.Frame(label="Buddies")
        self.buddy_scroll = Gtk.ScrolledWindow()
        self.buddy_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.buddy_scroll.set_size_request(-1, 240)
        self.buddy_list = Gtk.ListBox()
        self.buddy_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.buddy_scroll.add(self.buddy_list)
        list_frame.add(self.buddy_scroll)
        self.pack_start(list_frame, True, True, 0)

    def on_copy_invite(self, *_):
        cb = Gtk.Clipboard.get_default(Gdk.Display.get_default())
        cb.set_text(self.invite_entry.get_text(), -1)
        self.add_status.set_text("invite copied to clipboard")

    def _render_buddy_rows(self, blocked):
        clear_children(self.buddy_list)
        self._buddy_rows = {}
        contacts = self.app.session.contacts()
        contact_nodes = {info["node"] for info in contacts.values()}
        for petname, info in sorted(contacts.items()):
            row = Gtk.ListBoxRow()
            row.set_selectable(False)
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            box.set_border_width(8)
            labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            title = Gtk.Label()
            title.set_markup(f"<b>{GLib.markup_escape_text(info.get('screen', petname))}</b>")
            title.set_xalign(0.0)
            labels.pack_start(title, False, False, 0)
            sub = Gtk.Label(label=f"{petname} · {info['node'][:20]}…")
            sub.set_xalign(0.0)
            sub.get_style_context().add_class("muted")
            labels.pack_start(sub, False, False, 0)
            box.pack_start(labels, True, True, 0)
            if info["node"] in blocked:
                title.get_style_context().add_class("muted")
                labels.get_style_context().add_class("muted")
                btn = Gtk.Button(label="Unblock")
                btn.connect("clicked", self.on_unblock, info["node"])
            else:
                btn = Gtk.Button(label="Remove")
                btn.connect("clicked", self.on_remove, petname)
            box.pack_start(btn, False, False, 0)
            row.add(box)
            self.buddy_list.add(row)
            row.show_all()
            self._buddy_rows[petname] = title
        for node in sorted(blocked):
            if node in contact_nodes:
                continue
            name = self.app.session.cache.blocked_screen(node)
            row = Gtk.ListBoxRow()
            row.set_selectable(False)
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            box.set_border_width(8)
            labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            title = Gtk.Label(label=name or f"{node[:20]}…")
            title.set_xalign(0.0)
            title.get_style_context().add_class("muted")
            labels.pack_start(title, False, False, 0)
            sub = Gtk.Label(label=f"{node[:20]}…" if name else "blocked")
            sub.set_xalign(0.0)
            sub.get_style_context().add_class("muted")
            labels.pack_start(sub, False, False, 0)
            box.pack_start(labels, True, True, 0)
            ub = Gtk.Button(label="Unblock")
            ub.connect("clicked", self.on_unblock, node)
            box.pack_start(ub, False, False, 0)
            row.add(box)
            self.buddy_list.add(row)
            row.show_all()
            self._buddy_rows[node] = title
        self.refresh_presence(getattr(self, "_cached_presence", {}))

    def refresh(self):
        # The buddy list renders immediately from local state (no daemon call on
        # the UI thread); the blocklist is fetched in the worker and only re-renders
        # if it changed, so a slow daemon can never stall the interface.
        self._render_buddy_rows(getattr(self, "_blocked", set()))
        self.refresh_presence(getattr(self, "_cached_presence", {}))

        def worker():
            invite = self.app.session.my_invite()
            presence = {p["key"]: p for p in self.app.session.client.presence(timeout=3)}
            try:
                blocked = set(self.app.session.client.blocklist())
            except DaemonError:
                blocked = set()
            return invite, presence, blocked

        def done(result):
            invite, presence, blocked = result
            self._cached_presence = presence
            self.invite_entry.set_text(invite)
            if blocked != getattr(self, "_blocked", set()):
                self._blocked = blocked
                self._render_buddy_rows(blocked)
            self.refresh_presence(presence)

        run_async(worker, on_done=done)

    def refresh_presence(self, presence):
        contacts = self.app.session.contacts()
        client = self.app.session.client
        for petname, title in getattr(self, "_buddy_rows", {}).items():
            info = contacts.get(petname, {})
            node = info.get("node")
            label = info.get("screen", petname)
            if not info:
                # synthetic blocked-stranger row: prefer the stored screen name
                node = petname
                label = self.app.session.cache.blocked_screen(petname) or label
            online, away, client_offline = _presence_state(presence.get(node, {}), client)
            if client_offline:
                color, state = C["away"], "client offline"
            elif away:
                color, state = C["away"], away
            else:
                # The same grey the sidebar uses for an offline buddy - two
                # tabs must not paint the same person in two shades.
                color = C["online"] if online else C["offline"]
                state = "online" if online else "offline"
            title.set_markup(
                f"<b>{GLib.markup_escape_text(label)}</b> "
                f"<span foreground='{color}' size='small'>{state}</span>")

    def on_add(self, *_):
        invite = self.add_invite_entry.get_text().strip()
        petname = self.add_petname_entry.get_text().strip()
        try:
            client_hex, node_hex, screen = protocol.parse_invite(invite)
        except ValueError as e:
            self.add_status.set_text(f"error: {e}")
            return
        addr = protocol.invite_addr(invite)
        if client_hex == self.app.session.pubkey_hex:
            self.add_status.set_text("error: that's your own invite - send it to a friend")
            return
        contacts = self.app.session.contacts()
        for k, c in contacts.items():
            if c.get("pubkey") == client_hex:
                if c.get("node") != node_hex or c.get("addr", "") != addr:
                    allc = protocol.load_contacts(contacts_path())
                    allc[k]["node"] = node_hex
                    allc[k]["addr"] = addr
                    protocol.save_contacts(contacts_path(), allc)
                    self.add_status.set_text(f"updated routing key for {k}")
                    self.app.messages.sync_sidebar()
                    return
                self.add_status.set_text(f"already known as {k}")
                return
        allc = protocol.load_contacts(contacts_path())
        allc[petname or screen] = {"pubkey": client_hex, "node": node_hex, "screen": screen, "addr": addr}
        protocol.save_contacts(contacts_path(), allc)

        def add_worker():
            try:
                self.app.session.client.unblock(node_hex)
            except DaemonError:
                pass
            return self.app.session.client.add_contact(node_hex, addr)

        run_async(add_worker)
        self.add_invite_entry.set_text("")
        self.add_petname_entry.set_text("")
        self.add_status.set_text(f"added {screen}")
        self.refresh()
        self.app.messages.sync_sidebar()

    def _confirm_remove(self, petname):
        dlg = Gtk.MessageDialog(transient_for=self.get_toplevel(), modal=True,
                                message_type=Gtk.MessageType.WARNING,
                                text=f"Remove {petname}?",
                                buttons=Gtk.ButtonsType.OK_CANCEL)
        dlg.format_secondary_text(
            "This removes them from your list on this device. They can still send to you "
            "and will prompt you again if they do.")
        dlg.set_default_response(Gtk.ResponseType.CANCEL)
        resp = dlg.run()
        dlg.destroy()
        return resp == Gtk.ResponseType.OK

    def on_remove(self, btn, petname):
        if not self._confirm_remove(petname):
            return
        allc = protocol.load_contacts(contacts_path())
        allc.pop(petname, None)
        protocol.save_contacts(contacts_path(), allc)
        self.add_status.set_text(f"removed {petname}")
        self.refresh()
        self.app.messages.sync_sidebar()

    def on_unblock(self, btn, node):
        self.app.session.cache.unmute(node)
        self.app.session.cache.clear_blocked_screen(node)

        def worker():
            return self.app.session.client.unblock(node)

        def done(_r):
            self.refresh()

        def fail(e):
            self.add_status.set_text(f"unblock failed: {e}")
            self.refresh()

        run_async(worker, on_done=done, on_error=fail)


class ActivityView(Gtk.Box):
    def __init__(self, app):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.app = app
        self.set_border_width(14)
        self.info_label = Gtk.Label(label="daemon: …")
        self.info_label.set_xalign(0.0)
        self.info_label.get_style_context().add_class("muted")
        self.pack_start(self.info_label, False, False, 0)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.get_style_context().add_class("aimless-log")
        self.log_view = Gtk.TextView(editable=False, cursor_visible=False, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.log_view.set_left_margin(10)
        self.log_view.set_right_margin(10)
        scroll.add(self.log_view)
        self.pack_start(scroll, True, True, 0)
        self.info_label.set_markup(f"<span foreground='{C['muted']}'>○  checking daemon …</span>")

    def refresh_info(self, st):
        tunnel = None
        try:
            tunnel = self.app.supervisor.tunnel_state()
        except Exception:
            tunnel = None
        tnote = ""
        if tunnel is not None:
            esc = GLib.markup_escape_text
            tn = f"ssh tunnel: {esc(tunnel['host'])} - {'up' if tunnel['up'] else 'down'}"
            if tunnel["restarts"]:
                tn += f" · restarts {tunnel['restarts']}"
            if not tunnel["up"] and tunnel.get("last_error"):
                tn += f" · {esc(tunnel['last_error'])}"
            tnote = (f"\n{tn}"
                     f"\n  remote {esc(tunnel['remote_socket'])}"
                     f"\n  local  {esc(tunnel['local_socket'])}")
        if not st:
            self.info_label.set_markup(
                f"<span foreground='{C['danger']}'>●  offline - daemon not reachable</span>{tnote}")
            return
        build = st.get("build", "")
        version_note = ""
        if not build:
            version_note = (f"\n<span foreground='{C['danger']}'>this daemon is an old build - "
                            "run `aimless stop`, then reopen aimless to update</span>")
            build = "unknown"
        else:
            try:
                dv = tuple(int(x) for x in build.split("/", 1)[1].split("."))
            except (IndexError, ValueError):
                dv = None
            if dv is not None and dv < MIN_DAEMON_BUILD:
                need = ".".join(str(x) for x in MIN_DAEMON_BUILD)
                version_note = (f"\n<span foreground='{C['danger']}'>daemon {build} is too old for this client "
                                f"(needs ≥ {need}) - update aimlessd-linux-amd64, then `aimless stop` and "
                                "reopen</span>")
                if not getattr(self, "_version_warned", False):
                    self._version_warned = True
                    self.log(f"⚠ daemon {build} is too old for this client (needs ≥ {need}) "
                             f"- update aimlessd-linux-amd64, then `aimless stop` and reopen")
        colour, text = _self_state(st)
        state = f"<span foreground='{colour}'>●  {text}</span>"
        nl = net_label(st)
        net_part = f"  ·  {nl}" if nl else ""
        self.info_label.set_markup(
            f"{state}  -  address <b>{short_address(st['address'])}</b>{net_part}\n"
            f"daemon: {build}  ·  client: aimless/{client_version}{version_note}{tnote}")

    def log(self, line):
        stamp = datetime.now().strftime("%H:%M:%S")
        buf = self.log_view.get_buffer()
        buf.insert(buf.get_end_iter(), f"[{stamp}] {line}\n")
        scroll_to_bottom(self.log_view.get_parent())
