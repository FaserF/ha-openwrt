# mypy: disable-error-code="attr-defined"
from __future__ import annotations

import logging

from ..base import (
    DiagnosticResult,
    ServiceInfo,
    classify_service,
)
from .exceptions import UbusError, UbusPermissionError

_LOGGER = logging.getLogger(__name__)
UBUS_JSONRPC_VERSION = "2.0"
UBUS_ID_AUTH = 1
UBUS_ID_CALL = 2


from .services_packages import UbusServicesPackagesMixin


class UbusServicesMixin(UbusServicesPackagesMixin):
    """Services methods for UbusClient."""

    async def get_services(self) -> list[ServiceInfo]:
        """Get init.d services via the rc ubus interface."""
        services: list[ServiceInfo] = []
        result = await self._call("rc", "list")

        # 'service list' distinguishes daemons (which own procd instances) from
        # one-shot scripts, which procd always reports as not running.
        try:
            service_map = await self._call("service", "list")
        except Exception:  # noqa: BLE001
            service_map = {}
        if not isinstance(service_map, dict):
            service_map = {}

        for name, data in result.items():
            services.append(classify_service(name, data, service_map.get(name)))
        return services

    async def manage_service(self, name: str, action: str) -> bool:
        """Manage a system service (start/stop/restart/enable/disable)."""
        try:
            # 1. Try standard ubus rc.init (best practice)
            await self._call("rc", "init", {"name": name, "action": action})
            self._invalidate_slow_poll()
            return True
        except (
            UbusPermissionError,
            UbusError,
        ):
            try:
                # 2. Try ubus file.exec (direct init script call)
                await self._call(
                    "file",
                    "exec",
                    {"command": f"/etc/init.d/{name}", "params": [action]},
                )
                self._invalidate_slow_poll()
                return True
            except Exception:
                # 3. Final fallback to shell execute_command
                try:
                    await self.execute_command(f"/etc/init.d/{name} {action}")
                    self._invalidate_slow_poll()
                    return True
                except Exception as err:
                    _LOGGER.debug(
                        "Failed to manage service %s (%s) via any method: %s",
                        name,
                        action,
                        err,
                    )
                    return False

    async def get_installed_packages(self) -> list[str]:
        """Get a list of installed packages via apk or opkg.

        On OpenWrt 25.x+ with APK: 'apk info' lists one package per line
        (no version suffix in the default output).  On older opkg-based
        firmware the first field (before the first space) is the package name.
        """
        try:
            # Try apk first (OpenWrt 25+); fall back to opkg.
            # apk info -q suppresses progress/warnings. We strip any trailing
            # version suffix that some apk builds append (e.g. "pkg-1.0-r0").
            # Try apk (OpenWrt 25+) or opkg. Use absolute paths as fallback.
            cmd = (
                "if command -v apk >/dev/null 2>&1; then APK=apk; "
                "elif [ -x /sbin/apk ]; then APK=/sbin/apk; fi; "
                'if [ -n "$APK" ]; then $APK info 2>/dev/null; '
                "else "
                "  if command -v opkg >/dev/null 2>&1; then OPKG=opkg; "
                "  elif [ -x /bin/opkg ]; then OPKG=/bin/opkg; fi; "
                "  if [ -n \"$OPKG\" ]; then $OPKG list-installed 2>/dev/null | cut -d' ' -f1; fi; "
                "fi"
            )
            output = await self.execute_command(cmd)
            if not output:
                return []
            packages: list[str] = []
            for line in output.splitlines():
                name = line.strip()
                if name:
                    packages.append(name)
            return packages
        except UbusError:
            _LOGGER.debug("Failed to list installed packages via ubus file.exec")
            return []
        except Exception as err:
            _LOGGER.debug("Unexpected error listing installed packages: %s", err)
            return []

    async def get_leds(self) -> list:
        """Get LEDs from /sys/class/leds via file.exec."""
        from ..base import LedInfo

        leds: list[LedInfo] = []
        cmd = (
            "for led in /sys/class/leds/*/; do "
            'name=$(basename "$led"); '
            'brightness=$(cat "$led/brightness" 2>/dev/null || echo 0); '
            'max=$(cat "$led/max_brightness" 2>/dev/null || echo 255); '
            'trigger=$(cat "$led/trigger" 2>/dev/null | tr " " "\\n" | grep "^\\[" | tr -d "[]" || echo none); '
            'echo "$name|$brightness|$max|$trigger"; '
            "done"
        )
        stdout = await self.execute_command(cmd)
        for line in stdout.strip().splitlines():
            parts = line.strip().split("|")
            if len(parts) >= 4:
                brightness = int(parts[1]) if parts[1].isdigit() else 0
                max_b = int(parts[2]) if parts[2].isdigit() else 255
                leds.append(
                    LedInfo(
                        name=parts[0],
                        brightness=brightness,
                        max_brightness=max_b,
                        trigger=parts[3],
                        active=brightness > 0,
                    ),
                )
        return leds

    async def perform_diagnostics(self) -> list[DiagnosticResult]:
        """Perform ubus-specific diagnostic checks."""
        results: list[DiagnosticResult] = []

        # Check connection error
        if self._last_connect_error:
            err_str = str(self._last_connect_error)
            if (
                "ConnectionRefusedError" in err_str
                or "connection refused" in err_str.lower()
                or "connect call failed" in err_str.lower()
                or "1225" in err_str
            ):
                results.append(
                    DiagnosticResult(
                        name="Router Web Server",
                        status="FAIL",
                        message="Connection refused by the router's web server.",
                        details=(
                            "The router's web server (uhttpd) is not running or is blocking the connection on port 80/443. "
                            "Please check that uhttpd/nginx is enabled and running on the router (e.g. run "
                            "'/etc/init.d/uhttpd start' over SSH)."
                        ),
                    )
                )
            elif "404" in err_str or "not found" in err_str.lower():
                results.append(
                    DiagnosticResult(
                        name="RPC Endpoint Availability",
                        status="FAIL",
                        message="API endpoint not found (HTTP 404).",
                        details=(
                            "The router returned HTTP 404 (Not Found) for the API endpoint. "
                            "This indicates that the required RPC package is not installed on the router. "
                            "Please ensure that the 'uhttpd-mod-ubus' package is installed."
                        ),
                    )
                )

        # 1. Check Session ACLs
        try:
            session_data = await self._call("session", "list")
            acls = session_data.get("acls", {})
            if acls:
                results.append(
                    DiagnosticResult(
                        name="Session ACLs",
                        status="PASS",
                        message=f"Session for '{self.username}' is active with {len(acls)} ACL groups.",
                        details=str(acls),
                    )
                )
            else:
                results.append(
                    DiagnosticResult(
                        name="Session ACLs",
                        status="WARN",
                        message="Session is active but no ACLs were returned. This may be a restricted session.",
                    )
                )
        except Exception as err:
            results.append(
                DiagnosticResult(
                    name="Session ACLs",
                    status="FAIL",
                    message="Failed to retrieve session ACLs.",
                    details=str(err),
                )
            )

        # 2. Check for key objects
        required_objects = ["system", "network.interface", "uci"]

        try:
            objects = await self._list_objects()
            found_req = [obj for obj in required_objects if obj in objects]

            if len(found_req) == len(required_objects):
                results.append(
                    DiagnosticResult(
                        name="Core Ubus Objects",
                        status="PASS",
                        message=f"All core objects found: {', '.join(found_req)}",
                    )
                )
            else:
                missing = set(required_objects) - set(found_req)
                results.append(
                    DiagnosticResult(
                        name="Core Ubus Objects",
                        status="FAIL",
                        message=f"Missing core objects: {', '.join(missing)}",
                        remedy="Ensure 'rpcd' is running and your user has access to these objects in /etc/config/rpcd.",
                    )
                )
        except Exception as err:
            results.append(
                DiagnosticResult(
                    name="Ubus Objects",
                    status="FAIL",
                    message="Failed to list ubus objects.",
                    details=str(err),
                )
            )

        # 3. Check for logread flag support (Fixes #17 analysis)
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

        # 4. Check for file.exec (Common blocker)
        try:
            await self.execute_command("id")
            results.append(
                DiagnosticResult(
                    name="Command Execution (file.exec)",
                    status="PASS",
                    message="Successfully executed 'id' command.",
                )
            )
        except UbusPermissionError:
            results.append(
                DiagnosticResult(
                    name="Command Execution (file.exec)",
                    status="FAIL",
                    message="Permission denied for file.exec.",
                    remedy="Grant 'file': ['exec'] permission to your user or connect as 'root'. Note: Some OEM firmwares block this entirely.",
                )
            )
        except Exception as err:
            results.append(
                DiagnosticResult(
                    name="Command Execution (file.exec)",
                    status="FAIL",
                    message="Failed to execute command.",
                    details=str(err),
                )
            )

        # 5. Get recent logs
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

    async def set_led(self, name: str, brightness: int) -> bool:
        """Enable or disable an LED via ubus."""
        try:
            val = str(int(brightness))
            # First ensure trigger is set to none
            await self.execute_command(
                f"echo none > /sys/class/leds/{name}/trigger 2>/dev/null"
            )
            # Try file.write
            await self._call(
                "file",
                "write",
                {"path": f"/sys/class/leds/{name}/brightness", "data": val},
            )
            self._invalidate_slow_poll()
            return True
        except Exception:
            try:
                # Fallback to shell
                await self.execute_command(
                    f"echo {val} > /sys/class/leds/{name}/brightness"
                )
                self._invalidate_slow_poll()
                return True
            except Exception as err:
                _LOGGER.debug("Failed to set LED %s via ubus: %s", name, err)
                return False

    async def is_reboot_required(self) -> bool:
        """Check if reboot is required via common OpenWrt flags."""
        try:
            # Check for common flags
            paths = ["/tmp/.reboot-needed", "/var/run/reboot-required"]
            for path in paths:
                res = await self._call("file", "stat", {"path": path})
                if res and isinstance(res, dict) and "type" in res:
                    return True
            return False
        except Exception:
            # Fallback to shell
            try:
                output = await self.execute_command(
                    "[ -f /tmp/.reboot-needed ] || [ -f /var/run/reboot-required ] && echo 1"
                )
                return output.strip() == "1"
            except Exception:
                return False
