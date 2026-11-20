import json

from tests.conftest import FakeBus
from warden.__main__ import build, schedule
from warden.config import load


def config(tmp_path, **extra):
    (tmp_path / "pw").write_text("x")
    env = {"WARDEN_MQTT_HOST": "h", "WARDEN_MQTT_USER": "devil", "WARDEN_MQTT_PASSWORD_FILE": str(tmp_path / "pw")}
    return load(env | extra)


def test_build_wires_configured_modules(tmp_path):
    bus = FakeBus()
    agent = build(config(tmp_path, WARDEN_MODULES="system,power"), bus)
    agent.on_connect()
    meta = json.loads(bus.last("meta"))
    assert meta["modules"] == ["system", "power"]
    assert meta["commands"] == ["shutdown", "reboot", "suspend", "cancel", "refresh"]
    assert set(bus.state()) == {"system", "power"}


def test_real_scheduler_runs_under_the_agent_lock(tmp_path):
    bus = FakeBus()
    agent = build(config(tmp_path, WARDEN_MODULES="system"), bus)
    timer = schedule(agent)(0, agent.changed)
    timer.join(2)
    assert bus.last("state") is not None


def test_build_wires_router_modules(tmp_path):
    bus = FakeBus()
    c = config(
        tmp_path,
        WARDEN_MODULES="system,wake,wan,clients",
        WARDEN_WAKE_TARGETS="devil=02:00:00:00:00:01@192.0.2.4",
        WARDEN_WAKE_BROADCAST="192.0.2.255",
        WARDEN_WANS="bsnl=wan_bsnl",
    )
    agent = build(c, bus)
    agent.on_connect()
    meta = json.loads(bus.last("meta"))
    assert meta["commands"] == ["wake", "refresh"]
    assert meta["wakes"] == ["devil"]
    assert [m.name for m in agent.modules if hasattr(m, "poll")] == ["wake", "wan"]


def test_poll_forever_survives_any_error(capsys):
    import threading

    from warden.__main__ import poll_forever

    calls = []
    stop = threading.Event()

    class Flaky:
        name = "flaky"

        def poll_every_s(self):
            return 0.01

        def poll(self):
            calls.append(1)
            if len(calls) == 3:
                stop.set()
            raise (OSError("ping refused") if len(calls) == 1 else ValueError("bug"))

    poll_forever(Flaky(), stop)
    assert len(calls) == 3
    err = capsys.readouterr().err
    assert "flaky poll failed" in err
    assert "ValueError: bug" in err
