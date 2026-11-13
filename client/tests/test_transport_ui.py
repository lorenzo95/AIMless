"""Transport-aware UI helpers (Phase 5): the status/route bars and tray must
describe tailcat's relay model, not Yggdrasil's peer count, and keep the long
tailcat address readable."""
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