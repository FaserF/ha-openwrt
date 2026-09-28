"""Tests for cleanup and removal of client devices the entry no longer tracks."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.openwrt.api.base import ConnectedDevice, OpenWrtData
from custom_components.openwrt.const import DATA_COORDINATOR, DOMAIN
from custom_components.openwrt.coordinator import OpenWrtDataCoordinator

ROUTER_ID = "00:00:5e:00:53:00"
TRACKED_MAC = "00:00:5e:00:53:01"
STALE_MAC = "00:00:5e:00:53:02"
UNTRACKED_WITH_ENTITY_MAC = "00:00:5e:00:53:03"


def _entry(options: dict) -> MagicMock:
    entry = MagicMock()
    entry.entry_id = "test_entry_id"
    entry.unique_id = ROUTER_ID
    entry.data = {"host": "192.0.2.1"}
    entry.options = options
    return entry


def _client_device(
    dev_id: str, mac: str, config_entries: set[str] | None = None
) -> MagicMock:
    device = MagicMock(
        id=dev_id,
        model="Tracked device",
        config_entries=config_entries or {"test_entry_id"},
        identifiers={(DOMAIN, mac)},
        via_device_id="dev_router",
        disabled_by=None,
        entry_type=None,
    )
    # MagicMock(name=...) names the mock itself; the attribute must be set after.
    device.name = mac
    return device


async def _run_cleanup(options: dict, stale_shared: bool = False) -> MagicMock:
    """Run the registry pass over three client devices and return the registry."""
    entry = _entry(options)
    coordinator = OpenWrtDataCoordinator(MagicMock(), entry, AsyncMock())
    coordinator.router_id = ROUTER_ID

    stale_entries = {"test_entry_id", "other_entry"} if stale_shared else None
    devices = [
        _client_device("dev_tracked", TRACKED_MAC),
        _client_device("dev_stale", STALE_MAC, stale_entries),
        _client_device("dev_untracked_entity", UNTRACKED_WITH_ENTITY_MAC),
    ]
    entities = {
        "dev_untracked_entity": [MagicMock(config_entry_id=entry.entry_id)],
    }

    dev_registry = MagicMock()
    dev_registry.devices = {}
    dev_registry.async_get_device.return_value = MagicMock(id="dev_router")

    with (
        patch(
            "custom_components.openwrt.coordinator.dr.async_get",
            return_value=dev_registry,
        ),
        patch(
            "custom_components.openwrt.coordinator.dr.async_entries_for_config_entry",
            return_value=devices,
        ),
        patch("custom_components.openwrt.coordinator.er.async_get"),
        patch(
            "custom_components.openwrt.coordinator.er.async_entries_for_device",
            side_effect=lambda _reg, dev_id, **_kw: entities.get(dev_id, []),
        ),
    ):
        await coordinator._async_update_device_registry(OpenWrtData())

    return dev_registry


def _removed(dev_registry: MagicMock) -> set[str]:
    return {call.args[0] for call in dev_registry.async_remove_device.call_args_list}


def _detached(dev_registry: MagicMock) -> set[str]:
    return {
        call.args[0]
        for call in dev_registry.async_update_device.call_args_list
        if "remove_config_entry_id" in call.kwargs
    }


CLIENTS = {"dev_tracked", "dev_stale", "dev_untracked_entity"}


@pytest.mark.asyncio
async def test_untracked_client_device_without_entities_is_removed() -> None:
    """A client outside the whitelist with no entities left is removed."""
    dev_registry = await _run_cleanup(
        {"track_devices": True, "tracked_devices": [TRACKED_MAC]}
    )

    assert _removed(dev_registry) & CLIENTS == {"dev_stale"}
    assert not _detached(dev_registry)


@pytest.mark.asyncio
async def test_untracked_shared_client_device_is_only_detached() -> None:
    """Before HA 2026.9 a device could be shared; then only this entry is dropped."""
    dev_registry = await _run_cleanup(
        {"track_devices": True, "tracked_devices": [TRACKED_MAC]}, stale_shared=True
    )

    dev_registry.async_update_device.assert_any_call(
        "dev_stale", remove_config_entry_id="test_entry_id"
    )
    assert _detached(dev_registry) == {"dev_stale"}
    assert not _removed(dev_registry) & CLIENTS


@pytest.mark.asyncio
async def test_client_devices_untouched_without_whitelist() -> None:
    """With no whitelist every client is tracked, so nothing is swept."""
    dev_registry = await _run_cleanup({"track_devices": True})

    assert not _removed(dev_registry) & CLIENTS
    assert not _detached(dev_registry)


def _hass_with_coordinator(coordinator: MagicMock) -> MagicMock:
    hass = MagicMock()
    hass.data = {DOMAIN: {"test_entry_id": {DATA_COORDINATOR: coordinator}}}
    return hass


def _coordinator(connected: list[str]) -> MagicMock:
    coordinator = MagicMock()
    coordinator.active_device_identifiers = {
        (DOMAIN, ROUTER_ID),
        (DOMAIN, f"{ROUTER_ID}_radio_radio0"),
    }
    data = OpenWrtData()
    data.connected_devices = [ConnectedDevice(mac=mac) for mac in connected]
    data.dhcp_leases = []
    coordinator.data = data
    return coordinator


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("identifier", "expected"),
    [
        (ROUTER_ID, False),
        (f"{ROUTER_ID}_radio_radio0", False),
        (TRACKED_MAC, False),
        (STALE_MAC, True),
        (f"{ROUTER_ID}_radio_radio9", True),
    ],
)
async def test_remove_config_entry_device(identifier: str, expected: bool) -> None:
    """Live devices are refused; stale ones may be removed from the UI."""
    from custom_components.openwrt import async_remove_config_entry_device

    hass = _hass_with_coordinator(_coordinator([TRACKED_MAC]))
    device = MagicMock(identifiers={(DOMAIN, identifier)})

    assert await async_remove_config_entry_device(hass, _entry({}), device) is expected


@pytest.mark.asyncio
async def test_remove_config_entry_device_entry_not_loaded() -> None:
    """An unloaded entry has nothing to recreate the device, so allow removal."""
    from custom_components.openwrt import async_remove_config_entry_device

    hass = MagicMock()
    hass.data = {}
    device = MagicMock(identifiers={(DOMAIN, ROUTER_ID)})

    assert await async_remove_config_entry_device(hass, _entry({}), device) is True
