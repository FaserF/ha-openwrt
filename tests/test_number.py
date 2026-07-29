"""Tests for the number platform of the OpenWrt integration."""

from unittest.mock import MagicMock

import pytest
from homeassistant.const import CONF_HOST

from custom_components.openwrt.api.base import (
    OpenWrtData,
    OpenWrtPermissions,
    WirelessInterface,
)
from custom_components.openwrt.number import (
    OpenWrtTxPowerNumber,
    async_setup_entry,
)


@pytest.mark.asyncio
async def test_txpower_number_creation_and_control() -> None:
    """Test wireless TX Power number entities are created and work when txpower is 0 or greater."""
    wifi = WirelessInterface(
        name="phy0-ap0",
        ssid="MyNet",
        radio="radio0",
        txpower=0,
    )

    coordinator = MagicMock()

    async def mock_refresh(*args, **kwargs):
        pass

    coordinator.async_request_refresh = MagicMock(side_effect=mock_refresh)

    # Permissions are required to have write_wireless=True
    perms = OpenWrtPermissions(write_wireless=True)
    coordinator.data = OpenWrtData(wireless_interfaces=[wifi], permissions=perms)

    client = MagicMock()

    async def mock_execute(*args, **kwargs):
        pass

    client.execute_command = MagicMock(side_effect=mock_execute)
    coordinator.client = client

    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.unique_id = "router_mac"
    entry.data = {CONF_HOST: "192.168.1.1"}

    # Mock setup
    added_entities = []

    def async_add_entities(entities):
        added_entities.extend(entities)

    hass = MagicMock()
    hass.data = {
        "openwrt": {"test_entry": {"coordinator": coordinator, "client": client}}
    }

    # Run async_setup_entry
    await async_setup_entry(hass, entry, async_add_entities)

    assert len(added_entities) == 1
    entity = added_entities[0]
    assert isinstance(entity, OpenWrtTxPowerNumber)

    # Mock hass.data for set_native_value
    entity.hass = hass

    assert entity.native_value == 0

    # Test setting native value
    await entity.async_set_native_value(15)
    client.execute_command.assert_called_with(
        "uci set wireless.radio0.txpower='15' && uci commit wireless && wifi reload"
    )
    coordinator.async_request_refresh.assert_called()


def test_txpower_number_status_matching() -> None:
    """Verify OpenWrtTxPowerNumber matches txpower using section when iface name differs."""
    from custom_components.openwrt.coordinator import OpenWrtDataCoordinator
    from custom_components.openwrt.number import OpenWrtTxPowerNumber

    config_entry = MagicMock()
    config_entry.entry_id = "test_entry"
    config_entry.unique_id = "test_router"
    config_entry.options = {"update_interval": 60}
    config_entry.data = {CONF_HOST: "192.168.1.1"}

    coordinator = OpenWrtDataCoordinator(MagicMock(), config_entry, MagicMock())
    coordinator.data = MagicMock()
    wifi_iface = WirelessInterface(
        name="wlan0", section="default_radio0", radio="radio0", txpower=20
    )
    coordinator.data.wireless_interfaces = [wifi_iface]

    num = OpenWrtTxPowerNumber(
        coordinator,
        config_entry,
        "default_radio0",
        "MyNet",
        "2.4 GHz",
        section_id="default_radio0",
    )
    assert num.native_value == 20

    wifi_iface.txpower = 14
    assert num.native_value == 14
