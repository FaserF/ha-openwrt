"""The router device links to its gateway even when another config entry owns it."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.helpers import device_registry as dr

from custom_components.openwrt.api.base import OpenWrtData
from custom_components.openwrt.coordinator import OpenWrtDataCoordinator

ROUTER_MAC = "00:00:5e:00:53:04"
GATEWAY_MAC = "00:00:5e:00:53:01"


async def _router_via_device(gateway_matches: list[MagicMock]) -> object:
    """Run the registry pass and return the via_device_id given to the router."""
    entry = MagicMock()
    entry.entry_id = "this_entry"
    entry.unique_id = ROUTER_MAC
    entry.data = {"host": "192.0.2.4"}
    entry.options = {}
    coordinator = OpenWrtDataCoordinator(MagicMock(), entry, AsyncMock())
    coordinator.router_id = ROUTER_MAC

    dev_registry = MagicMock()
    dev_registry.devices = {}
    gw_conn = {(dr.CONNECTION_NETWORK_MAC, GATEWAY_MAC)}
    dev_registry.async_get_devices.side_effect = lambda **kw: (
        gateway_matches if kw.get("connections") == gw_conn else []
    )

    data = OpenWrtData()
    data.device_info = MagicMock()
    data.device_info.mac_address = ROUTER_MAC
    data.device_info.gateway_mac = GATEWAY_MAC
    data.device_info.release_distribution = "OpenWrt"

    with (
        patch(
            "homeassistant.helpers.device_registry.async_get", return_value=dev_registry
        ),
        patch(
            "custom_components.openwrt.coordinator.dr.async_entries_for_config_entry",
            return_value=[],
        ),
    ):
        await coordinator._async_update_device_registry(data)

    dev_registry.async_get_devices.assert_any_call(connections=gw_conn)
    router_call = dev_registry.async_get_or_create.call_args_list[0]
    return router_call.kwargs.get("via_device_id")


def _device(dev_id: str, config_entry_id: str) -> MagicMock:
    """Build a registry device owned by the given config entry."""
    device = MagicMock(id=dev_id, config_entry_id=config_entry_id)
    device.config_entries = {config_entry_id}
    return device


@pytest.mark.asyncio
async def test_gateway_owned_by_another_entry_is_linked() -> None:
    """The upstream router belongs to another config entry and must still match."""
    via = await _router_via_device([_device("gw", "upstream_router_entry")])
    assert via == "gw"


@pytest.mark.asyncio
async def test_own_client_device_for_gateway_mac_is_skipped() -> None:
    """A client device this entry tracks for the gateway's MAC is not the gateway."""
    via = await _router_via_device(
        [_device("own_client", "this_entry"), _device("gw", "upstream_router_entry")]
    )
    assert via == "gw"


@pytest.mark.asyncio
async def test_no_gateway_device_leaves_router_unlinked() -> None:
    """Without a gateway device in another entry, the router has no via device."""
    via = await _router_via_device([_device("own_client", "this_entry")])
    assert via is None
