"""HA Repairs integration for OpenWrt.

Creates actionable repair issues for common problems:
- Authentication failures (with reauth link)
- WAN connectivity loss
- Missing recommended OpenWrt packages
- Outdated firmware warnings
"""

from __future__ import annotations

import logging
from typing import Any, cast

from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

ISSUE_AUTH_FAILED = "auth_failed_{entry_id}"
ISSUE_WAN_DOWN = "wan_down_{entry_id}"
ISSUE_MISSING_PACKAGES = "missing_packages_{entry_id}"
ISSUE_FIRMWARE_OUTDATED = "firmware_outdated_{entry_id}"
ISSUE_CONNECTION_LOST = "connection_lost_{entry_id}"
ISSUE_STALE_PERMISSIONS = "stale_permissions_{entry_id}"


@callback
def async_create_auth_repair(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> None:
    """Create a repair issue for authentication failure."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        ISSUE_AUTH_FAILED.format(entry_id=entry.entry_id),
        is_fixable=True,
        is_persistent=True,
        severity=ir.IssueSeverity.ERROR,
        translation_key="auth_failed",
        translation_placeholders={
            "host": entry.data.get("host", "unknown"),
            "entry_title": entry.title,
        },
        data={"entry_id": entry.entry_id},
    )


@callback
def async_create_stale_permissions_repair(
    hass: HomeAssistant,
    entry: ConfigEntry,
    is_upgrade: bool = False,
) -> None:
    """Create a repair issue for stale permissions."""
    translation_key = "stale_permissions_upgrade" if is_upgrade else "stale_permissions"

    ir.async_create_issue(
        hass,
        DOMAIN,
        ISSUE_STALE_PERMISSIONS.format(entry_id=entry.entry_id),
        is_fixable=True,
        is_persistent=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key=translation_key,
        translation_placeholders={
            "host": entry.data.get("host", "unknown"),
            "username": entry.data.get("username", "homeassistant"),
        },
        data={"entry_id": entry.entry_id},
    )


@callback
def async_delete_stale_permissions_repair(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> None:
    """Remove stale permissions issue."""
    ir.async_delete_issue(
        hass,
        DOMAIN,
        ISSUE_STALE_PERMISSIONS.format(entry_id=entry.entry_id),
    )


@callback
def async_create_connection_lost_repair(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> None:
    """Create a repair issue for connection loss."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        ISSUE_CONNECTION_LOST.format(entry_id=entry.entry_id),
        is_fixable=False,
        is_persistent=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="connection_lost",
        translation_placeholders={
            "host": entry.data.get("host", "unknown"),
            "entry_title": entry.title,
        },
    )


@callback
def async_delete_connection_lost_repair(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> None:
    """Remove connection lost issue once reconnected."""
    ir.async_delete_issue(
        hass,
        DOMAIN,
        ISSUE_CONNECTION_LOST.format(entry_id=entry.entry_id),
    )


@callback
def async_create_wan_down_repair(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> None:
    """Create a repair issue for WAN connectivity loss."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        ISSUE_WAN_DOWN.format(entry_id=entry.entry_id),
        is_fixable=False,
        is_persistent=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="wan_down",
        translation_placeholders={
            "host": entry.data.get("host", "unknown"),
            "entry_title": entry.title,
        },
    )


@callback
def async_delete_wan_down_repair(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> None:
    """Remove WAN down issue once connectivity is restored."""
    ir.async_delete_issue(
        hass,
        DOMAIN,
        ISSUE_WAN_DOWN.format(entry_id=entry.entry_id),
    )


@callback
def async_create_missing_packages_repair(
    hass: HomeAssistant,
    entry: ConfigEntry,
    packages: list[str],
) -> None:
    """Create a repair issue for missing recommended OpenWrt packages."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        ISSUE_MISSING_PACKAGES.format(entry_id=entry.entry_id),
        is_fixable=False,
        is_persistent=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key="missing_packages",
        translation_placeholders={
            "host": entry.data.get("host", "unknown"),
            "packages": ", ".join(packages),
        },
    )


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, Any] | None,
) -> RepairsFlow:
    """Create a repair flow for fixable issues."""
    if issue_id.startswith("auth_failed_"):
        return AuthFailedRepairFlow(issue_id, data)
    if issue_id.startswith("stale_permissions_"):
        return StalePermissionsRepairFlow(issue_id, data)
    return ConfirmRepairFlow()


class AuthFailedRepairFlow(RepairsFlow):
    """Handler for auth failure repair flow - triggers re-authentication."""

    def __init__(self, issue_id: str, data: dict[str, Any] | None) -> None:
        """Initialize."""
        self.issue_id = issue_id
        self.data = data

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> FlowResult:
        """Handle the init step - redirect to reauth."""
        entry_id = self.data.get("entry_id") if self.data else None
        if not entry_id:
            entry_id = self.issue_id.split("_")[-1]

        if user_input is not None:
            if entry_id:
                entry = self.hass.config_entries.async_get_entry(str(entry_id))
                if entry:
                    entry.async_start_reauth(self.hass)
            return self.async_abort(reason="reauth_started")

        return self.async_show_form(step_id="init")


class StalePermissionsRepairFlow(RepairsFlow):
    """Handler for stale permissions repair flow - re-provisions the user."""

    def __init__(self, issue_id: str, data: dict[str, Any] | None) -> None:
        """Initialize."""
        self.issue_id = issue_id
        self.data = data
        self._root_data: dict[str, Any] = {}

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> FlowResult:
        """Handle the init step."""
        entry_id = self.data.get("entry_id") if self.data else None
        if not entry_id:
            entry_id = self.issue_id.split("_")[-1]

        if not entry_id:
            return self.async_abort(reason="unknown_error")

        entry = self.hass.config_entries.async_get_entry(str(entry_id))
        if not entry:
            return self.async_abort(reason="unknown_error")

        if user_input is not None:
            return await self.async_step_root_login()

        return self.async_show_form(
            step_id="init",
            description_placeholders={
                "host": entry.data.get("host", "unknown"),
                "username": entry.data.get("username", "homeassistant"),
            },
        )

    async def async_step_root_login(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> FlowResult:
        """Ask for root credentials."""
        import voluptuous as vol
        from homeassistant.const import CONF_PASSWORD, CONF_USERNAME

        from .coordinator import create_client

        errors: dict[str, str] = {}
        entry_id = self.data.get("entry_id") if self.data else None
        if not entry_id:
            entry_id = self.issue_id.split("_")[-1]

        if not entry_id:
            return self.async_abort(reason="unknown_error")
        entry = self.hass.config_entries.async_get_entry(str(entry_id))
        if not entry:
            return self.async_abort(reason="unknown_error")

        if user_input is not None:
            root_data = dict(entry.data)
            root_data[CONF_USERNAME] = user_input.get(CONF_USERNAME, "root")
            root_data[CONF_PASSWORD] = user_input[CONF_PASSWORD]

            client = create_client(root_data)
            try:
                await client.connect()
                # Provision the user again with EXISTING credentials
                ha_username = entry.data.get(CONF_USERNAME, "homeassistant")
                ha_password = cast(str, entry.data.get(CONF_PASSWORD, ""))

                success, error = await client.provision_user(ha_username, ha_password)
                if success:
                    # We didn't change the password, so just reload or just mark as fixed
                    await self.hass.config_entries.async_reload(entry.entry_id)
                    return self.async_create_entry(title="", data={})
                errors["base"] = "provision_failed"
                _LOGGER.error("Provisioning failed during repair: %s", error)
            except Exception:
                _LOGGER.exception("Failed to connect as root")
                errors["base"] = "cannot_connect"
            finally:
                await client.disconnect()

        return self.async_show_form(
            step_id="root_login",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME, default="root"): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
            description_placeholders={"host": entry.data.get(CONF_HOST, "unknown")},
        )
