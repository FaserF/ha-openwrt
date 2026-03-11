"""HA Repairs integration for OpenWrt.

Creates actionable repair issues for common problems:
- Authentication failures (with reauth link)
- WAN connectivity loss
- Missing recommended OpenWrt packages
- Outdated firmware warnings
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow
from homeassistant.config_entries import ConfigEntry
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
        return AuthFailedRepairFlow()
    return ConfirmRepairFlow()


class AuthFailedRepairFlow(RepairsFlow):
    """Handler for auth failure repair flow - triggers re-authentication."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the init step - redirect to reauth."""
        if user_input is not None:
            entry_id = self.data.get("entry_id") if self.data else None
            if entry_id:
                entry = self.hass.config_entries.async_get_entry(str(entry_id))
                if entry:
                    entry.async_start_reauth(self.hass)
            return self.async_abort(reason="reauth_started")

        return self.async_show_form(step_id="init")
