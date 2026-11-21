import math
import subprocess


def upsc(ups):
    """Variables of a NUT UPS as printed by `upsc <name@host>`: one `key: value` per line."""
    try:
        out = subprocess.run(["upsc", ups], capture_output=True, text=True, check=False, timeout=10)
    except subprocess.TimeoutExpired as e:
        raise OSError("upsc timed out") from e
    if out.returncode != 0:
        raise OSError((out.stderr or out.stdout).strip()[:200] or f"upsc exited {out.returncode}")
    values = {}
    for line in out.stdout.splitlines():
        key, sep, value = line.partition(": ")
        if sep:
            values[key.strip()] = value.strip()
    return values


class Ups:
    """A UPS read through NUT's upsd. `status` comes from the `ups.status` flags: LB beats OB beats OL.

    State publishes when status, flags or a rounded reading (charge, load, power, runtime to the minute) changes;
    other readings ride along with the next publish. While upsc fails, the last values stay and `error` says why;
    `ups_lost` and `ups_found` mark the change.
    """

    name = "ups"
    commands = ()

    def __init__(self, ctx, ups, read=upsc):
        self._ctx = ctx
        self._ups = ups
        self._read = read
        self._view = None
        self._error = None

    def handle(self, command, args, source):
        raise ValueError(f"ups has no command {command!r}")

    def poll_every_s(self):
        return 5

    def poll(self):
        try:
            view, error = _view(self._read(self._ups)), None
        except (OSError, subprocess.TimeoutExpired) as e:
            view, error = None, f"cannot read {self._ups}: {e}"[:200]
        self._ctx.locked(lambda: self._apply(view, error))()

    def _apply(self, view, error):
        if error is not None:
            if self._error is None:
                self._ctx.emit("ups_lost", error=error)
            changed = error != self._error
            self._error = error
            if changed:
                self._ctx.changed()
            return
        if self._error is not None:
            self._ctx.emit("ups_found")
        changed = self._error is not None or self._view is None or _key(view) != _key(self._view)
        self._emit_transition(self._view["status"] if self._view else None, view)
        self._view, self._error = view, None
        if changed:
            self._ctx.changed()

    def _emit_transition(self, was, view):
        now = view["status"]
        if was is None or was == now or now == "unknown":
            return
        fields = {k: view[k] for k in ("charge_pct", "runtime_s") if k in view}
        if now == "on_battery":
            self._ctx.emit("on_battery", **fields)
        elif now == "low_battery":
            self._ctx.emit("low_battery", **fields)
        elif now == "online" and was in ("on_battery", "low_battery"):
            self._ctx.emit("on_line", **fields)

    def state(self):
        out = dict(self._view) if self._view else {"status": "unknown"}
        if self._error is not None:
            out["error"] = self._error
        return out


def _view(values):
    flags = values.get("ups.status", "").split()
    status = (
        "low_battery" if "LB" in flags else "on_battery" if "OB" in flags else "online" if "OL" in flags else "unknown"
    )
    out = {"status": status, "flags": flags}
    charge, load, runtime = _num(values, "battery.charge"), _num(values, "ups.load"), _num(values, "battery.runtime")
    nominal, voltage = _num(values, "ups.realpower.nominal"), _num(values, "input.voltage")
    if charge is not None:
        out["charge_pct"] = round(charge)
    if runtime is not None:
        out["runtime_s"] = int(runtime)
    if load is not None:
        out["load_pct"] = round(load)
        if nominal:
            out["power_w"] = round(load * nominal / 100)
    if voltage is not None:
        out["input_v"] = round(voltage)
    model = values.get("device.model") or values.get("ups.model")
    if model:
        out["model"] = model
    return out


def _key(view):
    """What decides a publish: everything but input voltage, with runtime to the minute."""
    return {k: (v // 60 if k == "runtime_s" else v) for k, v in view.items() if k != "input_v"}


def _num(values, key):
    try:
        value = float(values[key])
    except (KeyError, ValueError):
        return None
    return value if math.isfinite(value) and value >= 0 else None
