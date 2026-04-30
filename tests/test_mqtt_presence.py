"""Test the OpenWrt MQTT presence detection integration."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from custom_components.openwrt.const import (
    CONF_MQTT_BROKER,
    CONF_MQTT_PASSWORD,
    CONF_MQTT_PORT,
    CONF_MQTT_PRESENCE,
    CONF_MQTT_USERNAME,
    CONF_REDEPLOY_MQTT,
    DOMAIN,
)
from custom_components.openwrt.device_tracker import async_setup_entry


@pytest.fixture
def mock_config_entry():
    """Mock config entry."""
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.unique_id = "11:22:33:44:55:66"
    entry.data = {
        CONF_HOST: "192.168.1.1",
        CONF_USERNAME: "root",
        CONF_PASSWORD: "password",
    }
    entry.options = {}
    entry.add_to_hass = MagicMock()
    return entry


async def test_device_tracker_skips_when_mqtt_enabled(
    hass: HomeAssistant, mock_config_entry
) -> None:
    """Test that device tracker platform skips setup if MQTT presence is enabled."""
    mock_config_entry.options = {CONF_MQTT_PRESENCE: True}

    mock_coordinator = MagicMock()
    hass.data[DOMAIN] = {mock_config_entry.entry_id: {"coordinator": mock_coordinator}}

    with patch("custom_components.openwrt.device_tracker._LOGGER.info") as mock_info:
        await async_setup_entry(hass, mock_config_entry, AsyncMock())

        mock_info.assert_called_once_with(
            "MQTT Presence Detection enabled, skipping standard device trackers for %s",
            "192.168.1.1",
        )


async def test_config_flow_mqtt_steps(hass: HomeAssistant) -> None:
    """Test the MQTT presence configuration steps in the config flow."""
    from custom_components.openwrt.config_flow import OpenWrtConfigFlow

    flow = OpenWrtConfigFlow()
    flow.hass = hass
    flow._data = {CONF_HOST: "192.168.1.1"}

    # 1. Show MQTT presence form
    result = await flow.async_step_mqtt_presence()
    assert result["type"].lower() == "form"
    assert result["step_id"] == "mqtt_presence"

    # 2. Submit MQTT presence details
    user_input = {
        CONF_MQTT_PRESENCE: True,
        CONF_MQTT_BROKER: "192.168.1.10",
        CONF_MQTT_PORT: 1883,
        CONF_MQTT_USERNAME: "user",
        CONF_MQTT_PASSWORD: "pass",
    }

    with (
        patch(
            "custom_components.openwrt.helpers.mqtt_presence.async_deploy_mqtt_presence",
            return_value=(True, None),
        ) as mock_deploy,
        patch(
            "custom_components.openwrt.config_flow.create_client",
            return_value=AsyncMock(),
        ),
        patch.object(flow, "_create_entry", return_value=AsyncMock()) as mock_create,
    ):
        result = await flow.async_step_mqtt_presence(user_input)

        assert mock_deploy.called
        assert mock_create.called


async def test_options_flow_mqtt_redeploy(
    hass: HomeAssistant, mock_config_entry
) -> None:
    """Test the MQTT presence re-deployment in options flow."""
    from custom_components.openwrt.config_flow import OpenWrtOptionsFlow

    # Register the mock entry in hass
    mock_config_entry.add_to_hass(hass)

    flow = OpenWrtOptionsFlow(mock_config_entry)
    flow.hass = hass

    # Submit redeploy option
    user_input = {
        CONF_REDEPLOY_MQTT: True,
        CONF_MQTT_PRESENCE: True,
    }

    with (
        patch(
            "custom_components.openwrt.helpers.mqtt_presence.async_deploy_mqtt_presence",
            return_value=(True, None),
        ) as mock_deploy,
        patch(
            "custom_components.openwrt.config_flow.create_client",
            return_value=AsyncMock(),
        ),
        patch.object(
            flow, "async_step_options_permissions", return_value=AsyncMock()
        ) as mock_perms,
    ):
        result = await flow.async_step_init(user_input)

        # Should have gone to mqtt_presence form first to confirm/update details
        assert result["step_id"] == "mqtt_presence"

        # Submit the details
        result = await flow.async_step_options_mqtt_presence(user_input)

        assert mock_deploy.called
        assert mock_perms.called


async def test_deploy_helper_success(hass: HomeAssistant) -> None:
    """Test the deployment helper logic (partial mock)."""
    from custom_components.openwrt.helpers.mqtt_presence import (
        async_deploy_mqtt_presence,
    )

    mock_client = AsyncMock()
    mock_client.execute_command.return_value = "OK"
    mqtt_config = {
        "broker": "127.0.0.1",
        "port": 1883,
        "username": "u",
        "password": "p",
    }

    # Mock aiohttp session
    with patch(
        "custom_components.openwrt.helpers.mqtt_presence.async_get_clientsession"
    ) as mock_session_func:
        mock_session = mock_session_func.return_value
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.text.return_value = 'file content with BROKER="192.168.1.10"'
        mock_session.get.return_value.__aenter__.return_value = mock_resp

        success, error = await async_deploy_mqtt_presence(
            hass, mock_client, mqtt_config
        )

        assert success is True
        assert error is None
        # Verify commands were called
        mock_client.execute_command.assert_any_call("mkdir -p /etc/presence")
        # Verify MQTT config replacement (check if execute_command was called with base64)
        # We can't easily check the exact base64 without re-implementing,
        # but we check if it was called multiple times for files
        assert mock_client.execute_command.call_count >= 10
