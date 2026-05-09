"""Tests for session management and leak prevention."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.openwrt.config_flow import OpenWrtConfigFlow


@pytest.mark.asyncio
async def test_config_flow_test_connection_cleanup(hass):
    """Test that OpenWrtConfigFlow._test_connection always disconnects the client."""
    flow = OpenWrtConfigFlow()
    flow.hass = hass

    with patch("custom_components.openwrt.config_flow.create_client") as mock_create:
        mock_client = MagicMock()
        mock_client.disconnect = AsyncMock()
        mock_client.connect = AsyncMock(
            side_effect=Exception("Connection test failed intentionally")
        )
        mock_client.perform_diagnostics = AsyncMock(return_value={})
        mock_create.return_value = mock_client

        # This should call perform_diagnostics and then disconnect even if connect fails
        await flow._test_connection(
            {"host": "192.168.1.1", "username": "root", "password": "password"}
        )

        # Verify disconnect was called in the finally block
        mock_client.disconnect.assert_called_once()


@pytest.mark.asyncio
async def test_config_flow_provision_cleanup(hass):
    """Test that OpenWrtConfigFlow.async_step_do_provision always disconnects the client."""
    flow = OpenWrtConfigFlow()
    flow.hass = hass
    flow._data = {"host": "192.168.1.1", "username": "root", "password": "password"}

    with patch("custom_components.openwrt.config_flow.create_client") as mock_create:
        mock_client = MagicMock()
        mock_client.disconnect = AsyncMock()
        mock_client.connect = AsyncMock()
        mock_client.provision_user = AsyncMock(
            side_effect=Exception("Provisioning failed intentionally")
        )
        mock_create.return_value = mock_client

        result = await flow.async_step_do_provision()

        assert result["step_id"] == "provision_failed"
        # Verify disconnect was called in the finally block
        mock_client.disconnect.assert_called_once()
