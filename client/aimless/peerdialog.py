"""The Yggdrasil peer-manager dialog.

Extracted from gtkui.py: open_external (browser launcher), the PeerListBox
widget and ask_peers (setup-wizard step / live peer manager). The active
palette and window-icon helper come from uibase, read lazily so they stay
correct across theme switches.
"""
import subprocess

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib, Pango

from . import peers as peersmod


def open_external(url):
    """Open a URL in the user's browser.

    Used for the public peer list, so nobody has to go looking for it.
    """
    for opener in (["xdg-open", url], ["gio", "open", url]):
        try:
            subprocess.Popen(opener, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            return True
        except (OSError, FileNotFoundError):
            continue
    return False


class PeerListBox(Gtk.Box):
    """The peers we connect through, and how each one is doing.

    Used twice: in the setup wizard, where there is no daemon to ask yet, and
    in the peer manager, where each peer shows its live state.
    """

    def __init__(self, peer_list=None, live=False, defaults=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.live = live
        self.defaults = list(defaults or [])
        self._uris = list(peer_list or [])
        self._rows = {}

        self.list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.pack_start(self.list_box, False, False, 0)

        add_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.entry = Gtk.Entry(placeholder_text="tls://host:port  (or tcp://, quic://, ws://)")
        add_row.pack_start(self.entry, True, True, 0)
        add_btn = Gtk.Button(label="Add")
        add_btn.connect("clicked", self._on_add)
        add_row.pack_start(add_btn, False, False, 0)
        self.pack_start(add_row, False, False, 0)

        link = Gtk.Button(label="Browse the public peer list")
        link.set_halign(Gtk.Align.START)
        link.connect("clicked", self._on_link)
        self.pack_start(link, False, False, 0)

        self.restore_btn = Gtk.Button(label="Restore the built-in peers")
        self.restore_btn.set_halign(Gtk.Align.START)
        self.restore_btn.set_tooltip_text("Put the peers AIMless ships with back")
        self.restore_btn.connect("clicked", self._on_restore)
        self.restore_btn.set_no_show_all(True)
        self.pack_start(self.restore_btn, False, False, 0)

        self.status = Gtk.Label(label="")
        self.status.set_xalign(0.0)
        self.status.set_line_wrap(True)
        self.status.get_style_context().add_class("muted")
        self.pack_start(self.status, False, False, 0)

        self.restore_btn.set_visible(bool(self.defaults))
        self._rebuild()

    # -- the list ---------------------------------------------------------
    def get_peers(self):
        return list(self._uris)

    def set_peers(self, peer_list):
        self._uris = list(peer_list or [])
        self._rebuild()

    def add_peer(self, uri):
        """Add a peer. Returns None on success, or the reason it was refused."""
        uri = (uri or "").strip()
        problem = peersmod.validate_peer_uri(uri)
        if problem:
            self._say(problem)
            return problem
        if any(peersmod.peer_key(u) == peersmod.peer_key(uri) for u in self._uris):
            self._say("that peer is already in the list")
            return "already in the list"
        self._uris.append(uri)
        self._rebuild()
        self._say("")
        return None

    def remove_peer(self, uri):
        self._uris = [u for u in self._uris if u != uri]
        self._rebuild()

    # -- live state -------------------------------------------------------
    def set_states(self, details):
        """Show what the daemon says about each peer."""
        from .uibase import C
        by_key = {}
        for d in details or []:
            by_key[peersmod.peer_key(d.get("uri", ""))] = d
        for uri, label in self._rows.items():
            d = by_key.get(peersmod.peer_key(uri))
            label.get_style_context().remove_class("muted")
            if d is None:
                # Configured, but the daemon has no link for it: it was refused.
                label.set_markup(f"<span foreground='{C['away']}'>not connected</span>")
                continue
            state = peersmod.peer_state(d)
            colour = C["online"] if d.get("up") else C["away"]
            if d.get("error"):
                colour = C["danger"]
            label.set_markup(f"<span foreground='{colour}'>"
                             f"{GLib.markup_escape_text(state)}</span>")

    # -- internals --------------------------------------------------------
    def _say(self, text):
        self.status.set_text(text)

    def _on_add(self, _btn):
        self.add_peer(self.entry.get_text())
        self.entry.set_text("")

    def _on_link(self, _btn):
        if not open_external(peersmod.PEER_LIST_URL):
            self._say(f"could not open a browser - the peer list is at {peersmod.PEER_LIST_URL}")

    def _on_restore(self, _btn):
        """Put the built-in peers back.

        The way to undo "I deleted them all" - which is exactly how you end up
        with a node that cannot reach anyone.
        """
        if not self.defaults:
            self._say("this daemon does not report a built-in peer list")
            return
        self._uris = list(self.defaults)
        self._rebuild()
        self._say("the built-in peers are back - press Save to apply them")

    def _rebuild(self):
        for child in self.list_box.get_children():
            self.list_box.remove(child)
        self._rows = {}
        if not self._uris:
            empty = Gtk.Label(label="No peers - AIMless cannot reach the network.")
            empty.set_xalign(0.0)
            empty.get_style_context().add_class("muted")
            self.list_box.pack_start(empty, False, False, 0)
        for uri in self._uris:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            name = Gtk.Label(label=uri)
            name.set_xalign(0.0)
            name.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            name.set_selectable(True)
            name.set_max_width_chars(48)
            row.pack_start(name, True, True, 0)
            state = Gtk.Label(label="")
            state.set_xalign(0.0)
            row.pack_start(state, False, False, 0)
            rm = Gtk.Button(label="Remove")
            rm.connect("clicked", lambda _b, u=uri: self.remove_peer(u))
            row.pack_start(rm, False, False, 0)
            self.list_box.pack_start(row, False, False, 0)
            self._rows[uri] = state
        self.list_box.show_all()


def ask_peers(parent, peer_list, live=False, details=None, defaults=None):
    """The peers step of the setup wizard, and the peer manager.

    In the wizard this is the second step: which nodes we reach the network
    through. In the manager it also shows what each peer is doing right now.

    Returns the new peer list, or None if the user cancelled.
    """
    from .uibase import setup_app_icon
    title = "AIMless - peers" if live else "AIMless - step 2 of 2: peers"
    dlg = Gtk.Dialog(title=title, transient_for=parent, modal=True)
    setup_app_icon(dlg)
    if live:
        dlg.add_button("Close", Gtk.ResponseType.CLOSE)
        dlg.add_button("Save", Gtk.ResponseType.OK)
    else:
        dlg.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dlg.add_button("Finish", Gtk.ResponseType.OK)
    dlg.set_default_size(560, -1)
    box = dlg.get_content_area()
    box.set_spacing(10)
    box.set_border_width(14)

    head = Gtk.Label(label="AIMless reaches the network through public peers. "
                            "The built-in ones are nearby and checked every few minutes - "
                            "keep them, or add your own.")
    head.set_xalign(0.0)
    head.set_line_wrap(True)
    box.pack_start(head, False, False, 0)

    plist = PeerListBox(peer_list, live=live, defaults=defaults)
    box.pack_start(plist, False, False, 0)

    dlg.show_all()
    if live:
        plist.set_states(details)
    resp = dlg.run()
    peers = plist.get_peers() if resp == Gtk.ResponseType.OK else None
    dlg.destroy()
    return peers
