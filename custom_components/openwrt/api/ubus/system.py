# mypy: disable-error-code="attr-defined"
from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from typing import Any

from ..base import (
    PROVISION_SCRIPT_TEMPLATE,
    DeviceInfo,
    ProcessInfo,
    SystemResources,
    UsbDevice,
)
from .exceptions import *

_LOGGER = logging.getLogger(__name__)
UBUS_JSONRPC_VERSION = "2.0"
UBUS_ID_AUTH = 1
UBUS_ID_CALL = 2


class UbusSystemMixin:
    """System methods for UbusClient."""

    async def get_device_info(self) -> DeviceInfo:
        """Get device information from system.board."""
        info = DeviceInfo()
        data = await self._call("system", "board")
        info.hostname = data.get("hostname", "")
        model = data.get("model")
        if isinstance(model, dict):
            info.model = str(model.get("name", model.get("id", info.model)))
        else:
            info.model = str(model or data.get("board_name", ""))
        info.board_name = data.get("board_name", "")
        info.kernel_version = data.get("kernel", "")
        info.architecture = data.get("system", "")

        release = data.get("release", {})
        info.release_distribution = release.get("distribution", "OpenWrt")
        info.release_version = release.get("version", "")
        info.release_revision = release.get("revision", "")
        info.target = release.get("target", data.get("board_name", ""))
        info.firmware_version = f"{info.release_version} ({info.release_revision})"

        # Fallback for model name via /tmp/sysinfo
        if not info.model:
            try:
                # Try reading via ubus file read if available
                model_data = await self._call(
                    "file", "read", {"path": "/tmp/sysinfo/model"}
                )
                if model_data and isinstance(model_data, dict):
                    info.model = model_data.get("data", "").strip()

                if not info.board_name:
                    board_data = await self._call(
                        "file", "read", {"path": "/tmp/sysinfo/board_name"}
                    )
                    if board_data and isinstance(board_data, dict):
                        info.board_name = board_data.get("data", "").strip()

                # Fallback 2: /etc/model
                if not info.model:
                    model_data = await self._call(
                        "file", "read", {"path": "/etc/model"}
                    )
                    if model_data and isinstance(model_data, dict):
                        info.model = model_data.get("data", "").strip()
            except Exception:
                pass

        try:
            sys_info = await self._call("system", "info")
            info.uptime = sys_info.get("uptime", 0)
            info.local_time = str(sys_info.get("localtime", ""))
        except UbusError:
            pass

        # Get MAC address from primary interface
        try:
            ifaces = await self.get_network_interfaces()
            for iface in ifaces:
                if iface.name == "lan" or iface.device == "br-lan":
                    info.mac_address = iface.mac_address
                    break
        except Exception:
            pass

        if not info.mac_address:
            # Robust fallback via shell command (often works even if ubus network fails)
            try:
                # We can't use 'self.execute_command' here as it's not in OpenWrtClient base,
                # but UbusClient has its own way to run commands if sys.exec is available.
                # Actually, ubus 'file' or 'sys' might work.
                # Let's use 'sys.exec' if available.
                sys_exec_out = await self._call(
                    "sys",
                    "exec",
                    {
                        "command": "cat /sys/class/net/br-lan/address 2>/dev/null || cat /sys/class/net/eth0/address 2>/dev/null",
                    },
                )
                if (
                    sys_exec_out
                    and isinstance(sys_exec_out, str)
                    and ":" in sys_exec_out
                ):
                    info.mac_address = sys_exec_out.strip().lower()
            except Exception:
                pass

        return info

    async def get_system_resources(self) -> SystemResources:
        """Get system resource usage."""
        resources = SystemResources()

        # Fetch resources in parallel where possible
        results = await asyncio.gather(
            self._call("system", "info"),
            self.execute_command("cat /proc/stat 2>/dev/null"),
            self._call("file", "read", {"path": "/proc/stat"}),
            return_exceptions=True,
        )

        # 1. System Info (Memory, Swap, Uptime, Load, and maybe CPU)
        data = results[0]
        if not isinstance(data, Exception) and isinstance(data, dict):
            self._parse_system_info(resources, data)

        # 2. Storage fallback via luci.getMountPoints
        if resources.filesystem_total == 0:
            await self._fetch_mount_points(resources)

        # 3. CPU usage fallback from /proc/stat
        if resources.cpu_usage == 0.0:
            self._fetch_cpu_usage(resources, results[1], results[2])

        # 4. Temperature fetching
        await self._fetch_temperature(resources)

        # 5. Detailed Storage monitoring via df
        await self._fetch_detailed_storage(resources)

        # 6. USB Device Discovery
        await self._fetch_usb_devices(resources)

        # 7. Top Processes Discovery
        await self._fetch_top_processes(resources)

        return resources

    def _parse_system_info(
        self, resources: SystemResources, data: dict[str, Any]
    ) -> None:
        """Parse core system information from ubus 'system info'."""
        # Memory parsing
        mem = data.get("memory", {})
        resources.memory_total = mem.get("total", 0)
        resources.memory_free = mem.get("free", 0)
        resources.memory_buffered = mem.get("buffered", 0)
        resources.memory_cached = mem.get("cached", 0)

        # Calculate available memory
        resources.memory_available = mem.get(
            "available",
            resources.memory_free + resources.memory_buffered + resources.memory_cached,
        )

        if resources.memory_total > 0:
            resources.memory_available_percent = round(
                (resources.memory_available / resources.memory_total) * 100.0, 1
            )
            resources.memory_used_percent = round(
                (
                    (resources.memory_total - resources.memory_available)
                    / resources.memory_total
                )
                * 100.0,
                1,
            )

        resources.memory_used = resources.memory_total - resources.memory_available

        # Swap parsing
        swap = data.get("swap", {})
        resources.swap_total = swap.get("total", 0)
        resources.swap_free = swap.get("free", 0)
        resources.swap_used = resources.swap_total - resources.swap_free

        resources.uptime = data.get("uptime", 0)

        # Thermal parsing from ubus
        thermal = data.get("thermal", {})
        if thermal and isinstance(thermal, dict):
            for zone, temp in thermal.items():
                if isinstance(temp, (int, float)):
                    # OpenWrt ubus typically reports in mC (milli-Celsius)
                    val = temp / 1000.0 if temp > 1000 else float(temp)
                    resources.temperatures[zone] = val
                    if resources.temperature is None or zone == "zone0":
                        resources.temperature = val

        # CPU frequency from ubus
        if "cpu" in data and isinstance(data["cpu"], dict):
            freq = data["cpu"].get("frequency")
            if isinstance(freq, (int, float)):
                resources.cpu_frequency = (
                    freq / 1000000.0 if freq > 10000 else float(freq)
                )

        # Load parsing
        load = data.get("load", [])
        if len(load) >= 3:
            # Some OpenWrt versions return load scaled by 65536, others as float
            if any(isinstance(val, int) and val > 500 for val in load):
                resources.load_1min = round(load[0] / 65536.0, 2)
                resources.load_5min = round(load[1] / 65536.0, 2)
                resources.load_15min = round(load[2] / 65536.0, 2)
            else:
                resources.load_1min = float(load[0])
                resources.load_5min = float(load[1])
                resources.load_15min = float(load[2])

        # Disk info from ubus if available
        self._parse_disk_info_ubus(resources, data)

        # Check if system info HAS a cpu field (common in some OpenWrt versions)
        if "cpu" in data and isinstance(data["cpu"], dict):
            cpu = data["cpu"]
            # Format it like /proc/stat line for _calculate_cpu_usage
            stat_line = (
                f"cpu  {cpu.get('user', 0)} {cpu.get('nice', 0)} "
                f"{cpu.get('system', 0)} {cpu.get('idle', 0)} "
                f"{cpu.get('iowait', 0)} {cpu.get('irq', 0)} "
                f"{cpu.get('softirq', 0)} {cpu.get('steal', 0)}"
            )
            resources.cpu_usage = self._calculate_cpu_usage(stat_line)

    def _parse_disk_info_ubus(
        self, resources: SystemResources, data: dict[str, Any]
    ) -> None:
        """Parse disk info if present in system info."""
        if "disk" in data:
            disk = data["disk"]
            root = disk.get("root", disk.get("/", {}))
            if isinstance(root, dict) and root.get("total"):
                resources.filesystem_total = root.get("total", 0)
                resources.filesystem_used = root.get("used", 0)
                resources.filesystem_free = (
                    resources.filesystem_total - resources.filesystem_used
                )

    async def _fetch_mount_points(self, resources: SystemResources) -> None:
        """Fallback to luci.getMountPoints for disk info."""
        with contextlib.suppress(Exception):
            mounts = await self._call("luci", "getMountPoints")
            if isinstance(mounts, dict) and "result" in mounts:
                for mount in mounts["result"]:
                    if mount.get("mount") in ("/", "/overlay"):
                        resources.filesystem_total = mount.get("size", 0)
                        resources.filesystem_free = mount.get("free", 0) or mount.get(
                            "avail", 0
                        )
                        resources.filesystem_used = (
                            resources.filesystem_total - resources.filesystem_free
                        )
                        break

    def _fetch_cpu_usage(
        self, resources: SystemResources, cmd_res: Any, file_res: Any
    ) -> None:
        """Calculate CPU usage from proc stat fallback results."""
        # Try Priority 2: file.read results
        if (
            not isinstance(file_res, Exception)
            and isinstance(file_res, dict)
            and file_res.get("data")
        ):
            resources.cpu_usage = self._calculate_cpu_usage(file_res["data"])

        # Try Priority 3: command execution results
        if (
            resources.cpu_usage == 0.0
            and not isinstance(cmd_res, Exception)
            and cmd_res
        ):
            resources.cpu_usage = self._calculate_cpu_usage(cmd_res)

    async def _fetch_temperature(self, resources: SystemResources) -> None:
        """Fetch system temperature from known sysfs paths."""
        temp_paths = [
            "/sys/class/thermal/thermal_zone0/temp",
            "/sys/class/thermal/thermal_zone1/temp",
            "/sys/class/thermal/thermal_zone2/temp",
            "/sys/class/hwmon/hwmon0/temp1_input",
            "/sys/class/hwmon/hwmon1/temp1_input",
            "/sys/class/hwmon/hwmon2/temp1_input",
            "/sys/devices/virtual/thermal/thermal_zone0/temp",
        ]

        # 1. Try ubus file read (RPC restricted)
        for path in temp_paths:
            with contextlib.suppress(Exception):
                res = await self._call("file", "read", {"path": path})
                if res and isinstance(res, dict) and res.get("data"):
                    self._parse_temp_raw(resources, res.get("data", ""), path)

        # 2. Fallback to shell command
        if not resources.temperatures:
            for path in temp_paths:
                with contextlib.suppress(Exception):
                    temp_raw = await self.execute_command(f"cat {path} 2>/dev/null")
                    if temp_raw:
                        self._parse_temp_raw(resources, temp_raw, path)

    def _parse_temp_raw(
        self, resources: SystemResources, raw: str, path: str = ""
    ) -> bool:
        """Parse raw temperature string into resources."""

        match = re.search(r"(\d+)", raw)
        if match:
            temp = float(match.group(1))
            if temp > 200:  # millidegrees
                temp /= 1000.0
            if 0 < temp < 150:
                if resources.temperature is None:
                    resources.temperature = temp

                # Add to multi-zone dict
                name = "System"
                if "thermal_zone" in path:
                    zone = path.split("thermal_zone")[-1].split("/")[0]
                    name = f"Zone {zone}"
                elif "hwmon" in path:
                    hw = path.split("hwmon")[-1].split("/")[0]
                    name = f"Hwmon {hw}"

                resources.temperatures[name] = temp
                return True
        return False

    async def _fetch_detailed_storage(self, resources: SystemResources) -> None:
        """Fetch detailed storage usage via 'df' command."""
        with contextlib.suppress(Exception):
            df_output = await self.execute_command("df -Pk 2>/dev/null")
            if df_output:
                from ..base import StorageUsage

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
                                self._update_legacy_fs_fields(resources, usage)
                            except (ValueError, IndexError):
                                continue

    def _update_legacy_fs_fields(self, resources: SystemResources, usage: Any) -> None:
        """Keep legacy filesystem fields updated for backward compatibility."""
        if resources.filesystem_total == 0 and usage.mount_point in ("/", "/overlay"):
            resources.filesystem_total = usage.total
            resources.filesystem_used = usage.used
            resources.filesystem_free = usage.free
        elif usage.mount_point == "/overlay":
            # Overlay has priority for root filesystem metrics
            resources.filesystem_total = usage.total
            resources.filesystem_used = usage.used
            resources.filesystem_free = usage.free

    async def _fetch_usb_devices(self, resources: SystemResources) -> None:
        """Fetch connected USB devices using lsusb or sysfs."""
        try:
            # Try lsusb via file.exec if available (hardened provisioning usually allows it)
            res = await self._call(
                "file", "exec", {"command": "/usr/bin/lsusb", "params": ["-v"]}
            )
            if res and res.get("code") == 0:
                self._parse_lsusb_output(resources, res.get("stdout", ""))
                return

            # Fallback: simple lsusb
            res = await self._call("file", "exec", {"command": "/usr/bin/lsusb"})
            if res and res.get("code") == 0:
                for line in res.get("stdout", "").splitlines():
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

    async def _fetch_top_processes(self, resources: SystemResources) -> None:
        """Fetch top CPU-consuming processes."""
        try:
            # We use top -n 1 -b to get a single batch output
            stdout = await self.execute_command("top -n 1 -b 2>/dev/null")
            if not stdout:
                return

            self._parse_top_output(resources, stdout)
        except Exception:
            pass

    def _parse_top_output(self, resources: SystemResources, stdout: str) -> None:
        """Parse busybox top output."""
        lines = stdout.splitlines()
        # Find the header line
        header_idx = -1
        for i, line in enumerate(lines):
            if "PID" in line and "COMMAND" in line:
                header_idx = i
                break

        if header_idx == -1 or header_idx + 1 >= len(lines):
            return

        # Busybox top columns: PID  PPID USER     STAT   VSZ %VSZ %CPU COMMAND
        # Some versions might differ. We try to be flexible.
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
            except (ValueError, IndexError):
                continue

            # Only keep top 10
            if len(resources.top_processes) >= 10:
                break

    async def get_system_logs(self, count: int = 10) -> list[str]:
        """Get recent system log entries via execute_command (logread)."""
        try:
            # Directly use execute_command (file.exec) with logread
            # Calling direct ubus log.read via JSON-RPC causes uhttpd spam on certain devices
            cmd = await self._get_logread_command(count)
            output = await self.execute_command(cmd)
            if output:
                return [line.strip() for line in output.splitlines() if line.strip()]
        except Exception as err:
            _LOGGER.debug("Failed to get system logs via ubus: %s", err)
        return []

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

    async def install_firmware(self, url: str, keep_settings: bool = True) -> None:
        """Install firmware from the given URL via ubus."""
        keep = "" if keep_settings else "-n"
        cmd = (
            f"wget -O /tmp/firmware.bin '{url}' && sysupgrade {keep} /tmp/firmware.bin"
        )
        try:
            _LOGGER.info("Initiating firmware installation via ubus from: %s", url)
            await self.execute_command(cmd)
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

            # ubus file.read returns base64 encoded data (in "data" key)
            res = await self._call("file", "read", {"path": remote_path})
            if res and isinstance(res, dict) and "data" in res:
                with open(local_path, "wb") as f:
                    f.write(base64.b64decode(res["data"]))
                return True
        except Exception as err:
            _LOGGER.exception("Failed to download file via ubus: %s", err)
        return False
