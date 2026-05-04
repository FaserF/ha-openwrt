"""Tests for MAC vendor information helper."""

import pytest
from custom_components.openwrt.helpers.mac_vendor import get_mac_vendor_info

@pytest.mark.parametrize(
    "mac,expected",
    [
        ("D4:AD:FC:2C:08:0D", ("Govee", "Smart IoT Device")),
        ("18:65:71:FF:9E:A9", ("Philips/AOC", "Smart TV/Monitor")),
        ("00:33:7A:91:3D:09", ("Tuya", "Tuya IoT device")),
        ("00:60:34:0A:D5:0D", ("Bosch", "Bosch Device")),
        ("1C:69:7A:F1:FD:7B", ("Elitegroup", "Computing Device")),
        ("4C:66:A6:A1:B0:2F", ("Samsung", "Samsung Device")),
        ("8C:B8:4A:1C:43:22", ("Samsung", "Samsung Device")),
        ("10:96:93:BC:EF:22", ("Amazon", "Amazon Device")),
        ("18:83:BF:D4:5F:DE", ("Arcadyan", "Networking Device")),
        ("20:2B:20:1D:11:4F", ("Xiaomi/Foxconn", "Xiaomi Device")),
        ("34:51:80:D0:44:29", ("TCL", "TCL Smart TV")),
        ("38:00:25:12:07:FA", ("Intel", "Network/Computing Device")),
        ("BC:E9:2F:9D:FF:9C", ("HP", "HP Computer")),
        ("DC:ED:83:DC:D4:82", ("Xiaomi", "Xiaomi Device")),
        ("F8:81:1A:01:22:54", ("Somfy", "Smart Home Device")),
        ("DC:B5:4F:21:CA:2E", ("Apple", "Apple Device")),
    ],
)
def test_get_mac_vendor_info_new_mappings(mac, expected):
    """Test that new MAC OUI mappings return correct manufacturer and model."""
    assert get_mac_vendor_info(mac) == expected

def test_get_mac_vendor_info_normalized():
    """Test that MAC addresses are correctly normalized before OUI lookup."""
    # Test lowercase
    assert get_mac_vendor_info("d4:ad:fc:00:00:00") == ("Govee", "Smart IoT Device")
    # Test dashes
    assert get_mac_vendor_info("D4-AD-FC-00-00-00") == ("Govee", "Smart IoT Device")

def test_get_mac_vendor_info_randomized():
    """Test that randomized/private MAC addresses are identified."""
    # x2:xx...
    assert get_mac_vendor_info("02:00:00:00:00:00") == ("Private MAC", "Randomized Address")
    # x6:xx...
    assert get_mac_vendor_info("46:00:00:00:00:00") == ("Private MAC", "Randomized Address")
    # xA:xx...
    assert get_mac_vendor_info("AA:00:00:00:00:00") == ("Private MAC", "Randomized Address")
    # xE:xx...
    assert get_mac_vendor_info("EE:00:00:00:00:00") == ("Private MAC", "Randomized Address")
