# mypy: disable-error-code="attr-defined,has-type,assignment"
from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigFlowResult,
)
from homeassistant.const import CONF_HOST, CONF_USERNAME

from ..const import (
    CONF_CONNECTION_TYPE,
    CONF_ENABLE_FIREWALL,
    CONF_ENABLE_LED,
    CONF_ENABLE_LOAD,
    CONF_ENABLE_NLBWMON_SENSORS,
    CONF_ENABLE_SERVICES,
    CONF_ENABLE_SNORT_SENSORS,
    CONF_ENABLE_SQM,
    CONF_ENABLE_VPN,
    CONF_MANUAL_TRACKED_DEVICES,
    CONF_MQTT_BROKER,
    CONF_MQTT_PASSWORD,
    CONF_MQTT_PORT,
    CONF_MQTT_PRESENCE,
    CONF_MQTT_USERNAME,
    CONF_MQTT_ZONE,
    CONF_TRACK_DEVICES,
    CONF_TRACKED_DEVICES,
    CONNECTION_TYPE_LUCI_RPC,
    CONNECTION_TYPE_SSH,
    CONNECTION_TYPE_UBUS,
    DOMAIN,
)
from .helpers import selector, translation


def create_client(*args: Any, **kwargs: Any) -> Any:
    from .. import config_flow

    return config_flow.create_client(*args, **kwargs)


from .helpers import (
    _build_mqtt_deploy_args,
    _generate_package_table,
    _generate_permission_table,
    _resolve_tracked_device_label,
)

_LOGGER = logging.getLogger(__name__)

CONNECTION_TYPE_MAP = {
    CONNECTION_TYPE_UBUS: "ubus (HTTP/HTTPS)",
    CONNECTION_TYPE_LUCI_RPC: "LuCI RPC",
    CONNECTION_TYPE_SSH: "SSH (not recommended)",
}


class FeaturesFlowMixin:
    """Permissions, package check, selective tracking, and MQTT setup steps."""

    async def async_step_permissions(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Show permissions summary."""
        if user_input is not None:
            if getattr(self, "_packages", None) is not None:
                return await self.async_step_packages()
            return await self.async_step_mqtt_presence()

        if self._permissions is None:
            if getattr(self, "_packages", None) is not None:
                return await self.async_step_packages()
            return await self.async_step_mqtt_presence()

        if self._ubus_restricted:
            return await self.async_step_ubus_restricted()

        # Get translations for table
        translations = await translation.async_get_translations(
            self.hass, self.hass.config.language, "config", [DOMAIN]
        )
        table = _generate_permission_table(self._permissions, translations)

        step_id = "permissions"
        if self._data.get(CONF_CONNECTION_TYPE) == CONNECTION_TYPE_UBUS:
            step_id = "permissions_ubus"

        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema({vol.Optional("acknowledge", default=True): bool}),
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
            data_schema=vol.Schema({vol.Optional("acknowledge", default=True): bool}),
            description_placeholders={
                "host": self._data.get(CONF_HOST, ""),
                "model": self._device_info.get("model", "Router"),
            },
        )

    async def async_step_packages(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Show packages summary and allow enabling/disabling features."""
        if user_input is not None:
            # Remove internal acknowledge field and update data
            user_input.pop("acknowledge", None)
            self._data.update(user_input)
            if user_input.get(CONF_TRACK_DEVICES):
                return await self.async_step_selective_tracking()
            return await self.async_step_mqtt_presence()

        if getattr(self, "_packages", None) is None:
            return await self.async_step_mqtt_presence()

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

        # Build dynamic schema for feature toggles
        schema_dict = {
            vol.Optional(CONF_TRACK_DEVICES, default=True): bool,
            vol.Optional(CONF_ENABLE_LOAD, default=True): bool,
            vol.Optional(CONF_ENABLE_SERVICES, default=True): bool,
            vol.Optional(CONF_ENABLE_FIREWALL, default=True): bool,
            vol.Optional(CONF_ENABLE_LED, default=True): bool,
        }

        schema_dict[
            vol.Optional(CONF_ENABLE_SQM, default=bool(self._packages.sqm_scripts))
        ] = bool
        schema_dict[
            vol.Optional(
                CONF_ENABLE_VPN,
                default=bool(
                    self._packages.wireguard
                    or self._packages.openvpn
                    or self._packages.tailscale
                ),
            )
        ] = bool
        schema_dict[
            vol.Optional(
                CONF_ENABLE_NLBWMON_SENSORS,
                default=bool(self._packages.nlbwmon),
            )
        ] = bool
        schema_dict[
            vol.Optional(
                CONF_ENABLE_SNORT_SENSORS,
                default=bool(self._packages.snort),
            )
        ] = bool

        return self.async_show_form(
            step_id="packages",
            data_schema=vol.Schema(schema_dict),
            description_placeholders={"packages_table": table},
        )

    async def async_step_selective_tracking(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Allow the user to select which devices to track during initial setup."""
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_mqtt_presence()

        errors = {}
        device_options = {}

        try:
            client = create_client(self.hass, self._data)
            async with asyncio.timeout(15):
                await client.connect()
                # Fetch both connected devices and DHCP leases for a complete list
                devices = await client.get_connected_devices()
                leases = await client.get_dhcp_leases()
                await client.disconnect()

                # Hostnames already learned by other config entries, so an AP
                # with no DHCP server of its own still shows real names.
                shared_hostnames: dict[str, str] = (
                    self.hass.data.get(DOMAIN, {}).get("hostname_registry", {})
                    if isinstance(getattr(self.hass, "data", None), dict)
                    else {}
                )

                # Combine them. "*" is dnsmasq's placeholder for a client that
                # sent no hostname, so it counts as absent rather than a label.
                for d in devices:
                    if d.mac:
                        mac = d.mac.lower()
                        device_options[mac] = _resolve_tracked_device_label(
                            mac=mac,
                            original_mac=d.mac,
                            hostname=d.hostname,
                            shared_hostnames=shared_hostnames,
                            hass=self.hass,
                        )
                for lease in leases:
                    if lease.mac:
                        mac = lease.mac.lower()
                        if mac not in device_options:
                            device_options[mac] = _resolve_tracked_device_label(
                                mac=mac,
                                original_mac=lease.mac,
                                hostname=lease.hostname,
                                shared_hostnames=shared_hostnames,
                                hass=self.hass,
                                suffix=" [Lease]",
                            )
        except Exception as err:
            _LOGGER.warning("Could not fetch devices for selective tracking: %s", err)
            errors["base"] = "cannot_connect"

        current_tracked = self._data.get(CONF_TRACKED_DEVICES, [])
        if isinstance(current_tracked, str):
            current_tracked = [
                d.strip() for d in current_tracked.splitlines() if d.strip()
            ]
        current_manual = self._data.get(CONF_MANUAL_TRACKED_DEVICES, "")
        if isinstance(current_manual, list):
            current_manual = "\n".join(current_manual)

        shared_hostnames = self.hass.data.get(DOMAIN, {}).get("hostname_registry", {})
        for mac in current_tracked:
            if mac not in device_options:
                device_options[mac] = _resolve_tracked_device_label(
                    mac=mac.lower(),
                    original_mac=mac,
                    shared_hostnames=shared_hostnames,
                    hass=self.hass,
                )

        options: list[dict[str, str]] = sorted(
            [{"value": k, "label": v} for k, v in device_options.items()],
            key=lambda opt: opt["label"].lower(),
        )

        return self.async_show_form(
            step_id="selective_tracking",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_TRACKED_DEVICES,
                        default=current_tracked,
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=options,
                            multiple=True,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Optional(
                        CONF_MANUAL_TRACKED_DEVICES,
                        default=current_manual,
                    ): selector.TextSelector(
                        selector.TextSelectorConfig(
                            multiline=True,
                            type=selector.TextSelectorType.TEXT,
                        )
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_mqtt_presence(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Handle MQTT presence step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get(CONF_MQTT_PRESENCE):
                return await self._create_entry()

            # Inline validation
            broker = user_input.get(CONF_MQTT_BROKER, "")
            port = user_input.get(CONF_MQTT_PORT)

            if not broker:
                errors[CONF_MQTT_BROKER] = "empty_broker"
            if not port or not (1 <= port <= 65535):
                errors[CONF_MQTT_PORT] = "invalid_port"

            if not errors:
                if self._permissions is None:
                    from .api.base import OpenWrtPermissions

                    self._permissions = OpenWrtPermissions()
                if self._permissions and not self._permissions.write_mqtt:
                    errors["base"] = "mqtt_permission_missing"
                else:
                    self._data.update(user_input)
                    return await self.async_step_mqtt_zone()

        return self.async_show_form(
            step_id="mqtt_presence",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_MQTT_PRESENCE, default=False): bool,
                    vol.Optional(
                        CONF_MQTT_BROKER,
                        default=self._data.get(CONF_MQTT_BROKER, ""),
                    ): str,
                    vol.Optional(
                        CONF_MQTT_PORT,
                        default=self._data.get(CONF_MQTT_PORT, 1883),
                    ): int,
                    vol.Optional(
                        CONF_MQTT_USERNAME,
                        default=self._data.get(CONF_MQTT_USERNAME, ""),
                    ): str,
                    vol.Optional(
                        CONF_MQTT_PASSWORD,
                        default=self._data.get(CONF_MQTT_PASSWORD, ""),
                    ): str,
                }
            ),
            errors=errors,
        )

    async def async_step_mqtt_zone(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Handle MQTT zone selection step."""
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_do_deploy_mqtt_presence()

        return self.async_show_form(
            step_id="mqtt_zone",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_MQTT_ZONE,
                        default=self._data.get(CONF_MQTT_ZONE, "zone.home"),
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="zone")
                    ),
                }
            ),
        )

    async def async_step_do_deploy_mqtt_presence(self) -> ConfigFlowResult:
        """Perform the actual deployment."""
        from ..helpers.mqtt_presence import async_deploy_mqtt_presence

        client = create_client(self.hass, self._data)
        try:
            await client.connect()

            mqtt_config, tracked_devices, consider_home = _build_mqtt_deploy_args(
                self._data
            )

            success, error = await async_deploy_mqtt_presence(
                self.hass, client, mqtt_config, tracked_devices, consider_home
            )
            if success:
                return await self._create_entry()

            return self.async_show_form(
                step_id="deploy_failed",
                description_placeholders={"error": error or "Unknown error"},
            )
        except Exception as err:
            _LOGGER.exception("Deployment failed")
            return self.async_show_form(
                step_id="deploy_failed",
                description_placeholders={"error": str(err)},
            )
        finally:
            await client.disconnect()

    async def async_step_deploy_failed(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle deployment failure."""
        if user_input is not None:
            # Re-try the deployment or go back to settings
            if user_input.get("action") == "retry":
                return await self.async_step_do_deploy_mqtt_presence()
            return await self.async_step_mqtt_presence()

        return self.async_show_form(
            step_id="deploy_failed",
            data_schema=vol.Schema(
                {
                    vol.Required("action", default="retry"): vol.In(["retry", "back"]),
                }
            ),
        )
