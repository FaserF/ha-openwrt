"""Diagnostics support for OpenWrt integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import HomeAssistant

from .const import CONF_SSH_KEY, DATA_COORDINATOR, DOMAIN

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
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id][DATA_COORDINATOR]
    data = coordinator.data

    diag: dict[str, Any] = {
        "config_entry": async_redact_data(dict(entry.data), REDACT_KEYS),
        "options": dict(entry.options),
    }

    if data:
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
        diag["wireless_interfaces"] = [
            {
                "name": w.name,
                "ssid": w.ssid,
                "mode": w.mode,
                "channel": w.channel,
                "signal": w.signal,
                "clients_count": w.clients_count,
                "enabled": w.enabled,
                "up": w.up,
            }
            for w in data.wireless_interfaces
        ]
        diag["network_interfaces"] = [
            {
                "name": n.name,
                "up": n.up,
                "protocol": n.protocol,
                "device": n.device,
                "ipv4_address": n.ipv4_address,
                "rx_bytes": n.rx_bytes,
                "tx_bytes": n.tx_bytes,
                "uptime": n.uptime,
            }
            for n in data.network_interfaces
        ]
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
        diag["mwan_status"] = [
            {
                "interface": m.interface_name,
                "status": m.status,
                "online_ratio": m.online_ratio,
            }
            for m in data.mwan_status
        ]
        diag["services_count"] = len(data.services)
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

    return diag
