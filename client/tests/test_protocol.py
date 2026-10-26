import base64

import pytest

from aimless import crypto, protocol


def _identity():
    return crypto.new_identity()


def test_invite_roundtrip():
    identity = _identity()
    node_hex = "ab" * 32
    invite = protocol.make_invite(identity, node_hex, "alice")
    client_hex, node_out, screen = protocol.parse_invite(invite)
    assert client_hex == bytes(identity.verify_key).hex()
    assert node_out == node_hex
    assert screen == "alice"
    assert invite.startswith("aimless1:")
    # base58-encoded key fields are ~44 chars, never the 64-char hex form
    field = invite[len("aimless1:"):].split(":")[0]
    assert len(field) != 64


def test_invite_legacy_hex_still_parses():
    identity = _identity()
    node_hex = "ab" * 32
    legacy = f"aimless1:{bytes(identity.verify_key).hex()}:{node_hex}:alice"
    client_hex, node_out, screen = protocol.parse_invite(legacy)
    assert client_hex == bytes(identity.verify_key).hex()
    assert node_out == node_hex
    assert screen == "alice"


def test_invite_malformed_key_raises():
    # '0', 'O', 'I', 'l' are not in the base58 alphabet and the field is not hex
    with pytest.raises(ValueError):
        protocol.parse_invite("aimless1:0OIl10:aabb:alice")
    # valid base58 but wrong length
    with pytest.raises(ValueError):
        protocol.parse_invite("aimless1:1111:aabb:alice")


def test_invite_bad_prefix():
    with pytest.raises(ValueError):
        protocol.parse_invite("nostr:abc:def:ghi")


def test_invite_bad_hex():
    with pytest.raises(ValueError):
        protocol.parse_invite("aimless1:zzzz:aabb:alice")


def test_invite_short_key():
    with pytest.raises(ValueError):
        protocol.parse_invite("aimless1:aabb:aabb:alice")


def test_invite_missing_screen():
    with pytest.raises(ValueError):
        protocol.parse_invite("aimless1:" + "ab" * 32 + ":" + "ab" * 32 + ":")


def test_message_seal_open():
    alice, bob = _identity(), _identity()
    bob_hex = bytes(bob.verify_key).hex()
    payload = protocol.seal_message(alice, bob_hex, "hello bob", 1234)
    opened = protocol.open_message(bob, payload)
    assert opened["text"] == "hello bob"
    assert opened["from"] == bytes(alice.verify_key).hex()
    assert opened["ts"] == 1234


def test_message_rejects_wrong_recipient():
    alice, bob, eve = _identity(), _identity(), _identity()
    bob_hex = bytes(bob.verify_key).hex()
    payload = protocol.seal_message(alice, bob_hex, "secret", 1)
    with pytest.raises(Exception):
        protocol.open_message(eve, payload)


def test_message_rejects_forged_signature():
    alice, bob, mallory = _identity(), _identity(), _identity()
    bob_hex = bytes(bob.verify_key).hex()
    payload = protocol.seal_message(alice, bob_hex, "hi", 1)
    _, curve_sk = crypto.curve_keys(bob)
    import nacl.public
    import json

    inner = nacl.public.SealedBox(nacl.public.PrivateKey(curve_sk)).decrypt(base64.b64decode(payload))
    obj = json.loads(inner)
    obj["from"] = bytes(mallory.verify_key).hex()
    inner2 = json.dumps(obj).encode()
    reforged = base64.b64encode(nacl.public.SealedBox(nacl.public.PrivateKey(curve_sk)).encrypt(inner2)).decode()
    with pytest.raises(ValueError):
        protocol.open_message(bob, reforged)


def test_status_seal_open():
    alice, bob = _identity(), _identity()
    bob_hex = bytes(bob.verify_key).hex()
    payload = protocol.seal_status(alice, bob_hex, "alice", "brb", 99)
    st = protocol.open_status(bob, payload)
    assert st["screen"] == "alice"
    assert st["away"] == "brb"
    assert st["from"] == bytes(alice.verify_key).hex()


def test_status_away_none():
    alice, bob = _identity(), _identity()
    bob_hex = bytes(bob.verify_key).hex()
    payload = protocol.seal_status(alice, bob_hex, "alice", None, 99)
    st = protocol.open_status(bob, payload)
    assert st["away"] is None


# -- room rosters ---------------------------------------------------------

def _member(node):
    return {"node": node, "pubkey": "cd" * 32, "screen": node[:8]}


def test_valid_room_members_accepts_a_roster_that_hashes_to_the_id():
    members = [_member("aa" * 32), _member("bb" * 32)]
    conv = protocol.room_id([m["node"] for m in members])
    roster = protocol.valid_room_members(conv, members)
    assert roster is not None, "a consistent roster must be accepted"
    assert set(roster) == {"aa" * 32, "bb" * 32}


def test_valid_room_members_rejects_a_roster_for_another_conversation():
    """A peer must not be able to hand us a roster for a room we are in - the
    roster decides who our replies are sealed to."""
    members = [_member("aa" * 32), _member("bb" * 32)]
    other = protocol.room_id(["cc" * 32, "dd" * 32])
    assert protocol.valid_room_members(other, members) is None


def test_valid_room_members_rejects_junk():
    assert protocol.valid_room_members("aa" * 32, None) is None
    assert protocol.valid_room_members("aa" * 32, []) is None
    assert protocol.valid_room_members("aa" * 32, [{"nope": 1}]) is None
    assert protocol.valid_room_members(None, [{"node": "aa" * 32}]) is None
    # a roster that is not a list of dicts must not blow up
    assert protocol.valid_room_members("aa" * 32, ["not-a-dict"]) is None
