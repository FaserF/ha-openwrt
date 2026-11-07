"""ASU (Attended Sysupgrade) Client for OpenWrt integration."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any, NamedTuple

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)

DEFAULT_ASU_URL = "https://sysupgrade.openwrt.org"


def _integration_version() -> str:
    """Return the numeric part of this integration's manifest version."""
    try:
        manifest = Path(__file__).resolve().parent.parent / "manifest.json"
        version = str(json.loads(manifest.read_text(encoding="utf-8"))["version"])
    except (OSError, ValueError, KeyError) as err:
        _LOGGER.debug("Could not read the integration version: %s", err)
        return "0"
    match = re.match(r"\d+(?:\.\d+)*", version)
    return match.group(0) if match else "0"


# Sent as the ASU "client" field. The server expects "<name>/<version>"
# (owut sends e.g. "owut/2026.04.09") and answers 500 to other formats.
# It goes to a public server, so it never carries router or entry names.
# Read once at import: update.py imports this module at the top, and Home
# Assistant imports platform modules outside the event loop.
ASU_CLIENT_NAME = f"ha-openwrt/{_integration_version()}"


class AsuClientError(Exception):
    """Base exception for ASU client errors."""


class AsuImage(NamedTuple):
    """A built image ready to flash."""

    url: str
    sha256: str


def select_sysupgrade_image(images: list[dict[str, Any]]) -> dict[str, Any]:
    """Pick the sysupgrade image from an ASU build's image list.

    Entries look like profiles.json's: name, type, filesystem, sha256,
    sha256_unsigned. Only an image whose type is "sysupgrade" is accepted,
    since sysupgrade must never be handed a factory or kernel image. When
    several filesystems are built, squashfs (the OpenWrt default) wins.
    """
    candidates = [
        img
        for img in images
        if isinstance(img, dict) and img.get("type") == "sysupgrade" and img.get("name")
    ]
    if not candidates:
        found = [
            (img.get("type"), img.get("name"))
            for img in images
            if isinstance(img, dict)
        ]
        msg = f"ASU build contains no sysupgrade image (found: {found})"
        raise AsuClientError(msg)
    squashfs = [img for img in candidates if img.get("filesystem") == "squashfs"]
    image = (squashfs or candidates)[0]
    if not re.fullmatch(r"[0-9a-fA-F]{64}", str(image.get("sha256", ""))):
        msg = f"ASU image {image['name']} has no valid sha256; refusing to flash it"
        raise AsuClientError(msg)
    return image


class AsuClient:
    """Client for the OpenWrt Attended Sysupgrade server."""

    def __init__(self, hass: HomeAssistant, base_url: str = DEFAULT_ASU_URL) -> None:
        """Initialize the ASU client."""
        self._hass = hass
        self._base_url = base_url.rstrip("/")
        self._session = async_get_clientsession(hass)

    async def _request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        timeout: float = 60.0,
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
        client_name: str = ASU_CLIENT_NAME,
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
        self,
        request_hash: str,
        timeout: float = 600.0,
        step: float = 5.0,
    ) -> AsuImage:
        """Poll the ASU server until the build is ready.

        Args:
            request_hash: The hash from request_build.
            timeout: Maximum time to wait in seconds (builds can take 2-5 min).
            step: Seconds between polling attempts.

        Returns:
            The sysupgrade image's download URL and published sha256.
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
                image = select_sysupgrade_image(resp.get("images", []))
                if not bin_dir:
                    msg = f"Build marked as Done, but missing bin_dir: {resp}"
                    raise AsuClientError(msg)

                url = f"{self._base_url}/store/{bin_dir}/{image['name']}"
                _LOGGER.info("ASU build complete. Image URL: %s", url)
                return AsuImage(url=url, sha256=image["sha256"])

            if "error" in status or resp.get("status") in (400, 500):
                msg = f"ASU build failed: {status}"
                raise AsuClientError(msg)

            _LOGGER.debug("ASU build status: %s", status)
            await asyncio.sleep(step)

        msg = f"Timeout waiting for ASU build ({timeout}s)"
        raise AsuClientError(msg)
