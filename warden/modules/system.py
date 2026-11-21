import os
from pathlib import Path


class System:
    name = "system"
    commands = ()

    def __init__(self, ctx, proc=Path("/proc"), kernel=None, disks=("/",), statvfs=os.statvfs):
        self._ctx = ctx
        self._proc = Path(proc)
        self._kernel = kernel or os.uname().release
        self._disks = tuple(disks)
        self._statvfs = statvfs

    def handle(self, command, args, source):
        raise ValueError(f"system has no command {command!r}")

    def state(self):
        out = {"kernel": self._kernel}
        uptime = self._uptime()
        if uptime is not None:
            out["uptime_s"] = int(uptime)
            out["boot_time"] = int(self._ctx.wall() - uptime)
        load = self._load()
        if load is not None:
            out["load"] = load
        mem = self._mem_used_pct()
        if mem is not None:
            out["mem_used_pct"] = mem
        disks = [d for d in map(self._disk, self._disks) if d]
        if disks:
            out["disks"] = disks
        return out

    def _disk(self, mount):
        """Usage as df reports it: blocks reserved for root count as neither used nor available."""
        try:
            st = self._statvfs(mount)
        except OSError:
            return None
        used = st.f_blocks - st.f_bfree
        if st.f_blocks <= 0 or used < 0 or used + st.f_bavail <= 0:
            return None
        return {
            "mount": mount,
            "used_pct": round(100 * used / (used + st.f_bavail), 1),
            "size_bytes": st.f_blocks * st.f_frsize,
        }

    def _read(self, name):
        try:
            return (self._proc / name).read_text()
        except OSError:
            return None

    def _uptime(self):
        text = self._read("uptime")
        try:
            return float(text.split()[0]) if text else None
        except (ValueError, IndexError):
            return None

    def _load(self):
        text = self._read("loadavg")
        try:
            return [round(float(x), 2) for x in text.split()[:3]] if text else None
        except ValueError:
            return None

    def _mem_used_pct(self):
        text = self._read("meminfo")
        if not text:
            return None
        fields = {}
        for line in text.splitlines():
            key, _, rest = line.partition(":")
            parts = rest.split()
            if parts and parts[0].isdigit():
                fields[key] = int(parts[0])
        total, available = fields.get("MemTotal"), fields.get("MemAvailable")
        if not total or available is None or available > total:
            return None
        return round(100 * (total - available) / total, 1)
