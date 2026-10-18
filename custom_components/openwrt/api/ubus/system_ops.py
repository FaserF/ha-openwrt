# mypy: disable-error-code="attr-defined,has-type"
"""Ubus system execution, provisioning, and firmware operations mixin."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from ..base import PROVISION_SCRIPT_TEMPLATE
from ..firmware_install import (
    FirmwareInstallError,
    build_sysupgrade_command,
    check_sysupgrade_output,
)
from .exceptions import (
    UbusError,
    UbusPermissionError,
)

_LOGGER = logging.getLogger(__name__)


class UbusSystemOpsMixin:
    """Mixin for Ubus system operations (commands, reboot, provisioning, firmware)."""

    async def reboot(self) -> bool:
        """Reboot the device via ubus."""
        try:
            await self._call("system", "reboot")
            return True
        except UbusError:
            # Fallback to shell if system.reboot is not available
            try:
                await self.execute_command("reboot")
                return True
            except Exception:
                return False

    async def execute_command(self, command: str) -> str:
        """Execute a shell command on the device via ubus."""
        try:
            # Wrap in /bin/sh -c to handle operators like && or >
            res = await self._call(
                "file",
                "exec",
                {"command": "/bin/sh", "params": ["-c", command.strip()]},
            )
            if not res or not isinstance(res, dict):
                return ""

            stdout = str(res.get("stdout") or "").strip()
            stderr = str(res.get("stderr") or "").strip()
            code = res.get("code", 0)

            if code != 0:
                _LOGGER.debug(
                    "Command failed (code %d). stdout: %s, stderr: %s",
                    code,
                    stdout,
                    stderr,
                )
                # If stdout is empty but stderr has content, return stderr to help with debugging
                return stdout or stderr

            return stdout
        except UbusPermissionError as err:
            _LOGGER.debug(
                "Permission denied for command via ubus file.exec: %s",
                err,
            )
            return ""
        except UbusError as err:
            _LOGGER.debug("Command failed via ubus file.exec: %s", err)
            return ""

    async def file_exec(
        self, command: str, params: list[str] | None = None
    ) -> dict[str, Any]:
        """Execute a binary directly via rpcd file.exec without shell wrapping."""
        try:
            return await self._call(
                "file", "exec", {"command": command, "params": params or []}
            )
        except UbusError:
            raise

    async def read_file(self, path: str) -> str | None:
        """Read a file via rpcd file.read (needs only 'read' ACL on the path)."""
        try:
            res = await self._call("file", "read", {"path": path})
            if isinstance(res, dict) and res.get("data") is not None:
                return str(res["data"])
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("file.read failed for %s: %s", path, err)
        return None

    async def user_exists(self, username: str) -> bool:
        """Check if a system user exists on the device."""
        # 1. Try via ubus file.read (more robust/standard than exec)
        try:
            res = await self._call("file", "read", {"path": "/etc/passwd"})
            if (
                res
                and isinstance(res, dict)
                and "data" in res
                and f"{username}:" in res["data"]
            ):
                return True
        except Exception:
            pass

        # 2. Fallback to base method (which uses execute_command)
        return await super().user_exists(username)

    async def provision_user(
        self,
        username: str,
        password: str,
    ) -> tuple[bool, str | None]:
        """Create a dedicated system user and configure RPC permissions via ubus.

        This requires 'file.exec' RPC permission on the current session, which
        means provisioning can only be performed as root (or a user that already
        has exec rights).  If the current user lacks those rights the method
        returns a clear error message instead of silently failing.
        """
        # Use the harmonized provisioning script from base
        script = PROVISION_SCRIPT_TEMPLATE.format(username=username, password=password)
        try:
            output = await self.execute_command(script)

            if output is None:
                output = ""

            if output:
                _LOGGER.debug("Provisioning output for %s: %s", username, output)

            if "Provisioning SUCCESS" in output:
                return True, None

            if "LOG: FAIL:" in output:
                fail_msg = output.split("LOG: FAIL:")[1].splitlines()[0].strip()
                _LOGGER.error("Provisioning failed: %s", fail_msg)
                return False, fail_msg

            # Empty output: the inline sh -c approach may exceed the ubus JSON-RPC
            # command-line buffer limit for long scripts. Retry by writing the
            # script to /tmp first and then executing the file directly.
            if not output:
                _LOGGER.debug(
                    "Provisioning for %s returned empty output via inline exec. "
                    "Retrying with file.write + file.exec approach.",
                    username,
                )
                output = await self._provision_via_tmp_file(script, username)

            if "Provisioning SUCCESS" in output:
                return True, None

            if "LOG: FAIL:" in output:
                fail_msg = output.split("LOG: FAIL:")[1].splitlines()[0].strip()
                _LOGGER.error("Provisioning failed: %s", fail_msg)
                return False, fail_msg

            if not output:
                # Still empty after both approaches
                if self.username == "root":
                    hint = (
                        "Provisioning returned empty output even as root. "
                        "This is common on Xiaomi and other OEM firmwares that block 'file.exec' via ubus. "
                        "Check router syslog ('logread | grep ha-openwrt') for details or try connecting via SSH."
                    )
                else:
                    hint = (
                        f"Provisioning failed: empty response from ubus file.exec. "
                        f"Ensure '{self.username}' has file.exec permission, "
                        "or run provisioning as 'root'."
                    )
                _LOGGER.warning(
                    "Provisioning for %s returned empty output (connected as %s). %s",
                    username,
                    self.username,
                    hint,
                )
                return False, hint

            return (
                False,
                "Provisioning script returned failure without specific error. Check router logs (logread).",
            )
        except UbusPermissionError as err:
            msg = (
                f"Provisioning failed: '{self.username}' lacks 'file.exec' RPC permission. "
                "Switch to 'root' or grant exec rights to this user in the rpcd ACL."
            )
            _LOGGER.error("%s (%s)", msg, err)
            return False, msg
        except Exception as err:
            _LOGGER.exception("Failed to provision user %s via ubus: %s", username, err)
            return False, str(err)

    async def _provision_via_tmp_file(self, script: str, username: str) -> str:
        """Write provisioning script to /tmp and execute it.

        Used as a fallback when passing the script inline via 'sh -c <script>'
        fails (e.g. ubus JSON-RPC response buffer limit or command-line length).
        """
        tmp_path = "/tmp/ha_provision.sh"
        try:
            # 1. Write script to a temp file via ubus file.write
            await self._call(
                "file",
                "write",
                {"path": tmp_path, "data": script},
            )
            _LOGGER.debug(
                "Wrote provisioning script to %s for user %s", tmp_path, username
            )

            # 2. Execute the script file directly
            res = await self._call(
                "file",
                "exec",
                {"command": "/bin/sh", "params": [tmp_path]},
            )
            output = res.get("stdout", "") if isinstance(res, dict) else ""

            # 3. Clean up (best-effort)
            with contextlib.suppress(Exception):
                await self.execute_command(f"rm -f {tmp_path}")

            return output or ""
        except Exception as err:
            _LOGGER.debug("file.write/exec provisioning fallback failed: %s", err)
            return ""

    async def install_firmware(
        self,
        url: str,
        keep_settings: bool = True,
        force: bool = False,
        sha256: str = "",
    ) -> None:
        """Install firmware from the given URL via ubus."""
        cmd = build_sysupgrade_command(url, keep_settings, force, sha256)
        try:
            _LOGGER.info("Initiating firmware installation via ubus from: %s", url)
            output = await self.execute_command(cmd)
            check_sysupgrade_output(output)
        except FirmwareInstallError:
            raise
        except Exception as err:
            # If it's a connection error, it's likely the router rebooting
            err_msg = str(err).lower()
            if any(
                msg in err_msg
                for msg in [
                    "connection reset",
                    "broken pipe",
                    "closed",
                    "eof",
                    "timeout",
                    "cannot connect",
                    "could not connect",
                    "connection lost",
                    "connection failed",
                    "connection error",
                    "unreachable",
                    "host",
                ]
            ):
                _LOGGER.info(
                    "Ubus connection lost during sysupgrade - device is rebooting",
                )
                return
            _LOGGER.exception("Failed to execute sysupgrade via ubus: %s", err)
            msg = f"sysupgrade execution failed: {err}"
            raise UbusError(msg) from err

    async def download_file(self, remote_path: str, local_path: str) -> bool:
        """Download a file from the router via ubus file.read."""
        try:
            import base64

            # ubus file.read returns base64 encoded data (in "data" key) when base64: True is passed
            res = await self._call(
                "file", "read", {"path": remote_path, "base64": True}
            )
            if res and isinstance(res, dict) and "data" in res:
                data = base64.b64decode(res["data"])
                await asyncio.to_thread(self._write_bytes, local_path, data)
                return True
        except Exception as err:
            _LOGGER.exception("Failed to download file via ubus: %s", err)
        return False
