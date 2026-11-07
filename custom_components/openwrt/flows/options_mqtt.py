# mypy: disable-error-code="attr-defined,has-type,assignment"
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigFlowResult,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers import selector

from ..const import (
    CONF_MQTT_BROKER,
    CONF_MQTT_PASSWORD,
    CONF_MQTT_PORT,
    CONF_MQTT_PRESENCE,
    CONF_MQTT_USERNAME,
    CONF_MQTT_ZONE,
    CONF_REDEPLOY_USER,
    CONNECTION_TYPE_LUCI_RPC,
    CONNECTION_TYPE_SSH,
    CONNECTION_TYPE_UBUS,
    DATA_COORDINATOR,
    DOMAIN,
)


def create_client(*args: Any, **kwargs: Any) -> Any:
    from .. import config_flow

    return config_flow.create_client(*args, **kwargs)


from .helpers import (
    _build_mqtt_deploy_args,
    should_redeploy_mqtt_presence,
)

_LOGGER = logging.getLogger(__name__)

CONNECTION_TYPE_MAP = {
    CONNECTION_TYPE_UBUS: "ubus (HTTP/HTTPS)",
    CONNECTION_TYPE_LUCI_RPC: "LuCI RPC",
    CONNECTION_TYPE_SSH: "SSH (not recommended)",
}


class OptionsMqttFlowMixin:
    """MQTT presence and user redeployment steps for options flow."""

    async def async_step_options_mqtt_presence(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Handle MQTT presence step in options flow."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if self._permissions is None:
                try:
                    coordinator = (
                        self.hass.data.get(DOMAIN, {})
                        .get(self._config_entry.entry_id, {})
                        .get(DATA_COORDINATOR)
                    )
                    if coordinator and coordinator.data:
                        self._permissions = coordinator.data.permissions
                except (KeyError, AttributeError):
                    pass
            if not user_input.get(CONF_MQTT_PRESENCE):
                self._options.update(user_input)
                return await self.async_step_options_permissions()

            if self._permissions and not self._permissions.write_mqtt:
                errors["base"] = "mqtt_permission_missing"
            else:
                self._options.update(user_input)
                return await self.async_step_options_mqtt_zone()

        current = self._config_entry.options
        return self.async_show_form(
            step_id="options_mqtt_presence",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_MQTT_PRESENCE,
                        default=True,
                    ): bool,
                    vol.Optional(
                        CONF_MQTT_BROKER,
                        default=current.get(CONF_MQTT_BROKER, ""),
                    ): str,
                    vol.Optional(
                        CONF_MQTT_PORT,
                        default=current.get(CONF_MQTT_PORT, 1883),
                    ): int,
                    vol.Optional(
                        CONF_MQTT_USERNAME,
                        default=current.get(CONF_MQTT_USERNAME, ""),
                    ): str,
                    vol.Optional(
                        CONF_MQTT_PASSWORD,
                        default=current.get(CONF_MQTT_PASSWORD, ""),
                    ): str,
                }
            ),
            errors=errors,
        )

    async def async_step_options_mqtt_zone(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Handle MQTT zone selection step in options flow."""
        if user_input is not None:
            self._options.update(user_input)
            return await self.async_step_options_permissions()

        current = self._config_entry.options
        return self.async_show_form(
            step_id="options_mqtt_zone",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_MQTT_ZONE,
                        default=current.get(CONF_MQTT_ZONE, "zone.home"),
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="zone")
                    ),
                }
            ),
        )

    async def async_step_options_do_deploy_mqtt_presence(self) -> ConfigFlowResult:
        """Perform the actual deployment in options flow."""
        from ..helpers.mqtt_presence import async_deploy_mqtt_presence

        client = create_client(self.hass, {**self._config_entry.data, **self._options})
        try:
            await client.connect()

            current_opts = {**self._config_entry.options, **self._options}
            mqtt_config, tracked_devices, consider_home = _build_mqtt_deploy_args(
                current_opts
            )

            success, error = await async_deploy_mqtt_presence(
                self.hass, client, mqtt_config, tracked_devices, consider_home
            )
            if success:
                if self._options.get(CONF_REDEPLOY_USER) and not self._root_credentials:
                    return await self.async_step_options_redeploy_user()
                return self.async_create_entry(title="", data=self._options)

            return self.async_show_form(
                step_id="options_deploy_failed",
                description_placeholders={"error": error or "Unknown error"},
            )
        except Exception as err:
            _LOGGER.exception("Deployment failed")
            return self.async_show_form(
                step_id="options_deploy_failed",
                description_placeholders={"error": str(err)},
            )
        finally:
            await client.disconnect()

    async def async_step_options_deploy_failed(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle deployment failure in options flow."""
        if user_input is not None:
            # Re-try the deployment or go back to settings
            if user_input.get("action") == "retry":
                return await self.async_step_options_do_deploy_mqtt_presence()
            return await self.async_step_options_mqtt_presence()

        return self.async_show_form(
            step_id="options_deploy_failed",
            data_schema=vol.Schema(
                {
                    vol.Required("action", default="retry"): vol.In(["retry", "back"]),
                }
            ),
        )

    async def async_step_options_do_remove_mqtt_presence(self) -> ConfigFlowResult:
        """Perform the actual MQTT presence removal in options flow."""
        from ..helpers.mqtt_presence import async_remove_mqtt_presence

        client = create_client(self.hass, self._config_entry.data)
        try:
            await client.connect()
            success, error = await async_remove_mqtt_presence(client)
            if success:
                return self.async_create_entry(title="", data=self._options)

            return self.async_show_form(
                step_id="options_remove_failed",
                description_placeholders={"error": error or "Unknown error"},
            )
        except Exception as err:
            _LOGGER.exception("Failed to clean up MQTT presence on router")
            return self.async_show_form(
                step_id="options_remove_failed",
                description_placeholders={"error": str(err)},
            )
        finally:
            await client.disconnect()

    async def async_step_options_remove_failed(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle MQTT presence removal failure in options flow."""
        if user_input is not None:
            if user_input.get("action") == "retry":
                return await self.async_step_options_do_remove_mqtt_presence()
            return await self.async_step_init()

        return self.async_show_form(
            step_id="options_remove_failed",
            data_schema=vol.Schema(
                {
                    vol.Required("action", default="retry"): vol.In(["retry", "back"]),
                }
            ),
        )

    async def async_step_options_redeploy_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle redeploying the HA user and ACLs."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._root_credentials = user_input
            return await self.async_step_options_do_redeploy_user()

        return self.async_show_form(
            step_id="options_redeploy_user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME, default="root"): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    async def async_step_options_do_redeploy_user(self) -> ConfigFlowResult:
        """Perform the actual user redeployment."""
        # Use root credentials to redeploy the HA user
        root_data = {
            **self._config_entry.data,
            CONF_USERNAME: self._root_credentials[CONF_USERNAME],
            CONF_PASSWORD: self._root_credentials[CONF_PASSWORD],
        }
        client = create_client(self.hass, root_data)

        try:
            await client.connect()
            # The username to redeploy is the one from the config entry (usually 'homeassistant')
            ha_username = self._config_entry.data[CONF_USERNAME]
            ha_password = self._config_entry.data[CONF_PASSWORD]

            success, error = await client.provision_user(ha_username, ha_password)
            if success:
                if should_redeploy_mqtt_presence(
                    self._config_entry.options, self._options
                ):
                    return await self.async_step_options_mqtt_presence()
                return await self.async_step_options_permissions()

            return self.async_show_form(
                step_id="options_redeploy_user_failed",
                description_placeholders={"error": error or "Unknown error"},
            )
        except Exception as err:
            _LOGGER.exception("User redeployment failed")
            return self.async_show_form(
                step_id="options_redeploy_user_failed",
                description_placeholders={"error": str(err)},
            )
        finally:
            await client.disconnect()

    async def async_step_options_redeploy_user_failed(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle user redeployment failure."""
        if user_input is not None:
            if user_input.get("action") == "retry":
                return await self.async_step_options_redeploy_user()
            return await self.async_step_init()

        return self.async_show_form(
            step_id="options_redeploy_user_failed",
            data_schema=vol.Schema(
                {
                    vol.Required("action", default="retry"): vol.In(["retry", "back"]),
                }
            ),
        )
