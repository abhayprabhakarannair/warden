import socket

import pytest

from warden.modules.wake import Monitor, Target, Wake, icmp_ping, magic_packet

DEVIL = Target("devil", "02:00:00:00:00:01", "192.0.2.4")


class Net:
    def __init__(self):
        self.up = {}
        self.sent = []
        self.fail = None

    def ping(self, ip):
        return self.up.get(ip, False)

    def send(self, mac, broadcast):
        if self.fail:
            raise self.fail
        self.sent.append((mac, broadcast))


@pytest.fixture
def net():
    return Net()


@pytest.fixture
def wake(agent, net):
    module = Wake(agent, [DEVIL], "192.0.2.255", send=net.send, ping=net.ping, offline_after=3, wake_timeout_s=60)
    agent.add(module)
    return module


def test_magic_packet_layout():
    packet = magic_packet("02:00:00:00:00:01")
    assert len(packet) == 102
    assert packet[:6] == b"\xff" * 6
    assert packet[6:] == bytes.fromhex("020000000001") * 16


def test_meta_lists_targets(agent, bus, wake):
    agent.on_connect()
    assert '"wakes":["devil"]' in bus.last("meta")


def test_wake_always_sends_and_reports(agent, bus, net, wake):
    net.up[DEVIL.ip] = True
    wake.poll()
    agent.on_message(b"wake devil console")
    assert net.sent == [(DEVIL.mac, "192.0.2.255")]
    assert {"event": "wake_sent", "target": "devil", "source": "console"} in bus.events()
    assert bus.state()["wake"]["targets"]["devil"]["power"] == "waking"


def test_unknown_target_is_rejected(agent, bus, net, wake):
    agent.on_message(b"wake nobody")
    assert net.sent == []
    assert bus.events() == [{"event": "rejected", "command": "wake", "reason": "unknown target", "target": "nobody"}]


def test_send_failure_is_reported(agent, bus, net, wake):
    net.fail = OSError("network unreachable")
    agent.on_message(b"wake devil")
    assert bus.events() == [
        {"event": "wake_failed", "target": "devil", "source": "mqtt", "error": "network unreachable"}
    ]
    assert bus.state()["wake"]["targets"]["devil"]["power"] == "unknown"


def test_wake_then_up_reports_how_long_it_took(agent, bus, clock, net, wake):
    wake.poll()
    agent.on_message(b"wake devil")
    assert wake.poll_every_s() == 1
    clock.advance(12)
    net.up[DEVIL.ip] = True
    wake.poll()
    assert bus.events()[-1] == {"event": "up", "target": "devil", "after_s": 12}
    assert bus.state()["wake"]["targets"]["devil"] == {"power": "online", "last_seen_s": 0}
    assert wake.poll_every_s() == 5


def test_wake_times_out(agent, bus, clock, net, wake):
    agent.on_message(b"wake devil")
    clock.advance(60)
    wake.poll()
    assert bus.events()[-1] == {"event": "wake_timeout", "target": "devil", "after_s": 60}
    assert bus.state()["wake"]["targets"]["devil"]["power"] == "offline"


def test_down_only_after_consecutive_misses(agent, bus, net, wake):
    net.up[DEVIL.ip] = True
    wake.poll()
    net.up[DEVIL.ip] = False
    wake.poll()
    wake.poll()
    assert bus.state()["wake"]["targets"]["devil"]["power"] == "online"
    wake.poll()
    assert bus.events()[-1] == {"event": "down", "target": "devil"}
    assert bus.state()["wake"]["targets"]["devil"]["power"] == "offline"


def test_first_miss_settles_unknown_to_offline_without_event(agent, bus, net, wake):
    wake.poll()
    assert bus.events() == []
    assert bus.state()["wake"]["targets"]["devil"] == {"power": "offline"}


def test_recovery_from_offline_is_up_without_duration(agent, bus, net, wake):
    wake.poll()
    net.up[DEVIL.ip] = True
    wake.poll()
    assert bus.events() == [{"event": "up", "target": "devil"}]


def test_monitor_tracks_last_seen():
    m = Monitor(offline_after=2, wake_timeout_s=60)
    m.on_ping(True, 100)
    m.on_ping(False, 130)
    assert m.state(130) == {"power": "online", "last_seen_s": 30}


def test_icmp_ping_localhost():
    try:
        assert icmp_ping("127.0.0.1", timeout_s=1) is True
    except PermissionError:
        pytest.skip("ping sockets not allowed for this group (net.ipv4.ping_group_range)")


def test_icmp_ping_unreachable_times_out():
    try:
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP).close()
    except PermissionError:
        pytest.skip("ping sockets not allowed for this group (net.ipv4.ping_group_range)")
    assert icmp_ping("192.0.2.1", timeout_s=0.2) is False


def test_wake_while_online_and_still_answering_is_not_an_up(agent, bus, clock, net, wake):
    net.up[DEVIL.ip] = True
    wake.poll()
    agent.on_message(b"wake devil")
    clock.advance(1)
    wake.poll()
    assert [e["event"] for e in bus.events()] == ["wake_sent"]
    assert bus.state()["wake"]["targets"]["devil"]["power"] == "online"


def test_wake_while_looking_online_but_actually_off_reports_the_real_duration(agent, bus, clock, net, wake):
    net.up[DEVIL.ip] = True
    wake.poll()
    net.up[DEVIL.ip] = False
    agent.on_message(b"wake devil")
    clock.advance(1)
    wake.poll()
    clock.advance(20)
    net.up[DEVIL.ip] = True
    wake.poll()
    assert bus.events()[-1] == {"event": "up", "target": "devil", "after_s": 21}


def test_a_ping_error_on_one_target_does_not_stop_the_others(agent, bus):
    nas = Target("nas", "02:00:00:00:00:09", "192.0.2.9")

    def ping(ip):
        if ip == DEVIL.ip:
            raise OSError("no route to host")
        return True

    module = Wake(agent, [DEVIL, nas], "192.0.2.255", send=lambda *_: None, ping=ping)
    agent.add(module)
    module.poll()
    targets = bus.state()["wake"]["targets"]
    assert (targets["devil"]["power"], targets["nas"]["power"]) == ("offline", "online")


def test_a_refused_ping_socket_is_an_error_not_offline(agent, bus):
    def ping(ip):
        raise PermissionError("operation not permitted")

    module = Wake(agent, [DEVIL], "192.0.2.255", send=lambda *_: None, ping=ping)
    agent.add(module)
    with pytest.raises(PermissionError):
        module.poll()
    assert module.state()["targets"]["devil"]["power"] == "unknown"
