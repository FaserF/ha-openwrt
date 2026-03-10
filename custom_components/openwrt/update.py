"""Update platform for OpenWrt integration.

Provides a unified firmware update entity that supports both official
OpenWrt releases and custom repository sources.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_HOST,
    DATA_COORDINATOR,
    DOMAIN,
)
from .coordinator import OpenWrtDataCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up OpenWrt update entities."""
    coordinator: OpenWrtDataCoordinator = hass.data[DOMAIN][entry.entry_id][
        DATA_COORDINATOR
    ]

    async_add_entities([OpenWrtUpdateEntity(coordinator, entry)])


class OpenWrtUpdateEntity(CoordinatorEntity[OpenWrtDataCoordinator], UpdateEntity):
    """Representation of an OpenWrt firmware update."""

    _attr_has_entity_name = True
    _attr_translation_key = "firmware_update"
    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_supported_features = (
        UpdateEntityFeature.RELEASE_NOTES | UpdateEntityFeature.INSTALL
    )
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: OpenWrtDataCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the update entity."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_firmware_update"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.data[CONF_HOST])},
        }

    @property
    def installed_version(self) -> str | None:
        """Return the installed firmware version."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.firmware_current_version or None

    @property
    def latest_version(self) -> str | None:
        """Return the latest available firmware version."""
        if self.coordinator.data is None:
            return None
        latest = self.coordinator.data.firmware_latest_version
        if not latest:
            return self.installed_version
        return latest

    @property
    def release_url(self) -> str | None:
        """Return the release URL."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.firmware_release_url or None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        if self.coordinator.data is None:
            return {}

        data = self.coordinator.data
        attrs = {
            "is_custom_build": data.is_custom_build,
            "target": data.device_info.target,
            "board_name": data.device_info.board_name,
        }

        if data.firmware_checksum:
            attrs["sha256_checksum"] = data.firmware_checksum

        return attrs

    async def async_release_notes(self) -> str | None:
        """Return release notes for the latest version."""
        if self.coordinator.data is None:
            return None

        data = self.coordinator.data
        latest = data.firmware_latest_version
        if not latest:
            return None

        if data.is_custom_build:
            notes = f"## Custom Firmware: {latest}\n\n"
            if data.firmware_checksum:
                notes += f"**SHA256 Checksum:** `{data.firmware_checksum}`\n\n"
            notes += "This firmware update is retrieved from your configured custom repository.\n\n"
        else:
            notes = f"## OpenWrt {latest}\n\n"
            notes += "A new official OpenWrt release is available.\n\n"
            notes += f"Visit the [OpenWrt release page](https://openwrt.org/releases/{latest}) for details.\n\n"

        notes += f"**Target:** `{data.device_info.target}`\n"
        notes += f"**Board:** `{data.device_info.board_name}`\n\n"
        notes += "⚠️ **Always back up your configuration before upgrading!**"

        return notes

    async def async_install(
        self, version: str | None, backup: bool, **kwargs: Any
    ) -> None:
        """Install the latest firmware version."""
        if not self.release_url:
            raise ValueError("No firmware URL available for installation.")

        _LOGGER.info("Initiating firmware installation from: %s", self.release_url)
        try:
            await self.coordinator.client.install_firmware(self.release_url)
        except Exception as err:
            raise HomeAssistantError(
                f"Failed to initiate firmware installation: {err}"
            ) from err
