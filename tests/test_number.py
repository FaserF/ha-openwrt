"""Tests for the number platform of the OpenWrt integration."""

from unittest.mock import MagicMock, patch

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
    """Test a valid wireless TX power creates a controllable entity."""
    wifi = WirelessInterface(
        name="phy0-ap0",
        ssid="MyNet",
        radio="radio0",
        txpower=20,
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

    assert entity.native_value == 20
    assert entity._attr_name == "Transmit power"
    assert entity._attr_native_min_value == 1

    # Test setting native value
    await entity.async_set_native_value(15)
    client.execute_command.assert_called_with(
        "uci set wireless.radio0.txpower='15' && uci commit wireless && wifi reload"
    )
    coordinator.async_request_refresh.assert_called()


def test_txpower_number_status_matching() -> None:
    """Verify OpenWrtTxPowerNumber matches TX power by physical radio."""
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
        "radio0",
        "2.4 GHz",
    )
    assert num.native_value == 20

    wifi_iface.txpower = 14
    assert num.native_value == 14


def test_txpower_number_uses_canonical_router_id_without_entry_unique_id() -> None:
    """Attach TX power to the coordinator router when unique_id is not set yet."""
    coordinator = MagicMock()
    coordinator.router_id = "canonical_router"
    coordinator.data = OpenWrtData(
        wireless_interfaces=[
            WirelessInterface(name="wlan0", radio="radio0", txpower=20)
        ]
    )
    entry = MagicMock(entry_id="test_entry", unique_id=None)

    with patch("custom_components.openwrt.number.DeviceInfo", side_effect=dict):
        entity = OpenWrtTxPowerNumber(coordinator, entry, "radio0", "2.4 GHz")

    assert entity._attr_device_info["identifiers"] == {
        ("openwrt", "canonical_router_radio_radio0")
    }
    assert entity._attr_device_info["via_device"] == (
        "openwrt",
        "canonical_router",
    )


def test_txpower_number_max_value() -> None:
    """Verify OpenWrtTxPowerNumber native_max_value respects txpower_offset."""
    from custom_components.openwrt.coordinator import OpenWrtDataCoordinator
    from custom_components.openwrt.number import OpenWrtTxPowerNumber

    config_entry = MagicMock()
    config_entry.entry_id = "test_entry"
    config_entry.unique_id = "test_router"
    config_entry.options = {"update_interval": 60}
    config_entry.data = {CONF_HOST: "192.168.1.1"}

    coordinator = OpenWrtDataCoordinator(MagicMock(), config_entry, MagicMock())
    wifi_iface = WirelessInterface(
        name="wlan0",
        section="default_radio0",
        radio="radio0",
        txpower=9,
        txpower_offset=9,
    )
    coordinator.data = MagicMock()
    coordinator.data.wireless_interfaces = [wifi_iface]

    num = OpenWrtTxPowerNumber(
        coordinator,
        config_entry,
        "radio0",
        "2.4 GHz",
    )
    assert num.native_max_value == 9.0


@pytest.mark.asyncio
async def test_unknown_zero_txpower_does_not_create_entity() -> None:
    """Do not expose a fabricated 0 dBm value when OpenWrt reports no power."""
    coordinator = MagicMock()
    coordinator.data = OpenWrtData(
        wireless_interfaces=[
            WirelessInterface(name="phy0-ap0", radio="radio0", txpower=0)
        ],
        permissions=OpenWrtPermissions(write_wireless=True),
    )
    coordinator.async_add_listener = MagicMock()

    entry = MagicMock(entry_id="test_entry", unique_id="router_mac")
    entry.async_on_unload = MagicMock()
    hass = MagicMock()
    hass.data = {"openwrt": {"test_entry": {"coordinator": coordinator}}}
    added_entities = []

    stale = MagicMock(
        entity_id="number.radio1_tx_power",
        domain="number",
        unique_id="test_entry_txpower_radio1",
    )
    registry = MagicMock()
    with (
        patch(
            "custom_components.openwrt.number.er.async_get",
            return_value=registry,
        ),
        patch(
            "custom_components.openwrt.number.er.async_entries_for_config_entry",
            return_value=[stale],
        ),
    ):
        await async_setup_entry(hass, entry, added_entities.extend)

    assert added_entities == []
    registry.async_remove.assert_called_once_with("number.radio1_tx_power")


@pytest.mark.asyncio
async def test_txpower_number_is_created_once_per_radio() -> None:
    """Create one TX power control for a radio shared by multiple SSIDs."""
    wireless_interfaces = [
        WirelessInterface(
            name="phy0-ap0",
            section="main_24g",
            ssid="Main",
            radio="radio0",
            band="2.4 GHz",
            txpower=20,
        ),
        WirelessInterface(
            name="phy0-ap1",
            section="guest_24g",
            ssid="Guest",
            radio="radio0",
            band="2.4 GHz",
            txpower=20,
        ),
        WirelessInterface(
            name="phy1-ap0",
            section="main_5g",
            ssid="Main",
            radio="radio1",
            band="5 GHz",
            txpower=23,
        ),
    ]
    coordinator = MagicMock()
    coordinator.router_id = "02:00:00:00:00:01"
    coordinator.data = OpenWrtData(
        wireless_interfaces=wireless_interfaces,
        permissions=OpenWrtPermissions(write_wireless=True),
    )
    coordinator.async_add_listener = MagicMock()

    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.unique_id = "020000000001"
    entry.async_on_unload = MagicMock()

    added_entities: list[OpenWrtTxPowerNumber] = []
    hass = MagicMock()
    hass.data = {"openwrt": {"test_entry": {"coordinator": coordinator}}}

    with patch("custom_components.openwrt.number.DeviceInfo", side_effect=dict):
        await async_setup_entry(hass, entry, added_entities.extend)

    assert len(added_entities) == 2
    assert {entity._attr_unique_id for entity in added_entities} == {
        "test_entry_txpower_radio0",
        "test_entry_txpower_radio1",
    }
    assert all(entity.entity_registry_enabled_default for entity in added_entities)
    assert {entity.native_value for entity in added_entities} == {20, 23}
    assert {
        next(iter(entity._attr_device_info["identifiers"])) for entity in added_entities
    } == {
        ("openwrt", "02:00:00:00:00:01_radio_radio0"),
        ("openwrt", "02:00:00:00:00:01_radio_radio1"),
    }
    assert {entity._attr_device_info["name"] for entity in added_entities} == {
        "2.4 GHz",
        "5 GHz",
    }
