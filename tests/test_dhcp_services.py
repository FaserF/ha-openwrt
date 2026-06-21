"""Tests for OpenWrt static DHCP lease services."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError
import voluptuous as vol

from custom_components.openwrt import (
    _register_services,
)
from custom_components.openwrt.const import DOMAIN, DATA_CLIENT


@pytest.mark.asyncio
async def test_add_static_lease_service(hass) -> None:
    """Test the add_static_lease service handler."""
    mock_client = MagicMock()
    mock_client.execute_command = AsyncMock()

    hass.data = {
        DOMAIN: {
            "test_entry_id": {
                DATA_CLIENT: mock_client,
            }
        }
    }

    # Register services to get the handlers
    with patch("homeassistant.core.ServiceRegistry.async_register") as mock_register:
        _register_services(hass)
        
        # Find the registered add_static_lease handler
        add_handler = None
        for call in mock_register.call_args_list:
            if call[0][1] == "add_static_lease":
                add_handler = call[0][2]
                break

        assert add_handler is not None

        # Call the handler with mock data
        call_data = MagicMock()
        call_data.data = {
            "entry_id": "test_entry_id",
            "mac": "aa:bb:cc:dd:ee:ff",
            "ip": "192.168.1.100",
            "name": "test-device",
        }

        await add_handler(call_data)
        
        # Verify execute_command was called with script containing the set commands
        mock_client.execute_command.assert_called_once()
        script = mock_client.execute_command.call_args[0][0]
        assert "uci add dhcp host" in script
        assert "uci set dhcp.@host[-1].mac='aa:bb:cc:dd:ee:ff'" in script
        assert "uci set dhcp.@host[-1].ip='192.168.1.100'" in script
        assert "uci set dhcp.@host[-1].name='test-device'" in script
        assert "uci commit dhcp" in script


@pytest.mark.asyncio
async def test_delete_static_lease_service(hass) -> None:
    """Test the delete_static_lease service handler."""
    mock_client = MagicMock()
    mock_client.execute_command = AsyncMock()

    hass.data = {
        DOMAIN: {
            "test_entry_id": {
                DATA_CLIENT: mock_client,
            }
        }
    }

    with patch("homeassistant.core.ServiceRegistry.async_register") as mock_register:
        _register_services(hass)
        
        # Find the registered delete_static_lease handler
        del_handler = None
        for call in mock_register.call_args_list:
            if call[0][1] == "delete_static_lease":
                del_handler = call[0][2]
                break

        assert del_handler is not None

        # Call the handler with mock data
        call_data = MagicMock()
        call_data.data = {
            "entry_id": "test_entry_id",
            "mac": "aa:bb:cc:dd:ee:ff",
        }

        await del_handler(call_data)
        
        # Verify delete commands
        mock_client.execute_command.assert_called_once()
        script = mock_client.execute_command.call_args[0][0]
        assert "uci delete dhcp.@host" in script
        assert "uci commit dhcp" in script
