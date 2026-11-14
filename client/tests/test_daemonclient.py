"""The daemon client's reply handling.

Every daemon op answers with an op name of its own ("send" -> "queued",
"block" -> "blocked"). The client has to know the mapping, or the reply is
treated as a stranger and the request times out - even though the daemon did
the work.
"""
import json
import socket
import threading

import pytest

from aimless.daemon import DaemonClient, DaemonError


def test_every_reply_op_is_mapped():
    """The names the daemon answers with. A missing entry means the reply is
    stashed and the caller waits out the timeout."""
    from aimless.daemon import RESPONSE_MAP
    for op, reply in (("send", "queued"), ("sendfile", "queued"),
                      ("setpeers", "peers"), ("watch", "watching"),
                      ("setstatus", "statusset"),
                      ("pendingattachments", "pendingattachments"),
                      ("fetchattachment", "fetchattachment"),
                      ("ackfile", "ackfile"),
                      ("block", "blocked"), ("unblock", "unblocked")):
        assert RESPONSE_MAP.get(op) == reply, (
            f"the daemon answers {op!r} with {reply!r} - without that mapping "
            "the request times out")


def _serve_reply(path, reply_op, tag):
    """A daemon that answers one request with an op of its own choosing."""
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(path)
    srv.listen(1)

    def run():
        conn, _ = srv.accept()
        f = conn.makefile("r")
        req = json.loads(f.readline())
        conn.sendall((json.dumps({"op": reply_op, "id": req.get("id"),
                                  "tag": tag}) + "\n").encode())
        conn.close()

    threading.Thread(target=run, daemon=True).start()
    return srv


def test_a_reply_with_its_own_op_name_is_not_a_timeout(tmp_path):
    """The block op answers "blocked" - the request must come back, not sit
    there until the timeout while the daemon has already done the work."""
    path = str(tmp_path / "api.sock")
    _serve_reply(path, "blocked", "yes")
    d = DaemonClient(path)
    try:
        resp = d.request("block", to="ab" * 32, timeout=5)
        assert resp.get("op") == "blocked", resp
        assert resp.get("tag") == "yes", resp
    finally:
        d.close()


def test_an_unknown_reply_op_still_times_out(tmp_path):
    """A reply we do not know about is a stranger - it must not be mistaken
    for the answer."""
    path = str(tmp_path / "api.sock")
    _serve_reply(path, "somethingelse", "no")
    d = DaemonClient(path)
    try:
        with pytest.raises(DaemonError):
            d.request("block", to="ab" * 32, timeout=1)
    finally:
        d.close()


def test_the_cli_and_the_window_agree_about_a_buddy():
    """`aimless list` used to call a buddy with a closed client "online" while
    the window showed "client offline"."""
    from aimless.cli import buddy_state
    from aimless.daemon import presence_state

    class _Client:
        def decrypt_status(self, payload):
            raise ValueError("no status")

    p = {"online": True, "client_attached": False}
    assert buddy_state(p, _Client()) == "no client"
    assert presence_state(p, _Client())[2] is True

    p = {"online": True, "client_attached": True}
    assert buddy_state(p, _Client()) == "online"

    p = {"online": False}
    assert buddy_state(p, _Client()) == "offline"


def test_sync_contact_addrs_tracks_region_changes():
    from aimless.daemon import Client
    from aimless import protocol
    import nacl.signing

    recv = nacl.signing.SigningKey.generate()
    recv_hex = bytes(recv.verify_key).hex()
    buddy = nacl.signing.SigningKey.generate()
    node = "ab" * 32

    class FakeDaemon:
        def __init__(self):
            self.watch = []

        def request(self, op, **kw):
            if op == "watch":
                self.watch.append((kw.get("to"), kw.get("addr")))
            return {}

    client = Client(FakeDaemon(), recv, "me")
    fd = client.daemon

    def presence_with(addr):
        payload = protocol.seal_status(buddy, recv_hex, "buddy", None, 1, addr=addr)
        return {node: {"status_payload": payload}}

    client.sync_contact_addrs(presence_with("tcADDR1"), known_nodes={node})
    assert fd.watch == [(node, "tcADDR1")]          # first sighting registers
    client.sync_contact_addrs(presence_with("tcADDR1"), known_nodes={node})
    assert len(fd.watch) == 1                       # unchanged -> no re-register
    client.sync_contact_addrs(presence_with("tcADDR2"), known_nodes={node})
    assert fd.watch[-1] == (node, "tcADDR2")        # region change -> new address
    client.sync_contact_addrs(presence_with("200::1"), known_nodes={node})
    assert len(fd.watch) == 2                       # non-tailcat address ignored
    other = "cd" * 32
    p = presence_with("tcADDR3")
    p[other] = p.pop(node)
    client.sync_contact_addrs(p, known_nodes={node})
    assert len(fd.watch) == 2                       # non-contact not auto-added
