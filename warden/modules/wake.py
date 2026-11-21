import os
import re
import select
import socket
import struct
from dataclasses import dataclass
from types import MappingProxyType

MAC = re.compile(r"^[0-9a-f]{2}(?::[0-9a-f]{2}){5}$")


@dataclass(frozen=True)
class Target:
    name: str
    mac: str
    ip: str


def magic_packet(mac):
    return b"\xff" * 6 + bytes.fromhex(mac.replace(":", "")) * 16


def send_magic(mac, broadcast, port=9):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.sendto(magic_packet(mac), (broadcast, port))


def _checksum(data):
    if len(data) % 2:
        data += b"\0"
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    total = (total >> 16) + (total & 0xFFFF)
    total += total >> 16
    return ~total & 0xFFFF


def icmp_ping(ip, timeout_s=1.0):
    """One ICMP echo over an unprivileged ping socket (needs net.ipv4.ping_group_range to cover our group)."""
    seq = int.from_bytes(os.urandom(2), "big")
    payload = os.urandom(8)
    header = struct.pack("!BBHHH", 8, 0, 0, 0, seq)
    packet = struct.pack("!BBHHH", 8, 0, _checksum(header + payload), 0, seq) + payload
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP) as s:
        s.sendto(packet, (ip, 0))
        while True:
            ready, _, _ = select.select([s], [], [], timeout_s)
            if not ready:
                return False
            reply, (src, _) = s.recvfrom(1024)
            if src == ip and len(reply) >= 8 and reply[0] == 0 and struct.unpack("!H", reply[6:8])[0] == seq:
                return True


class Monitor:
    """One machine's power as seen by ping."""

    def __init__(self, offline_after, wake_timeout_s):
        self._offline_after = max(1, offline_after)
        self._wake_timeout_s = wake_timeout_s
        self._power = "unknown"
        self._fails = 0
        self._wake_at = None
        self._wake_from_online = False
        self._missed_since_wake = False
        self._seen_at = None

    def on_ping(self, reachable, now):
        """Returns (event, after_s) for a transition, else None."""
        if reachable:
            self._fails = 0
            self._seen_at = now
            was = self._power
            self._power = "online"
            if self._wake_at is not None:
                after = now - self._wake_at
                self._wake_at = None
                if self._wake_from_online and not self._missed_since_wake:
                    return None
                return "up", after
            return ("up", None) if was == "offline" else None
        self._fails += 1
        self._missed_since_wake = self._wake_at is not None
        if self._power == "unknown":
            self._power = "offline"
        elif self._power == "online" and self._fails >= self._offline_after:
            self._power = "offline"
            return "down", None
        return None

    def on_wake_sent(self, now):
        """A machine that answered before the wake and keeps answering was never down: no `up` for it."""
        self._wake_at = now
        self._wake_from_online = self._power == "online"
        self._missed_since_wake = False

    def tick(self, now):
        if self._wake_at is not None and now - self._wake_at >= self._wake_timeout_s:
            after = now - self._wake_at
            self._wake_at = None
            return "wake_timeout", after
        return None

    @property
    def waking(self):
        return self._wake_at is not None

    @property
    def power(self):
        return "waking" if self.waking else self._power

    def state(self, now):
        out = {"power": self.power}
        if self._seen_at is not None:
            out["last_seen_s"] = int(now - self._seen_at)
        if self.waking:
            out["waking_for_s"] = int(now - self._wake_at)
        return out


class Wake:
    """Wake-on-LAN plus ping monitoring for machines on this host's LAN.

    The magic packet is always sent, even to a machine that looks online: ping state lags by up to offline_after
    polls. poll() runs on its own thread and pings outside the agent lock.
    """

    name = "wake"
    commands = ("wake",)
    args = MappingProxyType({"wake": 1})

    def __init__(self, ctx, targets, broadcast, send=send_magic, ping=icmp_ping, offline_after=3, wake_timeout_s=180):
        self._ctx = ctx
        self._targets = {t.name: t for t in targets}
        self._broadcast = broadcast
        self._send = send
        self._ping = ping
        self._monitors = {t.name: Monitor(offline_after, wake_timeout_s) for t in targets}

    def meta(self):
        return {"wakes": list(self._targets)}

    def handle(self, command, args, source):
        target = self._targets.get(args[0])
        if target is None:
            self._ctx.emit("rejected", command=command, reason="unknown target", target=args[0])
            return
        try:
            self._send(target.mac, self._broadcast)
        except OSError as e:
            self._ctx.emit("wake_failed", target=target.name, source=source, error=str(e)[:200])
            return
        self._monitors[target.name].on_wake_sent(self._ctx.mono())
        self._ctx.emit("wake_sent", target=target.name, source=source)
        self._ctx.changed()

    def poll_every_s(self):
        return 1 if any(m.waking for m in self._monitors.values()) else 5

    def poll(self):
        results = [(name, self._reachable(t.ip)) for name, t in self._targets.items()]
        self._ctx.locked(lambda: self._apply(results))()

    def _reachable(self, ip):
        """A routing error means unreachable; a refused ping socket (ping_group_range) is a setup fault and
        propagates, so it is logged instead of every machine quietly reading offline."""
        try:
            return self._ping(ip)
        except PermissionError:
            raise
        except OSError:
            return False

    def _apply(self, results):
        now = self._ctx.mono()
        for name, reachable in results:
            monitor = self._monitors[name]
            before = monitor.power
            transitions = (monitor.on_ping(reachable, now), monitor.tick(now))
            if monitor.power != before:
                self._ctx.changed()
            for transition in transitions:
                if transition is None:
                    continue
                event, after = transition
                fields = {"target": name} if after is None else {"target": name, "after_s": int(after)}
                self._ctx.emit(event, **fields)
                self._ctx.changed()

    def state(self):
        now = self._ctx.mono()
        return {"targets": {name: m.state(now) for name, m in self._monitors.items()}}
