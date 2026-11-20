#!/usr/bin/env python3
"""Probe a DERP relay: STUN, /derp/probe, /generate_204 and the DERP upgrade.

Usage:
    python3 probe.py derp.example.com
    DERP_DOMAIN=derp.example.com python3 probe.py

Stdlib only, no dependencies. Run it from any machine - not just the relay
host - so it proves the relay is reachable from the outside. Exit status is 0
when every check passes, 1 otherwise.

The two control STUN checks at the end are deliberate: if they fail too, your
network is blocking STUN and the relay is not necessarily at fault.
"""
import os
import socket
import ssl
import struct
import sys
import time
import zlib

MAGIC = "DERP🔑".encode()  # 8 bytes
DEFAULT_HOST = os.environ.get("DERP_DOMAIN") or "derp.example.com"


def stun_request():
    """Tailscale-flavored STUN binding request: derper only answers requests
    carrying SOFTWARE="tailnode" and a valid trailing FINGERPRINT attribute."""
    software = b"tailnode"
    attrs = struct.pack(">HH", 0x8022, len(software)) + software
    msg = struct.pack(">HHI", 0x0001, len(attrs) + 8, 0x2112A442) + os.urandom(12) + attrs
    fp = (zlib.crc32(msg) & 0xFFFFFFFF) ^ 0x5354554E
    return msg + struct.pack(">HHI", 0x8028, 4, fp)


def stun_test(family, label, host, port=3478):
    try:
        infos = socket.getaddrinfo(host, port, family, socket.SOCK_DGRAM)
        addr = infos[0][4]
        req = stun_request()
        s = socket.socket(family, socket.SOCK_DGRAM)
        s.settimeout(5)
        t0 = time.monotonic()
        s.sendto(req, addr)
        data, _ = s.recvfrom(2048)
        rtt = (time.monotonic() - t0) * 1000
        s.close()
        typ, length, cookie = struct.unpack(">HHI", data[:8])
        assert typ == 0x0101 and cookie == 0x2112A442, f"bad STUN response type={typ:#x}"
        attrs, i = data[20:20 + length], 0
        mapped = None
        while i + 4 <= len(attrs):
            at, al = struct.unpack(">HH", attrs[i:i + 4])
            val = attrs[i + 4:i + 4 + al]
            if at == 0x0020:  # XOR-MAPPED-ADDRESS
                xport = struct.unpack(">H", val[2:4])[0] ^ 0x2112
                if val[1] == 0x01:
                    ip = socket.inet_ntop(socket.AF_INET, bytes(b ^ c for b, c in zip(val[4:8], struct.pack(">I", 0x2112A442))))
                else:
                    xor = struct.pack(">I", 0x2112A442) + data[8:20]
                    ip = socket.inet_ntop(socket.AF_INET6, bytes(b ^ c for b, c in zip(val[4:20], xor)))
                mapped = f"{ip}:{xport}"
            i += 4 + al + ((4 - al % 4) % 4)
        print(f"[OK]   STUN/{label}: binding response in {rtt:.1f} ms, our mapped address = {mapped}")
        return True
    except Exception as e:
        print(f"[FAIL] STUN/{label}: {e}")
        return False


def https_get(path, host, expect=None):
    ctx = ssl.create_default_context()
    t0 = time.monotonic()
    with socket.create_connection((host, 443), timeout=10) as raw:
        with ctx.wrap_socket(raw, server_hostname=host) as s:
            s.sendall(f"GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
            buf = b""
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
    rtt = (time.monotonic() - t0) * 1000
    status_line = buf.split(b"\r\n", 1)[0].decode()
    code = status_line.split(" ")[1] if " " in status_line else "?"
    body = buf.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in buf else b""
    ok = expect is None or code == expect
    print(f"[{'OK' if ok else 'FAIL'}]   GET {path}: {status_line} ({rtt:.1f} ms) body={body[:60]!r}")
    return ok


def derp_handshake(host):
    ctx = ssl.create_default_context()  # no ALPN -> HTTP/1.1, verifies cert chain
    t0 = time.monotonic()
    with socket.create_connection((host, 443), timeout=10) as raw:
        with ctx.wrap_socket(raw, server_hostname=host) as s:
            s.sendall(f"GET /derp HTTP/1.1\r\nHost: {host}\r\nUpgrade: DERP\r\nConnection: Upgrade\r\n\r\n".encode())
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
            head, _, rest = buf.partition(b"\r\n\r\n")
            status = head.split(b"\r\n", 1)[0].decode()
            if "101" not in status:
                print(f"[FAIL] DERP upgrade: {status}\n{head.decode(errors='replace')}")
                return False
            # first frame: 1-byte type + 4-byte big-endian length + payload
            data = rest
            while len(data) < 5:
                data += s.recv(4096)
            ftype, flen = data[0], struct.unpack(">I", data[1:5])[0]
            while len(data) < 5 + flen:
                data += s.recv(4096)
            payload = data[5:5 + flen]
            rtt = (time.monotonic() - t0) * 1000
            if payload.startswith(MAGIC) and flen == len(MAGIC) + 32:
                key = payload[len(MAGIC):].hex()
                print(f"[OK]   DERP upgrade: {status} in {rtt:.1f} ms; server key frame (type {ftype:#04x}), node key = {key}")
                return True
            print(f"[FAIL] DERP upgrade: unexpected first frame type={ftype:#04x} len={flen} payload={payload[:48]!r}")
            return False


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    host = argv[0] if argv else DEFAULT_HOST
    print(f"--- probing DERP relay {host} ---")
    results = [
        stun_test(socket.AF_INET, "IPv4", host),
        stun_test(socket.AF_INET6, "IPv6", host),
        https_get("/derp/probe", host, "200"),
        https_get("/generate_204", host, "204"),
        derp_handshake(host),
    ]
    print("--- control: public STUN from this machine ---")
    stun_test(socket.AF_INET, "control-cloudflare", "stun.cloudflare.com")
    stun_test(socket.AF_INET, "control-google", "stun.l.google.com", port=19302)
    print("---")
    print("ALL PASS" if all(results) else "SOME CHECKS FAILED")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
