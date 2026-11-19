import sys
import types
from types import SimpleNamespace

import pytest

CONFIG = SimpleNamespace(host="bus.example", port=8443, user="devil", password="pw")


class FakeClient:
    def __init__(self, *args, **kwargs):
        self.args, self.kwargs = args, kwargs
        self.calls = []
        self.connected = True

    def __getattr__(self, name):
        return lambda *a, **k: self.calls.append((name, a, k)) or SimpleNamespace(wait_for_publish=self.waited)

    def waited(self, timeout):
        self.calls.append(("wait_for_publish", (timeout,), {}))

    def is_connected(self):
        return self.connected


def load_bus(monkeypatch, with_v2):
    client_mod = types.ModuleType("paho.mqtt.client")
    client_mod.Client = FakeClient
    if with_v2:
        client_mod.CallbackAPIVersion = SimpleNamespace(VERSION2="v2")
    paho = types.ModuleType("paho")
    paho_mqtt = types.ModuleType("paho.mqtt")
    paho_mqtt.client = client_mod
    paho.mqtt = paho_mqtt
    monkeypatch.setitem(sys.modules, "paho", paho)
    monkeypatch.setitem(sys.modules, "paho.mqtt", paho_mqtt)
    monkeypatch.setitem(sys.modules, "paho.mqtt.client", client_mod)
    monkeypatch.delitem(sys.modules, "warden.bus", raising=False)
    import warden.bus

    return warden.bus.Bus(CONFIG, client_id="warden-devil", will_topic="home/devil/status")


@pytest.mark.parametrize("with_v2", [True, False])
def test_client_setup(monkeypatch, with_v2):
    bus = load_bus(monkeypatch, with_v2)
    client = bus._client
    assert client.args == (("v2",) if with_v2 else ())
    assert client.kwargs == {"client_id": "warden-devil", "clean_session": True}
    names = [c[0] for c in client.calls]
    assert names[:2] == ["username_pw_set", "tls_set_context"]
    assert ("will_set", ("home/devil/status", "offline"), {"qos": 1, "retain": True}) in client.calls
    assert "max_queued_messages_set" in names


def test_v2_on_connect_only_on_success(monkeypatch):
    bus = load_bus(monkeypatch, with_v2=True)
    seen = []
    bus.on_connect = lambda: seen.append("connected")
    bus._client.on_connect(None, None, {}, SimpleNamespace(is_failure=True), None)
    assert seen == []
    bus._client.on_connect(None, None, {}, SimpleNamespace(is_failure=False), None)
    assert seen == ["connected"]


def test_v1_on_connect_only_on_success(monkeypatch):
    bus = load_bus(monkeypatch, with_v2=False)
    seen = []
    bus.on_connect = lambda: seen.append("connected")
    bus._client.on_connect(None, None, {}, 5)
    assert seen == []
    bus._client.on_connect(None, None, {}, 0)
    assert seen == ["connected"]


@pytest.mark.parametrize("with_v2", [True, False])
def test_messages_and_publishing(monkeypatch, with_v2):
    bus = load_bus(monkeypatch, with_v2)
    got = []
    bus.on_message = got.append
    bus._client.on_message(None, None, SimpleNamespace(payload=b"reboot"))
    assert got == [b"reboot"]
    bus.publish("t", "p", retain=True)
    bus.subscribe("home/devil/cmd")
    assert ("publish", ("t", "p"), {"qos": 1, "retain": True}) in bus._client.calls
    assert ("subscribe", ("home/devil/cmd",), {"qos": 1}) in bus._client.calls


@pytest.mark.parametrize("connected", [True, False])
def test_close_flushes_only_when_connected(monkeypatch, connected):
    bus = load_bus(monkeypatch, True)
    bus.publish("t", "offline", retain=True)
    bus._client.connected = connected
    bus.close()
    names = [c[0] for c in bus._client.calls]
    assert ("wait_for_publish" in names) is connected
    assert names[-2:] == ["disconnect", "loop_stop"]
