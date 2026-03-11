"""Config flow for OpenWrt integration.

Supports three connection methods:
- ubus (HTTP/HTTPS JSON-RPC) - recommended
- LuCI RPC (via LuCI web interface)
- SSH (password or key-based authentication)

Supports adding multiple routers, device auto-discovery, options flow,
and re-authentication.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from typing import Any
from urllib.parse import urlparse

import voluptuous as vol
from homeassistant.components.ssdp import SsdpServiceInfo
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import callback

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
    CONF_CONNECTION_TYPE,
    CONF_CONSIDER_HOME,
    CONF_CUSTOM_FIRMWARE_REPO,
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

_LOGGER = logging.getLogger(__name__)

CONNECTION_TYPE_MAP = {
    CONNECTION_TYPE_UBUS: "ubus (HTTP/HTTPS) — Recommended",
    CONNECTION_TYPE_LUCI_RPC: "LuCI RPC (Web Interface)",
    CONNECTION_TYPE_SSH: "SSH",
}


class OpenWrtConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for OpenWrt."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize flow."""
        self._data: dict[str, Any] = {}
        self._device_info: dict[str, Any] = {}
        self._discovered_host: str | None = None
        self._discovered_name: str | None = None

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
                vol.Required(CONF_HOST): str,
                vol.Required(
                    CONF_CONNECTION_TYPE, default=CONNECTION_TYPE_UBUS
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

    async def async_step_ssdp(self, discovery_info: SsdpServiceInfo) -> ConfigFlowResult:
        """Handle SSDP auto-discovery."""
        ssdp_location = discovery_info.get("ssdp_location", "")
        parsed = urlparse(ssdp_location)
        host = parsed.hostname or ""

        if not host:
            return self.async_abort(reason="no_host")

        await self.async_set_unique_id(host)
        self._abort_if_unique_id_configured()

        friendly_name = discovery_info.get("friendlyName", "")
        server = discovery_info.get("server", "")
        manufacturer = discovery_info.get("manufacturer", "")

        openwrt_indicators = ["openwrt", "lede", "miniupnpd"]
        combined = f"{friendly_name} {server} {manufacturer}".lower()
        if not any(indicator in combined for indicator in openwrt_indicators):
            return self.async_abort(reason="not_openwrt")

        self._discovered_host = host
        self._discovered_name = friendly_name or f"OpenWrt ({host})"

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

            return await self._create_entry()

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
                return await self._create_entry()

        connection_type = self._data.get(CONF_CONNECTION_TYPE, CONNECTION_TYPE_UBUS)
        is_ubus = connection_type == CONNECTION_TYPE_UBUS

        schema_dict: dict[Any, Any] = {
            vol.Required(CONF_USERNAME, default=DEFAULT_USERNAME): str,
            vol.Required(CONF_PASSWORD): str,
            vol.Required(CONF_USE_SSL, default=DEFAULT_USE_SSL): bool,
            vol.Optional(CONF_VERIFY_SSL, default=DEFAULT_VERIFY_SSL): bool,
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
                return await self._create_entry()

        schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME, default=DEFAULT_USERNAME): str,
                vol.Optional(CONF_PASSWORD): str,
                vol.Optional(CONF_SSH_KEY): str,
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
        from .coordinator import create_client

        client = create_client(data)

        try:
            async with asyncio.timeout(15):
                await client.connect()
                device_info = await client.get_device_info()
                self._device_info = {
                    "hostname": device_info.hostname,
                    "model": device_info.model,
                    "firmware_version": device_info.firmware_version,
                }
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
            _LOGGER.exception("Unexpected error during connection test: %s", err)
            return "unknown"

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

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

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
            }
        )

        return self.async_show_form(step_id="init", data_schema=schema)
