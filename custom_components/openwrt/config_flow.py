from __future__ import annotations

import asyncio

# mypy: disable-error-code="attr-defined,has-type,assignment"
import logging
from typing import Any

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
)
from homeassistant.const import CONF_HOST
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import selector, translation
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_ASU_URL,
    CONF_CONSIDER_HOME,
    CONF_CUSTOM_FIRMWARE_REPO,
    CONF_DHCP_SOFTWARE,
    CONF_ENABLE_FIREWALL,
    CONF_ENABLE_LED,
    CONF_ENABLE_LOAD,
    CONF_ENABLE_SERVICES,
    CONF_ENABLE_SQM,
    CONF_ENABLE_VPN,
    CONF_MANUAL_TRACKED_DEVICES,
    CONF_MQTT_BROKER,
    CONF_MQTT_PASSWORD,
    CONF_MQTT_PORT,
    CONF_MQTT_PRESENCE,
    CONF_MQTT_USERNAME,
    CONF_MQTT_ZONE,
    CONF_SKIP_RANDOM_MAC,
    CONF_TARGET_OVERRIDE,
    CONF_TRACK_DEVICES,
    CONF_TRACK_WIRED,
    CONF_TRACKED_DEVICES,
    CONF_UPDATE_INTERVAL,
    CONNECTION_TYPE_LUCI_RPC,
    CONNECTION_TYPE_SSH,
    CONNECTION_TYPE_UBUS,
    DOMAIN,
)
from .coordinator import create_client

_LOGGER = logging.getLogger(__name__)

CONNECTION_TYPE_MAP = {
    CONNECTION_TYPE_UBUS: "ubus (HTTP/HTTPS)",
    CONNECTION_TYPE_LUCI_RPC: "LuCI RPC",
    CONNECTION_TYPE_SSH: "SSH (not recommended)",
}

from .flows.auth import AuthFlowMixin
from .flows.discovery import DiscoveryFlowMixin
from .flows.features import FeaturesFlowMixin
from .flows.helpers import (
    MQTT_PRESENCE_REDEPLOY_KEYS,
    _build_mqtt_deploy_args,
    _generate_diagnostic_report,
    _generate_package_table,
    _generate_permission_table,
    _resolve_tracked_device_label,
    should_redeploy_mqtt_presence,
    should_remove_mqtt_presence,
)
from .flows.options import OpenWrtOptionsFlow
from .flows.provisioning import ProvisioningFlowMixin


class OpenWrtConfigFlow(
    DiscoveryFlowMixin,
    AuthFlowMixin,
    ProvisioningFlowMixin,
    FeaturesFlowMixin,
    ConfigFlow,
    domain=DOMAIN,
):  # type: ignore[call-arg]
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
        self._diagnostic_report: str | None = None
        self._repair_username: str = "root"
        self._repair_password: str = ""
        self._repair_port: int = 22
        self._repair_error: str | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OpenWrtOptionsFlow:
        """Get the options flow."""
        return OpenWrtOptionsFlow(config_entry)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reconfiguration of the integration."""
        entry = self._get_reconfigure_entry()
        self._data = dict(entry.data)
        self._data.update(entry.options)
        return await self.async_step_manual_entry()

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
                in_progress.close()
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
        if getattr(self, "context", {}).get("source") != "reconfigure":
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
            CONF_SKIP_RANDOM_MAC,
            CONF_CUSTOM_FIRMWARE_REPO,
            CONF_ASU_URL,
            CONF_MQTT_PRESENCE,
            CONF_MQTT_BROKER,
            CONF_MQTT_PORT,
            CONF_MQTT_USERNAME,
            CONF_MQTT_PASSWORD,
            CONF_MQTT_ZONE,
            CONF_TRACKED_DEVICES,
            CONF_MANUAL_TRACKED_DEVICES,
            CONF_ENABLE_FIREWALL,
            CONF_ENABLE_SERVICES,
            CONF_ENABLE_VPN,
            CONF_ENABLE_LED,
            CONF_ENABLE_SQM,
            CONF_ENABLE_LOAD,
        ]:
            if key in data:
                options[key] = data.pop(key)

        if CONF_TARGET_OVERRIDE in data:
            options[CONF_TARGET_OVERRIDE] = data.pop(CONF_TARGET_OVERRIDE)

        title = hostname if hostname else host

        if getattr(self, "context", {}).get("source") == "reconfigure":
            entry = self._get_reconfigure_entry()
            self.hass.config_entries.async_update_entry(
                entry, title=title, data=data, options=options
            )
            await self.hass.config_entries.async_reload(entry.entry_id)
            return self.async_abort(reason="reconfigure_successful")

        return self.async_create_entry(title=title, data=data, options=options)


__all__ = [
    "CONNECTION_TYPE_MAP",
    "MQTT_PRESENCE_REDEPLOY_KEYS",
    "OpenWrtConfigFlow",
    "OpenWrtOptionsFlow",
    "_build_mqtt_deploy_args",
    "_generate_diagnostic_report",
    "_generate_package_table",
    "_generate_permission_table",
    "_resolve_tracked_device_label",
    "should_redeploy_mqtt_presence",
    "should_remove_mqtt_presence",
    "create_client",
    "async_get_clientsession",
    "selector",
    "translation",
    "asyncio",
]
