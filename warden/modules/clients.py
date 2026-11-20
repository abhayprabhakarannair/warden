from pathlib import Path


class Clients:
    """Devices on the LAN: DHCP leases (dnsmasq format) whose address has a resolved neighbour entry.

    Publishes names and addresses only, never MAC addresses.
    """

    name = "clients"
    commands = ()

    def __init__(self, ctx, leases=Path("/tmp/dhcp.leases"), arp=Path("/proc/net/arp")):
        self._ctx = ctx
        self._leases = Path(leases)
        self._arp = Path(arp)

    def handle(self, command, args, source):
        raise ValueError(f"clients has no command {command!r}")

    def state(self):
        present = self._neighbours()
        devices = []
        for line in _lines(self._leases):
            parts = line.split()
            if len(parts) < 4:
                continue
            ip, name = parts[2], parts[3]
            if ip in present:
                devices.append({"name": None if name == "*" else name, "ip": ip})
        devices.sort(key=lambda d: tuple(int(x) if x.isdigit() else 0 for x in d["ip"].split(".")))
        return {"online": devices}

    def _neighbours(self):
        out = set()
        for line in _lines(self._arp)[1:]:
            parts = line.split()
            if len(parts) >= 3 and parts[2] != "0x0":
                out.add(parts[0])
        return out


def _lines(path):
    try:
        return path.read_text().splitlines()
    except OSError:
        return []
