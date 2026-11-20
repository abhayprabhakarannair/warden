import pytest

from warden.modules.system import System

MEMINFO = "MemTotal:       16000000 kB\nMemFree:         1000000 kB\nMemAvailable:    4000000 kB\n"


@pytest.fixture
def proc(tmp_path):
    (tmp_path / "uptime").write_text("3600.42 7000.00\n")
    (tmp_path / "loadavg").write_text("0.52 0.31 0.204 1/900 1234\n")
    (tmp_path / "meminfo").write_text(MEMINFO)
    return tmp_path


def test_full_state(agent, clock, proc):
    state = System(agent, proc=proc, kernel="7.3.5").state()
    assert state == {
        "kernel": "7.3.5",
        "uptime_s": 3600,
        "boot_time": int(clock.wall() - 3600.42),
        "load": [0.52, 0.31, 0.2],
        "mem_used_pct": 75.0,
    }


@pytest.mark.parametrize("missing", ["uptime", "loadavg", "meminfo"])
def test_missing_files_drop_only_their_fields(agent, proc, missing):
    (proc / missing).unlink()
    state = System(agent, proc=proc, kernel="k").state()
    dropped = {"uptime": {"uptime_s", "boot_time"}, "loadavg": {"load"}, "meminfo": {"mem_used_pct"}}[missing]
    assert dropped.isdisjoint(state)
    assert {"kernel", "uptime_s", "load", "mem_used_pct"} - dropped <= set(state) | {"boot_time"}


@pytest.mark.parametrize(
    ("name", "content", "field"),
    [
        ("uptime", "", "uptime_s"),
        ("uptime", "abc", "uptime_s"),
        ("loadavg", "x y z", "load"),
        ("meminfo", "MemTotal: 0 kB\nMemAvailable: 0 kB\n", "mem_used_pct"),
        ("meminfo", "MemTotal: 100 kB\n", "mem_used_pct"),
        ("meminfo", "MemTotal: 100 kB\nMemAvailable: 200 kB\n", "mem_used_pct"),
        ("meminfo", "garbage\n", "mem_used_pct"),
    ],
)
def test_garbage_is_ignored(agent, proc, name, content, field):
    (proc / name).write_text(content)
    assert field not in System(agent, proc=proc, kernel="k").state()


def test_has_no_commands(agent, proc):
    with pytest.raises(ValueError):
        System(agent, proc=proc, kernel="k").handle("anything", (), "x")
