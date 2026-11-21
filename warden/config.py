import ipaddress
import os
import re
from dataclasses import dataclass
from pathlib import Path

from warden.modules.wake import MAC, Target
from warden.modules.wan import Link

NAME = re.compile(r"^[a-z0-9_-]{1,31}$")
IFACE = re.compile(r"^[A-Za-z0-9_.-]{1,15}$")
UPS = re.compile(r"^[A-Za-z0-9_.-]{1,64}@[A-Za-z0-9_.-]{1,253}(:[0-9]{1,5})?$")
KNOWN_MODULES = ("power", "system", "sensors", "wake", "wan", "clients", "ups")
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
    wans: tuple[Link, ...] = ()
    wan_preferences: tuple[tuple[str, str], ...] = ()
    lan: str = ""
    wifi_api: str = ""
    wifi_user: str = ""
    wifi_password: str = ""
    ups: str = ""
    disks: tuple[str, ...] = ("/",)


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
    wans, preferences = (), ()
    if "wan" in modules:
        wans = _wans(need("WARDEN_WANS"), env.get("WARDEN_WAN_CAPS", ""))
        preferences = _preferences(env.get("WARDEN_WAN_PREFERENCES", ""), {w.name for w in wans})
    lan, wifi_api, wifi_user, wifi_password = "", "", "", ""
    if "clients" in modules:
        lan = _network("WARDEN_LAN", need("WARDEN_LAN"))
        wifi_api = env.get("WARDEN_WIFI_API", "").strip()
        if wifi_api:
            if not wifi_api.startswith(("http://", "https://")):
                raise ConfigError("WARDEN_WIFI_API must be an http(s) URL")
            wifi_user = need("WARDEN_WIFI_USER")
            wifi_password = _secret("WARDEN_WIFI_PASSWORD_FILE", need("WARDEN_WIFI_PASSWORD_FILE"))

    ups = ""
    if "ups" in modules:
        ups = need("WARDEN_UPS")
        if not UPS.match(ups):
            raise ConfigError(f"WARDEN_UPS must be name@host[:port], got {ups!r}")

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
        wan_preferences=preferences,
        lan=lan,
        wifi_api=wifi_api,
        wifi_user=wifi_user,
        wifi_password=wifi_password,
        ups=ups,
        disks=_disks(env.get("WARDEN_DISKS", "/")),
    )


def _secret(key, path):
    try:
        value = Path(path).read_text().strip()
    except OSError as e:
        raise ConfigError(f"cannot read {key}: {e}") from e
    if not value:
        raise ConfigError(f"{key} is empty")
    return value


def _disks(raw):
    mounts = tuple(m.strip() for m in raw.split(",") if m.strip())
    for mount in mounts:
        if not mount.startswith("/"):
            raise ConfigError(f"WARDEN_DISKS: {mount!r} is not an absolute path")
    if len(set(mounts)) != len(mounts):
        raise ConfigError("WARDEN_DISKS lists a mount twice")
    return mounts


def _network(key, value):
    try:
        return str(ipaddress.IPv4Network(value))
    except ValueError as e:
        raise ConfigError(f"{key}: {value!r} is not an IPv4 network") from e


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


def _wans(raw, caps_raw):
    """name=mwan3-interface[/device],… in failover order; caps as name=gigabytes,…"""
    caps = {}
    for name, value in _pairs("WARDEN_WAN_CAPS", caps_raw) if caps_raw.strip() else []:
        if not value.isdigit() or int(value) == 0:
            raise ConfigError(f"WARDEN_WAN_CAPS: {name} needs a whole number of gigabytes, got {value!r}")
        caps[name] = int(value) * 10**9
    links = []
    for name, value in _pairs("WARDEN_WANS", raw):
        iface, _, device = value.partition("/")
        for part in (iface, device) if device else (iface,):
            if not IFACE.match(part):
                raise ConfigError(f"WARDEN_WANS: {name} has an invalid interface {part!r}")
        links.append(Link(name, iface, device or None, caps.pop(name, None)))
    if caps:
        raise ConfigError(f"WARDEN_WAN_CAPS names unknown links: {', '.join(sorted(caps))}")
    return tuple(links)


def _preferences(raw, names):
    """ip=link,…: the link a device tries first."""
    out = []
    for item in (i.strip() for i in raw.split(",") if i.strip()):
        ip, sep, name = item.partition("=")
        if not sep or name.strip() not in names:
            raise ConfigError(f"WARDEN_WAN_PREFERENCES: expected ip=known-link, got {item!r}")
        out.append((_ipv4("WARDEN_WAN_PREFERENCES", ip.strip()), name.strip()))
    return tuple(out)
