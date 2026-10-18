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
    ASU_STATUS_AVAILABLE,
    ASU_STATUS_ERROR,
    ASU_STATUS_TEMPORARILY_UNAVAILABLE,
    ASU_STATUS_UNSUPPORTED,
    CONF_ASU_URL,
    CONF_CUSTOM_FIRMWARE_REPO,
    CONF_TARGET_OVERRIDE,
    DEFAULT_ASU_URL,
    OPENWRT_RELEASE_API,
)
from .firmware_helpers import (
    SNAPSHOT_TARGET_MAP,
    build_sysupgrade_pattern,
    find_board_profile,
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
                                break

        if not data.firmware_install_url:
            # Fallback to static URL construction
            board = info.board_name.replace("_", "-").replace(",", "-")
            dist = info.release_distribution or "openwrt"
            data.firmware_install_url = (
                f"https://downloads.openwrt.org/releases/{latest_stable}/targets/{target}/"
                f"{dist}-{latest_stable}-{target.replace('/', '-')}-{board}-squashfs-sysupgrade.bin"
            )

    def _asu_url(self) -> str:
        """Return the configured ASU server URL."""
        return str(
            self.config_entry.options.get(
                CONF_ASU_URL,
                self.config_entry.data.get(CONF_ASU_URL, DEFAULT_ASU_URL),
            )
        ).rstrip("/")

    def _asu_target_version(self, data: OpenWrtData, latest: list[str]) -> str:
        """Pick the version to ask ASU for.

        SNAPSHOT routers stay on their snapshot line. Release routers use the
        version the official check reported as latest, so ASU and the
        entity's latest_version always talk about the same release.
        """
        current = data.device_info.release_version or ""
        if "SNAPSHOT" in current.upper():
            match = re.search(r"(\d+\.\d+)-SNAPSHOT", current, re.IGNORECASE)
            return f"{match.group(1)}-SNAPSHOT" if match else "SNAPSHOT"
        reported = data.firmware_latest_version or ""
        if reported and "SNAPSHOT" not in reported.upper() and "(" not in reported:
            return reported
        return latest[0] if latest else ""

    @staticmethod
    def _asu_profile_candidates(target: str, board_name: str) -> list[str]:
        """ASU profile names to try for a board (ASU uses '_' where boards use ',')."""
        candidates = [board_name.replace(",", "_")]
        if board_name not in candidates:
            candidates.append(board_name)
        if target.split("/")[0] in ("x86", "armsr"):
            candidates.append("generic")
        return candidates

    @staticmethod
    def _set_asu_status(data: OpenWrtData, status: str, reason: str) -> None:
        """Record the ASU check result."""
        data.asu_status = status
        data.asu_status_reason = reason
        data.asu_supported = status in (
            ASU_STATUS_AVAILABLE,
            ASU_STATUS_TEMPORARILY_UNAVAILABLE,
        )
        if status != ASU_STATUS_AVAILABLE:
            data.asu_update_available = False
        _LOGGER.debug("ASU status: %s (%s)", status, reason)

    async def _check_asu_update(self, data: OpenWrtData) -> None:
        """Check whether ASU can build an image for this router.

        Uses the server's static JSON API, as owut does: overview.json lists
        branches, their versions and the targets indexed so far, and
        {path}/targets/{target}/{profile}.json describes one board.
        """
        distribution = (data.device_info.release_distribution or "").strip()
        if distribution and distribution.lower() not in ("openwrt", ""):
            return

        target = self._get_target(data.device_info.target)
        board_name = data.device_info.board_name
        if not target or not board_name:
            return

        asu_url = self._asu_url()
        session = async_get_clientsession(self.hass)

        overview = await self._fetch_asu_json(
            session, f"{asu_url}/json/v1/overview.json"
        )
        if not isinstance(overview, dict) or not isinstance(
            overview.get("branches"), dict
        ):
            self._set_asu_status(
                data,
                ASU_STATUS_ERROR,
                f"Could not read the branch overview from {asu_url}",
            )
            return

        latest = [str(v) for v in overview.get("latest") or []]
        version = self._asu_target_version(data, latest)
        if not version:
            self._set_asu_status(
                data, ASU_STATUS_ERROR, "Could not determine the version to request"
            )
            return

        branch = self._find_asu_branch(overview["branches"], version)
        if branch is None:
            if latest and self._version_is_newer(latest[0], version):
                self._set_asu_status(
                    data,
                    ASU_STATUS_TEMPORARILY_UNAVAILABLE,
                    f"The ASU server does not list OpenWrt {version} yet",
                )
            else:
                self._set_asu_status(
                    data,
                    ASU_STATUS_UNSUPPORTED,
                    f"The ASU server does not offer OpenWrt {version}",
                )
            return

        branch_name = branch.get("name", "")
        if not branch.get("enabled", True):
            self._set_asu_status(
                data,
                ASU_STATUS_UNSUPPORTED,
                f"Branch {branch_name} is disabled on the ASU server",
            )
            return

        targets = branch.get("targets") or {}
        if not targets:
            # Seen right after a release: the branch is listed but no target
            # has been indexed yet. The board may well be supported.
            self._set_asu_status(
                data,
                ASU_STATUS_TEMPORARILY_UNAVAILABLE,
                f"The ASU server has not indexed any targets for {branch_name} yet",
            )
            return

        if target not in targets:
            self._set_asu_status(
                data,
                ASU_STATUS_UNSUPPORTED,
                f"Target {target} is not available on the ASU server for {branch_name}",
            )
            return

        path = str(branch.get("path", "releases/{version}")).replace(
            "{version}", version
        )
        profile_info: dict[str, Any] | None = None
        transient = False
        for profile in self._asu_profile_candidates(target, board_name):
            url = f"{asu_url}/json/v1/{path}/targets/{target}/{profile}.json"
            status, payload = await self._fetch_asu_json_status(session, url)
            if status == 200 and isinstance(payload, dict):
                profile_info = payload
                data.asu_profile = profile
                break
            if status != 404:
                transient = True

        if profile_info is None:
            if transient:
                self._set_asu_status(
                    data,
                    ASU_STATUS_TEMPORARILY_UNAVAILABLE,
                    f"The ASU server could not describe {board_name} for {version} right now",
                )
            else:
                self._set_asu_status(
                    data,
                    ASU_STATUS_UNSUPPORTED,
                    f"The ASU server has no profile for {board_name} on {target}",
                )
            return

        data.asu_target_version = version
        self._set_asu_status(
            data, ASU_STATUS_AVAILABLE, f"ASU can build {version} for this device"
        )

        latest_version = version
        version_code = str(profile_info.get("version_code", ""))
        if "SNAPSHOT" in version.upper() and version_code:
            latest_version = f"{version} ({version_code})"

        if self._version_is_newer(data.firmware_current_version or "", latest_version):
            data.asu_update_available = True
            await self._update_firmware_metadata_from_asu(data, latest_version)

    @staticmethod
    def _find_asu_branch(
        branches: dict[str, Any], version: str
    ) -> dict[str, Any] | None:
        """Return the overview branch whose versions include the given version."""
        for name, branch in branches.items():
            if not isinstance(branch, dict):
                continue
            if version in (branch.get("versions") or []):
                return {"name": name, **branch}
        return None

    async def _fetch_asu_json_status(
        self, session: aiohttp.ClientSession, url: str
    ) -> tuple[int, Any]:
        """GET a JSON document. Returns (HTTP status, body); status 0 on errors."""
        try:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status != 200:
                    return resp.status, None
                return 200, await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            _LOGGER.debug("ASU request to %s failed: %s", url, err)
            return 0, None

    async def _fetch_asu_json(self, session: aiohttp.ClientSession, url: str) -> Any:
        """GET a JSON document, or None on any failure."""
        status, payload = await self._fetch_asu_json_status(session, url)
        return payload if status == 200 else None

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

    async def async_get_non_default_packages(
        self, data: OpenWrtData
    ) -> list[str] | None:
        """List explicitly installed packages that the stock image does not contain.

        Compares the router's explicitly requested packages with the default
        and device packages of the stock image for the version it runs now.
        Flashing a stock image would remove every package in the result.
        Returns None when either side cannot be determined.
        """
        user_packages = await self.client.get_user_installed_packages()
        if user_packages is None:
            return None

        target = self._get_target(data.device_info.target)
        board_name = data.device_info.board_name or ""
        current = data.device_info.release_version or ""
        if not target or not board_name or not current:
            return None

        is_snapshot = "SNAPSHOT" in current.upper()
        if is_snapshot:
            match = re.search(r"(\d+\.\d+)-SNAPSHOT", current, re.IGNORECASE)
            base = (
                f"https://downloads.openwrt.org/releases/{match.group(1)}-SNAPSHOT"
                if match
                else "https://downloads.openwrt.org/snapshots"
            )
        else:
            base = f"https://downloads.openwrt.org/releases/{current}"

        session = async_get_clientsession(self.hass)
        profiles_json = await self._fetch_asu_json(
            session, f"{base}/targets/{target}/profiles.json"
        )
        if not isinstance(profiles_json, dict):
            return None
        board_profile = find_board_profile(
            profiles_json.get("profiles") or {}, board_name
        )
        if board_profile is None:
            return None

        stock: set[str] = set()
        for pkg in [
            *(profiles_json.get("default_packages") or []),
            *(board_profile.get("device_packages") or []),
        ]:
            if pkg.startswith("-"):
                stock.discard(pkg[1:])
            else:
                stock.add(pkg)
        if not is_snapshot:
            # Release images ship LuCI although profiles.json does not list it.
            stock.add("luci")

        return sorted(pkg for pkg in user_packages if pkg not in stock)

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
