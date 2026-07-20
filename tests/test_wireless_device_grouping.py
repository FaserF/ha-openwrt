"""Tests for wireless interface discovery and Access Point device grouping."""

from unittest.mock import MagicMock, patch

import pytest

from custom_components.openwrt.api.ubus import UbusClient
from custom_components.openwrt.const import DOMAIN
from custom_components.openwrt.coordinator import OpenWrtDataCoordinator

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ubus_client() -> UbusClient:
    """Return a minimal UbusClient for testing."""
    client = UbusClient(
        MagicMock(), MagicMock(), host="192.168.1.1", username="root", password="secret"
    )
    client._session_id = "test-token"
    client.packages.wireless = True
    return client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VELOP_WIRELESS_STATUS = {
    "radio0": {
        "up": True,
        "disabled": False,
        "config": {"htmode": "VHT80", "hwmode": "11a", "txpower": 23},
        "interfaces": [
            {
                "section": "default_radio0",
                "ifname": "phy0-ap0",
                "config": {"mode": "ap", "ssid": "MyNet-Guest", "encryption": "psk2"},
            }
        ],
    },
    "radio1": {
        "up": True,
        "disabled": False,
        "config": {"htmode": "HT40", "hwmode": "11g", "txpower": 20},
        "interfaces": [
            {
                "section": "default_radio1",
                "ifname": "phy1-ap0",
                "config": {"mode": "ap", "ssid": "MyNet", "encryption": "psk2"},
            },
            {
                "section": "wifinet1",
                "ifname": "phy1-ap1",
                "config": {"mode": "ap", "ssid": "MyNet-Guest", "encryption": "psk2"},
            },
            {
                "section": "wifinet2",
                "ifname": "phy1-ap2",
                "config": {"mode": "ap", "ssid": "MyNet-IoT", "encryption": "psk2"},
            },
        ],
    },
    "radio2": {
        "up": True,
        "disabled": False,
        "config": {"htmode": "VHT80", "hwmode": "11a", "txpower": 23},
        "interfaces": [
            {
                "section": "default_radio2",
                "ifname": "phy2-ap0",
                "config": {"mode": "ap", "ssid": "MyNet", "encryption": "psk2"},
            }
        ],
    },
}

VELOP_IWINFO_DEVICES = {
    "devices": ["phy1-ap1", "phy0-ap0", "phy1-ap2", "phy1-ap0", "phy2-ap0"]
}

IWINFO_INFO = {
    "phy0-ap0": {
        "ssid": "MyNet-Guest",
        "bssid": "AA:BB:CC:DD:EE:01",
        "channel": 149,
        "frequency": 5745,
    },
    "phy1-ap0": {
        "ssid": "MyNet",
        "bssid": "AA:BB:CC:DD:EE:02",
        "channel": 11,
        "frequency": 2462,
    },
    "phy1-ap1": {
        "ssid": "MyNet-Guest",
        "bssid": "AA:BB:CC:DD:EE:03",
        "channel": 11,
        "frequency": 2462,
    },
    "phy1-ap2": {
        "ssid": "MyNet-IoT",
        "bssid": "AA:BB:CC:DD:EE:04",
        "channel": 11,
        "frequency": 2462,
    },
    "phy2-ap0": {
        "ssid": "MyNet",
        "bssid": "AA:BB:CC:DD:EE:05",
        "channel": 36,
        "frequency": 5180,
    },
}


async def _mock_call(obj: str, method: str, params: dict | None = None):
    """Simulate ubus _call responses for the Velop scenario."""
    if obj == "network.wireless" and method == "status":
        return VELOP_WIRELESS_STATUS
    if obj == "iwinfo" and method == "devices":
        return VELOP_IWINFO_DEVICES
    if obj == "iwinfo" and method == "info":
        device = (params or {}).get("device", "")
        return IWINFO_INFO.get(device, {})
    if obj == "iwinfo" and method == "assoclist":
        return {"results": []}
    return {}


# ---------------------------------------------------------------------------
# discovery Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_duplicate_interfaces_phy_ap_naming(ubus_client: UbusClient):
    """Each phy*-ap* interface must appear exactly once in the result list."""
    with patch.object(ubus_client, "_call", side_effect=_mock_call):
        interfaces = await ubus_client.get_wireless_interfaces()

    names = [w.name for w in interfaces]
    assert "phy0-ap0" in names
    assert "phy1-ap0" in names
    assert "phy1-ap1" in names
    assert "phy1-ap2" in names
    assert "phy2-ap0" in names

    assert len(names) == len(set(names))

    section_names = {
        "default_radio0",
        "default_radio1",
        "default_radio2",
        "wifinet1",
        "wifinet2",
    }
    for name in names:
        assert name not in section_names


@pytest.mark.asyncio
async def test_interfaces_have_correct_ssid(ubus_client: UbusClient):
    """Every interface must carry the correct SSID."""
    with patch.object(ubus_client, "_call", side_effect=_mock_call):
        interfaces = await ubus_client.get_wireless_interfaces()

    by_name = {w.name: w for w in interfaces}
    assert by_name["phy0-ap0"].ssid == "MyNet-Guest"
    assert by_name["phy1-ap0"].ssid == "MyNet"
    assert by_name["phy1-ap1"].ssid == "MyNet-Guest"
    assert by_name["phy1-ap2"].ssid == "MyNet-IoT"
    assert by_name["phy2-ap0"].ssid == "MyNet"


# ---------------------------------------------------------------------------
# Grouping Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_coordinator_stable_id_uses_band(ubus_client: UbusClient):
    """Coordinator must produce one stable_id per SSID+band, not per SSID+MHz channel."""
    with patch.object(ubus_client, "_call", side_effect=_mock_call):
        interfaces = await ubus_client.get_wireless_interfaces()

    ap_stable_ids: set[str] = set()
    for wifi in interfaces:
        if not wifi.name or not wifi.ssid:
            continue
        band = wifi.band or wifi.frequency or wifi.radio or "unknown"
        stable_id = f"{wifi.ssid}_{band}"
        ap_stable_ids.add(stable_id)

    assert len(ap_stable_ids) == 5
    expected_bands = {
        "MyNet-Guest_5 GHz",
        "MyNet-Guest_2.4 GHz",
        "MyNet_2.4 GHz",
        "MyNet_5 GHz",
        "MyNet-IoT_2.4 GHz",
    }
    assert ap_stable_ids == expected_bands


def test_ap_stable_id_consistency() -> None:
    """Test that AP devices use grouped SSID_Band as stable_id."""
    from custom_components.openwrt.sensor import OpenWrtWifiSensorEntity

    coordinator = MagicMock()
    coordinator.router_id = "router_mac"
    coordinator.interface_to_stable_id = {"phy0-ap0": "SSID_2.4 GHz"}

    entry = MagicMock()
    entry.unique_id = "router_mac"
    entry.entry_id = "entry_id"

    description = MagicMock()
    description.key = "test_wifi"
    description.name = "Signal"

    with patch("custom_components.openwrt.sensor.DeviceInfo", side_effect=dict):
        entity = OpenWrtWifiSensorEntity(
            coordinator,
            entry,
            description,
            "phy0-ap0",
            "SSID",
            "2.4 GHz",
        )

    device_info = entity._attr_device_info
    from custom_components.openwrt.helpers import format_ap_device_id

    expected_id = format_ap_device_id("router_mac", "SSID_2.4 GHz")
    assert (DOMAIN, expected_id) in device_info["identifiers"]


@pytest.mark.asyncio
async def test_ap_deduplication_and_naming() -> None:
    """Verify AP deduplication and disambiguation naming logic."""
    from custom_components.openwrt.api.base import WirelessInterface
    from custom_components.openwrt.sensor import OpenWrtWifiSensorEntity
    from custom_components.openwrt.switch import OpenWrtWirelessSwitch

    config_entry = MagicMock()
    config_entry.options = {"update_interval": 60}
    config_entry.data = {"host": "192.168.1.1"}
    config_entry.entry_id = "test_entry"
    config_entry.unique_id = "test_router"

    coordinator = OpenWrtDataCoordinator(MagicMock(), config_entry, MagicMock())
    coordinator.data = MagicMock()
    coordinator.data.wireless_interfaces = [
        WirelessInterface(name="phy1-ap1", ssid="MyNet", frequency="2.4 GHz"),
        WirelessInterface(name="phy1-ap2", ssid="MyNet", frequency="2.4 GHz"),
    ]

    from custom_components.openwrt.helpers import format_ap_name

    ap_info = {}
    coordinator.interface_to_stable_id = {}
    for wifi in coordinator.data.wireless_interfaces:
        band = wifi.band or wifi.frequency or wifi.radio or "unknown"
        stable_id = f"{wifi.ssid}_{band}"
        ap_info[stable_id] = format_ap_name(wifi.ssid, band)
        coordinator.interface_to_stable_id[wifi.name] = stable_id

    assert len(ap_info) == 1

    desc = MagicMock()
    desc.key = "signal"
    desc.name = "Signal"
    s1 = OpenWrtWifiSensorEntity(
        coordinator, config_entry, desc, "phy1-ap1", "MyNet", "2.4 GHz"
    )
    s2 = OpenWrtWifiSensorEntity(
        coordinator, config_entry, desc, "phy1-ap2", "MyNet", "2.4 GHz"
    )
    assert "phy1-ap1" in s1._attr_name
    assert "phy1-ap2" in s2._attr_name

    sw1 = OpenWrtWirelessSwitch(
        coordinator, config_entry, MagicMock(), "phy1-ap1", "MyNet", "2.4 GHz"
    )
    sw2 = OpenWrtWirelessSwitch(
        coordinator, config_entry, MagicMock(), "phy1-ap2", "MyNet", "2.4 GHz"
    )
    assert "phy1-ap1" in sw1._attr_name
    assert "phy1-ap2" in sw2._attr_name


def test_wireless_switch_status_matching() -> None:
    """Verify OpenWrtWirelessSwitch matches status using section when iface name differs."""
    from custom_components.openwrt.api.base import WirelessInterface
    from custom_components.openwrt.switch import OpenWrtWirelessSwitch

    config_entry = MagicMock()
    config_entry.entry_id = "test_entry"
    config_entry.unique_id = "test_router"
    config_entry.options = {"update_interval": 60}
    config_entry.data = {"host": "192.168.1.1", "update_interval": 60}

    coordinator = OpenWrtDataCoordinator(MagicMock(), config_entry, MagicMock())
    coordinator.data = MagicMock()
    wifi_iface = WirelessInterface(name="wlan0", section="default_radio0", enabled=True)
    coordinator.data.wireless_interfaces = [wifi_iface]

    sw = OpenWrtWirelessSwitch(
        coordinator,
        config_entry,
        MagicMock(),
        "default_radio0",
        "MyNet",
        "2.4 GHz",
        section_id="default_radio0",
    )
    assert sw.is_on is True

    wifi_iface.enabled = False
    assert sw.is_on is False


def test_wireless_interface_band_population():
    """Verify that WirelessInterface correctly populates band from frequency or hwmode."""
    from custom_components.openwrt.api.base import WirelessInterface

    # From MHz frequency
    assert WirelessInterface(frequency="2412").band == "2.4 GHz"
    assert WirelessInterface(frequency="5180").band == "5 GHz"
    assert WirelessInterface(frequency="6100").band == "6 GHz"

    # From band string
    assert WirelessInterface(frequency="2.4 GHz").band == "2.4 GHz"

    # From hwmode
    assert WirelessInterface(hwmode="11g").band == "2.4 GHz"
    assert WirelessInterface(hwmode="11a").band == "5 GHz"
    assert WirelessInterface(hwmode="11ac").band == "5 GHz"


@pytest.mark.asyncio
async def test_coordinator_orphan_cleanup_ghost_sections(hass):
    """Verify that coordinator cleans up ghost devices with section-based identifiers."""
    from custom_components.openwrt.api.base import DeviceInfo, OpenWrtData

    config_entry = MagicMock()
    config_entry.entry_id = "test_entry"
    config_entry.unique_id = "router_mac"
    config_entry.data = {"host": "192.168.1.1"}
    config_entry.options = {}

    client = MagicMock()
    coordinator = OpenWrtDataCoordinator(hass, config_entry, client)
    coordinator.router_id = "router_mac"

    # Mock device registry
    device_registry = MagicMock()
    device_registry.devices = {}

    # 1. Create a ghost device with a section-based identifier
    ghost_id = "router_mac_ap_default_radio0"
    ghost_dev = MagicMock()
    ghost_dev.id = "ghost_dev_id"
    ghost_dev.name = "AP default_radio0"
    ghost_dev.identifiers = {(DOMAIN, ghost_id)}
    ghost_dev.config_entries = {config_entry.entry_id}
    ghost_dev.via_device_id = "router_dev_id"
    device_registry.devices["ghost_dev_id"] = ghost_dev

    # 2. Create the main router device
    router_dev = MagicMock()
    router_dev.id = "router_dev_id"
    router_dev.identifiers = {(DOMAIN, "router_mac")}
    router_dev.config_entries = {config_entry.entry_id}
    device_registry.devices["router_dev_id"] = router_dev

    device_registry.async_get_device.return_value = router_dev

    # 3. Setup coordinator data (no wireless interfaces to ensure cleanup)
    data = OpenWrtData()
    data.device_info = DeviceInfo(mac_address="router_mac")
    data.wireless_interfaces = []

    with (
        patch(
            "homeassistant.helpers.device_registry.async_get",
            return_value=device_registry,
        ),
        patch(
            "custom_components.openwrt.coordinator.format_ap_device_id",
            side_effect=lambda r, s: f"{r}_ap_{s}",
        ),
    ):
        await coordinator._async_update_device_registry(data)

    # 4. Verify ghost device was removed
    device_registry.async_remove_device.assert_called_with("ghost_dev_id")


def test_router_id_mac_formatting_prevents_duplicate_ap():
    """Verify that MAC address formatting in unique_id ensures stable router and AP device identification."""
    from custom_components.openwrt.helpers import (
        _router_id,
        format_ap_device_id,
        format_ap_name,
        format_radio_device_id,
        format_radio_name,
    )

    # 1. Test helper extraction formatting
    config_entry = MagicMock()
    config_entry.unique_id = "9483c4ac7a13"
    config_entry.data = {"host": "192.168.1.1"}

    # Extract router ID should format the MAC
    assert _router_id(config_entry) == "94:83:c4:ac:7a:13"

    # Format AP device ID should use the formatted MAC
    assert (
        format_ap_device_id(config_entry, "stable_ssid_5 GHz")
        == "94:83:c4:ac:7a:13_ap_stable_ssid_5 GHz"
    )
    assert (
        format_radio_device_id(config_entry, "radio0")
        == "94:83:c4:ac:7a:13_radio_radio0"
    )
    assert format_radio_name("radio0", "2.4 GHz") == "2.4 GHz"
    assert format_radio_name("radio0", "") == "radio0"
    assert format_ap_name("Main", "2.4 GHz") == "SSID Main"


@pytest.mark.asyncio
async def test_coordinator_preserves_active_radio_device(hass):
    """Keep a configured physical radio during registry cleanup."""
    from custom_components.openwrt.api.base import (
        DeviceInfo,
        OpenWrtData,
        WirelessInterface,
    )

    config_entry = MagicMock()
    config_entry.entry_id = "test_entry"
    config_entry.unique_id = "router_mac"
    config_entry.data = {"host": "192.0.2.1"}
    config_entry.options = {}
    coordinator = OpenWrtDataCoordinator(hass, config_entry, MagicMock())
    coordinator.router_id = "router_mac"

    router_dev = MagicMock()
    router_dev.id = "router_dev_id"
    router_dev.identifiers = {(DOMAIN, "router_mac")}

    radio_dev = MagicMock()
    radio_dev.id = "radio_dev_id"
    radio_dev.name = "Radio radio0 (2.4 GHz)"
    radio_dev.identifiers = {(DOMAIN, "router_mac_radio_radio0")}
    radio_dev.config_entries = {"test_entry"}
    radio_dev.via_device_id = "router_dev_id"
    radio_dev.disabled_by = None
    radio_dev.entry_type = None
    radio_dev.manufacturer = "OpenWrt"
    radio_dev.model = "Wireless Radio"

    device_registry = MagicMock()
    device_registry.devices = {
        router_dev.id: router_dev,
        radio_dev.id: radio_dev,
    }
    device_registry.async_get_device.return_value = router_dev
    device_registry.async_get_or_create.return_value = radio_dev

    data = OpenWrtData(
        device_info=DeviceInfo(mac_address="router_mac"),
        wireless_interfaces=[
            WirelessInterface(
                name="phy0-ap0",
                ssid="Test",
                radio="radio0",
                band="2.4 GHz",
            )
        ],
    )

    with patch(
        "homeassistant.helpers.device_registry.async_get",
        return_value=device_registry,
    ):
        await coordinator._async_update_device_registry(data)

    device_registry.async_remove_device.assert_not_called()
    device_registry.async_update_device.assert_any_call(
        "radio_dev_id",
        name="2.4 GHz",
        manufacturer="OpenWrt",
        model="Wireless Radio",
    )
    created_identifiers = [
        identifier
        for call in device_registry.async_get_or_create.call_args_list
        for identifier in call.kwargs.get("identifiers", set())
    ]
    assert (DOMAIN, "router_mac_radio_radio0") in created_identifiers
    assert (DOMAIN, "router_mac_ap_Test_2.4 GHz") in created_identifiers
    ap_call = next(
        call
        for call in device_registry.async_get_or_create.call_args_list
        if (DOMAIN, "router_mac_ap_Test_2.4 GHz")
        in call.kwargs.get("identifiers", set())
    )
    assert ap_call.kwargs["name"] == "SSID Test"
    assert ap_call.kwargs["via_device"] == (
        DOMAIN,
        "router_mac_radio_radio0",
    )
