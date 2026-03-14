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
from typing import Any
from urllib.parse import urlparse

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers import selector
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

    VERSION = 1

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

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OpenWrtOptionsFlow:
        """Get the options flow."""
        return OpenWrtOptionsFlow(config_entry)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step - select connection type."""
        if user_input is not None:
            self._data.update(user_input)
            host = user_input[CONF_HOST]
            connection_type = user_input[CONF_CONNECTION_TYPE]

            if not await self._async_check_reachable(host, connection_type):
                return self.async_show_form(
                    step_id="user",
                    data_schema=self._async_user_schema(),
                    errors={CONF_HOST: "cannot_connect"},
                    description_placeholders={
                        "docs_url": "https://github.com/FaserF/ha-openwrt",
                    },
                )

            if connection_type == CONNECTION_TYPE_SSH:
                return await self.async_step_ssh()
            return await self.async_step_credentials()

        return self.async_show_form(
            step_id="user",
            data_schema=self._async_user_schema(),
            description_placeholders={
                "docs_url": "https://github.com/FaserF/ha-openwrt",
            },
        )

    def _async_user_schema(self) -> vol.Schema:
        """Return the schema for the user step."""
        return vol.Schema(
            {
                vol.Required(CONF_HOST, default="192.168.1.1"): str,
                vol.Required(
                    CONF_CONNECTION_TYPE, default=CONNECTION_TYPE_LUCI_RPC
                ): vol.In(CONNECTION_TYPE_MAP),
            }
        )

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
            _LOGGER.debug("Checking reachability of %s:%s", host, port)
            try:
                async with asyncio.timeout(2):
                    reader, writer = await asyncio.open_connection(host, port)
                    writer.close()
                    await writer.wait_closed()
                    return True
            except (TimeoutError, socket.gaierror, ConnectionRefusedError, OSError):
                continue

        return False

    async def async_step_ssdp(
        self, discovery_info: SsdpServiceInfo
    ) -> ConfigFlowResult:
        """Handle SSDP auto-discovery."""
        ssdp_location = discovery_info.ssdp_location or ""
        parsed = urlparse(ssdp_location)
        host = parsed.hostname or ""

        if not host:
            return self.async_abort(reason="no_host")

        await self.async_set_unique_id(host)
        self._abort_if_unique_id_configured()

        upnp = discovery_info.upnp or {}
        friendly_name = upnp.get("friendlyName", "")
        server = discovery_info.ssdp_headers.get("SERVER", "")
        manufacturer = upnp.get("manufacturer", "")
        model_name = upnp.get("modelName", "")

        openwrt_indicators = ["openwrt", "lede", "miniupnpd", "librecmc"]
        combined = f"{friendly_name} {server} {manufacturer} {model_name}".lower()
        if not any(indicator in combined for indicator in openwrt_indicators):
            _LOGGER.debug(
                "SSDP discovery for %s skipped: no OpenWrt identifiers in %s",
                host,
                combined,
            )
            return self.async_abort(reason="not_openwrt")

        self._discovered_host = host
        self._discovered_name = friendly_name or f"OpenWrt ({host})"

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
        await self.async_set_unique_id(host)
        self._abort_if_unique_id_configured()

        hostname = discovery_info.hostname.lower()
        mac = (discovery_info.macaddress or "").replace(":", "").upper()

        # MAC prefixes: Xiaomi (D4BC52, E848B8, B0B98A), GL-iNet (000C43), and generic OpenWrt strings
        openwrt_indicators = ["openwrt", "lede", "librecmc"]
        mac_prefixes = ["D4BC52", "E848B8", "000C43", "B0B98A"]

        is_openwrt = any(
            indicator in hostname for indicator in openwrt_indicators
        ) or any(mac.startswith(prefix) for prefix in mac_prefixes)

        if not is_openwrt:
            return self.async_abort(reason="not_openwrt")

        self._discovered_host = host
        self._discovered_name = f"OpenWrt ({discovery_info.hostname})"

        self.context["title_placeholders"] = {
            "name": self._discovered_name,
            "host": host,
        }

        return await self.async_step_ssdp_confirm()

    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        """Handle Zeroconf auto-discovery."""
        host = discovery_info.host
        await self.async_set_unique_id(host)
        self._abort_if_unique_id_configured()

        name = discovery_info.name.lower()
        hostname = (discovery_info.hostname or "").lower()

        # If the service type is _luci._tcp, it's almost certainly OpenWrt
        if "_luci._tcp" in discovery_info.type:
            pass
        else:
            openwrt_indicators = ["openwrt", "lede", "librecmc", "luci"]
            if not any(
                indicator in name or indicator in hostname
                for indicator in openwrt_indicators
            ):
                return self.async_abort(reason="not_openwrt")

        self._discovered_host = host
        self._discovered_name = discovery_info.hostname or f"OpenWrt ({host})"

        self.context["title_placeholders"] = {
            "name": self._discovered_name,
            "host": host,
        }

        return await self.async_step_ssdp_confirm()

    async def async_step_ssdp_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm the SSDP discovered device."""
        if user_input is not None:
            self._data[CONF_HOST] = self._discovered_host
            self._data[CONF_CONNECTION_TYPE] = CONNECTION_TYPE_UBUS
            self._data.update(user_input)

            if not user_input.get(CONF_PORT):
                if self._data.get(CONF_USE_SSL, False):
                    self._data[CONF_PORT] = DEFAULT_PORT_UBUS_SSL
                else:
                    self._data[CONF_PORT] = DEFAULT_PORT_UBUS

            error = await self._test_connection(self._data)
            if error:
                return self.async_show_form(
                    step_id="ssdp_confirm",
                    data_schema=vol.Schema(
                        {
                            vol.Required(CONF_USERNAME, default=DEFAULT_USERNAME): str,
                            vol.Required(CONF_PASSWORD): str,
                            vol.Required(CONF_USE_SSL, default=DEFAULT_USE_SSL): bool,
                            vol.Optional(CONF_PORT): int,
                        }
                    ),
                    errors={"base": error},
                    description_placeholders={
                        "name": self._discovered_name or "",
                        "host": self._discovered_host or "",
                    },
                )

            if self._data.get(CONF_USERNAME) == "root":
                return await self.async_step_provision_user()
            return await self.async_step_permissions()

        return self.async_show_form(
            step_id="ssdp_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME, default=DEFAULT_USERNAME): str,
                    vol.Required(CONF_PASSWORD): str,
                    vol.Required(CONF_USE_SSL, default=DEFAULT_USE_SSL): bool,
                    vol.Optional(CONF_PORT): int,
                }
            ),
            description_placeholders={
                "name": self._discovered_name or "",
                "host": self._discovered_host or "",
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
            vol.Optional(CONF_PORT): int,
        }

        if is_ubus:
            schema_dict[vol.Optional(CONF_UBUS_PATH, default=DEFAULT_UBUS_PATH)] = str

        return self.async_show_form(
            step_id="credentials",
            data_schema=vol.Schema(schema_dict),
            errors=errors,
            description_placeholders={
                "host": self._data.get(CONF_HOST, ""),
                "connection_type": CONNECTION_TYPE_MAP.get(
                    connection_type, connection_type
                ),
            },
        )

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

        schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME, default=DEFAULT_USERNAME): str,
                vol.Optional(CONF_PASSWORD): str,
                vol.Optional(CONF_SSH_KEY): str,
                vol.Optional(CONF_DHCP_SOFTWARE, default="auto"): vol.In(
                    ["auto", "dnsmasq", "odhcpd", "none"]
                ),
                vol.Optional(CONF_PORT, default=DEFAULT_PORT_SSH): int,
            }
        )

        return self.async_show_form(
            step_id="ssh",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "host": self._data.get(CONF_HOST, ""),
            },
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
            _LOGGER.exception("Unexpected error during connection test for %s: %s", data.get(CONF_USERNAME), err)
            return "unknown"

    async def async_step_provision_user(
        self, user_input: dict[str, Any] | None = None, errors: dict[str, str] | None = None
    ) -> ConfigFlowResult:
        """Step to ask if user wants to provision a dedicated user."""
        _LOGGER.info("Entering async_step_provision_user: input=%s, errors=%s", user_input, errors)
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
                success = await client.provision_user("homeassistant", self._generated_password)
                if not success:
                    self._provision_error = "Provisioning script returned failure. Check router logs (logread)."
                await client.disconnect()
        except TimeoutError:
            _LOGGER.warning("Provisioning timed out for %s. It might have succeeded if services are restarting.", self._data.get(CONF_HOST))
            # We don't mark as success here, but if the script worked,
            # the next step (testing new user) might still work
            self._provision_error = "Timeout during provisioning. The router might be slow or restarting services."
        except Exception as err:
            err_msg = str(err).lower()
            # If we get a connection drop, it's highly likely service restarts triggered it
            if any(m in err_msg for m in ["connection reset", "broken pipe", "closed", "eof"]):
                _LOGGER.info("Connection dropped during provisioning for %s - this is expected during service restarts.", self._data.get(CONF_HOST))
                # We assume success if the command was at least sent and no explicit error returned
                # The next step 'display_new_user' does a thorough re-connect test
                success = True
            else:
                _LOGGER.error("Provisioning failed for %s: %s", self._data.get(CONF_HOST), err)
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
            description_placeholders={"error": self._provision_error or "Unknown error"},
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
                _LOGGER.info("Provisioning finished. Waiting 10s for router services to restart...")
                await asyncio.sleep(10)

                # Re-check permissions with new user with retries
                new_user_success = False
                for attempt in range(10):
                    _LOGGER.info(
                        "Testing connection with new user 'homeassistant' (attempt %s/10)",
                        attempt + 1
                    )
                    # Use a fresh connection test to avoid session leakage
                    error = await self._test_connection(self._data)
                    if not error:
                        _LOGGER.info("Connection with new user successful on attempt %s", attempt + 1)
                        new_user_success = True
                        break

                    _LOGGER.warning(
                        "Auth attempt %s failed for %s: %s. Router might still be restarting services. Waiting 5s...",
                        attempt + 1, self._data.get(CONF_HOST), error
                    )
                    await asyncio.sleep(5)

                if not new_user_success:
                    _LOGGER.error(
                        "Failed to connect with new user 'homeassistant' after 10 attempts at %s. "
                        "Config might have applied but services didn't pick it up or user creation failed. "
                        "Check your router logs for 'ha-openwrt' tags. Last error: %s",
                        self._data.get(CONF_HOST),
                        error
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
