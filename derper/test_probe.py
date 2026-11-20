"""Offline tests for the DERP probe: the STUN request it builds and the host
plumbing. No network - the live checks are `python3 derper/probe.py <host>`.

Run: python3 -m pytest derper -q
"""
import struct
import zlib

import probe

CONTROLS = ("stun.cloudflare.com", "stun.l.google.com")


def test_stun_request_is_a_tailscale_flavored_binding_request():
    msg = probe.stun_request()
    typ, length, cookie = struct.unpack(">HHI", msg[:8])
    assert typ == 0x0001, "must be a Binding Request"
    assert cookie == 0x2112A442
    assert length == len(msg) - 20, "the length covers the attributes only"

    # SOFTWARE=tailnode, then a trailing FINGERPRINT over everything before it.
    at, al = struct.unpack(">HH", msg[20:24])
    assert at == 0x8022 and msg[24:24 + al] == b"tailnode"
    ft, fl, fp = struct.unpack(">HHI", msg[20 + length - 8:])
    assert ft == 0x8028 and fl == 4
    body = msg[:20 + length - 8]
    assert fp == ((zlib.crc32(body) & 0xFFFFFFFF) ^ 0x5354554E), \
        "derper drops requests whose FINGERPRINT does not validate"


def test_magic_is_the_eight_byte_derp_prefix():
    assert probe.MAGIC == b"DERP\xf0\x9f\x94\x91"
    assert len(probe.MAGIC) == 8


def test_help_does_not_touch_the_network(monkeypatch, capsys):
    def no_network(*a, **k):
        raise AssertionError("--help must not probe anything")
    monkeypatch.setattr(probe.socket, "getaddrinfo", no_network)
    monkeypatch.setattr(probe.socket, "create_connection", no_network)

    assert probe.main(["--help"]) == 0
    assert "Usage:" in capsys.readouterr().out


def test_host_argument_reaches_every_check(monkeypatch):
    seen = []

    def record(name, index):
        def fn(*a, **k):
            seen.append((name, a[index]))
            return True
        return fn

    # stun_test(family, label, host), https_get(path, host, expect),
    # derp_handshake(host)
    monkeypatch.setattr(probe, "stun_test", record("stun", 2))
    monkeypatch.setattr(probe, "https_get", record("get", 1))
    monkeypatch.setattr(probe, "derp_handshake", record("handshake", 0))

    assert probe.main(["derp.example.org"]) == 0
    checks = [s for s in seen if s[1] not in CONTROLS]
    assert checks == [("stun", "derp.example.org"), ("stun", "derp.example.org"),
                      ("get", "derp.example.org"), ("get", "derp.example.org"),
                      ("handshake", "derp.example.org")]


def test_default_host_comes_from_the_environment(monkeypatch):
    monkeypatch.setattr(probe, "DEFAULT_HOST", "derp.from.env")
    seen = []
    monkeypatch.setattr(probe, "stun_test", lambda *a, **k: seen.append(a[2]) or True)
    monkeypatch.setattr(probe, "https_get", lambda *a, **k: True)
    monkeypatch.setattr(probe, "derp_handshake", lambda host: seen.append(host) or True)

    assert probe.main([]) == 0
    assert seen.count("derp.from.env") == 3, "both STUN checks and the handshake"


def test_a_failing_check_sets_a_nonzero_exit(monkeypatch):
    monkeypatch.setattr(probe, "stun_test", lambda *a, **k: True)
    monkeypatch.setattr(probe, "https_get", lambda *a, **k: True)
    monkeypatch.setattr(probe, "derp_handshake", lambda host: False)

    assert probe.main(["derp.example.org"]) == 1
