"""Test the OpenWrt sensor platform."""

from __future__ import annotations

from unittest.mock import MagicMock

from homeassistant.const import UnitOfTime

from custom_components.openwrt.api.base import OpenWrtData, SystemResources
from custom_components.openwrt.sensor import OpenWrtSensorEntity, _get_system_sensors


def test_uptime_conversion() -> None:
    """Test that uptime uses seconds for HA duration formatting."""
    data = OpenWrtData(
        system_resources=SystemResources(
            uptime=120,
            memory_total=1000,
            memory_used=500,
            load_1min=0.1,
        ),
        connected_devices=[],
        network_interfaces=[],
        wireless_interfaces=[],
    )

    coordinator = MagicMock()
    coordinator.data = data
    entry = MagicMock()
    entry.entry_id = "test"

    # Find uptime description
    uptime_desc = next(d for d in _get_system_sensors() if d.key == "uptime")

    # Check description
    assert uptime_desc.native_unit_of_measurement == UnitOfTime.SECONDS

    # Check value via entity
    sensor = OpenWrtSensorEntity(coordinator, entry, uptime_desc)
    assert sensor.native_value == 120


def test_sensor_english_names() -> None:
    """Test that system sensors have explicit English names."""
    # Check some key sensors in _get_system_sensors()
    system_sensors = _get_system_sensors()
    memory_usage = next(d for d in system_sensors if d.key == "memory_usage")
    assert memory_usage.name == "Memory Usage"

    load_sensor = next(d for d in system_sensors if d.key == "load_1min")
    assert load_sensor.name == "System Load (1m)"

    uptime_sensor = next(d for d in system_sensors if d.key == "uptime")
    assert uptime_sensor.name == "Uptime"


def test_wifi_sensor_ap_mode_suppression() -> None:
    """Test that signal sensors are suppressed for AP mode interfaces."""
    from custom_components.openwrt.sensor import _create_wifi_sensors

    coordinator = MagicMock()
    entry = MagicMock()
    entry.entry_id = "test"

    # Test AP mode
    sensors_ap = _create_wifi_sensors(coordinator, entry, "wlan0", "TestSSID", "ap")
    # Should only have Clients, Channel, TX Power, HT Mode, Hardware Mode
    # Signal, Quality, Bitrate, Noise should be missing
    keys_ap = [s.entity_description.key for s in sensors_ap]
    assert "wifi_wlan0_clients" in keys_ap
    assert "wifi_wlan0_channel" in keys_ap
    assert "wifi_wlan0_signal" not in keys_ap
    assert "wifi_wlan0_quality" not in keys_ap
    assert "wifi_wlan0_bitrate" not in keys_ap

    # Test STA mode
    sensors_sta = _create_wifi_sensors(coordinator, entry, "wlan1", "TestSSID", "sta")
    keys_sta = [s.entity_description.key for s in sensors_sta]
    assert "wifi_wlan1_clients" in keys_sta
    assert "wifi_wlan1_signal" in keys_sta
    assert "wifi_wlan1_quality" in keys_sta
    assert "wifi_wlan1_bitrate" in keys_sta
