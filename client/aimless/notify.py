"""Desktop notifications and notification sounds for the AIMless client.

Extracted from gtkui.py. libnotify if present, else notify-send, else
silently nothing; sounds are synthesised (speaker-test/paplay/display bell),
no audio files shipped. Best-effort and silent without a backend.
"""
import os
import shutil
import subprocess
import threading
import time

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk

from . import appicon
from .prefs import pref

APP_NAME = "AIMless"


# libnotify bindings if present, else notify-send, else silently nothing.
_NOTIFY = {"ready": None, "mod": None}


def _notify_init():
    if _NOTIFY["ready"] is not None:
        return _NOTIFY["ready"]
    _NOTIFY["ready"] = False
    try:
        import gi
        gi.require_version("Notify", "0.7")
        from gi.repository import Notify
        Notify.init(APP_NAME)
        _NOTIFY["mod"] = Notify
        _NOTIFY["ready"] = True
    except Exception:
        _NOTIFY["mod"] = None
    return _NOTIFY["ready"]


def notify_new_message(sender, text):
    body = (text or "")[:200]
    if _notify_init():
        try:
            _NOTIFY["mod"].Notification.new(sender, body, "mail-unread").show()
            return
        except Exception:
            pass
    if shutil.which("notify-send"):
        try:
            subprocess.Popen(["notify-send", "--app-name", APP_NAME, sender, body])
        except Exception:
            pass


def should_notify(window_visible, muted, blocked, prefs):
    if not pref(prefs, "notifications"):
        return False
    if muted or blocked:
        return False
    return not window_visible


# --- notification sounds (synthesised; no audio files shipped) ---------------
# A short tone via speaker-test (as SimpleCal does), with paplay of a system
# event file and the display bell as fallbacks. Everything is best-effort and
# silent when there is no sound backend (e.g. in the container).

SOUND_OPTIONS = ("off", "single", "double", "triple", "long")
SOUND_MIN_GAP = 2.0
_BEEP = "timeout {dur}s speaker-test -t sine -f 800 -l 1"
_LAST_SOUND = [0.0]


def sound_command(kind):
    """The shell sequence for a sound option ("" for off/unknown). Pure."""
    if kind not in SOUND_OPTIONS or kind == "off":
        return ""
    if kind == "long":
        return _BEEP.format(dur="0.5")
    parts = []
    for i in range({"single": 1, "double": 2, "triple": 3}.get(kind, 0)):
        if i:
            parts.append("sleep 0.12")
        parts.append(_BEEP.format(dur="0.2"))
    return "; ".join(parts)


def sound_enabled(prefs):
    """Effective sound choice: AIMLESS_SOUND env overrides the saved pref."""
    env = os.environ.get("AIMLESS_SOUND")
    if env in SOUND_OPTIONS:
        return env
    kind = pref(prefs, "notification_sound")
    return kind if kind in SOUND_OPTIONS else "single"


def should_play_sound(window_visible, muted, blocked, prefs):
    if muted or blocked or window_visible:
        return False
    return sound_enabled(prefs) != "off"


def _run_beep(shell_cmd):
    if shutil.which("speaker-test") and shell_cmd:
        try:
            subprocess.Popen(["bash", "-c", shell_cmd], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            return
        except OSError:
            pass
    if shutil.which("paplay"):
        for path in ("/usr/share/sounds/freedesktop/stereo/message.oga",
                     "/usr/share/sounds/freedesktop/stereo/bell.oga"):
            if os.path.exists(path):
                try:
                    subprocess.Popen(["paplay", path], stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, start_new_session=True)
                    return
                except OSError:
                    pass
    try:
        display = Gdk.Display.get_default()
        if display is not None and hasattr(display, "beep"):
            display.beep()
    except Exception:
        pass


def play_notification_sound(kind, force=False):
    if kind not in SOUND_OPTIONS or kind == "off":
        return
    now = time.monotonic()
    if not force and now - _LAST_SOUND[0] < SOUND_MIN_GAP:
        return
    _LAST_SOUND[0] = now
    _run_beep(sound_command(kind))


# Guards the one-time libnotify setup, which now happens off the main thread.
_NOTIFY_LOCK = threading.Lock()

def notify_init_async():
    """Connect to the session bus in the background.

    g_bus_get_sync() blocks for a long time when the bus is not up yet, and
    at login the autostart entry runs early. The app must not stall on that,
    so the connection is set up on a side thread.
    """
    def work():
        try:
            gi.require_version("Notify", "0.7")
            from gi.repository import Notify
            with _NOTIFY_LOCK:
                if not Notify.is_initted():
                    Notify.init("aimless")
        except Exception:
            pass

    try:
        threading.Thread(target=work, daemon=True).start()
    except Exception:
        pass


def desktop_notify(title, body, icon_name=None):
    """Pop a desktop notification.

    Synchronous on purpose: libnotify hands the message to the caller's main
    context, so it has to be called from the main thread - a side thread
    sends the request but the daemon never shows it. The session bus is
    connected by notify_init_async(), so this does not block.
    """
    try:
        gi.require_version("Notify", "0.7")
        from gi.repository import Notify
        if not Notify.is_initted():
            # The bus is not connected yet - the background init is still
            # working on it. Say nothing rather than block the UI.
            return False
        note = Notify.Notification.new(title, body, icon_name or appicon.ICON_NAME)
        note.show()
        return True
    except Exception:
        return False
