"""Transport-aware UI helpers (Phase 5): the status/route bars and tray must
describe tailcat's relay model, not Yggdrasil's peer count, and keep the long
tailcat address readable."""
import json

from aimless import gtkui


def test_is_tailcat():
    assert gtkui.is_tailcat({"transport": "tailcat"})
    assert not gtkui.is_tailcat({"transport": "yggdrasil"})
    assert not gtkui.is_tailcat({})
    assert not gtkui.is_tailcat(None)


def test_net_label():
    assert gtkui.net_label({"transport": "tailcat", "peers_up": 1}) == "relay up"
    assert gtkui.net_label({"transport": "tailcat", "peers_up": 0}) == "relay down"
    assert gtkui.net_label({"transport": "yggdrasil", "peers_up": 3, "peers_total": 5}) == "peers 3/5"
    assert gtkui.net_label(None) == "peers 0/0"


def test_short_address():
    assert gtkui.short_address("abc") == "abc"
    assert gtkui.short_address("") == ""
    long = "tc" + "x" * 200
    short = gtkui.short_address(long)
    assert short.endswith("…")
    assert len(short) <= 29


def test_self_state_tailcat():
    _, text = gtkui._self_state({"transport": "tailcat", "peers_up": 1, "peers_total": 1})
    assert text == "online"
    _, text = gtkui._self_state({"transport": "tailcat", "peers_up": 0, "peers_total": 1})
    assert "relay" in text


def test_self_state_ygg_redundancy():
    # One peer left of several is "at risk" for Yggdrasil...
    _, text = gtkui._self_state({"transport": "yggdrasil", "peers_up": 1, "peers_total": 3})
    assert text == "at risk - one peer left"
    # ...but a single relay is normal for tailcat (no false amber).
    _, text = gtkui._self_state({"transport": "tailcat", "peers_up": 1, "peers_total": 1})
    assert text == "online"


def test_relay_tooltip_shows_region_not_long_address():
    long = "tc" + "x" * 200
    st = {"region": {"name": "Toronto", "code": "tor"}, "address": long}
    tip = gtkui.relay_tooltip(st)
    assert "Toronto" in tip
    assert long not in tip  # the long address belongs in the invite, not the hover
    # No region known yet -> still no long address.
    assert long not in gtkui.relay_tooltip({"address": long})


class _FakeSupervisor:
    def __init__(self, external=False, status=None, running=True):
        self.external = external
        self.sock = "/tmp/fake.sock"
        self._status = status
        self._running = running

    def status(self):
        return self._status

    def is_running(self):
        return self._running


def _patch_relay_io(monkeypatch, reply):
    """Stub DaemonClient + restart_daemon; record what apply_relay does."""
    rec = {"setrelay": [], "restart": 0}

    class _DC:
        def __init__(self, sock):
            pass

        def request(self, op, timeout=None, **fields):
            rec["setrelay"].append((op, fields))
            return reply

        def close(self):
            pass

    def _restart(supervisor, log=None):
        rec["restart"] += 1

    monkeypatch.setattr(gtkui, "DaemonClient", _DC)
    monkeypatch.setattr(gtkui, "restart_daemon", _restart)
    return rec


def test_apply_relay_local_restarts_and_reports_region(monkeypatch):
    new = {"address": "tcNEW", "region": {"id": 21, "name": "Toronto", "code": "tor"},
           "relay": {"region_id": 21}}
    sup = _FakeSupervisor(external=False, status=new, running=True)
    rec = _patch_relay_io(monkeypatch, {"op": "relay", "address": "tcOLD"})
    msg, got = gtkui.apply_relay(sup, {"hostname": "", "region_id": 21}, {})
    assert "Toronto" in msg
    assert got is new                     # refreshed status (new address) returned
    assert rec["restart"] == 1            # local daemon restarted to apply
    assert rec["setrelay"][0][0] == "setrelay"
    assert rec["setrelay"][0][1]["relay"] == {"hostname": "", "region_id": 21}


def test_apply_relay_noop_skips_restart(monkeypatch):
    sup = _FakeSupervisor(external=False, status={}, running=True)
    rec = _patch_relay_io(monkeypatch, {"op": "relay", "address": "tcOLD"})
    msg, got = gtkui.apply_relay(sup, {"hostname": "", "region_id": 21}, {"region_id": 21})
    assert msg == "No change."
    assert got is None
    assert rec["restart"] == 0            # don't drop connections for a no-op save


def test_apply_relay_remote_does_not_restart(monkeypatch):
    sup = _FakeSupervisor(external=True, status={}, running=True)
    rec = _patch_relay_io(monkeypatch, {"op": "relay", "address": "tcOLD"})
    msg, got = gtkui.apply_relay(sup, {"hostname": "", "region_id": 21}, {})
    assert "remote daemon" in msg
    assert got is None
    assert rec["restart"] == 0            # client can't restart a remote daemon


def test_apply_relay_error_reply(monkeypatch):
    sup = _FakeSupervisor(external=False, status={}, running=True)
    rec = _patch_relay_io(monkeypatch, {"op": "error", "error": "boom"})
    msg, got = gtkui.apply_relay(sup, {"region_id": 21}, {})
    assert msg.startswith("error:") and "boom" in msg
    assert got is None
    assert rec["restart"] == 0


def test_apply_relay_restart_failed(monkeypatch):
    sup = _FakeSupervisor(external=False, status=None, running=False)
    rec = _patch_relay_io(monkeypatch, {"op": "relay", "address": "tcOLD"})
    msg, got = gtkui.apply_relay(sup, {"region_id": 21}, {})
    assert "did not restart" in msg
    assert got is None
    assert rec["restart"] == 1


def test_update_peers_label_targets_the_window_item():
    # Regression: the menu item lives on the window, not the app. The old code
    # read self.peers_item on the app (always None), so the label never changed
    # from "Peers …" even on tailcat.
    class FakeItem:
        def __init__(self):
            self.label = None

        def set_label(self, label):
            self.label = label

    class FakeWin:
        def __init__(self):
            self.peers_item = FakeItem()

    win = FakeWin()
    gtkui.AimlessWindow._update_peers_label(win, {"transport": "tailcat"})
    assert win.peers_item.label == "Relay …"
    gtkui.AimlessWindow._update_peers_label(win, {"transport": "yggdrasil"})
    assert win.peers_item.label == "Peers …"
    # A window without the item yet must not raise.
    win.peers_item = None
    gtkui.AimlessWindow._update_peers_label(win, {"transport": "tailcat"})


def test_daemon_binary_finds_tailcat_without_env(tmp_path, monkeypatch):
    monkeypatch.delenv("AIMLESS_DAEMON_BIN", raising=False)
    binpath = tmp_path / "aimlessd-tailcat-linux-amd64"
    binpath.write_text("#!/bin/sh\n")
    monkeypatch.setattr(gtkui.sys, "argv", [str(tmp_path / "aimless.pyz")])
    assert gtkui.daemon_binary() == str(binpath)


def test_daemon_binary_prefers_yggdrasil_when_both_present(tmp_path, monkeypatch):
    monkeypatch.delenv("AIMLESS_DAEMON_BIN", raising=False)
    (tmp_path / "aimlessd").write_text("x")
    (tmp_path / "aimlessd-tailcat").write_text("x")
    monkeypatch.setattr(gtkui.sys, "argv", [str(tmp_path / "aimless.pyz")])
    assert gtkui.daemon_binary() == str(tmp_path / "aimlessd")


def test_reset_relay_config_keeps_other_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(gtkui.paths, "daemon_dir", lambda: str(tmp_path))
    (tmp_path / "config.json").write_text(
        json.dumps({"peers": ["x"], "relay": {"region_id": 21}}))
    assert gtkui.reset_relay_config() is True
    after = json.loads((tmp_path / "config.json").read_text())
    assert "relay" not in after and after["peers"] == ["x"]
    assert gtkui.reset_relay_config() is False  # nothing left to reset


def test_apply_relay_rejects_bad_map_url(monkeypatch):
    sup = _FakeSupervisor(external=False, status={}, running=True)
    rec = _patch_relay_io(monkeypatch, {"op": "relay", "address": "tcOLD"})
    monkeypatch.setattr(gtkui, "_derp_map_error", lambda url: "could not fetch")
    msg, got = gtkui.apply_relay(sup, {"derpmap_url": "http://bad.example/map"}, {})
    assert msg.startswith("error:") and "DERP map URL" in msg
    assert got is None and rec["restart"] == 0 and rec["setrelay"] == []


def test_derp_map_error_validates_regions(monkeypatch):
    import urllib.request

    class FakeResp:
        def __init__(self, body):
            self._body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self._body

    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: FakeResp(b'{"hello": 1}'))
    assert gtkui._derp_map_error("http://x/map") is not None
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: FakeResp(b'{"Regions": {"1": {}}}'))
    assert gtkui._derp_map_error("http://x/map") is None


def test_write_tailcat_key_and_relay_restore(tmp_path):
    keytext = '{"key": "privkey:abc", "psk": "psk:def"}'
    gtkui._write_tailcat_key(str(tmp_path), keytext)
    written = (tmp_path / "tailcat.key").read_text()
    assert written.strip() == keytext
    assert (tmp_path / "tailcat.key").stat().st_mode & 0o777 == 0o600
    # Relay merge preserves unrelated config keys.
    (tmp_path / "config.json").write_text(json.dumps({"peers": ["p"]}))
    gtkui._write_relay_config(str(tmp_path), {"region_id": 21})
    cfg = json.loads((tmp_path / "config.json").read_text())
    assert cfg["relay"] == {"region_id": 21} and cfg["peers"] == ["p"]


def test_region_label_country_and_fallback():
    assert gtkui.region_label({"name": "Toronto", "code": "tor"}) == "Canada - Toronto (tor)"
    assert gtkui.region_label({"name": "Frankfurt", "code": "fra"}) == "Germany - Frankfurt (fra)"
    assert gtkui.region_label({"name": "Nuremberg", "code": "nue"}) == "Germany - Nuremberg (nue)"
    # Unknown/custom region falls back to its own name.
    assert gtkui.region_label({"name": "My Relay", "code": "xyz"}) == "My Relay (xyz)"


def test_relay_tooltip_shows_region_and_servers():
    st = {"region": {"name": "Toronto", "code": "tor",
                     "nodes": ["derp21b.tailscale.com", "derp21c.tailscale.com"]}}
    tip = gtkui.relay_tooltip(st)
    assert "Toronto" in tip
    assert "derp21b.tailscale.com" in tip
    # Many nodes are truncated with a count.
    st["region"]["nodes"] = [f"derp{i}.example.com" for i in range(6)]
    assert "+3 more" in gtkui.relay_tooltip(st)


def test_make_region_list_sorts_and_preselects():
    regions = [{"id": 4, "code": "fra", "name": "Frankfurt"},
               {"id": 21, "code": "tor", "name": "Toronto"},
               {"id": 26, "code": "nue", "name": "Nuremberg"}]
    scroll, view = gtkui.make_region_list(regions, 21)
    model = view.get_model()
    labels = [model[i][0] for i in range(len(model))]
    assert labels == ["Canada - Toronto (tor)", "Germany - Frankfurt (fra)",
                      "Germany - Nuremberg (nue)"]
    sel_model, it = view.get_selection().get_selected()
    assert it is not None and sel_model.get_value(it, 1) == 21
    assert view.get_enable_search()