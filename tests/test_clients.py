import pytest

from warden.modules.clients import Clients, uci_hosts

LEASES = """\
1795208915 02:00:00:00:00:02 192.0.2.2 eap225 01:3c
1795208907 02:00:00:00:00:01 192.0.2.4 devil *
1795203817 02:00:00:00:00:03 192.0.2.204 * *
1795203817 02:00:00:00:00:04 192.0.2.30 phone *
garbage
"""

ARP = """\
IP address       HW type     Flags       HW address            Mask     Device
192.0.2.4        0x1         0x2         02:00:00:00:00:01     *        br-lan
192.0.2.204      0x1         0x2         02:00:00:00:00:03     *        br-lan
192.0.2.2        0x1         0x2         02:00:00:00:00:02     *        br-lan
192.0.2.103      0x1         0x2         02:00:00:00:00:06     *        br-lan
192.0.2.30       0x1         0x0         00:00:00:00:00:00     *        br-lan
198.51.100.1     0x1         0x2         02:00:00:00:00:05     *        lan2
"""


class Wifi:
    def __init__(self):
        self.fail = None
        self.table = {"02:00:00:00:00:03": {"band": "2.4 GHz", "signal": -61}}

    def stations(self):
        if self.fail:
            raise self.fail
        return self.table


@pytest.fixture
def files(tmp_path):
    (tmp_path / "leases").write_text(LEASES)
    (tmp_path / "arp").write_text(ARP)
    return tmp_path


def make(agent, files, **kw):
    hosts = kw.pop("hosts", lambda: {"192.0.2.103": "old-devil", "192.0.2.4": "devil"})
    module = Clients(agent, "192.0.2.0/24", leases=files / "leases", arp=files / "arp", hosts=hosts, **kw)
    agent.add(module)
    return module


def test_lan_neighbours_named_by_reservation_then_lease(agent, bus, files):
    clients = make(agent, files)
    clients.poll()
    assert bus.state()["clients"] == {
        "online": [
            {"name": "eap225", "ip": "192.0.2.2"},
            {"name": "devil", "ip": "192.0.2.4"},
            {"name": "old-devil", "ip": "192.0.2.103"},
            {"name": None, "ip": "192.0.2.204"},
        ]
    }
    assert "02:00" not in bus.last("state")


def test_links_from_the_access_point(agent, bus, files):
    clients = make(agent, files, wifi=Wifi())
    clients.poll()
    online = {d["ip"]: d for d in bus.state()["clients"]["online"]}
    assert online["192.0.2.204"] == {
        "name": None,
        "ip": "192.0.2.204",
        "link": "wifi",
        "band": "2.4 GHz",
        "signal": -61,
    }
    assert online["192.0.2.4"]["link"] == "wired"


def test_unreachable_access_point_leaves_links_unknown(agent, bus, files):
    wifi = Wifi()
    wifi.fail = OSError("timed out")
    clients = make(agent, files, wifi=wifi)
    clients.poll()
    assert all("link" not in d for d in bus.state()["clients"]["online"])


def test_route_is_added_per_device_at_publish_time(agent, bus, files):
    via = {"192.0.2.4": "bsnl"}
    clients = make(agent, files, route=lambda ip: {"via": via.get(ip, "jio"), "override": None})
    clients.poll()
    assert bus.state()["clients"]["online"][1] == {"name": "devil", "ip": "192.0.2.4", "via": "bsnl", "override": None}
    via["192.0.2.4"] = "railwire"
    agent.changed()
    agent.tick()
    assert bus.state()["clients"]["online"][1]["via"] == "railwire"


def test_joined_and_left_events(agent, bus, files):
    clients = make(agent, files)
    clients.poll()
    assert bus.events() == []
    arp = (files / "arp").read_text()
    (files / "arp").write_text(
        arp.replace("192.0.2.103 ", "192.0.2.150 ").replace(
            "0x0         00:00:00:00:00:00", "0x2         02:00:00:00:00:04"
        )
    )
    clients.poll()
    assert bus.events() == [
        {"event": "joined", "ip": "192.0.2.30", "name": "phone"},
        {"event": "joined", "ip": "192.0.2.150", "name": None},
        {"event": "left", "ip": "192.0.2.103", "name": "old-devil"},
    ]


def test_unchanged_poll_does_not_republish(agent, bus, files):
    clients = make(agent, files)
    clients.poll()
    bus.clear()
    clients.poll()
    assert bus.last("state") is None


def test_missing_files_mean_no_devices(agent, bus, tmp_path):
    clients = Clients(agent, "192.0.2.0/24", leases=tmp_path / "x", arp=tmp_path / "y", hosts=dict)
    agent.add(clients)
    clients.poll()
    assert bus.state()["clients"] == {"online": []}


def test_uci_hosts_parses_reservations(monkeypatch):
    out = (
        "dhcp.@host[0]=host\n"
        "dhcp.@host[0].name='devil'\n"
        "dhcp.@host[0].mac='02:00:00:00:00:01'\n"
        "dhcp.@host[0].ip='192.0.2.4'\n"
        "dhcp.@host[1].name='noip'\n"
        "dhcp.lan.start='100'\n"
    )

    class Result:
        stdout = out

    monkeypatch.setattr("warden.modules.clients.subprocess.run", lambda *a, **k: Result())
    assert uci_hosts() == {"192.0.2.4": "devil"}


def test_uci_hosts_without_uci(monkeypatch):
    def missing(*a, **k):
        raise FileNotFoundError("uci")

    monkeypatch.setattr("warden.modules.clients.subprocess.run", missing)
    assert uci_hosts() == {}
