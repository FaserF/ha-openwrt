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

import asyncio
import importlib
import logging
from typing import Any

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
from homeassistant.helpers import entity_registry as er

from .api.luci_rpc import LuciRpcAuthError, LuciRpcError
from .api.ssh import SshAuthError, SshError
from .api.ubus import UbusAuthError, UbusError
from .const import (
    CONF_BACKUP_RETENTION_DAYS,
    DATA_CLIENT,
    DATA_COORDINATOR,
    DEFAULT_BACKUP_RETENTION_DAYS,
    DOMAIN,
    PLATFORMS,
    SERVICE_ADD_STATIC_LEASE,
    SERVICE_BACKUP,
    SERVICE_DELETE_STATIC_LEASE,
    SERVICE_EXEC,
    SERVICE_GENERATE_REPORT,
    SERVICE_GET_SYSTEM_LOGS,
    SERVICE_INIT,
    SERVICE_REBOOT,
    SERVICE_UCI_GET,
    SERVICE_UCI_SET,
    SERVICE_WOL,
)
from .coordinator import OpenWrtDataCoordinator, create_client

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

_LOGGER = logging.getLogger(__name__)

OpenWrtConfigEntry = ConfigEntry


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up integration."""
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate entry."""
    _LOGGER.debug("Migrating from version %s", entry.version)

    if entry.version == 1:
        # Version 2 uses MAC address as unique_id instead of IP
        client = create_client(hass, dict(entry.data))
        try:
            await client.connect()
            device_info = await client.get_device_info()

            if device_info.mac_address:
                new_unique_id = dr.format_mac(device_info.mac_address)
                hass.config_entries.async_update_entry(
                    entry,
                    unique_id=new_unique_id,
                    version=2,
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
            _LOGGER.exception("Migration failed for %s: %s", entry.entry_id, err)
            return False
        finally:
            await client.disconnect()

    return True


def _async_migrate_entity_units(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Clear stale unit_of_measurement overrides that came from old integration versions.

    When native_unit_of_measurement changes in code, Home Assistant may have
    the old unit cached as a registry override. Clearing these overrides forces
    HA to re-read the current unit from the entity on the next state write.
    """
    ent_reg = er.async_get(hass)
    entries = er.async_entries_for_config_entry(ent_reg, entry.entry_id)

    # Keys whose units we have deliberately changed between versions.
    # Clearing the override (unit_of_measurement = None) makes HA use the
    # integration's native_unit_of_measurement again.
    stale_unit_keys = {
        # Uptime: was UnitOfTime.MINUTES, now UnitOfTime.SECONDS
        "_uptime",
        # Storage: was raw bytes (no explicit unit), now UnitOfInformation.MEGABYTES
        "_storage_free_",
        "_storage_used_",
        "_storage_total_",
        "_filesystem_free",
    }

    for ent in entries:
        if ent.domain != "sensor":
            continue
        uid = ent.unique_id or ""
        if any(key in uid for key in stale_unit_keys):
            # Only clear if there IS a stored override (unit_of_measurement != None)
            if ent.unit_of_measurement is not None:
                _LOGGER.debug(
                    "Clearing stale unit override '%s' for entity %s",
                    ent.unit_of_measurement,
                    ent.entity_id,
                )
                ent_reg.async_update_entity(
                    ent.entity_id,
                    unit_of_measurement=None,
                )


async def _async_cleanup_disabled_features(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Remove entities and devices from disabled features from registries."""
    from .const import (
        CONF_ENABLE_FIREWALL,
        CONF_ENABLE_LED,
        CONF_ENABLE_LOAD,
        CONF_ENABLE_NLBWMON_SENSORS,
        CONF_ENABLE_SERVICES,
        CONF_ENABLE_SNORT_SENSORS,
        CONF_ENABLE_SQM,
        CONF_ENABLE_VPN,
        CONF_TRACK_DEVICES,
        DEFAULT_TRACK_DEVICES,
    )

    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)

    track_devices = entry.options.get(CONF_TRACK_DEVICES, DEFAULT_TRACK_DEVICES)
    enable_led = entry.options.get(CONF_ENABLE_LED, False)
    enable_firewall = entry.options.get(CONF_ENABLE_FIREWALL, False)
    enable_services = entry.options.get(CONF_ENABLE_SERVICES, False)
    enable_vpn = entry.options.get(CONF_ENABLE_VPN, False)
    enable_sqm = entry.options.get(CONF_ENABLE_SQM, False)
    enable_load = entry.options.get(CONF_ENABLE_LOAD, False)
    enable_nlbwmon = entry.options.get(CONF_ENABLE_NLBWMON_SENSORS, False)
    enable_snort = entry.options.get(CONF_ENABLE_SNORT_SENSORS, False)

    # 1. Clean up entities of disabled features
    entity_entries = er.async_entries_for_config_entry(ent_reg, entry.entry_id)
    for ent in entity_entries:
        should_remove = False
        ent_unique_id = ent.unique_id or ""

        if not track_devices:
            if (
                ent.domain == "device_tracker"
                or "_wol" in ent_unique_id
                or "_kick_" in ent_unique_id
                or "_access_" in ent_unique_id
            ):
                should_remove = True

        if not enable_led and "_led_" in ent_unique_id:
            should_remove = True

        if not enable_firewall and "_firewall_" in ent_unique_id:
            should_remove = True

        if not enable_services and "_service_" in ent_unique_id:
            should_remove = True

        if not enable_vpn and "_wg_" in ent_unique_id:
            should_remove = True

        if not enable_sqm and "_sqm_" in ent_unique_id:
            should_remove = True

        if not enable_load and "_load_" in ent_unique_id:
            should_remove = True

        if not enable_nlbwmon and "_nlbwmon_" in ent_unique_id:
            should_remove = True

        if not enable_snort and "_snort_" in ent_unique_id:
            should_remove = True

        if should_remove:
            _LOGGER.info(
                "Removing entity %s of disabled feature (unique_id=%s)",
                ent.entity_id,
                ent_unique_id,
            )
            try:
                ent_reg.async_remove(ent.entity_id)
            except KeyError:
                pass

    # 2. Clean up device registry for tracked devices if device tracking is disabled
    if not track_devices:
        entry_unique_id = entry.unique_id
        if entry_unique_id and len(entry_unique_id.replace(":", "")) == 12:
            router_id = dr.format_mac(entry_unique_id)
        elif entry_unique_id:
            router_id = entry_unique_id
        else:
            router_id = str(entry.data[CONF_HOST])

        for dev in dr.async_entries_for_config_entry(dev_reg, entry.entry_id):
            is_router_or_ap = False
            for identifier in dev.identifiers:
                if identifier[0] == DOMAIN:
                    ident_str = str(identifier[1])
                    norm_ident = ident_str.replace(":", "").lower()
                    norm_router_id = (
                        router_id.replace(":", "").lower()
                        if isinstance(router_id, str)
                        else ""
                    )
                    norm_host = (
                        str(entry.data.get(CONF_HOST, "")).replace(":", "").lower()
                    )

                    if (
                        ident_str == router_id
                        or norm_ident == norm_router_id
                        or norm_ident == norm_host
                        or "_ap_" in ident_str
                        or "_ra" in ident_str
                    ):
                        is_router_or_ap = True
                        break
            if not is_router_or_ap:
                _LOGGER.info(
                    "Removing tracked client device %s (%s) because device tracking is disabled",
                    dev.name,
                    dev.id,
                )
                dev_reg.async_remove_device(dev.id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up from a config entry."""
    client = create_client(hass, {**entry.data, **entry.options})

    try:
        try:
            await client.connect()
        except (UbusAuthError, LuciRpcAuthError, SshAuthError) as err:
            msg = f"Authentication failed: {err}"
            raise ConfigEntryAuthFailed(msg) from err
        except (UbusError, LuciRpcError, SshError) as err:
            msg = f"Cannot connect to {entry.data[CONF_HOST]}: {err}"
            raise ConfigEntryNotReady(msg) from err

        coordinator = OpenWrtDataCoordinator(hass, entry, client)

        # Initialize coordinator data which also handles device registry updates
        await coordinator.async_config_entry_first_refresh()

        # Clear any stale unit_of_measurement overrides from previous versions
        _async_migrate_entity_units(hass, entry)

        # Clean up any entities or devices from disabled features
        await _async_cleanup_disabled_features(hass, entry)

        hass.data.setdefault(DOMAIN, {})
        hass.data[DOMAIN][entry.entry_id] = {
            DATA_COORDINATOR: coordinator,
            DATA_CLIENT: client,
        }

        # Pre-import platforms in the background to avoid blocking the event loop
        # during async_forward_entry_setups which calls sync import_module
        async def _import_platform(platform: str) -> None:
            try:
                await hass.async_add_import_executor_job(
                    importlib.import_module,
                    f"custom_components.{DOMAIN}.{platform}",
                )
            except Exception:
                _LOGGER.debug("Could not pre-import platform %s", platform)

        await asyncio.gather(*(_import_platform(platform) for platform in PLATFORMS))

        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

        if not hass.services.has_service(DOMAIN, SERVICE_REBOOT):
            _register_services(hass)

        entry.async_on_unload(entry.add_update_listener(_async_update_listener))

        return True
    except Exception:
        await client.disconnect()
        raise


async def async_unload_entry(hass: HomeAssistant, entry: OpenWrtConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        entry_data = hass.data[DOMAIN].pop(entry.entry_id)
        coordinator: OpenWrtDataCoordinator = entry_data[DATA_COORDINATOR]
        await coordinator.async_shutdown()

    return unload_ok


async def _async_update_listener(
    hass: HomeAssistant,
    entry: OpenWrtConfigEntry,
) -> None:
    """Handle options update."""
    await hass.config_entries.async_reload(entry.entry_id)


def _register_services(hass: HomeAssistant) -> None:
    """Register services."""

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
            msg = f"Config entry {entry_id} not found"
            raise vol.Invalid(msg)

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
            msg = f"Failed to get UCI value: {err}"
            raise HomeAssistantError(msg) from err

    async def _handle_uci_set(call: ServiceCall) -> None:
        """Handle UCI set service call."""
        entry_id = call.data["entry_id"]
        config = call.data["config"]
        section = call.data["section"]
        option = call.data.get("option")
        value = call.data["value"]

        if entry_id not in hass.data[DOMAIN]:
            msg = f"Config entry {entry_id} not found"
            raise vol.Invalid(msg)

        client = hass.data[DOMAIN][entry_id][DATA_CLIENT]

        target = f"{config}.{section}"
        if option:
            target += f".{option}"

        cmd = f"uci set {target}='{value}' && uci commit {config} && reload_config"
        try:
            await client.execute_command(cmd)
        except Exception as err:
            msg = f"Failed to set UCI value: {err}"
            raise HomeAssistantError(msg) from err

    async def _handle_wol(call: ServiceCall) -> None:
        """Handle Wake-on-LAN service call."""
        entry_id = call.data["target"]
        mac = call.data.get("mac")
        device_id = call.data.get("device_id")
        entity_id = call.data.get("entity_id")
        interface = call.data.get("interface")

        if entry_id not in hass.data[DOMAIN]:
            msg = f"Config entry {entry_id} not found"
            raise vol.Invalid(msg)

        if not mac:
            if entity_id:
                ent_reg = er.async_get(hass)
                entity = ent_reg.async_get(entity_id)
                if entity and entity.device_id:
                    device_id = entity.device_id

            if device_id:
                dev_reg = dr.async_get(hass)
                device = dev_reg.async_get(device_id)
                if device:
                    for conn in device.connections:
                        if conn[0] == dr.CONNECTION_NETWORK_MAC:
                            mac = conn[1]
                            break
                    if not mac:
                        for ident in device.identifiers:
                            if ident[0] == DOMAIN:
                                potential_mac = ident[1]
                                if len(potential_mac.replace(":", "")) == 12:
                                    mac = potential_mac
                                    break

        if not mac:
            raise HomeAssistantError("Could not resolve a MAC address for Wake-on-LAN")

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
            msg = f"Failed to send WoL packet: {err}"
            raise HomeAssistantError(msg) from err

    hass.services.async_register(
        DOMAIN,
        SERVICE_REBOOT,
        _handle_reboot,
        schema=vol.Schema(
            {
                vol.Optional("entry_id"): cv.string,
            },
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
            },
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
                    ["start", "stop", "restart", "enable", "disable"],
                ),
            },
        ),
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_WOL,
        _handle_wol,
        schema=vol.Schema(
            {
                vol.Required("target"): cv.string,
                vol.Optional("mac"): cv.string,
                vol.Optional("device_id"): cv.string,
                vol.Optional("entity_id"): cv.string,
                vol.Optional("interface"): cv.string,
            },
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
            },
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
            },
        ),
    )

    async def _handle_backup(call: ServiceCall) -> ServiceResponse:
        """Handle create backup service call."""
        import os

        entry_id = call.data["entry_id"]
        custom_path = call.data.get("download_path")

        if entry_id not in hass.data[DOMAIN]:
            msg = f"Config entry {entry_id} not found"
            raise vol.Invalid(msg)

        client = hass.data[DOMAIN][entry_id][DATA_CLIENT]
        try:
            backup_path = await client.create_backup()
            if not backup_path:
                raise HomeAssistantError("Backup creation returned empty path")

            filename = os.path.basename(backup_path)
            if custom_path:
                local_dir = custom_path
                if not os.path.isabs(local_dir):
                    local_dir = hass.config.path(local_dir)
            else:
                local_dir = hass.config.path("openwrt_backups")

            os.makedirs(local_dir, exist_ok=True)
            local_path = os.path.join(local_dir, filename)

            # Download the backup file from the router
            download_success = await client.download_file(backup_path, local_path)

            # Clean up the remote backup file
            try:
                await client.execute_command(f"rm -f {backup_path}")
            except Exception as clean_err:
                _LOGGER.warning(
                    "Could not delete remote backup file %s: %s", backup_path, clean_err
                )

            if not download_success:
                raise HomeAssistantError(
                    "Failed to download the backup file from the router"
                )

            # Perform backup retention cleanup
            try:
                # Find the config entry options to get the retention days
                entry = hass.config_entries.async_get_entry(entry_id)
                retention_days = DEFAULT_BACKUP_RETENTION_DAYS
                if entry and entry.options:
                    retention_days = entry.options.get(
                        CONF_BACKUP_RETENTION_DAYS, DEFAULT_BACKUP_RETENTION_DAYS
                    )

                import time

                now = time.time()
                retention_seconds = retention_days * 86400

                for f in os.listdir(local_dir):
                    if f.startswith("backup-ha-") and f.endswith(".tar.gz"):
                        f_path = os.path.join(local_dir, f)
                        try:
                            mtime = os.path.getmtime(f_path)
                            if now - mtime > retention_seconds:
                                os.remove(f_path)
                                _LOGGER.info(
                                    "Removed old backup file %s due to retention policy (%s days)",
                                    f_path,
                                    retention_days,
                                )
                        except Exception as file_err:
                            _LOGGER.warning(
                                "Could not check/remove old backup file %s: %s",
                                f_path,
                                file_err,
                            )
            except Exception as cleanup_err:
                _LOGGER.warning(
                    "Error performing backup retention cleanup: %s", cleanup_err
                )

            return {
                "backup_path": backup_path,
                "local_path": local_path,
                "filename": filename,
            }
        except Exception as err:
            msg = f"Failed to create backup: {err}"
            raise HomeAssistantError(msg) from err

    hass.services.async_register(
        DOMAIN,
        SERVICE_BACKUP,
        _handle_backup,
        schema=vol.Schema(
            {
                vol.Required("entry_id"): cv.string,
                vol.Optional("download_path"): cv.string,
            },
        ),
        supports_response=SupportsResponse.ONLY,
    )

    async def _handle_generate_report(call: ServiceCall) -> ServiceResponse:
        """Handle generate system report service call."""
        entry_id = call.data["entry_id"]
        if entry_id not in hass.data[DOMAIN]:
            msg = f"Config entry {entry_id} not found"
            raise vol.Invalid(msg)

        coordinator: OpenWrtDataCoordinator = hass.data[DOMAIN][entry_id][
            DATA_COORDINATOR
        ]
        data = coordinator.data
        if not data:
            return {"report": "No data available"}

        report = f"# OpenWrt System Report - {data.device_info.hostname}\n\n"
        report += f"**Model:** {data.device_info.model}\n"
        report += f"**Firmware:** {data.device_info.firmware_version}\n"
        report += f"**Uptime:** {data.device_info.uptime}\n\n"

        report += "## System Resources\n"
        report += f"- CPU Load: {data.system_resources.load_1min}, {data.system_resources.load_5min}, {data.system_resources.load_15min}\n"
        report += f"- Memory: {data.system_resources.memory_used} / {data.system_resources.memory_total} MB\n\n"

        report += "## Top Processes\n"
        for p in data.system_resources.top_processes[:10]:
            report += f"- PID {p.pid}: {p.command} ({p.cpu_usage}% CPU, {p.user})\n"

        report += "\n## Recent Logs\n"
        report += "`\n"
        report += "\n".join(data.system_logs[-20:])
        report += "\n`\n"

        return {"report": report}

    async def _handle_add_static_lease(call: ServiceCall) -> None:
        """Handle add static lease service call."""
        entry_id = call.data["entry_id"]
        mac = call.data["mac"]
        ip = call.data["ip"]
        name = call.data.get("name")

        if entry_id not in hass.data[DOMAIN]:
            msg = f"Config entry {entry_id} not found"
            raise vol.Invalid(msg)

        client = hass.data[DOMAIN][entry_id][DATA_CLIENT]

        del_script = f"""m="{mac}"
i=0
while true; do
  val=$(uci get dhcp.@host[$i].mac 2>/dev/null)
  [ -z "$val" ] && break
  if [ "$(echo "$val" | tr 'A-Z' 'a-z')" = "$(echo "$m" | tr 'A-Z' 'a-z')" ]; then
    uci delete dhcp.@host[$i]
    continue
  fi
  i=$((i+1))
done"""

        add_parts = [
            "uci add dhcp host",
            f"uci set dhcp.@host[-1].mac='{mac}'",
            f"uci set dhcp.@host[-1].ip='{ip}'",
        ]
        if name:
            add_parts.append(f"uci set dhcp.@host[-1].name='{name}'")

        add_parts.extend(
            [
                "uci commit dhcp",
                "/etc/init.d/dnsmasq restart || /etc/init.d/odhcpd restart",
            ]
        )

        script = f"{del_script}\n" + "\n".join(add_parts)

        try:
            await client.execute_command(script)
        except Exception as err:
            msg = f"Failed to add static lease: {err}"
            raise HomeAssistantError(msg) from err

    async def _handle_delete_static_lease(call: ServiceCall) -> None:
        """Handle delete static lease service call."""
        entry_id = call.data["entry_id"]
        mac = call.data["mac"]

        if entry_id not in hass.data[DOMAIN]:
            msg = f"Config entry {entry_id} not found"
            raise vol.Invalid(msg)

        client = hass.data[DOMAIN][entry_id][DATA_CLIENT]

        script = f"""m="{mac}"
i=0
while true; do
  val=$(uci get dhcp.@host[$i].mac 2>/dev/null)
  [ -z "$val" ] && break
  if [ "$(echo "$val" | tr 'A-Z' 'a-z')" = "$(echo "$m" | tr 'A-Z' 'a-z')" ]; then
    uci delete dhcp.@host[$i]
    continue
  fi
  i=$((i+1))
done
uci commit dhcp
/etc/init.d/dnsmasq restart || /etc/init.d/odhcpd restart"""

        try:
            await client.execute_command(script)
        except Exception as err:
            msg = f"Failed to delete static lease: {err}"
            raise HomeAssistantError(msg) from err

    hass.services.async_register(
        DOMAIN,
        SERVICE_GENERATE_REPORT,
        _handle_generate_report,
        schema=vol.Schema(
            {
                vol.Required("entry_id"): cv.string,
            },
        ),
        supports_response=SupportsResponse.ONLY,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_ADD_STATIC_LEASE,
        _handle_add_static_lease,
        schema=vol.Schema(
            {
                vol.Required("entry_id"): cv.string,
                vol.Required("mac"): cv.string,
                vol.Required("ip"): cv.string,
                vol.Optional("name"): cv.string,
            },
        ),
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_DELETE_STATIC_LEASE,
        _handle_delete_static_lease,
        schema=vol.Schema(
            {
                vol.Required("entry_id"): cv.string,
                vol.Required("mac"): cv.string,
            },
        ),
    )

    async def _handle_get_system_logs(call: ServiceCall) -> ServiceResponse:
        """Handle get system/kernel logs service call."""
        entry_id = call.data["entry_id"]
        count = call.data.get("count", 50)
        log_type = call.data.get("log_type", "system")

        if entry_id not in hass.data[DOMAIN]:
            msg = f"Config entry {entry_id} not found"
            raise vol.Invalid(msg)

        client = hass.data[DOMAIN][entry_id][DATA_CLIENT]
        try:
            if log_type == "kernel":
                logs = await client.get_dmesg_logs(count=count)
            else:
                logs = await client.get_system_logs(count=count)
            return {"logs": logs}
        except Exception as err:
            msg = f"Failed to retrieve logs: {err}"
            raise HomeAssistantError(msg) from err

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_SYSTEM_LOGS,
        _handle_get_system_logs,
        schema=vol.Schema(
            {
                vol.Required("entry_id"): cv.string,
                vol.Optional("count"): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=1000)
                ),
                vol.Optional("log_type"): vol.In(["system", "kernel"]),
            },
        ),
        supports_response=SupportsResponse.ONLY,
    )
