import pytest

from warden.modules.wan import Wan

WANS = [("bsnl", "wan_bsnl"), ("railwire", "wan_railwire"), ("jio", "wan_jio")]


@pytest.fixture
def status(tmp_path):
    def write(**states):
        for iface, value in states.items():
            d = tmp_path / iface
            d.mkdir(exist_ok=True)
            (d / "STATUS").write_text(value + "\n")

    write(wan_bsnl="online", wan_railwire="online", wan_jio="online")
    return write


@pytest.fixture
def wan(agent, tmp_path, status):
    module = Wan(agent, WANS, status_dir=tmp_path)
    agent.add(module)
    return module


def test_active_is_first_online_in_order(agent, bus, status, wan):
    wan.poll()
    assert bus.state()["wan"] == {
        "active": "bsnl",
        "links": [
            {"name": "bsnl", "status": "online"},
            {"name": "railwire", "status": "online"},
            {"name": "jio", "status": "online"},
        ],
    }
    status(wan_bsnl="offline")
    wan.poll()
    assert bus.state()["wan"]["active"] == "railwire"


def test_transitions_are_events_but_first_poll_is_not(agent, bus, tmp_path, status, wan):
    wan.poll()
    assert bus.events() == []
    status(wan_jio="disconnecting")
    wan.poll()
    status(wan_jio="offline")
    wan.poll()
    (tmp_path / "wan_jio" / "STATUS").unlink()
    wan.poll()
    status(wan_jio="online")
    wan.poll()
    assert bus.events() == [
        {"event": "wan_down", "wan": "jio", "status": "disconnecting"},
        {"event": "wan_up", "wan": "jio", "status": "online"},
    ]


def test_missing_status_means_down_and_none_active(agent, bus, tmp_path, wan):
    for iface in ("wan_bsnl", "wan_railwire", "wan_jio"):
        (tmp_path / iface / "STATUS").unlink()
    assert wan.state()["active"] is None
    assert {link["status"] for link in wan.state()["links"]} == {"down"}


def test_unchanged_poll_does_not_republish(agent, bus, wan):
    wan.poll()
    bus.clear()
    wan.poll()
    assert bus.last("state") is None
