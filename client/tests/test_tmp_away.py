import time
from test_e2e import two_nodes
from aimless import crypto
from aimless.daemon import DaemonClient, Client


def test_away_reaches_the_buddy(two_nodes):
    sock_a, sock_b = two_nodes
    a_id = crypto.new_identity()
    b_id = crypto.new_identity()
    a = Client(DaemonClient(sock_a), a_id, "Alice")
    b = Client(DaemonClient(sock_b), b_id, "Bob")
    a_node, b_node = a.node_key(), b.node_key()
    a.add_contact(b_node)
    b.add_contact(a_node)
    time.sleep(1.5)

    print("B sees A before:", b.presence())
    a.set_status(bytes(b_id.verify_key).hex(), b_node, "brb")
    time.sleep(1.5)
    pres = b.presence()
    print("B sees A after :", pres)
    entry = [p for p in pres if p["key"] == a_node]
    print("entry:", entry)
    if entry and entry[0].get("status_payload"):
        print("decoded:", b.decrypt_status(entry[0]["status_payload"]))
