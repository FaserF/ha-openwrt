# mypy: disable-error-code="attr-defined,has-type"
"""SSH system diagnostics, logs, and reboot mixin."""

from __future__ import annotations

import json
import logging
import re

from ..base import DiagnosticResult

_LOGGER = logging.getLogger(__name__)


class SshSystemDiagnosticsMixin:
    """Mixin for SSH diagnostics, logs, and reboot operations."""

    async def get_system_logs(self, count: int = 10) -> list[str]:
        """Get recent system log entries via SSH."""
        try:
            # 1. Try via direct ubus call (More robust/structured)
            try:
                # Use double braces for f-string escaping if needed,
                # but here we just need to ensure the JSON is valid for the shell
                res_out = await self._exec(
                    f"ubus call log read '{{\"lines\": {int(count or 10)}}}'"
                )
                if res_out:
                    res = json.loads(res_out)
                    if res and isinstance(res, dict) and "log" in res:
                        return [
                            entry.get("msg", "").strip()
                            for entry in res.get("log", [])
                            if entry.get("msg")
                        ]
            except Exception as err:
                _LOGGER.debug("Direct SSH ubus log read failed: %s", err)

            # 2. Fallback to logread command
            cmd = await self._get_logread_command(count)
            output = await self._exec(cmd)
            if output:
                return [line.strip() for line in output.splitlines() if line.strip()]
        except Exception as err:
            _LOGGER.debug("Failed to get system logs via SSH: %s", err)
        return []

    async def reboot(self) -> bool:
        """Reboot the device."""
        try:
            await self._exec("reboot")
            return True
        except Exception as err:
            _LOGGER.exception("Failed to reboot: %s", err)
            return False

    async def perform_diagnostics(self) -> list[DiagnosticResult]:
        """Perform SSH-specific diagnostic checks."""
        results: list[DiagnosticResult] = []

        # Check connection error
        if self._last_connect_error:
            err_str = str(self._last_connect_error)
            if (
                "connection refused" in err_str.lower()
                or "connect call failed" in err_str.lower()
                or "1225" in err_str
            ):
                results.append(
                    DiagnosticResult(
                        name="SSH Service",
                        status="FAIL",
                        message="Connection refused by the router's SSH server.",
                        details=(
                            "The router's SSH service is not running or is blocking the connection on port 22 (or custom port). "
                            "Please check that dropbear/openssh is enabled and running on the router."
                        ),
                    )
                )
            elif (
                "unreachable" in err_str.lower()
                or "no route to host" in err_str.lower()
            ):
                results.append(
                    DiagnosticResult(
                        name="Host Reachability",
                        status="FAIL",
                        message="Host is unreachable.",
                        details=(
                            "The host is unreachable. Please verify the IP address or hostname is correct, "
                            "and that the router is online and connected to the network."
                        ),
                    )
                )

        # 1. Check Shell Access
        try:
            output = await self.execute_command("echo 'OpenWrt-SSH-Test'")
            if "OpenWrt-SSH-Test" in output:
                results.append(
                    DiagnosticResult(
                        name="Shell Access",
                        status="PASS",
                        message="Successfully executed simple echo command via SSH.",
                    )
                )
        except Exception as err:
            results.append(
                DiagnosticResult(
                    name="Shell Access",
                    status="FAIL",
                    message="Failed to execute shell command.",
                    details=str(err),
                )
            )

        # 1.5 Firmware Identification
        try:
            os_info = await self.execute_command(
                "cat /etc/openwrt_release /etc/os-release 2>/dev/null"
            )
            distro = "Unknown"
            if os_info:
                if "DISTRIB_DESCRIPTION" in os_info:
                    match = re.search(
                        r'DISTRIB_DESCRIPTION=["\'](.*?)["\']', os_info
                    ) or re.search(r"DISTRIB_DESCRIPTION=(.*)", os_info)
                    if match:
                        distro = match.group(1).strip()
                elif "PRETTY_NAME" in os_info:
                    match = re.search(r'PRETTY_NAME=["\'](.*?)["\']', os_info)
                    if match:
                        distro = match.group(1).strip()

            results.append(
                DiagnosticResult(
                    name="Firmware Identification",
                    status="PASS" if "OpenWrt" in distro else "WARN",
                    message=f"Detected: {distro}",
                    details=os_info if os_info else "No release info found.",
                )
            )
        except Exception:
            pass

        # 2. Check Package Manager
        try:
            # Try multiple ways to find package managers
            apk_check = await self.execute_command(
                "command -v apk || which apk || ls /sbin/apk /usr/bin/apk 2>/dev/null"
            )
            opkg_check = await self.execute_command(
                "command -v opkg || which opkg || ls /bin/opkg /usr/bin/opkg 2>/dev/null"
            )

            msg = []
            if apk_check and ("/" in apk_check or "apk" in apk_check):
                path = apk_check.strip().splitlines()[0]
                msg.append(f"apk found ({path})")
            if opkg_check and ("/" in opkg_check or "opkg" in opkg_check):
                path = opkg_check.strip().splitlines()[0]
                msg.append(f"opkg found ({path})")

            results.append(
                DiagnosticResult(
                    name="Package Manager",
                    status="PASS" if msg else "WARN",
                    message=(
                        ", ".join(msg)
                        if msg
                        else "No package manager (apk/opkg) detected. Firmware may be restricted or custom."
                    ),
                )
            )
        except Exception as err:
            results.append(
                DiagnosticResult(
                    name="Package Manager",
                    status="WARN",
                    message="Failed to check for package managers.",
                    details=str(err),
                )
            )

        # 3. Check for logread flag support
        try:
            cmd = await self._get_logread_command(1)
            results.append(
                DiagnosticResult(
                    name="Logread Compatibility",
                    status="PASS",
                    message=f"Using command: {cmd}",
                    details=f"Detected flag: {self._logread_flag}",
                )
            )
        except Exception as err:
            results.append(
                DiagnosticResult(
                    name="Logread Compatibility",
                    status="FAIL",
                    message="Failed to detect logread capabilities.",
                    details=str(err),
                )
            )

        # 4. Get recent logs
        try:
            logs = await self.get_system_logs(20)
            if logs:
                results.append(
                    DiagnosticResult(
                        name="System Logs (Recent)",
                        status="INFO",
                        message=f"Retrieved {len(logs)} log entries.",
                        details="\n".join(logs),
                    )
                )
        except Exception:
            pass

        return results
