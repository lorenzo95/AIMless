"""The away status has to reach the buddy - and mean something there."""
import time

from test_e2e import two_nodes  # noqa: F401  (the fixture)

from aimless import crypto
from aimless.daemon import Client, DaemonClient


def _client(sock, name):
    return Client(DaemonClient(sock), crypto.new_identity(), name)


def test_the_away_message_reaches_the_buddy(two_nodes):
    """A buddy who set themselves away is reachable, but not at the keyboard.

    The status is a sealed blob the daemon only forwards, so this checks the
    whole path: the client seals it, the daemon pushes it to the peer, and the
    peer can read it back.

    The route between the two nodes takes a moment to come up, and a status
    sent before that is simply lost - so wait for it.
    """
    sock_a, sock_b = two_nodes
    alice = _client(sock_a, "Alice")
    bob = _client(sock_b, "Bob")
    a_node, b_node = alice.node_key(), bob.node_key()
    alice.add_contact(b_node)
    bob.add_contact(a_node)

    deadline = time.time() + 20
    while time.time() < deadline:
        alice.set_status(bytes(bob.identity.verify_key).hex(), b_node, "brb")
        time.sleep(1)
        entry = next((p for p in bob.presence() if p["key"] == a_node), None)
        if entry and entry.get("status_payload"):
            status = bob.decrypt_status(entry["status_payload"])
            assert status.get("away") == "brb", status
            assert status.get("screen") == "Alice", status
            return
    raise AssertionError("the away status never arrived at the buddy")
