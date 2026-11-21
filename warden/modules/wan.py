import ipaddress
import json
import statistics
import subprocess
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from warden.modules.clients import uci_hosts


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


def _wanpref(method, args):
    """Calls the router's wanpref helper (root, via a ubus ACL for this user)."""
    try:
        out = subprocess.run(
            ["ubus", "call", "wanpref", method, json.dumps(args)],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except subprocess.TimeoutExpired as e:
        raise OSError("ubus call wanpref timed out") from e
    if out.returncode != 0:
        raise OSError((out.stderr or out.stdout).strip()[:200] or f"ubus exited {out.returncode}")


def ubus_set(ip, isp):
    """Routes ip through isp, or back to automatic."""
    _wanpref("set", {"ip": ip, "isp": isp})


def ubus_degraded(isps):
    """Tells the router which ISPs are degraded, so it drops overrides to them."""
    _wanpref("degraded", {"isps": list(isps)})


class Wan:
    """Internet uplinks as tracked by mwan3, in failover order; the active one is the first online.

    Status comes from mwan3track's per-interface STATUS file; a missing file means tracking is stopped, as when the
    link is down. With a device and usage source, each link reports bytes used this cycle (and its cap); if usage
    cannot be read, the last values stay and `usage_error` says why until a read succeeds. A link with a device also
    reports `rx_bps` and `tx_bps`, averaged over RATE_EVERY_S from the interface counters; a missing or reset counter
    drops them for that window. A rate change alone publishes only when it is large (RATE_STEP_BPS or RATE_STEP).

    Quality: mwan3track rewrites TRACK_<ip> (`up`, `down` or `skipped`) and LATENCY_<ip> (ms) every round. Each
    TRACK file whose mtime advanced is one sample, kept for WINDOW_S while the link is online. With MIN_SAMPLES, a
    link reports `loss_pct` and the median `latency_ms`, and becomes `degraded` at DEGRADED_LOSS_PCT or
    DEGRADED_LATENCY_MS, recovering only at RECOVERED_LOSS_PCT and RECOVERED_LATENCY_MS. The degraded set goes to the
    router through `set_degraded` whenever it changes and every NOTIFY_EVERY_S, so the router drops overrides to
    those links; route() treats them the same way. Loss or latency alone publishes only when it moves by
    LOSS_STEP_PCT or LATENCY_STEP.

    Per device: `preferences` maps an address to the ISP it tries first; `isp <ip> <isp|auto>` sets a runtime
    override through `set_override`, which the router keeps in `override_dir` (one file per address) and drops
    while that ISP is offline. Only devices with a DHCP reservation (from `reserved`) can be switched: another device
    could later lease an unreserved address and inherit its override. route(ip, reserved) says which ISP a device's
    traffic uses.
    """

    name = "wan"
    commands = ("isp",)
    args = MappingProxyType({"isp": 2})
    USAGE_EVERY_S = 60
    RATE_EVERY_S = 5
    RATE_STEP_BPS = 100_000
    RATE_STEP = 0.1
    WINDOW_S = 60
    MIN_SAMPLES = 20
    DEGRADED_LOSS_PCT = 5
    DEGRADED_LATENCY_MS = 300
    RECOVERED_LOSS_PCT = 2
    RECOVERED_LATENCY_MS = 200
    LOSS_STEP_PCT = 1
    LATENCY_STEP = 0.2
    NOTIFY_EVERY_S = 60
    NOTIFY_RETRY_S = 10

    def __init__(
        self,
        ctx,
        links,
        preferences=None,
        status_dir=Path("/var/run/mwan3track"),
        override_dir=Path("/tmp/wanpref"),
        usage=vnstat_usage,
        set_override=ubus_set,
        reserved=lambda: uci_hosts().keys(),
        net_dir=Path("/sys/class/net"),
        set_degraded=ubus_degraded,
    ):
        self._ctx = ctx
        self._links = tuple(links)
        self._names = [link.name for link in self._links]
        self._preferences = dict(preferences or {})
        self._dir = Path(status_dir)
        self._override_dir = Path(override_dir)
        self._usage = usage
        self._set_override = set_override
        self._reserved = reserved
        self._net_dir = Path(net_dir)
        self._sample = None
        self._rates = {}
        self._rates_marked = {}
        self._status = None
        self._overrides = {}
        self._used = {}
        self._usage_at = None
        self._usage_error = None
        self._overrides_error = None
        self._set_degraded = set_degraded
        self._seen = {}
        self._windows = {link.name: deque() for link in self._links}
        self._quality = {}
        self._quality_marked = {}
        self._degraded = set()
        self._notified = None
        self._notify_at = None
        self._notify_error = None

    def handle(self, command, args, source):
        ip, isp = args
        if not _is_ipv4(ip):
            self._ctx.emit("rejected", command=command, reason="invalid ip", ip=ip)
            return
        if isp != "auto" and isp not in self._names:
            self._ctx.emit("rejected", command=command, reason="unknown isp", isp=isp)
            return
        if isp != "auto" and ip not in self._reserved():
            self._ctx.emit("rejected", command=command, reason="not a reserved address", ip=ip)
            return
        try:
            self._set_override(ip, isp)
        except OSError as e:
            self._ctx.emit("isp_failed", ip=ip, isp=isp, source=source, error=str(e)[:200])
            return
        self._ctx.emit("isp_set", ip=ip, isp=isp, source=source)
        overrides = self._overrides_or_last()
        if self._overrides_error is not None:
            # The router accepted the change; reflect it rather than the stale copy until the files are readable.
            overrides = {k: v for k, v in overrides.items() if k != ip}
            if isp != "auto":
                overrides[ip] = isp
        self._overrides = overrides
        self._ctx.changed()

    def poll_every_s(self):
        return 1

    def poll(self):
        status = self._read_status()
        overrides = self._overrides_or_last()
        used = usage_error = None
        now = self._ctx.mono()
        devices = {link.device for link in self._links if link.device}
        if devices and (self._usage_at is None or now - self._usage_at >= self.USAGE_EVERY_S):
            self._usage_at = now
            try:
                usage = self._usage(devices)
                used = {link.name: usage[link.device] for link in self._links if link.device in usage}
            except (OSError, subprocess.TimeoutExpired) as e:
                usage_error = f"cannot read usage: {e}"[:200]
        rates = self._measure(now)
        samples = self._read_samples()
        self._ctx.locked(lambda: self._apply(status, overrides, used, usage_error, rates, samples, now))()
        self._notify(now)

    def _read_samples(self):
        """New mwan3track results per link since the last poll: (ok, latency ms or None)."""
        out = {}
        for link in self._links:
            found = []
            try:
                tracks = sorted(self._dir.joinpath(link.iface).glob("TRACK_*"))
            except OSError:
                tracks = []
            for path in tracks:
                try:
                    mtime = path.stat().st_mtime_ns
                    result = path.read_text().strip()
                except OSError:
                    continue
                key = (link.name, path.name)
                if self._seen.get(key) == mtime:
                    continue
                self._seen[key] = mtime
                if result == "up":
                    found.append((True, self._latency(path.with_name("LATENCY_" + path.name[len("TRACK_") :]))))
                elif result == "down":
                    found.append((False, None))
            out[link.name] = found
        return out

    @staticmethod
    def _latency(path):
        try:
            ms = int(path.read_text().strip())
        except (OSError, ValueError):
            return None
        return ms if 0 <= ms < 999999 else None

    def _notify(self, now):
        """Sends the degraded set when it changed or NOTIFY_EVERY_S passed; a failure is retried after
        NOTIFY_RETRY_S and reported once per distinct reason."""
        degraded = sorted(self._degraded)
        if self._notify_at is not None:
            if self._notify_error is not None:
                if now - self._notify_at < self.NOTIFY_RETRY_S:
                    return
            elif degraded == self._notified and now - self._notify_at < self.NOTIFY_EVERY_S:
                return
        self._notify_at = now
        try:
            self._set_degraded(degraded)
        except (OSError, subprocess.TimeoutExpired) as e:
            reason = f"cannot report degraded links: {e}"[:200]
            if reason != self._notify_error:
                self._ctx.emit("error", reason=reason)
            self._notify_error = reason
            return
        self._notified, self._notify_error = degraded, None

    def _measure(self, now):
        """Rates in bit/s per link since the last sample, once RATE_EVERY_S has passed; None until then."""
        if self._sample is not None and now - self._sample[0] < self.RATE_EVERY_S:
            return None
        counters = {link.name: c for link in self._links if link.device and (c := self._counters(link.device))}
        previous, self._sample = self._sample, (now, counters)
        if previous is None:
            return None
        elapsed = now - previous[0]
        rates = {}
        for name, (rx, tx) in counters.items():
            before = previous[1].get(name)
            if before and rx >= before[0] and tx >= before[1]:
                rates[name] = (round((rx - before[0]) * 8 / elapsed), round((tx - before[1]) * 8 / elapsed))
        return rates

    def _counters(self, device):
        stats = self._net_dir / device / "statistics"
        try:
            return int((stats / "rx_bytes").read_text()), int((stats / "tx_bytes").read_text())
        except (OSError, ValueError):
            return None

    def _read_status(self):
        out = {}
        for link in self._links:
            try:
                out[link.name] = (self._dir / link.iface / "STATUS").read_text().strip() or "unknown"
            except OSError:
                out[link.name] = "down"
        return out

    def _overrides_or_last(self):
        """Overrides are optional: if they cannot be read, keep the last known ones so links, events and usage keep
        flowing, and report the failure once (again only after it has cleared)."""
        try:
            overrides = self._read_overrides()
        except OSError as e:
            reason = f"cannot read overrides: {e}"[:200]
            if reason != self._overrides_error:
                self._overrides_error = reason
                self._ctx.emit("error", reason=reason)
            return self._overrides
        self._overrides_error = None
        return overrides

    def _read_overrides(self):
        """No directory means no overrides; any other error is raised."""
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

    def _apply(self, status, overrides, used, usage_error, rates, samples, at):
        if self._status is not None:
            for name, now in status.items():
                was_online, is_online = self._status.get(name) == "online", now == "online"
                if was_online != is_online:
                    self._ctx.emit("wan_up" if is_online else "wan_down", wan=name, status=now)
        changed = status != self._status or overrides != self._overrides or (used is not None and used != self._used)
        self._status, self._overrides = status, overrides
        if used is not None:
            self._used = used
            changed |= self._usage_error is not None
            self._usage_error = None
        elif usage_error is not None and usage_error != self._usage_error:
            self._usage_error = usage_error
            self._ctx.emit("error", reason=usage_error)
            changed = True
        if rates is not None:
            self._rates = rates
            if _moved(self._rates_marked, rates, self.RATE_STEP_BPS, self.RATE_STEP):
                changed = True
        changed |= self._judge(status, samples, at)
        if changed:
            self._rates_marked = self._rates
            self._quality_marked = dict(self._quality)
            self._ctx.changed()

    def _judge(self, status, samples, now):
        """Updates each link's window, quality and degraded flag; True if that must publish."""
        flipped = False
        for name, window in self._windows.items():
            if status.get(name) != "online":
                window.clear()
                self._quality.pop(name, None)
                flipped |= name in self._degraded
                self._degraded.discard(name)
                continue
            window.extend((now, ok, ms) for ok, ms in samples.get(name, ()))
            while window and now - window[0][0] >= self.WINDOW_S:
                window.popleft()
            if len(window) < self.MIN_SAMPLES:
                self._quality.pop(name, None)
                continue
            loss = round(100 * sum(not ok for _, ok, _ in window) / len(window), 1)
            times = [ms for _, ok, ms in window if ok and ms is not None]
            latency = int(statistics.median(times)) if times else None
            self._quality[name] = (loss, latency)
            slow = latency is not None and latency >= self.DEGRADED_LATENCY_MS
            if name not in self._degraded and (loss >= self.DEGRADED_LOSS_PCT or slow):
                self._degraded.add(name)
                self._ctx.emit("wan_degraded", wan=name, loss_pct=loss, latency_ms=latency)
                flipped = True
            elif (
                name in self._degraded
                and loss <= self.RECOVERED_LOSS_PCT
                and (latency is None or latency <= self.RECOVERED_LATENCY_MS)
            ):
                self._degraded.discard(name)
                self._ctx.emit("wan_recovered", wan=name, loss_pct=loss, latency_ms=latency)
                flipped = True
        return flipped or self._quality_moved()

    def _quality_moved(self):
        old, new = self._quality_marked, self._quality
        if old.keys() != new.keys():
            return True
        for name, (loss, latency) in new.items():
            was_loss, was_latency = old[name]
            if abs(loss - was_loss) >= self.LOSS_STEP_PCT:
                return True
            if (latency is None) != (was_latency is None):
                return True
            if latency is not None and abs(latency - was_latency) >= self.LATENCY_STEP * max(was_latency, 1):
                return True
        return False

    def _online(self):
        status = self._status if self._status is not None else self._read_status()
        return [n for n in self._names if status.get(n) == "online"]

    def route(self, ip, reserved):
        online = self._online()
        override = self._overrides.get(ip) if reserved else None
        preferred = self._preferences.get(ip)
        order = ([preferred] if preferred else []) + [n for n in self._names if n != preferred]
        usable = override in online and override not in self._degraded
        via = override if usable else next((n for n in order if n in online), None)
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
            if link.name in self._rates:
                entry["rx_bps"], entry["tx_bps"] = self._rates[link.name]
            if link.name in self._quality:
                entry["loss_pct"], latency = self._quality[link.name]
                if latency is not None:
                    entry["latency_ms"] = latency
            entry["degraded"] = link.name in self._degraded
            links.append(entry)
        out = {"active": online[0] if online else None, "links": links}
        if self._usage_error is not None:
            out["usage_error"] = self._usage_error
        return out


def _moved(old, new, step_bps, step):
    if old.keys() != new.keys():
        return True
    return any(abs(b - a) >= max(step_bps, step * a) for name in new for a, b in zip(old[name], new[name], strict=True))


def _is_ipv4(text):
    try:
        return str(ipaddress.IPv4Address(text)) == text
    except ValueError:
        return False
