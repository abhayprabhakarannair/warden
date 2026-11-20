import pytest

from warden.config import DEFAULT_MODULES, ConfigError, load


@pytest.fixture
def env(tmp_path):
    secret = tmp_path / "password"
    secret.write_text("s3cret\n")
    return {"WARDEN_MQTT_HOST": "bus.example", "WARDEN_MQTT_USER": "devil", "WARDEN_MQTT_PASSWORD_FILE": str(secret)}


def test_defaults(env):
    c = load(env)
    assert (c.host, c.port, c.user, c.password, c.topic) == ("bus.example", 8443, "devil", "s3cret", "home/devil")
    assert c.modules == DEFAULT_MODULES
    assert (c.grace_s, c.state_every_s, c.dry_run) == (10, 30, False)


def test_overrides(env):
    env |= {
        "WARDEN_MQTT_PORT": "1883",
        "WARDEN_TOPIC": "lab/devil/",
        "WARDEN_MODULES": " power , system ",
        "WARDEN_GRACE_S": "0",
        "WARDEN_STATE_EVERY_S": "5",
        "WARDEN_DRY_RUN": "1",
    }
    c = load(env)
    assert (c.port, c.topic, c.modules, c.grace_s, c.state_every_s, c.dry_run) == (
        1883,
        "lab/devil",
        ("power", "system"),
        0,
        5,
        True,
    )


@pytest.mark.parametrize("key", ["WARDEN_MQTT_HOST", "WARDEN_MQTT_USER", "WARDEN_MQTT_PASSWORD_FILE"])
@pytest.mark.parametrize("value", [None, "", "   "])
def test_required(env, key, value):
    if value is None:
        del env[key]
    else:
        env[key] = value
    with pytest.raises(ConfigError, match=key):
        load(env)


@pytest.mark.parametrize("user", ["Devil", "dev il", "a" * 32, "dev/il", "dévil"])
def test_invalid_user(env, user):
    env["WARDEN_MQTT_USER"] = user
    with pytest.raises(ConfigError, match="not a valid client name"):
        load(env)


def test_missing_password_file(env, tmp_path):
    env["WARDEN_MQTT_PASSWORD_FILE"] = str(tmp_path / "absent")
    with pytest.raises(ConfigError, match="cannot read"):
        load(env)


def test_empty_password_file(env, tmp_path):
    (tmp_path / "password").write_text(" \n")
    with pytest.raises(ConfigError, match="empty"):
        load(env)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("WARDEN_MQTT_PORT", "0"),
        ("WARDEN_MQTT_PORT", "65536"),
        ("WARDEN_MQTT_PORT", "-1"),
        ("WARDEN_MQTT_PORT", "80a"),
        ("WARDEN_GRACE_S", "3601"),
        ("WARDEN_STATE_EVERY_S", "0"),
    ],
)
def test_integer_bounds(env, key, value):
    env[key] = value
    with pytest.raises(ConfigError, match=key):
        load(env)


@pytest.mark.parametrize(
    ("value", "message"),
    [("power,ups", "unknown modules: ups"), ("power,power", "twice"), ("", "no modules"), (" , ", "no modules")],
)
def test_modules(env, value, message):
    env["WARDEN_MODULES"] = value
    with pytest.raises(ConfigError, match=message):
        load(env)


@pytest.mark.parametrize("value", ["yes", "true", "2"])
def test_dry_run_is_strict(env, value):
    env["WARDEN_DRY_RUN"] = value
    with pytest.raises(ConfigError, match="WARDEN_DRY_RUN"):
        load(env)


def test_router_modules(env):
    env |= {
        "WARDEN_MODULES": "system,wake,wan,clients",
        "WARDEN_WAKE_TARGETS": "devil=02:00:00:00:0A:01@192.0.2.4, nas=aa:bb:cc:dd:ee:ff@192.0.2.9",
        "WARDEN_WAKE_BROADCAST": "192.0.2.255",
        "WARDEN_WANS": "bsnl=wan_bsnl,railwire=wan_railwire",
    }
    c = load(env)
    assert [(t.name, t.mac, t.ip) for t in c.wake_targets] == [
        ("devil", "02:00:00:00:0a:01", "192.0.2.4"),
        ("nas", "aa:bb:cc:dd:ee:ff", "192.0.2.9"),
    ]
    assert c.wake_broadcast == "192.0.2.255"
    assert c.wans == (("bsnl", "wan_bsnl"), ("railwire", "wan_railwire"))


def test_module_settings_are_only_required_when_enabled(env):
    assert load(env).wake_targets == ()


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"WARDEN_WAKE_TARGETS": None}, "WARDEN_WAKE_TARGETS is required"),
        ({"WARDEN_WAKE_BROADCAST": None}, "WARDEN_WAKE_BROADCAST is required"),
        ({"WARDEN_WAKE_TARGETS": "devil=02:00:00:00:01@192.0.2.4"}, "mac@ip"),
        ({"WARDEN_WAKE_TARGETS": "devil=02:00:00:00:00:01"}, "mac@ip"),
        ({"WARDEN_WAKE_TARGETS": "devil=02:00:00:00:00:01@192.0.2.400"}, "not an IPv4"),
        ({"WARDEN_WAKE_TARGETS": "Devil=02:00:00:00:00:01@192.0.2.4"}, "name=value"),
        ({"WARDEN_WAKE_TARGETS": "a=02:00:00:00:00:01@1.2.3.4,a=02:00:00:00:00:02@1.2.3.5"}, "twice"),
        ({"WARDEN_WAKE_BROADCAST": "everyone"}, "not an IPv4"),
    ],
)
def test_bad_wake_settings(env, extra, message):
    env |= {
        "WARDEN_MODULES": "wake",
        "WARDEN_WAKE_TARGETS": "devil=02:00:00:00:00:01@192.0.2.4",
        "WARDEN_WAKE_BROADCAST": "192.0.2.255",
    }
    for key, value in extra.items():
        if value is None:
            del env[key]
        else:
            env[key] = value
    with pytest.raises(ConfigError, match=message):
        load(env)


@pytest.mark.parametrize("wans", ["bsnl", "bsnl=wan bsnl", "bsnl=a_very_long_interface_name"])
def test_bad_wans(env, wans):
    env |= {"WARDEN_MODULES": "wan", "WARDEN_WANS": wans}
    with pytest.raises(ConfigError, match="WARDEN_WANS"):
        load(env)
