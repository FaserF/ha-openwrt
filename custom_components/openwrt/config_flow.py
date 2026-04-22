"""Config flow for OpenWrt integration.

Supports three connection methods:
- ubus (HTTP/HTTPS JSON-RPC)
- LuCI RPC (via LuCI web interface)
- SSH (password or key-based authentication)

Supports adding multiple routers, device auto-discovery, options flow,
and re-authentication.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import socket
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import selector, translation
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo
from homeassistant.helpers.service_info.ssdp import SsdpServiceInfo
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .api.luci_rpc import (
    LuciRpcAuthError,
    LuciRpcConnectionError,
    LuciRpcError,
    LuciRpcPackageMissingError,
    LuciRpcSslError,
    LuciRpcTimeoutError,
)
from .api.ssh import (
    SshAuthError,
    SshConnectionError,
    SshError,
    SshKeyError,
    SshTimeoutError,
)
from .api.ubus import (
    UbusAuthError,
    UbusConnectionError,
    UbusError,
    UbusPackageMissingError,
    UbusPermissionError,
    UbusSslError,
    UbusTimeoutError,
)
from .const import (
    CONF_ASU_URL,
    CONF_AUTO_BACKUP,
    CONF_CONNECTION_TYPE,
    CONF_CONSIDER_HOME,
    CONF_CUSTOM_FIRMWARE_REPO,
    CONF_DHCP_SOFTWARE,
    CONF_SSH_KEY,
    CONF_TRACK_DEVICES,
    CONF_TRACK_WIRED,
    CONF_UBUS_PATH,
    CONF_UPDATE_INTERVAL,
    CONF_USE_SSL,
    CONF_VERIFY_SSL,
    CONNECTION_TYPE_LUCI_RPC,
    CONNECTION_TYPE_SSH,
    CONNECTION_TYPE_UBUS,
    DEFAULT_CONSIDER_HOME,
    DEFAULT_PORT_SSH,
    DEFAULT_PORT_UBUS,
    DEFAULT_PORT_UBUS_SSL,
    DEFAULT_TRACK_DEVICES,
    DEFAULT_TRACK_WIRED,
    DEFAULT_UBUS_PATH,
    DEFAULT_UPDATE_INTERVAL,
    DEFAULT_USE_SSL,
    DEFAULT_USERNAME,
    DEFAULT_VERIFY_SSL,
    DOCS_URL,
    DOMAIN,
)
from .coordinator import create_client

_LOGGER = logging.getLogger(__name__)

CONNECTION_TYPE_MAP = {
    CONNECTION_TYPE_UBUS: "ubus (HTTP/HTTPS)",
    CONNECTION_TYPE_LUCI_RPC: "LuCI RPC",
    CONNECTION_TYPE_SSH: "SSH (not recommended)",
}


def _generate_permission_table(perms: Any) -> str:
    """Generate markdown table for permissions."""

    def to_icon(val: bool) -> str:
        return "✅" if val else "❌"

    def get_missing(read: bool, write: bool, name: str, features: list[str]) -> str:
        missing = []
        if not read:
            missing.append(f"{name} Sensors")
        if not write:
            missing.extend(features)
        return ", ".join(missing) if missing else "-"

    return (
        "| Subsystem | Read | Write | Missing Features |\n"
        "|-----------|------|-------|------------------|\n"
        f"| **System** | {to_icon(perms.read_system)} | {to_icon(perms.write_system)} | {get_missing(perms.read_system, perms.write_system, 'System', ['Reboot', 'Upgrade', 'Backup'])} |\n"
        f"| **Network** | {to_icon(perms.read_network)} | {to_icon(perms.write_network)} | {get_missing(perms.read_network, perms.write_network, 'Interface', ['Up/Down/Reconnect'])} |\n"
        f"| **Wireless** | {to_icon(perms.read_wireless)} | {to_icon(perms.write_wireless)} | {get_missing(perms.read_wireless, perms.write_wireless, 'WiFi', ['Toggle WiFi', 'WPS Control'])} |\n"
        f"| **Firewall** | {to_icon(perms.read_firewall)} | {to_icon(perms.write_firewall)} | {get_missing(perms.read_firewall, perms.write_firewall, 'Firewall', ['Toggling Rules/Redirects', 'Access Control'])} |\n"
        f"| **Devices** | {to_icon(perms.read_devices)} | {to_icon(perms.write_devices)} | {get_missing(perms.read_devices, perms.write_devices, 'Device', ['Wake on LAN', 'Kick Client'])} |\n"
        f"| **VPN** | {to_icon(perms.read_vpn)} | - | {'-' if perms.read_vpn else 'WireGuard/OpenVPN Sensors'} |\n"
        f"| **SQM** | {to_icon(perms.read_sqm)} | {to_icon(perms.write_sqm)} | {get_missing(perms.read_sqm, perms.write_sqm, 'SQM', ['Toggle SQM', 'Change limits'])} |\n"
        f"| **Services**| {to_icon(perms.read_services)} | {to_icon(perms.write_services)} | {get_missing(perms.read_services, perms.write_services, 'Service', ['Start/Stop/Restart'])} |\n"
        f"| **LEDs** | {to_icon(perms.read_led)} | {to_icon(perms.write_led)} | {get_missing(perms.read_led, perms.write_led, 'LED', ['Control LEDs'])} |\n"
        f"| **MWAN3** | {to_icon(perms.read_mwan)} | - | {'-' if perms.read_mwan else 'Multi-WAN Sensors'} |"
    )


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
    from .const import CONNECTION_TYPE_LUCI_RPC

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
        f"| **luci-mod-rpc** | {to_icon(packages.luci_mod_rpc)} | {get_missing(packages.luci_mod_rpc, luci_info, 'luci_mod_rpc', required=luci_required)} |\n"
        f"| **luci-app-attendedsysupgrade** | {to_icon(packages.asu)} | {get_missing(packages.asu, 'Firmware Upgrade (ASU)', 'asu')} |"
    )


class OpenWrtConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for OpenWrt."""

    VERSION = 2

    def __init__(self) -> None:
        """Initialize flow."""
        self._data: dict[str, Any] = {}
        self._device_info: dict[str, Any] = {}
        self._discovered_name: str | None = None
        self._permissions: Any = None
        self._packages: Any = None
        self._homeassistant_user_exists: bool = False
        self._provision_error: str | None = None
        self._generated_password: str | None = None
        self._discovered_host: str | None = None
        self._discovered_routers: list[dict[str, str]] = []
        self._ubus_restricted: bool = False

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OpenWrtOptionsFlow:
        """Get the options flow."""
        return OpenWrtOptionsFlow(config_entry)

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Handle the initial step - Welcome Screen."""
        if user_input is not None:
            if user_input.get("flow_type") == "manual":
                return await self.async_step_manual_entry()
            return await self.async_step_discovery()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "flow_type",
                        default="manual",
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=["discovery", "manual"],
                            translation_key="flow_type",
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        ),
                    ),
                },
            ),
            description_placeholders={"docs_url": DOCS_URL},
        )

    async def async_step_discovery(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Scan for routers in the background."""
        if user_input is not None or self._discovered_routers:
            if not self._discovered_routers:
                return self.async_show_form(
                    step_id="discovery",
                    errors={"base": "no_devices_found"},
                    description_placeholders={"discovery_info": ""},
                )

            if len(self._discovered_routers) == 1:
                return await self.async_step_confirm_discovery()

            return await self.async_step_select_device()

        # Perform scanning
        potential_hosts = await self._async_get_potential_hosts()
        tasks = [self._async_probe_router(host) for host in potential_hosts]
        results = await asyncio.gather(*tasks)

        existing_hosts = {
            entry.data.get(CONF_HOST)
            for entry in self.hass.config_entries.async_entries(DOMAIN)
        }

        for router_info in results:
            if router_info:
                host = router_info["host"]
                # Skip already configured routers or duplicates
                if host not in existing_hosts and not any(
                    r["host"] == host for r in self._discovered_routers
                ):
                    self._discovered_routers.append(router_info)

        if not self._discovered_routers:
            return await self.async_step_manual_entry()

        return await self.async_step_discovery(user_input={})

    async def _async_get_potential_hosts(self) -> set[str]:
        """Gather potential router IPs from defaults and HA network adapters."""
        potential_hosts: set[str] = {"192.168.1.1", "192.168.0.1", "10.0.0.1"}

        with contextlib.suppress(Exception):
            from homeassistant.components import network

            adapters = await network.async_get_adapters(self.hass)
            for adapter in adapters:
                for ipv4 in adapter.get("ipv4", []):
                    local_ip = ipv4.get("address")
                    if local_ip and (parts := local_ip.split(".")) and len(parts) == 4:
                        potential_hosts.add(".".join([*parts[:-1], "1"]))
                        potential_hosts.add(".".join([*parts[:-1], "254"]))

        return potential_hosts

    async def async_step_manual_entry(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Handle manual entry if discovery fails or if chosen."""
        if user_input is not None:
            self._data.update(user_input)
            if user_input[CONF_CONNECTION_TYPE] == CONNECTION_TYPE_SSH:
                return await self.async_step_ssh()
            return await self.async_step_credentials()

        return self.async_show_form(
            step_id="manual_entry",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST, default="192.168.1.1"): str,
                    vol.Required(
                        CONF_CONNECTION_TYPE,
                        default=CONNECTION_TYPE_LUCI_RPC,
                    ): vol.In(CONNECTION_TYPE_MAP),
                },
            ),
        )

    async def async_step_select_device(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Handle selecting a router when multiple are found."""
        if user_input is not None:
            host = user_input["device"]
            if host == "manual":
                return await self.async_step_manual_entry()

            router = next(r for r in self._discovered_routers if r["host"] == host)
            self._discovered_routers = [router]
            self._discovered_name = router.get("hostname")
            return await self.async_step_confirm_discovery()

        device_options = {
            r[
                "host"
            ]: f"{r.get('hostname', 'OpenWrt')} ({r['host']}) - {r['method'].upper()} [Available: {', '.join(r['capabilities'])}]"
            for r in self._discovered_routers
        }
        # Add manual entry option
        device_options["manual"] = "Enter details manually..."

        return self.async_show_form(
            step_id="select_device",
            data_schema=vol.Schema(
                {
                    vol.Required("device"): vol.In(device_options),
                },
            ),
            description_placeholders={"public_info": ""},
        )

    def _is_excluded(
        self,
        host: str,
        hostname: str | None = None,
        properties: Mapping[str, Any] | None = None,
    ) -> bool:
        """Centralized check for non-router OpenWrt devices like vacuums."""
        exclusions = [
            "valetudo",
            "vacuum",
            "dreame",
            "roborock",
            "cleaner",
            "mop",
            "robot",
            "airpurifier",
            "washer",
            "dryer",
            "fridge",
            "oven",
            "camera",
            "tuya",
            "broadlink",
            "shelly",
            "opnsense",
            "pfsense",
            "fortigate",
            "mikrotik",
            "ubnt",
            "unifi",
            "tplink",
            "asuswrt",
            "ddwrt",
            "padavan",
            "proxmox",
            "esxi",
            "truenas",
            "freenas",
        ]

        # 1. Check hostname/name
        search_target = ""
        if hostname:
            search_target += hostname.lower()
        if properties:
            # Check all property values for exclusions
            for val in properties.values():
                if isinstance(val, str):
                    search_target += " " + val.lower()

        if any(exc in search_target for exc in exclusions):
            _LOGGER.info(
                "Definitively excluded %s (%s) as a non-router device",
                host,
                hostname,
            )
            return True

        return False

    async def _async_probe_router(
        self,
        host: str,
        hostname: str | None = None,
    ) -> dict[str, Any] | None:
        """Probe a host and return metadata if it's OpenWrt."""
        _LOGGER.debug("Probing router logic for %s (hint: %s)", host, hostname)

        # 1. Definitive exclusions
        if self._is_excluded(host, hostname):
            return None

        effective_hostname = hostname
        if not effective_hostname or effective_hostname == host:
            try:
                loop = asyncio.get_running_loop()
                result = await loop.run_in_executor(None, socket.gethostbyaddr, host)
                effective_hostname = result[0]
            except Exception:
                effective_hostname = effective_hostname or host

        # Re-check exclusion after reverse DNS
        if self._is_excluded(host, effective_hostname):
            return None

        # 2. Port accessibility check
        if not await self._async_check_reachable(
            host, CONNECTION_TYPE_UBUS
        ) and not await self._async_check_reachable(host, CONNECTION_TYPE_SSH):
            return None

        # 3. Deep probe for LuCI/metadata and capabilities
        capabilities = []
        best_method = None

        # Check ubus/luci with the already resolved hostname
        probed_methods = await self._async_probe_openwrt(host, effective_hostname)
        if probed_methods:
            capabilities.extend(probed_methods)
            if CONNECTION_TYPE_UBUS in probed_methods:
                best_method = CONNECTION_TYPE_UBUS
            else:
                best_method = probed_methods[0]

        # Check SSH
        if await self._async_check_reachable(host, CONNECTION_TYPE_SSH):
            # Robust SSH banner check is already inside check_reachable (or should be)
            if "ssh" not in capabilities:
                capabilities.append("ssh")
            if not best_method:
                best_method = "ssh"

        if best_method:
            return {
                "host": host,
                "hostname": effective_hostname,
                "capabilities": capabilities,
                "method": best_method,
            }

        return None

    async def _async_check_reachable(self, host: str, connection_type: str) -> bool:
        """Check if the host is reachable on the expected ports."""
        ports = [22] if connection_type == CONNECTION_TYPE_SSH else [80, 443]
        if ":" in host:
            try:
                host_part, port_str = host.split(":")
                ports = [int(port_str)]
                host = host_part
            except ValueError:
                pass

        for port in ports:
            try:
                async with asyncio.timeout(1.5):
                    reader, writer = await asyncio.open_connection(host, port)
                    if port == 22:
                        # Try to read SSH banner to reduce false positives
                        try:
                            banner = await reader.read(100)
                            banner_str = banner.decode("utf-8", errors="ignore").lower()
                            if (
                                "dropbear" not in banner_str
                                and "openwrt" not in banner_str
                            ):
                                _LOGGER.debug(
                                    "Excluded %s: SSH banner '%s' does not look like OpenWrt",
                                    host,
                                    banner_str.strip(),
                                )
                                writer.close()
                                await writer.wait_closed()
                                continue
                        except Exception:
                            # If we can't read the banner, we still allow it but it's less certain
                            pass
                    writer.close()
                    await writer.wait_closed()
                    return True
            except (
                TimeoutError,
                socket.gaierror,
                ConnectionRefusedError,
                OSError,
            ):
                continue
        return False

    async def async_step_ssdp(
        self,
        discovery_info: SsdpServiceInfo,
    ) -> ConfigFlowResult:
        """Handle SSDP auto-discovery."""
        host = (
            urlparse(discovery_info.ssdp_location or "").hostname
            or discovery_info.ssdp_location
        )
        if not host:
            return self.async_abort(reason="no_host")

        # SSDP often includes a serial number which is often the MAC
        serial = discovery_info.upnp.get("serialNumber")
        if serial:
            # Serial is often the MAC or contains it
            unique_id = (
                dr.format_mac(serial) if ":" in serial or len(serial) == 12 else serial
            )
            await self.async_set_unique_id(unique_id)
            self._abort_if_unique_id_configured(updates={CONF_HOST: host})
        else:
            await self.async_set_unique_id(host)
            self._abort_if_unique_id_configured()

        hostname = discovery_info.upnp.get("friendlyName") or discovery_info.upnp.get(
            "modelName",
        )
        if self._is_excluded(host, hostname, discovery_info.upnp):
            return self.async_abort(reason="not_openwrt")

        probe_result = await self._async_probe_router(host, hostname)
        if not probe_result:
            return self.async_abort(reason="not_openwrt")

        self._discovered_routers = [probe_result]
        self._discovered_name = probe_result.get("hostname") or f"OpenWrt ({host})"

        self.context["title_placeholders"] = {
            "name": self._discovered_name,
            "host": host,
        }

        return await self.async_step_confirm_discovery()

    async def async_step_dhcp(
        self,
        discovery_info: DhcpServiceInfo,
    ) -> ConfigFlowResult:
        """Handle DHCP auto-discovery."""
        host = discovery_info.ip
        mac = discovery_info.macaddress
        await self.async_set_unique_id(dr.format_mac(mac))
        self._abort_if_unique_id_configured(updates={CONF_HOST: host})

        probe_result = await self._async_probe_router(host, discovery_info.hostname)
        if not probe_result:
            return self.async_abort(reason="not_openwrt")

        self._discovered_routers = [probe_result]
        self._discovered_name = probe_result.get("hostname") or f"OpenWrt ({host})"

        self.context.update(
            {
                "title_placeholders": {
                    "name": self._discovered_name,
                    "host": host,
                },
            },
        )

        return await self.async_step_confirm_discovery()

    async def async_step_zeroconf(
        self,
        discovery_info: ZeroconfServiceInfo,
    ) -> ConfigFlowResult:
        """Handle Zeroconf auto-discovery."""
        host = discovery_info.host
        # Zeroconf properties might have MAC
        mac = discovery_info.properties.get("mac")
        if mac:
            await self.async_set_unique_id(dr.format_mac(mac))
            self._abort_if_unique_id_configured(updates={CONF_HOST: host})
        else:
            await self.async_set_unique_id(host)
            self._abort_if_unique_id_configured()

        if self._is_excluded(host, discovery_info.name, discovery_info.properties):
            return self.async_abort(reason="not_openwrt")

        probe_result = await self._async_probe_router(host, discovery_info.name)
        if not probe_result:
            return self.async_abort(reason="not_openwrt")

        self._discovered_routers = [probe_result]
        self._discovered_name = probe_result.get("hostname") or f"OpenWrt ({host})"

        self.context.update(
            {
                "title_placeholders": {
                    "name": self._discovered_name,
                    "host": host,
                },
            },
        )

        return await self.async_step_confirm_discovery()

    async def async_step_confirm_discovery(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Confirm the discovered device."""
        if user_input is not None:
            if user_input.get(CONF_CONNECTION_TYPE) == "manual":
                return await self.async_step_manual_entry()

            router = self._discovered_routers[-1]
            self._data[CONF_HOST] = router["host"]
            self._data[CONF_CONNECTION_TYPE] = user_input.get(
                CONF_CONNECTION_TYPE,
                router["method"],
            )
            return await self.async_step_credentials()

        router = self._discovered_routers[-1]
        capabilities = ", ".join(
            [CONNECTION_TYPE_MAP.get(c, c) for c in router.get("capabilities", [])],
        )

        schema = vol.Schema({})
        options: list[str] = list(router.get("capabilities", []))
        if "manual" not in options:
            options.append("manual")

        if len(options) > 0:
            schema = vol.Schema(
                {
                    vol.Required(
                        CONF_CONNECTION_TYPE,
                        default=router["method"],
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=options,
                            translation_key="connection_type",
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        ),
                    ),
                },
            )

        return self.async_show_form(
            step_id="confirm_discovery",
            data_schema=schema,
            description_placeholders={
                "name": self._discovered_name or "OpenWrt Router",
                "host": router["host"],
                "method": CONNECTION_TYPE_MAP.get(router["method"], router["method"]),
                "capabilities": capabilities,
            },
        )

    async def async_step_credentials(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Handle credentials step for ubus/LuCI RPC."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._data.update(user_input)

            if not user_input.get(CONF_PORT):
                if self._data.get(CONF_USE_SSL, False):
                    self._data[CONF_PORT] = DEFAULT_PORT_UBUS_SSL
                else:
                    self._data[CONF_PORT] = DEFAULT_PORT_UBUS

            error = await self._test_connection(self._data)
            if error:
                errors["base"] = error
            else:
                await self._async_set_unique_id_and_check()
                if self._data.get(CONF_USERNAME) == "root":
                    return await self.async_step_provision_user()
                return await self.async_step_permissions()

        host = self._data.get(CONF_HOST, "")
        connection_type = self._data.get(CONF_CONNECTION_TYPE, CONNECTION_TYPE_UBUS)

        # Determine if we have a hint
        auto_detected_info = ""
        if any(r["host"] == host for r in self._discovered_routers):
            hostname = self._discovered_name or host
            auto_detected_info = f"💡 Auto-detected: **{hostname}** ({host})"

        return self.async_show_form(
            step_id="credentials",
            data_schema=self._async_credentials_schema(),
            errors=errors,
            description_placeholders={
                "host": host,
                "connection_type": CONNECTION_TYPE_MAP.get(
                    connection_type,
                    connection_type,
                ),
                "auto_detected_info": auto_detected_info,
            },
        )

    def _async_credentials_schema(self) -> vol.Schema:
        """Return the schema for credentials step."""
        connection_type = self._data.get(CONF_CONNECTION_TYPE, CONNECTION_TYPE_UBUS)
        is_ubus = connection_type == CONNECTION_TYPE_UBUS

        schema_dict: dict[Any, Any] = {
            vol.Required(CONF_USERNAME, default=DEFAULT_USERNAME): str,
            vol.Required(CONF_PASSWORD): str,
            vol.Required(CONF_USE_SSL, default=DEFAULT_USE_SSL): bool,
            vol.Optional(CONF_VERIFY_SSL, default=DEFAULT_VERIFY_SSL): bool,
            vol.Optional(CONF_DHCP_SOFTWARE, default="auto"): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=["auto", "dnsmasq", "odhcpd", "none"],
                    translation_key="dhcp_software",
                    mode=selector.SelectSelectorMode.DROPDOWN,
                ),
            ),
            vol.Optional(CONF_TRACK_DEVICES, default=True): bool,
            vol.Optional(CONF_PORT): int,
        }

        if is_ubus:
            schema_dict[vol.Optional(CONF_UBUS_PATH, default=DEFAULT_UBUS_PATH)] = str

        return vol.Schema(schema_dict)

    async def async_step_ssh(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Handle SSH connection step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._data.update(user_input)

            if not user_input.get(CONF_PORT):
                self._data[CONF_PORT] = DEFAULT_PORT_SSH

            error = await self._test_connection(self._data)
            if error:
                errors["base"] = error
            else:
                await self._async_set_unique_id_and_check()
                if self._data.get(CONF_USERNAME) == "root":
                    return await self.async_step_provision_user()
                return await self.async_step_permissions()

        return self.async_show_form(
            step_id="ssh",
            data_schema=self._async_ssh_schema(),
            errors=errors,
            description_placeholders={
                "host": self._data.get(CONF_HOST, ""),
                "security_link": "https://github.com/FaserF/ha-openwrt/blob/main/SECURITY.md",
            },
        )

    def _async_ssh_schema(self) -> vol.Schema:
        """Return the schema for SSH step."""
        return vol.Schema(
            {
                vol.Required(CONF_USERNAME, default=DEFAULT_USERNAME): str,
                vol.Optional(CONF_PASSWORD): str,
                vol.Optional(CONF_SSH_KEY): str,
                vol.Optional(CONF_DHCP_SOFTWARE, default="auto"): vol.In(
                    ["auto", "dnsmasq", "odhcpd", "none"],
                ),
                vol.Optional(CONF_TRACK_DEVICES, default=True): bool,
                vol.Optional(CONF_PORT, default=DEFAULT_PORT_SSH): int,
            },
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Handle re-authentication."""
        self._data = dict(entry_data)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Handle re-authentication confirmation."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._data.update(user_input)
            error = await self._test_connection(self._data)
            if error:
                errors["base"] = error
            else:
                entry = self.hass.config_entries.async_get_entry(
                    self.context["entry_id"],
                )
                if entry:
                    self.hass.config_entries.async_update_entry(
                        entry,
                        data={**entry.data, **user_input},
                    )
                    await self.hass.config_entries.async_reload(entry.entry_id)
                    return self.async_abort(reason="reauth_successful")

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_USERNAME,
                    default=self._data.get(CONF_USERNAME, DEFAULT_USERNAME),
                ): str,
                vol.Required(CONF_PASSWORD): str,
            },
        )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=schema,
            errors=errors,
        )

    async def _test_connection(self, data: dict[str, Any]) -> str | None:
        """Test connection to the device. Returns error key or None on success."""
        _LOGGER.debug(
            "Starting connection test with data: %s",
            {k: (v if k != CONF_PASSWORD else "********") for k, v in data.items()},
        )
        try:
            client = create_client(data)
            _LOGGER.debug("Client created: %s", client)
            async with asyncio.timeout(30):
                await self._perform_connection_test(client, data)
            return None
        except Exception as err:
            _LOGGER.exception("Connection test failed: %s", err)
            return self._handle_test_error(err, data.get(CONF_USERNAME))

    async def _perform_connection_test(self, client: Any, data: dict[str, Any]) -> None:
        """Perform the actual connection and info gathering."""
        await client.connect()
        self._homeassistant_user_exists = False
        if data.get(CONF_USERNAME) == "root":
            with contextlib.suppress(Exception):
                self._homeassistant_user_exists = await client.user_exists(
                    "homeassistant"
                )

        info = await client.get_device_info()
        self._device_info = {
            "hostname": info.hostname,
            "model": info.model,
            "firmware_version": info.firmware_version,
            "mac_address": info.mac_address,
        }

        with contextlib.suppress(Exception):
            self._permissions = await client.check_permissions()
        with contextlib.suppress(Exception):
            self._packages = await client.check_packages()

        # Specific check for restricted Ubus (like Xiaomi firmwares)
        if data.get(CONF_CONNECTION_TYPE) == CONNECTION_TYPE_UBUS:
            try:
                radios = await client.get_wireless_interfaces()
                services = await client.get_services()
                if not radios and not services:
                    self._ubus_restricted = True
                    _LOGGER.info(
                        "Detected restricted Ubus on %s (0 radios, 0 services found)",
                        data.get(CONF_HOST),
                    )
            except Exception:
                self._ubus_restricted = True

        await client.disconnect()

    def _handle_test_error(self, err: Exception, username: str | None) -> str:
        """Map connection exceptions to translation keys."""
        if isinstance(
            err, (UbusAuthError, LuciRpcAuthError, SshAuthError, SshKeyError)
        ):
            _LOGGER.warning("Authentication failed: %s", err)
            return "invalid_auth"
        if isinstance(
            err, (UbusTimeoutError, LuciRpcTimeoutError, SshTimeoutError, TimeoutError)
        ):
            _LOGGER.warning("Timeout during test: %s", err)
            return "timeout"
        if isinstance(
            err,
            (
                UbusConnectionError,
                LuciRpcConnectionError,
                SshConnectionError,
                UbusError,
                LuciRpcError,
                SshError,
            ),
        ):
            _LOGGER.warning("Connection/API error: %s", err)
            return "cannot_connect"
        if isinstance(err, (UbusSslError, LuciRpcSslError)):
            _LOGGER.warning("SSL error: %s", err)
            return "ssl_error"
        if isinstance(err, (UbusPackageMissingError, LuciRpcPackageMissingError)):
            _LOGGER.warning("Package missing: %s", err)
            return "package_missing"
        if isinstance(err, UbusPermissionError):
            _LOGGER.warning("Permission error: %s", err)
            return "permission_error"

        _LOGGER.exception(
            "Unexpected error during connection test for %s: %s", username, err
        )
        return "unknown"

    async def _async_probe_openwrt(
        self,
        host: str,
        hostname: str | None = None,
    ) -> list[str]:
        """Probe a host to see if it responds like OpenWrt (LuCI/UBus)."""
        _LOGGER.debug("Probing %s (%s) for OpenWrt endpoints", host, hostname)

        if self._is_excluded(host, hostname):
            return []

        found_methods = []
        session = async_get_clientsession(self.hass)

        # 1. Try LuCI (Main endpoint and static assets)
        if await self._probe_luci(host, session):
            found_methods.append(CONNECTION_TYPE_LUCI_RPC)

        # 2. Try UBus
        if await self._probe_ubus(host, session):
            found_methods.append(CONNECTION_TYPE_UBUS)

        return list(set(found_methods))

    async def _probe_luci(self, host: str, session: aiohttp.ClientSession) -> bool:
        """Check for LuCI presence."""
        # Check main CGI endpoint
        luci_url = f"http://{host}/cgi-bin/luci/"
        with contextlib.suppress(Exception):
            async with asyncio.timeout(2):
                async with session.get(luci_url, allow_redirects=True) as resp:
                    server = resp.headers.get("Server", "").lower()
                    if "valetudo" in server or "valetudo" in resp.headers:
                        return False

                    text = await resp.text()
                    # Common non-router exclusions in text
                    if any(
                        s in text.lower()
                        for s in ["valetudo", "dreame", "roborock", "vacuum"]
                    ):
                        return False

                    patterns = [
                        "luci - openwrt",
                        "<title>luci",
                        "ubus rpc",
                        "cgi-bin/luci",
                    ]
                    if any(p in text.lower() for p in patterns) or "uhttpd" in server:
                        return True

        # Check static asset fallback
        asset_url = f"http://{host}/luci-static/resources/luci.js"
        with contextlib.suppress(Exception):
            async with asyncio.timeout(2):
                async with session.get(asset_url) as resp:
                    if resp.status == 200:
                        return True

        return False

    async def _probe_ubus(self, host: str, session: aiohttp.ClientSession) -> bool:
        """Check for UBus presence."""
        ubus_url = f"http://{host}/ubus"
        with contextlib.suppress(Exception):
            async with asyncio.timeout(2):
                async with session.post(ubus_url, json={}) as resp:
                    content_type = resp.headers.get("Content-Type", "").lower()
                    server = resp.headers.get("Server", "").lower()

                    if "valetudo" in server or "valetudo" in resp.headers:
                        return False

                    if resp.status in (200, 405):
                        if "json" not in content_type and resp.status == 405:
                            return False

                        if resp.status == 200:
                            text = (await resp.text()).lower()
                            if any(s in text for s in ["valetudo", "vacuum", "dreame"]):
                                return False
                            try:
                                if not isinstance(await resp.json(), dict):
                                    return False
                            except Exception:
                                return False
                        return True

                    # Try specific JSON-RPC check
                    with contextlib.suppress(Exception):
                        if (
                            isinstance(await resp.json(), dict)
                            and "jsonrpc" in await resp.json()
                        ):
                            return True
        return False

    async def _async_discover_router(self) -> str | None:
        """Try to discover an OpenWrt router on common gateway IPs."""
        potential_hosts = ["192.168.1.1", "192.168.0.1", "10.0.0.1"]

        # Try to guess gateway from local IP
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.5)
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
            s.close()

            parts = local_ip.split(".")
            if len(parts) == 4:
                # Common gateway is .1 or .254
                for suffix in ["1", "254"]:
                    gateway_ip = ".".join([*parts[:-1], suffix])
                    if gateway_ip not in potential_hosts:
                        potential_hosts.append(gateway_ip)
        except Exception as err:
            _LOGGER.debug("Could not determine local gateway for discovery: %s", err)

        # Probing in parallel for speed
        tasks = [self._async_probe_openwrt(host) for host in potential_hosts]
        results = await asyncio.gather(*tasks)

        for host, found in zip(potential_hosts, results, strict=False):
            if found:
                return host

        return None

    async def async_step_provision_user(
        self,
        user_input: dict[str, Any] | None = None,
        errors: dict[str, str] | None = None,
    ) -> ConfigFlowResult:
        """Step to ask if user wants to provision a dedicated user."""
        _LOGGER.info(
            "Entering async_step_provision_user: input=%s, errors=%s",
            user_input,
            errors,
        )
        if user_input is not None:
            mode = user_input.get("mode")
            if mode in {"create", "reset"}:
                return await self.async_step_do_provision()
            if mode == "reuse":
                return await self.async_step_reuse_user()
            return await self.async_step_permissions()

        options = ["create", "skip"]
        default_mode = "create"
        user_exists_info = ""

        if self._homeassistant_user_exists:
            options = ["reuse", "reset", "skip"]
            default_mode = "reuse"
            user_exists_info = "An existing **homeassistant** user was detected on your router. You can either reuse it or reset it with a new password and freshly generated permissions."

        return self.async_show_form(
            step_id="provision_user",
            data_schema=vol.Schema(
                {
                    vol.Required("mode", default=default_mode): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=options,
                            translation_key="provision_mode",
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        ),
                    ),
                },
            ),
            errors=errors or {},
            description_placeholders={
                "security_link": "https://github.com/FaserF/ha-openwrt/blob/main/SECURITY.md",
                "user_exists_info": user_exists_info,
            },
        )

    async def async_step_reuse_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Step to ask for existing user password."""
        errors: dict[str, str] = {}
        if user_input is not None:
            test_data = self._data.copy()
            test_data[CONF_USERNAME] = "homeassistant"
            test_data[CONF_PASSWORD] = user_input[CONF_PASSWORD]

            error = await self._test_connection(test_data)
            if not error:
                self._data.update(test_data)
                return await self.async_step_permissions()
            errors["base"] = error

        return self.async_show_form(
            step_id="reuse_user",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            errors=errors,
        )

    def _analyze_provision_error(self, error_text: str) -> str:
        """Analyze the provisioning error and return a user-friendly tip."""
        if "ANALYSIS: PASSWD_WRITE_FAILED" in error_text:
            return "The router's /etc/passwd file is read-only or the disk is full. This is common on some specialized firmwares or if the Overlay partition is not mounted."
        if (
            "ANALYSIS: CHPASSWD_FAILED" in error_text
            or "ANALYSIS: PASSWD_BINARY_FAILED" in error_text
        ):
            return "The password update failed. This can happen if the router uses a specialized authentication backend or if the password contains unsupported characters."
        if "ANALYSIS: ACL_WRITE_FAILED" in error_text:
            return "Could not create the ACL file at /usr/share/rpcd/acl.d/. This usually means the partition is read-only (standard for SquashFS builds without an Overlay)."
        if "ANALYSIS: UCI_COMMIT_FAILED" in error_text:
            return "Failed to save configuration changes via UCI. Check if another process is locking the UCI database."
        if not error_text or error_text.strip() == "":
            return "The router returned an empty response. This often means the 'file.exec' command was blocked by the router's security policy (common on Xiaomi/OEM firmwares)."
        if "permission denied" in error_text.lower():
            return "Permission denied. Ensure you are connecting as 'root' or that your current user has 'file.exec' permissions."

        return "An unexpected error occurred during execution. See the logs below for details."

    async def async_step_do_provision(self) -> ConfigFlowResult:
        """Perform the actual provisioning."""
        self._generated_password = secrets.token_hex(16)
        client = create_client(self._data)
        success = False
        self._provision_error = None

        try:
            async with asyncio.timeout(45):
                await client.connect()
                # The provisioning script will restart services in background
                # it's possible the connection drops exactly when/after sending SUCCESS
                success, self._provision_error = await client.provision_user(
                    "homeassistant",
                    self._generated_password,
                )

                # Fallback to SSH for root if ubus provisioning fails
                # (e.g. Xiaomi/OEM routers block file.exec via ubus)
                if (
                    not success
                    and self._data.get(CONF_USERNAME) == "root"
                    and self._data.get(CONF_CONNECTION_TYPE) != CONNECTION_TYPE_SSH
                ):
                    _LOGGER.info(
                        "Provisioning via %s failed for root, trying SSH fallback on port 22",
                        self._data.get(CONF_CONNECTION_TYPE),
                    )
                    from .api.ssh import SshClient

                    # Always use port 22 for SSH fallback, independent of the ubus port
                    ssh_client = SshClient(
                        host=self._data[CONF_HOST],
                        username="root",
                        password=self._data.get(CONF_PASSWORD, ""),
                        port=DEFAULT_PORT_SSH,
                    )
                    try:
                        if await ssh_client.connect():
                            success, ssh_error = await ssh_client.provision_user(
                                "homeassistant",
                                self._generated_password,
                            )
                            if success:
                                self._provision_error = None
                                _LOGGER.info(
                                    "SSH fallback provisioning succeeded for %s",
                                    self._data.get(CONF_HOST),
                                )
                            else:
                                self._provision_error = (
                                    f"ubus file.exec is blocked on this router (common on Xiaomi/OEM firmwares). "
                                    f"SSH fallback also failed: {ssh_error}. "
                                    "Please reconfigure using SSH as the connection type."
                                )
                            await ssh_client.disconnect()
                        else:
                            self._provision_error = (
                                "ubus file.exec is blocked on this router (common on Xiaomi/OEM firmwares) "
                                "and SSH on port 22 is not reachable either. "
                                "Please enable SSH on the router (System → Administration → SSH Access) "
                                "and reconfigure using SSH as the connection type."
                            )
                    except Exception as err:
                        _LOGGER.debug("SSH fallback failed: %s", err)
                        self._provision_error = (
                            f"ubus file.exec is blocked on this router (common on Xiaomi/OEM firmwares). "
                            f"SSH fallback error: {err}. "
                            "Please enable SSH (System → Administration → SSH Access) "
                            "and reconfigure using SSH as the connection type."
                        )

                await client.disconnect()
        except TimeoutError:
            _LOGGER.warning(
                "Provisioning timed out for %s. It might have succeeded if services are restarting.",
                self._data.get(CONF_HOST),
            )
            # We don't mark as success here, but if the script worked,
            # the next step (testing new user) might still work
            self._provision_error = "Timeout during provisioning. The router might be slow or restarting services."
        except Exception as err:
            err_msg = str(err).lower()
            # If we get a connection drop, it's highly likely service restarts triggered it
            if any(
                m in err_msg
                for m in ["connection reset", "broken pipe", "closed", "eof"]
            ):
                _LOGGER.info(
                    "Connection dropped during provisioning for %s - this is expected during service restarts.",
                    self._data.get(CONF_HOST),
                )
                # We assume success if the command was at least sent and no explicit error returned
                # The next step 'display_new_user' does a thorough re-connect test
                success = True
            else:
                _LOGGER.exception(
                    "Provisioning failed for %s: %s",
                    self._data.get(CONF_HOST),
                    err,
                )
                self._provision_error = str(err)

        if success:
            # Wait for rpcd to fully restart and apply ACLs
            # We already changed the script to background restart with sleep,
            # but we wait here too for a good first attempt in the next step
            await asyncio.sleep(5)
            return await self.async_step_display_new_user()

        tip = self._analyze_provision_error(self._provision_error or "")

        return self.async_show_form(
            step_id="provision_failed",
            errors={"base": "provision_failed"},
            description_placeholders={
                "error": self._provision_error or "Unknown error",
                "tip": tip,
            },
        )

    async def async_step_provision_failed(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Handle provisioning failure (only skip available)."""
        return await self.async_step_permissions()

    async def async_step_display_new_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Display the new user credentials and ask to use them."""
        if user_input is not None:
            if user_input.get("use_new_user"):
                self._data[CONF_USERNAME] = "homeassistant"
                self._data[CONF_PASSWORD] = self._generated_password

                # Wait for services to fully restart after provisioning
                # Slower devices need more time for rpcd to come back up
                # We wait 10s now initially as it's a critical phase
                _LOGGER.info(
                    "Provisioning finished. Waiting 10s for router services to restart...",
                )
                await asyncio.sleep(10)

                # Re-check permissions with new user with retries
                new_user_success = False
                for attempt in range(10):
                    _LOGGER.info(
                        "Testing connection with new user 'homeassistant' (attempt %s/10)",
                        attempt + 1,
                    )
                    # Use a fresh connection test to avoid session leakage
                    error = await self._test_connection(self._data)
                    if not error:
                        _LOGGER.info(
                            "Connection with new user successful on attempt %s",
                            attempt + 1,
                        )
                        new_user_success = True
                        break

                    _LOGGER.warning(
                        "Auth attempt %s failed for %s: %s. Router might still be restarting services. Waiting 5s...",
                        attempt + 1,
                        self._data.get(CONF_HOST),
                        error,
                    )
                    await asyncio.sleep(5)

                if not new_user_success:
                    _LOGGER.error(
                        "Failed to connect with new user 'homeassistant' after 10 attempts at %s. "
                        "Config might have applied but services didn't pick it up or user creation failed. "
                        "Check your router logs for 'ha-openwrt' tags. Last error: %s",
                        self._data.get(CONF_HOST),
                        error,
                    )
                    return await self.async_step_provision_user(
                        errors={"base": error or "invalid_auth"},
                    )
            return await self.async_step_permissions()

        return self.async_show_form(
            step_id="display_new_user",
            data_schema=vol.Schema({vol.Required("use_new_user", default=True): bool}),
            description_placeholders={
                "username": "homeassistant",
                "password": self._generated_password or "",
            },
        )

    async def async_step_permissions(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Show permissions summary."""
        if user_input is not None:
            if getattr(self, "_packages", None) is not None:
                return await self.async_step_packages()
            return await self._create_entry()

        if self._permissions is None:
            if getattr(self, "_packages", None) is not None:
                return await self.async_step_packages()
            return await self._create_entry()

        if self._ubus_restricted:
            return await self.async_step_ubus_restricted()

        table = _generate_permission_table(self._permissions)

        step_id = "permissions"
        if self._data.get(CONF_CONNECTION_TYPE) == CONNECTION_TYPE_UBUS:
            step_id = "permissions_ubus"

        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema({}),
            description_placeholders={
                "permissions_table": table,
                "username": self._data.get(CONF_USERNAME, ""),
            },
        )

    async def async_step_permissions_ubus(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Show permissions summary (ubus variant)."""
        return await self.async_step_permissions(user_input)

    async def async_step_ubus_restricted(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Inform the user about restricted Ubus access."""
        if user_input is not None:
            if getattr(self, "_packages", None) is not None:
                return await self.async_step_packages()
            return await self._create_entry()

        return self.async_show_form(
            step_id="ubus_restricted",
            data_schema=vol.Schema({}),
            description_placeholders={
                "host": self._data.get(CONF_HOST, ""),
                "model": self._device_info.get("model", "Router"),
            },
        )

    async def async_step_packages(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Show packages summary."""
        if user_input is not None:
            return await self._create_entry()

        if getattr(self, "_packages", None) is None:
            return await self._create_entry()

        # Get translations for package features
        translations = await translation.async_get_translations(
            self.hass,
            self.hass.config.language,
            "entity",
            [DOMAIN],
        )
        prefix = f"component.{DOMAIN}.entity.sensor.package_feature.state."
        feature_translations = {
            key.replace(prefix, ""): value
            for key, value in translations.items()
            if key.startswith(prefix)
        }

        table = _generate_package_table(
            self._packages,
            self._data.get(CONF_CONNECTION_TYPE),
            translations=feature_translations,
        )

        return self.async_show_form(
            step_id="packages",
            data_schema=vol.Schema({}),
            description_placeholders={"packages_table": table},
        )

    async def _async_set_unique_id_and_check(self) -> None:
        """Set unique ID and check for existing config flow or entry."""
        mac = self._device_info.get("mac_address")
        host = self._data[CONF_HOST]
        unique_id = dr.format_mac(mac) if mac else host

        # Abort other flows with the same unique ID to allow this one to proceed
        # This prevents "already_in_progress" if discovery found the router first
        try:
            in_progress = self.hass.config_entries.flow.async_progress()
            import inspect as _inspect

            if _inspect.iscoroutine(in_progress):
                in_progress = []
        except Exception:  # noqa: BLE001
            in_progress = []
        for flow in in_progress:
            if (
                flow["flow_id"] != self.flow_id
                and flow.get("handler") == DOMAIN
                and flow.get("context", {}).get("unique_id") == unique_id
            ):
                _LOGGER.info(
                    "Aborting existing OpenWrt flow %s to allow manual setup for %s",
                    flow["flow_id"],
                    unique_id,
                )
                self.hass.config_entries.flow.async_abort(flow["flow_id"])

        # Set unique ID and abort if already configured
        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured()

    async def _create_entry(self) -> ConfigFlowResult:
        """Create the config entry."""
        # Unique ID is already set and checked in credential steps
        host = self._data[CONF_HOST]
        hostname = self._device_info.get("hostname", host)

        # Split data and options
        # Options are toggles that the user might want to change later via Configure
        data = self._data.copy()
        options = {}

        for key in [
            CONF_TRACK_DEVICES,
            CONF_TRACK_WIRED,
            CONF_UPDATE_INTERVAL,
            CONF_CONSIDER_HOME,
            CONF_DHCP_SOFTWARE,
            CONF_CUSTOM_FIRMWARE_REPO,
            CONF_ASU_URL,
        ]:
            if key in data:
                options[key] = data.pop(key)

        title = hostname if hostname else host
        return self.async_create_entry(title=title, data=data, options=options)


class OpenWrtOptionsFlow(OptionsFlow):
    """Handle options flow for OpenWrt."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        self._config_entry = config_entry
        self._options: dict[str, Any] = {}
        self._permissions: Any = None
        self._packages: Any = None
        self._ubus_restricted: bool = False

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            self._options = user_input
            return await self.async_step_permissions()

        current = self._config_entry.options

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_UPDATE_INTERVAL,
                    default=current.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL),
                ): vol.All(vol.Coerce(int), vol.Range(min=10, max=3600)),
                vol.Optional(
                    CONF_TRACK_DEVICES,
                    default=current.get(CONF_TRACK_DEVICES, DEFAULT_TRACK_DEVICES),
                ): bool,
                vol.Optional(
                    CONF_TRACK_WIRED,
                    default=current.get(CONF_TRACK_WIRED, DEFAULT_TRACK_WIRED),
                ): bool,
                vol.Optional(
                    CONF_CONSIDER_HOME,
                    default=current.get(CONF_CONSIDER_HOME, DEFAULT_CONSIDER_HOME),
                ): vol.All(vol.Coerce(int), vol.Range(min=0, max=3600)),
                vol.Optional(
                    CONF_AUTO_BACKUP,
                    default=current.get(CONF_AUTO_BACKUP, True),
                ): bool,
                vol.Optional(
                    CONF_CUSTOM_FIRMWARE_REPO,
                    default=current.get(CONF_CUSTOM_FIRMWARE_REPO, ""),
                ): str,
                vol.Optional(
                    CONF_ASU_URL,
                    default=current.get(CONF_ASU_URL, "https://sysupgrade.openwrt.org"),
                ): str,
                vol.Optional(
                    CONF_DHCP_SOFTWARE,
                    default=current.get(CONF_DHCP_SOFTWARE, "auto"),
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=["auto", "dnsmasq", "odhcpd", "none"],
                        translation_key="dhcp_software",
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    ),
                ),
            },
        )

        return self.async_show_form(step_id="init", data_schema=schema)

    async def async_step_permissions(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Show permissions summary."""
        if user_input is not None:
            if self._packages is not None:
                return await self.async_step_packages()
            return self.async_create_entry(title="", data=self._options)

        client = create_client({**self._config_entry.data, **self._options})
        try:
            async with asyncio.timeout(15):
                await client.connect()
                try:
                    self._permissions = await client.check_permissions()
                except Exception:
                    self._permissions = None
                try:
                    self._packages = await client.check_packages()
                except Exception:
                    self._packages = None

                # Specific check for restricted Ubus (like Xiaomi firmwares)
                if (
                    self._config_entry.data.get(CONF_CONNECTION_TYPE)
                    == CONNECTION_TYPE_UBUS
                ):
                    try:
                        radios = await client.get_wireless_interfaces()
                        services = await client.get_services()
                        if not radios and not services:
                            self._ubus_restricted = True
                    except Exception:
                        self._ubus_restricted = True

                await client.disconnect()
        except Exception:
            self._permissions = None
            self._packages = None

        if self._permissions is None:
            if self._packages is not None:
                return await self.async_step_packages()
            return self.async_create_entry(title="", data=self._options)

        if self._ubus_restricted:
            return await self.async_step_ubus_restricted()

        table = _generate_permission_table(self._permissions)

        step_id = "permissions"
        if self._config_entry.data.get(CONF_CONNECTION_TYPE) == CONNECTION_TYPE_UBUS:
            step_id = "permissions_ubus"

        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema({}),
            description_placeholders={
                "permissions_table": table,
                "username": self._config_entry.data.get(CONF_USERNAME, ""),
            },
        )

    async def async_step_permissions_ubus(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Show permissions summary (ubus variant)."""
        return await self.async_step_permissions(user_input)

    async def async_step_ubus_restricted(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Inform the user about restricted Ubus access."""
        if user_input is not None:
            if self._packages is not None:
                return await self.async_step_packages()
            return self.async_create_entry(title="", data=self._options)

        # Try to get model from direct client or use existing device registry if available
        model = self._config_entry.data.get(CONF_HOST, "Router")

        return self.async_show_form(
            step_id="ubus_restricted",
            data_schema=vol.Schema({}),
            description_placeholders={
                "host": self._config_entry.data.get(CONF_HOST, ""),
                "model": model,
            },
        )

    async def async_step_packages(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Show packages summary."""
        if user_input is not None:
            return self.async_create_entry(title="", data=self._options)

        if self._packages is None:
            return self.async_create_entry(title="", data=self._options)

        # Get translations for package features
        translations = await translation.async_get_translations(
            self.hass,
            self.hass.config.language,
            "entity",
            [DOMAIN],
        )
        prefix = f"component.{DOMAIN}.entity.sensor.package_feature.state."
        feature_translations = {
            key.replace(prefix, ""): value
            for key, value in translations.items()
            if key.startswith(prefix)
        }

        table = _generate_package_table(
            self._packages,
            self._config_entry.data.get(CONF_CONNECTION_TYPE),
            translations=feature_translations,
        )

        return self.async_show_form(
            step_id="packages",
            data_schema=vol.Schema({}),
            description_placeholders={"packages_table": table},
        )
