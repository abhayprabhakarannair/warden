import os
import subprocess

import pytest

from warden.modules.wan import Link, Wan, ubus_degraded, ubus_set

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
        self.degraded_calls = []
        self.degraded_fail = None
        self.ticks = {}
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

    def counters(self, device, rx, tx):
        d = self.root / "net" / device / "statistics"
        d.mkdir(parents=True, exist_ok=True)
        (d / "rx_bytes").write_text(f"{rx}\n")
        (d / "tx_bytes").write_text(f"{tx}\n")

    def track(self, iface, target, result, latency=None):
        """Writes one mwan3track round for a target, with a fresh mtime."""
        d = self.root / "status" / iface
        d.mkdir(parents=True, exist_ok=True)
        tick = self.ticks[iface, target] = self.ticks.get((iface, target), 0) + 1
        path = d / f"TRACK_{target}"
        path.write_text(result + "\n")
        os.utime(path, ns=(tick * 10**9, tick * 10**9))
        if latency is not None:
            (d / f"LATENCY_{target}").write_text(f"{latency}\n")

    def set_degraded(self, isps):
        if self.degraded_fail:
            raise self.degraded_fail
        self.degraded_calls.append(list(isps))

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
        reserved=lambda: {STB, TV},
        net_dir=router.root / "net",
        set_degraded=router.set_degraded,
    )
    agent.add(module)
    return module


def test_active_is_first_online_in_order_with_usage_and_caps(agent, bus, router, wan):
    wan.poll()
    assert bus.state()["wan"] == {
        "active": "bsnl",
        "links": [
            {"name": "bsnl", "status": "online", "used_bytes": 5, "degraded": False},
            {"name": "railwire", "status": "online", "used_bytes": 0, "degraded": False},
            {"name": "jio", "status": "online", "used_bytes": 300, "cap_bytes": 1000 * 10**9, "degraded": False},
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


def test_usage_failure_keeps_the_last_values_and_is_reported_until_a_read_succeeds(agent, bus, clock, router, wan):
    wan.poll()
    bus.clear()
    router.fail = OSError("vnstat exited 1: Error: Database is empty.")
    clock.advance(60)
    wan.poll()
    state = bus.state()["wan"]
    assert state["links"][2]["used_bytes"] == 300
    assert state["usage_error"] == "cannot read usage: vnstat exited 1: Error: Database is empty."
    clock.advance(60)
    wan.poll()
    assert [e["event"] for e in bus.events()] == ["error"]
    router.fail = None
    router.usage["lan3"] = 400
    clock.advance(60)
    wan.poll()
    state = bus.state()["wan"]
    assert "usage_error" not in state
    assert state["links"][2]["used_bytes"] == 400


def test_usage_failing_from_the_start_reports_why_with_no_values(bus, router, wan):
    router.fail = subprocess.TimeoutExpired("vnstat", 10)
    wan.poll()
    state = bus.state()["wan"]
    assert state["usage_error"].startswith("cannot read usage: Command 'vnstat' timed out")
    assert all("used_bytes" not in link for link in state["links"])


def test_recovery_without_new_usage_still_clears_the_error(bus, clock, router, wan):
    wan.poll()
    router.fail = OSError("vnstat missing")
    clock.advance(60)
    wan.poll()
    router.fail = None
    clock.advance(60)
    wan.poll()
    assert "usage_error" not in bus.state()["wan"]


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
    assert wan.route(STB, True) == {"via": "jio", "override": None}
    assert wan.route(TV, True) == {"via": "bsnl", "override": None}
    router.status(wan_jio="offline")
    wan.poll()
    assert wan.route(STB, True) == {"via": "bsnl", "override": None}


def test_isp_command_sets_an_override(agent, bus, router, wan):
    wan.poll()
    agent.on_message(b"isp 192.0.2.8 jio console")
    assert router.set_calls == [(TV, "jio")]
    assert {"event": "isp_set", "ip": TV, "isp": "jio", "source": "console"} in bus.events()
    assert wan.route(TV, True) == {"via": "jio", "override": "jio"}


def test_override_on_an_offline_isp_falls_back(agent, router, wan):
    agent.on_message(b"isp 192.0.2.8 jio")
    router.status(wan_jio="offline")
    wan.poll()
    assert wan.route(TV, True) == {"via": "bsnl", "override": "jio"}


def test_auto_clears_the_override(agent, router, wan):
    agent.on_message(b"isp 192.0.2.8 railwire")
    agent.on_message(b"isp 192.0.2.8 auto")
    wan.poll()
    assert wan.route(TV, True) == {"via": "bsnl", "override": None}


def test_unreserved_address_is_rejected_without_calling_the_router(agent, bus, router, wan):
    agent.on_message(b"isp 192.0.2.9 jio")
    assert router.set_calls == []
    assert bus.events() == [
        {"event": "rejected", "command": "isp", "reason": "not a reserved address", "ip": "192.0.2.9"}
    ]


def test_auto_is_allowed_for_an_unreserved_address(agent, router, wan):
    agent.on_message(b"isp 192.0.2.9 auto")
    assert router.set_calls == [("192.0.2.9", "auto")]


def test_override_of_an_unreserved_address_is_ignored(router, wan):
    (router.root / "overrides" / "192.0.2.9").write_text("jio\n")
    wan.poll()
    assert wan.route("192.0.2.9", False) == {"via": "bsnl", "override": None}


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
    assert wan.route(TV, True)["override"] is None


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
    assert wan.route(TV, True)["override"] == "jio"
    wan._read_overrides = readable
    wan.poll()
    wan._read_overrides = deny
    wan.poll()
    assert [e["event"] for e in bus.events()].count("error") == 2


def test_missing_override_directory_means_none(router, wan):
    import shutil

    shutil.rmtree(router.root / "overrides")
    wan.poll()
    assert wan.route(TV, True)["override"] is None


def test_isp_command_reports_unreadable_overrides_without_failing(agent, bus, router, wan):
    def broken():
        raise IsADirectoryError("Is a directory: '/tmp/wanpref/192.0.2.8'")

    wan._read_overrides = broken
    agent.on_message(b"isp 192.0.2.8 jio console")
    assert [e["event"] for e in bus.events()] == ["isp_set", "error"]
    assert "cannot read overrides" in bus.events()[-1]["reason"]


def test_isp_command_with_unreadable_overrides_reflects_what_the_router_accepted(agent, bus, router, wan):
    agent.on_message(b"isp 192.0.2.7 railwire")
    wan.poll()

    def deny():
        raise PermissionError("Permission denied: '/tmp/wanpref'")

    wan._read_overrides = deny
    agent.on_message(b"isp 192.0.2.8 jio")
    assert wan.route(TV, True)["override"] == "jio"
    agent.on_message(b"isp 192.0.2.7 auto")
    assert wan.route(STB, True)["override"] is None
    assert bus.state()["wan"]["active"] == "bsnl"


def rates(bus):
    return {link["name"]: (link.get("rx_bps"), link.get("tx_bps")) for link in bus.state()["wan"]["links"]}


def test_rates_are_averaged_over_the_window(bus, clock, router, wan):
    router.counters("wan1", 1_000_000, 500_000)
    router.counters("lan2", 0, 0)
    wan.poll()
    assert rates(bus) == {"bsnl": (None, None), "railwire": (None, None), "jio": (None, None)}
    router.counters("wan1", 26_000_000, 3_000_000)
    clock.advance(2)
    wan.poll()
    assert rates(bus)["bsnl"] == (None, None)
    clock.advance(3)
    wan.poll()
    assert rates(bus) == {"bsnl": (40_000_000, 4_000_000), "railwire": (0, 0), "jio": (None, None)}


def test_a_reset_counter_drops_the_rate_for_one_window(bus, clock, router, wan):
    router.counters("wan1", 5_000_000, 5_000_000)
    wan.poll()
    router.counters("wan1", 1_000, 1_000)
    clock.advance(5)
    wan.poll()
    assert rates(bus)["bsnl"] == (None, None)
    router.counters("wan1", 626_000, 1_000)
    clock.advance(5)
    wan.poll()
    assert rates(bus)["bsnl"] == (1_000_000, 0)


@pytest.mark.parametrize("content", [None, "garbage"])
def test_unreadable_counters_give_no_rate(bus, clock, router, wan, content):
    router.counters("wan1", 0, 0)
    wan.poll()
    stats = router.root / "net" / "wan1" / "statistics" / "rx_bytes"
    if content is None:
        stats.unlink()
    else:
        stats.write_text(content)
    clock.advance(5)
    wan.poll()
    assert rates(bus)["bsnl"] == (None, None)


def test_only_large_rate_changes_publish(bus, clock, router, wan):
    rx = 0
    router.counters("wan1", rx, 0)
    wan.poll()

    def step(bps):
        nonlocal rx
        rx += bps * 5 // 8
        router.counters("wan1", rx, 0)
        clock.advance(5)
        bus.clear()
        wan.poll()
        return bus.last("state") is not None

    assert step(10_000_000)
    assert not step(10_500_000)
    assert step(11_000_000)
    assert not step(11_050_000)
    assert step(0)
    assert not step(90_000)
    assert step(200_000)


def ubus_args(monkeypatch):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    monkeypatch.setattr("warden.modules.wan.subprocess.run", run)
    return calls


def test_ubus_calls_name_the_method_and_pass_json(monkeypatch):
    calls = ubus_args(monkeypatch)
    ubus_set("192.0.2.8", "jio")
    ubus_degraded(["railwire"])
    assert calls == [
        ["ubus", "call", "wanpref", "set", '{"ip": "192.0.2.8", "isp": "jio"}'],
        ["ubus", "call", "wanpref", "degraded", '{"isps": ["railwire"]}'],
    ]


def rounds(router, clock, wan, results, iface="wan_railwire", target="1.1.1.1", latency=20):
    """One mwan3track round and one poll per result, a second apart."""
    for ok in results:
        router.track(iface, target, "up" if ok else "down", latency if ok else 999999)
        clock.advance(1)
        wan.poll()


def railwire(bus):
    return next(link for link in bus.state()["wan"]["links"] if link["name"] == "railwire")


def test_quality_needs_enough_samples(bus, clock, router, wan):
    rounds(router, clock, wan, [False] * 19)
    link = railwire(bus)
    assert "loss_pct" not in link and "latency_ms" not in link
    assert link["degraded"] is False


def test_each_round_counts_once_and_skipped_targets_are_ignored(bus, clock, router, wan):
    rounds(router, clock, wan, [True] * 19)
    for _ in range(3):
        router.track("wan_railwire", "8.8.8.8", "skipped")
        clock.advance(1)
        wan.poll()
    assert "loss_pct" not in railwire(bus)
    router.track("wan_railwire", "8.8.8.8", "down")
    clock.advance(1)
    wan.poll()
    assert railwire(bus)["loss_pct"] == 5.0


def test_loss_degrades_and_recovers_with_hysteresis(bus, clock, router, wan):
    rounds(router, clock, wan, [True] * 18 + [False, False])
    link = railwire(bus)
    assert (link["loss_pct"], link["latency_ms"], link["degraded"]) == (10.0, 20, True)
    assert bus.events()[-1] == {"event": "wan_degraded", "wan": "railwire", "loss_pct": 10.0, "latency_ms": 20}
    rounds(router, clock, wan, [True] * 40)  # 2 of 60 lost: 3.3 %, between the thresholds
    assert railwire(bus)["degraded"] is True
    rounds(router, clock, wan, [True] * 30)  # the losses left the window
    link = railwire(bus)
    assert (link["loss_pct"], link["degraded"]) == (0.0, False)
    assert bus.events()[-1] == {"event": "wan_recovered", "wan": "railwire", "loss_pct": 1.7, "latency_ms": 20}


def test_high_latency_alone_degrades(bus, clock, router, wan):
    rounds(router, clock, wan, [True] * 20, latency=350)
    assert railwire(bus)["degraded"] is True
    rounds(router, clock, wan, [True] * 60, latency=250)
    assert railwire(bus)["degraded"] is True
    rounds(router, clock, wan, [True] * 60, latency=150)
    assert railwire(bus)["degraded"] is False


def test_all_lost_gives_no_latency(bus, clock, router, wan):
    rounds(router, clock, wan, [False] * 20)
    link = railwire(bus)
    assert link["loss_pct"] == 100.0 and "latency_ms" not in link
    assert bus.events()[-1]["latency_ms"] is None


def test_going_offline_clears_quality_without_a_recovery_event(bus, clock, router, wan):
    rounds(router, clock, wan, [False] * 20)
    router.status(wan_railwire="offline")
    clock.advance(1)
    wan.poll()
    link = railwire(bus)
    assert link["degraded"] is False and "loss_pct" not in link
    assert [e["event"] for e in bus.events()][-2:] == ["wan_degraded", "wan_down"]
    router.status(wan_railwire="online")
    rounds(router, clock, wan, [True] * 19)
    assert "loss_pct" not in railwire(bus)


def test_router_hears_the_degraded_set_on_change_and_every_minute(clock, router, wan):
    rounds(router, clock, wan, [True] * 20)
    assert router.degraded_calls == [[]]
    rounds(router, clock, wan, [False] * 2)
    assert router.degraded_calls == [[], ["railwire"]]
    rounds(router, clock, wan, [False] * 59)
    assert router.degraded_calls == [[], ["railwire"]]
    rounds(router, clock, wan, [False])
    assert router.degraded_calls == [[], ["railwire"], ["railwire"]]


def test_router_failure_is_reported_once_and_retried(bus, clock, router, wan):
    router.degraded_fail = OSError("Command failed: Method not found")
    rounds(router, clock, wan, [True] * 25)
    errors = [e for e in bus.events() if e["event"] == "error"]
    assert errors == [{"event": "error", "reason": "cannot report degraded links: Command failed: Method not found"}]
    router.degraded_fail = None
    rounds(router, clock, wan, [True] * 10)
    assert router.degraded_calls == [[]]


def test_degraded_override_falls_back_like_an_offline_one(agent, clock, router, wan):
    agent.on_message(b"isp 192.0.2.8 railwire")
    rounds(router, clock, wan, [True] * 18 + [False, False])
    assert wan.route(TV, True) == {"via": "bsnl", "override": "railwire"}
    assert wan.route(STB, True) == {"via": "jio", "override": None}


def test_small_quality_changes_do_not_publish(bus, clock, router, wan):
    rounds(router, clock, wan, [True] * 20, latency=20)
    bus.clear()
    rounds(router, clock, wan, [True] * 5, latency=22)
    assert bus.last("state") is None
    rounds(router, clock, wan, [True] * 30, latency=30)
    assert bus.last("state") is not None
