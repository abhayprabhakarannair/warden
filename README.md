# warden

Host agent: reports a machine's state and runs actions on it, over MQTT. Deployed by [homelab](https://github.com/abhayprabhakarannair/homelab) as an unprivileged service: on a workstation under systemd, with polkit granting power-off, reboot and suspend only; on an OpenWrt router under procd.

## Modules

| Module | State | Commands |
|---|---|---|
| `power` | `pending`: action, source, seconds left | `shutdown`, `reboot`, `suspend` after `WARDEN_GRACE_S`; `cancel` |
| `system` | `kernel`, `uptime_s`, `boot_time`, `load`, `mem_used_pct` | |
| `sensors` | `temps`: °C per hwmon chip and label | |
| `wake` | `targets`: per machine `power` (`online`, `offline`, `waking`, `unknown`), `last_seen_s`, `waking_for_s`; same shape and events (`wake_sent`, `wake_failed`, `up`, `down`, `wake_timeout`) as imp. Adds `wakes` to `meta` | `wake <target>`: always sends the magic packet |
| `wan` | `active` (first online link) and `links`: name and mwan3 status, in failover order; events `wan_up`, `wan_down` | |
| `clients` | `online`: name and address of each DHCP lease with a resolved neighbour entry; no MAC addresses | |

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
| `WARDEN_WANS` | required by `wan` | `name=mwan3-interface,…` in failover order |
| `WARDEN_GRACE_S` | `10` | |
| `WARDEN_STATE_EVERY_S` | `30` | |
| `WARDEN_DRY_RUN` | `0` | `1` logs power actions instead of running them |

## Development

```sh
pytest
ruff check . && ruff format --check .
```

`wake` pings over unprivileged ICMP sockets, so `net.ipv4.ping_group_range` must include the service's group. `wan` reads mwan3track's status files and `clients` reads dnsmasq's lease file and `/proc/net/arp`; all are world-readable on OpenWrt. Requires `paho-mqtt` 1.6 or 2.x at runtime; tests need neither the network nor paho. Enable the hooks once per clone: `git config core.hooksPath .githooks`.
