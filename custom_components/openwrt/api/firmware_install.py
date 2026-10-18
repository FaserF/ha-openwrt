"""Firmware download, verification and sysupgrade helpers shared by all clients."""

from __future__ import annotations

import logging
import re
import shlex
from typing import Any

_LOGGER = logging.getLogger(__name__)

FIRMWARE_TMP_PATH = "/tmp/firmware.bin"  # noqa: S108
FIRMWARE_DOWNLOAD_FAILED = "__OPENWRT_HA_FW_DOWNLOAD_FAILED__"
FIRMWARE_CHECKSUM_MISMATCH = "__OPENWRT_HA_FW_CHECKSUM_MISMATCH__"


class FirmwareInstallError(Exception):
    """The firmware image was not flashed (download or checksum failure)."""


def build_sysupgrade_command(
    url: str, keep_settings: bool = True, force: bool = False, sha256: str = ""
) -> str:
    """Build the shell command that downloads, verifies and flashes an image.

    TLS certificates are verified. When a sha256 is given, the image is
    checked before sysupgrade runs and deleted on mismatch, and sysupgrade is
    never started. Failures print a marker that check_sysupgrade_output
    turns into an exception.
    """
    if sha256 and not re.fullmatch(r"[0-9a-fA-F]{64}", sha256):
        msg = f"Invalid SHA256 checksum: {sha256!r}"
        raise FirmwareInstallError(msg)

    path = FIRMWARE_TMP_PATH
    safe_url = shlex.quote(url)
    download = (
        f"if command -v uclient-fetch >/dev/null 2>&1; then "
        f"uclient-fetch -O {path} {safe_url}; "
        f"elif command -v curl >/dev/null 2>&1; then "
        f"curl -fL -o {path} {safe_url}; "
        f"else "
        f"wget -O {path} {safe_url}; "
        f"fi"
    )
    steps = [
        f"rm -f {path}",
        f"{{ {download}; }} || {{ echo {FIRMWARE_DOWNLOAD_FAILED}; false; }}",
    ]
    if sha256:
        steps.append(
            f'{{ [ "$(sha256sum {path} | cut -d " " -f 1)" = "{sha256.lower()}" ] '
            f"|| {{ rm -f {path}; echo {FIRMWARE_CHECKSUM_MISMATCH}; false; }}; }}"
        )
    keep = "" if keep_settings else "-n "
    force_flag = "-F " if force else ""
    steps.append(f"sysupgrade {force_flag}{keep}{path}")
    return " && ".join(steps)


def check_sysupgrade_output(output: Any) -> None:
    """Raise FirmwareInstallError if the command reported it did not flash."""
    if not isinstance(output, str):
        return
    if FIRMWARE_CHECKSUM_MISMATCH in output:
        msg = (
            "Firmware checksum mismatch: the downloaded image does not match "
            "the published SHA256. It was deleted and sysupgrade was not started."
        )
        raise FirmwareInstallError(msg)
    if FIRMWARE_DOWNLOAD_FAILED in output:
        msg = "Firmware download failed on the router; sysupgrade was not started."
        raise FirmwareInstallError(msg)
