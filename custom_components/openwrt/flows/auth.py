# mypy: disable-error-code="attr-defined,has-type,assignment"
from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import (
    ConfigFlowResult,
)
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME

from .helpers import selector, translation


def async_get_clientsession(*args: Any, **kwargs: Any) -> Any:
    from .. import config_flow

    return config_flow.async_get_clientsession(*args, **kwargs)


from ..api.luci_rpc import (
    LuciRpcAuthError,
    LuciRpcConnectionError,
    LuciRpcError,
    LuciRpcPackageMissingError,
    LuciRpcSslError,
    LuciRpcTimeoutError,
)
from ..api.ssh import (
    SshAuthError,
    SshConnectionError,
    SshError,
    SshKeyError,
    SshTimeoutError,
)
from ..api.ubus import (
    UbusAuthError,
    UbusConnectionError,
    UbusError,
    UbusPackageMissingError,
    UbusPermissionError,
    UbusSslError,
    UbusTimeoutError,
)
from ..const import (
    CONF_CONNECTION_TYPE,
    CONF_DHCP_SOFTWARE,
    CONF_SSH_KEY,
    CONF_UBUS_PATH,
    CONF_USE_SSL,
    CONF_VERIFY_SSL,
    CONNECTION_TYPE_LUCI_RPC,
    CONNECTION_TYPE_SSH,
    CONNECTION_TYPE_UBUS,
    DEFAULT_PORT_SSH,
    DEFAULT_PORT_UBUS,
    DEFAULT_PORT_UBUS_SSL,
    DEFAULT_UBUS_PATH,
    DEFAULT_USE_SSL,
    DEFAULT_USERNAME,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
)


def create_client(*args: Any, **kwargs: Any) -> Any:
    from .. import config_flow

    return config_flow.create_client(*args, **kwargs)


from .helpers import (
    _generate_diagnostic_report,
)

_LOGGER = logging.getLogger(__name__)

CONNECTION_TYPE_MAP = {
    CONNECTION_TYPE_UBUS: "ubus (HTTP/HTTPS)",
    CONNECTION_TYPE_LUCI_RPC: "LuCI RPC",
    CONNECTION_TYPE_SSH: "SSH (not recommended)",
}


class AuthFlowMixin:
    """Credentials, SSH, reauthentication, and connection test steps."""

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
                if self._diagnostic_report:
                    return await self.async_step_diagnostics()
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
            vol.Required(
                CONF_USERNAME,
                default=self._data.get(CONF_USERNAME, DEFAULT_USERNAME),
            ): str,
            vol.Required(
                CONF_PASSWORD,
                default=self._data.get(CONF_PASSWORD, ""),
            ): str,
            vol.Required(
                CONF_USE_SSL,
                default=self._data.get(CONF_USE_SSL, DEFAULT_USE_SSL),
            ): bool,
            vol.Optional(
                CONF_VERIFY_SSL,
                default=self._data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL),
            ): bool,
            vol.Optional(
                CONF_DHCP_SOFTWARE,
                default=self._data.get(CONF_DHCP_SOFTWARE, "auto"),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=["auto", "dnsmasq", "odhcpd", "none"],
                    translation_key="dhcp_software",
                    mode=selector.SelectSelectorMode.DROPDOWN,
                ),
            ),
        }

        if CONF_PORT in self._data:
            schema_dict[vol.Optional(CONF_PORT, default=self._data[CONF_PORT])] = int
        else:
            schema_dict[vol.Optional(CONF_PORT)] = int

        if is_ubus:
            schema_dict[
                vol.Optional(
                    CONF_UBUS_PATH,
                    default=self._data.get(CONF_UBUS_PATH, DEFAULT_UBUS_PATH),
                )
            ] = str

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
                if self._diagnostic_report:
                    return await self.async_step_diagnostics()
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
                vol.Required(
                    CONF_USERNAME,
                    default=self._data.get(CONF_USERNAME, DEFAULT_USERNAME),
                ): str,
                vol.Optional(
                    CONF_PASSWORD,
                    default=self._data.get(CONF_PASSWORD, ""),
                ): str,
                vol.Optional(
                    CONF_SSH_KEY,
                    default=self._data.get(CONF_SSH_KEY, ""),
                ): str,
                vol.Optional(
                    CONF_DHCP_SOFTWARE,
                    default=self._data.get(CONF_DHCP_SOFTWARE, "auto"),
                ): vol.In(["auto", "dnsmasq", "odhcpd", "none"]),
                vol.Optional(
                    CONF_PORT,
                    default=self._data.get(CONF_PORT, DEFAULT_PORT_SSH),
                ): int,
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
                if self._diagnostic_report:
                    return await self.async_step_diagnostics()
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
        self._diagnostic_report = None
        _LOGGER.debug(
            "Starting connection test with data: %s",
            {k: (v if k != CONF_PASSWORD else "********") for k, v in data.items()},
        )
        try:
            client = create_client(self.hass, data)
            _LOGGER.debug("Client created: %s", client)
            async with asyncio.timeout(30):
                await self._perform_connection_test(client, data)
            return None
        except Exception as err:
            _LOGGER.exception("Connection test failed: %s", err)
            # Try to run diagnostics if possible, but skip for plain auth errors
            # to avoid overwhelming the user with a report for a simple wrong password.
            if not isinstance(
                err, (UbusAuthError, LuciRpcAuthError, SshAuthError, SshKeyError)
            ):
                with contextlib.suppress(Exception):
                    if client:
                        results = await client.perform_diagnostics()
                        if results:
                            # Get translations for report
                            translations = await translation.async_get_translations(
                                self.hass, self.hass.config.language, "config", [DOMAIN]
                            )
                            self._diagnostic_report = _generate_diagnostic_report(
                                results, translations
                            )
            return self._handle_test_error(err, data.get(CONF_USERNAME))
        finally:
            await client.disconnect()

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
            err_str = str(err).lower()
            if (
                "connection refused" in err_str
                or "connect call failed" in err_str
                or "1225" in err_str
            ):
                _LOGGER.error(
                    "Connection refused during setup for user %s. The router's service (uhttpd/nginx or SSH) may be offline/crashed, or firewall rules are blocking the connection.",
                    username,
                )
            elif "404" in err_str or "not found" in err_str:
                _LOGGER.error(
                    "API endpoint not found (HTTP 404) during setup. The required package (uhttpd-mod-ubus or luci-mod-rpc) is likely missing on the router.",
                )
            else:
                _LOGGER.warning("Connection/API error during test: %s", err)
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
