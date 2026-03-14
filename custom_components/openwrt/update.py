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

    if coordinator.data:
        perms = coordinator.data.permissions
        if perms.read_system:
            async_add_entities([OpenWrtUpdateEntity(coordinator, entry)])


class OpenWrtUpdateEntity(CoordinatorEntity[OpenWrtDataCoordinator], UpdateEntity):
    """Representation of an OpenWrt firmware update."""

    _attr_has_entity_name = True
    _attr_name = "Firmware"
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
            "asu_supported": data.asu_supported,
            "asu_update_available": data.asu_update_available,
        }

        if data.asu_image_status:
            attrs["asu_image_status"] = data.asu_image_status

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
        data = self.coordinator.data
        if not data:
            raise HomeAssistantError("No data available to process firmware update.")

        if not self.release_url and not data.asu_supported:
            raise ValueError("No firmware URL available for installation.")

        # ASU Update Flow
        if data.asu_supported and data.asu_update_available:
            _LOGGER.info(
                "Initiating ASU custom firmware build for %s",
                data.firmware_latest_version,
            )
            try:
                from .const import CONF_ASU_URL
                from .helpers.asu import AsuClient

                asu_url = self.coordinator.config_entry.options.get(
                    CONF_ASU_URL, "https://sysupgrade.openwrt.org"
                )
                asu_client = AsuClient(self.hass, asu_url)

                request_hash = await asu_client.request_build(
                    version=data.firmware_latest_version,
                    target=data.device_info.target,
                    board_name=data.device_info.board_name,
                    packages=data.installed_packages,
                    client_name=f"Home Assistant OpenWrt Integration ({self.coordinator.name})",
                )

                _LOGGER.info(
                    "ASU build requested (hash: %s). Waiting for image...", request_hash
                )
                download_url = await asu_client.poll_build_status(request_hash)

                _LOGGER.info(
                    "ASU build complete. Flashing image from: %s", download_url
                )
                await self.coordinator.client.install_firmware(download_url)

            except Exception as err:
                _LOGGER.error("ASU firmware update failed: %s", err)
                raise HomeAssistantError(f"ASU firmware update failed: {err}") from err

            return

        # Standard Update Flow
        _LOGGER.info(
            "Initiating standard firmware installation from: %s", self.release_url
        )
        try:
            if url := self.release_url:
                await self.coordinator.client.install_firmware(url)
            else:
                raise ValueError("No firmware URL available for installation.")
        except Exception as err:
            raise HomeAssistantError(
                f"Failed to initiate firmware installation: {err}"
            ) from err
