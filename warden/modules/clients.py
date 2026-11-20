import ipaddress
import subprocess
from pathlib import Path


def uci_hosts():
    """DHCP reservations as {ip: name}, from OpenWrt's dhcp config."""
    try:
        out = subprocess.run(["uci", "-q", "show", "dhcp"], capture_output=True, text=True, check=False).stdout
    except OSError:
        return {}
    fields = {}
    for line in out.splitlines():
        key, _, value = line.partition("=")
        section, _, option = key.rpartition(".")
        if section.startswith("dhcp.@host[") and option in ("name", "ip"):
            fields.setdefault(section, {})[option] = value.strip("'")
    return {f["ip"]: f["name"] for f in fields.values() if "ip" in f and "name" in f}


class Clients:
    """Devices on the LAN: every address in `lan` with a resolved neighbour entry.

    Names come from DHCP reservations, then leases. With a Wi-Fi source, each device also gets `link` (`wifi` with
    `band` and `signal`, or `wired`); with a route source, `via` (the ISP its traffic uses) and `override`.
    Publishes no MAC addresses. Emits `joined` and `left`.
    """

    name = "clients"
    commands = ()

    def __init__(
        self,
        ctx,
        lan,
        leases=Path("/tmp/dhcp.leases"),
        arp=Path("/proc/net/arp"),
        hosts=uci_hosts,
        wifi=None,
        route=None,
    ):
        self._ctx = ctx
        self._lan = ipaddress.IPv4Network(lan)
        self._leases = Path(leases)
        self._arp = Path(arp)
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
        neighbours = self._neighbours()
        names = {**self._leased(), **self._hosts()}
        stations = None
        if self._wifi is not None:
            try:
                stations = self._wifi.stations()
            except OSError:
                stations = None
        devices = {}
        for ip, mac in neighbours.items():
            device = {"name": names.get(ip), "ip": ip}
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
                device |= self._route(ip)
            out.append(device)
        return {"online": out}

    def _leased(self):
        out = {}
        for line in _lines(self._leases):
            parts = line.split()
            if len(parts) >= 4 and parts[3] != "*":
                out[parts[2]] = parts[3]
        return out

    def _neighbours(self):
        out = {}
        for line in _lines(self._arp)[1:]:
            parts = line.split()
            if len(parts) < 4 or parts[2] == "0x0":
                continue
            try:
                inside = ipaddress.IPv4Address(parts[0]) in self._lan
            except ValueError:
                continue
            if inside:
                out[parts[0]] = parts[3].lower()
        return out


def _order(ip):
    return ipaddress.IPv4Address(ip)


def _lines(path):
    try:
        return path.read_text().splitlines()
    except OSError:
        return []
