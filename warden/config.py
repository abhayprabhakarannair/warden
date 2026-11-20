import ipaddress
import os
import re
from dataclasses import dataclass
from pathlib import Path

from warden.modules.wake import MAC, Target

NAME = re.compile(r"^[a-z0-9_-]{1,31}$")
IFACE = re.compile(r"^[A-Za-z0-9_.-]{1,15}$")
KNOWN_MODULES = ("power", "system", "sensors", "wake", "wan", "clients")
DEFAULT_MODULES = ("power", "system", "sensors")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    user: str
    password: str
    topic: str
    modules: tuple[str, ...]
    grace_s: int
    state_every_s: int
    dry_run: bool
    wake_targets: tuple[Target, ...] = ()
    wake_broadcast: str = ""
    wans: tuple[tuple[str, str], ...] = ()


def load(env=os.environ):
    def need(key):
        value = env.get(key, "").strip()
        if not value:
            raise ConfigError(f"{key} is required")
        return value

    def integer(key, default, low, high):
        raw = env.get(key, str(default)).strip()
        if not raw.isdigit() or not low <= int(raw) <= high:
            raise ConfigError(f"{key} must be an integer in [{low}, {high}], got {raw!r}")
        return int(raw)

    user = need("WARDEN_MQTT_USER")
    if not NAME.match(user):
        raise ConfigError(f"WARDEN_MQTT_USER {user!r} is not a valid client name")

    password_file = Path(need("WARDEN_MQTT_PASSWORD_FILE"))
    try:
        password = password_file.read_text().strip()
    except OSError as e:
        raise ConfigError(f"cannot read WARDEN_MQTT_PASSWORD_FILE: {e}") from e
    if not password:
        raise ConfigError("WARDEN_MQTT_PASSWORD_FILE is empty")

    modules = tuple(m.strip() for m in env.get("WARDEN_MODULES", ",".join(DEFAULT_MODULES)).split(",") if m.strip())
    if not modules:
        raise ConfigError("WARDEN_MODULES lists no modules")
    unknown = sorted(set(modules) - set(KNOWN_MODULES))
    if unknown:
        raise ConfigError(f"unknown modules: {', '.join(unknown)}")
    if len(set(modules)) != len(modules):
        raise ConfigError("WARDEN_MODULES lists a module twice")

    dry_run = env.get("WARDEN_DRY_RUN", "0").strip()
    if dry_run not in ("0", "1"):
        raise ConfigError("WARDEN_DRY_RUN must be 0 or 1")

    wake_targets, wake_broadcast = (), ""
    if "wake" in modules:
        wake_targets = _targets(need("WARDEN_WAKE_TARGETS"))
        wake_broadcast = _ipv4("WARDEN_WAKE_BROADCAST", need("WARDEN_WAKE_BROADCAST"))
    wans = _wans(need("WARDEN_WANS")) if "wan" in modules else ()

    return Config(
        host=need("WARDEN_MQTT_HOST"),
        port=integer("WARDEN_MQTT_PORT", 8443, 1, 65535),
        user=user,
        password=password,
        topic=env.get("WARDEN_TOPIC", f"home/{user}").strip().rstrip("/"),
        modules=modules,
        grace_s=integer("WARDEN_GRACE_S", 10, 0, 3600),
        state_every_s=integer("WARDEN_STATE_EVERY_S", 30, 1, 3600),
        dry_run=dry_run == "1",
        wake_targets=wake_targets,
        wake_broadcast=wake_broadcast,
        wans=wans,
    )


def _ipv4(key, value):
    try:
        return str(ipaddress.IPv4Address(value))
    except ValueError as e:
        raise ConfigError(f"{key}: {value!r} is not an IPv4 address") from e


def _pairs(key, raw):
    out = []
    for item in (i.strip() for i in raw.split(",")):
        name, sep, value = item.partition("=")
        if not sep or not NAME.match(name.strip()):
            raise ConfigError(f"{key}: expected name=value, got {item!r}")
        out.append((name.strip(), value.strip()))
    names = [n for n, _ in out]
    if len(set(names)) != len(names):
        raise ConfigError(f"{key} lists a name twice")
    return out


def _targets(raw):
    """name=mac@ip,…"""
    targets = []
    for name, value in _pairs("WARDEN_WAKE_TARGETS", raw):
        mac, sep, ip = value.lower().partition("@")
        if not sep or not MAC.match(mac):
            raise ConfigError(f"WARDEN_WAKE_TARGETS: {name} needs mac@ip, got {value!r}")
        targets.append(Target(name, mac, _ipv4("WARDEN_WAKE_TARGETS", ip)))
    return tuple(targets)


def _wans(raw):
    """name=mwan3-interface,… in failover order"""
    wans = _pairs("WARDEN_WANS", raw)
    for name, iface in wans:
        if not IFACE.match(iface):
            raise ConfigError(f"WARDEN_WANS: {name} has an invalid interface {iface!r}")
    return tuple(wans)
