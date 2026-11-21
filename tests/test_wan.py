import subprocess

import pytest

from warden.modules.wan import Link, Wan, ubus_set

LINKS = [
    Link("bsnl", "wan_bsnl", "wan1"),
    Link("railwire", "wan_railwire", "lan2"),
    Link("jio", "wan_jio", "lan3", cap_bytes=1000 * 10**9),
]
STB, TV = "192.0.2.7", "192.0.2.8"


class Router:
    def __init__(self, root):
        self.root = root
        self.usage = {"wan1": 5, "lan2": 0, "lan3": 300}
        self.usage_calls = 0
        self.fail = None
        self.set_calls = []
        (root / "overrides").mkdir()
        self.status(wan_bsnl="online", wan_railwire="online", wan_jio="online")

    def status(self, **states):
        for iface, value in states.items():
            d = self.root / "status" / iface
            d.mkdir(parents=True, exist_ok=True)
            (d / "STATUS").write_text(value + "\n")

    def read_usage(self, devices):
        self.usage_calls += 1
        if self.fail:
            raise self.fail
        return {d: b for d, b in self.usage.items() if d in devices}

    def set_override(self, ip, isp):
        if self.fail:
            raise self.fail
        self.set_calls.append((ip, isp))
        path = self.root / "overrides" / ip
        if isp == "auto":
            path.unlink(missing_ok=True)
        else:
            path.write_text(isp + "\n")


@pytest.fixture
def router(tmp_path):
    return Router(tmp_path)


@pytest.fixture
def wan(agent, router):
    module = Wan(
        agent,
        LINKS,
        preferences={STB: "jio"},
        status_dir=router.root / "status",
        override_dir=router.root / "overrides",
        usage=router.read_usage,
        set_override=router.set_override,
    )
    agent.add(module)
    return module


def test_active_is_first_online_in_order_with_usage_and_caps(agent, bus, router, wan):
    wan.poll()
    assert bus.state()["wan"] == {
        "active": "bsnl",
        "links": [
            {"name": "bsnl", "status": "online", "used_bytes": 5},
            {"name": "railwire", "status": "online", "used_bytes": 0},
            {"name": "jio", "status": "online", "used_bytes": 300, "cap_bytes": 1000 * 10**9},
        ],
    }
    router.status(wan_bsnl="offline")
    wan.poll()
    assert bus.state()["wan"]["active"] == "railwire"


def test_usage_is_read_once_a_minute(agent, clock, router, wan):
    wan.poll()
    clock.advance(30)
    wan.poll()
    assert router.usage_calls == 1
    clock.advance(30)
    wan.poll()
    assert router.usage_calls == 2


def test_usage_failure_keeps_the_last_values(agent, bus, clock, router, wan):
    wan.poll()
    router.fail = OSError("vnstat missing")
    clock.advance(60)
    wan.poll()
    assert bus.state()["wan"]["links"][2]["used_bytes"] == 300


def test_transitions_are_events_but_first_poll_is_not(agent, bus, router, wan):
    wan.poll()
    assert bus.events() == []
    router.status(wan_jio="disconnecting")
    wan.poll()
    router.status(wan_jio="offline")
    wan.poll()
    (router.root / "status" / "wan_jio" / "STATUS").unlink()
    wan.poll()
    router.status(wan_jio="online")
    wan.poll()
    assert bus.events() == [
        {"event": "wan_down", "wan": "jio", "status": "disconnecting"},
        {"event": "wan_up", "wan": "jio", "status": "online"},
    ]


def test_missing_status_means_down_and_none_active(router, wan):
    for iface in ("wan_bsnl", "wan_railwire", "wan_jio"):
        (router.root / "status" / iface / "STATUS").unlink()
    assert wan.state()["active"] is None
    assert {link["status"] for link in wan.state()["links"]} == {"down"}


def test_unchanged_poll_does_not_republish(bus, wan):
    wan.poll()
    bus.clear()
    wan.poll()
    assert bus.last("state") is None


def test_route_follows_preference_then_house_order(router, wan):
    wan.poll()
    assert wan.route(STB) == {"via": "jio", "override": None}
    assert wan.route(TV) == {"via": "bsnl", "override": None}
    router.status(wan_jio="offline")
    wan.poll()
    assert wan.route(STB) == {"via": "bsnl", "override": None}


def test_isp_command_sets_an_override(agent, bus, router, wan):
    wan.poll()
    agent.on_message(b"isp 192.0.2.8 jio console")
    assert router.set_calls == [(TV, "jio")]
    assert {"event": "isp_set", "ip": TV, "isp": "jio", "source": "console"} in bus.events()
    assert wan.route(TV) == {"via": "jio", "override": "jio"}


def test_override_on_an_offline_isp_falls_back(agent, router, wan):
    agent.on_message(b"isp 192.0.2.8 jio")
    router.status(wan_jio="offline")
    wan.poll()
    assert wan.route(TV) == {"via": "bsnl", "override": "jio"}


def test_auto_clears_the_override(agent, router, wan):
    agent.on_message(b"isp 192.0.2.8 railwire")
    agent.on_message(b"isp 192.0.2.8 auto")
    wan.poll()
    assert wan.route(TV) == {"via": "bsnl", "override": None}


def test_unknown_isp_is_rejected_without_calling_the_router(agent, bus, router, wan):
    agent.on_message(b"isp 192.0.2.8 starlink")
    assert router.set_calls == []
    assert bus.events() == [{"event": "rejected", "command": "isp", "reason": "unknown isp", "isp": "starlink"}]


def test_router_refusal_is_reported(agent, bus, router, wan):
    router.fail = OSError("Command failed: Permission denied")
    agent.on_message(b"isp 192.0.2.8 jio")
    assert bus.events() == [
        {
            "event": "isp_failed",
            "ip": TV,
            "isp": "jio",
            "source": "mqtt",
            "error": "Command failed: Permission denied",
        }
    ]


def test_unknown_override_files_are_ignored(router, wan):
    (router.root / "overrides" / TV).write_text("starlink\n")
    wan.poll()
    assert wan.route(TV)["override"] is None


@pytest.mark.parametrize("ip", ["..", "...", "192.0.2", "999.1.1.1", "192.0.2.07"])
def test_invalid_addresses_never_reach_the_router(agent, bus, router, wan, ip):
    agent.on_message(f"isp {ip} jio".encode())
    assert router.set_calls == []
    assert bus.events() == [{"event": "rejected", "command": "isp", "reason": "invalid ip", "ip": ip}]


def test_override_files_not_named_by_an_address_are_ignored(router, wan):
    (router.root / "overrides" / "..x").write_text("jio\n")
    wan.poll()
    assert wan._overrides == {}


def test_ubus_set_times_out_as_an_os_error(monkeypatch):
    def hang(argv, **kw):
        assert kw["timeout"] == 10
        raise subprocess.TimeoutExpired(argv, kw["timeout"])

    monkeypatch.setattr("warden.modules.wan.subprocess.run", hang)
    with pytest.raises(OSError, match="timed out"):
        ubus_set("192.0.2.8", "jio")


def test_hung_router_call_is_reported_and_releases_the_agent(agent, bus, router, wan):
    def hang(ip, isp):
        raise OSError("ubus call wanpref timed out")

    wan._set_override = hang
    agent.on_message(b"isp 192.0.2.8 jio")
    assert bus.events()[-1]["event"] == "isp_failed"
    agent.on_message(b"refresh")
    assert bus.last("meta") is not None


def test_unreadable_overrides_keep_status_flowing_and_are_reported_once(agent, bus, router, wan):
    agent.on_message(b"isp 192.0.2.8 jio")
    wan.poll()
    bus.clear()

    def deny():
        raise PermissionError("Permission denied: '/tmp/wanpref'")

    readable = wan._read_overrides
    wan._read_overrides = deny
    router.status(wan_bsnl="offline")
    wan.poll()
    wan.poll()
    assert [e["event"] for e in bus.events()] == ["error", "wan_down"]
    assert bus.state()["wan"]["active"] == "railwire"
    assert wan.route(TV)["override"] == "jio"
    wan._read_overrides = readable
    wan.poll()
    wan._read_overrides = deny
    wan.poll()
    assert [e["event"] for e in bus.events()].count("error") == 2


def test_missing_override_directory_means_none(router, wan):
    import shutil

    shutil.rmtree(router.root / "overrides")
    wan.poll()
    assert wan.route(TV)["override"] is None


def test_isp_command_reports_unreadable_overrides_without_failing(agent, bus, router, wan):
    def broken():
        raise IsADirectoryError("Is a directory: '/tmp/wanpref/192.0.2.8'")

    wan._read_overrides = broken
    agent.on_message(b"isp 192.0.2.8 jio console")
    assert [e["event"] for e in bus.events()] == ["isp_set", "error"]
    assert "cannot read overrides" in bus.events()[-1]["reason"]
