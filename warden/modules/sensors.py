import re
from pathlib import Path

INPUT = re.compile(r"^temp(\d+)_input$")


class Sensors:
    """Temperatures in °C from hwmon, grouped by chip; duplicate chip names get -2, -3 suffixes."""

    name = "sensors"
    commands = ()

    def __init__(self, ctx, hwmon=Path("/sys/class/hwmon")):
        self._ctx = ctx
        self._hwmon = Path(hwmon)

    def handle(self, command, source):
        raise ValueError(f"sensors has no command {command!r}")

    def state(self):
        chips = {}
        for chip in sorted(self._hwmon.glob("hwmon*"), key=lambda p: int(p.name[5:]) if p.name[5:].isdigit() else -1):
            readings = self._readings(chip)
            if not readings:
                continue
            base = _read(chip / "name") or chip.name
            key, n = base, 2
            while key in chips:
                key, n = f"{base}-{n}", n + 1
            chips[key] = readings
        return {"temps": chips}

    def _readings(self, chip):
        inputs = sorted(
            (int(m.group(1)), p) for p in chip.glob("temp*_input") if (m := INPUT.match(p.name)) is not None
        )
        out = {}
        for index, path in inputs:
            raw = _read(path)
            try:
                millideg = int(raw)
            except (TypeError, ValueError):
                continue
            if not -50_000 <= millideg <= 200_000:
                continue
            base = _read(chip / f"temp{index}_label") or f"temp{index}"
            label, n = base, 2
            while label in out:
                label, n = f"{base}-{n}", n + 1
            out[label] = round(millideg / 1000, 1)
        return out


def _read(path):
    try:
        return path.read_text().strip() or None
    except OSError:
        return None
