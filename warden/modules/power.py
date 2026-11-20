import itertools
from dataclasses import dataclass

ACTIONS = {
    "shutdown": ["systemctl", "poweroff", "--check-inhibitors=no"],
    "reboot": ["systemctl", "reboot", "--check-inhibitors=no"],
    "suspend": ["systemctl", "suspend", "--check-inhibitors=no"],
}


@dataclass
class Pending:
    action: str
    source: str
    due: float
    token: int
    handle: object


class Power:
    """Power actions with a cancellable grace period.

    ctx provides emit(event, **fields), set_status(status), changed() and mono().
    schedule(delay, fn) returns a handle with cancel(); run(argv) returns (returncode, output).
    """

    name = "power"
    commands = (*ACTIONS, "cancel")

    def __init__(self, ctx, grace_s, schedule, run, dry_run=False):
        self._ctx = ctx
        self._grace_s = grace_s
        self._schedule = schedule
        self._run = run
        self._dry_run = dry_run
        self._pending = None
        self._tokens = itertools.count(1)

    def handle(self, command, args, source):
        if command == "cancel":
            self._cancel(source)
        else:
            self._request(command, source)

    def state(self):
        p = self._pending
        if p is None:
            return {"pending": None}
        return {"pending": {"action": p.action, "source": p.source, "in_s": max(0, round(p.due - self._ctx.mono()))}}

    def _request(self, action, source):
        if self._pending is not None:
            self._ctx.emit("rejected", command=action, reason=f"{self._pending.action} already pending")
            return
        token = next(self._tokens)
        handle = self._schedule(self._grace_s, lambda: self._execute(token))
        self._pending = Pending(action, source, self._ctx.mono() + self._grace_s, token, handle)
        self._ctx.emit("pending", action=action, source=source, in_s=self._grace_s)
        self._ctx.changed()

    def _cancel(self, source):
        p = self._pending
        if p is None:
            self._ctx.emit("rejected", command="cancel", reason="nothing pending")
            return
        p.handle.cancel()
        self._pending = None
        self._ctx.emit("cancelled", action=p.action, source=source)
        self._ctx.changed()

    def _execute(self, token):
        p = self._pending
        if p is None or p.token != token:
            return
        self._pending = None
        self._ctx.emit("executing", action=p.action, source=p.source)
        self._ctx.set_status("suspended" if p.action == "suspend" else "offline")
        code, output = (0, "") if self._dry_run else self._run(ACTIONS[p.action])
        if code != 0:
            self._ctx.emit("failed", action=p.action, error=output.strip()[:200] or f"exit {code}")
        if code != 0 or self._dry_run:
            self._ctx.set_status("online")
        self._ctx.changed()
