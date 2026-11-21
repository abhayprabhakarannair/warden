import ipaddress
import subprocess
from pathlib import Path


def uci_hosts():
    """DHCP reservations as {ip: name}, from OpenWrt's dhcp config."""
    try:
        out = subprocess.run(
            ["uci", "-q", "show", "dhcp"], capture_output=True, text=True, check=False, timeout=5
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}
    fields = {}
    for line in out.splitlines():
        key, _, value = line.partition("=")
        section, _, option = key.rpartition(".")
        if section.startswith("dhcp.@host[") and option in ("name", "ip"):
            fields.setdefault(section, {})[option] = value.strip("'")
    return {f["ip"]: f["name"] for f in fields.values() if "ip" in f and "name" in f}


def ip_neigh():
    """The IPv4 neighbour table as `ip -4 neigh show` prints it."""
    try:
        return subprocess.run(
            ["ip", "-4", "neigh", "show"], capture_output=True, text=True, check=False, timeout=5
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


# Most recently confirmed first; FAILED and INCOMPLETE entries are not devices.
FRESHNESS = {"PERMANENT": 0, "REACHABLE": 0, "DELAY": 1, "PROBE": 1, "STALE": 2}


class Clients:
    """Devices on the LAN: every address in `lan` with a resolved neighbour entry, one per hardware address.

    A device seen at several addresses (it moved, and the old entry has not expired) is listed once: at the address
    its DHCP lease names, else the most recently confirmed one. Names come from DHCP reservations, then leases;
    `reserved` says whether the address is a reservation. With a Wi-Fi source, each device also gets `link` (`wifi`
    with `band` and `signal`, or `wired`); with a route source, `via` (the ISP its traffic uses) and `override`.
    Publishes no MAC addresses. Emits `joined` and `left`.
    """

    name = "clients"
    commands = ()

    def __init__(
        self,
        ctx,
        lan,
        leases=Path("/tmp/dhcp.leases"),
        neighbours=ip_neigh,
        hosts=uci_hosts,
        wifi=None,
        route=None,
    ):
        self._ctx = ctx
        self._lan = ipaddress.IPv4Network(lan)
        self._leases = Path(leases)
        self._neigh = neighbours
        self._hosts = hosts
        self._wifi = wifi
        self._route = route
        self._devices = None

    def handle(self, command, args, source):
        raise ValueError(f"clients has no command {command!r}")

    def poll_every_s(self):
        return 5

    def poll(self):
        devices = self._scan()
        self._ctx.locked(lambda: self._apply(devices))()

    def _scan(self):
        leases = self._leases_by_ip()
        neighbours = self._neighbours({ip: mac for ip, (mac, _) in leases.items()})
        hosts = self._hosts()
        names = {**{ip: name for ip, (_, name) in leases.items() if name}, **hosts}
        stations = None
        if self._wifi is not None:
            try:
                stations = self._wifi.stations()
            except OSError:
                stations = None
        devices = {}
        for ip, mac in neighbours.items():
            device = {"name": names.get(ip), "ip": ip, "reserved": ip in hosts}
            if stations is not None:
                station = stations.get(mac)
                device |= {"link": "wifi", **station} if station else {"link": "wired"}
            devices[ip] = device
        return devices

    def _apply(self, devices):
        if self._devices is not None:
            for ip in sorted(devices.keys() - self._devices.keys(), key=_order):
                self._ctx.emit("joined", ip=ip, name=devices[ip]["name"])
            for ip in sorted(self._devices.keys() - devices.keys(), key=_order):
                self._ctx.emit("left", ip=ip, name=self._devices[ip]["name"])
        if devices != self._devices:
            self._devices = devices
            self._ctx.changed()

    def state(self):
        out = []
        for ip in sorted(self._devices or {}, key=_order):
            device = dict(self._devices[ip])
            if self._route is not None:
                device |= self._route(ip, device["reserved"])
            out.append(device)
        return {"online": out}

    def _leases_by_ip(self):
        out = {}
        for line in _lines(self._leases):
            parts = line.split()
            if len(parts) >= 4:
                out[parts[2]] = (parts[1].lower(), None if parts[3] == "*" else parts[3])
        return out

    def _neighbours(self, leased):
        """{ip: mac} for live LAN entries, keeping one address per mac."""
        by_mac = {}
        for line in self._neigh().splitlines():
            parts = line.split()
            if len(parts) < 4 or "lladdr" not in parts or parts[-1] not in FRESHNESS:
                continue
            ip, mac = parts[0], parts[parts.index("lladdr") + 1].lower()
            try:
                if ipaddress.IPv4Address(ip) not in self._lan:
                    continue
            except ValueError:
                continue
            by_mac.setdefault(mac, []).append((ip, FRESHNESS[parts[-1]]))
        out = {}
        for mac, seen in by_mac.items():
            ip, _ = min(seen, key=lambda e: (leased.get(e[0]) != mac, e[1], _order(e[0])))
            out[ip] = mac
        return out


def _order(ip):
    return ipaddress.IPv4Address(ip)


def _lines(path):
    try:
        return path.read_text().splitlines()
    except OSError:
        return []
