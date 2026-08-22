"""Tests for physical wireless radio switches."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.openwrt.api.base import OpenWrtData, WirelessInterface
from custom_components.openwrt.switch import (
    OpenWrtRadioSwitch,
    OpenWrtWirelessSwitch,
    _add_wireless_switches,
    async_setup_entry,
)


@pytest.mark.asyncio
async def test_setup_removes_only_orphaned_radio_switches(hass) -> None:
    """Remove radio switches whose physical radio is no longer discovered."""
    coordinator = MagicMock()
    coordinator.data = OpenWrtData(
        wireless_interfaces=[WirelessInterface(name="phy0-ap0", radio="radio0")]
    )
    coordinator.async_add_listener = MagicMock()
    coordinator._device_history = {}

    entry = MagicMock(entry_id="test_entry")
    entry.data = {}
    entry.options = {}
    entry.async_on_unload = MagicMock()
    hass.data = {
        "openwrt": {entry.entry_id: {"coordinator": coordinator, "client": MagicMock()}}
    }
    hass.add_job = MagicMock(side_effect=lambda callback: callback())

    active = MagicMock(
        domain="switch",
        entity_id="switch.radio0",
        unique_id="test_entry_radio_radio0",
    )
    orphaned = MagicMock(
        domain="switch",
        entity_id="switch.radio1",
        unique_id="test_entry_radio_radio1",
    )
    registry = MagicMock()
    with (
        patch("custom_components.openwrt.switch.er.async_get", return_value=registry),
        patch(
            "custom_components.openwrt.switch.er.async_entries_for_config_entry",
            return_value=[active, orphaned],
        ),
    ):
        await async_setup_entry(hass, entry, MagicMock())

    registry.async_remove.assert_called_once_with("switch.radio1")


@pytest.mark.asyncio
async def test_radio_switches_are_deduplicated_and_control_radio() -> None:
    """Expose and control each physical radio exactly once."""
    coordinator = MagicMock()
    coordinator.data = OpenWrtData(
        wireless_interfaces=[
            WirelessInterface(
                name="phy0-ap0",
                radio="radio0",
                band="2.4 GHz",
                radio_enabled=True,
            ),
            WirelessInterface(
                name="phy0-ap1",
                radio="radio0",
                band="2.4 GHz",
                radio_enabled=True,
            ),
            WirelessInterface(
                name="phy1-ap0",
                radio="radio1",
                band="5 GHz",
                radio_enabled=False,
            ),
        ]
    )
    coordinator.interface_to_stable_id = {}
    coordinator.router_id = "router_id"
    coordinator.async_request_refresh = AsyncMock()
    coordinator.hass.async_create_task = MagicMock(
        side_effect=lambda task: task.close()
    )

    entry = MagicMock(entry_id="test_entry", unique_id="stale_router_id")
    client = MagicMock()
    client.set_radio_enabled = AsyncMock(return_value=True)
    entities = []

    with patch("custom_components.openwrt.switch.DeviceInfo", side_effect=dict):
        _add_wireless_switches(coordinator, entry, client, entities, set())

    radios = [entity for entity in entities if isinstance(entity, OpenWrtRadioSwitch)]
    assert [entity._attr_unique_id for entity in radios] == [
        "test_entry_radio_radio0",
        "test_entry_radio_radio1",
    ]
    assert [entity.is_on for entity in radios] == [True, False]
    assert {entity._attr_device_info["name"] for entity in radios} == {
        "2.4 GHz",
        "5 GHz",
    }
    assert {entity._attr_name for entity in radios} == {"Physical radio"}

    radio0_entities = [
        entity
        for entity in entities
        if isinstance(entity, (OpenWrtRadioSwitch, OpenWrtWirelessSwitch))
        and getattr(entity, "_radio", None) == "radio0"
    ]
    assert len(radio0_entities) == 3
    assert next(iter(radios[0]._attr_device_info["identifiers"])) == (
        "openwrt",
        "router_id_radio_radio0",
    )
    assert radios[0]._attr_device_info["via_device"] == (
        "openwrt",
        "router_id",
    )
    ssid_entities = [
        entity
        for entity in radio0_entities
        if isinstance(entity, OpenWrtWirelessSwitch)
    ]
    assert {
        next(iter(entity._attr_device_info["identifiers"])) for entity in ssid_entities
    } == {
        ("openwrt", "router_id_ap_phy0-ap0"),
        ("openwrt", "router_id_ap_phy0-ap1"),
    }
    assert {entity._attr_device_info["via_device"] for entity in ssid_entities} == {
        ("openwrt", "router_id_radio_radio0")
    }

    await radios[1].async_turn_on()

    client.set_radio_enabled.assert_awaited_once_with("radio1", True)
    assert all(
        wifi.radio_enabled
        for wifi in coordinator.data.wireless_interfaces
        if wifi.radio == "radio1"
    )


@pytest.mark.asyncio
async def test_radio_change_recomputes_combined_ssid_state() -> None:
    """Keep each SSID's combined state aligned with radio and interface state."""
    active = WirelessInterface(
        name="phy0-ap0",
        radio="radio0",
        enabled=True,
        interface_enabled=True,
        radio_enabled=True,
    )
    disabled = WirelessInterface(
        name="phy0-ap1",
        radio="radio0",
        enabled=False,
        interface_enabled=False,
        radio_enabled=True,
    )
    coordinator = MagicMock()
    coordinator.router_id = "router_id"
    coordinator.data = OpenWrtData(wireless_interfaces=[active, disabled])
    coordinator.async_request_refresh = AsyncMock()
    coordinator.hass.async_create_task = MagicMock(
        side_effect=lambda task: task.close()
    )
    client = MagicMock()
    client.set_radio_enabled = AsyncMock(return_value=True)
    switch = OpenWrtRadioSwitch(
        coordinator,
        MagicMock(entry_id="test_entry", unique_id="router_id"),
        client,
        "radio0",
        "2.4 GHz",
    )

    await switch.async_turn_off()
    assert active.enabled is False
    assert disabled.enabled is False

    await switch.async_turn_on()
    assert active.enabled is True
    assert disabled.enabled is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("other_interface_enabled", "expected_disable_radio"),
    [(False, True), (True, False)],
)
async def test_disabling_ssid_powers_down_only_an_unused_radio(
    other_interface_enabled: bool,
    expected_disable_radio: bool,
) -> None:
    """Power down a radio only after its last configured SSID is disabled."""
    target = WirelessInterface(
        name="phy0-ap0",
        section="main",
        ssid="Main",
        radio="radio0",
        interface_enabled=True,
        radio_enabled=True,
    )
    sibling = WirelessInterface(
        name="phy0-ap1",
        section="guest",
        ssid="Guest",
        radio="radio0",
        interface_enabled=other_interface_enabled,
        radio_enabled=True,
    )
    coordinator = MagicMock()
    coordinator.data = OpenWrtData(wireless_interfaces=[target, sibling])
    coordinator.async_request_refresh = AsyncMock()
    coordinator.hass.async_create_task = MagicMock(
        side_effect=lambda task: task.close()
    )
    client = MagicMock()
    client.set_wireless_network_enabled = AsyncMock(return_value=True)
    switch = OpenWrtWirelessSwitch(
        coordinator,
        MagicMock(entry_id="test_entry", unique_id="router_id"),
        client,
        target.name,
        target.ssid,
        section_id=target.section,
        radio=target.radio,
    )
    assert switch.is_on is True

    await switch.async_turn_off()

    client.set_wireless_network_enabled.assert_awaited_once_with(
        "main",
        "radio0",
        False,
        disable_radio=expected_disable_radio,
    )
    assert target.interface_enabled is False
    assert target.enabled is False
    assert target.radio_enabled is not expected_disable_radio
    assert switch.is_on is False


@pytest.mark.asyncio
async def test_enabling_ssid_also_enables_its_radio() -> None:
    """Restore the physical radio whenever an SSID is enabled."""
    wifi = WirelessInterface(
        name="phy1-ap0",
        section="main_5g",
        ssid="Main",
        radio="radio1",
        interface_enabled=False,
        radio_enabled=False,
    )
    coordinator = MagicMock()
    coordinator.data = OpenWrtData(wireless_interfaces=[wifi])
    coordinator.async_request_refresh = AsyncMock()
    coordinator.hass.async_create_task = MagicMock(
        side_effect=lambda task: task.close()
    )
    client = MagicMock()
    client.set_wireless_network_enabled = AsyncMock(return_value=True)
    switch = OpenWrtWirelessSwitch(
        coordinator,
        MagicMock(entry_id="test_entry", unique_id="router_id"),
        client,
        wifi.name,
        wifi.ssid,
        section_id=wifi.section,
        radio=wifi.radio,
    )
    assert switch.is_on is False

    await switch.async_turn_on()

    client.set_wireless_network_enabled.assert_awaited_once_with(
        "main_5g",
        "radio1",
        True,
        disable_radio=False,
    )
    assert wifi.interface_enabled is True
    assert wifi.enabled is True
    assert wifi.radio_enabled is True
    assert switch.is_on is True


@pytest.mark.asyncio
async def test_enabling_ssid_recomputes_enabled_sibling_state() -> None:
    """Restore configured sibling SSIDs when their shared radio is enabled."""
    target = WirelessInterface(
        name="phy1-ap0",
        section="main_5g",
        ssid="Main",
        radio="radio1",
        enabled=False,
        interface_enabled=False,
        radio_enabled=False,
    )
    sibling = WirelessInterface(
        name="phy1-ap1",
        section="guest_5g",
        ssid="Guest",
        radio="radio1",
        enabled=False,
        interface_enabled=True,
        radio_enabled=False,
    )
    coordinator = MagicMock()
    coordinator.data = OpenWrtData(wireless_interfaces=[target, sibling])
    coordinator.async_request_refresh = AsyncMock()
    coordinator.hass.async_create_task = MagicMock(
        side_effect=lambda task: task.close()
    )
    client = MagicMock()
    client.set_wireless_network_enabled = AsyncMock(return_value=True)
    switch = OpenWrtWirelessSwitch(
        coordinator,
        MagicMock(entry_id="test_entry", unique_id="router_id"),
        client,
        target.name,
        target.ssid,
        section_id=target.section,
        radio=target.radio,
    )

    await switch.async_turn_on()

    assert target.enabled is True
    assert sibling.enabled is True


@pytest.mark.asyncio
async def test_rejected_radio_change_preserves_coordinator_state() -> None:
    """Do not optimistically update a radio after a rejected command."""
    wifi = WirelessInterface(name="wlan0", radio="radio0", radio_enabled=True)
    coordinator = MagicMock()
    coordinator.router_id = "router_id"
    coordinator.data = OpenWrtData(wireless_interfaces=[wifi])
    client = MagicMock()
    client.set_radio_enabled = AsyncMock(return_value=False)
    switch = OpenWrtRadioSwitch(
        coordinator,
        MagicMock(entry_id="test_entry", unique_id="router_id"),
        client,
        "radio0",
        "2.4 GHz",
    )

    with pytest.raises(HomeAssistantError):
        await switch.async_turn_off()

    assert wifi.radio_enabled is True


@pytest.mark.asyncio
async def test_rejected_ssid_change_preserves_coordinator_state() -> None:
    """Do not optimistically update an SSID after a rejected command."""
    wifi = WirelessInterface(
        name="wlan0",
        section="main",
        radio="radio0",
        interface_enabled=True,
        radio_enabled=True,
    )
    coordinator = MagicMock()
    coordinator.router_id = "router_id"
    coordinator.data = OpenWrtData(wireless_interfaces=[wifi])
    client = MagicMock()
    client.set_wireless_network_enabled = AsyncMock(return_value=False)
    switch = OpenWrtWirelessSwitch(
        coordinator,
        MagicMock(entry_id="test_entry", unique_id="router_id"),
        client,
        wifi.name,
        "Main",
        section_id=wifi.section,
        radio=wifi.radio,
    )

    with pytest.raises(HomeAssistantError):
        await switch.async_turn_off()

    assert wifi.interface_enabled is True
    assert wifi.radio_enabled is True


@pytest.mark.asyncio
async def test_section_matched_ssid_gets_optimistic_update() -> None:
    """Update an SSID whose runtime name differs from its UCI section."""
    wifi = WirelessInterface(
        name="wlan0",
        section="main",
        radio="radio0",
        interface_enabled=True,
        radio_enabled=True,
    )
    coordinator = MagicMock()
    coordinator.router_id = "router_id"
    coordinator.data = OpenWrtData(wireless_interfaces=[wifi])
    coordinator.async_request_refresh = AsyncMock()
    coordinator.hass.async_create_task = MagicMock(
        side_effect=lambda task: task.close()
    )
    client = MagicMock()
    client.set_wireless_network_enabled = AsyncMock(return_value=True)
    switch = OpenWrtWirelessSwitch(
        coordinator,
        MagicMock(entry_id="test_entry", unique_id="router_id"),
        client,
        "main",
        "Main",
        section_id="main",
        radio="radio0",
    )

    await switch.async_turn_off()

    assert wifi.interface_enabled is False
    assert wifi.enabled is False


@pytest.mark.asyncio
async def test_empty_radio_does_not_modify_unrelated_interfaces() -> None:
    """Do not update radio state on interfaces when switch has no radio assigned."""
    wifi_target = WirelessInterface(
        name="wlan0",
        section="main",
        radio=None,
        interface_enabled=True,
        radio_enabled=True,
        enabled=True,
    )
    wifi_unrelated = WirelessInterface(
        name="wlan1",
        section="guest",
        radio=None,
        interface_enabled=True,
        radio_enabled=True,
        enabled=True,
    )
    coordinator = MagicMock()
    coordinator.router_id = "router_id"
    coordinator.data = OpenWrtData(wireless_interfaces=[wifi_target, wifi_unrelated])
    coordinator.async_request_refresh = AsyncMock()
    coordinator.hass.async_create_task = MagicMock(
        side_effect=lambda task: task.close()
    )
    client = MagicMock()
    client.set_wireless_enabled = AsyncMock(return_value=True)
    switch = OpenWrtWirelessSwitch(
        coordinator,
        MagicMock(entry_id="test_entry", unique_id="router_id"),
        client,
        "main",
        "Main",
        section_id="main",
        radio="",
    )

    await switch.async_turn_off()

    assert wifi_target.interface_enabled is False
    assert wifi_target.enabled is False
    assert wifi_unrelated.interface_enabled is True
    assert wifi_unrelated.enabled is True
    assert wifi_unrelated.radio_enabled is True
