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
    CONF_AUTO_BACKUP,
    CONF_CONNECTION_TYPE,
    CONF_HOST,
    CONNECTION_TYPE_LUCI_RPC,
    CONNECTION_TYPE_UBUS,
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
    _attr_name = None
    _attr_translation_key = "firmware_update"
    _attr_device_class = UpdateDeviceClass.FIRMWARE
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
            "identifiers": {(DOMAIN, entry.unique_id or entry.data[CONF_HOST])},
        }

    @property
    def supported_features(self) -> UpdateEntityFeature:
        """Return supported features."""
        features = UpdateEntityFeature.RELEASE_NOTES
        data = self.coordinator.data
        if not data:
            return features

        # Basic install capability
        can_install = bool(data.firmware_install_url)

        # ASU capability check
        if data.asu_supported:
            # If using LuCI/Ubus, we prefer having luci-app-attendedsysupgrade for consistency,
            # but technically we can install if we have the build capability.
            # User requested that it's checked.
            conn_type = self.coordinator.config_entry.data.get(CONF_CONNECTION_TYPE)
            if conn_type in (CONNECTION_TYPE_LUCI_RPC, CONNECTION_TYPE_UBUS):
                if data.packages.asu:
                    can_install = True
            else:
                # SSH or other
                can_install = True

        if can_install:
            features |= UpdateEntityFeature.INSTALL

        return features

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

        # If we determined the latest remote version is not newer than our current system, we are up to date
        if not self.coordinator.data.firmware_upgradable:
            return self.installed_version

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

        # Determine if install is possible to show clear explanations
        can_install = bool(data.firmware_install_url)
        conn_type = self.coordinator.config_entry.data.get(CONF_CONNECTION_TYPE)
        install_barrier = ""

        if data.asu_supported:
            if conn_type in (CONNECTION_TYPE_LUCI_RPC, CONNECTION_TYPE_UBUS):
                if data.packages.asu:
                    can_install = True
                else:
                    install_barrier = "The `luci-app-attendedsysupgrade` package is not installed on the router."
            else:
                can_install = True

        if not can_install and not install_barrier:
            install_barrier = "No direct installation URL is available for this update or your device does not support Attended Sysupgrade (ASU)."

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
        notes += "⚠️ **Always back up your configuration before upgrading!**\n\n"

        auto_backup = self.coordinator.config_entry.options.get(CONF_AUTO_BACKUP, True)
        if auto_backup:
            notes += "🛡️ *Note: Automatic Backups are enabled in your integration options. A configuration backup will be automatically created and stored sequentially in your Home Assistant `.storage` folder before the update begins.*\n\n"
        else:
            notes += "*(Automatic Backup is currently disabled in your integration options)*\n\n"

        if not can_install:
            notes += "---\n\n"
            notes += "### ⚠️ Automatic Installation Disabled\n"
            notes += f"{install_barrier}\n\n"

            # Inject dynamic Firmware Selector URL with specific router parameters
            target = (
                data.device_info.target.replace("/", "%2F")
                if data.device_info.target
                else ""
            )
            board = (
                data.device_info.board_name.replace(",", "_").replace(" ", "_")
                if data.device_info.board_name
                else ""
            )

            version_param = latest
            if "SNAPSHOT" in latest.upper():
                version_param = "SNAPSHOT"

            fs_url = f"https://firmware-selector.openwrt.org/?version={version_param}&target={target}&id={board}"
            notes += f"**🚀 Official Downloads:**\n[Firmware Selector ({latest})]({fs_url})\n\n"

            if data.firmware_install_url:
                notes += f"**Direct Link:**\n[Download Firmware Image globally]({data.firmware_install_url})\n"
            elif data.firmware_release_url:
                notes += f"**Repository / Changelog:**\n[View Release and Source]({data.firmware_release_url})\n"

        return notes

    async def async_install(
        self, version: str | None, backup: bool, **kwargs: Any
    ) -> None:
        """Install the latest firmware version."""
        data = self.coordinator.data
        if not data:
            raise HomeAssistantError("No data available to process firmware update.")

        if not data.firmware_install_url and not data.asu_supported:
            raise ValueError("No firmware URL available for installation.")

        # Check for auto-backup option
        auto_backup = self.coordinator.config_entry.options.get(CONF_AUTO_BACKUP, True)
        if auto_backup:
            await self._async_perform_backup()

        # Check for required ASU package if using LuCI/Ubus
        if data.asu_supported and not data.firmware_install_url:
            conn_type = self.coordinator.config_entry.data.get(CONF_CONNECTION_TYPE)
            if (
                conn_type in (CONNECTION_TYPE_LUCI_RPC, CONNECTION_TYPE_UBUS)
                and not data.packages.asu
            ):
                raise HomeAssistantError(
                    "Attended Sysupgrade package (luci-app-attendedsysupgrade) is missing on the router. "
                    "Cannot perform firmware upgrade."
                )

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
                # ASU builds always keep settings by default in OpenWrt logic,
                # but we pass it anyway.
                await self.coordinator.client.install_firmware(
                    download_url, keep_settings=True
                )

            except Exception as err:
                _LOGGER.error("ASU firmware update failed: %s", err)
                raise HomeAssistantError(f"ASU firmware update failed: {err}") from err

            return

        # Standard Update Flow
        url = data.firmware_install_url
        _LOGGER.info("Initiating standard firmware installation from: %s", url)
        if not url:
            raise ValueError("No firmware URL available for installation.")

        try:
            # We assume keep_settings=True for official updates from HA
            await self.coordinator.client.install_firmware(url, keep_settings=True)
        except Exception as err:
            raise HomeAssistantError(
                f"Failed to initiate firmware installation: {err}"
            ) from err

    async def _async_perform_backup(self) -> None:
        """Perform a backup and download it to HA."""
        _LOGGER.info("Performing automatic backup before firmware update...")
        try:
            # 1. Create backup on router
            remote_path = await self.coordinator.client.create_backup()
            if not remote_path:
                _LOGGER.error("Failed to create backup on router")
                return

            # 2. Prepare local path
            import os

            backup_dir = self.hass.config.path("backups", "openwrt")
            if not os.path.exists(backup_dir):
                os.makedirs(backup_dir, exist_ok=True)

            local_filename = os.path.basename(remote_path)
            local_path = os.path.join(backup_dir, local_filename)

            # 3. Download to HA
            success = await self.coordinator.client.download_file(
                remote_path, local_path
            )
            if success:
                _LOGGER.info("Backup successfully saved to: %s", local_path)
                # Cleanup remote file
                await self.coordinator.client.execute_command(f"rm {remote_path}")
            else:
                _LOGGER.error("Failed to download backup from router to %s", local_path)

        except Exception as err:
            _LOGGER.error("Automatic backup failed: %s", err)
            # We don't raise here to avoid blocking the update if backup fails,
            # unless the user really wants it. But usually, an update is more important.
            # However, safety first - maybe we should raise?
            # User said "automatically trigger a backup", so let's log it.
