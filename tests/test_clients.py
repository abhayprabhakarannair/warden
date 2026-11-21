import pytest

from warden.modules.clients import Clients, uci_hosts

LEASES = """\
1795208915 02:00:00:00:00:02 192.0.2.2 eap225 01:3c
1795208907 02:00:00:00:00:01 192.0.2.4 devil *
1795203817 02:00:00:00:00:03 192.0.2.204 * *
1795203817 02:00:00:00:00:04 192.0.2.30 phone *
garbage
"""

NEIGH = """\
192.0.2.4 dev br-lan lladdr 02:00:00:00:00:01 REACHABLE
192.0.2.204 dev br-lan lladdr 02:00:00:00:00:03 STALE
192.0.2.2 dev br-lan lladdr 02:00:00:00:00:02 DELAY
192.0.2.103 dev br-lan lladdr 02:00:00:00:00:06 STALE
192.0.2.30 dev br-lan  FAILED
192.0.2.31 dev br-lan  INCOMPLETE
198.51.100.1 dev lan2 lladdr 02:00:00:00:00:05 REACHABLE
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
    (tmp_path / "neigh").write_text(NEIGH)
    return tmp_path


def make(agent, files, **kw):
    hosts = kw.pop("hosts", lambda: {"192.0.2.103": "old-devil", "192.0.2.4": "devil"})
    neighbours = lambda: (files / "neigh").read_text()
    module = Clients(agent, "192.0.2.0/24", leases=files / "leases", neighbours=neighbours, hosts=hosts, **kw)
    agent.add(module)
    return module


def test_lan_neighbours_named_by_reservation_then_lease(agent, bus, files):
    clients = make(agent, files)
    clients.poll()
    assert bus.state()["clients"] == {
        "online": [
            {"name": "eap225", "ip": "192.0.2.2", "reserved": False},
            {"name": "devil", "ip": "192.0.2.4", "reserved": True},
            {"name": "old-devil", "ip": "192.0.2.103", "reserved": True},
            {"name": None, "ip": "192.0.2.204", "reserved": False},
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
        "reserved": False,
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
    seen = {}

    def route(ip, reserved):
        seen[ip] = reserved
        return {"via": via.get(ip, "jio"), "override": None}

    clients = make(agent, files, route=route)
    clients.poll()
    assert bus.state()["clients"]["online"][1] == {
        "name": "devil",
        "ip": "192.0.2.4",
        "reserved": True,
        "via": "bsnl",
        "override": None,
    }
    assert seen["192.0.2.4"] is True and seen["192.0.2.204"] is False
    via["192.0.2.4"] = "railwire"
    agent.changed()
    agent.tick()
    assert bus.state()["clients"]["online"][1]["via"] == "railwire"


def test_joined_and_left_events(agent, bus, files):
    clients = make(agent, files)
    clients.poll()
    assert bus.events() == []
    neigh = (files / "neigh").read_text()
    (files / "neigh").write_text(
        neigh.replace(
            "192.0.2.103 dev br-lan lladdr 02:00:00:00:00:06", "192.0.2.150 dev br-lan lladdr 02:00:00:00:00:06"
        ).replace("192.0.2.30 dev br-lan  FAILED", "192.0.2.30 dev br-lan lladdr 02:00:00:00:00:04 REACHABLE")
    )
    clients.poll()
    assert bus.events() == [
        {"event": "joined", "ip": "192.0.2.30", "name": "phone"},
        {"event": "joined", "ip": "192.0.2.150", "name": None},
        {"event": "left", "ip": "192.0.2.103", "name": "old-devil"},
    ]


def test_a_device_that_moved_is_listed_once_at_its_leased_address(agent, bus, files):
    with (files / "neigh").open("a") as f:
        f.write("192.0.2.200 dev br-lan lladdr 02:00:00:00:00:02 REACHABLE\n")
    clients = make(agent, files)
    clients.poll()
    ips = [d["ip"] for d in bus.state()["clients"]["online"]]
    assert "192.0.2.2" in ips
    assert "192.0.2.200" not in ips


def test_without_a_lease_the_freshest_address_wins(agent, bus, files):
    with (files / "neigh").open("a") as f:
        f.write("192.0.2.99 dev br-lan lladdr 02:00:00:00:00:06 REACHABLE\n")
    clients = make(agent, files)
    clients.poll()
    ips = [d["ip"] for d in bus.state()["clients"]["online"]]
    assert "192.0.2.99" in ips
    assert "192.0.2.103" not in ips


def test_equally_fresh_addresses_without_a_lease_keep_the_lowest(agent, bus, files):
    with (files / "neigh").open("a") as f:
        f.write("192.0.2.50 dev br-lan lladdr 02:00:00:00:00:06 STALE\n")
    clients = make(agent, files)
    clients.poll()
    ips = [d["ip"] for d in bus.state()["clients"]["online"]]
    assert ("192.0.2.50" in ips, "192.0.2.103" in ips) == (True, False)


def test_unchanged_poll_does_not_republish(agent, bus, files):
    clients = make(agent, files)
    clients.poll()
    bus.clear()
    clients.poll()
    assert bus.last("state") is None


def test_missing_sources_mean_no_devices(agent, bus, tmp_path):
    clients = Clients(agent, "192.0.2.0/24", leases=tmp_path / "x", neighbours=str, hosts=dict)
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


def test_uci_hosts_timeout_means_no_names(monkeypatch):
    import subprocess

    def hang(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw["timeout"])

    monkeypatch.setattr("warden.modules.clients.subprocess.run", hang)
    assert uci_hosts() == {}


def test_ip_neigh_failure_means_no_neighbours(monkeypatch):
    import subprocess

    from warden.modules.clients import ip_neigh

    def hang(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw["timeout"])

    monkeypatch.setattr("warden.modules.clients.subprocess.run", hang)
    assert ip_neigh() == ""
