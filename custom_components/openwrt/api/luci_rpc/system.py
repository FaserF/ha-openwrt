from __future__ import annotations
from typing import TYPE_CHECKING
from .exceptions import *
import asyncio
import contextlib
import json
import logging
import re
from typing import Any
import aiohttp
from ..base import (
    PROVISION_SCRIPT_TEMPLATE,
    AccessControl,
    AdBlockStatus,
    BanIpStatus,
    ConnectedDevice,
    DeviceInfo,
    DhcpLease,
    DiagnosticResult,
    FirewallRedirect,
    FirewallRule,
    LedInfo,
    LldpNeighbor,
    MwanStatus,
    NetworkInterface,
    NlbwmonTraffic,
    OpenWrtClient,
    OpenWrtPackages,
    OpenWrtPermissions,
    ServiceInfo,
    SimpleAdBlockStatus,
    SqmStatus,
    StorageUsage,
    SystemResources,
    UpnpMapping,
    WifiCredentials,
    WireGuardInterface,
    WireGuardPeer,
    WirelessInterface,
    WpsStatus,
)
_LOGGER = logging.getLogger(__name__)

class LuciRpcSystemMixin:
    """System methods for LuciRpcClient."""

    async def get_device_info(self) -> DeviceInfo:
        """Get device information."""
        info = DeviceInfo()

        result = await self._rpc_call("uci", "get_all", ["system", "@system[0]"])
        if isinstance(result, dict):
            info.hostname = result.get("hostname", info.hostname)

        if not info.hostname:
            try:
                hostname = await self._rpc_call("sys", "hostname")
                info.hostname = hostname or ""
            except LuciRpcError:
                pass

        try:
            version_str = await self._rpc_call(
                "sys",
                "exec",
                ["cat /etc/openwrt_release"],
            )
            if version_str:
                for line in version_str.strip().split("\n"):
                    if "DISTRIB_RELEASE" in line:
                        info.release_version = line.split("=")[1].strip().strip("'\"")
                    elif "DISTRIB_REVISION" in line:
                        info.release_revision = line.split("=")[1].strip().strip("'\"")
                    elif "DISTRIB_TARGET" in line:
                        info.target = line.split("=")[1].strip().strip("'\"")
                    elif "DISTRIB_ARCH" in line:
                        info.architecture = line.split("=")[1].strip().strip("'\"")
                info.firmware_version = (
                    f"{info.release_version} ({info.release_revision})"
                )
        except LuciRpcError:
            pass

        # Populate model and hardware info from system.board if available
        try:
            board_out = await self.execute_command("ubus call system board 2>/dev/null")
            if board_out and board_out.strip().startswith("{"):
                board_data = json.loads(board_out)
                model = board_data.get("model")
                if isinstance(model, dict):
                    info.model = str(model.get("name", model.get("id", info.model)))
                elif model:
                    info.model = str(model)
                info.board_name = board_data.get("board_name", info.board_name)
        except Exception:
            pass

        if not info.model:
            # Fallback for model
            try:
                model_out = await self.execute_command(
                    "cat /tmp/sysinfo/model 2>/dev/null",
                )
                if model_out:
                    info.model = model_out.strip()

                # Fallback 2: /etc/model
                if not info.model:
                    model_out = await self.execute_command(
                        "cat /etc/model 2>/dev/null",
                    )
                    if model_out:
                        info.model = model_out.strip()
            except Exception:
                pass

        # Get MAC address from primary interface more robustly
        try:
            # Try to get the MAC for br-lan FIRST as it's the primary LAN identity
            mac_out = await self.execute_command(
                "if [ -f /sys/class/net/br-lan/address ]; then cat /sys/class/net/br-lan/address; "
                "elif [ -f /sys/class/net/lan/address ]; then cat /sys/class/net/lan/address; "
                "elif [ -f /sys/class/net/eth0/address ]; then cat /sys/class/net/eth0/address; "
                "else cat /sys/class/net/*/address | grep -v '00:00:00:00:00:00' | head -n 1; fi",
            )
            if mac_out and isinstance(mac_out, str) and ":" in mac_out:
                info.mac_address = mac_out.strip().lower()
        except Exception:
            pass

        # If MAC is still missing, try a different approach (ifconfig/ip)
        if not info.mac_address:
            try:
                ip_addr_out = await self.execute_command(
                    "ip addr show br-lan || ip addr show lan || ip addr show eth0",
                )
                if "link/ether" in ip_addr_out:
                    mac = ip_addr_out.split("link/ether")[1].strip().split()[0]
                    info.mac_address = mac.lower()
            except Exception:
                pass

        return info
    async def get_system_resources(self) -> SystemResources:
        """Get system resource usage."""
        resources = SystemResources()

        # Fetch basic system stats
        cmds = [
            "cat /proc/meminfo",
            "cat /proc/loadavg",
            "cat /proc/uptime",
            "cat /proc/stat",
            "df -Pk 2>/dev/null",
            "ubus call system info 2>/dev/null",
            "ubus call luci getMountPoints 2>/dev/null",
        ]

        # Parallel execution via Luci RPC
        results = await asyncio.gather(
            *[self.execute_command(cmd) for cmd in cmds],
            return_exceptions=True,
        )

        # 1. Memory (from /proc/meminfo)
        meminfo = results[0]
        if isinstance(meminfo, str) and meminfo:
            for line in meminfo.strip().split("\n"):
                parts = line.split()
                if len(parts) >= 2:
                    key = parts[0].rstrip(":")
                    try:
                        val = int(parts[1]) * 1024  # Convert kB to bytes
                        if key == "MemTotal":
                            resources.memory_total = val
                        elif key == "MemFree":
                            resources.memory_free = val
                        elif key == "Buffers":
                            resources.memory_buffered = val
                        elif key == "Cached":
                            resources.memory_cached = val
                        elif key == "SwapTotal":
                            resources.swap_total = val
                        elif key == "SwapFree":
                            resources.swap_free = val
                    except ValueError:
                        continue
            resources.memory_total = resources.memory_total // 1048576
            resources.memory_free = resources.memory_free // 1048576
            resources.memory_buffered = resources.memory_buffered // 1048576
            resources.memory_cached = resources.memory_cached // 1048576
            resources.memory_used = (
                resources.memory_total
                - resources.memory_free
                - resources.memory_buffered
                - resources.memory_cached
            )
            resources.swap_total = resources.swap_total // 1048576
            resources.swap_free = resources.swap_free // 1048576
            resources.swap_used = resources.swap_total - resources.swap_free

        # 2. Load (from /proc/loadavg)
        loadavg = results[1]
        if isinstance(loadavg, str) and loadavg:
            parts = loadavg.strip().split()
            if len(parts) >= 3:
                try:
                    resources.load_1min = float(parts[0])
                    resources.load_5min = float(parts[1])
                    resources.load_15min = float(parts[2])
                except ValueError:
                    pass

        # 3. Uptime (from /proc/uptime)
        uptime_str = results[2]
        if isinstance(uptime_str, str) and uptime_str:
            with contextlib.suppress(ValueError, IndexError):
                resources.uptime = int(float(uptime_str.strip().split()[0]))

        # 4. System Info (Memory fallback and CPU/Disk)
        sys_info = results[5]
        if isinstance(sys_info, str) and sys_info.strip().startswith("{"):
            try:
                data = json.loads(sys_info)
                # Fallback memory if proc/meminfo failed
                if resources.memory_total == 0:
                    mem = data.get("memory", {})
                    resources.memory_total = mem.get("total", 0) // 1048576
                    resources.memory_used = (
                        mem.get("total", 0) - mem.get("free", 0)
                    ) // 1048576

                # CPU info
                if "cpu" in data and isinstance(data["cpu"], dict):
                    cpu = data["cpu"]
                    stat_line = (
                        f"cpu  {cpu.get('user', 0)} {cpu.get('nice', 0)} "
                        f"{cpu.get('system', 0)} {cpu.get('idle', 0)} "
                        f"{cpu.get('iowait', 0)} {cpu.get('irq', 0)} "
                        f"{cpu.get('softirq', 0)} {cpu.get('steal', 0)}"
                    )
                    resources.cpu_usage = self._calculate_cpu_usage(stat_line)

                # CPU Frequency and Thermal
                if "cpu" in data and isinstance(data["cpu"], dict):
                    freq = data["cpu"].get("frequency")
                    if isinstance(freq, (int, float)):
                        resources.cpu_frequency = (
                            freq / 1000000.0 if freq > 10000 else float(freq)
                        )

                thermal = data.get("thermal", {})
                if thermal and isinstance(thermal, dict):
                    for zone, temp in thermal.items():
                        if isinstance(temp, (int, float)):
                            temp_val = temp / 1000.0 if temp > 1000 else float(temp)
                            resources.temperatures[zone] = temp_val
                            if resources.temperature is None or zone == "zone0":
                                resources.temperature = temp_val

                # Disk info
                if "disk" in data:
                    disk = data["disk"]
                    root = disk.get("root", disk.get("/", {}))
                    if isinstance(root, dict) and root.get("total"):
                        resources.filesystem_total = root.get("total", 0)
                        resources.filesystem_used = root.get("used", 0)
                        resources.filesystem_free = root.get("total", 0) - root.get(
                            "used",
                            0,
                        )
            except Exception:
                pass

        # 5. Storage fallback via luci.getMountPoints
        if resources.filesystem_total == 0:
            mounts_str = results[6]
            if isinstance(mounts_str, str) and mounts_str.strip().startswith("{"):
                try:
                    mounts = json.loads(mounts_str)
                    if isinstance(mounts, dict) and "result" in mounts:
                        for mount in mounts["result"]:
                            if mount.get("mount") in ("/", "/overlay"):
                                resources.filesystem_total = mount.get("size", 0)
                                resources.filesystem_free = mount.get(
                                    "free",
                                    0,
                                ) or mount.get("avail", 0)
                                resources.filesystem_used = (
                                    resources.filesystem_total
                                    - resources.filesystem_free
                                )
                                break
                except Exception:
                    pass

        # 6. Detailed Storage monitoring via df
        df_output = results[4]
        if isinstance(df_output, str) and df_output:
            try:
                lines = df_output.strip().split("\n")
                if len(lines) > 1:
                    for line in lines[1:]:
                        parts = line.split()
                        if len(parts) >= 6:
                            try:
                                usage = StorageUsage(
                                    device=parts[0],
                                    total=int(parts[1]) * 1024,
                                    used=int(parts[2]) * 1024,
                                    free=int(parts[3]) * 1024,
                                    percent=float(parts[4].rstrip("%")),
                                    mount_point=parts[5],
                                )
                                resources.storage.append(usage)

                                # Update legacy fields for compatibility
                                if usage.mount_point in ("/", "/overlay"):
                                    if (
                                        usage.mount_point == "/overlay"
                                        or resources.filesystem_total == 0
                                    ):
                                        resources.filesystem_total = usage.total
                                        resources.filesystem_used = usage.used
                                        resources.filesystem_free = usage.free
                            except (
                                ValueError,
                                IndexError,
                            ):
                                continue
            except Exception:  # noqa: BLE001
                pass

        # 7. CPU usage fallback from /proc/stat
        if resources.cpu_usage == 0.0:
            proc_stat = results[3]
            if isinstance(proc_stat, str) and proc_stat:
                resources.cpu_usage = self._calculate_cpu_usage(proc_stat)

        # 8. Thermal
        try:
            for zone in range(3):
                temp_raw = await self.execute_command(
                    f"cat /sys/class/thermal/thermal_zone{zone}/temp 2>/dev/null"
                )
                if temp_raw:
                    match = re.search(r"(\d+)", temp_raw)
                    if match:
                        temp = float(match.group(1))
                        if temp > 200:
                            temp /= 1000.0
                        if 0 < temp < 150:
                            resources.temperature = temp
                            break
        except Exception:
            pass

        return resources
    async def reboot(self) -> bool:
        """Reboot the device via LuCI RPC."""
        try:
            await self._rpc_call("sys", "reboot")
            return True
        except LuciRpcError:
            try:
                await self.execute_command("reboot")
                return True
            except Exception:
                return False
    async def get_installed_packages(self) -> list[str]:
        """Get a list of installed packages via apk or opkg.

        On OpenWrt 25.x+ with APK: 'apk info' lists one package per line
        (no version suffix in the default output).  On older opkg-based
        firmware the first field (before the first space) is the package name.
        """
        try:
            cmd = (
                "if command -v apk >/dev/null 2>&1; then "
                "  apk info 2>/dev/null; "
                "else "
                "  opkg list-installed 2>/dev/null | cut -d' ' -f1; "
                "fi"
            )
            output = await self.execute_command(cmd)
            if not output:
                return []
            packages: list[str] = []
            for line in output.splitlines():
                name = line.strip()
                if not name:
                    continue

                # Strip version for apk (package-version-release)
                if "-" in name and any(c.isdigit() for c in name):
                    parts = name.split("-")
                    for i in range(1, len(parts)):
                        if parts[i] and parts[i][0].isdigit():
                            name = "-".join(parts[:i])
                            break

                packages.append(name)
            return list(set(packages))
        except LuciRpcError:
            _LOGGER.debug("Failed to list installed packages via LuCI RPC")
            return []
        except Exception as err:
            _LOGGER.debug("Unexpected error listing installed packages: %s", err)
            return []
    async def get_system_logs(self, count: int = 10) -> list[str]:
        """Get recent system log entries via execute_command (logread)."""
        try:
            # Directly use execute_command (sys.exec) with logread
            # Calling direct ubus log.read via LuCI RPC causes uhttpd spam on certain devices
            cmd = await self._get_logread_command(count)
            output = await self.execute_command(cmd)
            if output:
                return [line.strip() for line in output.splitlines() if line.strip()]
        except Exception as err:
            _LOGGER.debug("Failed to get system logs via LuCI RPC: %s", err)
        return []
    async def perform_diagnostics(self) -> list[DiagnosticResult]:
        """Perform LuCI RPC-specific diagnostic checks."""
        results: list[DiagnosticResult] = []

        # Check connection error
        if self._last_connect_error:
            err_str = str(self._last_connect_error)
            if "ConnectionRefusedError" in err_str or "connection refused" in err_str.lower() or "connect call failed" in err_str.lower() or "1225" in err_str:
                results.append(
                    DiagnosticResult(
                        name="Router Web Server",
                        status="FAIL",
                        message="Connection refused by the router's web server.",
                        details=(
                            "The router's web server (uhttpd) is not running or is blocking the connection on port 80/443. "
                            "Please check that uhttpd/nginx is enabled and running on the router (e.g. run "
                            "'/etc/init.d/uhttpd start' over SSH)."
                        )
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
                            "Please ensure that the 'luci-mod-rpc' package is installed."
                        )
                    )
                )

        # 1. Check Session
        if self._auth_token:
            results.append(
                DiagnosticResult(
                    name="LuCI Session",
                    status="PASS",
                    message="Session token is active.",
                    details=f"Token: {self._auth_token[:8]}...",
                )
            )
        else:
            results.append(
                DiagnosticResult(
                    name="LuCI Session",
                    status="FAIL",
                    message="No active session token.",
                )
            )

        # 2. Check for luci object
        try:
            # Try to call a simple luci method
            await self._rpc_call("luci", "getRPCDeclaration")
            results.append(
                DiagnosticResult(
                    name="LuCI RPC declaration",
                    status="PASS",
                    message="Successfully retrieved RPC declaration from 'luci' object.",
                )
            )
        except Exception as err:
            results.append(
                DiagnosticResult(
                    name="LuCI RPC declaration",
                    status="FAIL",
                    message="Failed to call 'luci' object.",
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
    async def is_reboot_required(self) -> bool:
        """Check if reboot is required via LuCI RPC."""
        try:
            output = await self.execute_command(
                "[ -f /tmp/.reboot-needed ] || [ -f /var/run/reboot-required ] && echo 1"
            )
            return output.strip() == "1"
        except Exception:
            return False
