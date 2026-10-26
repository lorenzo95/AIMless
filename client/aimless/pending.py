"""Count the messages that are waiting while the app is still locked.

An app that starts at login sits in the tray with its identity locked.  It
cannot decrypt anything - that needs the passphrase - but it still has to say
"a message is waiting", otherwise the user finds out only by opening the app
and looking.  A silent tray at boot is exactly the bug this module exists for.

Nothing in here needs the passphrase:

* ``contacts.json`` holds the buddy node keys in the clear (routing metadata);
* the per-peer watermark in the local store is plaintext routing metadata too.

So the number of waiting messages is simply what the daemon journaled past our
own watermark.  The daemon's ``history`` op returns that tail without decrypting
a single byte, and it works against a locked app: the daemon does the storing,
the client only reads the journal.

The watermark is deliberately *not* advanced here.  The unlocked sweep
(``Sync.apply``) owns the watermark, so the same messages are still counted as
unread when the user finally unlocks - the badge never lies about what is
waiting, and nothing is swallowed.
"""

import json
import os
import sqlite3


def contact_nodes(contacts_path: str) -> dict:
    """Buddy node keys -> petname.  Plaintext on disk, no passphrase needed."""
    try:
        with open(contacts_path) as f:
            contacts = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(contacts, dict):
        return {}
    out = {}
    for petname, info in contacts.items():
        if petname == "_self" or not isinstance(info, dict):
            continue
        node = info.get("node")
        if isinstance(node, str) and node:
            out[node] = petname
    return out


def watermarks(store_path: str) -> dict:
    """Peer -> last seq we ingested.  Plaintext routing metadata.

    Read-only and best effort: the file may not exist yet (first run) or the
    table may not be there (an old store), and the daemon may have trimmed
    entries we never saw.
    """
    if not os.path.exists(store_path):
        return {}
    conn = None
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % store_path, uri=True)
        rows = conn.execute("SELECT peer, seq FROM watermark").fetchall()
        return {peer: int(seq) for peer, seq in rows}
    except (sqlite3.Error, OSError, ValueError):
        return {}
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


def _history(client, node, after_seq):
    """Ask the daemon for the tail of one peer's journal.

    Accepts the low-level daemon client (``request``) as well as the
    high-level one (``history``), so the helper is easy to fake in tests.
    """
    request = getattr(client, "request", None)
    if callable(request):
        return request("history", **{"from": node, "seq": after_seq})
    return client.history(node, after_seq)


def pending_count(client, contacts_path: str, store_path: str) -> int:
    """How many messages the daemon journaled past our watermark.

    ``client`` is a daemon client (anything with ``request(op, **kwargs)`` or
    ``history(node, seq)``).  The daemon is asked per peer because its journal
    is a per-peer stream; the watermark says how far we got with that peer.
    """
    peers = contact_nodes(contacts_path)
    if not peers:
        return 0
    marks = watermarks(store_path)
    total = 0
    asked = 0
    first_error = None
    for node in peers:
        try:
            resp = _history(client, node, marks.get(node, 0))
        except Exception as e:
            # A peer we cannot ask about right now (daemon busy, peer gone)
            # must not make the whole count fail - but if *every* peer fails
            # something is broken, and a silent "0 messages" would hide it.
            if first_error is None:
                first_error = e
            continue
        asked += 1
        if isinstance(resp, dict):
            total += len(resp.get("msgs") or [])
        elif isinstance(resp, list):
            total += len(resp)
    if asked == 0 and first_error is not None:
        raise first_error
    return total
