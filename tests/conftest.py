import json

import pytest

from warden.agent import Agent


class FakeBus:
    def __init__(self):
        self.sent = []
        self.subscriptions = []

    def publish(self, topic, payload, retain=False):
        self.sent.append((topic, payload, retain))

    def subscribe(self, topic):
        self.subscriptions.append(topic)

    def last(self, leaf):
        for topic, payload, _ in reversed(self.sent):
            if topic.endswith("/" + leaf):
                return payload
        return None

    def events(self):
        return [json.loads(p) for t, p, _ in self.sent if t.endswith("/event")]

    def statuses(self):
        return [p for t, p, _ in self.sent if t.endswith("/status")]

    def state(self):
        return json.loads(self.last("state"))

    def clear(self):
        self.sent.clear()


class FakeClock:
    def __init__(self, wall=1_800_000_000.0, mono=1000.0):
        self._wall, self._mono = wall, mono

    def wall(self):
        return self._wall

    def mono(self):
        return self._mono

    def advance(self, seconds, asleep=0.0):
        self._mono += seconds
        self._wall += seconds + asleep


class FakeScheduler:
    def __init__(self, clock):
        self._clock = clock
        self.timers = []

    def __call__(self, delay, fn):
        timer = FakeTimer(self._clock.mono() + delay, fn)
        self.timers.append(timer)
        return timer

    def run_due(self):
        for timer in list(self.timers):
            if not timer.cancelled and not timer.fired and timer.due <= self._clock.mono():
                timer.fired = True
                timer.fn()


class FakeTimer:
    def __init__(self, due, fn):
        self.due, self.fn = due, fn
        self.cancelled = self.fired = False

    def cancel(self):
        self.cancelled = True


@pytest.fixture
def bus():
    return FakeBus()


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def agent(bus, clock):
    return Agent(bus, "home/devil", "9.9.9", clock=clock, state_every_s=30)
