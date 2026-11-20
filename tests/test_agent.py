import json

import pytest


class Echo:
    name = "echo"
    commands = ("ping",)

    def __init__(self, ctx):
        self.ctx = ctx
        self.calls = []

    def handle(self, command, args, source):
        self.calls.append((command, *args, source))
        self.ctx.changed()

    def state(self):
        return {"calls": len(self.calls)}


@pytest.fixture
def echo(agent):
    module = Echo(agent)
    agent.add(module)
    return module


def test_connect_announces_everything(agent, bus, echo):
    agent.on_connect()
    assert bus.subscriptions == ["home/devil/cmd"]
    assert ("home/devil/status", "online", True) in bus.sent
    meta = json.loads(bus.last("meta"))
    assert meta == {"kind": "warden", "version": "9.9.9", "modules": ["echo"], "commands": ["ping", "refresh"]}
    assert all(retain for topic, _, retain in bus.sent if not topic.endswith("/event"))
    assert bus.state() == {"echo": {"calls": 0}}


def test_reconnect_reannounces_online(agent, bus, echo):
    agent.set_status("suspended")
    agent.on_connect()
    assert bus.statuses()[-1] == "online"


def test_routes_commands_with_source(agent, bus, echo):
    agent.on_message(b"ping console")
    agent.on_message(b"  ping  ")
    assert echo.calls == [("ping", "console"), ("ping", "mqtt")]
    assert bus.state() == {"echo": {"calls": 2}}


@pytest.mark.parametrize(
    "payload",
    [b"", b"   ", b"PING", b"ping a b", b"ping a/b", b"\xff\xfe", b"ping " + b"a" * 32, b"ping\tconsole", b"x" * 97],
)
def test_rejects_malformed(agent, bus, echo, payload):
    agent.on_message(payload)
    assert bus.events() == [{"event": "rejected", "reason": "malformed"}]
    assert echo.calls == []


def test_rejects_unknown_command(agent, bus, echo):
    agent.on_message(b"format c")
    assert bus.events() == [{"event": "rejected", "command": "format", "reason": "unknown command"}]


def test_refresh_republishes_meta_and_state(agent, bus, echo):
    agent.on_message(b"refresh")
    assert bus.last("meta") is not None
    assert bus.state() == {"echo": {"calls": 0}}


def test_command_clash_is_refused(agent, echo):
    with pytest.raises(ValueError, match="ping"):
        agent.add(Echo(agent))

    class Refresh(Echo):
        commands = ("refresh",)

    with pytest.raises(ValueError, match="refresh"):
        agent.add(Refresh(agent))


def test_state_is_periodic(agent, bus, clock, echo):
    agent.tick()
    bus.clear()
    clock.advance(29)
    agent.tick()
    assert bus.last("state") is None
    clock.advance(1)
    agent.tick()
    assert bus.last("state") is not None


def test_changed_forces_state_on_next_tick(agent, bus, clock, echo):
    agent.tick()
    bus.clear()
    agent.changed()
    agent.tick()
    assert bus.last("state") is not None


def test_resume_is_detected_from_clock_divergence(agent, bus, clock, echo):
    agent.set_status("suspended")
    bus.clear()
    clock.advance(1, asleep=600)
    agent.tick()
    assert bus.statuses() == ["online"]
    assert bus.events() == [{"event": "resumed", "slept_s": 600}]


def test_small_clock_jitter_is_not_a_resume(agent, bus, clock, echo):
    clock.advance(1, asleep=4)
    agent.tick()
    assert bus.events() == []


def test_stop_reports_offline_only_when_online(agent, bus):
    agent.set_status("online")
    agent.stop()
    assert bus.statuses()[-1] == "offline"
    bus.clear()
    agent.set_status("suspended")
    bus.clear()
    agent.stop()
    assert bus.statuses() == []


def test_locked_runs_and_flushes_state(agent, bus, echo):
    agent.locked(lambda: (echo.calls.append(("timer", "x")), agent.changed()))()
    assert bus.state() == {"echo": {"calls": 1}}


def test_events_are_compact_json(agent, bus):
    agent.emit("x", a=1, b="two")
    assert bus.sent[-1] == ("home/devil/event", '{"event":"x","a":1,"b":"two"}', False)


class Pair(Echo):
    name = "pair"
    commands = ("wake",)
    args = {"wake": 1}  # noqa: RUF012

    def meta(self):
        return {"wakes": ["devil"]}


@pytest.fixture
def pair(agent):
    module = Pair(agent)
    agent.add(module)
    return module


def test_commands_take_declared_arguments_then_source(agent, bus, pair):
    agent.on_message(b"wake devil console")
    agent.on_message(b"wake devil")
    assert pair.calls == [("wake", "devil", "console"), ("wake", "devil", "mqtt")]


@pytest.mark.parametrize("payload", [b"wake", b"wake devil console extra", b"wake Devil"])
def test_wrong_argument_count_is_malformed(agent, bus, pair, payload):
    agent.on_message(payload)
    assert bus.events() == [{"event": "rejected", "reason": "malformed"}]
    assert pair.calls == []


def test_modules_add_meta_fields(agent, bus, pair):
    agent.on_connect()
    assert json.loads(bus.last("meta"))["wakes"] == ["devil"]


def test_refresh_accepts_a_source_only(agent, bus, echo):
    agent.on_message(b"refresh console")
    assert bus.events() == []
    agent.on_message(b"refresh a b")
    assert bus.events() == [{"event": "rejected", "reason": "malformed"}]
