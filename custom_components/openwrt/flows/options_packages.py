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
    CONF_TRACK_DEVICES,
    CONNECTION_TYPE_LUCI_RPC,
    CONNECTION_TYPE_SSH,
    CONNECTION_TYPE_UBUS,
    DOMAIN,
)
from .helpers import translation


def create_client(*args: Any, **kwargs: Any) -> Any:
    from .. import config_flow

    return config_flow.create_client(*args, **kwargs)


from .helpers import (
    _generate_package_table,
    _generate_permission_table,
)

_LOGGER = logging.getLogger(__name__)

CONNECTION_TYPE_MAP = {
    CONNECTION_TYPE_UBUS: "ubus (HTTP/HTTPS)",
    CONNECTION_TYPE_LUCI_RPC: "LuCI RPC",
    CONNECTION_TYPE_SSH: "SSH (not recommended)",
}


class OptionsPackagesFlowMixin:
    """Permissions and packages review steps for options flow."""

    async def async_step_options_permissions(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Show permissions summary."""
        if user_input is not None:
            if self._packages is not None:
                return await self.async_step_options_packages()
            return await self._async_finalize_options()

        client = create_client(self.hass, {**self._config_entry.data, **self._options})
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
                return await self.async_step_options_packages()
            return await self._async_finalize_options()

        if self._ubus_restricted:
            return await self.async_step_options_ubus_restricted()

        # Get translations for table
        translations = await translation.async_get_translations(
            self.hass, self.hass.config.language, "config", [DOMAIN]
        )
        table = _generate_permission_table(self._permissions, translations)

        step_id = "options_permissions"
        if self._config_entry.data.get(CONF_CONNECTION_TYPE) == CONNECTION_TYPE_UBUS:
            step_id = "options_permissions_ubus"

        _LOGGER.debug(
            "Showing options permissions step: id=%s, username=%s, table_len=%d",
            step_id,
            self._config_entry.data.get(CONF_USERNAME, ""),
            len(table),
        )

        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema({vol.Optional("acknowledge", default=True): bool}),
            description_placeholders={
                "permissions_table": table,
                "username": self._config_entry.data.get(CONF_USERNAME, ""),
            },
        )

    async def async_step_options_permissions_ubus(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Show permissions summary (ubus variant)."""
        return await self.async_step_options_permissions(user_input)

    async def async_step_options_ubus_restricted(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Inform the user about restricted Ubus access."""
        if user_input is not None:
            if self._packages is not None:
                return await self.async_step_options_packages()
            return await self._async_finalize_options()

        # Try to get model from direct client or use existing device registry if available
        model = self._config_entry.data.get(CONF_HOST, "Router")

        return self.async_show_form(
            step_id="options_ubus_restricted",
            data_schema=vol.Schema({vol.Optional("acknowledge", default=True): bool}),
            description_placeholders={
                "host": self._config_entry.data.get(CONF_HOST, ""),
                "model": model,
            },
        )

    async def async_step_options_packages(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Show packages summary and allow enabling/disabling features."""
        if user_input is not None:
            # Remove internal acknowledge field and update options
            user_input.pop("acknowledge", None)
            self._options.update(user_input)
            if user_input.get(CONF_TRACK_DEVICES):
                return await self.async_step_options_select_devices()
            return await self._async_finalize_options()

        if self._packages is None:
            return await self._async_finalize_options()

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

        # Build dynamic schema for feature toggles based on current settings
        current = {**self._config_entry.options, **self._options}
        schema_dict = {
            vol.Optional(
                CONF_TRACK_DEVICES,
                default=current.get(CONF_TRACK_DEVICES, True),
            ): bool,
            vol.Optional(
                CONF_ENABLE_LOAD,
                default=current.get(CONF_ENABLE_LOAD, True),
            ): bool,
            vol.Optional(
                CONF_ENABLE_SERVICES,
                default=current.get(CONF_ENABLE_SERVICES, True),
            ): bool,
            vol.Optional(
                CONF_ENABLE_FIREWALL,
                default=current.get(CONF_ENABLE_FIREWALL, True),
            ): bool,
            vol.Optional(
                CONF_ENABLE_LED,
                default=current.get(CONF_ENABLE_LED, True),
            ): bool,
        }

        schema_dict[
            vol.Optional(
                CONF_ENABLE_SQM,
                default=(
                    current.get(CONF_ENABLE_SQM, True)
                    if self._packages.sqm_scripts
                    else False
                ),
            )
        ] = bool
        schema_dict[
            vol.Optional(
                CONF_ENABLE_VPN,
                default=(
                    current.get(CONF_ENABLE_VPN, True)
                    if (
                        self._packages.wireguard
                        or self._packages.openvpn
                        or self._packages.tailscale
                    )
                    else False
                ),
            )
        ] = bool
        schema_dict[
            vol.Optional(
                CONF_ENABLE_NLBWMON_SENSORS,
                default=(
                    current.get(
                        CONF_ENABLE_NLBWMON_SENSORS,
                        self._config_entry.data.get(CONF_ENABLE_NLBWMON_SENSORS, False),
                    )
                    if self._packages.nlbwmon
                    else False
                ),
            )
        ] = bool
        schema_dict[
            vol.Optional(
                CONF_ENABLE_SNORT_SENSORS,
                default=(
                    current.get(
                        CONF_ENABLE_SNORT_SENSORS,
                        self._config_entry.data.get(CONF_ENABLE_SNORT_SENSORS, False),
                    )
                    if self._packages.snort
                    else False
                ),
            )
        ] = bool

        return self.async_show_form(
            step_id="options_packages",
            data_schema=vol.Schema(schema_dict),
            description_placeholders={"packages_table": table},
        )
