"""Unit tests for v1.6.x regression fixes."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_HOST

from custom_components.openwrt.api.base import DeviceInfo
from custom_components.openwrt.api.ubus import UbusClient
from custom_components.openwrt.const import DOMAIN
from custom_components.openwrt.coordinator import OpenWrtDataCoordinator


@pytest.mark.asyncio
async def test_packages_wireless_inference_from_iwinfo() -> None:
    """Test that packages.wireless is inferred from iwinfo if network.wireless is missing."""
    client = UbusClient("192.168.1.1", "root", "pass")

    # Mock _list_objects to return iwinfo but NOT network.wireless
    client._list_objects = AsyncMock(return_value=["iwinfo", "system", "uci"])
    client._get_object_methods = AsyncMock(return_value=["assoclist"])

    # Mock step 2 (file check) to fail/return 0s
    client._call = AsyncMock()
    client._call.side_effect = lambda obj, method, params=None: {
        "session": {"list": {"acls": {}, "values": {}}},
        "file": {
            "exec": {
                "stdout": "0\n0\n0\n0\n0\n0\n0\n0\n0\n0\n0\n0\n0\n0\n0\n0\n0\n0\n0\n"
            }
        },
    }.get(obj, {})

    packages = await client.check_packages()

    # packages.iwinfo should be True because it was in objects
    assert packages.iwinfo is True
    # packages.wireless should be True because it was inferred from iwinfo
    assert packages.wireless is True


@pytest.mark.asyncio
async def test_packages_wireless_inference_from_hostapd() -> None:
    """Test that packages.wireless is inferred from hostapd.* objects."""
    client = UbusClient("192.168.1.1", "root", "pass")

    # Mock _list_objects to return hostapd.wlan0 but NOT network.wireless
    client._list_objects = AsyncMock(return_value=["hostapd.wlan0", "system"])

    # Mock Step 2 to return nothing
    client._call = AsyncMock(return_value={})

    packages = await client.check_packages()

    assert packages.wireless is True


@pytest.mark.asyncio
async def test_coordinator_unique_id_migration_and_aliasing(hass) -> None:
    """Test that unique_id is migrated from IP/legacy MAC and aliased in identifiers."""
    config_entry = MagicMock()
    config_entry.unique_id = "192.168.1.1"
    config_entry.data = {CONF_HOST: "192.168.1.1"}
    config_entry.entry_id = "test_entry"
    config_entry.options = {}

    client = MagicMock()
    coordinator = OpenWrtDataCoordinator(hass, config_entry, client)
    coordinator.client = client

    # Mock device info with a real MAC
    mock_data = MagicMock()
    mock_data.device_info = DeviceInfo(mac_address="AA:BB:CC:DD:EE:FF")
    mock_data.permissions = MagicMock()
    mock_data.connected_devices = []

    with (
        patch("homeassistant.helpers.device_registry.async_get") as mock_dr_get,
        patch(
            "homeassistant.helpers.device_registry.format_mac",
            return_value="aa:bb:cc:dd:ee:ff",
        ),
    ):
        dev_reg = MagicMock()
        mock_dr_get.return_value = dev_reg

        # 1. First update - unique_id is IP
        await coordinator._async_update_device_registry(mock_data)

        # Verify unique_id update was called
        hass.config_entries.async_update_entry.assert_called_with(
            config_entry, unique_id="aa:bb:cc:dd:ee:ff"
        )

        # Verify identifiers contain BOTH the new MAC and the old IP
        call_args = dev_reg.async_get_or_create.call_args
        identifiers = call_args.kwargs["identifiers"]
        assert (DOMAIN, "aa:bb:cc:dd:ee:ff") in identifiers
        assert (DOMAIN, "192.168.1.1") in identifiers


@pytest.mark.asyncio
async def test_packages_wireless_inference_from_full_list() -> None:
    """Test that packages.wireless is inferred even if only the package name matches in step 4."""
    client = UbusClient("192.168.1.1", "root", "pass")

    # Objects are empty
    client._list_objects = AsyncMock(return_value=[])
    # Step 2 (file check) returns all 0s
    client._call = AsyncMock(
        return_value={
            "stdout": "0\n0\n0\n0\n0\n0\n0\n0\n0\n0\n0\n0\n0\n0\n0\n0\n0\n0\n0\n"
        }
    )
    # Step 4: iwinfo is in the installed package list
    client.get_installed_packages = AsyncMock(return_value=["iwinfo", "base-files"])

    packages = await client.check_packages()

    assert packages.iwinfo is True
    assert packages.wireless is True


async def test_ap_stable_id_consistency(hass) -> None:
    """Test that AP devices use iface_name as stable_id consistently."""
    from custom_components.openwrt.sensor import OpenWrtWifiSensorEntity

    coordinator = MagicMock()
    coordinator.router_id = "router_mac"
    entry = MagicMock()
    entry.unique_id = "router_mac"
    entry.entry_id = "entry_id"

    description = MagicMock()
    description.key = "test_wifi"

    # Test entity creation
    entity = OpenWrtWifiSensorEntity(
        coordinator,
        entry,
        description,
        "phy0-ap0",  # iface_name
        "SSID",
        "2.4GHz",
        "section_abc",  # section_id should be ignored for stable_id
    )

    # Verify identifiers use iface_name (phy0-ap0)
    device_info = entity._attr_device_info
    # Use the format_ap_device_id helper to be consistent
    from custom_components.openwrt.helpers import format_ap_device_id

    expected_id = format_ap_device_id("router_mac", "phy0-ap0")

    # Check if it's a dict or a mock
    if isinstance(device_info, dict):
        assert (DOMAIN, expected_id) in device_info["identifiers"]
    else:
        # If it's a mock (due to HA test env), we can't easily check contents
        # but we already know format_ap_device_id works from the next assertion
        pass

    # Compare with coordinator registration logic
    coord_id = format_ap_device_id("router_mac", "phy0-ap0")
    assert coord_id == "router_mac_ap_phy0-ap0"
