"""Test cleanup of entities and devices when features are disabled."""

from unittest.mock import MagicMock, patch

from homeassistant.core import HomeAssistant

from custom_components.openwrt import _async_cleanup_disabled_features
from custom_components.openwrt.const import CONF_ENABLE_LED, CONF_TRACK_DEVICES, DOMAIN


async def test_cleanup_disabled_features(hass: HomeAssistant) -> None:
    """Test cleanup of entities and devices when features are disabled."""
    entry = MagicMock()
    entry.version = 2
    entry.domain = DOMAIN
    entry.title = "OpenWrt Test"
    entry.data = {"host": "192.168.1.1"}
    entry.options = {
        CONF_TRACK_DEVICES: False,
        CONF_ENABLE_LED: False,
    }
    entry.unique_id = "11:22:33:44:55:66"
    entry.entry_id = "test_entry_id"

    mock_tracker_ent = MagicMock()
    mock_tracker_ent.entity_id = "device_tracker.client"
    mock_tracker_ent.domain = "device_tracker"
    mock_tracker_ent.unique_id = "test_entry_id_00:11:22:33:44:55"

    mock_led_ent = MagicMock()
    mock_led_ent.entity_id = "light.led_status"
    mock_led_ent.domain = "light"
    mock_led_ent.unique_id = "test_entry_id_led_status"

    mock_router_ent = MagicMock()
    mock_router_ent.entity_id = "sensor.uptime"
    mock_router_ent.domain = "sensor"
    mock_router_ent.unique_id = "test_entry_id_uptime"

    mock_router_dev = MagicMock()
    mock_router_dev.id = "router_device_id"
    mock_router_dev.name = "Router"
    mock_router_dev.identifiers = {(DOMAIN, "11:22:33:44:55:66")}

    mock_client_dev = MagicMock()
    mock_client_dev.id = "client_device_id"
    mock_client_dev.name = "Client"
    mock_client_dev.identifiers = {(DOMAIN, "00:11:22:33:44:55")}

    mock_ap_dev = MagicMock()
    mock_ap_dev.id = "ap_device_id"
    mock_ap_dev.name = "AP Device"
    mock_ap_dev.identifiers = {(DOMAIN, "11:22:33:44:55:66_ap_ra0")}

    mock_ent_reg = MagicMock()
    mock_dev_reg = MagicMock()

    with (
        patch(
            "custom_components.openwrt.dr.format_mac",
            side_effect=lambda x: x.lower(),
        ),
        patch(
            "custom_components.openwrt.er.async_get",
            return_value=mock_ent_reg,
        ),
        patch(
            "custom_components.openwrt.dr.async_get",
            return_value=mock_dev_reg,
        ),
        patch(
            "custom_components.openwrt.er.async_entries_for_config_entry",
            return_value=[mock_tracker_ent, mock_led_ent, mock_router_ent],
        ),
        patch(
            "custom_components.openwrt.dr.async_entries_for_config_entry",
            return_value=[mock_router_dev, mock_client_dev, mock_ap_dev],
        ),
    ):
        await _async_cleanup_disabled_features(hass, entry)

        mock_ent_reg.async_remove.assert_any_call("device_tracker.client")
        mock_ent_reg.async_remove.assert_any_call("light.led_status")

        mock_dev_reg.async_remove_device.assert_any_call("client_device_id")
        for call in mock_dev_reg.async_remove_device.call_args_list:
            assert call[0][0] != "router_device_id"
            assert call[0][0] != "ap_device_id"
