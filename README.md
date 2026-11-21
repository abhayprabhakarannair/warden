# warden

Host agent: reports a machine's state and runs actions on it, over MQTT. Deployed by [homelab](https://github.com/abhayprabhakarannair/homelab) as an unprivileged service: on a workstation under systemd, with polkit granting power-off, reboot and suspend only; on an OpenWrt router under procd.

## Modules

| Module | State | Commands |
|---|---|---|
| `power` | `pending`: action, source, seconds left | `shutdown`, `reboot`, `suspend` after `WARDEN_GRACE_S`; `cancel` |
| `system` | `kernel`, `uptime_s`, `boot_time`, `load`, `mem_used_pct` | |
| `sensors` | `temps`: °C per hwmon chip and label | |
| `wake` | `targets`: per machine `power` (`online`, `offline`, `waking`, `unknown`), `last_seen_s`, `waking_for_s`; same shape and events (`wake_sent`, `wake_failed`, `up`, `down`, `wake_timeout`) as imp. Adds `wakes` to `meta` | `wake <target>`: always sends the magic packet |
| `wan` | `active` (first online link) and `links`: name, mwan3 status, `used_bytes` this cycle (vnstat, when a device is given) and `cap_bytes`; `usage_error` while usage cannot be read (last values kept); events `wan_up`, `wan_down`, `isp_set`, `isp_failed` | `isp <ip> <link\|auto>`: route one device through a link, through the router's `wanpref` ubus helper; the router drops the override while that link is offline |
| `clients` | `online`: every LAN address with a resolved neighbour entry, named by DHCP reservation then lease; `link` (`wifi` with `band` and `signal`, or `wired`) when a Wi-Fi API is set; `via` and `override` from `wan`; events `joined`, `left`; no MAC addresses | |

Power actions bypass inhibitors (`systemctl … --check-inhibitors=no`): a remote command must not be blocked by a desktop app.

## MQTT

Under `WARDEN_TOPIC` (default `home/<user>`): `meta` and `state` (retained), `status` (`online`/`offline`/`suspended`, retained, last will), `event` (`pending`, `cancelled`, `executing`, `failed`, `rejected`, `resumed`), and `cmd` (`<command> [args…] [source]`, plus `refresh`; each module declares how many arguments its commands take).

## Configuration

| Variable | Default | |
|---|---|---|
| `WARDEN_MQTT_HOST` | required | |
| `WARDEN_MQTT_PORT` | `8443` | TLS |
| `WARDEN_MQTT_USER` | required | also the default topic name |
| `WARDEN_MQTT_PASSWORD_FILE` | required | |
| `WARDEN_TOPIC` | `home/<user>` | |
| `WARDEN_MODULES` | `power,system,sensors` | also `wake`, `wan`, `clients` |
| `WARDEN_WAKE_TARGETS` | required by `wake` | `name=mac@ip,…` |
| `WARDEN_WAKE_BROADCAST` | required by `wake` | LAN broadcast address for magic packets |
| `WARDEN_WANS` | required by `wan` | `name=mwan3-interface[/device],…` in failover order; the device enables usage |
| `WARDEN_WAN_CAPS` | | `name=gigabytes,…` |
| `WARDEN_WAN_PREFERENCES` | | `ip=name,…`: the link a device tries first |
| `WARDEN_LAN` | required by `clients` | LAN network, e.g. `192.0.2.0/24` |
| `WARDEN_WIFI_API`, `WARDEN_WIFI_USER`, `WARDEN_WIFI_PASSWORD_FILE` | | an access point's ubus JSON-RPC URL and a login allowed `hostapd.*` `get_clients` |
| `WARDEN_GRACE_S` | `10` | |
| `WARDEN_STATE_EVERY_S` | `30` | |
| `WARDEN_DRY_RUN` | `0` | `1` logs power actions instead of running them |

## Development

```sh
pytest
ruff check . && ruff format --check .
```

`wake` pings over unprivileged ICMP sockets, so `net.ipv4.ping_group_range` must include the service's group. `wan` reads mwan3track's status files, `vnstat --json` and the override files in `/tmp/wanpref`, and calls `ubus call wanpref set`; `clients` reads dnsmasq's lease file, `ip -4 neigh show` and `uci show dhcp`. All are readable by an unprivileged user on OpenWrt; the ubus call needs an ACL. Requires `paho-mqtt` 1.6 or 2.x at runtime; tests need neither the network nor paho. Enable the hooks once per clone: `git config core.hooksPath .githooks`.
