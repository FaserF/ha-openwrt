"""Firmware update checking mixin for OpenWrt coordinator."""

from __future__ import annotations

import contextlib
import dataclasses
import logging
import re
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import aiohttp
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from ..api.base import OpenWrtData
from ..const import (
    CONF_CUSTOM_FIRMWARE_REPO,
    CONF_TARGET_OVERRIDE,
    OPENWRT_RELEASE_API,
)
from .firmware_asu import AsuFirmwareMixin
from .firmware_helpers import (
    SNAPSHOT_TARGET_MAP,
    build_sysupgrade_pattern,
    find_board_profile,
    parse_repo,
)

if TYPE_CHECKING:
    from .base import CoordinatorBase

    _Base = CoordinatorBase
else:
    _Base = object

_LOGGER = logging.getLogger(__name__)

FIRMWARE_CHECK_INTERVAL = timedelta(hours=6)
# Delay before the first check after setup (see OpenWrtDataCoordinator.__init__)
FIRMWARE_FIRST_CHECK_DELAY = timedelta(minutes=2)
# Delay before re-checking firmware after the router's firmware changed
FIRMWARE_RECHECK_DELAY = timedelta(minutes=2)


class FirmwareMixin(AsuFirmwareMixin):
    """Mixin for OpenWrt firmware update checking (official, snapshot, stable, ASU, custom)."""

    def _firmware_check_due(self, now: float) -> bool:
        """Return True when the periodic firmware check should run."""
        return now - self._last_firmware_check > FIRMWARE_CHECK_INTERVAL.total_seconds()

    async def _async_refresh_device_info_after_reboot(self, data: OpenWrtData) -> None:
        """Re-read device info when the router rebooted or was just flashed.

        Device info is otherwise only refreshed on slow polls (every 15 min),
        so the installed firmware version would stay stale after an upgrade.
        """
        uptime = data.system_resources.uptime
        last = getattr(self, "_last_uptime", None)
        rebooted = last is not None and 0 < uptime < last - 10
        pending = getattr(self, "_device_info_refresh_pending", False)
        if not (rebooted or pending):
            return
        self._device_info_refresh_pending = False
        try:
            info = await self.client.async_refresh_device_info()
        except Exception as err:
            # Keep it pending, so the next poll retries instead of showing the
            # cached (pre-upgrade) device info until the next slow poll.
            _LOGGER.debug("Could not refresh device info after reboot: %s", err)
            self._device_info_refresh_pending = True
            return
        if info:
            data.device_info = info

    @staticmethod
    def _reset_firmware_state(data: OpenWrtData) -> None:
        """Clear every firmware/ASU update field (all but the current version)."""
        fresh = OpenWrtData()
        for field in dataclasses.fields(OpenWrtData):
            name = field.name
            if name == "firmware_current_version":
                continue
            if name.startswith(("firmware_", "asu_")) or name in (
                "installed_packages",
                "is_custom_build",
            ):
                setattr(data, name, getattr(fresh, name))

    def _async_sync_firmware_state(self, data: OpenWrtData) -> None:
        """Sync firmware metadata."""
        if not data.device_info:
            return

        # Always initialize current version from device info
        data.firmware_current_version = (
            data.device_info.firmware_version or data.device_info.release_version
        )

        # The running firmware changed (an upgrade, or a downgrade by hand):
        # what the last check found no longer applies, so drop it instead of
        # offering the update that was just installed, and check again soon.
        revision = data.device_info.release_revision or ""
        previous = getattr(self, "_firmware_revision", None)
        self._firmware_revision = revision
        if previous and revision and previous != revision:
            _LOGGER.info(
                "Firmware revision changed from %s to %s; re-checking for updates",
                previous,
                revision,
            )
            self._reset_firmware_state(data)
            now = self.hass.loop.time()
            self._last_firmware_check = (
                now
                - FIRMWARE_CHECK_INTERVAL.total_seconds()
                + FIRMWARE_RECHECK_DELAY.total_seconds()
            )
            return

        if (
            self.data
            and self.data.device_info.release_revision
            == data.device_info.release_revision
        ):
            # Preserve previously discovered current version if it was set
            if self.data.firmware_current_version:
                data.firmware_current_version = self.data.firmware_current_version

            data.firmware_latest_version = self.data.firmware_latest_version
            data.firmware_upgradable = self.data.firmware_upgradable
            data.firmware_release_url = self.data.firmware_release_url
            data.firmware_install_url = self.data.firmware_install_url
            data.firmware_checksum = self.data.firmware_checksum
            data.is_custom_build = self.data.is_custom_build
            data.asu_supported = self.data.asu_supported
            data.asu_update_available = self.data.asu_update_available
            data.asu_status = self.data.asu_status
            data.asu_status_reason = self.data.asu_status_reason
            data.asu_target_version = self.data.asu_target_version
            data.asu_profile = self.data.asu_profile
            data.asu_image_status = self.data.asu_image_status
            data.asu_image_url = self.data.asu_image_url
            data.installed_packages = self.data.installed_packages

    async def _check_firmware_update(self, data: OpenWrtData) -> None:
        """Check for firmware updates (official or custom)."""
        custom_repo = self.config_entry.options.get(
            CONF_CUSTOM_FIRMWARE_REPO,
            self.config_entry.data.get(CONF_CUSTOM_FIRMWARE_REPO, ""),
        )
        if custom_repo:
            await self._check_custom_firmware_update(data, custom_repo)
        else:
            await self._check_official_firmware_update(data)
            await self._check_asu_update(data)

    async def _check_official_firmware_update(self, data: OpenWrtData) -> None:
        """Check for firmware updates from the OpenWrt release API."""
        # Skip update checks for non-OpenWrt firmware distributions (e.g. RUTOS, DDWRT).
        # Promoting a cross-firmware upgrade could be harmful or confusing.
        distribution = (data.device_info.release_distribution or "").strip()
        if distribution and distribution.lower() not in ("openwrt", ""):
            _LOGGER.debug(
                "Skipping official firmware update check for non-OpenWrt distribution: %s",
                distribution,
            )
            return

        current_version = data.device_info.release_version
        session = async_get_clientsession(self.hass)

        if "SNAPSHOT" in current_version.upper():
            await self._check_snapshot_update(data, session)
        else:
            await self._check_stable_release_update(data, session)

    def _get_target(self, target: str) -> str:
        """Apply target migrations/mappings if needed."""
        override = self.config_entry.options.get(CONF_TARGET_OVERRIDE)
        if override:
            return override
        return SNAPSHOT_TARGET_MAP.get(target, target)

    async def _check_snapshot_update(
        self, data: OpenWrtData, session: aiohttp.ClientSession
    ) -> None:
        """Check for updates in SNAPSHOT builds."""
        target = self._get_target(data.device_info.target)
        _LOGGER.info(
            "Checking snapshot update for target: %s (original: %s)",
            target,
            data.device_info.target,
        )
        if not target:
            return

        import re

        current_version = data.device_info.release_version or ""
        match = re.search(r"(\d+\.\d+)-SNAPSHOT", current_version, re.IGNORECASE)
        if match:
            branch = f"{match.group(1)}-SNAPSHOT"
            base_url = (
                f"https://downloads.openwrt.org/releases/{branch}/targets/{target}/"
            )
        else:
            base_url = f"https://downloads.openwrt.org/snapshots/targets/{target}/"

        url = f"{base_url}profiles.json"

        with contextlib.suppress(Exception):
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                _LOGGER.info(
                    "Snapshot profiles.json status for %s: %s", target, resp.status
                )
                if resp.status != 200:
                    return
                profile_data = await resp.json()
                version_code = profile_data.get("version_code", "")
                if not version_code:
                    return

                if match:
                    latest_snapshot = f"{branch} ({version_code})"
                else:
                    latest_snapshot = f"SNAPSHOT ({version_code})"

                _LOGGER.info(
                    "Comparing snapshot versions: current=%s, latest=%s",
                    data.firmware_current_version,
                    latest_snapshot,
                )
                data.firmware_latest_version = latest_snapshot
                if self._version_is_newer(
                    data.firmware_current_version, latest_snapshot
                ):
                    data.firmware_upgradable = True
                    _LOGGER.info(
                        "Newer snapshot found for %s: %s", target, latest_snapshot
                    )
                    data.firmware_release_url = base_url
                else:
                    data.firmware_upgradable = False
                    _LOGGER.debug("Snapshot is up-to-date: %s", latest_snapshot)

                # Find sysupgrade image
                profiles = profile_data.get("profiles", {})
                board_name = data.device_info.board_name or ""
                board_key = board_name.replace("-", "_").replace(",", "_")
                board_profile = profiles.get(board_key)
                if board_profile:
                    for img in board_profile.get("images", []):
                        if "sysupgrade" in img.get("name", ""):
                            data.firmware_install_url = f"{base_url}{img.get('name')}"
                            data.firmware_checksum = img.get("sha256", "")
                            break

    async def _check_stable_release_update(
        self, data: OpenWrtData, session: aiohttp.ClientSession
    ) -> None:
        """Check for updates in stable releases."""
        with contextlib.suppress(Exception):
            async with session.get(
                OPENWRT_RELEASE_API, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status != 200:
                    return
                versions_data = await resp.json()
                latest_stable = versions_data.get(
                    "stable_version", versions_data.get("latest", "")
                )

                if not latest_stable and isinstance(versions_data, dict):
                    for key in sorted(versions_data.keys(), reverse=True):
                        if not key.startswith(".") and not key.startswith("_"):
                            latest_stable = key
                            break

                if latest_stable:
                    data.firmware_latest_version = latest_stable
                    if self._version_is_newer(
                        data.device_info.release_version, latest_stable
                    ):
                        data.firmware_upgradable = True
                        await self._async_set_stable_release_urls(
                            data, latest_stable, session
                        )
                    else:
                        data.firmware_upgradable = False

    async def _async_set_stable_release_urls(
        self, data: OpenWrtData, latest_stable: str, session: aiohttp.ClientSession
    ) -> None:
        """Determine release and install URLs for a stable release."""
        data.firmware_release_url = f"https://openwrt.org/releases/{latest_stable}"
        info = data.device_info
        target = self._get_target(info.target)
        if not target or not info.board_name:
            return

        # Try to fetch profiles.json to get exact sysupgrade file name (extension, spelling)
        url = f"https://downloads.openwrt.org/releases/{latest_stable}/targets/{target}/profiles.json"

        with contextlib.suppress(Exception):
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == 200:
                    profile_data = await resp.json()
                    board_profile = find_board_profile(
                        profile_data.get("profiles", {}), info.board_name or ""
                    )

                    if board_profile:
                        for img in board_profile.get("images", []):
                            if "sysupgrade" in img.get("name", ""):
                                data.firmware_install_url = f"https://downloads.openwrt.org/releases/{latest_stable}/targets/{target}/{img.get('name')}"
                                data.firmware_checksum = img.get("sha256", "")
                                break

        if not data.firmware_install_url:
            # Fallback to static URL construction. The file name is guessed,
            # so there is no published checksum to verify it against.
            data.firmware_checksum = ""
            board = info.board_name.replace("_", "-").replace(",", "-")
            dist = info.release_distribution or "openwrt"
            data.firmware_install_url = (
                f"https://downloads.openwrt.org/releases/{latest_stable}/targets/{target}/"
                f"{dist}-{latest_stable}-{target.replace('/', '-')}-{board}-squashfs-sysupgrade.bin"
            )

    async def _check_custom_firmware_update(
        self,
        data: OpenWrtData,
        repo_input: str,
    ) -> None:
        """Check for firmware updates from a custom GitHub repository."""
        data.is_custom_build = True
        owner, repo = self._parse_repo(repo_input)
        if not owner or not repo:
            return

        router_hash = self._get_router_hash(data)
        _LOGGER.debug(
            "Checking custom firmware for %s/%s (router hash: %s)",
            owner,
            repo,
            router_hash,
        )

        session = async_get_clientsession(self.hass)
        headers = {"Accept": "application/vnd.github+json"}

        # 1. Get releases
        with contextlib.suppress(Exception):
            url = f"https://api.github.com/repos/{owner}/{repo}/releases"
            async with session.get(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    return
                releases = await resp.json()
                if not releases:
                    return

            # 2. Try to identify current version by commit hash if unknown
            if router_hash:
                tag_name = await self._find_tag_by_hash(
                    owner, repo, router_hash, headers, session
                )
                if tag_name:
                    data.firmware_current_version = tag_name

            # 3. Find the latest release that contains a matching sysupgrade image for this router
            matching_release = None
            for release in releases:
                if self._release_has_matching_asset(data, release):
                    matching_release = release
                    break

            # Fallback to the first release if no specific asset match is found
            target_release = matching_release or releases[0]

            latest_tag = target_release.get("tag_name", "")
            latest_version = self._get_latest_version_string(target_release)

            data.firmware_latest_version = latest_version
            data.firmware_release_url = target_release.get("html_url", "")

            # 4. Check if upgradable
            if (
                data.firmware_current_version == latest_tag
                or data.firmware_current_version == target_release.get("name")
            ):
                is_upgradable = False
            else:
                is_upgradable = self._version_is_newer(
                    data.firmware_current_version or "", latest_tag
                )
                if not is_upgradable and latest_version != latest_tag:
                    is_upgradable = self._version_is_newer(
                        data.firmware_current_version or "", latest_version
                    )
            data.firmware_upgradable = is_upgradable

            # 5. Find sysupgrade image and checksum from the target release
            await self._process_custom_release_assets(data, target_release, session)

    def _get_router_hash(self, data: OpenWrtData) -> str:
        """Extract commit hash from revision string."""
        revision = data.device_info.release_revision
        if revision and "-" in revision:
            return revision.split("-")[-1].strip()
        return ""

    async def _find_tag_by_hash(
        self,
        owner: str,
        repo: str,
        router_hash: str,
        headers: dict,
        session: aiohttp.ClientSession,
    ) -> str | None:
        """Find a GitHub tag that matches the router's commit hash."""
        with contextlib.suppress(Exception):
            url = f"https://api.github.com/repos/{owner}/{repo}/tags"
            async with session.get(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == 200:
                    tags = await resp.json()
                    for tag in tags:
                        sha = tag.get("commit", {}).get("sha", "")
                        if sha.startswith(router_hash):
                            return str(tag.get("name"))
        return None

    def _get_latest_version_string(self, release: dict[str, Any]) -> str:
        """Format the latest version string from release info."""
        tag = release.get("tag_name", "")
        if "SNAPSHOT" not in tag.upper():
            return tag

        published = release.get("published_at", "")
        commit = release.get("target_commitish", "")
        if commit and len(commit) >= 7:
            return f"{tag} ({commit[:7]})"
        if published:
            return f"{tag} ({published.split('T')[0]})"
        return tag

    def _release_has_matching_asset(
        self, data: OpenWrtData, release: dict[str, Any]
    ) -> bool:
        """Check if a release contains a sysupgrade asset matching the router."""
        assets = release.get("assets", [])
        pattern = self._build_sysupgrade_pattern(data)
        board_name = data.device_info.board_name or ""
        board = board_name.replace(",", "_").replace(" ", "_")

        for asset in assets:
            name = asset.get("name", "")
            if pattern and re.match(pattern, name, re.IGNORECASE):
                return True
            if board and board in name and "sysupgrade" in name:
                return True

        return False

    async def _process_custom_release_assets(
        self, data: OpenWrtData, release: dict[str, Any], session: aiohttp.ClientSession
    ) -> None:
        """Find the best sysupgrade asset and its checksum from release."""
        assets = release.get("assets", [])
        pattern = self._build_sysupgrade_pattern(data)
        best_asset = None
        sha_url = None

        for asset in assets:
            name = asset.get("name", "")
            if "sha256sum" in name.lower() or name == "sha256sums":
                sha_url = asset.get("browser_download_url")
            if pattern and re.match(pattern, name, re.IGNORECASE):
                best_asset = asset

        if not best_asset:
            board_name = data.device_info.board_name or ""
            board = board_name.replace(",", "_").replace(" ", "_")
            for asset in assets:
                if board in asset.get("name", "") and "sysupgrade" in asset.get(
                    "name", ""
                ):
                    best_asset = asset
                    break

        if best_asset:
            new_url = best_asset.get("browser_download_url")
            if new_url != data.firmware_install_url:
                # The checksum carried over from the last poll belongs to the
                # previous image; keep it only while the URL is unchanged.
                data.firmware_checksum = ""
            data.firmware_install_url = new_url
            if sha_url:
                await self._fetch_custom_checksum(
                    data, sha_url, best_asset.get("name", ""), session
                )

    async def _fetch_custom_checksum(
        self,
        data: OpenWrtData,
        sha_url: str,
        asset_name: str,
        session: aiohttp.ClientSession,
    ) -> None:
        """Fetch and parse checksum file from GitHub."""
        with contextlib.suppress(Exception):
            async with session.get(
                sha_url, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == 200:
                    content = await resp.text()
                    for line in content.splitlines():
                        # "<sha256>  <name>" or "<sha256> *<name>" (binary mode).
                        # Match the file name exactly: a substring match would
                        # also hit e.g. "<name>.sig" or a longer file name.
                        parts = line.split()
                        if len(parts) >= 2 and parts[-1].lstrip("*") == asset_name:
                            data.firmware_checksum = parts[0]
                            break

    _parse_repo = staticmethod(parse_repo)
    _build_sysupgrade_pattern = staticmethod(build_sysupgrade_pattern)
