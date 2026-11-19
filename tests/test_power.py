import pytest

from tests.conftest import FakeScheduler
from warden.modules.power import ACTIONS, Power


class Runner:
    def __init__(self, code=0, output=""):
        self.code, self.output, self.calls = code, output, []

    def __call__(self, argv):
        self.calls.append(argv)
        return self.code, self.output


@pytest.fixture
def scheduler(clock):
    return FakeScheduler(clock)


@pytest.fixture
def runner():
    return Runner()


@pytest.fixture
def power(agent, scheduler, runner):
    module = Power(agent, 10, scheduler, runner)
    agent.add(module)
    return module


def fire(clock, scheduler, seconds=10):
    clock.advance(seconds)
    scheduler.run_due()


@pytest.mark.parametrize("action", sorted(ACTIONS))
def test_action_waits_for_the_grace_period(agent, bus, clock, scheduler, runner, power, action):
    agent.on_message(f"{action} console".encode())
    assert bus.events() == [{"event": "pending", "action": action, "source": "console", "in_s": 10}]
    assert bus.state()["power"]["pending"] == {"action": action, "source": "console", "in_s": 10}
    fire(clock, scheduler, 9)
    assert runner.calls == []
    fire(clock, scheduler, 1)
    assert runner.calls == [ACTIONS[action]]
    assert bus.events()[-1] == {"event": "executing", "action": action, "source": "console"}
    assert bus.statuses()[-1] == ("suspended" if action == "suspend" else "offline")
    assert power.state() == {"pending": None}


def test_pending_countdown(agent, clock, power):
    agent.on_message(b"reboot")
    clock.advance(3.4)
    assert power.state()["pending"]["in_s"] == 7
    clock.advance(30)
    assert power.state()["pending"]["in_s"] == 0


def test_bypasses_inhibitors():
    assert all("--check-inhibitors=no" in argv for argv in ACTIONS.values())


def test_cancel_prevents_execution(agent, bus, clock, scheduler, runner, power):
    agent.on_message(b"shutdown")
    agent.on_message(b"cancel phone")
    assert scheduler.timers[0].cancelled
    assert bus.events()[-1] == {"event": "cancelled", "action": "shutdown", "source": "phone"}
    assert bus.state()["power"] == {"pending": None}
    fire(clock, scheduler, 60)
    assert runner.calls == []


def test_cancel_with_nothing_pending(agent, bus, power):
    agent.on_message(b"cancel")
    assert bus.events() == [{"event": "rejected", "command": "cancel", "reason": "nothing pending"}]


def test_second_action_while_pending_is_rejected(agent, bus, clock, scheduler, runner, power):
    agent.on_message(b"reboot")
    agent.on_message(b"shutdown")
    assert bus.events()[-1] == {"event": "rejected", "command": "shutdown", "reason": "reboot already pending"}
    fire(clock, scheduler)
    assert runner.calls == [ACTIONS["reboot"]]


def test_stale_timer_cannot_fire_a_newer_request(agent, clock, scheduler, runner, power):
    agent.on_message(b"shutdown")
    agent.on_message(b"cancel")
    clock.advance(5)
    agent.on_message(b"reboot")
    stale = scheduler.timers[0]
    stale.cancelled = False
    stale.fn()
    assert runner.calls == []
    fire(clock, scheduler)
    assert runner.calls == [ACTIONS["reboot"]]


def test_failure_is_reported_and_status_restored(agent, bus, clock, scheduler, power, runner):
    runner.code, runner.output = 1, "Access denied\n"
    agent.on_message(b"reboot")
    fire(clock, scheduler)
    assert bus.events()[-1] == {"event": "failed", "action": "reboot", "error": "Access denied"}
    assert bus.statuses()[-2:] == ["offline", "online"]


def test_failure_without_output_reports_exit_code(agent, bus, clock, scheduler, power, runner):
    runner.code = 3
    agent.on_message(b"suspend")
    fire(clock, scheduler)
    assert bus.events()[-1]["error"] == "exit 3"


def test_failure_output_is_truncated(agent, bus, clock, scheduler, power, runner):
    runner.code, runner.output = 1, "x" * 1000
    agent.on_message(b"reboot")
    fire(clock, scheduler)
    assert len(bus.events()[-1]["error"]) == 200


def test_dry_run_runs_nothing_and_comes_back_online(agent, bus, clock, scheduler, runner):
    agent.add(Power(agent, 10, scheduler, runner, dry_run=True))
    agent.on_message(b"shutdown")
    fire(clock, scheduler)
    assert runner.calls == []
    assert bus.statuses()[-2:] == ["offline", "online"]


def test_zero_grace_executes_on_next_scheduler_run(agent, clock, scheduler, runner):
    agent.add(Power(agent, 0, scheduler, runner))
    agent.on_message(b"reboot")
    scheduler.run_due()
    assert runner.calls == [ACTIONS["reboot"]]


def test_can_act_again_after_execution(agent, bus, clock, scheduler, runner, power):
    agent.on_message(b"suspend")
    fire(clock, scheduler)
    agent.on_message(b"suspend")
    assert bus.events()[-1]["event"] == "pending"
