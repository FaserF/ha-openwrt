"""Test the OpenWrt button platform."""

from unittest.mock import MagicMock, patch

import pytest

from custom_components.openwrt.api.base import (
    ConnectedDevice,
    DeviceInfo,
    OpenWrtData,
    OpenWrtPackages,
    OpenWrtPermissions,
)
from custom_components.openwrt.button import async_setup_entry
from custom_components.openwrt.const import DATA_CLIENT, DATA_COORDINATOR, DOMAIN


@pytest.mark.asyncio
async def test_wol_button_restriction(hass) -> None:
    """Test that Wake on LAN button is only created for wired devices."""
    coordinator = MagicMock()
    coordinator.data = OpenWrtData(
        device_info=DeviceInfo(hostname="router"),
        connected_devices=[
            ConnectedDevice(
                mac="AA:BB:CC:DD:EE:01",
                hostname="wired-dev",
                is_wireless=False,
                interface="br-lan",
            ),
            ConnectedDevice(
                mac="AA:BB:CC:DD:EE:02",
                hostname="wireless-dev",
                is_wireless=True,
                interface="wlan0",
            ),
        ],
        permissions=OpenWrtPermissions(read_devices=True),
        packages=OpenWrtPackages(etherwake=True),
    )

    entry = MagicMock()
    entry.data = {"host": "192.168.1.1"}
    entry.entry_id = "test_entry"

    async_add_entities = MagicMock()

    hass.data[DOMAIN] = {
        entry.entry_id: {DATA_COORDINATOR: coordinator, DATA_CLIENT: MagicMock()},
    }
    with patch(
        "custom_components.openwrt.button.OpenWrtWakeOnLanButton",
    ) as mock_wol_button:
        await async_setup_entry(hass, entry, async_add_entities)

        # Check calls to OpenWrtWakeOnLanButton
        # Only the wired device should have triggered a button creation
        assert mock_wol_button.call_count == 1
        args, _kwargs = mock_wol_button.call_args
        # args[3] is the mac address in OpenWrtWakeOnLanButton(coordinator, entry, client, mac, name, interface)
        assert args[3] == "AA:BB:CC:DD:EE:01"


@pytest.mark.asyncio
async def test_kick_button_default_disabled(hass) -> None:
    """Test that Kick (Disconnect) button is disabled by default."""
    coordinator = MagicMock()
    coordinator.data = OpenWrtData(
        device_info=DeviceInfo(hostname="router"),
        connected_devices=[
            ConnectedDevice(
                mac="AA:BB:CC:DD:EE:02",
                hostname="wireless-dev",
                is_wireless=True,
                interface="wlan0",
            ),
        ],
        permissions=OpenWrtPermissions(read_wireless=True),
        packages=OpenWrtPackages(iwinfo=True),
    )

    entry = MagicMock()
    entry.data = {"host": "192.168.1.1"}
    entry.entry_id = "test_entry"

    async_add_entities = MagicMock()

    hass.data[DOMAIN] = {
        entry.entry_id: {DATA_COORDINATOR: coordinator, DATA_CLIENT: MagicMock()},
    }

    await async_setup_entry(hass, entry, async_add_entities)

    # Find the kick button in the added entities
    kick_buttons = [
        e
        for e in async_add_entities.call_args[0][0]
        if hasattr(e, "_attr_translation_key")
        and e._attr_translation_key == "kick_device"
    ]

    assert len(kick_buttons) == 1
    assert kick_buttons[0].entity_registry_enabled_default is False
