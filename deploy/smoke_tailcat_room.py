"""Real-world tailcat ROOM smoke: three aimlessd-tailcat daemons over PUBLIC
DERP. Alice fans out a room message; Bob and Carol receive it and learn the
roster (with dial addresses); Bob replies and Alice+Carol receive it.

Proves group rooms work over the internet: roster address propagation,
client-side fan-out, and bidirectional delivery. Needs the public DERP relays.

Usage:
    python3 deploy/smoke_tailcat_room.py
    AIMLESSD_TAILCAT=/path/to/aimlessd-tailcat python3 deploy/smoke_tailcat_room.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "client"))

from aimless import crypto, protocol  # noqa: E402
from aimless.daemon import Client, DaemonClient  # noqa: E402


def build_binary(tmp):
    existing = os.environ.get("AIMLESSD_TAILCAT")
    if existing:
        return existing
    go = shutil.which("go")
    if go is None:
        print("go toolchain not found; set AIMLESSD_TAILCAT", file=sys.stderr)
        sys.exit(2)
    binpath = os.path.join(tmp, "aimlessd-tailcat")
    subprocess.run([go, "build", "-tags", "tailcat", "-o", binpath, "."],
                   cwd=os.path.join(ROOT, "daemon"), check=True, capture_output=True)
    return binpath


def main():
    tmp = tempfile.mkdtemp(prefix="aimless-tc-room-")
    binpath = build_binary(tmp)
    procs = []

    def start(name):
        d = os.path.join(tmp, name, "daemon")
        os.makedirs(d, exist_ok=True)
        sock = os.path.join(d, "api.sock")
        procs.append(subprocess.Popen(
            [binpath, "-datadir", d, "-api", sock, "-retry", "500ms", "-probe", "500ms"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        for _ in range(120):
            if os.path.exists(sock):
                time.sleep(0.5)
                return sock
            time.sleep(0.25)
        raise RuntimeError(f"{name} socket never appeared")

    def recv_room(dclient, identity, from_node, want, timeout=60):
        for _ in range(timeout):
            h = dclient.request("history", **{"from": from_node, "seq": 0})
            for m in h.get("msgs", []):
                try:
                    opened = protocol.open_message(identity, m["payload"])
                except Exception:
                    continue
                if opened.get("text") == want and opened.get("conv"):
                    return opened
            time.sleep(1)
        return None

    try:
        sa, sb, sc = start("a"), start("b"), start("c")
        da, db, dc = DaemonClient(sa), DaemonClient(sb), DaemonClient(sc)
        ia, ib, ic = crypto.new_identity(), crypto.new_identity(), crypto.new_identity()
        wa, wb, wc = da.request("whoami"), db.request("whoami"), dc.request("whoami")
        members = [
            {"node": wa["key"], "pubkey": bytes(ia.verify_key).hex(), "screen": "alice", "addr": wa["address"]},
            {"node": wb["key"], "pubkey": bytes(ib.verify_key).hex(), "screen": "bob", "addr": wb["address"]},
            {"node": wc["key"], "pubkey": bytes(ic.verify_key).hex(), "screen": "carol", "addr": wc["address"]},
        ]
        # Forming a room registers every member's dial address with each daemon,
        # exactly as the client does via add_contact when a roster is sent or
        # accepted. A receiver needs this to attribute an inbound connection.
        for d in (da, db, dc):
            for m in members:
                d.request("watch", to=m["node"], addr=m["addr"])
        conv = protocol.room_id([m["node"] for m in members])
        ca, cb = Client(da, ia, "alice"), Client(db, ib, "bob")
        print("3 tailcat daemons up; room", conv[:16])

        ca.send_room(members, conv, "hello room", int(time.time() * 1000))
        ob = recv_room(db, ib, wa["key"], "hello room")
        oc = recv_room(dc, ic, wa["key"], "hello room")
        assert ob and oc, "Bob/Carol did not receive the room message"
        roster_b = {m["node"]: m for m in ob["members"]}
        assert roster_b[wc["key"]]["addr"] == wc["address"], "roster lost Carol's address"
        print("Alice -> room: Bob and Carol received; roster carries addresses")

        cb.send_room(members, conv, "reply from bob", int(time.time() * 1000))
        ra = recv_room(da, ia, wb["key"], "reply from bob")
        rc = recv_room(dc, ic, wb["key"], "reply from bob")
        assert ra and rc, "Alice/Carol did not receive Bob's reply"
        print("Bob -> room: Alice and Carol received the reply")
        print("TAILCAT ROOM SMOKE PASSED")
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()