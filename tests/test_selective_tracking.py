"""Test selective device tracking in OpenWrt coordinator."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.openwrt.api.base import (
    ConnectedDevice,
    DhcpLease,
    OpenWrtData,
    SystemResources,
)
from custom_components.openwrt.const import CONF_TRACKED_DEVICES
from custom_components.openwrt.coordinator import OpenWrtDataCoordinator


@pytest.mark.asyncio
async def test_coordinator_selective_tracking() -> None:
    """Test that coordinator filters devices based on whitelist."""
    hass = MagicMock()
    hass.loop = MagicMock()
    hass.loop.time = MagicMock(return_value=123456789.0)

    config_entry = MagicMock()
    # Whitelist only device1
    config_entry.options = {CONF_TRACKED_DEVICES: ["aa:bb:cc:dd:ee:01"]}
    config_entry.data = {"host": "192.168.1.1"}
    config_entry.entry_id = "test_entry"

    mock_client = AsyncMock()
    mock_client.connected = True

    # Mock data with two devices
    raw_data = OpenWrtData(
        system_resources=SystemResources(uptime=100),
        connected_devices=[
            ConnectedDevice(
                mac="aa:bb:cc:dd:ee:01",
                hostname="device1",
                interface="br-lan",
                is_wireless=True,
            ),
            ConnectedDevice(
                mac="aa:bb:cc:dd:ee:02",
                hostname="device2",
                interface="br-lan",
                is_wireless=True,
            ),
        ],
        dhcp_leases=[
            DhcpLease(mac="aa:bb:cc:dd:ee:01", hostname="device1", ip="192.168.1.10"),
            DhcpLease(mac="aa:bb:cc:dd:ee:02", hostname="device2", ip="192.168.1.11"),
        ],
        network_interfaces=[],
        wireless_interfaces=[],
    )
    mock_client.get_all_data.return_value = raw_data

    with patch("custom_components.openwrt.coordinator.storage.Store") as mock_store:
        mock_store.return_value.async_load = AsyncMock(return_value={})
        mock_store.return_value.async_save = AsyncMock()
        coordinator = OpenWrtDataCoordinator(hass, config_entry, mock_client)

    # Run update
    data = await coordinator._async_update_data()

    # Should only contain device1
    assert len(data.connected_devices) == 1
    assert data.connected_devices[0].mac == "aa:bb:cc:dd:ee:01"

    assert len(data.dhcp_leases) == 1
    assert data.dhcp_leases[0].mac == "aa:bb:cc:dd:ee:01"


@pytest.mark.asyncio
async def test_coordinator_no_whitelist() -> None:
    """Test that coordinator tracks all devices if no whitelist is configured."""
    hass = MagicMock()
    hass.loop = MagicMock()
    hass.loop.time = MagicMock(return_value=123456789.0)

    config_entry = MagicMock()
    config_entry.options = {}  # No whitelist
    config_entry.data = {"host": "192.168.1.1"}
    config_entry.entry_id = "test_entry"

    mock_client = AsyncMock()
    mock_client.connected = True

    raw_data = OpenWrtData(
        system_resources=SystemResources(uptime=100),
        connected_devices=[
            ConnectedDevice(
                mac="aa:bb:cc:dd:ee:01",
                hostname="device1",
                interface="br-lan",
                is_wireless=True,
            ),
            ConnectedDevice(
                mac="aa:bb:cc:dd:ee:02",
                hostname="device2",
                interface="br-lan",
                is_wireless=True,
            ),
        ],
        dhcp_leases=[
            DhcpLease(mac="aa:bb:cc:dd:ee:01", hostname="device1", ip="192.168.1.10"),
            DhcpLease(mac="aa:bb:cc:dd:ee:02", hostname="device2", ip="192.168.1.11"),
        ],
        network_interfaces=[],
        wireless_interfaces=[],
    )
    mock_client.get_all_data.return_value = raw_data

    with patch("custom_components.openwrt.coordinator.storage.Store") as mock_store:
        mock_store.return_value.async_load = AsyncMock(return_value={})
        mock_store.return_value.async_save = AsyncMock()
        coordinator = OpenWrtDataCoordinator(hass, config_entry, mock_client)

    # Run update
    data = await coordinator._async_update_data()

    # Should contain both devices
    assert len(data.connected_devices) == 2
    assert len(data.dhcp_leases) == 2
