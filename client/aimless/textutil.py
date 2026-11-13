"""Pure text and path-safety helpers for the AIMless UI.

Extracted from gtkui.py: filename/conv-id sanitisation (path-traversal
defence), human-readable sizes, and Pango-markup linkification. No GTK
widget or app state - only re and GLib.markup_escape_text.
"""
import re

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import GLib


def sanitize_filename(name):
    """Strict allowlist so a sender-controlled filename can't traverse paths."""
    clean = re.sub(r"[^A-Za-z0-9 ._-]", "", name or "")
    clean = clean.strip(" .")
    if not clean:
        clean = "file"
    return clean[:64]


# A conversation id ends up as a directory name for attachments, so it has to
# be something we can put on disk without thinking about it. Legitimate ids
# are hex: a node key (a DM) or the sha256 of the member set (a room).
_CONV_ID_RE = re.compile(r"^[0-9a-f]{64}$")


def safe_conv_id(conv, fallback=None):
    """Return a conversation id that is safe to use as a path component.

    A peer chooses the ``conv`` inside a file chunk, and the receiver stores
    attachments under a directory named after it. An unsanitised value is a
    path traversal: ``../../.config/autostart`` would drop a file into the
    autostart directory and run it at the next login.

    Anything that is not a hex id is refused rather than sanitised - a
    conversation we do not recognise is not one of ours. ``fallback`` is used
    when the primary value is unusable (the sender's node key, which is the
    right directory for a DM).
    """
    if conv and _CONV_ID_RE.match(conv):
        return conv
    if fallback and _CONV_ID_RE.match(fallback):
        return fallback
    return None


def _human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


# Only http/https can ever become a link: the character class rules out the
# markup-breaking bytes (<, >, ", ') up front, and everything a peer can type
# that is NOT a matched URL is passed through markup_escape_text, so literal
# markup in a message can never produce a live anchor.
_URL_RE = re.compile(r"\bhttps?://[^\s<>\"']+")
_TRAILING_PUNCT = ".,;:!?)']"


def linkify(text: str) -> str:
    """Escape `text` into Pango markup, turning bare http(s) URLs into anchors.
    `https://x.example.` drops the trailing period; everything else - including
    <a href=...> the sender literally typed - is escaped to inert text."""
    out = []
    pos = 0
    for m in _URL_RE.finditer(text):
        url = m.group(0).rstrip(_TRAILING_PUNCT)
        if not url:
            continue
        out.append(GLib.markup_escape_text(text[pos:m.start()]))
        esc = GLib.markup_escape_text(url)
        out.append(f'<a href="{esc}">{esc}</a>')
        # advance past the SHORTENED url so stripped trailing punctuation is not
        # silently dropped from the message - it's picked up by the next segment
        pos = m.start() + len(url)
    out.append(GLib.markup_escape_text(text[pos:]))
    return "".join(out)
