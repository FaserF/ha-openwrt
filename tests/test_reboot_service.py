"""Tests for the openwrt.reboot action."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.openwrt.const import (
    DATA_CLIENT,
    DATA_COORDINATOR,
    DOMAIN,
    SERVICE_REBOOT,
)
from custom_components.openwrt.services import register_services


def _reboot_handler(hass):
    registered = {}
    hass.services = MagicMock()
    hass.services.async_register = lambda domain, service, handler, **_: (
        registered.__setitem__(service, handler)
    )
    register_services(hass)
    return registered[SERVICE_REBOOT]


def _router(name: str, error: Exception | None = None) -> dict:
    coordinator = MagicMock()
    coordinator.name = name
    coordinator.async_reboot = AsyncMock(side_effect=error)
    return {DATA_COORDINATOR: coordinator, DATA_CLIENT: MagicMock()}


@pytest.mark.asyncio
async def test_reboot_all_reports_failures_after_trying_every_router(hass) -> None:
    """One router failing does not stop the others; the failure is raised."""
    first = _router("router-a", HomeAssistantError("did not accept"))
    second = _router("router-b")
    hass.data[DOMAIN] = {
        "hostname_store": MagicMock(),
        "wireless_history": {"00:11:22:33:44:01": True},
        "entry_a": first,
        "entry_b": second,
    }
    handler = _reboot_handler(hass)
    call = MagicMock()
    call.data = {}

    with pytest.raises(HomeAssistantError, match="router-a: did not accept"):
        await handler(call)

    first[DATA_COORDINATOR].async_reboot.assert_awaited_once()
    second[DATA_COORDINATOR].async_reboot.assert_awaited_once()


@pytest.mark.asyncio
async def test_reboot_one_router(hass) -> None:
    """With entry_id, only that router is rebooted."""
    first = _router("router-a")
    second = _router("router-b")
    hass.data[DOMAIN] = {"entry_a": first, "entry_b": second}
    handler = _reboot_handler(hass)
    call = MagicMock()
    call.data = {"entry_id": "entry_b"}

    await handler(call)

    first[DATA_COORDINATOR].async_reboot.assert_not_called()
    second[DATA_COORDINATOR].async_reboot.assert_awaited_once()
