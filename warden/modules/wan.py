import ipaddress
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType


@dataclass(frozen=True)
class Link:
    name: str
    iface: str
    device: str | None = None
    cap_bytes: int | None = None


def vnstat_usage(devices):
    """Bytes in the current vnstat month (the billing cycle, with MonthRotate) per device, both directions."""
    out = subprocess.run(["vnstat", "--json", "m", "1"], capture_output=True, text=True, check=False, timeout=10)
    if out.returncode != 0:
        raise OSError(f"vnstat exited {out.returncode}: {out.stderr.strip()[:200]}")
    try:
        data = json.loads(out.stdout)
    except ValueError as e:
        raise OSError(f"vnstat printed invalid JSON: {e}") from e
    usage = {}
    for iface in data.get("interfaces", []):
        months = iface.get("traffic", {}).get("month") or []
        if iface.get("name") in devices and months:
            usage[iface["name"]] = int(months[-1].get("rx", 0)) + int(months[-1].get("tx", 0))
    return usage


def ubus_set(ip, isp):
    """Asks the router's wanpref helper (root, via a ubus ACL for this user) to route ip through isp or auto."""
    args = json.dumps({"ip": ip, "isp": isp})
    try:
        out = subprocess.run(
            ["ubus", "call", "wanpref", "set", args], capture_output=True, text=True, check=False, timeout=10
        )
    except subprocess.TimeoutExpired as e:
        raise OSError("ubus call wanpref timed out") from e
    if out.returncode != 0:
        raise OSError((out.stderr or out.stdout).strip()[:200] or f"ubus exited {out.returncode}")


class Wan:
    """Internet uplinks as tracked by mwan3, in failover order; the active one is the first online.

    Status comes from mwan3track's per-interface STATUS file; a missing file means tracking is stopped, as when the
    link is down. With a device and usage source, each link reports bytes used this cycle (and its cap).

    Per device: `preferences` maps an address to the ISP it tries first; `isp <ip> <isp|auto>` sets a runtime
    override through `set_override`, which the router keeps in `override_dir` (one file per address) and drops
    while that ISP is offline. route(ip) says which ISP a device's traffic uses.
    """

    name = "wan"
    commands = ("isp",)
    args = MappingProxyType({"isp": 2})
    USAGE_EVERY_S = 60

    def __init__(
        self,
        ctx,
        links,
        preferences=None,
        status_dir=Path("/var/run/mwan3track"),
        override_dir=Path("/tmp/wanpref"),
        usage=vnstat_usage,
        set_override=ubus_set,
    ):
        self._ctx = ctx
        self._links = tuple(links)
        self._names = [link.name for link in self._links]
        self._preferences = dict(preferences or {})
        self._dir = Path(status_dir)
        self._override_dir = Path(override_dir)
        self._usage = usage
        self._set_override = set_override
        self._status = None
        self._overrides = {}
        self._used = {}
        self._usage_at = None

    def handle(self, command, args, source):
        ip, isp = args
        if not _is_ipv4(ip):
            self._ctx.emit("rejected", command=command, reason="invalid ip", ip=ip)
            return
        if isp != "auto" and isp not in self._names:
            self._ctx.emit("rejected", command=command, reason="unknown isp", isp=isp)
            return
        try:
            self._set_override(ip, isp)
        except OSError as e:
            self._ctx.emit("isp_failed", ip=ip, isp=isp, source=source, error=str(e)[:200])
            return
        self._ctx.emit("isp_set", ip=ip, isp=isp, source=source)
        try:
            self._overrides = self._read_overrides()
        except PermissionError as e:
            self._ctx.emit("error", reason=f"cannot read overrides: {e}"[:200])
        self._ctx.changed()

    def poll_every_s(self):
        return 2

    def poll(self):
        status, overrides = self._read_status(), self._read_overrides()
        used = None
        now = self._ctx.mono()
        devices = {link.device for link in self._links if link.device}
        if devices and (self._usage_at is None or now - self._usage_at >= self.USAGE_EVERY_S):
            self._usage_at = now
            try:
                usage = self._usage(devices)
                used = {link.name: usage[link.device] for link in self._links if link.device in usage}
            except (OSError, subprocess.TimeoutExpired):
                used = None
        self._ctx.locked(lambda: self._apply(status, overrides, used))()

    def _read_status(self):
        out = {}
        for link in self._links:
            try:
                out[link.name] = (self._dir / link.iface / "STATUS").read_text().strip() or "unknown"
            except OSError:
                out[link.name] = "down"
        return out

    def _read_overrides(self):
        """No directory means no overrides; one this user cannot read is a setup fault and propagates, so it is
        logged instead of every device quietly reading as not overridden."""
        out = {}
        try:
            entries = list(self._override_dir.iterdir())
        except FileNotFoundError:
            return out
        for path in entries:
            try:
                isp = path.read_text().strip()
            except FileNotFoundError:
                continue
            if isp in self._names and _is_ipv4(path.name):
                out[path.name] = isp
        return out

    def _apply(self, status, overrides, used):
        if self._status is not None:
            for name, now in status.items():
                was_online, is_online = self._status.get(name) == "online", now == "online"
                if was_online != is_online:
                    self._ctx.emit("wan_up" if is_online else "wan_down", wan=name, status=now)
        changed = status != self._status or overrides != self._overrides or (used is not None and used != self._used)
        self._status, self._overrides = status, overrides
        if used is not None:
            self._used = used
        if changed:
            self._ctx.changed()

    def _online(self):
        status = self._status if self._status is not None else self._read_status()
        return [n for n in self._names if status.get(n) == "online"]

    def route(self, ip):
        online = self._online()
        override = self._overrides.get(ip)
        preferred = self._preferences.get(ip)
        order = ([preferred] if preferred else []) + [n for n in self._names if n != preferred]
        via = override if override in online else next((n for n in order if n in online), None)
        return {"via": via, "override": override}

    def state(self):
        status = self._status if self._status is not None else self._read_status()
        online = self._online()
        links = []
        for link in self._links:
            entry = {"name": link.name, "status": status[link.name]}
            if link.name in self._used:
                entry["used_bytes"] = self._used[link.name]
            if link.cap_bytes is not None:
                entry["cap_bytes"] = link.cap_bytes
            links.append(entry)
        return {"active": online[0] if online else None, "links": links}


def _is_ipv4(text):
    try:
        return str(ipaddress.IPv4Address(text)) == text
    except ValueError:
        return False
