"""Relay/transport display helpers for the AIMless client.

Extracted from gtkui.py. Pure status formatting (transport label, short
address, relay tooltip) plus the country-sorted DERP region picker widget.
Kept separate so the transport wording lives in one place and can be tested
without building a window.
"""
import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk


def is_tailcat(st):
    """True when the daemon runs the tailcat transport (vs Yggdrasil)."""
    return bool(st) and st.get("transport") == "tailcat"


def net_label(st):
    """Short network label for the status/route bars. Yggdrasil counts peers;
    tailcat's relay state is already shown by the status dot, so no extra label."""
    if not st:
        return "peers 0/0"
    if is_tailcat(st):
        return ""
    return f"peers {st.get('peers_up', 0)}/{st.get('peers_total', 0)}"


def short_address(addr, n=28):
    """A tailcat address is ~217 chars; show a readable prefix. The full value
    lives in the tooltip and the invite."""
    if not addr:
        return ""
    return addr if len(addr) <= n else addr[:n] + "…"


def relay_tooltip(st):
    """Hover text for the tailcat route bar: the relay region, not the long
    address (which lives in the invite)."""
    region = st.get("region") or {}
    name = region.get("name") or region.get("code")
    base = f"Relay region: {name}" if name else "tailcat relay"
    nodes = region.get("nodes") or []
    if nodes:
        base += "\nRelay servers: " + ", ".join(nodes[:3])
        if len(nodes) > 3:
            base += f" (+{len(nodes) - 3} more)"
    for p in st.get("peer_details") or []:
        if p.get("error"):
            base += "\nRelay problem: " + p["error"]
            break
    return base + "\nYour full address is in your invite (Contacts -> Copy)."


# Country labels for the official DERP regions so the picker can sort by country
# and type-ahead jumps (c -> Canada, g -> Germany). Unknown/custom regions fall
# back to their own name.
REGION_COUNTRY = {
    "nyc": "United States", "sfo": "United States", "dfw": "United States",
    "sea": "United States", "ord": "United States", "den": "United States",
    "mia": "United States", "lax": "United States", "hnl": "United States",
    "iad": "United States",
    "tor": "Canada", "sao": "Brazil", "lhr": "United Kingdom", "fra": "Germany",
    "nue": "Germany", "par": "France", "mad": "Spain", "ams": "Netherlands",
    "waw": "Poland", "hel": "Finland", "sin": "Singapore", "tok": "Japan",
    "hkg": "Hong Kong", "bom": "India", "syd": "Australia", "jnb": "South Africa",
    "nai": "Kenya", "dbi": "United Arab Emirates",
}


def region_label(r):
    name = r.get("name") or r.get("code", "")
    code = r.get("code", "")
    country = REGION_COUNTRY.get(code)
    if country:
        return f"{country} - {name} ({code})"
    return f"{name} ({code})"


def make_region_list(regions, current_id):
    """A scrollable, country-sorted region list with type-ahead search. Returns
    (scrolled_window, treeview)."""
    ordered = sorted(regions, key=lambda r: region_label(r).lower())
    store = Gtk.ListStore(str, int)  # label, region id
    for r in ordered:
        store.append([region_label(r), r.get("id", 0)])
    view = Gtk.TreeView(model=store)
    view.set_headers_visible(False)
    view.set_enable_search(True)
    view.set_search_column(0)
    view.append_column(Gtk.TreeViewColumn("Region", Gtk.CellRendererText(), text=0))
    if ordered:
        sel = view.get_selection()
        for i, r in enumerate(ordered):
            if r.get("id") == current_id:
                it = store.iter_nth_child(None, i)
                sel.select_iter(it)
                view.scroll_to_cell(store.get_path(it), None, False, 0, 0)
                break
    scroll = Gtk.ScrolledWindow()
    scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
    scroll.set_min_content_height(180)
    scroll.set_shadow_type(Gtk.ShadowType.IN)
    scroll.add(view)
    return scroll, view
