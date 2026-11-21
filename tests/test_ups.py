import subprocess

import pytest

from warden.modules import ups as ups_module
from warden.modules.ups import Ups, upsc

ONLINE = {
    "device.model": "Back-UPS RS 1500G-IN",
    "ups.status": "OL CHRG",
    "battery.charge": "100",
    "battery.runtime": "1830",
    "ups.load": "21",
    "ups.realpower.nominal": "865",
    "input.voltage": "232.0",
}


class Nut:
    def __init__(self):
        self.values = dict(ONLINE)
        self.fail = None

    def read(self, name):
        assert name == "apc@localhost"
        if self.fail:
            raise self.fail
        return dict(self.values)


@pytest.fixture
def nut():
    return Nut()


@pytest.fixture
def ups(agent, nut):
    module = Ups(agent, "apc@localhost", read=nut.read)
    agent.add(module)
    return module


def test_state_before_a_read_is_unknown(agent, bus, ups):
    agent.on_connect()
    assert bus.state()["ups"] == {"status": "unknown"}


def test_full_state(bus, ups):
    ups.poll()
    assert bus.state()["ups"] == {
        "status": "online",
        "flags": ["OL", "CHRG"],
        "charge_pct": 100,
        "runtime_s": 1830,
        "load_pct": 21,
        "power_w": 182,
        "input_v": 232,
        "model": "Back-UPS RS 1500G-IN",
    }
    assert bus.events() == []


def test_outage_sequence_emits_each_transition_once(bus, nut, ups):
    ups.poll()
    for status, charge in [("OB DISCHRG", "90"), ("OB DISCHRG", "60"), ("OB DISCHRG LB", "30"), ("OL CHRG", "31")]:
        nut.values |= {"ups.status": status, "battery.charge": charge}
        ups.poll()
    assert [(e["event"], e["charge_pct"]) for e in bus.events()] == [
        ("on_battery", 90),
        ("low_battery", 30),
        ("on_line", 31),
    ]
    assert bus.state()["ups"]["status"] == "online"


def test_low_battery_wins_over_on_battery_and_online(bus, nut, ups):
    nut.values["ups.status"] = "OL OB LB"
    ups.poll()
    assert bus.state()["ups"]["status"] == "low_battery"


def test_unknown_flags_report_unknown_without_events(bus, nut, ups):
    ups.poll()
    nut.values["ups.status"] = "BYPASS"
    ups.poll()
    assert bus.state()["ups"]["status"] == "unknown"
    assert bus.events() == []


def test_missing_and_garbage_values_are_left_out(bus, nut, ups):
    nut.values = {"ups.status": "OL", "battery.charge": "n/a", "ups.load": "-1", "battery.runtime": "inf"}
    ups.poll()
    assert bus.state()["ups"] == {"status": "online", "flags": ["OL"]}


def test_power_needs_a_nominal_rating(bus, nut, ups):
    del nut.values["ups.realpower.nominal"]
    ups.poll()
    state = bus.state()["ups"]
    assert state["load_pct"] == 21
    assert "power_w" not in state


def test_ups_model_is_the_fallback(bus, nut, ups):
    del nut.values["device.model"]
    nut.values["ups.model"] = "Back-UPS"
    ups.poll()
    assert bus.state()["ups"]["model"] == "Back-UPS"


def test_small_changes_do_not_publish(bus, nut, ups):
    ups.poll()
    bus.clear()
    nut.values |= {"input.voltage": "229.4", "battery.runtime": "1820"}
    ups.poll()
    assert bus.last("state") is None
    nut.values["battery.runtime"] = "1790"
    ups.poll()
    assert bus.state()["ups"]["input_v"] == 229


@pytest.mark.parametrize("fail", [OSError("Error: Data stale"), subprocess.TimeoutExpired("upsc", 10)])
def test_read_failure_keeps_values_and_reports_once(bus, nut, ups, fail):
    ups.poll()
    nut.fail = fail
    ups.poll()
    ups.poll()
    state = bus.state()["ups"]
    assert state["charge_pct"] == 100
    assert state["error"].startswith("cannot read apc@localhost")
    assert [e["event"] for e in bus.events()] == ["ups_lost"]
    nut.fail = None
    ups.poll()
    assert "error" not in bus.state()["ups"]
    assert [e["event"] for e in bus.events()] == ["ups_lost", "ups_found"]


def test_failure_before_the_first_read(bus, nut, ups):
    nut.fail = OSError("Connection refused")
    ups.poll()
    assert bus.state()["ups"]["status"] == "unknown"
    assert "Connection refused" in bus.state()["ups"]["error"]


def test_has_no_commands(ups):
    with pytest.raises(ValueError):
        ups.handle("anything", (), "x")


class Run:
    def __init__(self, returncode=0, stdout="", stderr="", timeout=False):
        self.result = subprocess.CompletedProcess([], returncode, stdout, stderr)
        self.timeout = timeout
        self.argv = None

    def __call__(self, argv, **kwargs):
        self.argv = argv
        if self.timeout:
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return self.result


def test_upsc_parses_key_value_lines(monkeypatch):
    run = Run(stdout="battery.charge: 100\nups.status: OL CHRG\nnoise\ndevice.mfr: American Power Conversion \n")
    monkeypatch.setattr(ups_module.subprocess, "run", run)
    assert upsc("apc@localhost") == {
        "battery.charge": "100",
        "ups.status": "OL CHRG",
        "device.mfr": "American Power Conversion",
    }
    assert run.argv == ["upsc", "apc@localhost"]


@pytest.mark.parametrize(
    ("run", "message"),
    [
        (Run(returncode=1, stderr="Error: Driver not connected\n"), "Driver not connected"),
        (Run(returncode=1), "upsc exited 1"),
        (Run(timeout=True), "timed out"),
    ],
)
def test_upsc_failures_raise_oserror(monkeypatch, run, message):
    monkeypatch.setattr(ups_module.subprocess, "run", run)
    with pytest.raises(OSError, match=message):
        upsc("apc@localhost")
