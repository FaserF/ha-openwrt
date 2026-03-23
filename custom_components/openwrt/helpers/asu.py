"""ASU (Attended Sysupgrade) Client for OpenWrt integration."""

from __future__ import annotations

import asyncio
import logging

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)

DEFAULT_ASU_URL = "https://sysupgrade.openwrt.org"


class AsuClientError(Exception):
    """Base exception for ASU client errors."""


class AsuClient:
    """Client for the OpenWrt Attended Sysupgrade server."""

    def __init__(self, hass: HomeAssistant, base_url: str = DEFAULT_ASU_URL) -> None:
        """Initialize the ASU client."""
        self._hass = hass
        self._base_url = base_url.rstrip("/")
        self._session = async_get_clientsession(hass)

    async def _request(
        self, method: str, path: str, payload: dict | None = None, timeout: float = 60.0,
    ) -> dict:
        """Make an HTTP request to the ASU server."""
        url = f"{self._base_url}{path}"
        headers = {"Accept": "application/json"}

        try:
            async with self._session.request(
                method,
                url,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status not in (200, 202):
                    text = await resp.text()
                    msg = f"ASU API error ({resp.status}): {text}"
                    raise AsuClientError(msg)
                return await resp.json()
        except aiohttp.ClientError as err:
            msg = f"Connection to ASU failed: {err}"
            raise AsuClientError(msg) from err
        except TimeoutError as err:
            msg = "Request to ASU timed out"
            raise AsuClientError(msg) from err

    async def request_build(
        self,
        version: str,
        target: str,
        board_name: str,
        packages: list[str],
        client_name: str = "Home Assistant OpenWrt Integration",
        distribution: str = "openwrt",
    ) -> str:
        """Request a custom firmware build.

        Returns:
            The request_hash to poll for status.
        """
        payload = {
            "distro": distribution,
            "version": version,
            "target": target,
            "profile": board_name,
            "packages": packages,
            "diff_packages": True,
            "client": client_name,
        }

        _LOGGER.debug(
            "Requesting ASU build for %s (%s) with %d packages",
            board_name,
            version,
            len(packages),
        )

        resp = await self._request("POST", "/api/v1/build", payload)
        request_hash = resp.get("request_hash")
        if not request_hash:
            msg = "ASU response missing request_hash"
            raise AsuClientError(msg)

        return request_hash

    async def poll_build_status(
        self, request_hash: str, timeout: float = 600.0, step: float = 5.0,
    ) -> str:
        """Poll the ASU server until the build is ready.

        Args:
            request_hash: The hash from request_build.
            timeout: Maximum time to wait in seconds (builds can take 2-5 min).
            step: Seconds between polling attempts.

        Returns:
            The direct URL to download the sysupgrade.bin file.
        """
        path = f"/api/v1/build/{request_hash}"
        deadline = asyncio.get_running_loop().time() + timeout

        _LOGGER.debug("Polling ASU build status for %s", request_hash)

        while asyncio.get_running_loop().time() < deadline:
            resp = await self._request("GET", path)
            status = resp.get("detail", "").lower()

            # The ASU API usually returns something like "Building", "In Queue", "Done"
            if "done" in status or resp.get("status") == 200:
                bin_dir = resp.get("bin_dir")
                images = resp.get("images", [])

                sysupgrade_file = None
                for img in images:
                    if "sysupgrade" in img.get("name", ""):
                        sysupgrade_file = img.get("name")
                        break

                if not sysupgrade_file and images:
                    # Fallback to the first image if no sysupgrade specifically matched
                    sysupgrade_file = images[0].get("name")

                if bin_dir and sysupgrade_file:
                    url = f"{self._base_url}/store/{bin_dir}/{sysupgrade_file}"
                    _LOGGER.info("ASU build complete. Image URL: %s", url)
                    return url

                msg = f"Build marked as Done, but missing image info: {resp}"
                raise AsuClientError(
                    msg,
                )

            if "error" in status or resp.get("status") in (400, 500):
                msg = f"ASU build failed: {status}"
                raise AsuClientError(msg)

            _LOGGER.debug("ASU build status: %s", status)
            await asyncio.sleep(step)

        msg = f"Timeout waiting for ASU build ({timeout}s)"
        raise AsuClientError(msg)
