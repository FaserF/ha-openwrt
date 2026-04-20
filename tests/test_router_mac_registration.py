"""Tests for router device creation with MAC address."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.helpers import device_registry as dr

from custom_components.openwrt.api.base import (
    DeviceInfo,
    NetworkInterface,
    OpenWrtData,
)
from custom_components.openwrt.coordinator import OpenWrtDataCoordinator


@pytest.mark.asyncio
async def test_router_device_registration_with_mac(hass):
    """Test that the router device is registered with a MAC address connection."""
    entry = MagicMock()
    entry.data = {"host": "192.168.1.1", "username": "root", "password": "password"}
    entry.entry_id = "test_entry_id"
    entry.title = "OpenWrt Router"
    entry.options = {}

    # Mock client and data
    mock_client = AsyncMock()
    mock_client.connect = AsyncMock()

    device_info = DeviceInfo(
        hostname="OpenWrt",
        release_distribution="OpenWrt",
        release_version="23.05.0",
        release_revision="r12345",
        firmware_version="23.05.0 (r12345)",
    )

    network_interfaces = [
        NetworkInterface(name="br-lan", mac_address="AA:BB:CC:DD:EE:FF", up=True),
    ]

    data = OpenWrtData(
        device_info=device_info,
        network_interfaces=network_interfaces,
    )

    # We need to trigger the logic in get_all_data or simulate it
    # For this test, we simulate the state after get_all_data has run
    data.device_info.mac_address = "AA:BB:CC:DD:EE:FF"

    mock_client.get_all_data = AsyncMock(return_value=data)

    with (patch("custom_components.openwrt.coordinator.dr.async_get") as mock_dr_get,):
        mock_registry = MagicMock()
        mock_dr_get.return_value = mock_registry

        # Create coordinator and run the registration logic directly
        coordinator = OpenWrtDataCoordinator(hass, entry, mock_client)
        await coordinator._async_update_device_registry(data)

        # Check if device_registry.async_get_or_create was called correctly
        calls = mock_registry.async_get_or_create.call_args_list
        router_call = None
        for call in calls:
            kwargs = call.kwargs
            if (
                kwargs.get("name") == "OpenWrt"
                or kwargs.get("name") == "OpenWrt Router"
            ):
                router_call = call
                break

        assert router_call is not None
        assert (
            dr.CONNECTION_NETWORK_MAC,
            "AA:BB:CC:DD:EE:FF".lower(),
        ) in router_call.kwargs["connections"]
