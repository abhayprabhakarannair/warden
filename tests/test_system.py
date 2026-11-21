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
    state = System(agent, proc=proc, kernel="7.3.5", disks=()).state()
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


class Vfs:
    def __init__(self, blocks, bfree, bavail, frsize=4096):
        self.f_blocks, self.f_bfree, self.f_bavail, self.f_frsize = blocks, bfree, bavail, frsize


def test_disks_report_like_df(agent, proc):
    mounts = {"/": Vfs(1000, 300, 250), "/home": Vfs(2000, 2000, 2000)}
    state = System(agent, proc=proc, kernel="k", disks=("/", "/home"), statvfs=mounts.__getitem__).state()
    assert state["disks"] == [
        {"mount": "/", "used_pct": 73.7, "size_bytes": 4096000},
        {"mount": "/home", "used_pct": 0.0, "size_bytes": 8192000},
    ]


def test_unreadable_and_empty_mounts_are_skipped(agent, proc):
    def statvfs(mount):
        if mount == "/gone":
            raise FileNotFoundError(mount)
        return Vfs(0, 0, 0)

    state = System(agent, proc=proc, kernel="k", disks=("/gone", "/proc"), statvfs=statvfs).state()
    assert "disks" not in state
