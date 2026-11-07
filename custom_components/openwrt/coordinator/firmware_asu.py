"""Attended sysupgrade (ASU) detection mixin for the OpenWrt coordinator."""

from __future__ import annotations

import logging
import re
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
    DEFAULT_ASU_URL,
)
from .firmware_helpers import find_board_profile, version_is_newer

if TYPE_CHECKING:
    from .base import CoordinatorBase

    _Base = CoordinatorBase
else:
    _Base = object

_LOGGER = logging.getLogger(__name__)


class AsuFirmwareMixin(_Base):
    """Attended sysupgrade (ASU) detection mixin for the OpenWrt coordinator."""

    _version_is_newer = staticmethod(version_is_newer)

    if TYPE_CHECKING:

        def _get_target(self, target: str) -> str:
            """Apply target migrations/mappings (FirmwareMixin)."""
            ...

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
        # apk world pins the kernel ("kernel=<version>"); every image has one.
        stock.add("kernel")
        if not is_snapshot:
            # Release images ship LuCI although profiles.json does not list it.
            stock.add("luci")

        return sorted(pkg for pkg in user_packages if pkg not in stock)
