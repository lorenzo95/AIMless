"""Modal dialogs for the AIMless client: passphrase, identity creation, text.

Extracted from gtkui.py. The app name and window-icon helper are read from
gtkui lazily to avoid a circular import and to stay correct across theme
switches - the same pattern peerdialog.py uses.
"""
import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk


def ask_secret(parent, title, confirm=False):
    from .gtkui import setup_app_icon
    dlg = Gtk.Dialog(title=title, transient_for=parent, modal=True)
    setup_app_icon(dlg)
    dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "OK", Gtk.ResponseType.OK)
    dlg.set_default_response(Gtk.ResponseType.OK)
    dlg.set_default_size(380, -1)
    box = dlg.get_content_area()
    box.set_spacing(8)
    box.set_border_width(10)
    lbl = Gtk.Label(label=title)
    lbl.set_xalign(0.0)
    box.add(lbl)
    e1 = Gtk.Entry()
    e1.set_visibility(False)
    box.add(e1)
    e2 = None
    if confirm:
        e2 = Gtk.Entry()
        e2.set_visibility(False)
        e2.set_placeholder_text("confirm")
        box.add(e2)
    box.show_all()
    resp = dlg.run()
    pw = e1.get_text()
    conf = e2.get_text() if e2 is not None else pw
    dlg.destroy()
    if resp != Gtk.ResponseType.OK:
        return None
    if not pw:
        parent and _error_dialog(parent, "Passphrase must not be empty.")
        return None
    if pw != conf:
        parent and _error_dialog(parent, "Passphrases do not match.")
        return None
    return pw


def _error_dialog(parent, text):
    dlg = Gtk.MessageDialog(transient_for=parent, modal=True,
                            message_type=Gtk.MessageType.ERROR, text=text,
                            buttons=Gtk.ButtonsType.OK)
    dlg.run()
    dlg.destroy()


def ask_passphrase(parent):
    from .gtkui import setup_app_icon
    dlg = Gtk.Dialog(title="AIMless - passphrase", transient_for=parent, modal=True)
    setup_app_icon(dlg)
    dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Unlock", Gtk.ResponseType.OK)
    dlg.set_default_response(Gtk.ResponseType.OK)
    dlg.set_default_size(360, 100)
    box = dlg.get_content_area()
    box.set_spacing(8)
    box.set_border_width(10)
    box.add(Gtk.Label(label="Enter your passphrase to unlock your identity"))
    entry = Gtk.Entry(visibility=False, activates_default=True)
    box.add(entry)
    dlg.show_all()
    resp = dlg.run()
    text = entry.get_text()
    dlg.destroy()
    if resp == Gtk.ResponseType.OK and text:
        return text
    return None


def ask_create_identity(parent):
    """Ask for passphrase + confirm + screen name, or offer to import a backup.

    Returns None (cancelled / empty), ("__mismatch__",), ("__import__",) or
    (passphrase, screen)."""
    from .gtkui import APP_NAME, setup_app_icon
    dlg = Gtk.Dialog(title=f"{APP_NAME} - step 1 of 2: your identity", transient_for=parent, modal=True)
    setup_app_icon(dlg)
    dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL,
                    "Import backup…", Gtk.ResponseType.APPLY,
                    "Create identity", Gtk.ResponseType.OK)
    dlg.set_default_response(Gtk.ResponseType.OK)
    dlg.set_default_size(380, 120)
    box = dlg.get_content_area()
    box.set_spacing(8)
    box.set_border_width(10)
    box.add(Gtk.Label(label="No identity on this machine yet - create one, or restore a backup. "
                            "Next you pick the peers AIMless connects through."))
    box.add(Gtk.Label(label="Passphrase (protects your keys; re-enter it later to unlock)"))
    pw = Gtk.Entry(visibility=False, activates_default=True)
    box.add(pw)
    box.add(Gtk.Label(label="Confirm passphrase"))
    pw2 = Gtk.Entry(visibility=False, activates_default=True)
    box.add(pw2)
    box.add(Gtk.Label(label="Screen name"))
    screen = Gtk.Entry(activates_default=True)
    box.add(screen)
    dlg.show_all()
    resp = dlg.run()
    p1, p2, sn = pw.get_text(), pw2.get_text(), screen.get_text()
    dlg.destroy()
    if resp == Gtk.ResponseType.APPLY:
        return ("__import__",)
    if resp != Gtk.ResponseType.OK or not p1:
        return None
    if p1 != p2:
        return ("__mismatch__",)
    return (p1, sn)


def ask_text(parent, title, label):
    dlg = Gtk.Dialog(title=title, transient_for=parent, modal=True)
    dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "OK", Gtk.ResponseType.OK)
    dlg.set_default_response(Gtk.ResponseType.OK)
    dlg.set_default_size(420, 100)
    box = dlg.get_content_area()
    box.set_spacing(8)
    box.set_border_width(10)
    box.add(Gtk.Label(label=label))
    entry = Gtk.Entry(activates_default=True)
    box.add(entry)
    dlg.show_all()
    resp = dlg.run()
    text = entry.get_text()
    dlg.destroy()
    if resp == Gtk.ResponseType.OK:
        return text
    return None
