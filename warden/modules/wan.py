from pathlib import Path


class Wan:
    """Internet uplinks as tracked by mwan3, in failover order; the active one is the first online.

    wans maps a display name to its mwan3 interface. Status comes from mwan3track's per-interface STATUS file
    (online, offline, connecting, disconnecting); a missing file means tracking is stopped, as when the link is down.
    """

    name = "wan"
    commands = ()

    def __init__(self, ctx, wans, status_dir=Path("/var/run/mwan3track")):
        self._ctx = ctx
        self._wans = dict(wans)
        self._dir = Path(status_dir)
        self._last = None

    def handle(self, command, args, source):
        raise ValueError(f"wan has no command {command!r}")

    def poll_every_s(self):
        return 2

    def poll(self):
        self._ctx.locked(self._apply)()

    def _read(self):
        out = {}
        for name, iface in self._wans.items():
            try:
                out[name] = (self._dir / iface / "STATUS").read_text().strip() or "unknown"
            except OSError:
                out[name] = "down"
        return out

    def _apply(self):
        current = self._read()
        if self._last is not None:
            for name, status in current.items():
                was_online, is_online = self._last.get(name) == "online", status == "online"
                if was_online != is_online:
                    self._ctx.emit("wan_up" if is_online else "wan_down", wan=name, status=status)
        if current != self._last:
            self._last = current
            self._ctx.changed()

    def state(self):
        current = self._last if self._last is not None else self._read()
        active = next((n for n, s in current.items() if s == "online"), None)
        return {"active": active, "links": [{"name": n, "status": s} for n, s in current.items()]}
