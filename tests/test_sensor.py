"""Test the OpenWrt sensor platform."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

from homeassistant.components.sensor import SensorDeviceClass

from custom_components.openwrt.api.base import OpenWrtData, SystemResources
from custom_components.openwrt.sensor import OpenWrtSensorEntity, _get_system_sensors


def test_uptime_conversion() -> None:
    """Test that uptime uses timestamp for HA formatting."""
    now = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
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
        boot_time=now - timedelta(seconds=120),
    )

    coordinator = MagicMock()
    coordinator.data = data
    entry = MagicMock()
    entry.entry_id = "test"

    # Find uptime description
    uptime_desc = next(d for d in _get_system_sensors() if d.key == "uptime")

    # Check description
    assert uptime_desc.device_class == SensorDeviceClass.TIMESTAMP

    # Check value via entity
    with patch("custom_components.openwrt.sensor.dt_util.utcnow", return_value=now):
        sensor = OpenWrtSensorEntity(coordinator, entry, uptime_desc)
        assert sensor.native_value == now - timedelta(seconds=120)


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


def test_device_sensor_case_insensitivity() -> None:
    """Test that device diagnostic sensors correctly match MAC addresses case-insensitively."""
    from custom_components.openwrt.api.base import ConnectedDevice
    from custom_components.openwrt.sensor import _create_device_sensors

    # Device has lowercased MAC
    device = ConnectedDevice(
        mac="aa:bb:cc:dd:ee:ff",
        is_wireless=True,
        rx_rate=120100,
        tx_rate=86600,
        signal=-50,
        noise=-95,
    )

    coordinator = MagicMock()
    # Data has device with uppercase MAC
    coordinator.data = OpenWrtData(
        connected_devices=[
            ConnectedDevice(
                mac="AA:BB:CC:DD:EE:FF",
                is_wireless=True,
                rx_rate=120100,
                tx_rate=86600,
                signal=-50,
                noise=-95,
            )
        ]
    )
    # Mock coordinator update status
    coordinator.last_update_success = True
    entry = MagicMock()
    entry.entry_id = "test"

    sensors = _create_device_sensors(coordinator, entry, device)

    # We should have 4 sensors (signal, rx_rate, tx_rate, noise)
    assert len(sensors) == 4

    # Check that they are available and return the correct value despite case difference
    for sensor in sensors:
        assert sensor.available is True
        if "signal" in sensor.entity_description.key:
            assert sensor.native_value == -50
        elif "rx_rate" in sensor.entity_description.key:
            assert sensor.native_value == 120.1
        elif "tx_rate" in sensor.entity_description.key:
            assert sensor.native_value == 86.6
        elif "noise" in sensor.entity_description.key:
            assert sensor.native_value == -95


def test_assoc_rate_robustness() -> None:
    """Test that _get_assoc_rate successfully parses multiple nested formats from iwinfo and hostapd."""
    from custom_components.openwrt.api.base import OpenWrtClient

    # 1. Hostapd nested "rate" dict (in Kbps)
    assert OpenWrtClient._get_assoc_rate(None, {"rate": {"rx": 866700}}, "rx") == 866700

    # 2. Iwinfo direction dict containing "rate" (in Kbps)
    assert OpenWrtClient._get_assoc_rate(None, {"rx": {"rate": 120100}}, "rx") == 120100

    # 3. Hostapd legacy/tenths-of-Mbps "rx_rate" or "tx_rate" containing "rate" dict (converted to Kbps)
    assert (
        OpenWrtClient._get_assoc_rate(None, {"rx_rate": {"rate": 8660}}, "rx") == 866000
    )

    # 4. Hostapd legacy/tenths-of-Mbps "rx_rate" as int (converted to Kbps)
    assert OpenWrtClient._get_assoc_rate(None, {"rx_rate": 8660}, "rx") == 866000

    # 5. Fallback/Direct number
    assert OpenWrtClient._get_assoc_rate(None, {"rx": 120100}, "rx") == 120100


def test_wifi_sensor_section_and_ifname_matching() -> None:
    """Test that wifi sensors match values using section or ifname when name differs."""
    from custom_components.openwrt.api.base import WirelessInterface
    from custom_components.openwrt.sensor import _create_wifi_sensors

    coordinator = MagicMock()
    entry = MagicMock()
    entry.entry_id = "test"

    wifi_iface = WirelessInterface(
        name="phy0-ap0",
        section="default_radio0",
        ifname="phy0-ap0",
        ssid="MyNet",
        mode="ap",
        channel=1,
        txpower=18,
    )
    coordinator.data = OpenWrtData(wireless_interfaces=[wifi_iface])

    # Create wifi sensors initialized with iface_name="default_radio0" and section_id="default_radio0"
    sensors = _create_wifi_sensors(
        coordinator,
        entry,
        iface_name="default_radio0",
        ssid="MyNet",
        mode="ap",
        frequency="2.4 GHz",
        section_id="default_radio0",
        ifname="phy0-ap0",
    )

    # Verify txpower diagnostic sensor reads txpower correctly via section fallback
    tx_sensor = next(s for s in sensors if "txpower" in s.entity_description.key)
    assert tx_sensor.native_value == 18

    # Verify channel sensor reads channel correctly via section fallback
    channel_sensor = next(s for s in sensors if "channel" in s.entity_description.key)
    assert channel_sensor.native_value == 1
