"""Firmware update checking mixin for OpenWrt coordinator."""

from __future__ import annotations

import contextlib
import logging
import re
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import aiohttp
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from ..api.base import OpenWrtData
from ..const import (
    CONF_ASU_URL,
    CONF_CUSTOM_FIRMWARE_REPO,
    CONF_TARGET_OVERRIDE,
    OPENWRT_RELEASE_API,
)
from .firmware_helpers import (
    SNAPSHOT_TARGET_MAP,
    build_sysupgrade_pattern,
    parse_repo,
    version_is_newer,
)

if TYPE_CHECKING:
    from .base import CoordinatorBase

    _Base = CoordinatorBase
else:
    _Base = object

_LOGGER = logging.getLogger(__name__)

FIRMWARE_CHECK_INTERVAL = timedelta(hours=6)


class FirmwareMixin(_Base):
    """Mixin for OpenWrt firmware update checking (official, snapshot, stable, ASU, custom)."""

    def _async_sync_firmware_state(self, data: OpenWrtData) -> None:
        """Sync firmware metadata."""
        if not data.device_info:
            return

        # Always initialize current version from device info
        data.firmware_current_version = (
            data.device_info.firmware_version or data.device_info.release_version
        )

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
                    profiles = profile_data.get("profiles", {})
                    board_name = info.board_name or ""

                    def normalize(name: str) -> str:
                        return (
                            name.lower()
                            .replace("-", "")
                            .replace("_", "")
                            .replace(",", "")
                        )

                    norm_board = normalize(board_name)
                    board_profile = None

                    for k in [
                        board_name,
                        board_name.replace("-", "_").replace(",", "_"),
                        board_name.replace("_", "-").replace(",", "-"),
                    ]:
                        if k in profiles:
                            board_profile = profiles[k]
                            break

                    if not board_profile:
                        for k, prof in profiles.items():
                            if normalize(k) == norm_board:
                                board_profile = prof
                                break

                    if board_profile:
                        for img in board_profile.get("images", []):
                            if "sysupgrade" in img.get("name", ""):
                                data.firmware_install_url = f"https://downloads.openwrt.org/releases/{latest_stable}/targets/{target}/{img.get('name')}"
                                break

        if not data.firmware_install_url:
            # Fallback to static URL construction
            board = info.board_name.replace("_", "-").replace(",", "-")
            dist = info.release_distribution or "openwrt"
            data.firmware_install_url = (
                f"https://downloads.openwrt.org/releases/{latest_stable}/targets/{target}/"
                f"{dist}-{latest_stable}-{target.replace('/', '-')}-{board}-squashfs-sysupgrade.bin"
            )

    async def _check_asu_update(self, data: OpenWrtData) -> None:
        """Check for updates via the ASU (Attended Sysupgrade) API."""
        distribution = (data.device_info.release_distribution or "").strip()
        if distribution and distribution.lower() not in ("openwrt", ""):
            return

        target = self._get_target(data.device_info.target)
        if not target or not data.device_info.board_name:
            return

        asu_url = self.config_entry.options.get(
            CONF_ASU_URL,
            self.config_entry.data.get(CONF_ASU_URL, "https://sysupgrade.openwrt.org"),
        )
        session = async_get_clientsession(self.hass)

        # 1. Fetch info from ASU
        asu_info = await self._fetch_asu_info(data, asu_url, session)
        if not asu_info:
            return

        # 2. Process findings
        data.asu_supported = True
        version = asu_info.get("version", "")
        revision = asu_info.get("revision", "")

        latest_version = version or revision
        if revision and ("SNAPSHOT" in version.upper() or not version):
            latest_version = f"{version or 'SNAPSHOT'} ({revision})"

        if not latest_version:
            return

        if self._version_is_newer(data.firmware_current_version or "", latest_version):
            data.asu_update_available = True
            await self._update_firmware_metadata_from_asu(data, latest_version)

    async def _fetch_asu_info(
        self, data: OpenWrtData, asu_url: str, session: aiohttp.ClientSession
    ) -> dict[str, Any] | None:
        """Fetch metadata from ASU API with model name variation fallback."""
        target = self._get_target(data.device_info.target)
        model = data.device_info.board_name
        is_snapshot = "SNAPSHOT" in data.device_info.release_version.upper()

        async def _do_fetch(m: str) -> dict[str, Any] | None:
            url = f"{asu_url.rstrip('/')}/api/v1/info?target={target}&model={m}"
            if is_snapshot:
                url += "&version=SNAPSHOT"
            with contextlib.suppress(Exception):
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    if resp.status == 404:
                        return {"status": 404}
            return None

        # Try primary model name
        res = await _do_fetch(model)
        if res and res.get("status") != 404:
            return res

        # Try fallback variation (comma to underscore) if first failed with 404
        if res and res.get("status") == 404 and "," in model:
            return await _do_fetch(model.replace(",", "_"))

        return None

    async def _update_firmware_metadata_from_asu(
        self, data: OpenWrtData, latest_version: str
    ) -> None:
        """Update coordinator data with findings from ASU."""
        # Ensure we have package list for future upgrade requests
        with contextlib.suppress(Exception):
            data.installed_packages = await self.client.get_installed_packages()

        if self._version_is_newer(
            data.firmware_latest_version or "0.0.0", latest_version
        ):
            data.firmware_latest_version = latest_version
            data.firmware_upgradable = True
            data.firmware_release_url = f"https://openwrt.org/releases/{latest_version}"
            data.firmware_install_url = ""  # Built on demand

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
            data.firmware_install_url = best_asset.get("browser_download_url")
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
            async with session.get(sha_url) as resp:
                if resp.status == 200:
                    content = await resp.text()
                    for line in content.splitlines():
                        if asset_name in line:
                            data.firmware_checksum = line.split()[0]
                            break

    _parse_repo = staticmethod(parse_repo)
    _build_sysupgrade_pattern = staticmethod(build_sysupgrade_pattern)
    _version_is_newer = staticmethod(version_is_newer)
