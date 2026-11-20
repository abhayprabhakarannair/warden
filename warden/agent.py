import json
import re
import threading
import time

WORD = re.compile(r"^[a-z0-9_-]{1,31}$")
MAX_PAYLOAD = 96
RESUME_GAP_S = 5


class Clock:
    wall = staticmethod(time.time)
    mono = staticmethod(time.monotonic)


class Agent:
    """Publishes meta/status/state/event under topic and routes topic/cmd to modules.

    bus provides publish(topic, payload, retain) and subscribe(topic). Every entry point takes the same lock,
    so module code never runs concurrently.

    A command is `<command> [args…] [source]`; a module's `args` maps each of its commands to how many arguments
    it takes (default none), and its optional meta() adds fields to meta.
    """

    def __init__(self, bus, topic, version, clock=Clock, state_every_s=30):
        self._bus = bus
        self._topic = topic
        self._version = version
        self._clock = clock
        self._state_every_s = state_every_s
        self._modules = []
        self._commands = {}
        self._lock = threading.RLock()
        self._dirty = True
        self._last_state = None
        self._last_wall = clock.wall()
        self._last_mono = clock.mono()
        self._status = "online"

    # context offered to modules
    def emit(self, event, **fields):
        self._bus.publish(f"{self._topic}/event", json.dumps({"event": event, **fields}, separators=(",", ":")))

    def set_status(self, status):
        self._status = status
        self._bus.publish(f"{self._topic}/status", status, retain=True)

    def changed(self):
        self._dirty = True

    def wall(self):
        return self._clock.wall()

    def mono(self):
        return self._clock.mono()

    def locked(self, fn):
        def run():
            with self._lock:
                fn()
                self._flush()

        return run

    @property
    def modules(self):
        return tuple(self._modules)

    def add(self, module):
        clash = set(module.commands) & (set(self._commands) | {"refresh"})
        if clash:
            raise ValueError(f"{module.name} redefines {', '.join(sorted(clash))}")
        self._modules.append(module)
        for command in module.commands:
            self._commands[command] = module

    # entry points
    def on_connect(self):
        with self._lock:
            self._bus.subscribe(f"{self._topic}/cmd")
            self.set_status("online")
            self._publish_meta()
            self._publish_state()

    def on_message(self, payload):
        with self._lock:
            self._dispatch(payload)
            self._flush()

    def tick(self):
        with self._lock:
            wall, mono = self._clock.wall(), self._clock.mono()
            slept = (wall - self._last_wall) - (mono - self._last_mono)
            self._last_wall, self._last_mono = wall, mono
            if slept > RESUME_GAP_S:
                self.set_status("online")
                self.emit("resumed", slept_s=int(slept))
                self._dirty = True
            due = self._last_state is None or mono - self._last_state >= self._state_every_s
            if due or self._dirty:
                self._publish_state()

    def stop(self):
        with self._lock:
            if self._status == "online":
                self.set_status("offline")

    # internals
    def _dispatch(self, payload):
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            self.emit("rejected", reason="malformed")
            return
        words = text.strip().split(" ") if len(payload) <= MAX_PAYLOAD else []
        words = [w for w in words if w]
        if not words or not all(WORD.match(w) for w in words):
            self.emit("rejected", reason="malformed")
            return
        command, rest = words[0], words[1:]
        module = self._commands.get(command)
        if module is None and command != "refresh":
            self.emit("rejected", command=command, reason="unknown command")
            return
        arity = getattr(module, "args", {}).get(command, 0)
        if len(rest) not in (arity, arity + 1):
            self.emit("rejected", reason="malformed")
            return
        args, source = tuple(rest[:arity]), rest[arity] if len(rest) > arity else "mqtt"
        if module is None:
            self._publish_meta()
            self._dirty = True
            return
        module.handle(command, args, source)

    def _flush(self):
        if self._dirty:
            self._publish_state()

    def _publish_meta(self):
        meta = {
            "kind": "warden",
            "version": self._version,
            "modules": [m.name for m in self._modules],
            "commands": [*self._commands, "refresh"],
        }
        for m in self._modules:
            if hasattr(m, "meta"):
                meta |= m.meta()
        self._bus.publish(f"{self._topic}/meta", json.dumps(meta, separators=(",", ":")), retain=True)

    def _publish_state(self):
        state = {m.name: m.state() for m in self._modules}
        self._bus.publish(f"{self._topic}/state", json.dumps(state, separators=(",", ":")), retain=True)
        self._dirty = False
        self._last_state = self._clock.mono()
