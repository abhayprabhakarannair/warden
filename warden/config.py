import os
import re
from dataclasses import dataclass
from pathlib import Path

NAME = re.compile(r"^[a-z0-9_-]{1,31}$")
KNOWN_MODULES = ("power", "system", "sensors")


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

    modules = tuple(m.strip() for m in env.get("WARDEN_MODULES", ",".join(KNOWN_MODULES)).split(",") if m.strip())
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
    )
