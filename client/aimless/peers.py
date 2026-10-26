"""The peer list: what a peer URI may look like, and how to read its state.

A peer is a public Yggdrasil node we connect to, so the rest of the network
can reach us. The URIs come from three places - the built-in list, the
daemon's config file, and the user - so they are validated in one place
before anything is dialled or stored.

The format is the one Yggdrasil itself uses::

    [protocol]://[host]:[port][?options]

with tcp, tls, quic, ws, wss and unix supported, and options such as
``?key=`` (pin the peer's public key) or ``?sni=``.
"""

import re
from urllib.parse import urlsplit

# Where the current list of public peers lives. The app links to it so nobody
# has to go hunting for it.
PEER_LIST_URL = "https://publicpeers.neilalexander.dev/"

PEER_SCHEMES = ("tcp", "tls", "quic", "ws", "wss", "unix")

_PORT_RE = re.compile(r"^\d+$")
# Characters that have no business in a peer URI. A peer URI ends up in a
# config file and in a URL parser, so shell metacharacters and whitespace are
# refused rather than escaped.
_BAD_CHARS = set(" \t\r\n;|&`$><'\"\\()")


def validate_peer_uri(uri: str):
    """Check a peer URI.

    Returns None when the URI is usable, or a message explaining why it is
    not. The message is shown to the user, so it says what is wrong.
    """
    if not uri or not uri.strip():
        return "a peer URI cannot be empty"
    uri = uri.strip()
    bad = sorted(set(uri) & _BAD_CHARS)
    if bad:
        return f"a peer URI cannot contain {' '.join(bad)}"
    if ".." in uri:
        return "a peer URI cannot contain .."

    try:
        parts = urlsplit(uri)
    except ValueError as e:
        return f"that is not a valid peer URI: {e}"

    scheme = parts.scheme.lower()
    if not scheme:
        return "a peer URI needs a protocol, e.g. tls://host:port"
    if scheme not in PEER_SCHEMES:
        return (f"unknown protocol {scheme!r} - use one of: "
                + ", ".join(scheme + "://" for scheme in PEER_SCHEMES))

    if scheme == "unix":
        if not parts.path:
            return "a unix peer URI needs a socket path"
        return None

    try:
        host = parts.hostname
        port = parts.port
    except ValueError as e:
        return f"that is not a valid peer URI: {e}"
    if not host:
        return "a peer URI needs a host, e.g. tls://host:port"
    if port is None:
        return "a peer URI needs a port, e.g. tls://host:port"
    if not _PORT_RE.match(str(port)) or not 1 <= int(port) <= 65535:
        return f"{port} is not a valid port"
    if parts.path not in ("", "/"):
        return "a peer URI cannot have a path"
    return None


def is_valid_peer_uri(uri: str) -> bool:
    """True when the URI is usable as a peer."""
    return validate_peer_uri(uri) is None


def peer_key(uri: str) -> str:
    """A key that matches a configured peer to the one the daemon reports.

    The network library identifies a link by scheme, host and port - the
    options (?key=, ?sni=) are not part of its identity, and it strips them
    from the URI it reports. So the key ignores them too, otherwise
    "tls://host:443?key=..." would never match the "tls://host:443" the daemon
    reports back.
    """
    try:
        parts = urlsplit((uri or "").strip())
    except ValueError:
        return (uri or "").strip().lower()
    host = (parts.hostname or "").lower()
    try:
        port = parts.port
    except ValueError:
        port = None
    if host and ":" in host:
        netloc = f"[{host}]"
    else:
        netloc = host
    if port:
        netloc = f"{netloc}:{port}"
    return f"{parts.scheme.lower()}://{netloc}{parts.path}"


def format_bytes(n) -> str:
    """A short byte count for the peer list."""
    try:
        n = int(n or 0)
    except (TypeError, ValueError):
        return "0 B"
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    return f"{n / (1024 * 1024 * 1024):.1f} GB"


def peer_state(peer: dict) -> str:
    """What is happening with one peer.

    ``peer`` is one entry of the daemon's status reply: uri, up, latency_ms,
    rx_bytes, tx_bytes and (when it is not usable) error.
    """
    error = peer.get("error") or ""
    if error:
        return f"not usable: {error}"
    if not peer.get("up"):
        return "down"
    parts = ["up"]
    latency = peer.get("latency_ms")
    if latency:
        parts.append(f"{latency} ms")
    rx = peer.get("rx_bytes")
    tx = peer.get("tx_bytes")
    if rx or tx:
        parts.append(f"{format_bytes(rx)} in / {format_bytes(tx)} out")
    if peer.get("inbound"):
        parts.append("inbound")
    return " · ".join(parts)


def describe_peer(peer: dict) -> str:
    """One line describing a peer: its URI and what is happening with it."""
    return f"{peer.get('uri') or '?'} - {peer_state(peer)}"
