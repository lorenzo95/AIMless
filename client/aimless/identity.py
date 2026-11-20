"""Creating and restoring an AIMless identity on disk.

Extracted from gtkui.py. Session-independent, so the init screen can create or
import an identity before any window exists. The path wrappers are read from
gtkui lazily to avoid a circular import.
"""
import json
import os

from . import crypto, paths, protocol
from . import keys as keysmod


def create_identity(passphrase, screen):
    """Persist a fresh identity and self contact entry. Mirrors `aimless init`."""
    from .gtkui import contacts_path, identity_path
    identity = crypto.new_identity()
    crypto.save_identity(identity_path(), identity, passphrase)
    contacts = protocol.load_contacts(contacts_path())
    contacts["_self"] = {"screen": screen or "anonymous",
                         "pubkey": bytes(identity.verify_key).hex()}
    protocol.save_contacts(contacts_path(), contacts)
    return passphrase


def restore_bundle(path, bundle_passphrase, app_passphrase):
    """Write an encrypted backup bundle onto disk. Returns the bundle dict.

    Session-independent so the init screen can import before a window exists.
    Raises ValueError on a bad passphrase / missing identity seed.
    """
    from .gtkui import contacts_path, identity_path
    data = keysmod.load_bundle(path, bundle_passphrase)
    seed = bytes.fromhex(data.get("identitySeed", ""))
    if len(seed) != 32:
        raise ValueError("backup is missing the identity seed")
    crypto.save_identity(identity_path(), keysmod.signing_key(seed), app_passphrase)
    if data.get("nodeSeed"):
        keysmod.write_node_key(paths.daemon_dir(), bytes.fromhex(data["nodeSeed"]))
    if data.get("tailcatKey"):
        keysmod.write_tailcat_key(paths.daemon_dir(), data["tailcatKey"])
    if data.get("relay"):
        _write_relay_config(paths.daemon_dir(), data["relay"])
    if data.get("contacts"):
        protocol.save_contacts(contacts_path(), data["contacts"])
    return data


def _write_relay_config(datadir, relay):
    """Merge a relay block into the daemon's config.json, preserving other keys."""
    os.makedirs(datadir, exist_ok=True)
    path = os.path.join(datadir, "config.json")
    try:
        with open(path) as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    cfg["relay"] = relay
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)
