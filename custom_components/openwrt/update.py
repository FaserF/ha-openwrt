"""Update platform for OpenWrt integration.

Provides a unified firmware update entity that supports both official
OpenWrt releases and custom repository sources.
"""

from __future__ import annotations

import asyncio
import logging
import re
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
    ASU_STATUS_AVAILABLE,
    ASU_STATUS_TEMPORARILY_UNAVAILABLE,
    CONF_ASU_URL,
    CONF_AUTO_BACKUP,
    CONF_CHECK_FIRMWARE_UPDATES,
    CONF_CONNECTION_TYPE,
    CONNECTION_TYPE_LUCI_RPC,
    CONNECTION_TYPE_UBUS,
    DATA_COORDINATOR,
    DEFAULT_ASU_URL,
    DEFAULT_CHECK_FIRMWARE_UPDATES,
    DOMAIN,
)
from .coordinator import OpenWrtDataCoordinator

_LOGGER = logging.getLogger(__name__)

# Install modes returned by OpenWrtUpdateEntity._install_plan
INSTALL_ASU = "asu"
INSTALL_STOCK = "stock"
INSTALL_CUSTOM = "custom"
INSTALL_NONE = "none"

# Package names the ASU server accepts (its request schema: ^[\w.,-]*$)
ASU_PACKAGE_NAME = re.compile(r"[\w.,-]+", re.ASCII)

MANUAL_UPGRADE_HINT = (
    "Upgrade by hand with owut or the LuCI Attended Sysupgrade page, which "
    "keep your installed packages."
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up update entities."""
    coordinator: OpenWrtDataCoordinator = hass.data[DOMAIN][entry.entry_id][
        DATA_COORDINATOR
    ]

    tracked_keys: set[str] = set()

    def _async_add_new_entities() -> None:
        if not coordinator.data:
            return

        new_entities: list[UpdateEntity] = []

        # 1. Add firmware update entity
        fw_key = f"{entry.entry_id}_firmware_update"
        check_enabled = entry.options.get(
            CONF_CHECK_FIRMWARE_UPDATES, DEFAULT_CHECK_FIRMWARE_UPDATES
        )
        if check_enabled and fw_key not in tracked_keys:
            perms = coordinator.data.permissions
            if perms.read_system:
                tracked_keys.add(fw_key)
                new_entities.append(OpenWrtUpdateEntity(coordinator, entry))

        # 2. Add package update entities
        for pkg_name, latest_ver in coordinator.data.upgradeable_packages.items():
            pkg_key = f"{entry.entry_id}_pkg_update_{pkg_name}"
            if pkg_key not in tracked_keys:
                tracked_keys.add(pkg_key)
                new_entities.append(
                    OpenWrtPackageUpdateEntity(coordinator, entry, pkg_name, latest_ver)
                )

        if new_entities:
            async_add_entities(new_entities)

    _async_add_new_entities()
    entry.async_on_unload(coordinator.async_add_listener(_async_add_new_entities))


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
        """Initialize."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_firmware_update"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, coordinator.router_id)},
        }
        self._in_progress = False

    @property
    def supported_features(self) -> UpdateEntityFeature:
        """Return supported features."""
        features = UpdateEntityFeature.RELEASE_NOTES
        data = self.coordinator.data
        if not data:
            return features

        mode, _ = self._install_plan(data)
        can_install = mode != INSTALL_NONE

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

        # If we determined the latest remote version is not newer than our
        # current system, we are up to date
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

        if data.asu_status:
            attrs["asu_status"] = data.asu_status
            attrs["asu_status_reason"] = data.asu_status_reason

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

        # Header and Checksum
        notes = self._get_release_notes_header(data, latest)

        # Device Info
        notes += (
            f"**Target:** `{data.device_info.target}`\n"
            f"**Board:** `{data.device_info.board_name}`\n\n"
        )

        # Backup info
        notes += self._get_release_notes_backup_info()

        # Installation info (if not possible)
        notes += self._get_release_notes_install_info(data, latest)

        return notes

    def _get_release_notes_header(self, data: Any, latest: str) -> str:
        """Get the header portion of release notes."""
        if data.is_custom_build:
            notes = f"## Custom Firmware: {latest}\n\n"
            if data.firmware_checksum:
                notes += f"**SHA256 Checksum:** `{data.firmware_checksum}`\n\n"
            if data.firmware_install_url:
                notes += f"**Firmware File:** [{data.firmware_install_url}]({data.firmware_install_url})\n\n"
            notes += (
                "This OpenWrt update is retrieved from your configured "
                "custom repository.\n\n"
            )
        else:
            notes = f"## OpenWrt {latest}\n\n"
            notes += "A new official OpenWrt release is available.\n\n"
            notes += (
                f"Visit the [OpenWrt release page]"
                f"(https://openwrt.org/releases/{latest}) for details.\n\n"
            )
        return notes

    def _get_release_notes_backup_info(self) -> str:
        """Get the backup information portion of release notes."""
        notes = "⚠️ **Always back up your configuration before upgrading!**\n\n"
        auto_backup = self.coordinator.config_entry.options.get(CONF_AUTO_BACKUP, True)
        if auto_backup:
            notes += (
                "🛡️ *Note: Automatic Backups are enabled in your integration "
                "options. A configuration backup will be automatically created "
                "and stored sequentially in your Home Assistant `.storage` "
                "folder before the update begins.*\n\n"
            )
        else:
            notes += (
                "*(Automatic Backup is currently disabled in your "
                "integration options)*\n\n"
            )
        return notes

    def _get_release_notes_install_info(self, data: Any, latest: str) -> str:
        """Get the installation information portion of release notes."""
        can_install, install_barrier = self._check_installability(data)
        if can_install:
            return ""

        notes = "---\n\n"
        notes += "### ⚠️ Automatic Installation Disabled\n"
        notes += f"{install_barrier}\n\n"

        # Inject dynamic Firmware Selector URL
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

        fs_url = (
            f"https://firmware-selector.openwrt.org/?version={version_param}"
            f"&target={target}&id={board}"
        )
        notes += (
            f"**🚀 Official Downloads:**\n[Firmware Selector ({latest})]({fs_url})\n\n"
        )

        if data.firmware_install_url:
            notes += (
                f"**Direct Link:**\n"
                f"[Download Firmware Image globally]({data.firmware_install_url})\n"
            )
        elif data.firmware_release_url:
            notes += (
                f"**Repository / Changelog:**\n"
                f"[View Release and Source]({data.firmware_release_url})\n"
            )

        return notes

    def _check_installability(self, data: Any) -> tuple[bool, str]:
        """Check if firmware can be installed automatically."""
        mode, reason = self._install_plan(data)
        return mode != INSTALL_NONE, reason

    def _install_plan(self, data: Any) -> tuple[str, str]:
        """Decide how this update would be installed, or why it cannot be.

        The plain downloads.openwrt.org image only contains the default
        packages, so flashing it removes everything the user added. It is
        therefore never used when ASU can (or soon will) build an image for
        this router; that case is ASU or nothing. When ASU does not support
        the board, the stock image is still subject to the package check in
        async_install.
        """
        if data.is_custom_build:
            if data.firmware_install_url:
                return INSTALL_CUSTOM, ""
            return INSTALL_NONE, (
                "No matching firmware image was found in the configured "
                "custom repository."
            )

        status = data.asu_status
        if status == ASU_STATUS_AVAILABLE:
            if not data.asu_update_available:
                return INSTALL_NONE, (
                    "Attended Sysupgrade (ASU) supports this device but offers "
                    "no newer build for it. The plain OpenWrt image is not used "
                    "on ASU-capable devices because it would remove every "
                    "package you installed. " + MANUAL_UPGRADE_HINT
                )
            conn_type = self.coordinator.config_entry.data.get(CONF_CONNECTION_TYPE)
            if (
                conn_type in (CONNECTION_TYPE_LUCI_RPC, CONNECTION_TYPE_UBUS)
                and not data.packages.asu
            ):
                return INSTALL_NONE, (
                    "The `luci-app-attendedsysupgrade` package is not "
                    "installed on the router."
                )
            return INSTALL_ASU, ""

        if status == ASU_STATUS_TEMPORARILY_UNAVAILABLE:
            return INSTALL_NONE, (
                f"Attended Sysupgrade (ASU) is temporarily unavailable: "
                f"{data.asu_status_reason}. Installation stays disabled rather "
                f"than falling back to the plain OpenWrt image, which would "
                f"remove every package you installed. Try again later. "
                + MANUAL_UPGRADE_HINT
            )

        if data.firmware_install_url:
            return INSTALL_STOCK, ""
        return INSTALL_NONE, (
            "No direct installation URL is available for this update or "
            "your device does not support Attended Sysupgrade (ASU)."
        )

    async def async_install(
        self,
        version: str | None,
        backup: bool,  # noqa: FBT001
        **kwargs: Any,
    ) -> None:
        """Install the latest firmware version."""
        if self._in_progress:
            _LOGGER.warning("Firmware update is already in progress.")
            return

        self._in_progress = True
        try:
            data = self.coordinator.data
            if not data:
                msg = "No data available to process firmware update."
                raise HomeAssistantError(msg)

            mode, reason = self._install_plan(data)
            if mode == INSTALL_NONE:
                raise HomeAssistantError(reason)

            packages: list[str] = []
            if mode == INSTALL_ASU:
                packages = await self._async_asu_packages()

            if mode == INSTALL_STOCK:
                extra = await self.coordinator.async_get_non_default_packages(data)
                if extra is None:
                    msg = (
                        "Refusing to flash the plain OpenWrt image: could not "
                        "confirm that the router has no packages beyond the "
                        "stock image, and flashing it would remove them. "
                        + MANUAL_UPGRADE_HINT
                    )
                    raise HomeAssistantError(msg)
                if extra:
                    shown = ", ".join(extra[:15])
                    if len(extra) > 15:
                        shown += f" and {len(extra) - 15} more"
                    msg = (
                        "Refusing to flash the plain OpenWrt image: it would "
                        f"remove these installed packages: {shown}. "
                        + MANUAL_UPGRADE_HINT
                    )
                    raise HomeAssistantError(msg)

            # Check for auto-backup option
            auto_backup = self.coordinator.config_entry.options.get(
                CONF_AUTO_BACKUP, True
            )
            if auto_backup:
                await self._async_perform_backup()

            if mode == INSTALL_ASU:
                await self._async_install_asu(data, packages)
                return

            # Stock (verified above) or custom repository image
            url = data.firmware_install_url
            _LOGGER.info("Initiating firmware installation from: %s", url)
            try:
                # Mark coordinator that a reboot/sysupgrade is expected
                self.coordinator.in_reboot_installation = True
                await self.coordinator.client.install_firmware(
                    url, keep_settings=True, force=data.is_custom_build
                )
            except Exception as err:
                msg = f"Failed to initiate firmware installation: {err}"
                raise HomeAssistantError(msg) from err
        finally:
            self._in_progress = False

    async def _async_asu_packages(self) -> list[str]:
        """Return the package list to send with an ASU build request.

        Like owut and the LuCI ASU page, send the explicitly installed set
        (/etc/apk/world, or opkg's user-installed packages). The full
        installed list is the fallback when that cannot be read. An empty
        list is refused: with diff_packages, ASU would build the stock set.
        """
        client = self.coordinator.client
        try:
            packages = await client.get_user_installed_packages()
            if packages is None:
                _LOGGER.debug(
                    "Explicitly installed packages unreadable; "
                    "using the full installed list for ASU"
                )
                packages = await client.get_installed_packages()
        except Exception as err:
            msg = f"Could not read the installed packages from the router: {err}"
            raise HomeAssistantError(msg) from err
        packages = [p for p in packages or [] if isinstance(p, str) and p]
        if not packages:
            msg = (
                "Could not read the installed packages from the router, "
                "so an ASU image would not include them. Aborting."
            )
            raise HomeAssistantError(msg)
        # ASU rejects (422) any name outside this pattern; say which one
        # instead of sending a request that fails.
        invalid = [p for p in packages if not ASU_PACKAGE_NAME.fullmatch(p)]
        if invalid:
            msg = (
                "Cannot request an ASU build: these installed package entries "
                f"are not valid package names: {', '.join(invalid)}. "
                + MANUAL_UPGRADE_HINT
            )
            raise HomeAssistantError(msg)
        return packages

    async def _async_install_asu(self, data: Any, packages: list[str]) -> None:
        """Request an ASU build with the router's packages and flash it."""
        version = data.asu_target_version or data.firmware_latest_version
        _LOGGER.info("Initiating ASU custom firmware build for %s", version)
        try:
            from .helpers.asu import AsuClient

            entry = self.coordinator.config_entry
            asu_url = entry.options.get(
                CONF_ASU_URL, entry.data.get(CONF_ASU_URL, DEFAULT_ASU_URL)
            )
            asu_client = AsuClient(self.hass, asu_url)

            request_hash = await asu_client.request_build(
                version=version,
                target=self.coordinator._get_target(data.device_info.target),
                board_name=data.asu_profile or data.device_info.board_name,
                packages=packages,
                client_name=(
                    f"Home Assistant OpenWrt Integration ({self.coordinator.name})"
                ),
            )

            _LOGGER.info(
                "ASU build requested (hash: %s). Waiting for image...",
                request_hash,
            )
            download_url = await asu_client.poll_build_status(request_hash)

            _LOGGER.info(
                "ASU build complete. Flashing image from: %s",
                download_url,
            )
            self.coordinator.in_reboot_installation = True
            await self.coordinator.client.install_firmware(
                download_url,
                keep_settings=True,
            )

        except Exception as err:
            _LOGGER.exception("ASU firmware update failed")
            msg = f"ASU firmware update failed: {err}"
            raise HomeAssistantError(msg) from err

    async def _async_perform_backup(self) -> None:
        """Perform a backup and download it to HA."""
        _LOGGER.info("Performing automatic backup before firmware update...")
        try:
            # Create backup on router
            remote_path = await self.coordinator.client.create_backup()
            if not remote_path:
                msg = "Failed to create backup on router"
                _LOGGER.error(msg)
                raise HomeAssistantError(msg)

            # Prepare local path
            from pathlib import Path

            backup_dir = Path(self.hass.config.path("backups", "openwrt"))
            await asyncio.to_thread(backup_dir.mkdir, parents=True, exist_ok=True)

            local_filename = Path(remote_path).name
            local_path = str(backup_dir / local_filename)

            # Download to HA
            success = await self.coordinator.client.download_file(
                remote_path,
                local_path,
            )
            if success:
                _LOGGER.info("Backup successfully saved to: %s", local_path)
                # Cleanup remote file
                await self.coordinator.client.execute_command(f"rm {remote_path}")
            else:
                msg = f"Failed to download backup from router to {local_path}"
                _LOGGER.error(msg)
                raise HomeAssistantError(msg)

        except Exception as err:
            _LOGGER.exception("Automatic backup failed")
            if isinstance(err, HomeAssistantError):
                raise
            msg = f"Automatic backup failed: {err}"
            raise HomeAssistantError(msg) from err


class OpenWrtPackageUpdateEntity(
    CoordinatorEntity[OpenWrtDataCoordinator], UpdateEntity
):
    """Representation of an OpenWrt package update."""

    _attr_has_entity_name = True
    _attr_entity_registry_enabled_default = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: OpenWrtDataCoordinator,
        entry: ConfigEntry,
        package_name: str,
        latest_version: str,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._package_name = package_name
        self._latest_version = latest_version
        self._attr_name = f"Package {package_name}"
        self._attr_unique_id = f"{entry.entry_id}_pkg_update_{package_name}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, coordinator.router_id)},
        }

    @property
    def supported_features(self) -> UpdateEntityFeature:
        """Return supported features."""
        return UpdateEntityFeature.INSTALL

    @property
    def installed_version(self) -> str | None:
        """Return the installed version."""
        return "outdated"

    @property
    def latest_version(self) -> str | None:
        """Return the latest version."""
        if self.coordinator.data is None:
            return self._latest_version
        return self.coordinator.data.upgradeable_packages.get(
            self._package_name, self._latest_version
        )

    async def async_install(
        self, version: str | None, backup: bool, **kwargs: Any
    ) -> None:
        """Install package update."""
        client = self.coordinator.client
        try:
            script = (
                f"if command -v apk >/dev/null 2>&1; then "
                f"  apk add --upgrade {self._package_name}; "
                f"elif command -v opkg >/dev/null 2>&1; then "
                f"  opkg install {self._package_name}; "
                f"fi"
            )
            await client.execute_command(script)
            await self.coordinator.async_request_refresh()
        except Exception as err:
            raise HomeAssistantError(
                f"Failed to install package {self._package_name}: {err}"
            ) from err
