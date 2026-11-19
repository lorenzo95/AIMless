#!/usr/bin/env python3
"""gtkui.py - entry point and public facade for the aimless GTK desktop app.

One process: messages window + tray icon + the aimlessd daemon.

The implementation lives in focused modules; this one wires them together and
re-exports their names, so `gtkui.X` keeps working for callers and tests:

  uibase    shared UI layer: theme state, path wrappers, GTK/markup helpers
  session   daemon supervision, pid helpers, the client Session
  messages  MessagesView (sidebar, chat, composer, attachments)
  window    AimlessWindow (tabs, route bar, identity/relay dialogs)
  views     ContactsView, ActivityView
  app       TrayIcon, ConnectWindow, AimlessApp, desktop integration
  dialogs   passphrase / identity / text prompts
  identity  create/restore an identity on disk
  relay     relay display helpers and the region picker
  relayctl  applying a relay config to a running daemon

Modes:
  aimless            everything: tray icon, daemon, messages window
  aimless tray       starts hidden - tray icon only, window opens on first click (autostart)
  aimless gui        same as running aimless with no arguments
  aimless autostart  install login autostart entry for `aimless tray`
"""

import base64
import hashlib
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import threading
import traceback
import time
import warnings
from datetime import datetime, timedelta

warnings.filterwarnings("ignore", category=DeprecationWarning)

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib, Gdk, Pango, GdkPixbuf, Gio

from . import crypto, protocol, logging, paths
from . import tunnel
from . import appicon
from . import pending
from . import peers as peersmod
from . import keys as keysmod
from .daemon import DaemonClient, Client, DaemonError, presence_state
from .service import Sync
from .store import Store
from . import __version__ as client_version
from . import MIN_DAEMON_BUILD
from .textutil import sanitize_filename, safe_conv_id, _human_size, linkify
from .theme import THEMES, THEME_ENTRIES, detect_system_palette, get_theme, build_css
from .prefs import load_prefs, save_prefs, DEFAULT_PREFS, pref
from .notify import (_NOTIFY, _NOTIFY_LOCK, _BEEP, _LAST_SOUND, SOUND_OPTIONS,
                     SOUND_MIN_GAP, _notify_init, notify_new_message, should_notify,
                     sound_command, sound_enabled, should_play_sound, _run_beep,
                     play_notification_sound, notify_init_async, desktop_notify)
from .relay import (is_tailcat, net_label, short_address, relay_tooltip,
                    REGION_COUNTRY, region_label, make_region_list)
from .peerdialog import open_external, PeerListBox, ask_peers

from .uibase import (
    APP_NAME, STATUS_REASSERT_SECONDS, PENDING_CHECK_SECONDS, prefs_file, attachments_dir,
    sock_path, contacts_path, identity_path, cache_path, C, install_css_provider, set_theme,
    current_theme, format_time, date_label, clamp_to_workarea, first_icon, LINK_GRACE_SECONDS,
    ISLAND_GRACE_SECONDS, PEER_CHECK_SECONDS, TRAY_ADOPT_SECONDS, DETACHED_NOTICE_SECONDS,
    fmt_duration, detached_gap_seconds, set_net_wm_icon_name, setup_app_icon,
    setup_application_identity, tray_icon_name, scroll_to_bottom, clear_children,
    daemon_binary, _presence_state, _self_state, _presence_color, _room_dots_markup,
    _sidebar_title_markup, _room_header_markup, _free_petname, _add_contact_from_roster,
    run_async)


# Rebinding theme state lives in uibase; forward attribute access so
# `gtkui.CURRENT_THEME` / `gtkui.CSS_PROVIDER` stay live after a theme switch.
_FORWARDED = {"CURRENT_THEME", "CSS_PROVIDER", "_ENV_THEME"}


def __getattr__(name):
    if name in _FORWARDED:
        from . import uibase
        return getattr(uibase, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

from .session import (DaemonSupervisor, Session, daemon_pid_from_procs,
                      daemon_pid_from_socket, pid_alive, read_pid, stop_all)
from .views import ActivityView, ContactsView
from .app import (AimlessApp, ConnectWindow, TrayIcon, _make_excepthook,
                  _make_thread_hook, acquire_app_lock, app_exec_line, app_log_path,
                  install_autostart, install_desktop_entry)
from .messages import MessagesView
from .window import AimlessWindow
from .dialogs import (ask_secret, _error_dialog, ask_passphrase,
                      ask_create_identity, ask_text)
from .identity import (create_identity, restore_bundle, _write_tailcat_key,
                       _write_relay_config)
from .relayctl import (restart_daemon, relay_cfg_equal, apply_relay,
                       _derp_map_error, reset_relay_config)


def run_app(open_window=True):
    app = AimlessApp()
    return app.start(open_window=open_window)


def main():
    args = sys.argv[1:]
    if "autostart" in args:
        print(install_autostart())
    else:
        sys.exit(run_app(open_window="tray" not in args))


if __name__ == "__main__":
    main()
