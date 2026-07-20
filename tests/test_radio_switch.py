"""Tests for physical wireless radio switches."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.openwrt.api.base import OpenWrtData, WirelessInterface
from custom_components.openwrt.switch import (
    OpenWrtRadioSwitch,
    _add_wireless_switches,
)


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
    coordinator.async_request_refresh = AsyncMock()
    coordinator.hass.async_create_task = MagicMock(
        side_effect=lambda task: task.close()
    )

    entry = MagicMock(entry_id="test_entry", unique_id="router_id")
    client = MagicMock()
    client.set_radio_enabled = AsyncMock(return_value=True)
    entities = []

    _add_wireless_switches(coordinator, entry, client, entities, set())

    radios = [entity for entity in entities if isinstance(entity, OpenWrtRadioSwitch)]
    assert [entity._attr_unique_id for entity in radios] == [
        "test_entry_radio_radio0",
        "test_entry_radio_radio1",
    ]
    assert [entity.is_on for entity in radios] == [True, False]

    await radios[1].async_turn_on()

    client.set_radio_enabled.assert_awaited_once_with("radio1", True)
    assert all(
        wifi.radio_enabled
        for wifi in coordinator.data.wireless_interfaces
        if wifi.radio == "radio1"
    )
