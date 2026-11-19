"""Data-backup primitives: store snapshot/rekey/merge and the data bundle.

These are the no-GUI core of the portable data backup (rooms + history). The
store key is scrypt(passphrase, salt) with a per-DB salt, so a backup must be
re-keyed to travel between machines/passphrases; merge must dedup and never
overwrite an existing roster.
"""
import base64
import os

import nacl.utils
import pytest

from aimless import keys as keysmod
from aimless.store import Store, rekey_file
from aimless import window


def populate(store):
    store.ingest("nodeA", "nodeA", 1, 1000, "hello from A")
    store.ingest("nodeA", "nodeA", 2, 2000, "second message",
                 attachment={"path": "att1.bin", "name": "f.bin", "size": 3})
    store.add_sent("nodeA", {"nodeA": 1}, 3000, "my reply")
    members = {"nodeA": {"pubkey": "aa", "screen": "A"},
               "nodeB": {"pubkey": "bb", "screen": "B"}}
    store.ensure_room("room1", members, created=500)
    store.ingest("room1", "nodeB", 1, 4000, "room message")
    store.set_cursor("nodeA", 2)
    store.mark_read("nodeA")
    store.set_blocked_screen("nodeC", "C Screen")
    store.add_pending({"node": "nodeD", "pubkey": "dd", "screen": "D"})
    store.mute("nodeE")


def texts(store, conv):
    return [m["text"] for m in store.messages(conv)]


def test_rekey_roundtrip_across_passphrases(tmp_path):
    src = Store(str(tmp_path / "src.db"), "pwA")
    populate(src)
    snap = str(tmp_path / "snap.db")
    src.snapshot_to(snap)
    src.close()

    rekey_file(snap, "pwA", "pwB", nacl.utils.random(16))
    reopened = Store(snap, "pwB")
    assert texts(reopened, "nodeA") == ["hello from A", "second message", "my reply"]
    assert texts(reopened, "room1") == ["room message"]
    assert reopened.rooms() == ["room1"]
    assert reopened.members("room1")["nodeB"]["screen"] == "B"
    assert reopened.cursor("nodeA") == 2
    assert reopened.blocked_screen("nodeC") == "C Screen"
    assert reopened.pending()[0]["node"] == "nodeD"
    assert reopened.is_muted("nodeE")
    att = reopened.messages("nodeA")[1]["attachment"]
    assert att["path"] == "att1.bin"
    reopened.close()
    # The old passphrase must no longer open it.
    with pytest.raises(Exception):
        Store(snap, "pwA").messages("nodeA")


def test_merge_dedup_max_watermark_and_roster(tmp_path):
    dst = Store(str(tmp_path / "dst.db"), "pw")
    dst.ingest("nodeA", "nodeA", 1, 1000, "existing message")
    dst.set_cursor("nodeA", 5)  # newer than the backup's
    dst.ensure_room("room1", {"nodeA": {"pubkey": "aa", "screen": "Original"}}, created=1)

    other = Store(str(tmp_path / "other.db"), "pw")
    other.ingest("nodeA", "nodeA", 1, 1000, "existing message")  # duplicate
    other.ingest("nodeA", "nodeA", 2, 2000, "new from backup")
    other.set_cursor("nodeA", 2)  # older than dst's
    other.ensure_room("room1", {"nodeA": {"pubkey": "aa", "screen": "Imposter"}}, created=1)
    other_path = str(tmp_path / "other.db")
    other.close()

    # Re-key other to dst's exact key (same passphrase AND salt) so BLOBs copy.
    rekey_file(other_path, "pw", "pw", dst.salt)
    dst.merge_from(other_path)

    assert texts(dst, "nodeA") == ["existing message", "new from backup"]  # deduped
    assert dst.cursor("nodeA") == 5  # MAX kept, not regressed to 2
    assert dst.members("room1")["nodeA"]["screen"] == "Original"  # roster kept
    dst.close()


def test_data_bundle_cross_passphrase_roundtrip(tmp_path):
    src = Store(str(tmp_path / "src.db"), "pwA")
    populate(src)
    snap = str(tmp_path / "snap.db")
    src.snapshot_to(snap)
    src.close()

    backup_pw = "backup-pass"
    rekey_file(snap, "pwA", backup_pw, nacl.utils.random(16))
    db_bytes = open(snap, "rb").read()
    bundle = str(tmp_path / "data.aimless")
    keysmod.save_data_bundle(bundle, backup_pw, db_bytes,
                             attachments={"att1.bin": base64.b64encode(b"xyz").decode()},
                             counts={"messages": 4, "rooms": 1})

    loaded = keysmod.load_data_bundle(bundle, backup_pw)
    assert loaded["kind"] == "data"
    assert loaded["counts"]["rooms"] == 1
    assert base64.b64decode(loaded["attachments"]["att1.bin"]) == b"xyz"

    # Restore into a fresh store under a DIFFERENT app passphrase.
    dst = Store(str(tmp_path / "dst.db"), "pwC")
    tmp_db = str(tmp_path / "restore.db")
    with open(tmp_db, "wb") as f:
        f.write(base64.b64decode(loaded["db"]))
    rekey_file(tmp_db, backup_pw, "pwC", dst.salt)
    dst.merge_from(tmp_db)

    assert texts(dst, "nodeA") == ["hello from A", "second message", "my reply"]
    assert texts(dst, "room1") == ["room message"]
    assert dst.rooms() == ["room1"]
    assert dst.members("room1")["nodeB"]["screen"] == "B"
    assert dst.cursor("nodeA") == 2
    assert dst.blocked_screen("nodeC") == "C Screen"
    dst.close()


def test_load_data_bundle_rejects_identity_bundle(tmp_path):
    p = str(tmp_path / "identity.aimless")
    keysmod.save_bundle(p, "pw", {"identitySeed": "00" * 32})
    with pytest.raises(ValueError):
        keysmod.load_data_bundle(p, "pw")


class _FakeAppRef:
    def __init__(self, pw):
        self.passphrase = pw


class _FakeSession:
    def __init__(self, store):
        self.store = store


class _FakeWin:
    def __init__(self, store, pw):
        self.session = _FakeSession(store)
        self.app_ref = _FakeAppRef(pw)


def test_data_backup_export_restore_attachments(tmp_path, monkeypatch):
    from aimless import gtkui
    adir = tmp_path / "att_src"
    (adir / "conv1").mkdir(parents=True)
    fsrc = adir / "conv1" / "t1-file.bin"
    fsrc.write_bytes(b"attachment-bytes")
    monkeypatch.setattr(window, "attachments_dir", lambda: str(adir))

    src = Store(str(tmp_path / "src.db"), "pwA")
    src.ingest("conv1", "nodeA", 1, 1000, "see file",
               attachment={"path": str(fsrc), "filename": "file.bin"})
    win = _FakeWin(src, "pwA")
    bundle = str(tmp_path / "data.json")
    counts = gtkui.AimlessWindow.export_data_backup_to(win, bundle, "backup-pw", True)
    assert counts["messages"] == 1
    src.close()

    # Restore into a fresh store, a DIFFERENT attachments dir and passphrase.
    adir2 = tmp_path / "att_dst"
    monkeypatch.setattr(window, "attachments_dir", lambda: str(adir2))
    dst = Store(str(tmp_path / "dst.db"), "pwC")
    win2 = _FakeWin(dst, "pwC")
    gtkui.AimlessWindow.restore_data_backup_from(win2, bundle, "backup-pw", "pwC")

    msgs = dst.messages("conv1")
    assert len(msgs) == 1 and msgs[0]["text"] == "see file"
    expected = adir2 / "conv1" / "t1-file.bin"
    assert msgs[0]["attachment"]["path"] == str(expected)
    assert expected.read_bytes() == b"attachment-bytes"
    dst.close()


def test_identity_bundle_has_kind_and_still_loads(tmp_path):
    # New identity bundles carry kind="identity"; old ones (no kind) still load.
    p = str(tmp_path / "id.json")
    keysmod.save_bundle(p, "pw", {"kind": "identity", "identitySeed": "11" * 32})
    assert keysmod.load_bundle(p, "pw")["kind"] == "identity"
    old = str(tmp_path / "old.json")
    keysmod.save_bundle(old, "pw", {"identitySeed": "22" * 32})
    assert keysmod.load_bundle(old, "pw").get("kind") is None