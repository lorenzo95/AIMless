"""The messages view: conversation sidebar, chat pane, composer, attachments.

Extracted from gtkui.py. Depends only on the shared UI layer (uibase) and
lower-level aimless modules, so it imports without gtkui - no cycle.
"""
import base64
import hashlib
import os
import shutil
import time
from datetime import datetime

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib, Gdk, GdkPixbuf, Gio, Pango

from . import protocol
from .daemon import DaemonError
from .notify import (notify_new_message, play_notification_sound, should_notify,
                     should_play_sound, sound_enabled)
from .prefs import pref, save_prefs
from .textutil import _human_size, linkify, safe_conv_id, sanitize_filename
from .uibase import (C, _add_contact_from_roster, _presence_color, _presence_state,
                     _room_header_markup, _sidebar_title_markup, attachments_dir,
                     clear_children, contacts_path, date_label, first_icon,
                     format_time, run_async, scroll_to_bottom)


class MessagesView(Gtk.Box):
    def __init__(self, app):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.app = app
        self.get_style_context().add_class("aimless-chat")
        self.threads = {}
        self.selected = None
        self._send_in_flight = False
        self._history_busy = False
        self._catchup_busy = False
        self._catchup_attachments_busy = False
        self._file_bufs = {}
        # Sent-file delivery tracking: (node, seq) -> delivery record, plus a
        # small buffer of already-seen acks so an ack that lands while a send is
        # still being enqueued is not lost.
        self._pending_files = {}
        self._acked_seen = set()
        self._bubble_status = {}   # outgoing message id -> status Gtk.Label
        self._last_msg_date = None
        self._at_bottom = True
        # The order the list is in, so the open conversation can be kept in
        # place instead of jumping to the top when it gets a message.
        self._order = []
        # Where each conversation sits in that order - the list sorts by it.
        self._sort_rank = {}

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)

        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        sidebar.get_style_context().add_class("aimless-sidebar")
        sidebar.set_size_request(240, -1)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        hint = Gtk.Label(label="Buddies")
        hint.set_xalign(0.0)
        hint.set_margin_start(10)
        hint.set_margin_top(8)
        hint.set_margin_bottom(4)
        hint.get_style_context().add_class("muted")
        header.pack_start(hint, True, True, 0)
        # The order of the list: recent activity, or by name.
        self.order_btn = Gtk.Button()
        self.order_btn.set_relief(Gtk.ReliefStyle.NONE)
        self.order_btn.set_always_show_image(True)
        self.order_btn.set_margin_end(8)
        self.order_btn.set_margin_top(4)
        self.order_btn.connect("clicked", self.on_toggle_order)
        header.pack_start(self.order_btn, False, False, 0)
        sidebar.pack_start(header, False, False, 0)

        # Filter: narrows the list as you type.
        self.filter_entry = Gtk.Entry(placeholder_text="Filter buddies\u2026")
        self.filter_entry.set_margin_start(6)
        self.filter_entry.set_margin_end(6)
        self.filter_entry.set_margin_bottom(4)
        self.filter_entry.connect("changed", self.on_filter_changed)
        sidebar.pack_start(self.filter_entry, False, False, 0)

        new_room_btn = Gtk.Button()
        new_room_btn.set_relief(Gtk.ReliefStyle.NONE)
        new_room_btn.set_halign(Gtk.Align.START)
        new_room_btn.set_margin_start(6)
        new_room_btn.set_margin_bottom(4)
        new_room_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        new_room_box.pack_start(Gtk.Image.new_from_icon_name("list-add-symbolic", Gtk.IconSize.BUTTON), False, False, 0)
        new_room_box.pack_start(Gtk.Label(label="New room…"), False, False, 0)
        new_room_btn.add(new_room_box)
        new_room_btn.connect("clicked", self.on_new_room)
        sidebar.pack_start(new_room_btn, False, False, 0)

        self.thread_scroll = Gtk.ScrolledWindow()
        self.thread_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.thread_list = Gtk.ListBox()
        self.thread_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.thread_list.connect("row-selected", self.on_thread_selected)
        self.thread_scroll.add(self.thread_list)
        sidebar.pack_start(self.thread_scroll, True, True, 0)

        self.sidebar_empty = Gtk.Label(
            label="No conversations yet.\nAdd a buddy in Contacts and share your invite.")
        self.sidebar_empty.set_xalign(0.0)
        self.sidebar_empty.set_line_wrap(True)
        self.sidebar_empty.set_margin_start(10)
        self.sidebar_empty.set_margin_end(10)
        self.sidebar_empty.set_margin_top(8)
        self.sidebar_empty.set_margin_bottom(10)
        self.sidebar_empty.get_style_context().add_class("muted")
        self.sidebar_empty.set_no_show_all(True)
        sidebar.pack_start(self.sidebar_empty, False, False, 0)

        # Shown when the filter hides everything: an empty list should say why.
        self.sidebar_no_match = Gtk.Label(label="Nothing matches that.")
        self.sidebar_no_match.set_xalign(0.0)
        self.sidebar_no_match.set_margin_start(10)
        self.sidebar_no_match.set_margin_end(10)
        self.sidebar_no_match.set_margin_top(8)
        self.sidebar_no_match.get_style_context().add_class("muted")
        self.sidebar_no_match.set_no_show_all(True)
        sidebar.pack_start(self.sidebar_no_match, False, False, 0)

        paned.pack1(sidebar, False, False)
        paned.set_position(240)

        self.stack = Gtk.Stack()

        placeholder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        placeholder.set_valign(Gtk.Align.CENTER)
        placeholder.set_halign(Gtk.Align.CENTER)
        ph_icon = Gtk.Image.new_from_icon_name(
            first_icon("mail-unread-symbolic", "dialog-information-symbolic"), Gtk.IconSize.DIALOG)
        ph_label = Gtk.Label(label="Select a conversation")
        ph_label.get_style_context().add_class("muted")
        ph_hint = Gtk.Label(label="Add a buddy in Contacts and share your invite to start chatting.")
        ph_hint.get_style_context().add_class("muted")
        ph_hint.set_justify(Gtk.Justification.CENTER)
        ph_hint.set_line_wrap(True)
        ph_hint.set_max_width_chars(40)
        placeholder.pack_start(ph_icon, False, False, 0)
        placeholder.pack_start(ph_label, False, False, 0)
        placeholder.pack_start(ph_hint, False, False, 0)
        self.stack.add_titled(placeholder, "placeholder", "placeholder")

        conversation_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header_box.set_border_width(10)
        self.conversation_header = Gtk.Label()
        self.conversation_header.set_ellipsize(Pango.EllipsizeMode.END)
        self.conversation_header.set_xalign(0.0)
        header_box.pack_start(self.conversation_header, True, True, 0)
        self.clear_btn = Gtk.Button(label="Clear history…")
        self.clear_btn.set_relief(Gtk.ReliefStyle.NONE)
        self.clear_btn.set_valign(Gtk.Align.START)
        self.clear_btn.get_style_context().add_class("muted")
        self.clear_btn.connect("clicked", self.on_clear_history)
        header_box.pack_end(self.clear_btn, False, False, 0)
        self.delete_btn = Gtk.Button(label="Delete room…")
        self.delete_btn.set_relief(Gtk.ReliefStyle.NONE)
        self.delete_btn.set_valign(Gtk.Align.START)
        self.delete_btn.get_style_context().add_class("muted")
        self.delete_btn.set_no_show_all(True)
        self.delete_btn.connect("clicked", self.on_delete_room)
        header_box.pack_end(self.delete_btn, False, False, 0)
        self.mute_btn = Gtk.Button(label="Mute room…")
        self.mute_btn.set_relief(Gtk.ReliefStyle.NONE)
        self.mute_btn.set_valign(Gtk.Align.START)
        self.mute_btn.get_style_context().add_class("muted")
        self.mute_btn.set_no_show_all(True)
        self.mute_btn.connect("clicked", self.on_toggle_mute)
        header_box.pack_end(self.mute_btn, False, False, 0)
        self.block_btn = Gtk.Button(label="Block…")
        self.block_btn.set_relief(Gtk.ReliefStyle.NONE)
        self.block_btn.set_valign(Gtk.Align.START)
        self.block_btn.get_style_context().add_class("muted")
        self.block_btn.set_no_show_all(True)
        self.block_btn.connect("clicked", self.on_block)
        header_box.pack_end(self.block_btn, False, False, 0)
        conversation_box.pack_start(header_box, False, False, 0)

        self.member_chips = Gtk.FlowBox()
        self.member_chips.set_selection_mode(Gtk.SelectionMode.NONE)
        self.member_chips.set_min_children_per_line(1)
        self.member_chips.set_max_children_per_line(12)
        self.member_chips.set_margin_start(8)
        self.member_chips.set_margin_top(2)
        self.member_chips.set_no_show_all(True)
        conversation_box.pack_start(self.member_chips, False, False, 0)
        conversation_box.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 0)

        self.conversation_scroll = Gtk.ScrolledWindow()
        self.conversation_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.conversation = Gtk.ListBox()
        self.conversation.set_selection_mode(Gtk.SelectionMode.NONE)
        self.conversation_scroll.add(self.conversation)
        conversation_box.pack_start(self.conversation_scroll, True, True, 0)

        self.jump_btn = Gtk.Button(label="↓ New messages")
        self.jump_btn.set_halign(Gtk.Align.CENTER)
        self.jump_btn.set_valign(Gtk.Align.END)
        self.jump_btn.set_margin_bottom(6)
        self.jump_btn.set_no_show_all(True)
        self.jump_btn.get_style_context().add_class("aimless-jump")
        self.jump_btn.connect("clicked", lambda *_: self.jump_to_bottom())
        conversation_box.pack_start(self.jump_btn, False, False, 0)
        self.conversation_scroll.get_vadjustment().connect("value-changed", self._on_scrolled)

        composer_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        composer_box.set_border_width(8)
        self.composer = Gtk.TextView()
        self.composer.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.composer.set_size_request(-1, 48)
        self.composer.set_left_margin(8)
        self.composer.set_right_margin(8)
        self.composer.set_top_margin(6)
        self.composer.set_bottom_margin(6)
        self.composer.connect("key-press-event", self.on_composer_key)
        composer_frame = Gtk.Frame()
        composer_frame.set_shadow_type(Gtk.ShadowType.NONE)
        composer_frame.get_style_context().add_class("aimless-composer-frame")
        composer_frame.add(self.composer)
        composer_box.pack_start(composer_frame, True, True, 0)
        self.send_button = Gtk.Button(label="Send")
        self.send_button.set_valign(Gtk.Align.END)
        self.send_button.get_style_context().add_class("aimless-send")
        self.send_button.connect("clicked", lambda *_: self.send_message())
        composer_box.pack_start(self.send_button, False, False, 0)
        self.attach_button = Gtk.Button(label="Attach")
        self.attach_button.set_valign(Gtk.Align.END)
        self.attach_button.get_style_context().add_class("muted")
        self.attach_button.connect("clicked", self.on_attach)
        composer_box.pack_start(self.attach_button, False, False, 0)
        conversation_box.pack_start(composer_box, False, False, 0)

        self.stack.add_titled(conversation_box, "conversation", "conversation")
        self.stack.set_visible_child_name("placeholder")

        paned.pack2(self.stack, True, True)
        self.pack_start(paned, True, True, 0)

        self.sync_sidebar()

    def sync_sidebar(self):
        session = self.app.session
        contacts = session.contacts()
        activity = session.cache.last_activity()
        contact_nodes = {info["node"] for info in contacts.values()}
        for key in list(self.threads):
            thread = self.threads[key]
            if thread.get("is_room"):
                continue
            if thread["node"] not in contact_nodes:
                self.threads.pop(key)
                if self.selected is thread:
                    self.selected = None
                    self.stack.set_visible_child_name("placeholder")
                if "row" in thread:
                    thread["row"].destroy()
        for petname, info in sorted(contacts.items()):
            node = info["node"]
            if node in self.threads:
                thread = self.threads[node]
                thread["petname"], thread["contact"] = petname, info
                thread["screen"] = info.get("screen", petname)
            else:
                thread = {
                    "node": node, "conv": node, "is_room": False,
                    "petname": petname, "contact": info, "online": False, "away": None,
                    "preview": "", "unread": 0, "screen": info.get("screen", petname),
                }
                self.threads[node] = thread
                self.append_thread_row(node, thread)
            self.update_thread_row(node)
        for conv in session.cache.rooms():
            members = session.cache.members(conv)
            screens = sorted(m.get("screen") or n[:8] for n, m in members.items()
                             if n != session.self_node)
            title = ", ".join(screens[:3]) + (f" +{len(screens) - 3}" if len(screens) > 3 else "")
            if conv in self.threads:
                thread = self.threads[conv]
                thread["members"] = members
                thread["screen"] = title
            else:
                thread = {
                    "node": conv, "conv": conv, "is_room": True, "petname": None, "contact": None,
                    "members": members, "screen": title, "online": False, "away": None,
                    "preview": "", "unread": 0, "presence_by_node": {},
                }
                self.threads[conv] = thread
                self.append_thread_row(conv, thread)
            self.update_thread_row(conv)
        if self.selected:
            row = self.selected.get("row")
            if row:
                self.thread_list.select_row(row)
        self._apply_order(activity)
        self._update_order_button()
        self.on_filter_changed(self.filter_entry)
        if self.threads:
            self.sidebar_empty.hide()
        else:
            self.sidebar_empty.show()

    def on_new_room(self, *_):
        contacts = self.app.session.contacts()
        if len(contacts) < 2:
            self.app.activity.log("a room needs at least two buddies - add more people first")
            return
        dlg = Gtk.Dialog(title="New room", transient_for=self.get_toplevel(), modal=True)
        dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Create", Gtk.ResponseType.OK)
        dlg.set_default_response(Gtk.ResponseType.OK)
        dlg.set_default_size(360, 300)
        box = dlg.get_content_area()
        box.set_border_width(10)
        box.add(Gtk.Label(label="Pick the buddies to include (2 or more):"))
        checks = {}
        for petname, info in sorted(contacts.items()):
            cb = Gtk.CheckButton(label=info.get("screen", petname))
            box.add(cb)
            checks[info["node"]] = (cb, info)
        dlg.show_all()
        resp = dlg.run()
        chosen = [info for cb, info in checks.values() if cb.get_active()]
        dlg.destroy()
        if resp != Gtk.ResponseType.OK:
            return
        if len(chosen) < 2:
            self.append_system_note("a room needs at least two buddies")
            return
        self.create_room(chosen)

    def create_room(self, chosen_infos):
        session = self.app.session
        self_addr = ""
        try:
            a = session.client.whoami().get("address", "")
            if a.startswith("tc"):
                self_addr = a
        except DaemonError:
            pass
        members = {session.self_node: {"node": session.self_node,
                                       "pubkey": session.client.pubkey_hex,
                                       "screen": session.self_screen, "addr": self_addr}}
        for info in chosen_infos:
            members[info["node"]] = {"node": info["node"], "pubkey": info["pubkey"],
                                     "screen": info.get("screen", ""), "addr": info.get("addr", "")}
        conv = protocol.room_id(sorted(members.keys()))
        session.cache.ensure_room(conv, members)
        self.sync_sidebar()
        thread = self.threads.get(conv)
        if thread and "row" in thread:
            self.thread_list.select_row(thread["row"])

    def refresh_presence(self, presence):
        client = self.app.session.client
        for conv, thread in self.threads.items():
            if thread.get("is_room"):
                pb = {}
                for n in thread["members"]:
                    if n == self.app.session.self_node:
                        continue
                    online, away, off = _presence_state(presence.get(n, {}), client)
                    pb[n] = {"online": online, "away": away, "client_offline": off}
                thread["presence_by_node"] = pb
                thread["online"] = any(v["online"] for v in pb.values())
                thread["away"] = None
            else:
                online, away, _off = _presence_state(presence.get(conv, {}), client)
                thread["online"] = online
                thread["away"] = away
            self.update_thread_row(conv)
        sel = self.selected
        if sel is not None and sel.get("is_room"):
            self.conversation_header.set_markup(_room_header_markup(sel, self.app.session.self_node))
            self._render_member_chips(sel)

    def _apply_order(self, activity=None):
        """Put the conversations in order.

        One list for DMs and rooms - a group chat is a conversation, not a
        footnote under the buddy list. Two orders:

        * **recent** - the newest conversation first, unread ones on top, and
          conversations that have never had a message keep a stable spot at
          the bottom instead of churning;
        * **alpha** - by name, the way a phone book is.

        The open conversation keeps its place: a new message in it should not
        move the row out from under you. By name the place is the name, so
        only the recent order pins it.
        """
        if activity is None:
            activity = self.app.session.cache.last_activity()
        by_name = pref(self.app.prefs, "sidebar_order") == "alpha"

        def key(conv):
            thread = self.threads[conv]
            name = (thread.get("screen") or "").lower()
            if by_name:
                return (name, conv)
            ts = activity.get(conv, 0)
            unread = 0 if thread.get("unread") else 1
            return (unread, -ts, name, conv)

        order = sorted(self.threads, key=key)
        selected = self.selected.get("conv") if self.selected else None
        if not by_name and selected and selected in order and selected in self._order:
            place = self._order.index(selected)
            order.remove(selected)
            order.insert(min(place, len(order)), selected)
        self._order = list(order)
        # The list is sorted by a rank, not by moving rows around: GTK3 has no
        # reorder_child, and taking a row out of the list to put it back drops
        # the selection - the open conversation would close under your hands.
        self._sort_rank = {conv: index for index, conv in enumerate(order)}
        self.thread_list.set_sort_func(self._compare_rows)

    def _compare_rows(self, row_a, row_b, _user_data=None):
        """Sort two sidebar rows by the rank `_apply_order` gave them."""
        rank = self._sort_rank
        first = rank.get(getattr(row_a, "aimless_conv", None), len(rank))
        second = rank.get(getattr(row_b, "aimless_conv", None), len(rank))
        return (first > second) - (first < second)

    def on_toggle_order(self, _btn):
        """Switch the list between recent activity and by name."""
        order = "alpha" if pref(self.app.prefs, "sidebar_order") == "recent" else "recent"
        self.app.prefs["sidebar_order"] = order
        save_prefs(self.app.prefs)
        self._update_order_button()
        self._apply_order()

    def _update_order_button(self):
        by_name = pref(self.app.prefs, "sidebar_order") == "alpha"
        icon = "view-sort-ascending-symbolic" if by_name else "view-sort-descending-symbolic"
        self.order_btn.set_image(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.BUTTON))
        self.order_btn.set_tooltip_text(
            "Sorted by name - click for recent activity" if by_name
            else "Sorted by recent activity - click for name")

    def on_filter_changed(self, entry):
        """Narrow the list as you type."""
        needle = (entry.get_text() or "").strip().lower()
        shown = 0
        for conv, thread in self.threads.items():
            row = thread.get("row")
            if row is None:
                continue
            visible = not needle or needle in self._search_text(thread)
            row.set_visible(visible)
            if visible:
                shown += 1
        if needle and not shown:
            self.sidebar_no_match.show()
        else:
            self.sidebar_no_match.hide()

    @staticmethod
    def _search_text(thread):
        """What the filter box matches against: the visible name of the
        conversation, and the buddy name behind it."""
        return " ".join(filter(None, [thread.get("screen"), thread.get("petname")])).lower()

    def append_thread_row(self, node, thread):
        row = Gtk.ListBoxRow()
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.set_border_width(10)

        title = Gtk.Label()
        title.set_markup(_sidebar_title_markup(thread, self.app.session.self_node))
        title.set_xalign(0.0)
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.set_use_markup(True)
        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        labels.pack_start(title, False, False, 0)

        subtitle_text = thread["away"] if thread["away"] else thread["preview"]
        subtitle = Gtk.Label(label=subtitle_text)
        subtitle.set_xalign(0.0)
        subtitle.set_ellipsize(Pango.EllipsizeMode.END)
        subtitle.set_single_line_mode(True)
        subtitle.get_style_context().add_class("muted")
        labels.pack_start(subtitle, False, False, 0)
        box.pack_start(labels, True, True, 0)

        meta = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        meta.set_valign(Gtk.Align.START)
        badge = None
        if thread["unread"] > 0:
            badge = Gtk.Label()
            badge.set_markup(f"<b>{thread['unread']}</b>")
            badge.set_halign(Gtk.Align.END)
            badge.get_style_context().add_class("aimless-badge")
            meta.pack_start(badge, False, False, 0)
        box.pack_start(meta, False, False, 0)

        row.add(box)
        self.thread_list.add(row)
        row.show_all()
        row.aimless_conv = node
        # A row added while the filter is active has to respect it.
        needle = (self.filter_entry.get_text() or "").strip().lower()
        if needle:
            row.set_visible(needle in self._search_text(thread))
        thread["row"] = row
        thread["widgets"] = {"title": title, "subtitle": subtitle, "badge": badge}

    def update_thread_row(self, node):
        thread = self.threads.get(node)
        if not thread or "row" not in thread:
            return
        w = thread["widgets"]
        w["title"].set_markup(_sidebar_title_markup(thread, self.app.session.self_node))
        muted = self.app.session.cache.is_conversation_muted(node)
        away_text = thread["away"]
        if muted:
            w["subtitle"].set_text("muted")
            tip = None
        else:
            w["subtitle"].set_text(away_text if away_text else thread["preview"])
            # Away/offline lines can be long and get ellipsized: reveal the full
            # text on hover.
            tip = away_text if away_text else None
        thread["row"].set_tooltip_text(tip)
        row_style = thread["row"].get_style_context()
        if muted:
            row_style.add_class("aimless-muted")
        else:
            row_style.remove_class("aimless-muted")
        if thread["unread"] > 0:
            if not w["badge"]:
                badge = Gtk.Label()
                badge.set_markup(f"<b>{thread['unread']}</b>")
                badge.set_halign(Gtk.Align.END)
                badge.get_style_context().add_class("aimless-badge")
                meta_box = thread["row"].get_child().get_children()[-1]
                meta_box.pack_start(badge, False, False, 0)
                badge.show()
                w["badge"] = badge
            else:
                w["badge"].set_markup(f"<b>{thread['unread']}</b>")
        elif thread["unread"] == 0 and w["badge"]:
            w["badge"].destroy()
            w["badge"] = None
        self.app.refresh_unread_indicator()

    def catchup_unread(self):
        """Startup sweep: route everything the daemon journaled while no client
        was connected, then count what arrived per conversation.

        Uses the single per-peer sync path (the daemon keeps running between app
        sessions, and recv events broadcast to nobody are lost).

        Returns False when the sweep could not run (one is already in flight),
        so the caller can try again instead of losing the sweep for good.
        """
        if self._catchup_busy:
            return False
        self._catchup_busy = True
        session = self.app.session
        peers = set()
        for conv, thread in list(self.threads.items()):
            nodes = list(thread.get("members", {}).keys()) if thread.get("is_room") else [conv]
            peers.update(nodes)

        def worker():
            return session.sync.fetch(peers)

        def done(hists):
            self._catchup_busy = False
            before = {c: len(session.store.messages(c)) for c in self.threads}
            for peer, resp in hists.items():
                session.sync.apply(peer, resp)
            arrived = 0
            for conv, thread in self.threads.items():
                delta = len(session.store.messages(conv)) - before.get(conv, 0)
                if delta > 0:
                    arrived += delta
                    if not self._is_viewing(thread):
                        thread["unread"] += delta
                self.update_thread_row(conv)
            if arrived:
                self.app.activity.log(
                    f"unread sweep: {arrived} message(s) arrived while you were away")

        def fail(_e):
            self._catchup_busy = False

        run_async(worker, on_done=done, on_error=fail)
        return True

    def on_thread_selected(self, listbox, row):
        if row is None:
            self.stack.set_visible_child_name("placeholder")
            self.selected = None
            self.delete_btn.hide()
            self.mute_btn.hide()
            self.block_btn.hide()
            self._render_member_chips(None)
            return
        conv = next((c for c, t in self.threads.items() if t.get("row") is row), None)
        if conv is None:
            return
        thread = self.threads[conv]
        self.selected = thread
        thread["unread"] = 0
        self.app.session.store.mark_read(conv)
        self.update_thread_row(conv)

        if thread.get("is_room"):
            self.conversation_header.set_markup(_room_header_markup(thread, self.app.session.self_node))
            self.delete_btn.show()
            self.mute_btn.show()
            self.mute_btn.set_label("Unmute…" if self.app.session.cache.is_conversation_muted(conv) else "Mute…")
            self.block_btn.hide()
            self._render_member_chips(thread)
        else:
            self.delete_btn.hide()
            self.mute_btn.show()
            self.mute_btn.set_label("Unmute…" if self.app.session.cache.is_conversation_muted(conv) else "Mute…")
            self.block_btn.show()
            self.block_btn.set_label("Block…")
            self._render_member_chips(None)
            self.conversation_header.set_markup(
                f"<big><b>{GLib.markup_escape_text(thread['screen'])}</b></big>"
                f"  <span size='small' foreground='{C['subtitle']}'>{conv[:16]}…</span>")
        self._render_messages(conv, thread)
        self.stack.set_visible_child_name("conversation")
        self.load_history_async(conv)

    def _sender_label(self, thread, m):
        sender = m.get("sender")
        if not sender or sender == "self":
            return None
        info = thread.get("members", {}).get(sender)
        if info:
            return info.get("screen") or sender[:8]
        contact = thread.get("contact")
        if contact and contact.get("node") == sender:
            return thread.get("screen")
        return sender[:8]

    def load_history_async(self, conv):
        if self._history_busy:
            return
        self._history_busy = True
        session = self.app.session
        thread = self.threads.get(conv)
        nodes = (list(thread["members"].keys()) if thread and thread.get("is_room") else [conv])

        def worker():
            # one stream per sender; every entry is decrypted once and routed to
            # its own conversation, so a single cursor per peer covers all rooms.
            return session.sync.fetch(nodes)

        def done(hists):
            self._history_busy = False
            for peer, resp in hists.items():
                session.sync.apply(peer, resp)
            self._history_loaded(conv)

        def fail(e):
            self._history_busy = False
            self._history_failed(e)

        run_async(worker, on_done=done, on_error=fail)

    def _history_loaded(self, conv):
        if self.selected is None or self.selected.get("conv") != conv:
            return False
        self._render_messages(conv, self.selected)
        return False

    def refresh_conversation(self, conv):
        """Redraw an open conversation after messages were stored outside the
        live-event path (e.g. a message released by Accept on a request popup)."""
        if self.selected is not None and self.selected.get("conv") == conv:
            self._history_loaded(conv)

    def _history_failed(self, e):
        self.append_system_note(f"history unavailable: {e}")
        return False

    def append_bubble(self, outgoing, text, ts, sender=None, attachment=None, msg=None):
        stamp = format_time(ts, pref(self.app.prefs, "time_format")) if ts else ""
        row = Gtk.ListBoxRow()
        row.set_selectable(False)
        row.set_activatable(False)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        for attr, val in (("margin-start", 8), ("margin-end", 8), ("margin-top", 8), ("margin-bottom", 1)):
            box.set_property(attr, val)
        box.set_halign(Gtk.Align.END if outgoing else Gtk.Align.START)
        if sender and not outgoing:
            who = Gtk.Label(label=sender)
            who.set_xalign(0.0)
            who.get_style_context().add_class("muted")
            box.pack_start(who, False, False, 0)
        if attachment:
            self._render_attachment_box(box, outgoing, attachment)
        else:
            bubble = Gtk.Label()
            bubble.set_markup(linkify(text))
            bubble.connect("activate-link", self.on_link_activated)
            bubble.set_line_wrap(True)
            bubble.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
            bubble.set_max_width_chars(48)
            bubble.set_xalign(0.0)
            bubble.set_selectable(True)
            bubble.set_halign(Gtk.Align.END if outgoing else Gtk.Align.START)
            style = bubble.get_style_context()
            style.add_class("aimless-bubble")
            style.add_class("aimless-bubble-out" if outgoing else "aimless-bubble-in")
            box.pack_start(bubble, False, False, 0)
        if outgoing and msg is not None and not attachment:
            total = msg.get("delivery_total")
            if total:
                acked = msg.get("delivered_count", 0)
                status = Gtk.Label()
                status.set_xalign(1.0)
                status.get_style_context().add_class("muted")
                if acked >= total:
                    status.set_text("✓ delivered")
                elif acked:
                    status.set_text(f"• {acked}/{total} delivered")
                else:
                    status.set_text("• sending…")
                box.pack_start(status, False, False, 0)
                self._bubble_status[msg["id"]] = status
        if stamp:
            time_label = Gtk.Label(label=stamp)
            time_label.set_xalign(1.0 if outgoing else 0.0)
            time_label.get_style_context().add_class("muted")
            box.pack_start(time_label, False, False, 0)
        row.add(box)
        self.conversation.add(row)
        row.show_all()

    # -- scrolling / date dividers ----------------------------------------
    def _on_scrolled(self, adj):
        self._at_bottom = self._near_bottom(adj)
        if self._at_bottom:
            self.jump_btn.hide()

    def _near_bottom(self, adj, margin=40):
        try:
            return adj.get_value() >= adj.get_upper() - adj.get_page_size() - margin
        except Exception:
            return True

    def jump_to_bottom(self):
        self.jump_btn.hide()
        self._at_bottom = True
        scroll_to_bottom(self.conversation_scroll)

    def _maybe_scroll(self):
        if self._at_bottom:
            scroll_to_bottom(self.conversation_scroll)
        else:
            self.jump_btn.show()

    def _maybe_date_divider(self, ts):
        day = datetime.fromtimestamp(ts / 1000).date()
        if day == self._last_msg_date:
            return
        self._last_msg_date = day
        self.append_date_divider(date_label(ts))

    def append_date_divider(self, text):
        row = Gtk.ListBoxRow()
        row.set_selectable(False)
        row.set_activatable(False)
        lbl = Gtk.Label(label=text)
        lbl.set_xalign(0.5)
        lbl.get_style_context().add_class("muted")
        row.add(lbl)
        self.conversation.add(row)
        row.show_all()

    def _render_messages(self, conv, thread):
        clear_children(self.conversation)
        self._bubble_status = {}
        self._last_msg_date = None
        store = self.app.session.store
        for m in store.messages(conv):
            self._maybe_date_divider(m["ts"])
            self.append_bubble(m["dir"] == "out", m["text"], m["ts"],
                               sender=None if m["dir"] == "out" else self._sender_label(thread, m),
                               attachment=m.get("attachment"), msg=m)
            if m["dir"] == "out" and m.get("attachment") and m.get("delivered") is not None:
                total, _done = store.delivery_members(m["id"])
                if total:
                    row = self._append_status_row("")
                    rec = {"row": row, "filename": m["text"], "members_total": total,
                           "keys": set(store.undelivered(m["id"]))}
                    for k in rec["keys"]:
                        self._pending_files[k] = rec
                    self._set_delivery_text(rec)
        self._at_bottom = True
        scroll_to_bottom(self.conversation_scroll)

    def _append_outgoing(self, conv, seqs, ts, text, attachment=None, delivery_keys=None):
        store = self.app.session.store
        store.add_sent(conv, seqs, ts, text, attachment, delivery_keys)
        mid = store.out_id(seqs)
        total, done = store.delivery_members(mid)
        msg = {"id": mid, "dir": "out", "ts": ts, "text": text,
               "attachment": attachment,
               "delivered": (done == total) if total else False,
               "delivery_total": total or None, "delivered_count": done}
        self._maybe_date_divider(ts)
        self.append_bubble(True, text, ts, attachment=attachment, msg=msg)
        return mid

    def on_link_activated(self, label, uri):
        try:
            ok = Gio.AppInfo.launch_default_for_uri(uri, None)
        except GLib.Error as e:
            self.app.activity.log(f"couldn't open link: {e}")
            return True
        if not ok:
            self.app.activity.log(f"couldn't open link: no default handler for {uri}")
        return True

    def _render_attachment_box(self, box, outgoing, attachment):
        path = attachment.get("path", "")
        filename = attachment.get("filename", "file")
        mime_hint = attachment.get("mime_hint", "")
        size = attachment.get("size", 0)
        name_lbl = Gtk.Label(label=filename)
        name_lbl.set_xalign(0.0)
        name_lbl.set_halign(Gtk.Align.END if outgoing else Gtk.Align.START)
        box.pack_start(name_lbl, False, False, 0)
        if mime_hint == "image" and os.path.exists(path):
            try:
                pix = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, 280, -1, True)
                img = Gtk.Image.new_from_pixbuf(pix)
                img.set_halign(Gtk.Align.END if outgoing else Gtk.Align.START)
                eb = Gtk.EventBox()
                eb.add(img)
                eb.connect("button-press-event", self.on_expand_image, path, filename)
                box.pack_start(eb, False, False, 0)
            except GLib.Error:
                pass
        size_lbl = Gtk.Label(label=_human_size(size))
        size_lbl.set_xalign(0.0)
        size_lbl.get_style_context().add_class("muted")
        box.pack_start(size_lbl, False, False, 0)
        if os.path.exists(path):
            openb = Gtk.Button(label="Open")
            openb.set_relief(Gtk.ReliefStyle.NONE)
            openb.get_style_context().add_class("muted")
            openb.connect("clicked", self.on_open_attachment, path)
            box.pack_start(openb, False, False, 0)
        save = Gtk.Button(label="Save")
        save.set_relief(Gtk.ReliefStyle.NONE)
        save.get_style_context().add_class("muted")
        save.connect("clicked", self.on_save_attachment, path, filename)
        box.pack_start(save, False, False, 0)

    def on_open_attachment(self, _btn, path):
        try:
            Gio.AppInfo.launch_default_for_uri(GLib.filename_to_uri(path, None), None)
        except (GLib.Error, OSError) as e:
            self.app.activity.log(f"couldn't open file: {e}")

    def on_expand_image(self, _w, _ev, path, filename):
        win = Gtk.Window(title=filename)
        win.set_default_size(900, 700)
        hb = Gtk.HeaderBar()
        hb.set_show_close_button(True)
        hb.set_title(filename)
        save = Gtk.Button(label="Save")
        save.connect("clicked", lambda *_: self.on_save_attachment(None, path, filename))
        hb.pack_end(save)
        win.set_titlebar(hb)
        sc = Gtk.ScrolledWindow()
        win.add(sc)
        try:
            pix = GdkPixbuf.Pixbuf.new_from_file(path)
            img = Gtk.Image.new_from_pixbuf(pix)
            sc.add(img)
        except GLib.Error:
            pass

        def on_key(w, e):
            if e.keyval == Gdk.KEY_Escape:
                w.close()
                return True
            return False
        win.connect("key-press-event", on_key)
        win.show_all()
        return False

    def on_save_attachment(self, _btn, path, filename):
        dlg = Gtk.FileChooserDialog(
            title="Save attachment", transient_for=self.get_toplevel(),
            action=Gtk.FileChooserAction.SAVE,
            buttons=("Cancel", Gtk.ResponseType.CANCEL, "Save", Gtk.ResponseType.OK))
        dlg.set_current_name(filename)
        resp = dlg.run()
        dest = dlg.get_filename()
        dlg.destroy()
        if resp != Gtk.ResponseType.OK or not dest:
            return
        try:
            shutil.copyfile(path, dest)
            self.app.activity.log(f"saved {filename}")
        except OSError as e:
            self.app.activity.log(f"save failed: {e}")

    def append_system_note(self, text):
        row = Gtk.ListBoxRow()
        row.set_selectable(False)
        row.set_activatable(False)
        note = Gtk.Label(label=text)
        note.set_line_wrap(True)
        note.set_xalign(0.5)
        note.get_style_context().add_class("muted")
        row.add(note)
        self.conversation.add(row)
        row.show_all()
        self._maybe_scroll()

    def _confirm_clear(self, title):
        dlg = Gtk.MessageDialog(transient_for=self.get_toplevel(), modal=True,
                                message_type=Gtk.MessageType.WARNING,
                                text=f"Clear history with {title}?",
                                buttons=Gtk.ButtonsType.OK_CANCEL)
        dlg.format_secondary_text(
            "This conversation is emptied here and the existing history is dismissed - "
            "only messages that arrive after this are shown.")
        dlg.set_default_response(Gtk.ResponseType.CANCEL)
        resp = dlg.run()
        dlg.destroy()
        return resp == Gtk.ResponseType.OK

    def _confirm_delete(self, title):
        dlg = Gtk.MessageDialog(transient_for=self.get_toplevel(), modal=True,
                                message_type=Gtk.MessageType.WARNING,
                                text=f"Delete the room {title}?",
                                buttons=Gtk.ButtonsType.OK_CANCEL)
        dlg.format_secondary_text(
            "Messages are removed from this device and old history is dismissed - "
            "if someone sends to the room again, it reappears with only the new messages.")
        dlg.set_default_response(Gtk.ResponseType.CANCEL)
        resp = dlg.run()
        dlg.destroy()
        return resp == Gtk.ResponseType.OK

    def on_delete_room(self, *_):
        thread = self.selected
        if thread is None or not thread.get("is_room"):
            return
        title = thread["screen"]
        if not self._confirm_delete(title):
            return
        conv = thread["conv"]
        session = self.app.session
        nodes = sorted(n for n in thread["members"] if n != session.self_node)

        def worker():
            return {n: session.client.history(n, 0).get("latest", 0) for n in nodes}

        def done(latests):
            session.cache.delete_room(conv, latests)
            gone = self.threads.pop(conv, None)
            if gone and "row" in gone:
                gone["row"].destroy()
            if self.selected is thread:
                self.selected = None
                self.stack.set_visible_child_name("placeholder")
                self.delete_btn.hide()
                self.mute_btn.hide()
                self.block_btn.hide()
            self.app.activity.log(f"deleted room {title}")

        def fail(e):
            self.append_system_note(f"delete failed: {e}")
            self.app.activity.log(f"delete failed: {e}")

        run_async(worker, on_done=done, on_error=fail)

    def on_clear_history(self, *_):
        if self.selected is None:
            return
        thread = self.selected
        title = thread["screen"]
        if not self._confirm_clear(title):
            return
        session = self.app.session
        conv = thread["conv"]
        nodes = (sorted(n for n in thread["members"] if n != session.self_node)
                 if thread.get("is_room") else [conv])

        def worker():
            return {n: session.client.history(n, 0).get("latest", 0) for n in nodes}

        def done(latests):
            session.cache.clear_history(conv)
            for n, latest in latests.items():
                if latest:
                    session.cache.set_scan_last(conv, n, latest)
            thread["preview"] = ""
            if self.selected is thread:
                clear_children(self.conversation)
            self.update_thread_row(conv)
            self.app.activity.log(f"cleared history with {title}")

        def fail(e):
            self.append_system_note(f"clear failed: {e}")
            self.app.activity.log(f"clear failed: {e}")

        run_async(worker, on_done=done, on_error=fail)

    def _confirm_add_member(self, screen):
        dlg = Gtk.MessageDialog(transient_for=self.get_toplevel(), modal=True,
                                message_type=Gtk.MessageType.QUESTION,
                                text=f"Add {screen} as a buddy?",
                                buttons=Gtk.ButtonsType.OK_CANCEL)
        dlg.format_secondary_text(
            "You'll be able to message them directly. Their identity is as claimed by "
            "whoever added them to this room - invites exchanged directly are stronger.")
        dlg.set_default_response(Gtk.ResponseType.OK)
        resp = dlg.run()
        dlg.destroy()
        return resp == Gtk.ResponseType.OK

    def _render_member_chips(self, thread):
        chips = self.member_chips
        for child in chips.get_children():
            chips.remove(child)
        if not thread or not thread.get("is_room"):
            chips.hide()
            return
        session = self.app.session
        contact_nodes = {info["node"] for info in session.contacts().values()}
        pb = thread.get("presence_by_node", {})
        entries = sorted((m.get("screen") or n[:8], n) for n, m in thread["members"].items()
                         if n != session.self_node)
        for screen, n in entries:
            p = pb.get(n, {})
            color = _presence_color(p.get("online"), p.get("away"))
            known = n in contact_nodes
            glyph = "●" if known else "○"
            btn = Gtk.Button()
            btn.set_relief(Gtk.ReliefStyle.NONE)
            lbl = Gtk.Label()
            lbl.set_markup(f"<span foreground='{color}'>{glyph}</span> {GLib.markup_escape_text(screen)}")
            lbl.set_xalign(0.0)
            btn.add(lbl)
            btn.get_style_context().add_class("aimless-chip")
            tip = "Open conversation - your buddy" if known else "Add as buddy (not your buddy yet)"
            if p.get("client_offline"):
                tip += " · client offline (messages will be delivered)"
            btn.set_tooltip_text(tip)
            btn.connect("clicked", self.on_member_chip, n, screen, known)
            chips.add(btn)
            btn.show_all()
        chips.show()

    def on_member_chip(self, _btn, node, screen, known):
        if known:
            dm = self.threads.get(node)
            if dm and "row" in dm:
                self.thread_list.select_row(dm["row"])
            return
        if not self._confirm_add_member(screen):
            return
        info = (self.selected or {}).get("members", {}).get(node)
        if not info:
            return
        petname = _add_contact_from_roster(node, info.get("pubkey"), screen, info.get("addr", ""))
        self.app.session.cache.unmute(node)
        self.app.daemon_block_set(node, blocked=False)
        self.app.contacts.refresh()
        self.sync_sidebar()
        self.app.activity.log(f"added {petname} from the room")
        dm = self.threads.get(node)
        if dm and "row" in dm:
            self.thread_list.select_row(dm["row"])

    def on_toggle_mute(self, *_):
        thread = self.selected
        if thread is None:
            return
        conv = thread["conv"]
        cache = self.app.session.cache
        if cache.is_conversation_muted(conv):
            cache.unmute_conversation(conv)
            self.app.activity.log(f"unmuted {thread['screen']}")
        else:
            cache.mute_conversation(conv)
            self.app.activity.log(f"muted {thread['screen']}")
        self.mute_btn.set_label("Unmute…" if cache.is_conversation_muted(conv) else "Mute…")
        self.update_thread_row(conv)

    def on_block(self, *_):
        thread = self.selected
        if thread is None or thread.get("is_room"):
            return
        node = thread["conv"]
        screen = thread["screen"]
        if not self._confirm_block(screen):
            return
        cache = self.app.session.cache
        cache.mute(node)
        cache.set_blocked_screen(node, screen)
        allc = protocol.load_contacts(contacts_path())
        for key in list(allc):
            if key != "_self" and allc[key].get("node") == node:
                del allc[key]
        protocol.save_contacts(contacts_path(), allc)
        gone = self.threads.pop(node, None)
        if gone and "row" in gone:
            gone["row"].destroy()
        if self.selected is thread:
            self.selected = None
            self.stack.set_visible_child_name("placeholder")
            self.delete_btn.hide()
            self.mute_btn.hide()
            self.block_btn.hide()
        self.app.daemon_block_set(node, blocked=True, on_done=self.app.contacts.refresh)
        self.app.activity.log(f"blocked {screen}")

    def _confirm_block(self, screen):
        dlg = Gtk.MessageDialog(transient_for=self.get_toplevel(), modal=True,
                                message_type=Gtk.MessageType.WARNING,
                                text=f"Block and remove {screen}?",
                                buttons=Gtk.ButtonsType.OK_CANCEL)
        dlg.format_secondary_text(
            "They won't be able to message you. You can unblock them later from the Contacts list.")
        dlg.set_default_response(Gtk.ResponseType.CANCEL)
        resp = dlg.run()
        dlg.destroy()
        return resp == Gtk.ResponseType.OK

    def on_composer_key(self, widget, event):
        if event.keyval not in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            return False
        shift = bool(event.state & Gdk.ModifierType.SHIFT_MASK)
        ctrl = bool(event.state & Gdk.ModifierType.CONTROL_MASK)
        enter_sends = pref(self.app.prefs, "enter_to_send")
        if (enter_sends and not shift) or (not enter_sends and ctrl):
            self.send_message()
            return True
        return False

    def send_message(self):
        if self.selected is None:
            return
        buf = self.composer.get_buffer()
        start, end = buf.get_bounds()
        text = buf.get_text(start, end, True).strip()
        if not text:
            return
        if self._send_in_flight:
            return
        thread = self.selected
        conv = thread["conv"]
        # Replying means this conversation is being read: clear any unread that
        # accumulated (e.g. while the window was hidden to the tray).
        if thread.get("unread"):
            thread["unread"] = 0
        self.app.session.store.mark_read(conv)
        ts = int(time.time() * 1000)
        self._send_in_flight = True
        self.send_button.set_sensitive(False)
        buf.set_text("")

        if thread.get("is_room"):
            members = list(thread["members"].values())

            def worker():
                return self.app.session.client.send_room(members, conv, text, ts)

            def done(seqs):
                self._send_in_flight = False
                self.send_button.set_sensitive(True)
                self._append_outgoing(conv, seqs, ts, text)
                self.selected["preview"] = text
                self.update_thread_row(conv)
                self._apply_order()
                self._maybe_scroll()
        else:
            contact = thread["contact"]

            def worker():
                return self.app.session.client.send(contact["pubkey"], contact["node"], text, ts)

            def done(resp):
                self._send_in_flight = False
                self.send_button.set_sensitive(True)
                self._append_outgoing(conv, {contact["node"]: resp.get("seq", 0)}, ts, text)
                self.selected["preview"] = text
                self.update_thread_row(conv)
                self._apply_order()
                self._maybe_scroll()

        def fail(e):
            self._send_in_flight = False
            self.send_button.set_sensitive(True)
            self.append_system_note(f"⚠ send failed: {e} - the message was not queued")
            self.app.activity.log(f"send failed: {e}")

        run_async(worker, on_done=done, on_error=fail)

    def on_attach(self, *_):
        thread = self.selected
        if thread is None:
            return
        dlg = Gtk.FileChooserDialog(
            title="Attach a file", transient_for=self.get_toplevel(),
            action=Gtk.FileChooserAction.OPEN,
            buttons=("Cancel", Gtk.ResponseType.CANCEL, "Attach", Gtk.ResponseType.OK))
        resp = dlg.run()
        path = dlg.get_filename()
        dlg.destroy()
        if resp != Gtk.ResponseType.OK or not path:
            return
        try:
            size = os.path.getsize(path)
            protocol.validate_send_size(size)
        except (OSError, ValueError) as e:
            self.append_system_note(f"attach failed: {e}")
            return
        filename = os.path.basename(path)
        if thread.get("is_room") and size > 5 * 1024 * 1024 and len(thread.get("members", {})) > 3:
            if not self._confirm_large_room_send(filename, size, len(thread["members"])):
                return
        self._send_file(thread, path, filename, size)

    def _confirm_large_room_send(self, filename, size, members):
        dlg = Gtk.MessageDialog(transient_for=self.get_toplevel(), modal=True,
                                message_type=Gtk.MessageType.WARNING,
                                text=f"Send {filename} ({_human_size(size)}) to {members} members?",
                                buttons=Gtk.ButtonsType.OK_CANCEL)
        dlg.format_secondary_text(
            "Every member receives a full copy - a large transfer to a big room uses "
            "real bandwidth, just like text does, scaled per file.")
        dlg.set_default_response(Gtk.ResponseType.CANCEL)
        resp = dlg.run()
        dlg.destroy()
        return resp == Gtk.ResponseType.OK

    def _send_file(self, thread, path, filename, size):
        conv = thread["conv"]
        is_room = thread.get("is_room")
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as e:
            self.append_system_note(f"attach failed: {e}")
            return
        tid = protocol.new_transfer_id()
        sha = hashlib.sha256(data).hexdigest()
        mime_hint = ("image" if filename.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))
                     else "application")
        pieces = [data[i:i + protocol.FILE_CHUNK_SIZE] for i in range(0, len(data), protocol.FILE_CHUNK_SIZE)]
        total = len(pieces)
        session = self.app.session
        client = session.client
        row = self._append_status_row(f"Sending {filename} - 0/{total}")
        attachment = {"path": path, "filename": filename, "mime_hint": mime_hint, "size": size}

        def worker():
            seqs = {}
            expected = set()
            for i, piece in enumerate(pieces):
                chunk = protocol.make_chunk(tid, i, total, filename, mime_hint, sha, size, piece,
                                            conv=conv if is_room else None)
                if is_room:
                    chunk_seqs = client.send_file_room(list(thread["members"].values()), conv, chunk)
                    seqs.update(chunk_seqs)
                    for n, s in chunk_seqs.items():
                        expected.add((n, int(s)))
                else:
                    payload = protocol.build_file_payload(
                        session.identity, thread["contact"]["pubkey"], chunk)
                    resp = client.send_file(conv, payload)
                    seq = int(resp.get("seq", 0))
                    seqs[conv] = seq
                    expected.add((conv, seq))
                GLib.idle_add(_progress, i + 1)
            return seqs, expected

        def _progress(done):
            self._update_status_row(row, f"Sending {filename} - {done}/{total}")
            return False

        def done(result):
            seqs, expected = result
            if row is not None:
                row.destroy()
            ts = int(time.time() * 1000)
            self._append_outgoing(conv, seqs, ts, filename, attachment=attachment,
                                  delivery_keys=expected)
            if thread is self.selected:
                thread["preview"] = filename
                self.update_thread_row(conv)
            # Persistent indicator under the bubble: the daemon reports an ack
            # per chunk once the peer has stored it, so we can show delivery
            # progress instead of the file silently vanishing into the queue.
            deliver_row = self._append_status_row("")
            self.register_file_delivery(deliver_row, filename, expected)
            self._maybe_scroll()

        def fail(e):
            if row is not None:
                self._update_status_row(row, f"{filename} - send failed: {e}")
            self.app.activity.log(f"send failed: {e}")

        run_async(worker, on_done=done, on_error=fail)

    def register_file_delivery(self, row, filename, keys):
        """Begin tracking delivery of a sent file. ``keys`` is the set of
        (node, seq) chunk acks still outstanding; progress is reported per
        recipient (a recipient is done when all their chunks are acked)."""
        keys = {(n, int(s)) for n, s in keys if int(s)}
        members_total = len({n for n, _ in keys})
        outstanding = keys - (keys & self._acked_seen)
        rec = {"row": row, "filename": filename, "keys": outstanding,
               "members_total": members_total}
        for k in outstanding:
            self._pending_files[k] = rec
        self._set_delivery_text(rec)

    def note_acked(self, node, seq):
        """A sent chunk was delivered (the daemon saw the peer's ACK). Returns
        True when it belonged to a tracked file transfer, so callers can keep
        the activity log for ordinary text acks without spamming it per chunk."""
        key = (node, int(seq or 0))
        self._acked_seen.add(key)
        if len(self._acked_seen) > 4096:
            self._acked_seen.clear()
        store = self.app.session.store
        mid = store.mark_delivered(node, seq)
        hit_file = False
        rec = self._pending_files.pop(key, None)
        if rec is not None:
            rec["keys"].discard(key)
            self._set_delivery_text(rec)
            hit_file = True
        if mid is not None:
            lbl = self._bubble_status.get(mid)
            if lbl is not None:
                total, done = store.delivery_members(mid)
                if total and done >= total:
                    lbl.set_text("✓ delivered")
                elif total:
                    lbl.set_text(f"• {done}/{total} delivered")
        return hit_file

    def _set_delivery_text(self, rec):
        total = rec.get("members_total", 0)
        left = len({n for n, _ in rec["keys"]})
        done = total - left
        if total == 0:
            text = f"Sent {rec['filename']}"
        elif left == 0:
            text = f"Delivered {rec['filename']} ✓"
        elif total == 1:
            text = f"Sending {rec['filename']}…"
        else:
            text = f"Sending {rec['filename']} - {done}/{total} delivered"
        self._update_status_row(rec["row"], text)

    def incoming(self, ev):
        if ev.get("type") == "file":
            self._incoming_file(ev)
            return
        node = ev.get("from")
        try:
            opened = self.app.session.client.decrypt_recv(ev)
        except (ValueError, KeyError):
            return
        session = self.app.session
        conv = opened.get("conv") or node
        # The roster decides who our replies are sealed to, so it is only
        # ours when it hashes to the conversation id. A peer that sends a
        # roster for a different conversation must not be able to rewrite a
        # room we are in.
        roster = protocol.valid_room_members(conv, opened.get("members"))
        if session.cache.is_conversation_muted(conv):
            session.cache.add_recv(conv, node, ev.get("seq", 0), opened["ts"], opened["text"])
            thread = self.threads.get(conv)
            if self.selected is thread and thread is not None:
                self.append_bubble(False, opened["text"], opened["ts"],
                                   sender=self._sender_label(thread, {"sender": node}))
                self._maybe_scroll()
            return
        contact_nodes = {info["node"] for info in session.contacts().values()}
        # A room can include people who are not buddies. Admit the whole roster
        # to the daemon: the tailcat transport accepts only registered peers, so
        # otherwise a non-buddy member's handshake is ignored and nothing they
        # send ever arrives.
        if roster:
            run_async(lambda: session.client.register_room_members(roster.values()))
        # A verified member of a room we are already in may write to it without
        # being a buddy: the roster hashes to the conversation id, and the sender
        # proves possession of that member's identity key. Membership is checked
        # against the roster we stored, not the one offered, and the room must
        # already exist so a stranger cannot create conversations out of nowhere.
        stored = session.cache.members(conv) if roster else {}
        member_ok = (bool(roster) and session.cache.is_room(conv)
                     and node in stored
                     and stored[node].get("pubkey") == opened.get("from"))
        if node not in contact_nodes and not member_ok:
            if session.cache.is_muted(node):
                return
            # A stranger's message stays out of our conversations: without a
            # roster that checks out it is filed as a plain DM request.
            session.cache.add_pending({
                "node": node, "pubkey": opened.get("from"),
                "screen": opened.get("screen") or node[:8],
                "conv": conv if roster else node,
                "members": list(roster.values()) if roster else [],
                "seq": ev.get("seq", 0), "ts": opened["ts"], "text": opened["text"],
            })
            self.app.surface_pending_requests()
            return
        if roster:
            session.cache.ensure_room(conv, {
                m["node"]: {"node": m["node"], "pubkey": m["pubkey"],
                            "screen": m.get("screen", ""), "addr": m.get("addr", "")}
                for m in roster.values()}, created=opened["ts"])
        thread = self.threads.get(conv)
        if thread is None:
            self.sync_sidebar()
            thread = self.threads.get(conv)
            if thread is None:
                return
        session.cache.add_recv(conv, node, ev.get("seq", 0), opened["ts"], opened["text"])
        thread["preview"] = opened["text"]
        # The chat view belongs to the selected thread, so the bubble must be
        # appended even while the window is hidden or another tab is showing.
        # Gating this on _is_viewing left the message in the sidebar preview
        # but out of the chat until the thread was re-selected. Whether it
        # counts as read is a separate question.
        if self.selected is thread:
            self.append_bubble(False, opened["text"], opened["ts"],
                               sender=self._sender_label(thread, {"sender": node}))
            self._maybe_scroll()
        if not self._is_viewing(thread):
            thread["unread"] += 1
        # Sort once the unread count is known: the conversation that just got
        # a message belongs on top.
        self._apply_order()
        self.update_thread_row(conv)
        visible, muted, blocked = self._alert_state(conv, node)
        if should_notify(visible, muted, blocked, self.app.prefs):
            notify_new_message(opened.get("screen") or node[:8], opened["text"])
        if should_play_sound(visible, muted, blocked, self.app.prefs):
            play_notification_sound(sound_enabled(self.app.prefs))

    def _is_viewing(self, thread):
        """True only if this conversation is actually on screen right now: the
        window is active, the messages tab is shown and this thread is selected.
        A window hidden to the tray must not count incoming messages as read -
        that is what left the tray with no unread badge."""
        return (thread is not None and self.selected is thread
                and self.app.is_active()
                and self.app.stack.get_visible_child_name() == "messages")

    def mark_selected_read(self):
        """Clear the unread of the conversation currently on screen. Selecting a
        thread already does this, but a thread that was selected while the window
        was hidden does not re-fire on_thread_selected when the window reopens -
        so the unread gathered while hidden must be cleared here too."""
        thread = self.selected
        if thread is None or not self._is_viewing(thread):
            return
        conv = thread.get("conv")
        self.app.session.store.mark_read(conv)
        if thread.get("unread"):
            thread["unread"] = 0
            self.update_thread_row(conv)

    def _alert_state(self, conv, node):
        """(visible, muted, blocked) for alerting on a message from ``node``."""
        visible = (self.app.is_active()
                   and self.app.stack.get_visible_child_name() == "messages"
                   and self.selected is not None and self.selected.get("conv") == conv)
        return (visible,
                self.app.session.cache.is_conversation_muted(conv),
                self.app.session.cache.is_muted(node))

    def _incoming_file(self, ev):
        node = ev.get("from")
        try:
            parsed = protocol.parse_file_payload(ev["payload"])
            chunk = protocol.open_file_chunk(self.app.session.identity, parsed["sealed"])
        except Exception:
            return
        if chunk.get("transfer_id") != parsed["tid"].hex() \
                or chunk.get("index") != parsed["index"] \
                or chunk.get("total") != parsed["total"]:
            return  # routing header does not match the sealed body
        conv = safe_conv_id(chunk.get("conv"), node)
        if conv is None:
            # The sender picked a conversation id we cannot put on disk.
            # Refuse the transfer instead of trusting it.
            return
        key = (conv, chunk["transfer_id"])
        buf = self._file_bufs.get(key)
        if buf is None:
            buf = {"total": chunk["total"], "meta": chunk, "chunks": {}, "row": None, "failed": False}
            self._file_bufs[key] = buf
            if self.selected is not None and self.selected.get("conv") == conv:
                buf["row"] = self._append_status_row(
                    f"Receiving {chunk.get('filename') or 'file'} - 1/{chunk['total']}")
        if buf.get("failed"):
            return
        buf["chunks"][chunk["index"]] = base64.b64decode(chunk["data"])
        have = len(buf["chunks"])
        if buf["row"] is not None:
            self._update_status_row(buf["row"], f"Receiving {chunk.get('filename') or 'file'} - {have}/{chunk['total']}")
            self._maybe_scroll()
        if have == buf["total"]:
            if not self._finalize_attachment(conv, node, buf, ev.get("seq", 0), ev.get("ts", 0),
                                              notify=True):
                self._mark_file_failed(buf)
            del self._file_bufs[key]

    def _append_status_row(self, text):
        row = Gtk.ListBoxRow()
        row.set_selectable(False)
        row.set_activatable(False)
        lbl = Gtk.Label(label=text)
        lbl.set_xalign(0.5)
        lbl.get_style_context().add_class("muted")
        row.add(lbl)
        self.conversation.add(row)
        row.show_all()
        return row

    def _update_status_row(self, row, text):
        lbl = row.get_child()
        if isinstance(lbl, Gtk.Label):
            lbl.set_text(text)

    def _mark_file_failed(self, buf):
        buf["failed"] = True
        meta = buf.get("meta") or {}
        if buf["row"] is not None:
            self._update_status_row(buf["row"], f"{meta.get('filename') or 'file'} - failed to receive")

    def _store_attachment(self, conv, tid, filename, data):
        # Belt and braces: the id is validated again right where it becomes a
        # path component, so no future caller can skip the check.
        safe = safe_conv_id(conv)
        if safe is None:
            raise ValueError(f"refusing to store an attachment under {conv!r}")
        d = os.path.join(attachments_dir(), safe)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"{tid}-{sanitize_filename(filename)}")
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        return path

    def _finalize_attachment(self, conv, node, buf, seq, ts, notify=False):
        chunk = buf["meta"]
        rebuilt = protocol.reassemble_file(buf["chunks"], buf["total"])
        if rebuilt is None:
            return False
        if hashlib.sha256(rebuilt).hexdigest() != chunk.get("sha256", ""):
            return False
        filename = chunk.get("filename") or "file"
        try:
            path = self._store_attachment(conv, chunk["transfer_id"], filename, rebuilt)
        except (OSError, ValueError) as e:
            # A transfer we cannot store safely (or at all) is dropped, not
            # left to break the rest of the sweep.
            self.app.activity.log(f"attachment refused: {e}")
            return False
        attachment = {"path": path, "filename": filename,
                      "mime_hint": chunk.get("mime_hint", ""), "size": len(rebuilt)}
        self.app.session.cache.add_recv(conv, node, seq, ts, filename, attachment=attachment)
        if buf["row"] is not None:
            buf["row"].destroy()
        thread = self.threads.get(conv)
        if thread is not None:
            thread["preview"] = filename
            if self.selected is thread:
                self.append_bubble(False, filename, ts,
                                   sender=self._sender_label(thread, {"sender": node}),
                                   attachment=attachment)
                self._maybe_scroll()
            if not self._is_viewing(thread):
                thread["unread"] += 1
            self._apply_order()
            self.update_thread_row(conv)
        run_async(lambda: self.app.session.client.ack_attachment(node, chunk["transfer_id"]))
        if notify:
            visible, muted, blocked = self._alert_state(conv, node)
            if should_notify(visible, muted, blocked, self.app.prefs):
                notify_new_message(self._sender_label(self.threads.get(conv) or {}, {"sender": node})
                                   or node[:8], f"file: {filename}")
            if should_play_sound(visible, muted, blocked, self.app.prefs):
                play_notification_sound(sound_enabled(self.app.prefs))
        return True

    def _process_fetched_transfer(self, node, tid, chunks):
        """Reassemble a transfer fetched from the daemon (startup sweep)."""
        buf = {"total": 0, "meta": {}, "chunks": {}, "row": None, "failed": False}
        seq, ts = 0, 0
        for entry in chunks:
            try:
                parsed = protocol.parse_file_payload(entry["payload"])
                chunk = protocol.open_file_chunk(self.app.session.identity, parsed["sealed"])
            except Exception:
                continue
            if chunk.get("transfer_id") != parsed["tid"].hex() \
                    or chunk.get("index") != parsed["index"] \
                    or chunk.get("total") != parsed["total"]:
                continue
            buf["total"] = chunk["total"]
            buf["meta"] = chunk
            buf["chunks"][chunk["index"]] = base64.b64decode(chunk["data"])
            if entry.get("seq", 0) > seq:
                seq = entry["seq"]
            if entry.get("ts", 0) > ts:
                ts = entry["ts"]
        if not buf["chunks"]:
            return
        conv = (buf["meta"].get("conv") or node)
        if self._finalize_attachment(conv, node, buf, seq, ts):
            pass

    def catchup_attachments(self):
        """Startup sweep: discover complete-but-unconsumed transfers on the daemon
        and reassemble them, mirroring catchup_unread for text.

        It has its own busy flag: the text sweep can still be running when
        this one is due, and sharing the flag used to mean the attachment
        sweep was skipped for good - files that arrived while the app was
        closed were never fetched.

        Returns False when the sweep could not run, so the caller can retry.
        """
        if self._catchup_attachments_busy:
            return False
        self._catchup_attachments_busy = True
        session = self.app.session
        nodes = set()
        for info in session.contacts().values():
            if info.get("node"):
                nodes.add(info["node"])
        for conv in session.cache.rooms():
            for n in session.cache.members(conv):
                if n != session.self_node:
                    nodes.add(n)

        def worker():
            jobs = []
            for n in nodes:
                try:
                    transfers = session.client.pending_attachments(n)
                except DaemonError:
                    continue
                for t in transfers:
                    try:
                        chunks = session.client.fetch_attachment(n, t["tid"])
                    except DaemonError:
                        continue
                    jobs.append((n, t["tid"], chunks))
            return jobs

        def done(jobs):
            self._catchup_attachments_busy = False
            for node, tid, chunks in jobs:
                self._process_fetched_transfer(node, tid, chunks)

        def fail(_e):
            self._catchup_attachments_busy = False

        run_async(worker, on_done=done, on_error=fail)
        return True
