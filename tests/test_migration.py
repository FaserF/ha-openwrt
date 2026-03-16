"""Tests for OpenWrt config entry migration."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from homeassistant.core import HomeAssistant
from custom_components.openwrt import async_migrate_entry

async def test_migration_v1_to_v2(hass: HomeAssistant):
    """Test migration from version 1 to 2."""
    entry = MagicMock()
    entry.version = 1
    entry.entry_id = "test_entry"
    entry.data = {"host": "192.168.1.1"}

    mock_device_info = MagicMock()
    mock_device_info.mac_address = "AA:BB:CC:DD:EE:FF"
    
    mock_client = AsyncMock()
    mock_client.get_device_info.return_value = mock_device_info
    
    with patch("custom_components.openwrt.create_client", return_value=mock_client), \
         patch("custom_components.openwrt.dr.format_mac", side_effect=lambda x: x.lower()), \
         patch.object(hass.config_entries, "async_update_entry") as mock_update:
        
        assert await async_migrate_entry(hass, entry) is True
        
        mock_update.assert_called_once_with(
            entry, unique_id="aa:bb:cc:dd:ee:ff", version=2
        )

async def test_migration_v1_to_v2_fail_mac(hass: HomeAssistant):
    """Test migration from version 1 to 2 when MAC cannot be retrieved."""
    entry = MagicMock()
    entry.version = 1
    entry.entry_id = "test_entry"
    entry.data = {"host": "192.168.1.1"}

    mock_device_info = MagicMock()
    mock_device_info.mac_address = None
    
    mock_client = AsyncMock()
    mock_client.get_device_info.return_value = mock_device_info
    
    with patch("custom_components.openwrt.create_client", return_value=mock_client), \
         patch.object(hass.config_entries, "async_update_entry") as mock_update:
        
        assert await async_migrate_entry(hass, entry) is True
        
        # Should still bump version
        mock_update.assert_called_once_with(
            entry, version=2
        )

async def test_migration_v1_to_v2_exceptions(hass: HomeAssistant):
    """Test migration fails on connection error."""
    entry = MagicMock()
    entry.version = 1
    entry.entry_id = "test_entry"
    entry.data = {"host": "192.168.1.1"}

    mock_client = AsyncMock()
    mock_client.connect.side_effect = Exception("Connection failed")
    
    with patch("custom_components.openwrt.create_client", return_value=mock_client), \
         patch.object(hass.config_entries, "async_update_entry") as mock_update:
        
        assert await async_migrate_entry(hass, entry) is False
        assert mock_update.call_count == 0
