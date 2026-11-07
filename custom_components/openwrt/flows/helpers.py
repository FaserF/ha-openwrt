"""Helper functions and Markdown generators for OpenWrt config/options flows."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Mapping
from typing import Any

from homeassistant.helpers import device_registry as dr

from ..const import (
    CONF_CONSIDER_HOME,
    CONF_MANUAL_TRACKED_DEVICES,
    CONF_MQTT_BROKER,
    CONF_MQTT_PASSWORD,
    CONF_MQTT_PORT,
    CONF_MQTT_PRESENCE,
    CONF_MQTT_USERNAME,
    CONF_MQTT_ZONE,
    CONF_REDEPLOY_MQTT,
    CONF_TRACKED_DEVICES,
)

_LOGGER = logging.getLogger(__name__)

# Configuration options that require redeploying MQTT presence scripts to the router when changed
MQTT_PRESENCE_REDEPLOY_KEYS = {
    CONF_MQTT_BROKER,
    CONF_MQTT_PORT,
    CONF_MQTT_USERNAME,
    CONF_MQTT_PASSWORD,
    CONF_MQTT_ZONE,
    CONF_CONSIDER_HOME,
    CONF_TRACKED_DEVICES,
    CONF_MANUAL_TRACKED_DEVICES,
}


def should_redeploy_mqtt_presence(
    current_options: Mapping[str, Any],
    new_options: Mapping[str, Any],
) -> bool:
    """Check if MQTT presence configuration or any associated parameter has changed."""
    # 1. Newly enabled
    if new_options.get(CONF_MQTT_PRESENCE) and not current_options.get(
        CONF_MQTT_PRESENCE
    ):
        return True

    # 2. Currently enabled: check explicitly requested redeploy or parameter changes
    if new_options.get(CONF_MQTT_PRESENCE):
        if new_options.get(CONF_REDEPLOY_MQTT):
            return True
        for key in MQTT_PRESENCE_REDEPLOY_KEYS:
            if key in new_options and new_options[key] != current_options.get(key):
                return True

    return False


def should_remove_mqtt_presence(
    current_options: Mapping[str, Any],
    new_options: Mapping[str, Any],
) -> bool:
    """Check if MQTT presence was previously enabled but is now disabled."""
    return bool(
        current_options.get(CONF_MQTT_PRESENCE, False)
        and not new_options.get(CONF_MQTT_PRESENCE, False)
    )


def _build_mqtt_deploy_args(
    data: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str], Any]:
    """Build MQTT presence deployment parameters from configuration data."""
    mqtt_config = {
        "broker": data.get(CONF_MQTT_BROKER, ""),
        "port": data.get(CONF_MQTT_PORT, 1883),
        "username": data.get(CONF_MQTT_USERNAME, ""),
        "password": data.get(CONF_MQTT_PASSWORD, ""),
        "zone": data.get(CONF_MQTT_ZONE, "zone.home"),
    }

    tracked_devices: list[str] = list(data.get(CONF_TRACKED_DEVICES, []))
    manual_devices = data.get(CONF_MANUAL_TRACKED_DEVICES, "")
    if isinstance(manual_devices, str) and manual_devices.strip():
        tracked_devices.extend(manual_devices.splitlines())

    consider_home = data.get(CONF_CONSIDER_HOME)
    return mqtt_config, tracked_devices, consider_home


def _resolve_tracked_device_label(
    mac: str,
    original_mac: str,
    hostname: str | None = None,
    shared_hostnames: dict[str, str] | None = None,
    hass: Any = None,
    suffix: str = "",
) -> str:
    """Resolve human-readable label for a tracked device across multiple registries."""
    from ..helpers.mac_vendor import get_mac_vendor_info

    local = ""
    if isinstance(hostname, str) and type(hostname).__name__ != "MagicMock":
        trimmed = hostname.strip()
        if trimmed and trimmed != "*":
            local = trimmed

    shared = ""
    if isinstance(shared_hostnames, dict):
        cand_shared = shared_hostnames.get(mac, "")
        if isinstance(cand_shared, str) and type(cand_shared).__name__ != "MagicMock":
            shared = cand_shared.strip()

    ha_name = ""
    if hass is not None:
        with contextlib.suppress(Exception):
            dev_reg = dr.async_get(hass)
            if hasattr(dev_reg, "async_get_device"):
                ha_dev = dev_reg.async_get_device(
                    connections={(dr.CONNECTION_NETWORK_MAC, mac)}
                )
                if ha_dev:
                    cand = getattr(ha_dev, "name_by_user", None) or getattr(
                        ha_dev, "name", None
                    )
                    if (
                        isinstance(cand, str)
                        and type(cand).__name__ != "MagicMock"
                        and cand.strip()
                        and cand.lower() != mac.lower()
                    ):
                        ha_name = cand.strip()

    vendor_name = ""
    if not (local or shared or ha_name):
        if vendor_info := get_mac_vendor_info(mac):
            mfg, model = vendor_info
            vendor_name = f"{mfg} {model}".strip() if model else mfg

    name = local or shared or ha_name or vendor_name or original_mac
    return f"{name} ({original_mac}){suffix}"


def _generate_permission_table(
    perms: Any, translations: dict[str, str] | None = None
) -> str:
    """Generate markdown table for permissions."""

    def t(key: str, default: str) -> str:
        full_key = f"component.openwrt.entity.sensor.config_flow_ui.state.{key}"
        if translations and full_key in translations:
            return translations[full_key]
        return default

    def to_icon(val: bool) -> str:
        return "✅" if val else "❌"

    def get_missing(read: bool, write: bool, name: str, features: list[str]) -> str:
        missing = []
        if not read:
            missing.append(f"{name} {t('permissions_sensors', 'Sensors')}")
        if not write:
            # Try to translate each feature if possible
            translated_features = []
            for f in features:
                feat_key = f"permissions_feature_{f}"
                translated_features.append(t(feat_key, f.replace("_", " ").title()))

            if translated_features:
                missing.append(f"{name} ({', '.join(translated_features)})")
        return ", ".join(missing) if missing else "-"

    header = (
        f"| {t('permissions_header_subsystem', 'Subsystem')} | "
        f"{t('permissions_header_read', 'Read')} | "
        f"{t('permissions_header_write', 'Write')} | "
        f"{t('permissions_header_missing', 'Missing Features')} |\n"
    )
    header += "| --- | --- | --- | --- |\n"

    rows = []
    # System
    rows.append(
        f"| {t('permissions_sub_system', 'System')} | {to_icon(perms.read_system)} | {to_icon(perms.write_system)} | {get_missing(perms.read_system, perms.write_system, t('permissions_sub_system', 'System'), ['reboot', 'upgrade', 'backup'])} |"
    )
    # Network
    rows.append(
        f"| {t('permissions_sub_network', 'Network')} | {to_icon(perms.read_network)} | {to_icon(perms.write_network)} | {get_missing(perms.read_network, perms.write_network, t('permissions_sub_network', 'Network'), ['up_down_reconnect'])} |"
    )
    # Wireless
    rows.append(
        f"| {t('permissions_sub_wireless', 'Wireless')} | {to_icon(perms.read_wireless)} | {to_icon(perms.write_wireless)} | {get_missing(perms.read_wireless, perms.write_wireless, t('permissions_sub_wireless', 'Wireless'), ['toggle_wifi', 'wps_control'])} |"
    )
    # Firewall
    rows.append(
        f"| {t('permissions_sub_firewall', 'Firewall')} | {to_icon(perms.read_firewall)} | {to_icon(perms.write_firewall)} | {get_missing(perms.read_firewall, perms.write_firewall, t('permissions_sub_firewall', 'Firewall'), ['toggling_rules_redirects'])} |"
    )
    # Devices (Device Tracker)
    rows.append(
        f"| {t('permissions_sub_devices', 'Devices')} | {to_icon(perms.read_devices)} | {to_icon(perms.write_devices)} | {get_missing(perms.read_devices, perms.write_devices, t('permissions_sub_device', 'Device'), ['access_control', 'wake_on_lan', 'kick_client'])} |"
    )
    # VPN
    rows.append(
        f"| {t('permissions_sub_vpn', 'VPN')} | {to_icon(perms.read_vpn)} | - | -"
    )
    # SQM
    rows.append(
        f"| {t('permissions_sub_sqm', 'SQM')} | {to_icon(perms.read_sqm)} | {to_icon(perms.write_sqm)} | {get_missing(perms.read_sqm, perms.write_sqm, t('permissions_sub_sqm', 'SQM'), ['toggle_sqm', 'change_limits'])} |"
    )
    # Services
    rows.append(
        f"| {t('permissions_sub_services', 'Services')} | {to_icon(perms.read_services)} | {to_icon(perms.write_services)} | {get_missing(perms.read_services, perms.write_services, t('permissions_sub_service', 'Service'), ['start_stop_restart'])} |"
    )
    # LEDs
    rows.append(
        f"| {t('permissions_sub_leds', 'LEDs')} | {to_icon(perms.read_led)} | {to_icon(perms.write_led)} | {get_missing(perms.read_led, perms.write_led, t('permissions_sub_leds', 'LEDs'), ['control_leds'])} |"
    )
    # MWAN3
    rows.append(
        f"| {t('permissions_sub_mwan3', 'MWAN3')} | {to_icon(perms.read_mwan)} | - | -"
    )
    # Batman
    rows.append(
        f"| {t('permissions_sub_batman', 'Batman-adv')} | {to_icon(perms.read_batman)} | - | {get_missing(perms.read_batman, True, t('permissions_sub_batman', 'Batman-adv'), [])}"
    )
    # MQTT
    rows.append(
        f"| {t('permissions_sub_mqtt', 'MQTT Setup')} | - | {to_icon(perms.write_mqtt)} | {get_missing(True, perms.write_mqtt, t('permissions_sub_mqtt', 'MQTT Setup'), ['deploy_scripts'])} |"
    )

    return header + "\n".join(rows)


def _generate_package_table(
    packages: Any,
    connection_type: str | None = None,
    translations: dict[str, str] | None = None,
) -> str:
    """Generate markdown table for installed packages."""

    def to_icon(val: bool | None) -> str:
        if val is None:
            return "❓"
        return "✅" if val else "❌"

    def get_missing(
        val: bool | None,
        name: str,
        key: str | None = None,
        required: bool = False,
    ) -> str:
        if val is None:
            return "Check failed"
        if val:
            return "-"

        # Use translated name if available
        display_name = name
        if translations and key and key in translations:
            display_name = translations[key]

        if not required:
            return display_name
        return f"{display_name} (required)"

    # LuCI RPC requirement depends on connection type
    from ..const import CONNECTION_TYPE_LUCI_RPC

    luci_required = connection_type == CONNECTION_TYPE_LUCI_RPC
    luci_info = (
        "LuCI Web API (required)"
        if luci_required
        else "LuCI Web API (not needed for this connection method)"
    )

    return (
        "| Package | Installed | Missing Features |\n"
        "|---------|-----------|------------------|\n"
        f"| **sqm-scripts** | {to_icon(packages.sqm_scripts)} | {get_missing(packages.sqm_scripts, 'SQM QoS Settings', 'sqm_scripts')} |\n"
        f"| **mwan3** | {to_icon(packages.mwan3)} | {get_missing(packages.mwan3, 'MWAN3 Sensors', 'mwan3')} |\n"
        f"| **iwinfo** | {to_icon(packages.iwinfo)} | {get_missing(packages.iwinfo, 'Enhanced WiFi Info', 'iwinfo')} |\n"
        f"| **etherwake** | {to_icon(packages.etherwake)} | {get_missing(packages.etherwake, 'Wake on LAN', 'etherwake')} |\n"
        f"| **wireguard-tools** | {to_icon(packages.wireguard)} | {get_missing(packages.wireguard, 'WireGuard Sensors', 'wireguard')} |\n"
        f"| **openvpn** | {to_icon(packages.openvpn)} | {get_missing(packages.openvpn, 'OpenVPN Sensors', 'openvpn')} |\n"
        f"| **tailscale** | {to_icon(packages.tailscale)} | {get_missing(packages.tailscale, 'Tailscale Sensors', 'tailscale')} |\n"
        f"| **luci-mod-rpc** | {to_icon(packages.luci_mod_rpc)} | {get_missing(packages.luci_mod_rpc, luci_info, 'luci_mod_rpc', required=luci_required)} |\n"
        f"| **luci-app-attendedsysupgrade** | {to_icon(packages.asu)} | {get_missing(packages.asu, 'Firmware Upgrade (ASU)', 'asu')} |\n"
        f"| **kmod-batman-adv** | {to_icon(packages.batman_adv)} | {get_missing(packages.batman_adv, 'Batman-adv Mesh', 'batman_adv')} |\n"
        f"| **batctl** | {to_icon(packages.batctl)} | {get_missing(packages.batctl, 'Batman-adv Control (batctl)', 'batctl')} |\n"
        f"| **nlbwmon** | {to_icon(packages.nlbwmon)} | {get_missing(packages.nlbwmon, 'Top Bandwidth Hosts Sensor', 'nlbwmon')} |\n"
        f"| **coreutils-stty** | {to_icon(packages.stty)} | {get_missing(packages.stty, 'GPS Modem Tracking (stty)', 'stty')} |\n"
        f"| **coreutils-timeout** | {to_icon(packages.timeout)} | {get_missing(packages.timeout, 'GPS Modem Tracking (timeout)', 'timeout')} |\n"
        f"| **banip** | {to_icon(packages.ban_ip)} | {get_missing(packages.ban_ip, 'banIP Service Control & Sensors', 'ban_ip')} |\n"
        f"| **snort** | {to_icon(packages.snort)} | {get_missing(packages.snort, 'Snort IDS Alerts Sensor', 'snort')} |\n"
        f"| **openssl-util** | {to_icon(getattr(packages, 'openssl_util', None))} | {get_missing(getattr(packages, 'openssl_util', None), 'Backup Generation (OpenSSL)', 'openssl_util')} |\n"
        f"| **coreutils-base64** | {to_icon(getattr(packages, 'coreutils_base64', None))} | {get_missing(getattr(packages, 'coreutils_base64', None), 'Backup Extraction (Base64)', 'coreutils_base64')} |"
    )


def _generate_diagnostic_report(
    results: list[Any], translations: dict[str, str] | None = None
) -> str:
    """Generate markdown for diagnostic report."""

    def t(key: str, default: str) -> str:
        full_key = f"component.openwrt.entity.sensor.config_flow_ui.state.{key}"
        if translations and full_key in translations:
            return translations[full_key]
        return default

    report = [
        f"### {t('diagnostics_header', 'Connection Diagnostic Report')}",
        t(
            "diagnostics_intro",
            "The following checks were performed to identify the issue:",
        ),
        "",
    ]
    for res in results:
        icon = (
            "✅"
            if res.status == "PASS"
            else (
                "❌" if res.status == "FAIL" else "⚠️" if res.status == "WARN" else "ℹ️"
            )
        )
        report.append(f"#### {icon} {res.name}")
        report.append(f"**{t('diagnostics_label_result', 'Result')}:** {res.message}")
        if res.details:
            report.append(
                f"**{t('diagnostics_label_details', 'Details')}:** `{res.details}`"
            )
        if res.remedy:
            report.append(
                f"**💡 {t('diagnostics_label_remedy', 'Remedy')}:** {res.remedy}"
            )
        report.append("")

    return "\n".join(report)


class _SelectorProxy:
    def __getattr__(self, name: str) -> Any:
        from .. import config_flow

        sel = getattr(config_flow, "selector", None)
        if sel is not None and sel is not self:
            return getattr(sel, name)
        from homeassistant.helpers import selector as ha_selector

        return getattr(ha_selector, name)


class _TranslationProxy:
    def __getattr__(self, name: str) -> Any:
        from .. import config_flow

        trans = getattr(config_flow, "translation", None)
        if trans is not None and trans is not self:
            return getattr(trans, name)
        from homeassistant.helpers import translation as ha_trans

        return getattr(ha_trans, name)


selector = _SelectorProxy()
translation = _TranslationProxy()
