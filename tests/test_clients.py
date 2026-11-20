from warden.modules.clients import Clients

LEASES = """\
1795208915 02:00:00:00:00:02 192.0.2.2 eap225 01:3c
1795208907 02:00:00:00:00:01 192.0.2.4 devil *
1795203817 02:00:00:00:00:03 192.0.2.204 * *
1795203817 02:00:00:00:00:04 192.0.2.30 oneplus *
garbage
"""

ARP = """\
IP address       HW type     Flags       HW address            Mask     Device
192.0.2.4      0x1         0x2         02:00:00:00:00:01     *        br-lan
192.0.2.204    0x1         0x2         02:00:00:00:00:03     *        br-lan
192.0.2.2      0x1         0x2         02:00:00:00:00:02     *        br-lan
192.0.2.30     0x1         0x0         00:00:00:00:00:00     *        br-lan
198.51.100.1     0x1         0x2         02:00:00:00:00:05     *        lan2
"""


def test_leased_devices_with_a_neighbour_entry_sorted_by_address(agent, tmp_path):
    (tmp_path / "leases").write_text(LEASES)
    (tmp_path / "arp").write_text(ARP)
    state = Clients(agent, leases=tmp_path / "leases", arp=tmp_path / "arp").state()
    assert state == {
        "online": [
            {"name": "eap225", "ip": "192.0.2.2"},
            {"name": "devil", "ip": "192.0.2.4"},
            {"name": None, "ip": "192.0.2.204"},
        ]
    }
    assert "02:00" not in str(state)


def test_missing_files_mean_no_devices(agent, tmp_path):
    assert Clients(agent, leases=tmp_path / "x", arp=tmp_path / "y").state() == {"online": []}
