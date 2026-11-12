"""Firmware download, verification and sysupgrade helpers shared by all clients."""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import shlex
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


_LOGGER = logging.getLogger(__name__)

FIRMWARE_TMP_PATH = "/tmp/firmware.bin"  # noqa: S108
FIRMWARE_DOWNLOAD_FAILED = "__OPENWRT_HA_FW_DOWNLOAD_FAILED__"
FIRMWARE_CHECKSUM_MISMATCH = "__OPENWRT_HA_FW_CHECKSUM_MISMATCH__"

# Detached install (ubus and LuCI RPC). rpcd kills a file.exec command after
# its exec timeout (30 s in OpenWrt's default /etc/config/rpcd) and uhttpd
# kills a LuCI request after its script_timeout (60 s by default), both far
# shorter than an image download. So the command runs in the background on
# the router with its output in a job directory, and Home Assistant polls it.
FIRMWARE_JOB_DIR_PREFIX = "/tmp/openwrt-ha-fw-"  # noqa: S108
FIRMWARE_JOB_STARTED = "__OPENWRT_HA_FW_STARTED__"
FIRMWARE_JOB_BUSY = "__OPENWRT_HA_FW_BUSY__"
FIRMWARE_JOB_LAUNCH_FAILED = "__OPENWRT_HA_FW_LAUNCH_FAILED__"
FIRMWARE_JOB_RUNNING = "__OPENWRT_HA_FW_RUNNING__"
FIRMWARE_JOB_MISSING = "__OPENWRT_HA_FW_NO_JOB__"
FIRMWARE_JOB_EXIT = "__OPENWRT_HA_FW_EXIT__"
FIRMWARE_JOB_CANCELLED = "__OPENWRT_HA_FW_CANCELLED__"
FIRMWARE_JOB_TOO_LATE = "__OPENWRT_HA_FW_TOO_LATE__"
FIRMWARE_FLASHING = "__OPENWRT_HA_FW_FLASHING__"
# How long the download and checks may take before the install is cancelled.
FIRMWARE_JOB_TIMEOUT = 900
# How long to keep watching once sysupgrade has started.
FIRMWARE_FLASH_GRACE = 120
FIRMWARE_POLL_INTERVAL = 2


class FirmwareInstallError(Exception):
    """The firmware image was not flashed (download or checksum failure)."""


def build_sysupgrade_command(
    url: str,
    keep_settings: bool = True,
    force: bool = False,
    sha256: str = "",
    job_dir: str = "",
) -> str:
    """Build the shell command that downloads, verifies and flashes an image.

    TLS certificates are verified. When a sha256 is given, the image is
    checked before sysupgrade runs and deleted on mismatch, and sysupgrade is
    never started. Failures print a marker that check_sysupgrade_output
    turns into an exception.

    With job_dir (a detached install, see run_detached_firmware_install), the
    command takes the job's gate before sysupgrade, so Home Assistant can
    still cancel it, and prints a marker once sysupgrade is about to start.
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
    if job_dir:
        steps.append(
            f"{{ mkdir {job_dir}/gate 2>/dev/null "
            f"|| {{ echo {FIRMWARE_JOB_CANCELLED}; false; }}; }}"
        )
        steps.append(f"echo {FIRMWARE_FLASHING}")
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


def build_detached_sysupgrade_command(
    job_id: str,
    url: str,
    keep_settings: bool = True,
    force: bool = False,
    sha256: str = "",
) -> str:
    """Build the command that starts the install in the background.

    The job directory is named after job_id and created with mkdir, which is
    atomic: if a transport retry delivers this command twice, the second copy
    finds the directory and starts nothing. A job from another install that
    is still running (alive and without an exit marker) blocks a new one;
    finished ones are removed.
    """
    if not re.fullmatch(r"[0-9a-f]{8,64}", job_id):
        msg = f"Invalid firmware job id: {job_id!r}"
        raise FirmwareInstallError(msg)
    job = f"{FIRMWARE_JOB_DIR_PREFIX}{job_id}"
    inner = build_sysupgrade_command(url, keep_settings, force, sha256, job_dir=job)
    return (
        f"if ! mkdir {job} 2>/dev/null; then "
        f"if [ -d {job} ]; then echo {FIRMWARE_JOB_STARTED}; "
        f"else echo {FIRMWARE_JOB_LAUNCH_FAILED}; fi; exit 0; fi; "
        f"busy=; for o in {FIRMWARE_JOB_DIR_PREFIX}*; do "
        f'[ "$o" = {job} ] && continue; '
        f'p=$(cat "$o/pid" 2>/dev/null); '
        f'if [ -n "$p" ] && kill -0 "$p" 2>/dev/null '
        f'&& ! grep -q {FIRMWARE_JOB_EXIT} "$o/log" 2>/dev/null; then busy=1; '
        f'else rm -rf "$o"; fi; done; '
        f'if [ -n "$busy" ]; then rm -rf {job}; echo {FIRMWARE_JOB_BUSY}; exit 0; fi; '
        f"( {inner}; echo {FIRMWARE_JOB_EXIT}$? ) >{job}/log 2>&1 </dev/null & "
        f"echo $! >{job}/pid; echo {FIRMWARE_JOB_STARTED}"
    )


def build_firmware_job_poll_command(job_id: str) -> str:
    """Print the job's log, then whether its process is still alive."""
    job = f"{FIRMWARE_JOB_DIR_PREFIX}{job_id}"
    return (
        f"if [ -d {job} ]; then cat {job}/log 2>/dev/null; echo; "
        f'kill -0 "$(cat {job}/pid 2>/dev/null)" 2>/dev/null '
        f"&& echo {FIRMWARE_JOB_RUNNING}; "
        f"else echo {FIRMWARE_JOB_MISSING}; fi"
    )


def build_firmware_job_cancel_command(job_id: str) -> str:
    """Take the job's gate so sysupgrade can no longer start.

    The job takes the same gate (mkdir) right before sysupgrade, so exactly
    one side wins: either the install is cancelled, or it was too late.
    """
    job = f"{FIRMWARE_JOB_DIR_PREFIX}{job_id}"
    return (
        f"if [ -d {job} ]; then "
        f"if mkdir {job}/gate 2>/dev/null; then echo {FIRMWARE_JOB_CANCELLED}; "
        f"else echo {FIRMWARE_JOB_TOO_LATE}; fi; "
        f"else echo {FIRMWARE_JOB_MISSING}; fi"
    )


def _log_tail(log: str, limit: int = 300) -> str:
    """Return the end of a job log, without the job's markers, for a message."""
    lines = [
        line.strip()
        for line in log.splitlines()
        if line.strip() and not line.startswith("__OPENWRT_HA_FW_")
    ]
    text = " / ".join(lines)
    return text if len(text) <= limit else "..." + text[-limit:]


class _FirmwareJob:
    """Watch a detached install on the router until it flashes or fails."""

    def __init__(self, run: Callable[[str], Awaitable[str]], job_id: str) -> None:
        self._run = run
        self._job_id = job_id
        self.flashing = False
        self.last_error: Exception | None = None

    async def poll_until(self, deadline: float) -> bool:
        """Poll until the router reboots into sysupgrade (True) or deadline (False).

        Raises FirmwareInstallError when the job reports that it did not flash
        or its state is lost.
        """
        loop = asyncio.get_running_loop()
        poll = build_firmware_job_poll_command(self._job_id)
        while loop.time() < deadline:
            await asyncio.sleep(FIRMWARE_POLL_INTERVAL)
            try:
                output = await self._run(poll)
            except Exception as err:  # noqa: BLE001
                if self.flashing:
                    # sysupgrade has taken over and stopped rpcd/uhttpd.
                    _LOGGER.info("Router stopped answering after sysupgrade started")
                    return True
                # Polling only reads; keep trying until the deadline.
                _LOGGER.debug("Polling the firmware install failed: %s", err)
                self.last_error = err
                continue
            if self._check(output or ""):
                return True
        return False

    def _check(self, output: str) -> bool:
        """Read one poll; True when sysupgrade has taken over."""
        if FIRMWARE_JOB_MISSING in output:
            if self.flashing:
                # /tmp was cleared by the reboot into the new image.
                return True
            msg = (
                "The router restarted, or its install status was removed, before "
                "it reported that sysupgrade had started. Check the router's "
                "firmware version before retrying."
            )
            raise FirmwareInstallError(msg)
        check_sysupgrade_output(output)
        if FIRMWARE_FLASHING in output and not self.flashing:
            _LOGGER.info("Firmware image verified; sysupgrade is starting")
            self.flashing = True
        exit_match = re.search(rf"{FIRMWARE_JOB_EXIT}(\d+)", output)
        if exit_match:
            code = int(exit_match.group(1))
            if code == 0 and self.flashing:
                return True
            msg = (
                f"sysupgrade did not start or refused the image (exit code {code}): "
                f"{_log_tail(output)}"
            )
            raise FirmwareInstallError(msg)
        if FIRMWARE_JOB_RUNNING not in output:
            if self.flashing:
                # sysupgrade stops all processes, this job included.
                return True
            msg = (
                "The firmware install on the router ended without a result: "
                f"{_log_tail(output)}"
            )
            raise FirmwareInstallError(msg)
        return False

    async def cancel(self) -> str:
        """Stop the job before sysupgrade; return the router's answer."""
        return await self._run(build_firmware_job_cancel_command(self._job_id))


async def run_detached_firmware_install(
    run: Callable[[str], Awaitable[str]],
    url: str,
    keep_settings: bool = True,
    force: bool = False,
    sha256: str = "",
) -> None:
    """Start a download-verify-sysupgrade job on the router and watch it.

    run executes one shell command on the router and returns its output; it
    must raise on any transport error or timeout instead of returning "".
    The flash command is sent once and never re-sent. Returns once sysupgrade
    has started (the router is rebooting into the new image); raises
    FirmwareInstallError in every other case, including when the router's
    state cannot be determined.
    """
    job_id = secrets.token_hex(8)
    cmd = build_detached_sysupgrade_command(job_id, url, keep_settings, force, sha256)
    try:
        output = await run(cmd)
    except Exception as err:
        msg = (
            f"Could not start the firmware install on the router: {err}. If the "
            "request reached it, the download may be running; check the router "
            "before retrying."
        )
        raise FirmwareInstallError(msg) from err
    output = output or ""
    if FIRMWARE_JOB_BUSY in output:
        msg = "Another firmware install is still running on the router."
        raise FirmwareInstallError(msg)
    if FIRMWARE_JOB_STARTED not in output:
        msg = f"The router did not start the firmware install: {_log_tail(output)}"
        raise FirmwareInstallError(msg)

    job = _FirmwareJob(run, job_id)
    loop = asyncio.get_running_loop()
    if await job.poll_until(loop.time() + FIRMWARE_JOB_TIMEOUT):
        return

    if not job.flashing:
        try:
            answer = await job.cancel()
        except Exception as err:
            msg = (
                f"The firmware install did not finish within {FIRMWARE_JOB_TIMEOUT}s "
                f"and the router could not be reached to cancel it ({err}); it is "
                "unknown whether sysupgrade will start. Check the router."
            )
            raise FirmwareInstallError(msg) from err
        if FIRMWARE_JOB_CANCELLED in answer:
            reason = f" (last error: {job.last_error})" if job.last_error else ""
            msg = (
                f"The firmware download and checks did not finish within "
                f"{FIRMWARE_JOB_TIMEOUT}s{reason}. The install was cancelled and "
                "sysupgrade will not run; the download may still finish in the "
                "background."
            )
            raise FirmwareInstallError(msg)
        if FIRMWARE_JOB_TOO_LATE not in answer:
            msg = (
                "The router restarted, or its install status was removed, before "
                "it reported that sysupgrade had started. Check the router's "
                "firmware version before retrying."
            )
            raise FirmwareInstallError(msg)
        # sysupgrade passed the gate just before the cancel did.
        job.flashing = True
        if await job.poll_until(loop.time() + FIRMWARE_FLASH_GRACE):
            return

    msg = (
        "sysupgrade started but the router has not restarted; it is unknown "
        "whether the image was flashed. Check the router."
    )
    raise FirmwareInstallError(msg)
