"""Tailcat group rooms: the roster must carry each member's dial address so
recipients can dial them, the sender must register those addresses with its
daemon, and the room id must stay keyed on node ids (not addresses)."""
from aimless import crypto, protocol
from aimless.daemon import Client


class FakeDaemon:
    def __init__(self, key):
        self.key = key
        self.requests = []

    def request(self, op, **kw):
        self.requests.append((op, kw))
        if op == "whoami":
            return {"key": self.key, "address": "tcFAKE"}
        if op == "send":
            return {"seq": len(self.requests)}
        if op == "watch":
            return {"op": "watching"}
        return {}


def _members():
    me = crypto.new_identity()
    a = crypto.new_identity()
    b = crypto.new_identity()
    my_node = "aa" * 32
    members = [
        {"node": my_node, "pubkey": bytes(me.verify_key).hex(), "screen": "me", "addr": "tcME"},
        {"node": "bb" * 32, "pubkey": bytes(a.verify_key).hex(), "screen": "a", "addr": "tcA"},
        {"node": "cc" * 32, "pubkey": bytes(b.verify_key).hex(), "screen": "b", "addr": "tcB"},
    ]
    return me, a, b, my_node, members


def test_send_room_roster_carries_addr_and_registers():
    me, a, b, my_node, members = _members()
    conv = protocol.room_id([m["node"] for m in members])
    fake = FakeDaemon(my_node)
    client = Client(fake, me, "me")
    client.send_room(members, conv, "hello room", 123)

    sends = [kw for op, kw in fake.requests if op == "send"]
    assert len(sends) == 2  # self skipped

    # The sender registered each buddy's dial address (watch carries addr).
    watches = {kw["to"]: kw.get("addr") for op, kw in fake.requests if op == "watch"}
    assert watches == {"bb" * 32: "tcA", "cc" * 32: "tcB"}

    # The roster inside the sealed message carries the addresses, so the
    # recipient can dial the other members.
    payload_a = next(kw["payload"] for kw in sends if kw["to"] == "bb" * 32)
    opened = protocol.open_message(a, payload_a)
    assert opened["conv"] == conv
    roster = {m["node"]: m for m in opened["members"]}
    assert roster["cc" * 32]["addr"] == "tcB"
    assert roster[my_node]["addr"] == "tcME"


def test_room_id_ignores_addr_and_validation_preserves_it():
    nodes = ["bb" * 32, "aa" * 32]
    rid = protocol.room_id(nodes)
    members = [{"node": "aa" * 32, "pubkey": "x", "screen": "s", "addr": "tcAAA"},
               {"node": "bb" * 32, "pubkey": "y", "screen": "t", "addr": "tcBBB"}]
    # Address does not change the room id.
    assert protocol.room_id([m["node"] for m in members]) == rid
    valid = protocol.valid_room_members(rid, members)
    assert valid is not None
    assert valid["aa" * 32]["addr"] == "tcAAA"
    # A roster that does not hash to the conv is still rejected.
    assert protocol.valid_room_members("0" * 64, members) is None


def test_send_room_yggdrasil_has_no_addr():
    # Yggdrasil members carry no addr: no watch-addr registration, roster addr "".
    me, a, b, my_node, members = _members()
    for m in members:
        m["addr"] = ""
    conv = protocol.room_id([m["node"] for m in members])
    fake = FakeDaemon(my_node)
    client = Client(fake, me, "me")
    client.send_room(members, conv, "hi", 1)
    watches = [kw for op, kw in fake.requests if op == "watch"]
    assert watches == []  # nothing to register without addresses


def test_register_room_members_admits_non_buddies():
    """A room can include people who are not buddies. The tailcat daemon admits
    only registered peers, so learning a roster must register every member's
    dial address or their handshake is ignored and nothing they send arrives."""
    me, a, b, my_node, members = _members()
    fake = FakeDaemon(my_node)
    client = Client(fake, me, "me")
    client.register_room_members(members)
    watches = {kw["to"]: kw.get("addr") for op, kw in fake.requests if op == "watch"}
    assert watches == {"bb" * 32: "tcA", "cc" * 32: "tcB"}  # self skipped


def test_register_room_members_skips_missing_addr_and_self():
    me, a, b, my_node, members = _members()
    fake = FakeDaemon(my_node)
    client = Client(fake, me, "me")
    client.register_room_members([
        {"node": my_node, "addr": "tcME"},          # self: never watch ourselves
        {"node": "bb" * 32, "pubkey": "x"},          # no addr: nothing to admit
        {"node": "", "addr": "tcA"},                 # no node
    ])
    assert [kw for op, kw in fake.requests if op == "watch"] == []