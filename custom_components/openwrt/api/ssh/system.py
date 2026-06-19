from __future__ import annotations
from typing import TYPE_CHECKING
from .exceptions import *
import asyncio
import contextlib
import json
import logging
import re
import shlex
from typing import Any
import paramiko
from homeassistant.helpers import storage
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
    LldpNeighbor,
    MwanStatus,
    NetworkInterface,
    NlbwmonTraffic,
    OpenWrtClient,
    OpenWrtPackages,
    OpenWrtPermissions,
    ProcessInfo,
    ServiceInfo,
    SimpleAdBlockStatus,
    SqmStatus,
    SystemResources,
    UpnpMapping,
    UsbDevice,
    WifiCredentials,
    WireGuardInterface,
    WireGuardPeer,
    WirelessInterface,
)
_LOGGER = logging.getLogger(__name__)

class SshSystemMixin:
    """System methods for SshClient."""

    async def get_device_info(self) -> DeviceInfo:
        """Get device information."""
        info = DeviceInfo()

        board_json = await self._exec(
            "ubus call system board 2>/dev/null || cat /etc/board.json 2>/dev/null",
        )
        if board_json and board_json.strip() and board_json.strip().startswith("{"):
            try:
                data = json.loads(board_json)
                info.hostname = data.get("hostname", "")
                model = data.get("model")
                if isinstance(model, dict):
                    info.model = str(model.get("name", model.get("id", info.model)))
                else:
                    info.model = str(model or data.get("board_name", ""))
                info.board_name = data.get("board_name", "")
                if not info.board_name and isinstance(model, dict):
                    info.board_name = model.get("id", "")
                info.kernel_version = data.get("kernel", "")
                info.architecture = data.get("system", "")
                release = data.get("release", {})
                info.release_distribution = release.get("distribution", "OpenWrt")
                info.release_version = release.get("version", "")
                info.release_revision = release.get("revision", "")
                info.target = release.get("target", "")
                info.firmware_version = (
                    f"{info.release_version} ({info.release_revision})"
                )
            except json.JSONDecodeError:
                _LOGGER.debug("Failed to parse board info JSON via SSH")

        if not info.model:
            try:
                # Fallback 1: /tmp/sysinfo/model (Standard OpenWrt)
                model_str = await self._exec("cat /tmp/sysinfo/model 2>/dev/null")
                if model_str:
                    info.model = model_str.strip()

                # Fallback 2: /etc/model (Xiaomi/Custom)
                if not info.model:
                    model_str = await self._exec("cat /etc/model 2>/dev/null")
                    if model_str:
                        info.model = model_str.strip()

                # Fallback for board_name
                if not info.board_name:
                    board_str = await self._exec(
                        "cat /tmp/sysinfo/board_name 2>/dev/null"
                    )
                    if board_str:
                        info.board_name = board_str.strip()
            except Exception:
                pass

        if not info.hostname:
            try:
                # Fallback 1: Direct kernel hostname access
                host_str = await self._exec("cat /proc/sys/kernel/hostname 2>/dev/null")
                if host_str and host_str.strip() not in ["", "localhost"]:
                    info.hostname = host_str.strip()

                # Fallback 2: UCI system hostname
                if not info.hostname:
                    host_str = await self._exec(
                        "uci get system.@system[0].hostname 2>/dev/null"
                    )
                    if host_str:
                        info.hostname = host_str.strip()

                # Fallback 3: Standard hostname command
                if not info.hostname:
                    host_str = await self._exec("hostname 2>/dev/null")
                    if host_str and host_str.strip() not in ["", "localhost"]:
                        info.hostname = host_str.strip()
            except Exception:
                pass

        if not info.hostname:
            try:
                # Fallback 2: UCI
                info.hostname = (
                    await self._exec("uci get system.@system[0].hostname 2>/dev/null")
                ).strip()
            except Exception:
                pass

        if not info.release_version:
            try:
                release_str = await self._exec("cat /etc/openwrt_release")
                for line in release_str.strip().split("\n"):
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
            except Exception:  # noqa: BLE001
                pass

        # Get MAC address from primary interface more robustly
        try:
            # Try to get the MAC for br-lan FIRST as it's the primary LAN identity
            mac_out = await self._exec(
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
                ip_addr_out = await self._exec(
                    "ip addr show br-lan || ip addr show lan || ip addr show eth0",
                )
                if isinstance(ip_addr_out, str) and "link/ether" in ip_addr_out:
                    mac = ip_addr_out.split("link/ether")[1].strip().split()[0]
                    info.mac_address = mac.lower()
            except Exception:
                pass

        try:
            uptime_str = await self._exec("cat /proc/uptime")
            info.uptime = int(float(uptime_str.strip().split()[0]))
        except Exception:  # noqa: BLE001
            pass

        return info
    async def get_system_resources(self) -> SystemResources:
        """Get system resource usage."""
        resources = SystemResources()

        # Fetch basic system stats in parallel
        cmds = [
            "cat /proc/meminfo",
            "cat /proc/loadavg",
            "cat /proc/uptime",
            "cat /proc/stat",
            "df -Pk 2>/dev/null",
        ]

        results = await asyncio.gather(
            *[self._exec(cmd) for cmd in cmds],
            return_exceptions=True,
        )

        # 1. Memory
        meminfo = results[0]
        if isinstance(meminfo, str) and meminfo:
            for line in meminfo.strip().split("\n"):
                parts = line.split()
                if len(parts) >= 2:
                    key = parts[0].rstrip(":")
                    val = int(parts[1]) * 1024
                    if key == "MemTotal":
                        resources.memory_total = val
                    elif key == "MemFree":
                        resources.memory_free = val
                    elif key == "Buffers":
                        resources.memory_buffered = val
                    elif key == "Cached":
                        resources.memory_cached = val
                    elif key == "MemAvailable":
                        resources.memory_available = val

            # Calculate available if not present
            if resources.memory_available == 0:
                resources.memory_available = (
                    resources.memory_free
                    + resources.memory_buffered
                    + resources.memory_cached
                )

            if resources.memory_total > 0:
                resources.memory_available_percent = round(
                    (resources.memory_available / resources.memory_total) * 100.0, 1
                )
                resources.memory_used = (
                    resources.memory_total - resources.memory_available
                )
                resources.memory_used_percent = round(
                    (resources.memory_used / resources.memory_total) * 100.0, 1
                )
            resources.swap_used = resources.swap_total - resources.swap_free

        # 2. Load
        loadavg = results[1]
        if isinstance(loadavg, str) and loadavg:
            parts = loadavg.strip().split()
            if len(parts) >= 3:
                resources.load_1min = float(parts[0])
                resources.load_5min = float(parts[1])
                resources.load_15min = float(parts[2])
            if len(parts) >= 4:
                resources.processes = (
                    int(parts[3].split("/")[1]) if "/" in parts[3] else 0
                )

        # 3. Uptime
        uptime_str = results[2]
        if isinstance(uptime_str, str) and uptime_str:
            resources.uptime = int(float(uptime_str.strip().split()[0]))

        # 4. CPU usage from /proc/stat
        proc_stat = results[3]
        if isinstance(proc_stat, str) and proc_stat:
            resources.cpu_usage = self._calculate_cpu_usage(proc_stat)

        # 5. Storage
        df_output = results[4]
        if isinstance(df_output, str) and df_output:
            from ..base import StorageUsage

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

        # Memory fallback if needed (e.g. if /proc/meminfo was missing or empty)
        if resources.memory_total == 0:
            try:
                stdout = await self._exec("ubus call system info 2>/dev/null")
                if stdout and stdout.startswith("{"):
                    data = json.loads(stdout)
                    mem = data.get("memory", {})
                    resources.memory_total = mem.get("total", 0)
                    resources.memory_free = mem.get("free", 0)
                    resources.memory_cached = mem.get("cached", 0)
                    resources.memory_buffered = mem.get("buffered", 0)
                    resources.memory_used = (
                        resources.memory_total
                        - resources.memory_free
                        - resources.memory_cached
                        - resources.memory_buffered
                    )
            except Exception:  # noqa: BLE001
                pass

        # 5. CPU Frequency
        try:
            freq = await self._exec(
                "cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq 2>/dev/null"
            )
            if freq and freq.strip().isdigit():
                resources.cpu_frequency = int(freq.strip()) / 1000.0
        except Exception:
            pass

        # 6. Dynamic Thermal Zones
        await self._fetch_dynamic_thermals_ssh(resources)

        # 7. USB Device Discovery
        await self._fetch_usb_devices_ssh(resources)

        # 8. Top Processes Discovery
        await self._fetch_top_processes_ssh(resources)

        return resources
    async def _fetch_dynamic_thermals_ssh(self, resources: SystemResources) -> None:
        """Fetch thermal zones dynamically via SSH."""
        try:
            # Find all thermal zones
            zones_output = await self._exec(
                "ls -d /sys/class/thermal/thermal_zone* 2>/dev/null"
            )
            if zones_output:
                for zone_path in zones_output.strip().split():
                    zone_name = zone_path.split("/")[-1]
                    try:
                        temp_str = await self._exec(f"cat {zone_path}/temp 2>/dev/null")
                        if temp_str and temp_str.strip().isdigit():
                            temp_val = int(temp_str.strip())
                            # Handle mC vs C
                            val = (
                                temp_val / 1000.0
                                if temp_val > 1000
                                else float(temp_val)
                            )
                            resources.temperatures[zone_name] = val
                            if (
                                resources.temperature is None
                                or zone_name == "thermal_zone0"
                            ):
                                resources.temperature = val
                    except Exception:
                        continue

            # Fallback for hwmon
            if not resources.temperatures:
                hwmon_temp = await self._exec(
                    "cat /sys/class/hwmon/hwmon0/temp1_input 2>/dev/null"
                )
                if hwmon_temp and hwmon_temp.strip().isdigit():
                    val = int(hwmon_temp.strip()) / 1000.0
                    resources.temperature = val
                    resources.temperatures["hwmon0"] = val
        except Exception:
            pass
    async def _fetch_usb_devices_ssh(self, resources: SystemResources) -> None:
        """Fetch connected USB devices via SSH."""
        try:
            # Try verbose lsusb
            stdout = await self._exec("lsusb -v 2>/dev/null")
            if stdout:
                self._parse_lsusb_output(resources, stdout)
                return

            # Simple lsusb fallback
            stdout = await self._exec("lsusb 2>/dev/null")
            if stdout:
                for line in stdout.splitlines():
                    if not line.strip():
                        continue
                    parts = line.split(None, 6)
                    if len(parts) >= 6:
                        resources.usb_devices.append(
                            UsbDevice(
                                id=f"{parts[1]}:{parts[3].strip(':')}",
                                vendor_id=parts[5].split(":")[0],
                                product_id=parts[5].split(":")[1],
                                product=parts[6] if len(parts) > 6 else "",
                            )
                        )
        except Exception:
            pass
    def _parse_lsusb_output(self, resources: SystemResources, stdout: str) -> None:
        """Parse verbose lsusb output."""
        current_dev = None
        for line in stdout.splitlines():
            line = line.strip()
            if line.startswith("Bus "):
                parts = line.split()
                if len(parts) >= 4:
                    current_dev = UsbDevice(id=f"{parts[1]}:{parts[3].strip(':')}")
                    resources.usb_devices.append(current_dev)
                    if len(parts) >= 6:
                        ids = parts[5].split(":")
                        if len(ids) == 2:
                            current_dev.vendor_id = ids[0]
                            current_dev.product_id = ids[1]
            elif current_dev:
                if "iManufacturer" in line:
                    current_dev.manufacturer = line.split(None, 2)[-1]
                elif "iProduct" in line:
                    current_dev.product = line.split(None, 2)[-1]
                elif "iSerial" in line:
                    current_dev.serial = line.split(None, 2)[-1]
                elif "bDeviceClass" in line:
                    current_dev.class_name = line.split(None, 2)[-1]
    async def _fetch_top_processes_ssh(self, resources: SystemResources) -> None:
        """Fetch top CPU-consuming processes via SSH."""
        try:
            stdout = await self._exec("top -n 1 -b 2>/dev/null")
            if not stdout:
                return
            self._parse_top_output_ssh(resources, stdout)
        except Exception:
            pass
    def _parse_top_output_ssh(self, resources: SystemResources, stdout: str) -> None:
        """Parse busybox top output from SSH."""
        lines = stdout.splitlines()
        header_idx = -1
        for i, line in enumerate(lines):
            if "PID" in line and "COMMAND" in line:
                header_idx = i
                break

        if header_idx == -1 or header_idx + 1 >= len(lines):
            return

        header = lines[header_idx].split()
        try:
            pid_idx = header.index("PID")
            user_idx = header.index("USER")
            vsz_idx = header.index("VSZ")
            cpu_idx = header.index("%CPU")
            cmd_idx = header.index("COMMAND")
        except ValueError:
            return

        for line in lines[header_idx + 1 :]:
            parts = line.split()
            if len(parts) <= max(pid_idx, user_idx, vsz_idx, cpu_idx, cmd_idx):
                continue

            try:
                resources.top_processes.append(
                    ProcessInfo(
                        pid=int(parts[pid_idx]),
                        user=parts[user_idx],
                        vsz=(
                            int(parts[vsz_idx].rstrip("mGk"))
                            if parts[vsz_idx].rstrip("mGk").isdigit()
                            else 0
                        ),
                        cpu_usage=float(parts[cpu_idx].rstrip("%")),
                        command=" ".join(parts[cmd_idx:]),
                    )
                )
            except ValueError, IndexError:
                continue

            if len(resources.top_processes) >= 10:
                break
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
            if "connection refused" in err_str.lower() or "connect call failed" in err_str.lower() or "1225" in err_str:
                results.append(
                    DiagnosticResult(
                        name="SSH Service",
                        status="FAIL",
                        message="Connection refused by the router's SSH server.",
                        details=(
                            "The router's SSH service is not running or is blocking the connection on port 22 (or custom port). "
                            "Please check that dropbear/openssh is enabled and running on the router."
                        )
                    )
                )
            elif "unreachable" in err_str.lower() or "no route to host" in err_str.lower():
                results.append(
                    DiagnosticResult(
                        name="Host Reachability",
                        status="FAIL",
                        message="Host is unreachable.",
                        details=(
                            "The host is unreachable. Please verify the IP address or hostname is correct, "
                            "and that the router is online and connected to the network."
                        )
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
