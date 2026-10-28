"""The buddy list: one list for DMs and group chats, in a useful order.

The sidebar is the only place where "who am I talking to" is answered, so it
has to answer it the same way every time: a group chat is a conversation, not
a footnote, and the order of the list must not surprise anyone.
"""

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

from aimless import gtkui  # noqa: E402
from aimless.store import Store  # noqa: E402

SELF = "self-node"
NODE_A = "node-aaaa"
NODE_B = "node-bbbb"
NODE_C = "node-cccc"


# -- the store -------------------------------------------------------------

def test_last_activity_is_the_newest_message(tmp_path):
    store = Store(str(tmp_path / "state.db"), "pw")
    store.ingest(NODE_A, NODE_A, 1, 1000, "one")
    store.ingest(NODE_A, NODE_A, 2, 5000, "two")
    store.ingest(NODE_B, NODE_B, 1, 3000, "one")
    activity = store.last_activity()
    assert activity[NODE_A] == 5000
    assert activity[NODE_B] == 3000


def test_a_conversation_keeps_the_time_of_its_first_message(tmp_path):
    """A message that arrives today but was written last week is still a week
    old - filing it now must not make it look fresh."""
    store = Store(str(tmp_path / "state.db"), "pw")
    store.ingest(NODE_A, NODE_A, 1, 1000, "written last week")
    assert store.last_activity()[NODE_A] == 1000


def test_a_new_room_is_activity(tmp_path):
    """A room you just made has no messages yet, but making it is something
    you did - it must not land at the bottom of the list."""
    store = Store(str(tmp_path / "state.db"), "pw")
    store.ensure_room("room-1", {SELF: {"node": SELF, "pubkey": "", "screen": "Me"},
                                 NODE_A: {"node": NODE_A, "pubkey": "", "screen": "Ana"}})
    activity = store.last_activity()
    assert activity["room-1"] > 0


def test_a_room_keeps_the_time_it_was_opened(tmp_path):
    """Re-opening a room must not restart its clock."""
    store = Store(str(tmp_path / "state.db"), "pw")
    members = {SELF: {"node": SELF, "pubkey": "", "screen": "Me"},
               NODE_A: {"node": NODE_A, "pubkey": "", "screen": "Ana"}}
    store.ensure_room("room-1", members, created=1000)
    store.ensure_room("room-1", members, created=9999)
    assert store.last_activity()["room-1"] == 1000


def test_an_old_room_with_a_new_message_is_recent(tmp_path):
    store = Store(str(tmp_path / "state.db"), "pw")
    members = {SELF: {"node": SELF, "pubkey": "", "screen": "Me"},
               NODE_A: {"node": NODE_A, "pubkey": "", "screen": "Ana"}}
    store.ensure_room("room-1", members, created=1000)
    store.ingest("room-1", NODE_A, 1, 7000, "hello")
    assert store.last_activity()["room-1"] == 7000


# -- the sidebar -----------------------------------------------------------

class FakeActivity:
    def log(self, *_):
        pass


class FakeSession:
    """Just enough session for the sidebar: our contacts, the store, our node."""

    def __init__(self, store, contacts):
        self.store = store
        self.cache = store
        self._contacts = contacts
        self.self_node = SELF
        self.client = None

    def contacts(self):
        return dict(self._contacts)


class FakeApp:
    def __init__(self, session, prefs=None):
        self.session = session
        self.prefs = {} if prefs is None else prefs
        self.activity = FakeActivity()

    def refresh_unread_indicator(self):
        pass


def contact(node, screen):
    return {"node": node, "pubkey": "00" * 32, "screen": screen}


def make_view(tmp_path, monkeypatch, contacts=None, store=None):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("AIMLESS_HOME", str(home))
    store = store or Store(str(tmp_path / "state.db"), "pw")
    session = FakeSession(store, contacts or {})
    app = FakeApp(session)
    view = gtkui.MessagesView(app)
    return view, session, store


def visible_order(view):
    """The conversations the user can see, top to bottom."""
    out = []
    for row in view.thread_list.get_children():
        if row.get_visible():
            out.append(getattr(row, "aimless_conv", None))
    return out


def test_sidebar_holds_dms_and_rooms_in_one_list(tmp_path, monkeypatch):
    """A group chat is a conversation. It sits in the same list as the DMs,
    not in a separate box underneath."""
    contacts = {"ana": contact(NODE_A, "Ana"), "bo": contact(NODE_B, "Bo")}
    view, session, store = make_view(tmp_path, monkeypatch, contacts)
    store.ensure_room("room-1", {SELF: {"node": SELF, "pubkey": "", "screen": "Me"},
                                 NODE_A: {"node": NODE_A, "pubkey": "", "screen": "Ana"},
                                 NODE_B: {"node": NODE_B, "pubkey": "", "screen": "Bo"}})
    view.sync_sidebar()
    order = visible_order(view)
    assert set(order) == {NODE_A, NODE_B, "room-1"}
    assert "room-1" in order


def test_the_newest_conversation_is_first(tmp_path, monkeypatch):
    contacts = {"ana": contact(NODE_A, "Ana"), "bo": contact(NODE_B, "Bo"),
                "cy": contact(NODE_C, "Cy")}
    view, session, store = make_view(tmp_path, monkeypatch, contacts)
    store.ingest(NODE_A, NODE_A, 1, 1000, "old")
    store.ingest(NODE_B, NODE_B, 1, 9000, "new")
    store.ingest(NODE_C, NODE_C, 1, 5000, "middle")
    view.sync_sidebar()
    assert visible_order(view) == [NODE_B, NODE_C, NODE_A]


def test_unread_conversations_come_first(tmp_path, monkeypatch):
    contacts = {"ana": contact(NODE_A, "Ana"), "bo": contact(NODE_B, "Bo")}
    view, session, store = make_view(tmp_path, monkeypatch, contacts)
    store.ingest(NODE_A, NODE_A, 1, 9000, "new")
    store.ingest(NODE_B, NODE_B, 1, 1000, "old")
    view.sync_sidebar()
    view.threads[NODE_B]["unread"] = 1
    view._apply_order()
    assert visible_order(view)[0] == NODE_B


def test_the_order_toggle_switches_to_the_phone_book(tmp_path, monkeypatch):
    contacts = {"ana": contact(NODE_A, "Zoe"), "bo": contact(NODE_B, "Ana")}
    view, session, store = make_view(tmp_path, monkeypatch, contacts)
    store.ingest(NODE_A, NODE_A, 1, 9000, "new")
    store.ingest(NODE_B, NODE_B, 1, 1000, "old")
    view.sync_sidebar()
    assert visible_order(view) == [NODE_A, NODE_B], "recent is the default"

    view.on_toggle_order(None)
    assert visible_order(view) == [NODE_B, NODE_A], "by name: Ana before Zoe"
    assert view.app.prefs["sidebar_order"] == "alpha"

    view.on_toggle_order(None)
    assert visible_order(view) == [NODE_A, NODE_B], "and back to recent"
    assert view.app.prefs["sidebar_order"] == "recent"


def test_the_order_is_remembered_between_sessions(tmp_path, monkeypatch):
    contacts = {"ana": contact(NODE_A, "Ana")}
    view, session, store = make_view(tmp_path, monkeypatch, contacts)
    view.on_toggle_order(None)
    assert gtkui.load_prefs()["sidebar_order"] == "alpha"


def test_the_filter_narrows_the_list_while_you_type(tmp_path, monkeypatch):
    contacts = {"ana": contact(NODE_A, "Ana"), "bo": contact(NODE_B, "Bo"),
                "cy": contact(NODE_C, "Cy")}
    view, session, store = make_view(tmp_path, monkeypatch, contacts)
    view.sync_sidebar()
    assert len(visible_order(view)) == 3

    view.filter_entry.set_text("a")
    visible = visible_order(view)
    assert NODE_A in visible, "one letter is enough to narrow it down"
    assert NODE_B not in visible

    view.filter_entry.set_text("bo")
    assert visible_order(view) == [NODE_B]

    view.filter_entry.set_text("")
    assert len(visible_order(view)) == 3


def test_the_filter_matches_the_buddy_name_too(tmp_path, monkeypatch):
    contacts = {"ana": contact(NODE_A, "Ana")}
    view, session, store = make_view(tmp_path, monkeypatch, contacts)
    view.sync_sidebar()
    view.filter_entry.set_text("ana")
    assert visible_order(view) == [NODE_A]


def test_the_open_conversation_stays_where_it_is(tmp_path, monkeypatch):
    """A message in the conversation you are reading must not move the row out
    from under you."""
    contacts = {"ana": contact(NODE_A, "Ana"), "bo": contact(NODE_B, "Bo")}
    view, session, store = make_view(tmp_path, monkeypatch, contacts)
    store.ingest(NODE_A, NODE_A, 1, 9000, "new")
    store.ingest(NODE_B, NODE_B, 1, 1000, "old")
    view.sync_sidebar()
    assert visible_order(view) == [NODE_A, NODE_B]

    view.thread_list.select_row(view.threads[NODE_B]["row"])
    before = visible_order(view).index(NODE_B)

    # Ana gets a new message: she is the newest now, but Bo is the open one.
    store.ingest(NODE_A, NODE_A, 2, 99000, "newer")
    view._apply_order()
    assert visible_order(view).index(NODE_B) == before, "the open row must not jump"
    assert visible_order(view)[0] == NODE_A, "but the busy conversation is on top"


def test_a_room_you_just_made_comes_first(tmp_path, monkeypatch):
    contacts = {"ana": contact(NODE_A, "Ana"), "bo": contact(NODE_B, "Bo")}
    view, session, store = make_view(tmp_path, monkeypatch, contacts)
    store.ingest(NODE_A, NODE_A, 1, 1000, "old")
    store.ingest(NODE_B, NODE_B, 1, 2000, "old")
    view.sync_sidebar()
    assert visible_order(view) == [NODE_B, NODE_A]

    store.ensure_room("room-1", {SELF: {"node": SELF, "pubkey": "", "screen": "Me"},
                                 NODE_A: {"node": NODE_A, "pubkey": "", "screen": "Ana"},
                                 NODE_B: {"node": NODE_B, "pubkey": "", "screen": "Bo"}})
    view.sync_sidebar()
    assert visible_order(view)[0] == "room-1", "a new group chat is not a footnote"


def test_a_filtered_out_row_is_still_there_when_the_filter_goes_away(tmp_path, monkeypatch):
    contacts = {"ana": contact(NODE_A, "Ana"), "bo": contact(NODE_B, "Bo")}
    view, session, store = make_view(tmp_path, monkeypatch, contacts)
    view.sync_sidebar()
    view.filter_entry.set_text("ana")
    assert visible_order(view) == [NODE_A]
    view.filter_entry.set_text("")
    assert set(visible_order(view)) == {NODE_A, NODE_B}


def test_filtering_everything_out_says_so(tmp_path, monkeypatch):
    """An empty list should explain itself, not look like a broken app."""
    contacts = {"ana": contact(NODE_A, "Ana")}
    view, session, store = make_view(tmp_path, monkeypatch, contacts)
    view.sync_sidebar()
    view.filter_entry.set_text("zzz")
    assert visible_order(view) == []
    assert view.sidebar_no_match.get_visible()
    view.filter_entry.set_text("")
    assert not view.sidebar_no_match.get_visible()
