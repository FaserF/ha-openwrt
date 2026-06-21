"""Tests for device-based Wake-on-LAN service resolution."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC

from custom_components.openwrt import _register_services
from custom_components.openwrt.const import DATA_CLIENT, DOMAIN


@pytest.mark.asyncio
async def test_wol_resolve_device(hass) -> None:
    """Test resolving WoL MAC from device registry."""
    mock_client = MagicMock()
    mock_client.execute_command = AsyncMock(return_value="Success")

    hass.data = {
        DOMAIN: {
            "test_entry_id": {
                DATA_CLIENT: mock_client,
            }
        }
    }

    # Mock device and connections
    mock_device = MagicMock()
    mock_device.connections = {(CONNECTION_NETWORK_MAC, "11:22:33:44:55:66")}

    mock_dev_reg = MagicMock()
    mock_dev_reg.async_get = MagicMock(return_value=mock_device)

    with (
        patch(
            "homeassistant.helpers.device_registry.async_get", return_value=mock_dev_reg
        ),
        patch("homeassistant.helpers.entity_registry.async_get"),
    ):
        with patch.object(hass.services, "async_register"):
            _register_services(hass)

            wol_handler = None
            for call in hass.services.async_register.call_args_list:
                if call[0][1] == "wake_on_lan":
                    wol_handler = call[0][2]
                    break

            assert wol_handler is not None

            call_data = MagicMock()
            call_data.data = {
                "target": "test_entry_id",
                "device_id": "mock_device_id",
            }
            await wol_handler(call_data)

            mock_client.execute_command.assert_called_once_with(
                "ether-wake 11:22:33:44:55:66"
            )
