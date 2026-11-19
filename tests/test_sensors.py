import pytest

from warden.modules.sensors import Sensors


def chip(root, index, name, temps, labels=None):
    d = root / f"hwmon{index}"
    d.mkdir()
    if name is not None:
        (d / "name").write_text(name + "\n")
    for n, value in temps.items():
        (d / f"temp{n}_input").write_text(f"{value}\n")
    for n, label in (labels or {}).items():
        (d / f"temp{n}_label").write_text(label + "\n")
    return d


def temps(agent, root):
    return Sensors(agent, hwmon=root).state()["temps"]


def test_labelled_and_unlabelled_readings(agent, tmp_path):
    chip(tmp_path, 0, "k10temp", {1: 45250, 3: 51000}, {1: "Tctl"})
    assert temps(agent, tmp_path) == {"k10temp": {"Tctl": 45.2, "temp3": 51.0}}


def test_duplicate_chip_names_are_suffixed_in_index_order(agent, tmp_path):
    chip(tmp_path, 10, "nvme", {1: 40000})
    chip(tmp_path, 2, "nvme", {1: 30000})
    chip(tmp_path, 3, "nvme", {1: 35000})
    assert temps(agent, tmp_path) == {"nvme": {"temp1": 30.0}, "nvme-2": {"temp1": 35.0}, "nvme-3": {"temp1": 40.0}}


def test_duplicate_labels_are_suffixed(agent, tmp_path):
    chip(tmp_path, 0, "amdgpu", {1: 50000, 2: 60000}, {1: "edge", 2: "edge"})
    assert temps(agent, tmp_path) == {"amdgpu": {"edge": 50.0, "edge-2": 60.0}}


def test_chip_without_name_uses_directory(agent, tmp_path):
    chip(tmp_path, 4, None, {1: 20000})
    assert temps(agent, tmp_path) == {"hwmon4": {"temp1": 20.0}}


@pytest.mark.parametrize("value", ["", "abc", "-60000", "250000", "4.5"])
def test_invalid_or_implausible_values_are_skipped(agent, tmp_path, value):
    d = chip(tmp_path, 0, "acpitz", {1: 30000})
    (d / "temp2_input").write_text(value)
    assert temps(agent, tmp_path) == {"acpitz": {"temp1": 30.0}}


def test_negative_and_boundary_values(agent, tmp_path):
    chip(tmp_path, 0, "probe", {1: -50000, 2: 200000, 3: -1500})
    assert temps(agent, tmp_path) == {"probe": {"temp1": -50.0, "temp2": 200.0, "temp3": -1.5}}


def test_chips_without_temperatures_are_omitted(agent, tmp_path):
    chip(tmp_path, 0, "nct6798", {})
    (tmp_path / "hwmon0" / "fan1_input").write_text("900\n")
    assert temps(agent, tmp_path) == {}


def test_unnumbered_and_unreadable_inputs_are_skipped(agent, tmp_path):
    d = chip(tmp_path, 0, "odd", {1: 25000})
    (d / "temp_input").write_text("99000\n")
    (d / "temp7_input").mkdir()
    assert temps(agent, tmp_path) == {"odd": {"temp1": 25.0}}


def test_missing_hwmon_directory(agent, tmp_path):
    assert temps(agent, tmp_path / "absent") == {}


def test_numeric_ordering_of_inputs(agent, tmp_path):
    chip(tmp_path, 0, "cpu", {10: 10000, 2: 2000, 1: 1000})
    assert list(temps(agent, tmp_path)["cpu"]) == ["temp1", "temp2", "temp10"]
