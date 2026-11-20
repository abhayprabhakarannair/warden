import json
import urllib.request

ANONYMOUS = "0" * 32


class UbusWifi:
    """Wi-Fi stations of an OpenWrt access point, read over its ubus JSON-RPC API (rpcd).

    The login needs read access to `hostapd.*` `get_clients`. stations() returns {mac: {"band", "signal"}} and
    raises OSError when the access point cannot be reached or refuses the login.
    """

    def __init__(self, url, user, password, timeout_s=3, opener=urllib.request.urlopen):
        self._url = url
        self._user = user
        self._password = password
        self._timeout_s = timeout_s
        self._open = opener
        self._session = None

    def stations(self):
        try:
            return self._stations()
        except PermissionError:
            self._session = None
            return self._stations()

    def _stations(self):
        if self._session is None:
            self._session = self._login()
        out = {}
        for obj in sorted(self._rpc("list", [self._session, "hostapd.*"]) or {}):
            data = self._call(obj, "get_clients")
            band = "5 GHz" if data.get("freq", 0) >= 4900 else "2.4 GHz"
            for mac, client in (data.get("clients") or {}).items():
                signal = client.get("signal") if isinstance(client, dict) else None
                out[mac.lower()] = {"band": band, "signal": signal if isinstance(signal, int) else None}
        return out

    def _login(self):
        data = self._call("session", "login", {"username": self._user, "password": self._password}, ANONYMOUS)
        session = data.get("ubus_rpc_session")
        if not isinstance(session, str):
            raise PermissionError("access point login returned no session")
        return session

    def _call(self, obj, method, args=None, session=None):
        result = self._rpc("call", [session or self._session, obj, method, args or {}])
        if not isinstance(result, list) or not result:
            raise OSError(f"unexpected reply from {obj}.{method}")
        if result[0] == 6:
            raise PermissionError(f"access point denied {obj}.{method}")
        if result[0] != 0:
            raise OSError(f"{obj}.{method} failed with ubus status {result[0]}")
        return result[1] if len(result) > 1 and isinstance(result[1], dict) else {}

    def _rpc(self, method, params):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        request = urllib.request.Request(self._url, data=body, headers={"Content-Type": "application/json"})
        try:
            with self._open(request, timeout=self._timeout_s) as response:
                reply = json.loads(response.read())
        except ValueError as e:
            raise OSError(f"bad JSON from access point: {e}") from e
        if "error" in reply:
            code = reply["error"].get("code")
            if code == -32002:
                raise PermissionError("access point session expired or denied")
            raise OSError(f"access point error: {reply['error']}")
        return reply.get("result")
