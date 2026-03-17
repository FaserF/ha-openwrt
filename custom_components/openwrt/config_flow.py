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
from homeassistant.helpers import selector
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
    CONNECTION_TYPE_SSH: "SSH",
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

    table = (
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
    return table


def _generate_package_table(packages: Any) -> str:
    """Generate markdown table for installed packages."""

    def to_icon(val: bool | None) -> str:
        if val is None:
            return "❓"
        return "✅" if val else "❌"

    def get_missing(val: bool | None, name: str) -> str:
        if val is None:
            return "Check failed"
        return "-" if val else name

    table = (
        "| Package | Installed | Missing Features |\n"
        "|---------|-----------|------------------|\n"
        f"| **sqm-scripts** | {to_icon(packages.sqm_scripts)} | {get_missing(packages.sqm_scripts, 'SQM QoS Settings')} |\n"
        f"| **mwan3** | {to_icon(packages.mwan3)} | {get_missing(packages.mwan3, 'MWAN3 Sensors')} |\n"
        f"| **iwinfo** | {to_icon(packages.iwinfo)} | {get_missing(packages.iwinfo, 'Enhanced WiFi Info')} |\n"
        f"| **etherwake** | {to_icon(packages.etherwake)} | {get_missing(packages.etherwake, 'Wake on LAN')} |\n"
        f"| **wireguard-tools** | {to_icon(packages.wireguard)} | {get_missing(packages.wireguard, 'WireGuard Sensors')} |\n"
        f"| **openvpn** | {to_icon(packages.openvpn)} | {get_missing(packages.openvpn, 'OpenVPN Sensors')} |"
    )
    return table


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

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OpenWrtOptionsFlow:
        """Get the options flow."""
        return OpenWrtOptionsFlow(config_entry)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step - Welcome Screen."""
        if user_input is not None:
            return await self.async_step_discovery()

        return self.async_show_form(
            step_id="user", description_placeholders={"docs_url": DOCS_URL}
        )

    async def async_step_discovery(
        self, user_input: dict[str, Any] | None = None
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
                router = self._discovered_routers[0]
                self._data[CONF_HOST] = router["host"]
                self._data[CONF_CONNECTION_TYPE] = CONNECTION_TYPE_UBUS
                self._discovered_name = router.get("hostname")
                return await self.async_step_credentials()

            return await self.async_step_select_device()

        # Perform scanning
        potential_hosts: set[str] = {"192.168.1.1", "192.168.0.1", "10.0.0.1"}

        # Try to get dynamic gateways from Home Assistant
        try:
            # We try to get gateway IPs from network config
            # This is more robust than guessing
            from homeassistant.components import network

            adapters = await network.async_get_adapters(self.hass)
            for adapter in adapters:
                for ipv4 in adapter.get("ipv4", []):
                    # Guess gateway by .1 and .254 in same subnet (simplified)
                    local_ip = ipv4.get("address")
                    if local_ip:
                        parts = local_ip.split(".")
                        if len(parts) == 4:
                            potential_hosts.add(".".join(parts[:-1] + ["1"]))
                            potential_hosts.add(".".join(parts[:-1] + ["254"]))
        except Exception:
            pass

        tasks = [self._async_probe_router(host) for host in potential_hosts]
        results = await asyncio.gather(*tasks)

        for router_info in results:
            if router_info:
                # Avoid duplicates
                if not any(
                    r["host"] == router_info["host"] for r in self._discovered_routers
                ):
                    self._discovered_routers.append(router_info)

        if not self._discovered_routers:
            return await self.async_step_manual_entry()

        return await self.async_step_discovery(user_input={})

    async def async_step_manual_entry(
        self, user_input: dict[str, Any] | None = None
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
                        CONF_CONNECTION_TYPE, default=CONNECTION_TYPE_LUCI_RPC
                    ): vol.In(CONNECTION_TYPE_MAP),
                }
            ),
        )

    async def async_step_select_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle selecting a router when multiple are found."""
        if user_input is not None:
            host = user_input["device"]
            router = next(r for r in self._discovered_routers if r["host"] == host)
            self._data[CONF_HOST] = router["host"]
            self._discovered_name = router.get("hostname")
            return await self.async_step_credentials()

        device_options = {
            r[
                "host"
            ]: f"{r.get('hostname', 'OpenWrt')} ({r['host']}) - {r['method'].upper()} [Available: {', '.join(r['capabilities'])}]"
            for r in self._discovered_routers
        }

        return self.async_show_form(
            step_id="select_device",
            data_schema=vol.Schema(
                {
                    vol.Required("device"): vol.In(device_options),
                }
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
            "smartlife",
            "broadlink",
            "shelly",
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
                "Definitively excluded %s (%s) as a non-router device", host, hostname
            )
            return True

        return False

    async def _async_probe_router(
        self, host: str, hostname: str | None = None
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
        if not await self._async_check_reachable(host, CONNECTION_TYPE_UBUS):
            # If not ubus, might be only SSH
            if not await self._async_check_reachable(host, CONNECTION_TYPE_SSH):
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
                    writer.close()
                    await writer.wait_closed()
                    return True
            except TimeoutError, socket.gaierror, ConnectionRefusedError, OSError:
                continue
        return False

    async def async_step_ssdp(
        self, discovery_info: SsdpServiceInfo
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
            "modelName"
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

        return await self.async_step_ssdp_confirm()

    async def async_step_dhcp(
        self, discovery_info: DhcpServiceInfo
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
                }
            }
        )

        return await self.async_step_ssdp_confirm()

    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
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
                }
            }
        )

        return await self.async_step_ssdp_confirm()

    async def async_step_ssdp_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm the discovered device."""
        if user_input is not None:
            router = self._discovered_routers[-1]
            self._data[CONF_HOST] = router["host"]
            self._data[CONF_CONNECTION_TYPE] = user_input.get(
                CONF_CONNECTION_TYPE, router["method"]
            )
            return await self.async_step_credentials()

        router = self._discovered_routers[-1]
        capabilities = ", ".join(
            [CONNECTION_TYPE_MAP.get(c, c) for c in router.get("capabilities", [])]
        )

        schema = vol.Schema({})
        if len(router.get("capabilities", [])) > 1:
            schema = vol.Schema(
                {
                    vol.Required(
                        CONF_CONNECTION_TYPE, default=router["method"]
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=router["capabilities"],
                            translation_key="connection_type",
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            )

        return self.async_show_form(
            step_id="ssdp_confirm",
            data_schema=schema,
            description_placeholders={
                "name": self._discovered_name or "OpenWrt Router",
                "host": router["host"],
                "method": CONNECTION_TYPE_MAP.get(router["method"], router["method"]),
                "capabilities": capabilities,
            },
        )

    async def async_step_credentials(
        self, user_input: dict[str, Any] | None = None
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
                    connection_type, connection_type
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
                )
            ),
            vol.Optional(CONF_TRACK_DEVICES, default=True): bool,
            vol.Optional(CONF_PORT): int,
        }

        if is_ubus:
            schema_dict[vol.Optional(CONF_UBUS_PATH, default=DEFAULT_UBUS_PATH)] = str

        return vol.Schema(schema_dict)

    async def async_step_ssh(
        self, user_input: dict[str, Any] | None = None
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
                if self._data.get(CONF_USERNAME) == "root":
                    return await self.async_step_provision_user()
                return await self.async_step_permissions()

        return self.async_show_form(
            step_id="ssh",
            data_schema=self._async_ssh_schema(),
            errors=errors,
            description_placeholders={
                "host": self._data.get(CONF_HOST, ""),
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
                    ["auto", "dnsmasq", "odhcpd", "none"]
                ),
                vol.Optional(CONF_TRACK_DEVICES, default=True): bool,
                vol.Optional(CONF_PORT, default=DEFAULT_PORT_SSH): int,
            }
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Handle re-authentication."""
        self._data = dict(entry_data)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
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
                    self.context["entry_id"]
                )
                if entry:
                    self.hass.config_entries.async_update_entry(
                        entry, data={**entry.data, **user_input}
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
            }
        )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=schema,
            errors=errors,
        )

    async def _test_connection(self, data: dict[str, Any]) -> str | None:
        """Test connection to the device. Returns error key or None on success."""

        client = create_client(data)

        try:
            async with asyncio.timeout(15):
                await client.connect()
                self._homeassistant_user_exists = False
                if data.get(CONF_USERNAME) == "root":
                    try:
                        self._homeassistant_user_exists = await client.user_exists(
                            "homeassistant"
                        )
                    except Exception:  # noqa: BLE001
                        pass
                device_info = await client.get_device_info()
                self._device_info = {
                    "hostname": device_info.hostname,
                    "model": device_info.model,
                    "firmware_version": device_info.firmware_version,
                    "mac_address": device_info.mac_address,
                }
                try:
                    self._permissions = await client.check_permissions()
                except Exception as err:
                    _LOGGER.warning("Could not check permissions: %s", err)
                    self._permissions = None
                try:
                    self._packages = await client.check_packages()
                except Exception as err:
                    _LOGGER.warning("Could not check packages: %s", err)
                    self._packages = None
                await client.disconnect()
            return None
        except (UbusAuthError, LuciRpcAuthError, SshAuthError, SshKeyError) as err:
            _LOGGER.warning("Authentication failed during connection test: %s", err)
            return "invalid_auth"
        except (
            UbusTimeoutError,
            LuciRpcTimeoutError,
            SshTimeoutError,
            TimeoutError,
        ) as err:
            _LOGGER.warning("Timeout during connection test: %s", err)
            return "timeout"
        except (UbusConnectionError, LuciRpcConnectionError, SshConnectionError) as err:
            _LOGGER.warning("Connection failed during connection test: %s", err)
            return "cannot_connect"
        except (UbusSslError, LuciRpcSslError) as err:
            _LOGGER.warning("SSL error during connection test: %s", err)
            return "ssl_error"
        except (UbusPackageMissingError, LuciRpcPackageMissingError) as err:
            _LOGGER.warning("Package missing during connection test: %s", err)
            return "package_missing"
        except UbusPermissionError as err:
            _LOGGER.warning("Permission error during connection test: %s", err)
            return "permission_error"
        except (UbusError, LuciRpcError, SshError) as err:
            _LOGGER.warning("API error during connection test: %s", err)
            return "cannot_connect"
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception(
                "Unexpected error during connection test for %s: %s",
                data.get(CONF_USERNAME),
                err,
            )
            return "unknown"

    async def _async_probe_openwrt(
        self, host: str, hostname: str | None = None
    ) -> list[str]:
        """Probe a host to see if it responds like OpenWrt (LuCI/UBus)."""
        _LOGGER.debug("Probing %s (%s) for OpenWrt endpoints", host, hostname)

        # 0. Quick exclusion check
        if self._is_excluded(host, hostname):
            return []

        # Exclusion list for keyword searching in bodies
        exclusions = [
            "valetudo",
            "manual control",
            "dreame",
            "roborock",
            "vacuum",
            "cleaner",
            "mop",
        ]

        found_methods = []
        session = async_get_clientsession(self.hass)

        # 1. Try LuCI
        luci_url = f"http://{host}/cgi-bin/luci/"
        try:
            async with asyncio.timeout(2):
                async with session.get(luci_url, allow_redirects=True) as response:
                    server = response.headers.get("Server", "").lower()
                    if "valetudo" in server or "valetudo" in response.headers:
                        return []

                    response_text = await response.text()
                    if any(s in response_text.lower() for s in exclusions):
                        _LOGGER.debug(
                            "Excluded %s as it appears to be a vacuum/valetudo device",
                            host,
                        )
                        return []

                    if (
                        any(
                            s in response_text.lower()
                            for s in ["luci", "openwrt", "ubus"]
                        )
                        or "uhttpd" in response.headers.get("Server", "").lower()
                    ):
                        _LOGGER.info("Found OpenWrt via LuCI probe at %s", host)
                        found_methods.append(CONNECTION_TYPE_LUCI_RPC)
        except TimeoutError, aiohttp.ClientError:
            pass

        # 2. Try LuCI static asset (more specific to OpenWrt)
        if CONNECTION_TYPE_LUCI_RPC not in found_methods:
            asset_url = f"http://{host}/luci-static/resources/luci.js"
            try:
                async with asyncio.timeout(2):
                    async with session.get(asset_url) as response:
                        if response.status == 200:
                            _LOGGER.info(
                                "Found OpenWrt via LuCI asset probe at %s", host
                            )
                            found_methods.append(CONNECTION_TYPE_LUCI_RPC)
            except TimeoutError, aiohttp.ClientError:
                pass

        # Check exclusions again before UBus if we didn't find LuCI yet
        # (This handles devices where only UBus is exposed but it's a known non-router)

        # 3. Try UBus endpoint (default path)
        ubus_url = f"http://{host}/ubus"
        try:
            async with asyncio.timeout(2):
                # A direct POST to ubus with empty data should return 405 or a JSON error
                # but it proves the endpoint exists. A router's ubus usually returns
                # a specific JSON structure or at least application/json content type.
                async with session.post(ubus_url, json={}) as response:
                    content_type = response.headers.get("Content-Type", "").lower()
                    server = response.headers.get("Server", "").lower()

                    if "valetudo" in server or "valetudo" in response.headers:
                        _LOGGER.info("Excluded %s: Valetudo detected via headers", host)
                        return []

                    if response.status in (200, 405):
                        # Router check: UBus usually responds with application/json
                        if "json" not in content_type and response.status == 405:
                            _LOGGER.debug(
                                "Excluded %s: UBus probe returned 405 but not JSON",
                                host,
                            )
                            return list(set(found_methods))

                        if response.status == 200:
                            text = await response.text()
                            if any(s in text.lower() for s in exclusions):
                                _LOGGER.debug(
                                    "Excluded %s: non-router keywords found in UBus response",
                                    host,
                                )
                                return list(set(found_methods))

                            # A 200 OK from /ubus without any JSON structure is suspicious for a router
                            try:
                                data = await response.json()
                                if not isinstance(data, dict):
                                    return list(set(found_methods))
                            except Exception:
                                # If it's 200 and not JSON, it's probably not ubus
                                return list(set(found_methods))

                        _LOGGER.info("Found OpenWrt via UBus probe at %s", host)
                        found_methods.append(CONNECTION_TYPE_UBUS)
                    else:
                        try:
                            # If it's a JSON error, check for exclusion in the error message if any
                            data = await response.json()
                            if isinstance(data, dict) and "jsonrpc" in data:
                                found_methods.append(CONNECTION_TYPE_UBUS)
                        except Exception:
                            pass
        except TimeoutError, aiohttp.ClientError:
            pass

        return list(set(found_methods))

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
                    gateway_ip = ".".join(parts[:-1] + [suffix])
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
            if mode == "create" or mode == "reset":
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
                        )
                    ),
                }
            ),
            errors=errors or {},
            description_placeholders={
                "security_link": "https://github.com/FaserF/ha-openwrt/blob/main/SECURITY.md",
                "user_exists_info": user_exists_info,
            },
        )

    async def async_step_reuse_user(
        self, user_input: dict[str, Any] | None = None
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
                success = await client.provision_user(
                    "homeassistant", self._generated_password
                )
                if not success:
                    self._provision_error = "Provisioning script returned failure. Check router logs (logread)."
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
                _LOGGER.error(
                    "Provisioning failed for %s: %s", self._data.get(CONF_HOST), err
                )
                self._provision_error = str(err)

        if success:
            # Wait for rpcd to fully restart and apply ACLs
            # We already changed the script to background restart with sleep,
            # but we wait here too for a good first attempt in the next step
            await asyncio.sleep(5)
            return await self.async_step_display_new_user()

        return self.async_show_form(
            step_id="provision_failed",
            errors={"base": "provision_failed"},
            description_placeholders={
                "error": self._provision_error or "Unknown error"
            },
        )

    async def async_step_provision_failed(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle provisioning failure (only skip available)."""
        return await self.async_step_permissions()

    async def async_step_display_new_user(
        self, user_input: dict[str, Any] | None = None
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
                    "Provisioning finished. Waiting 10s for router services to restart..."
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
                        errors={"base": error or "invalid_auth"}
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
        self, user_input: dict[str, Any] | None = None
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
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show permissions summary (ubus variant)."""
        return await self.async_step_permissions(user_input)

    async def async_step_packages(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show packages summary."""
        if user_input is not None:
            return await self._create_entry()

        if getattr(self, "_packages", None) is None:
            return await self._create_entry()

        table = _generate_package_table(self._packages)

        return self.async_show_form(
            step_id="packages",
            data_schema=vol.Schema({}),
            description_placeholders={"packages_table": table},
        )

    async def _create_entry(self) -> ConfigFlowResult:
        """Create the config entry."""
        host = self._data[CONF_HOST]
        hostname = self._device_info.get("hostname", host)
        mac = self._device_info.get("mac_address")

        if mac:
            await self.async_set_unique_id(dr.format_mac(mac))
        else:
            await self.async_set_unique_id(host)

        self._abort_if_unique_id_configured()

        title = hostname if hostname else host
        return self.async_create_entry(title=title, data=self._data)


class OpenWrtOptionsFlow(OptionsFlow):
    """Handle options flow for OpenWrt."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        self._config_entry = config_entry
        self._options: dict[str, Any] = {}
        self._permissions: Any = None
        self._packages: Any = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
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
                    )
                ),
            }
        )

        return self.async_show_form(step_id="init", data_schema=schema)

    async def async_step_permissions(
        self, user_input: dict[str, Any] | None = None
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
                await client.disconnect()
        except Exception:
            self._permissions = None
            self._packages = None

        if self._permissions is None:
            if self._packages is not None:
                return await self.async_step_packages()
            return self.async_create_entry(title="", data=self._options)

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
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show permissions summary (ubus variant)."""
        return await self.async_step_permissions(user_input)

    async def async_step_packages(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show packages summary."""
        if user_input is not None:
            return self.async_create_entry(title="", data=self._options)

        if self._packages is None:
            return self.async_create_entry(title="", data=self._options)

        table = _generate_package_table(self._packages)

        return self.async_show_form(
            step_id="packages",
            data_schema=vol.Schema({}),
            description_placeholders={"packages_table": table},
        )
