"""Test the OpenWrt button platform."""
from unittest.mock import MagicMock, patch
import pytest

from custom_components.openwrt.api.base import OpenWrtData, ConnectedDevice, DeviceInfo, OpenWrtPermissions, OpenWrtPackages
from custom_components.openwrt.button import async_setup_entry

@pytest.mark.asyncio
async def test_wol_button_restriction(hass) -> None:
    """Test that Wake on LAN button is only created for wired devices."""
    coordinator = MagicMock()
    coordinator.data = OpenWrtData(
        device_info=DeviceInfo(hostname="router"),
        connected_devices=[
            ConnectedDevice(mac="AA:BB:CC:DD:EE:01", hostname="wired-dev", is_wireless=False, interface="br-lan"),
            ConnectedDevice(mac="AA:BB:CC:DD:EE:02", hostname="wireless-dev", is_wireless=True, interface="wlan0"),
        ],
        permissions=OpenWrtPermissions(read_devices=True),
        packages=OpenWrtPackages(etherwake=True),
    )
    
    entry = MagicMock()
    entry.data = {"host": "192.168.1.1"}
    entry.entry_id = "test_entry"
    
    async_add_entities = MagicMock()
    
    with patch("custom_components.openwrt.button.OpenWrtWakeOnLanButton") as mock_wol_button:
        await async_setup_entry(hass, entry, async_add_entities)
        
        # Check calls to OpenWrtWakeOnLanButton
        # Only the wired device should have triggered a button creation
        assert mock_wol_button.call_count == 1
        args, kwargs = mock_wol_button.call_args
        # args[3] is the mac address in OpenWrtWakeOnLanButton(coordinator, entry, client, mac, name, interface)
        assert args[3] == "AA:BB:CC:DD:EE:01"
