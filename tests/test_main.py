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
