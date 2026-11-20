"""User preferences for the AIMless client.

Extracted from gtkui.py: loading/saving prefs.json and resolving a pref
against DEFAULT_PREFS. No GTK or app state - just paths + json.
"""
import json
import os

from . import paths


def load_prefs():
    try:
        with open(paths.prefs_path()) as f:
            return json.load(f)
    except Exception:
        return {}


def save_prefs(prefs):
    os.makedirs(paths.client_dir(), exist_ok=True)
    with open(paths.prefs_path(), "w") as f:
        json.dump(prefs, f, indent=2)


DEFAULT_PREFS = {
    "remember_position": True,
    "notifications": True,
    "notification_sound": "single",
    "enter_to_send": True,
    "time_format": "24h",
    "theme": "system",           # used by the 0.8.3 theming work
    "seen_onboarding": False,
    "sidebar_order": "recent",   # "recent" (newest first) or "alpha" (by name)
    # Age-based history retention: messages older than this are dropped along
    # with their attachment files. 0 keeps everything forever. Outgoing mail
    # that not every recipient has acked is never dropped, whatever its age.
    "history_days": 90,
}


def pref(prefs, key):
    return prefs.get(key, DEFAULT_PREFS.get(key))
