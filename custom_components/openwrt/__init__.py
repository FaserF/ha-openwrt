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

import importlib
import logging
from typing import Any, cast

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
)
from homeassistant.helpers import config_validation as cv
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
    SERVICE_BACKUP,
    SERVICE_EXEC,
    SERVICE_INIT,
    SERVICE_REBOOT,
    SERVICE_UCI_GET,
    SERVICE_UCI_SET,
    SERVICE_WOL,
)
from .coordinator import OpenWrtDataCoordinator, create_client

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

_LOGGER = logging.getLogger(__name__)

type OpenWrtConfigEntry = ConfigEntry


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up the OpenWrt integration (YAML not supported, config flow only)."""
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate old entry."""
    _LOGGER.debug("Migrating from version %s", entry.version)

    if entry.version == 1:
        # Version 2 uses MAC address as unique_id instead of IP
        client = create_client(dict(entry.data))
        try:
            await client.connect()
            device_info = await client.get_device_info()
            await client.disconnect()

            if device_info.mac_address:
                new_unique_id = dr.format_mac(device_info.mac_address)
                hass.config_entries.async_update_entry(
                    entry, unique_id=new_unique_id, version=2
                )
                _LOGGER.info(
                    "Migrated OpenWrt entry %s to version 2 (MAC: %s)",
                    entry.entry_id,
                    new_unique_id,
                )
            else:
                hass.config_entries.async_update_entry(entry, version=2)
                _LOGGER.warning(
                    "Could not get MAC for %s migration. Version bumped.",
                    entry.entry_id,
                )
        except Exception as err:
            _LOGGER.error("Migration failed for %s: %s", entry.entry_id, err)
            return False

    return True


async def async_setup_entry(hass: HomeAssistant, entry: OpenWrtConfigEntry) -> bool:
    """Set up OpenWrt from a config entry."""
    client = create_client({**entry.data, **entry.options})

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

    # Register AP devices to ensure via_device references in platforms are valid
    if coordinator.data:
        device_info = coordinator.data.device_info
        device_registry = dr.async_get(hass)
        for wifi in coordinator.data.wireless_interfaces:
            if not wifi.name:
                continue
            device_registry.async_get_or_create(
                config_entry_id=entry.entry_id,
                identifiers={(DOMAIN, f"{entry.unique_id}_ap_{wifi.name}")},
                name=f"AP {wifi.ssid or wifi.name}",
                manufacturer=device_info.release_distribution or ATTR_MANUFACTURER
                if device_info
                else ATTR_MANUFACTURER,
                model="Access Point",
                via_device=(DOMAIN, cast(str, entry.unique_id)),
            )

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        DATA_COORDINATOR: coordinator,
        DATA_CLIENT: client,
    }

    # Pre-import platforms in the background to avoid blocking the event loop
    # during async_forward_entry_setups which calls sync import_module
    for platform in PLATFORMS:
        hass.async_add_import_executor_job(
            importlib.import_module, f"custom_components.{DOMAIN}.{platform}"
        )

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

    async def _handle_uci_get(call: ServiceCall) -> ServiceResponse:
        """Handle UCI get service call."""
        entry_id = call.data["entry_id"]
        config = call.data["config"]
        section = call.data.get("section")
        option = call.data.get("option")

        if entry_id not in hass.data[DOMAIN]:
            raise vol.Invalid(f"Config entry {entry_id} not found")

        client = hass.data[DOMAIN][entry_id][DATA_CLIENT]

        cmd_parts = ["uci", "get", config]
        if section:
            cmd_parts[-1] += f".{section}"
            if option:
                cmd_parts[-1] += f".{option}"

        cmd = " ".join(cmd_parts)
        try:
            result = await client.execute_command(cmd)
            return {"value": result.strip() if result else ""}
        except Exception as err:
            raise HomeAssistantError(f"Failed to get UCI value: {err}") from err

    async def _handle_uci_set(call: ServiceCall) -> None:
        """Handle UCI set service call."""
        entry_id = call.data["entry_id"]
        config = call.data["config"]
        section = call.data["section"]
        option = call.data.get("option")
        value = call.data["value"]

        if entry_id not in hass.data[DOMAIN]:
            raise vol.Invalid(f"Config entry {entry_id} not found")

        client = hass.data[DOMAIN][entry_id][DATA_CLIENT]

        target = f"{config}.{section}"
        if option:
            target += f".{option}"

        cmd = f"uci set {target}='{value}' && uci commit {config} && reload_config"
        try:
            await client.execute_command(cmd)
        except Exception as err:
            raise HomeAssistantError(f"Failed to set UCI value: {err}") from err

    async def _handle_wol(call: ServiceCall) -> None:
        """Handle Wake-on-LAN service call."""
        entry_id = call.data["target"]
        mac = call.data["mac"]
        interface = call.data.get("interface")

        if entry_id not in hass.data[DOMAIN]:
            raise vol.Invalid(f"Config entry {entry_id} not found")

        client = hass.data[DOMAIN][entry_id][DATA_CLIENT]
        command = f"ether-wake {mac}"
        if interface:
            command = f"ether-wake -i {interface} {mac}"

        try:
            output = await client.execute_command(command)
            if output and "not found" in output.lower():
                command = command.replace("ether-wake", "etherwake")
                await client.execute_command(command)
        except Exception as err:
            raise HomeAssistantError(f"Failed to send WoL packet: {err}") from err

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

    hass.services.async_register(
        DOMAIN,
        SERVICE_WOL,
        _handle_wol,
        schema=vol.Schema(
            {
                vol.Required("target"): cv.string,
                vol.Required("mac"): cv.string,
                vol.Optional("interface"): cv.string,
            }
        ),
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_UCI_GET,
        _handle_uci_get,
        schema=vol.Schema(
            {
                vol.Required("entry_id"): cv.string,
                vol.Required("config"): cv.string,
                vol.Optional("section"): cv.string,
                vol.Optional("option"): cv.string,
            }
        ),
        supports_response=SupportsResponse.ONLY,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_UCI_SET,
        _handle_uci_set,
        schema=vol.Schema(
            {
                vol.Required("entry_id"): cv.string,
                vol.Required("config"): cv.string,
                vol.Required("section"): cv.string,
                vol.Optional("option"): cv.string,
                vol.Required("value"): cv.string,
            }
        ),
    )

    async def _handle_backup(call: ServiceCall) -> ServiceResponse:
        """Handle create backup service call."""
        entry_id = call.data["entry_id"]
        if entry_id not in hass.data[DOMAIN]:
            raise vol.Invalid(f"Config entry {entry_id} not found")

        client = hass.data[DOMAIN][entry_id][DATA_CLIENT]
        try:
            backup_path = await client.create_backup()
            return {"backup_path": backup_path}
        except Exception as err:
            raise HomeAssistantError(f"Failed to create backup: {err}") from err

    hass.services.async_register(
        DOMAIN,
        SERVICE_BACKUP,
        _handle_backup,
        schema=vol.Schema(
            {
                vol.Required("entry_id"): cv.string,
            }
        ),
        supports_response=SupportsResponse.ONLY,
    )
