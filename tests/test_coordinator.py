"""Test the OpenWrt coordinator."""

from __future__ import annotations

from unittest.mock import MagicMock, AsyncMock
import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.openwrt.api.base import OpenWrtData, DeviceInfo, SystemResources
from custom_components.openwrt.coordinator import OpenWrtDataCoordinator


@pytest.mark.asyncio
async def test_coordinator_stale_data_on_timeout() -> None:
    """Test that coordinator returns stale data when update fails with timeout."""
    hass = MagicMock()
    config_entry = MagicMock()
    config_entry.options = {}
    config_entry.data = {"host": "192.168.1.1", "username": "root", "password": "password"}
    config_entry.entry_id = "test_entry"
    
    mock_client = AsyncMock()
    mock_client.connected = True
    
    coordinator = OpenWrtDataCoordinator(hass, config_entry, mock_client)
    
    # Set initial data
    initial_data = OpenWrtData(
        system_resources=SystemResources(uptime=100),
        connected_devices=[],
        network_interfaces=[],
        wireless_interfaces=[],
    )
    coordinator.data = initial_data
    
    async def get_all_data_err(*args, **kwargs):
        raise TimeoutError("Connection timed out")
        
    async def connect_err(*args, **kwargs):
        raise Exception("Reconnect failed")
        
    mock_client.get_all_data = get_all_data_err
    mock_client.connect = connect_err
    
    # Run update
    data = await coordinator._async_update_data()
    
    # Should return initial_data
    assert data == initial_data
    assert coordinator.data == initial_data


@pytest.mark.asyncio
async def test_coordinator_update_failed_on_new_install() -> None:
    """Test that coordinator raises UpdateFailed if no stale data is available."""
    hass = MagicMock()
    config_entry = MagicMock()
    config_entry.options = {}
    config_entry.data = {"host": "192.168.1.1", "username": "root", "password": "password"}
    config_entry.entry_id = "test_entry"
    
    mock_client = AsyncMock()
    mock_client.connected = True
    
    coordinator = OpenWrtDataCoordinator(hass, config_entry, mock_client)
    coordinator.data = None
    
    async def get_all_data_err(*args, **kwargs):
        raise TimeoutError("Connection timed out")
        
    async def connect_err(*args, **kwargs):
        raise Exception("Reconnect failed")
        
    mock_client.get_all_data = get_all_data_err
    mock_client.connect = connect_err
    
    # Run update - should raise UpdateFailed
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
