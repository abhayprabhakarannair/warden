import io
import json

import pytest

from warden.modules.wifi import UbusWifi

SESSION = "a" * 32


class AccessPoint:
    """Fake rpcd JSON-RPC endpoint."""

    def __init__(self):
        self.logins = 0
        self.expire_next = False
        self.down = False
        self.requests = []

    def __call__(self, request, timeout):
        if self.down:
            raise OSError("connection refused")
        msg = json.loads(request.data)
        self.requests.append(msg)
        method, params = msg["method"], msg["params"]
        if method == "call" and params[1:3] == ["session", "login"]:
            self.logins += 1
            ok = params[3] == {"username": "warden", "password": "pw"}
            return self._reply({"result": [0, {"ubus_rpc_session": SESSION}] if ok else [6]})
        if self.expire_next:
            self.expire_next = False
            return self._reply({"error": {"code": -32002, "message": "Access denied"}})
        if method == "list":
            return self._reply({"result": {"hostapd.phy0-ap0": {}, "hostapd.phy1-ap0": {}}})
        if params[2] == "get_clients":
            if params[1] == "hostapd.phy0-ap0":
                data = {"freq": 5180, "clients": {"02:00:00:00:00:0A": {"signal": -58}}}
            else:
                data = {"freq": 2437, "clients": {"02:00:00:00:00:0b": {"signal": -70}, "02:00:00:00:00:0c": {}}}
            return self._reply({"result": [0, data]})
        return self._reply({"result": [3]})

    @staticmethod
    def _reply(body):
        return io.BytesIO(json.dumps(body).encode())


@pytest.fixture
def ap():
    return AccessPoint()


def test_stations_by_mac_with_band_and_signal(ap):
    wifi = UbusWifi("http://ap/ubus", "warden", "pw", opener=ap)
    assert wifi.stations() == {
        "02:00:00:00:00:0a": {"band": "5 GHz", "signal": -58},
        "02:00:00:00:00:0b": {"band": "2.4 GHz", "signal": -70},
        "02:00:00:00:00:0c": {"band": "2.4 GHz", "signal": None},
    }


def test_session_is_reused_and_renewed_when_it_expires(ap):
    wifi = UbusWifi("http://ap/ubus", "warden", "pw", opener=ap)
    wifi.stations()
    wifi.stations()
    assert ap.logins == 1
    ap.expire_next = True
    assert len(wifi.stations()) == 3
    assert ap.logins == 2


def test_wrong_password_is_a_permission_error(ap):
    with pytest.raises(PermissionError):
        UbusWifi("http://ap/ubus", "warden", "wrong", opener=ap).stations()


def test_unreachable_access_point_is_an_os_error(ap):
    ap.down = True
    with pytest.raises(OSError):
        UbusWifi("http://ap/ubus", "warden", "pw", opener=ap).stations()
