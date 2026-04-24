"""Diagnostics support for OpenWrt integration."""

from __future__ import annotations

import math
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import CONF_SSH_KEY, DATA_COORDINATOR, DOMAIN
from .coordinator import OpenWrtDataCoordinator

REDACT_KEYS = {
    CONF_PASSWORD,
    CONF_SSH_KEY,
    "password",
    "ssh_key",
    "external_ip",
    "ipv4_address",
    "ipv6_address",
    "host",
    "mac",
    "public_key",
    "endpoint",
    "allowed_ips",
}


def _to_json_safe(obj: Any) -> Any:
    """Convert an object to a JSON-serializable format."""
    if isinstance(obj, bool) or obj is None:
        return obj
    if isinstance(obj, (int, str)):
        return obj
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return str(obj)
        return obj
    if isinstance(obj, (list, tuple, set)):
        return [_to_json_safe(i) for i in obj]
    if isinstance(obj, dict):
        return {str(k): _to_json_safe(v) for k, v in obj.items()}
    if hasattr(obj, "__dict__"):
        return _to_json_safe(obj.__dict__)
    return str(obj)


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    try:
        coordinator: OpenWrtDataCoordinator = hass.data[DOMAIN][entry.entry_id][
            DATA_COORDINATOR
        ]
        data = coordinator.data
    except KeyError, AttributeError:
        return {"error": "Coordinator not found"}

    diag: dict[str, Any] = {
        "config_entry": async_redact_data(dict(entry.data), REDACT_KEYS),
        "options": async_redact_data(dict(entry.options), REDACT_KEYS),
        "coordinator_last_update_success": coordinator.last_update_success,
    }

    if data:
        try:
            diag["device_info"] = {
                "hostname": data.device_info.hostname,
                "model": data.device_info.model,
                "board_name": data.device_info.board_name,
                "firmware_version": data.device_info.firmware_version,
                "kernel_version": data.device_info.kernel_version,
                "architecture": data.device_info.architecture,
                "target": data.device_info.target,
                "release_distribution": data.device_info.release_distribution,
                "release_version": data.device_info.release_version,
                "release_revision": data.device_info.release_revision,
                "mac_address": data.device_info.mac_address,
                "uptime": data.device_info.uptime,
            }
            diag["system_resources"] = {
                "memory_total": data.system_resources.memory_total,
                "memory_used": data.system_resources.memory_used,
                "memory_free": data.system_resources.memory_free,
                "load_1min": data.system_resources.load_1min,
                "load_5min": data.system_resources.load_5min,
                "load_15min": data.system_resources.load_15min,
                "uptime": data.system_resources.uptime,
                "temperature": data.system_resources.temperature,
                "filesystem_total": data.system_resources.filesystem_total,
                "filesystem_used": data.system_resources.filesystem_used,
            }
            diag["connected_devices_count"] = len(data.connected_devices)
            diag["wireless_clients_count"] = sum(
                1 for d in data.connected_devices if d.is_wireless
            )
            diag["firmware"] = {
                "upgradable": data.firmware_upgradable,
                "current_version": data.firmware_current_version,
                "latest_version": data.firmware_latest_version,
                "is_custom_build": data.is_custom_build,
            }
            diag["wireless_interfaces"] = [
                {
                    "name": w.name,
                    "ssid": w.ssid,
                    "mode": w.mode,
                    "channel": w.channel,
                    "frequency": w.frequency,
                    "signal": w.signal,
                    "noise": w.noise,
                    "enabled": w.enabled,
                    "up": w.up,
                    "clients_count": w.clients_count,
                }
                for w in data.wireless_interfaces
            ]
            diag["mwan_status"] = [
                {
                    "interface": m.interface_name,
                    "status": m.status,
                    "online_ratio": m.online_ratio,
                }
                for m in data.mwan_status
            ]
            diag["packages"] = {
                "sqm_scripts": data.packages.sqm_scripts,
                "mwan3": data.packages.mwan3,
                "iwinfo": data.packages.iwinfo,
                "etherwake": data.packages.etherwake,
                "wireguard": data.packages.wireguard,
                "openvpn": data.packages.openvpn,
                "luci_mod_rpc": data.packages.luci_mod_rpc,
                "asu": data.packages.asu,
                "adblock": data.packages.adblock,
                "simple_adblock": data.packages.simple_adblock,
                "ban_ip": data.packages.ban_ip,
            }
            diag["top_processes"] = [
                {
                    "pid": p.pid,
                    "user": p.user,
                    "cpu": p.cpu_usage,
                    "command": p.command,
                }
                for p in data.system_resources.top_processes[:5]
            ]
        except Exception as err:
            diag["data_error"] = str(err)

    # Registry debug: safe serialization (all primitives)
    reg_devices = []
    reg_entities = []

    try:
        dev_reg = dr.async_get(hass)
        ent_reg = er.async_get(hass)

        for _dev in dev_reg.devices.values():
            # Only include devices related to this config entry
            if entry.entry_id not in _dev.config_entries:
                continue

            reg_devices.append(
                {
                    "id": str(_dev.id),
                    "name": str(_dev.name or ""),
                    "model": str(_dev.model or ""),
                    "manufacturer": str(_dev.manufacturer or ""),
                    "sw_version": str(_dev.sw_version or ""),
                    "via_device_id": str(_dev.via_device_id or ""),
                    "config_entries": list(_dev.config_entries),
                    "identifiers": [list(i) for i in _dev.identifiers],
                    "connections": [list(c) for c in _dev.connections],
                }
            )

        for _ent in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
            reg_entities.append(
                {
                    "entity_id": str(_ent.entity_id),
                    "unique_id": str(_ent.unique_id),
                    "domain": str(_ent.domain),
                    "disabled": _ent.disabled_by is not None,
                    "device_id": str(_ent.device_id),
                    "original_name": str(_ent.original_name or ""),
                }
            )
    except Exception as err:
        diag["registry_debug_error"] = str(err)

    diag["registry_debug"] = {
        "devices": reg_devices,
        "entities": reg_entities,
        "config_entry_unique_id": entry.unique_id,
        "config_entry_id": entry.entry_id,
    }

    # Ensure all data is JSON safe to prevent corruption/download failures
    return _to_json_safe(diag)
