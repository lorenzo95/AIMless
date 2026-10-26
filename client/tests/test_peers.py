import pytest

from aimless import peers


# -- what a peer URI may look like -----------------------------------------

@pytest.mark.parametrize("uri", [
    "tls://host.example:443",
    "tcp://10.0.0.1:9000",
    "quic://host.example:65535",
    "ws://host.example:80",
    "wss://host.example:443",
    "tls://ca.us.ygg.informatics.coop:443?key=0000000023fc6ffb62afb79448aefb0943df2e4472cf08bd489f5407378300a2",
    "unix:///run/aimless/peer.sock",
])
def test_good_peer_uris_are_accepted(uri):
    assert peers.validate_peer_uri(uri) is None, peers.validate_peer_uri(uri)


@pytest.mark.parametrize("uri,why", [
    ("", "empty"),
    ("   ", "empty"),
    ("host.example:443", "protocol"),
    ("ftp://host.example:21", "protocol"),
    ("tls://host.example", "port"),
    ("tls://:443", "host"),
    ("tls://host.example:0", "port"),
    ("tls://host.example:70000", "port"),
    ("tls://host.example:not-a-port", "port"),
    ("tls://host.example:443/../etc", "path"),
    ("tls://host.example:443; rm -rf /", "metacharacter"),
    ("tls://host.example:443|cat", "metacharacter"),
    ("tls://../../etc/passwd:443", ".."),
])
def test_bad_peer_uris_are_refused(uri, why):
    problem = peers.validate_peer_uri(uri)
    assert problem is not None, f"{uri!r} should have been refused ({why})"


def test_a_peer_uri_keeps_its_key_option():
    uri = ("tls://host.example:443?key="
           "0000000023fc6ffb62afb79448aefb0943df2e4472cf08bd489f5407378300a2")
    assert peers.is_valid_peer_uri(uri)


# -- matching a configured peer to the one the daemon reports --------------

def test_peer_key_ignores_case_and_options():
    """The daemon reports a peer without its options - the library identifies
    a link by scheme, host and port only."""
    a = "tls://Host.Example:443?key=aa&sni=bb"
    b = "tls://host.example:443"
    assert peers.peer_key(a) == peers.peer_key(b)
    assert peers.peer_key(b) == peers.peer_key("tls://host.example:443?sni=bb")


def test_peer_key_keeps_different_peers_apart():
    assert peers.peer_key("tls://a.example:443") != peers.peer_key("tls://b.example:443")
    assert peers.peer_key("tls://a.example:443") != peers.peer_key("tcp://a.example:443")
    assert peers.peer_key("tls://a.example:443") != peers.peer_key("tls://a.example:8443")


# -- reading a peer's state ------------------------------------------------

def test_describe_an_up_peer():
    text = peers.describe_peer({"uri": "tls://a.example:443", "up": True,
                                 "latency_ms": 43, "rx_bytes": 2048, "tx_bytes": 1024})
    assert "up" in text
    assert "43 ms" in text
    assert "2.0 KB in" in text


def test_describe_a_down_peer():
    assert "down" in peers.describe_peer({"uri": "tls://a.example:443", "up": False})


def test_describe_a_peer_that_could_not_be_used():
    text = peers.describe_peer({"uri": "tls://a.example:443", "up": False,
                                "error": "cannot resolve a.example"})
    assert "not usable" in text
    assert "cannot resolve" in text


def test_format_bytes():
    assert peers.format_bytes(0) == "0 B"
    assert peers.format_bytes(512) == "512 B"
    assert peers.format_bytes(2048) == "2.0 KB"
    assert peers.format_bytes(5 * 1024 * 1024) == "5.0 MB"


def test_peer_state_has_no_uri():
    """The peer list shows the URI in its own column, so the state is just
    the state."""
    state = peers.peer_state({"uri": "tls://a.example:443", "up": True,
                               "latency_ms": 43})
    assert state == "up · 43 ms"
    assert "a.example" not in state
