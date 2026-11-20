"""Age-based history retention (TTL).

The daemon side is already bounded (inbox capacity, attachment byte cap, outbox
drained on ACK); what grows without bound is the client's encrypted history and
the plaintext attachment files next to it. These tests pin the contract:

  * only messages older than the window go, and 0 disables retention;
  * outgoing mail that not every recipient has acked is NEVER dropped;
  * attachment files are only unlinked inside the attachments root;
  * watermarks, rosters, read marks and pending requests survive, so pruning
    cannot re-open a dismissed room or make sync re-fetch what it removed;
  * the dry run reports exactly what the real pass removes.
"""
import json
import time
from types import SimpleNamespace

import pytest

from aimless import app as appmod
from aimless import cli
from aimless.store import Store

DAY = 86400000
NOW = 1700000000000          # fixed clock: no wall-time flakes
OLD = NOW - 120 * DAY
RECENT = NOW - 2 * DAY
WALL = int(time.time() * 1000)   # the app and CLI passes read the wall clock


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "state.db"), "pw")
    yield s
    s.close()


def texts(s, conv):
    return [m["text"] for m in s.messages(conv)]


def legacy_outgoing(s, conv, ts, text):
    """An outgoing row with no delivery rows at all (pre-tracking history)."""
    mid = f"out:legacy:{ts}"
    s.conn.execute(
        "INSERT INTO messages(id, conv, sender, dir, ts, seqs, text) "
        "VALUES(?, ?, 'self', 'out', ?, ?, ?)",
        (mid, conv, ts, json.dumps({}), s._enc(text)))
    s.conn.commit()
    return mid


# -- the age window --------------------------------------------------------

def test_prune_drops_only_messages_older_than_the_window(store):
    store.ingest("nodeA", "nodeA", 1, OLD, "ancient")
    store.ingest("nodeA", "nodeA", 2, RECENT, "fresh")
    store.add_sent("nodeA", {"nodeA": 9}, OLD, "old reply")
    store.mark_delivered("nodeA", 9)

    gone = store.prune_history(90, now_ms=NOW)

    assert gone == {"messages": 2, "attachments": 0}
    assert texts(store, "nodeA") == ["fresh"]


def test_prune_boundary_is_strictly_older_than_the_cutoff(store):
    store.ingest("nodeA", "nodeA", 1, NOW - 90 * DAY, "exactly at the cutoff")
    store.ingest("nodeA", "nodeA", 2, NOW - 90 * DAY - 1, "one ms older")

    assert store.prune_history(90, now_ms=NOW)["messages"] == 1
    assert texts(store, "nodeA") == ["exactly at the cutoff"]


def test_zero_and_negative_disable_retention(store):
    store.ingest("nodeA", "nodeA", 1, OLD, "ancient")
    for days in (0, -1, None):
        assert store.prune_history(days, now_ms=NOW) == {"messages": 0, "attachments": 0}
        assert store.count_prunable(days, now_ms=NOW) == 0
    assert texts(store, "nodeA") == ["ancient"]


def test_prune_is_idempotent(store):
    store.ingest("nodeA", "nodeA", 1, OLD, "ancient")
    assert store.prune_history(90, now_ms=NOW)["messages"] == 1
    assert store.prune_history(90, now_ms=NOW)["messages"] == 0


def test_prune_spans_more_than_one_delete_batch(store):
    for i in range(600):
        store.ingest("nodeA", "nodeA", i + 1, OLD + i, f"m{i}")
    store.ingest("nodeA", "nodeA", 5000, RECENT, "fresh")

    assert store.count_prunable(90, now_ms=NOW) == 600
    assert store.prune_history(90, now_ms=NOW)["messages"] == 600
    assert texts(store, "nodeA") == ["fresh"]


def test_dry_run_counts_exactly_what_the_real_pass_removes(store):
    store.ingest("nodeA", "nodeA", 1, OLD, "ancient")
    store.ingest("nodeA", "nodeA", 2, RECENT, "fresh")
    store.add_sent("nodeA", {"nodeA": 9}, OLD, "still in flight")
    legacy_outgoing(store, "nodeA", OLD, "legacy reply")

    assert store.count_prunable(90, now_ms=NOW) == 2
    assert sorted(texts(store, "nodeA")) == sorted(
        ["ancient", "fresh", "still in flight", "legacy reply"])
    assert store.prune_history(90, now_ms=NOW)["messages"] == 2
    assert store.count_prunable(90, now_ms=NOW) == 0


# -- never lose undelivered mail -------------------------------------------

def test_inflight_outgoing_is_kept_whatever_its_age(store):
    store.add_sent("nodeA", {"nodeA": 1, "nodeB": 2}, OLD, "room fanout")
    store.mark_delivered("nodeA", 1)          # one recipient acked, one has not

    assert store.count_prunable(1, now_ms=NOW) == 0
    assert store.prune_history(1, now_ms=NOW)["messages"] == 0
    assert texts(store, "nodeA") == ["room fanout"]
    assert store.undelivered("out:nodeA=1,nodeB=2") == [("nodeB", 2)]

    store.mark_delivered("nodeB", 2)          # fully delivered: now prunable
    assert store.prune_history(1, now_ms=NOW)["messages"] == 1
    assert texts(store, "nodeA") == []


def test_legacy_outgoing_without_delivery_rows_is_prunable(store):
    legacy_outgoing(store, "nodeA", OLD, "legacy reply")
    assert store.prune_history(90, now_ms=NOW)["messages"] == 1
    assert texts(store, "nodeA") == []


def test_inflight_attachment_file_is_kept_too(store, tmp_path):
    root = tmp_path / "attachments"
    (root / "nodeA").mkdir(parents=True)
    f = root / "nodeA" / "pending.bin"
    f.write_bytes(b"x")
    store.add_sent("nodeA", {"nodeA": 1}, OLD, "sending a file",
                   attachment={"path": str(f), "filename": "pending.bin"})

    gone = store.prune_history(1, now_ms=NOW, attachments_root=str(root))

    assert gone == {"messages": 0, "attachments": 0}
    assert f.exists()


# -- attachment files ------------------------------------------------------

def test_prune_unlinks_attachment_files_and_empty_conv_dir(store, tmp_path):
    root = tmp_path / "attachments"
    (root / "nodeA").mkdir(parents=True)
    f = root / "nodeA" / "old.bin"
    f.write_bytes(b"payload")
    store.ingest("nodeA", "nodeA", 1, OLD, "see file",
                 attachment={"path": str(f), "filename": "old.bin"})

    gone = store.prune_history(90, now_ms=NOW, attachments_root=str(root))

    assert gone == {"messages": 1, "attachments": 1}
    assert not f.exists()
    assert not (root / "nodeA").exists(), "an emptied conv dir is cleaned up"
    assert root.exists(), "the attachments root itself stays"


def test_prune_keeps_sibling_files_and_their_directory(store, tmp_path):
    root = tmp_path / "attachments"
    (root / "nodeA").mkdir(parents=True)
    old = root / "nodeA" / "old.bin"
    keep = root / "nodeA" / "keep.bin"
    old.write_bytes(b"a")
    keep.write_bytes(b"b")
    store.ingest("nodeA", "nodeA", 1, OLD, "old file",
                 attachment={"path": str(old), "filename": "old.bin"})
    store.ingest("nodeA", "nodeA", 2, RECENT, "new file",
                 attachment={"path": str(keep), "filename": "keep.bin"})

    gone = store.prune_history(90, now_ms=NOW, attachments_root=str(root))

    assert gone == {"messages": 1, "attachments": 1}
    assert not old.exists()
    assert keep.exists()
    assert (root / "nodeA").exists()


def test_prune_never_unlinks_outside_the_attachments_root(store, tmp_path):
    root = tmp_path / "attachments"
    root.mkdir()
    outside = tmp_path / "important.bin"
    outside.write_bytes(b"do not delete")
    store.ingest("nodeA", "nodeA", 1, OLD, "elsewhere",
                 attachment={"path": str(outside), "filename": "important.bin"})

    gone = store.prune_history(90, now_ms=NOW, attachments_root=str(root))

    assert gone == {"messages": 1, "attachments": 0}
    assert outside.exists(), "a stored path outside the root must never be unlinked"


def test_prune_refuses_a_symlink_that_escapes_the_root(store, tmp_path):
    root = tmp_path / "attachments"
    (root / "nodeA").mkdir(parents=True)
    outside = tmp_path / "target.bin"
    outside.write_bytes(b"do not delete")
    link = root / "nodeA" / "link.bin"
    link.symlink_to(outside)
    store.ingest("nodeA", "nodeA", 1, OLD, "symlink",
                 attachment={"path": str(link), "filename": "link.bin"})

    gone = store.prune_history(90, now_ms=NOW, attachments_root=str(root))

    assert gone["attachments"] == 0
    assert outside.exists()
    assert link.is_symlink(), "the link itself is left alone, not followed"


def test_prune_tolerates_missing_malformed_and_corrupt_attachments(store, tmp_path):
    root = tmp_path / "attachments"
    root.mkdir()
    store.ingest("nodeA", "nodeA", 1, OLD, "gone file",
                 attachment={"path": str(root / "nodeA" / "missing.bin")})
    store.ingest("nodeA", "nodeA", 2, OLD, "no path", attachment={"filename": "x"})
    store.ingest("nodeA", "nodeA", 3, OLD, "not a dict path", attachment={"path": 7})
    # A blob that is not even JSON must not abort the pass either.
    store.ingest("nodeA", "nodeA", 4, OLD, "corrupt blob")
    store.conn.execute("UPDATE messages SET attachment=? WHERE id=?",
                       (store._enc("not json"), "in:nodeA:4"))
    store.conn.commit()

    gone = store.prune_history(90, now_ms=NOW, attachments_root=str(root))

    assert gone == {"messages": 4, "attachments": 0}
    assert texts(store, "nodeA") == []


def test_prune_without_a_root_leaves_files_alone(store, tmp_path):
    root = tmp_path / "attachments"
    (root / "nodeA").mkdir(parents=True)
    f = root / "nodeA" / "old.bin"
    f.write_bytes(b"x")
    store.ingest("nodeA", "nodeA", 1, OLD, "file",
                 attachment={"path": str(f), "filename": "old.bin"})

    gone = store.prune_history(90, now_ms=NOW)

    assert gone == {"messages": 1, "attachments": 0}
    assert f.exists()


# -- what must survive -----------------------------------------------------

def test_prune_preserves_sync_state_rosters_and_requests(store):
    members = {"nodeA": {"pubkey": "aa", "screen": "A"},
               "nodeB": {"pubkey": "bb", "screen": "B"}}
    store.ensure_room("room1", members, created=OLD)
    store.ingest("room1", "nodeB", 1, OLD, "old room message")
    store.ingest("nodeA", "nodeA", 1, OLD, "old dm")
    store.set_cursor("nodeA", 7)
    store.set_cursor("nodeB", 3)
    store.mark_read("nodeA")
    store.add_pending({"node": "nodeD", "pubkey": "dd", "screen": "D"})
    store.mute("nodeE")
    store.set_blocked_screen("nodeC", "C Screen")

    gone = store.prune_history(30, now_ms=NOW)

    assert gone["messages"] == 2
    assert store.cursor("nodeA") == 7 and store.cursor("nodeB") == 3
    assert store.rooms() == ["room1"], "an expired room keeps its roster"
    assert store.members("room1") == members
    assert store.is_room("room1")
    assert store.pending()[0]["node"] == "nodeD"
    assert store.is_muted("nodeE")
    assert store.blocked_screen("nodeC") == "C Screen"
    assert store.unread("nodeA") == 0, "pruning must not resurrect unread counts"
    # A new message still routes into the surviving conversation.
    store.ingest("room1", "nodeB", 2, RECENT, "new room message")
    assert texts(store, "room1") == ["new room message"]


def test_prune_keeps_unread_recent_mail_unread(store):
    store.ingest("nodeA", "nodeA", 1, OLD, "ancient")
    store.ingest("nodeA", "nodeA", 2, RECENT, "fresh")
    assert store.unread("nodeA") == 2
    store.prune_history(90, now_ms=NOW)
    assert store.unread("nodeA") == 1


# -- the app pass ----------------------------------------------------------

class FakeApp:
    """The real retention methods on a stub shell - no GTK, no daemon."""

    _prune_history = appmod.AimlessApp._prune_history
    _refresh_after_prune = appmod.AimlessApp._refresh_after_prune

    def __init__(self, store):
        self.session = SimpleNamespace(store=store) if store is not None else None
        self.window = None
        self.lines = []

    def log(self, line):
        self.lines.append(line)


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    """Point the app pass at a temp prefs dict and a temp attachments root."""
    prefs = {}
    root = tmp_path / "attachments"
    root.mkdir()
    monkeypatch.setattr(appmod, "load_prefs", lambda: dict(prefs))
    monkeypatch.setattr(appmod, "save_prefs", lambda p: prefs.update(p))
    monkeypatch.setattr(appmod, "attachments_dir", lambda: str(root))
    return SimpleNamespace(prefs=prefs, root=root)


def test_app_pass_prunes_and_records_the_daily_watermark(store, app_env):
    app_env.prefs["history_days"] = 90
    store.ingest("nodeA", "nodeA", 1, WALL - 120 * DAY, "ancient")
    app = FakeApp(store)

    gone = app._prune_history()

    assert gone["messages"] == 1
    assert app_env.prefs["history_pruned_at"] > 0
    assert any("history retention" in line for line in app.lines)


def test_app_pass_is_throttled_to_once_a_day(store, app_env):
    app_env.prefs.update({"history_days": 90, "history_pruned_at": time.time()})
    store.ingest("nodeA", "nodeA", 1, WALL - 120 * DAY, "ancient")
    app = FakeApp(store)

    assert app._prune_history() == {}
    assert texts(store, "nodeA") == ["ancient"], "the daily pass must not re-run"
    assert app._prune_history(force=True)["messages"] == 1


def test_app_pass_defaults_to_90_days(store, app_env):
    store.ingest("nodeA", "nodeA", 1, WALL - 120 * DAY, "ancient")
    store.ingest("nodeA", "nodeA", 2, WALL - 30 * DAY, "within the default")
    app = FakeApp(store)

    assert app._prune_history()["messages"] == 1
    assert texts(store, "nodeA") == ["within the default"]


def test_app_pass_off_when_the_pref_is_zero(store, app_env):
    app_env.prefs["history_days"] = 0
    store.ingest("nodeA", "nodeA", 1, WALL - 120 * DAY, "ancient")
    app = FakeApp(store)

    assert app._prune_history(force=True) == {}
    assert texts(store, "nodeA") == ["ancient"]
    assert "history_pruned_at" not in app_env.prefs


def test_app_pass_does_nothing_while_locked(app_env):
    app_env.prefs["history_days"] = 90
    assert FakeApp(None)._prune_history() == {}


def test_app_pass_reports_a_store_failure_and_retries_later(store, app_env, monkeypatch):
    app_env.prefs["history_days"] = 90

    def boom(*a, **k):
        raise RuntimeError("db is busy")
    monkeypatch.setattr(store, "prune_history", boom)
    app = FakeApp(store)

    assert app._prune_history() == {}
    assert any("skipped" in line for line in app.lines)
    assert "history_pruned_at" not in app_env.prefs, "a failed pass must not be marked done"


def test_app_pass_redraws_an_open_thread(store, app_env):
    app_env.prefs["history_days"] = 90
    store.ingest("nodeA", "nodeA", 1, WALL - 120 * DAY, "ancient")
    app = FakeApp(store)
    seen = []
    app.window = SimpleNamespace(messages=SimpleNamespace(
        selected={"conv": "nodeA"},
        refresh_conversation=lambda conv: seen.append(conv)))

    app._prune_history()

    assert seen == ["nodeA"]


# -- the CLI ---------------------------------------------------------------

@pytest.fixture
def cli_env(tmp_path, monkeypatch, store):
    identity = tmp_path / "identity.json"
    identity.write_text("{}")
    root = tmp_path / "attachments"
    root.mkdir()
    prefs = {"history_days": 90}
    monkeypatch.setattr(cli, "identity_path", lambda: str(identity))
    monkeypatch.setattr(cli, "cache_path", lambda: str(tmp_path / "state.db"))
    monkeypatch.setattr(cli, "get_passphrase", lambda *a, **k: "pw")
    monkeypatch.setattr(cli, "load_prefs", lambda: dict(prefs))
    monkeypatch.setattr(cli.paths, "attachments_dir", lambda: str(root))
    return SimpleNamespace(prefs=prefs, root=root)


def args(days=None, dry_run=False):
    return SimpleNamespace(days=days, dry_run=dry_run)


def test_cli_prune_dry_run_removes_nothing(cli_env, store, capsys):
    store.ingest("nodeA", "nodeA", 1, WALL - 120 * DAY, "ancient")

    cli.cmd_prune(args(dry_run=True))

    assert "1 message(s) older than 90 days would be removed" in capsys.readouterr().out
    assert texts(store, "nodeA") == ["ancient"]


def test_cli_prune_uses_the_stored_preference(cli_env, store, capsys):
    cli_env.prefs["history_days"] = 30
    store.ingest("nodeA", "nodeA", 1, WALL - 60 * DAY, "two months old")

    cli.cmd_prune(args())

    assert "older than 30 days" in capsys.readouterr().out
    assert texts(store, "nodeA") == []


def test_cli_prune_days_override_and_attachment_cleanup(cli_env, store, capsys):
    f = cli_env.root / "nodeA" / "old.bin"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"x")
    store.ingest("nodeA", "nodeA", 1, WALL - 10 * DAY, "file",
                 attachment={"path": str(f), "filename": "old.bin"})

    cli.cmd_prune(args(days=5))

    out = capsys.readouterr().out
    assert "removed 1 message(s) older than 5 days and 1 attachment file(s)" in out
    assert not f.exists()


def test_cli_prune_zero_reports_retention_off(cli_env, store, capsys):
    store.ingest("nodeA", "nodeA", 1, WALL - 120 * DAY, "ancient")

    cli.cmd_prune(args(days=0))

    assert "retention is off" in capsys.readouterr().out
    assert texts(store, "nodeA") == ["ancient"]


def test_cli_prune_rejects_a_negative_window(cli_env, store):
    with pytest.raises(SystemExit):
        cli.cmd_prune(args(days=-3))


def test_app_pass_ignores_a_malformed_window_pref(store, app_env):
    app_env.prefs["history_days"] = "ninety"
    store.ingest("nodeA", "nodeA", 1, WALL - 120 * DAY, "ancient")
    app = FakeApp(store)

    assert app._prune_history() == {}
    assert texts(store, "nodeA") == ["ancient"]
    assert app.lines == [], "a bad pref is skipped quietly, not logged every poll"


def test_app_pass_runs_when_the_watermark_is_unreadable(store, app_env):
    app_env.prefs.update({"history_days": 90, "history_pruned_at": "yesterday"})
    store.ingest("nodeA", "nodeA", 1, WALL - 120 * DAY, "ancient")

    assert FakeApp(store)._prune_history()["messages"] == 1


def test_cli_prune_rejects_a_malformed_pref(cli_env, store):
    cli_env.prefs["history_days"] = "ninety"

    with pytest.raises(SystemExit):
        cli.cmd_prune(args())
