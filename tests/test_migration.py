"""Tests for OpenWrt config entry migration."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
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

    with (
        patch("custom_components.openwrt.create_client", return_value=mock_client),
        patch(
            "custom_components.openwrt.dr.format_mac",
            side_effect=lambda x: x.lower(),
        ),
        patch.object(hass.config_entries, "async_update_entry") as mock_update,
    ):
        assert await async_migrate_entry(hass, entry) is True

        mock_update.assert_called_once_with(
            entry,
            unique_id="aa:bb:cc:dd:ee:ff",
            version=2,
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

    with (
        patch("custom_components.openwrt.create_client", return_value=mock_client),
        patch.object(hass.config_entries, "async_update_entry") as mock_update,
    ):
        assert await async_migrate_entry(hass, entry) is True

        # Should still bump version
        mock_update.assert_called_once_with(entry, version=2)


async def test_migration_v1_to_v2_exceptions(hass: HomeAssistant):
    """Test migration fails on connection error."""
    entry = MagicMock()
    entry.version = 1
    entry.entry_id = "test_entry"
    entry.data = {"host": "192.168.1.1"}

    mock_client = AsyncMock()
    mock_client.connect.side_effect = Exception("Connection failed")

    with (
        patch("custom_components.openwrt.create_client", return_value=mock_client),
        patch.object(hass.config_entries, "async_update_entry") as mock_update,
    ):
        assert await async_migrate_entry(hass, entry) is False
        assert mock_update.call_count == 0


@pytest.mark.asyncio
async def test_coordinator_unique_id_migration_and_aliasing(hass) -> None:
    """Test that unique_id is migrated from IP/legacy MAC and aliased in identifiers."""
    from custom_components.openwrt.api.base import DeviceInfo
    from custom_components.openwrt.const import CONF_HOST, DOMAIN
    from custom_components.openwrt.coordinator import OpenWrtDataCoordinator

    config_entry = MagicMock()
    config_entry.unique_id = "192.168.1.1"
    config_entry.data = {CONF_HOST: "192.168.1.1"}
    config_entry.entry_id = "test_entry"
    config_entry.options = {}

    client = MagicMock()
    coordinator = OpenWrtDataCoordinator(hass, config_entry, client)

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
