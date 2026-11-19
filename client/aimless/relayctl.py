"""Applying a relay configuration to a running daemon.

Extracted from gtkui.py: restart, config comparison, DERP-map validation and
reset. No GUI dependency, so it is testable without a window.
"""
import json
import os
import time

from . import paths
from .daemon import DaemonClient


def restart_daemon(supervisor, log=None):
    """Stop and start the daemon so it reloads an on-disk node key."""
    try:
        supervisor.stop()
    except Exception:
        pass
    for _ in range(50):
        if not supervisor.is_running():
            break
        time.sleep(0.1)
    try:
        supervisor.ensure(log=log)
    except Exception:
        pass
    for _ in range(150):
        if supervisor.is_running():
            break
        time.sleep(0.1)


def relay_cfg_equal(cfg, relay):
    """True when a pending relay config matches the applied one (nothing to save)."""
    return (
        cfg.get("hostname", "") == (relay.get("hostname") or "")
        and cfg.get("region_id", 0) == (relay.get("region_id") or 0)
        and (cfg.get("derpmap_url") or "") == (relay.get("derpmap_url") or "")
    )


def apply_relay(supervisor, cfg, relay, log=None):
    """Persist a relay config (setrelay) and apply it. Returns (message, new_status);
    new_status is the refreshed supervisor status only when the local daemon was
    restarted, else None. A remote (tunnel) daemon is not restarted here - the
    client only owns the local process, so the remote one must be restarted by hand.
    """
    map_url = (cfg.get("derpmap_url") or "").strip()
    if map_url:
        err = _derp_map_error(map_url)
        if err:
            return f"error: DERP map URL: {err}", None
    d = DaemonClient(supervisor.sock)
    try:
        r = d.request("setrelay", relay=cfg, timeout=10)
    finally:
        d.close()
    if r.get("op") != "relay":
        return f"error: {r.get('error', 'unknown')}", None
    if relay_cfg_equal(cfg, relay):
        return "No change.", None
    if supervisor.external:
        return "Saved. Restart the remote daemon to apply.", None
    restart_daemon(supervisor, log)
    new = supervisor.status()
    if new is None or not supervisor.is_running():
        return "Saved, but the daemon did not restart. Quit AIMless and reopen.", None
    cur = new.get("region") or {}
    name = cur.get("name") or cur.get("code") or f"region {cur.get('id', '?')}"
    return f"Applied - you are now on {name}.", new


def _derp_map_error(url):
    """None if url serves a plausible DERP map, else a short error string. Used
    to validate a custom map URL before saving it (a typo must not brick startup).
    """
    import urllib.request
    try:
        req = urllib.request.Request(url, headers={"Tailcat-Mode": "client"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.load(resp)
    except Exception as e:
        return f"could not fetch ({e})"
    regions = data.get("Regions") if isinstance(data, dict) else None
    if not isinstance(regions, dict) or not regions:
        return "response is not a DERP map (no Regions)"
    return None


def reset_relay_config():
    """Remove the relay block from the daemon's config.json so it falls back to
    the default public network - the escape hatch when a bad relay config keeps
    the daemon from starting. Returns True if a relay block was removed."""
    path = os.path.join(paths.daemon_dir(), "config.json")
    try:
        with open(path) as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return False
    if not isinstance(cfg, dict) or "relay" not in cfg:
        return False
    del cfg["relay"]
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cfg, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except OSError:
        return False
    return True
