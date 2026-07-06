"""Tests for firmware update backup failure and URL resolution."""

from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.openwrt.coordinator import OpenWrtData, OpenWrtDataCoordinator
from custom_components.openwrt.update import OpenWrtUpdateEntity


@pytest.mark.asyncio
async def test_firmware_update_backup_failure_aborts(hass: HomeAssistant) -> None:
    """Test that a backup failure during firmware update raises HomeAssistantError and aborts."""
    entry = MagicMock()
    entry.entry_id = "test_entry_id"
    entry.options = {"auto_backup": True, "update_interval": 30}
    entry.data = {"host": "192.168.1.1"}

    mock_client = AsyncMock()
    # Mock create_backup returning None (failure)
    mock_client.create_backup.return_value = None

    coordinator = MagicMock(spec=OpenWrtDataCoordinator)
    coordinator.hass = hass
    coordinator.config_entry = entry
    coordinator.client = mock_client
    coordinator.router_id = "11:22:33:44:55:66"
    coordinator.data = OpenWrtData()
    coordinator.data.firmware_install_url = "http://example.com/firmware.bin"

    update_entity = OpenWrtUpdateEntity(coordinator, entry)
    update_entity.hass = hass

    with pytest.raises(HomeAssistantError, match="Failed to create backup on router"):
        await update_entity.async_install(
            version="25.12.5", backup=True, keep_settings=True
        )


@pytest.mark.asyncio
async def test_async_set_stable_release_urls(hass: HomeAssistant) -> None:
    """Test that _async_set_stable_release_urls dynamically fetches profiles.json."""
    entry = MagicMock()
    entry.entry_id = "test_entry_id"
    entry.options = {"update_interval": 30}
    entry.data = {"host": "192.168.1.1"}

    mock_client = AsyncMock()
    coordinator = OpenWrtDataCoordinator(hass, entry, mock_client)

    data = OpenWrtData()
    data.device_info.target = "mediatek/filogic"
    data.device_info.board_name = "xiaomi_mi-router-ax3000t-ubootmod"
    data.device_info.release_distribution = "openwrt"

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(
        return_value={
            "profiles": {
                "xiaomi-mi-router-ax3000t-ubootmod": {
                    "images": [
                        {
                            "name": "openwrt-25.12.5-mediatek-filogic-xiaomi_mi-router-ax3000t-ubootmod-squashfs-sysupgrade.itb",
                            "type": "sysupgrade",
                        }
                    ]
                }
            }
        }
    )

    mock_session = MagicMock(spec=aiohttp.ClientSession)
    mock_session.get.return_value.__aenter__.return_value = mock_resp

    await coordinator._async_set_stable_release_urls(data, "25.12.5", mock_session)

    # Assert dynamic URL resolved from profiles.json
    assert (
        data.firmware_install_url
        == "https://downloads.openwrt.org/releases/25.12.5/targets/mediatek/filogic/openwrt-25.12.5-mediatek-filogic-xiaomi_mi-router-ax3000t-ubootmod-squashfs-sysupgrade.itb"
    )
