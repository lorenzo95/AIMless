"""Real-world tailcat smoke: two aimlessd-tailcat daemons over PUBLIC DERP
exchange a message via the daemon API.

Unlike deploy/smoke.py (Yggdrasil, local peers), this exercises the tailcat
transport over Tailscale's public relays, proving rendezvous + delivery work over
the internet, not just an in-process local DERP. Needs network access to
https://tailcat.dev/derpmap.json and the public DERP relays.

Usage:
    python3 deploy/smoke_tailcat.py            # builds aimlessd-tailcat first
    AIMLESSD_TAILCAT=/path/to/aimlessd-tailcat python3 deploy/smoke_tailcat.py
"""
import base64
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CLIENT = os.path.join(ROOT, "client")
DAEMON = os.path.join(ROOT, "daemon")
sys.path.insert(0, CLIENT)

from aimless.daemon import DaemonClient  # noqa: E402


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
                   cwd=DAEMON, check=True, capture_output=True)
    return binpath


def main():
    tmp = tempfile.mkdtemp(prefix="aimless-tc-smoke-")
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

    try:
        sa, sb = start("a"), start("b")
        da, db = DaemonClient(sa), DaemonClient(sb)
        wa, wb = da.request("whoami"), db.request("whoami")
        assert wa["address"].startswith("tc"), "expected a tailcat address"
        print("both tailcat daemons up (public DERP)")
        da.request("watch", to=wb["key"], addr=wb["address"])
        db.request("watch", to=wa["key"], addr=wa["address"])
        payload = base64.b64encode(b"hello over public DERP").decode()
        da.request("send", to=wb["key"], payload=payload)
        for _ in range(90):
            h = db.request("history", **{"from": wa["key"], "seq": 0})
            if h.get("msgs"):
                body = base64.b64decode(h["msgs"][0]["payload"])
                assert body == b"hello over public DERP", body
                print("online delivery over public DERP: OK")
                print("TAILCAT SMOKE PASSED")
                return
            time.sleep(1)
        print("FAIL: no message delivered over public DERP", file=sys.stderr)
        sys.exit(1)
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