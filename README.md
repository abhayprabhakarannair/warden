# warden

Host agent: reports a machine's state and runs power actions, over MQTT. Deployed by [homelab](https://github.com/abhayprabhakarannair/homelab) as a systemd service running as an unprivileged user; polkit grants that user power-off, reboot and suspend only.

## Modules

| Module | State | Commands |
|---|---|---|
| `power` | `pending`: action, source, seconds left | `shutdown`, `reboot`, `suspend` after `WARDEN_GRACE_S`; `cancel` |
| `system` | `kernel`, `uptime_s`, `boot_time`, `load`, `mem_used_pct` | |
| `sensors` | `temps`: °C per hwmon chip and label | |

Power actions bypass inhibitors (`systemctl … --check-inhibitors=no`): a remote command must not be blocked by a desktop app.

## MQTT

Under `WARDEN_TOPIC` (default `home/<user>`): `meta` and `state` (retained), `status` (`online`/`offline`/`suspended`, retained, last will), `event` (`pending`, `cancelled`, `executing`, `failed`, `rejected`, `resumed`), and `cmd` (`<command> [source]`, plus `refresh`).

## Configuration

| Variable | Default | |
|---|---|---|
| `WARDEN_MQTT_HOST` | required | |
| `WARDEN_MQTT_PORT` | `8443` | TLS |
| `WARDEN_MQTT_USER` | required | also the default topic name |
| `WARDEN_MQTT_PASSWORD_FILE` | required | |
| `WARDEN_TOPIC` | `home/<user>` | |
| `WARDEN_MODULES` | `power,system,sensors` | |
| `WARDEN_GRACE_S` | `10` | |
| `WARDEN_STATE_EVERY_S` | `30` | |
| `WARDEN_DRY_RUN` | `0` | `1` logs power actions instead of running them |

## Development

```sh
pytest
ruff check . && ruff format --check .
```

Requires `paho-mqtt` 1.6 or 2.x at runtime; tests need neither the network nor paho. Enable the hooks once per clone: `git config core.hooksPath .githooks`.
