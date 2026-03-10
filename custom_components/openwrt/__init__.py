"""The OpenWrt integration.

Provides deep integration with OpenWrt routers including:
- System monitoring (CPU, memory, storage, temperature)
- Network monitoring (interfaces, bandwidth, connected devices)
- Wireless management (WPS, radio control)
- Device tracking
- Firmware update detection (official & custom builds)
- Service management
- Remote commands
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr

from .api.luci_rpc import LuciRpcAuthError, LuciRpcError
from .api.ssh import SshAuthError, SshError
from .api.ubus import UbusAuthError, UbusError
from .const import (
    ATTR_MANUFACTURER,
    DATA_CLIENT,
    DATA_COORDINATOR,
    DOMAIN,
    PLATFORMS,
    SERVICE_EXEC,
    SERVICE_INIT,
    SERVICE_REBOOT,
)
from .coordinator import OpenWrtDataCoordinator, create_client

_LOGGER = logging.getLogger(__name__)

type OpenWrtConfigEntry = ConfigEntry


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up the OpenWrt integration (YAML not supported, config flow only)."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: OpenWrtConfigEntry) -> bool:
    """Set up OpenWrt from a config entry."""
    client = create_client(dict(entry.data))

    try:
        await client.connect()
    except (UbusAuthError, LuciRpcAuthError, SshAuthError) as err:
        raise ConfigEntryAuthFailed(f"Authentication failed: {err}") from err
    except (UbusError, LuciRpcError, SshError) as err:
        raise ConfigEntryNotReady(
            f"Cannot connect to {entry.data[CONF_HOST]}: {err}"
        ) from err

    coordinator = OpenWrtDataCoordinator(hass, entry, client)

    await coordinator.async_config_entry_first_refresh()

    device_info = coordinator.data.device_info if coordinator.data else None
    if device_info:
        device_registry = dr.async_get(hass)
        device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, entry.data[CONF_HOST])},
            manufacturer=device_info.release_distribution or ATTR_MANUFACTURER,
            model=device_info.model or device_info.board_name,
            name=device_info.hostname or entry.title,
            sw_version=device_info.firmware_version,
            hw_version=device_info.board_name,
            configuration_url=f"http://{entry.data[CONF_HOST]}",
        )

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        DATA_COORDINATOR: coordinator,
        DATA_CLIENT: client,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    if not hass.services.has_service(DOMAIN, SERVICE_REBOOT):
        _register_services(hass)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: OpenWrtConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        entry_data = hass.data[DOMAIN].pop(entry.entry_id)
        coordinator: OpenWrtDataCoordinator = entry_data[DATA_COORDINATOR]
        await coordinator.async_shutdown()

    return unload_ok


async def _async_update_listener(
    hass: HomeAssistant, entry: OpenWrtConfigEntry
) -> None:
    """Handle options update."""
    await hass.config_entries.async_reload(entry.entry_id)


def _register_services(hass: HomeAssistant) -> None:
    """Register integration services."""
    import voluptuous as vol  # noqa: PLC0415
    from homeassistant.helpers import config_validation as cv  # noqa: PLC0415

    async def _handle_reboot(call: ServiceCall) -> None:
        """Handle reboot service call."""
        entry_id = call.data.get("entry_id")
        for eid, data in hass.data[DOMAIN].items():
            if entry_id and eid != entry_id:
                continue
            client = data[DATA_CLIENT]
            await client.reboot()

    async def _handle_exec(call: ServiceCall) -> None:
        """Handle execute command service call."""
        entry_id = call.data["entry_id"]
        command = call.data["command"]
        if entry_id in hass.data[DOMAIN]:
            client = hass.data[DOMAIN][entry_id][DATA_CLIENT]
            result = await client.execute_command(command)
            _LOGGER.info("Command result from %s: %s", entry_id, result)

    async def _handle_init(call: ServiceCall) -> None:
        """Handle manage service call."""
        entry_id = call.data["entry_id"]
        service_name = call.data["service_name"]
        action = call.data["action"]
        if entry_id in hass.data[DOMAIN]:
            client = hass.data[DOMAIN][entry_id][DATA_CLIENT]
            await client.manage_service(service_name, action)

    hass.services.async_register(
        DOMAIN,
        SERVICE_REBOOT,
        _handle_reboot,
        schema=vol.Schema(
            {
                vol.Optional("entry_id"): cv.string,
            }
        ),
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_EXEC,
        _handle_exec,
        schema=vol.Schema(
            {
                vol.Required("entry_id"): cv.string,
                vol.Required("command"): cv.string,
            }
        ),
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_INIT,
        _handle_init,
        schema=vol.Schema(
            {
                vol.Required("entry_id"): cv.string,
                vol.Required("service_name"): cv.string,
                vol.Required("action"): vol.In(
                    ["start", "stop", "restart", "enable", "disable"]
                ),
            }
        ),
    )
