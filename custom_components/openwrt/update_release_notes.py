"""Release notes for the OpenWrt firmware update entity."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .const import CONF_AUTO_BACKUP

if TYPE_CHECKING:
    from .coordinator import OpenWrtDataCoordinator


class FirmwareReleaseNotesMixin:
    """Release notes for the OpenWrt firmware update entity."""

    if TYPE_CHECKING:
        coordinator: OpenWrtDataCoordinator

        def _check_installability(self, data: Any) -> tuple[bool, str]:
            """Check if firmware can be installed automatically (update entity)."""
            ...

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
