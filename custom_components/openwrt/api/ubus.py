"""OpenWrt ubus HTTP/HTTPS API client.

Communicates with OpenWrt via the ubus JSON-RPC interface exposed through
uhttpd. This is the recommended and most feature-complete connection method.

Requires packages on OpenWrt: uhttpd, uhttpd-mod-ubus, rpcd, rpcd-mod-iwinfo
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

import aiohttp

from .base import (
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
    IpNeighbor,
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
    WpsStatus,
)

_LOGGER = logging.getLogger(__name__)

UBUS_JSONRPC_VERSION = "2.0"
UBUS_ID_AUTH = 1
UBUS_ID_CALL = 2


class UbusError(Exception):
    """Error communicating with ubus."""


class UbusAuthError(UbusError):
    """Authentication error."""


class UbusTimeoutError(UbusError):
    """Connection or request timeout."""


class UbusConnectionError(UbusError):
    """TCP connection failure (e.g. refused, unreachable)."""


class UbusSslError(UbusError):
    """SSL/TLS verification failure."""


class UbusPackageMissingError(UbusError):
    """Required package missing (e.g. 404 on /ubus)."""


class UbusPermissionError(UbusError):
    """Insufficient RPC permissions (e.g. 403 or ACL error). Consider switching to LuCI RPC for better accessibility."""


class UbusClient(OpenWrtClient):
    """Client for OpenWrt ubus JSON-RPC API."""

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        port: int = 80,
        use_ssl: bool = False,
        verify_ssl: bool = False,
        ubus_path: str = "/ubus",
        dhcp_software: str = "auto",
    ) -> None:
        """Initialize the ubus client."""
        super().__init__(
            host,
            username,
            password,
            port,
            use_ssl,
            verify_ssl,
            dhcp_software,
        )
        self._ubus_path = ubus_path
        self._session_id: str = "00000000000000000000000000000000"
        self._session: aiohttp.ClientSession | None = None
        self._semaphore = asyncio.Semaphore(
            5
        )  # Limit concurrent RPC calls to avoid overloading uhttpd

    @property
    def _base_url(self) -> str:
        """Return base URL for ubus endpoint."""
        scheme = "https" if self.use_ssl else "http"
        return f"{scheme}://{self.host}:{self.port}{self._ubus_path}"

    def _build_request(
        self,
        method: str,
        params: list[Any] | dict[str, Any],
        request_id: int = UBUS_ID_CALL,
    ) -> dict[str, Any]:
        """Build a JSON-RPC request payload."""
        return {
            "jsonrpc": UBUS_JSONRPC_VERSION,
            "id": request_id,
            "method": method,
            "params": params,
        }

    async def _ensure_session(self) -> aiohttp.ClientSession:
        """Ensure an aiohttp session exists."""
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=30)
            connector = aiohttp.TCPConnector(
                ssl=self.verify_ssl if self.use_ssl else False,
            )
            self._session = aiohttp.ClientSession(
                timeout=timeout,
                connector=connector,
            )
        return self._session

    async def _call(
        self,
        ubus_object: str,
        ubus_method: str,
        params: dict[str, Any] | None = None,
        reauthenticated: bool = False,
    ) -> dict[str, Any]:
        """Make a ubus call."""
        session = await self._ensure_session()
        payload = self._build_request(
            "call",
            [self._session_id, ubus_object, ubus_method, params or {}],
        )

        reauth_needed = False
        data: dict[str, Any] = {}
        try:
            async with self._semaphore:
                async with session.post(
                    self._base_url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                ) as response:
                    response.raise_for_status()
                    data = await response.json()

                    # Check for session expiration in ubus response
                    if (
                        data.get("result")
                        and isinstance(data["result"], list)
                        and len(data["result"]) > 0
                        and data["result"][0] == 6  # Permission denied/Session expired
                    ):
                        reauth_needed = True

            if reauth_needed and not reauthenticated:
                _LOGGER.debug("Ubus session expired, re-authenticating...")
                self._session_id = "00000000000000000000000000000000"
                await self.connect()
                return await self._call(
                    ubus_object,
                    ubus_method,
                    params,
                    reauthenticated=True,
                )

        except TimeoutError as err:
            msg = f"Timeout communicating with {self.host}"
            raise UbusTimeoutError(msg) from err
        except aiohttp.ClientConnectorError as err:
            msg = (
                f"Cannot connect to {self.host}. Is the IP correct and uhttpd running?"
            )
            raise UbusConnectionError(
                msg,
            ) from err
        except aiohttp.ClientSSLError as err:
            msg = f"SSL verification failed for {self.host}. Try disabling 'Verify SSL Certificate' if you use a self-signed one."
            raise UbusSslError(
                msg,
            ) from err
        except aiohttp.ClientResponseError as err:
            if err.status == 404:
                msg = f"Ubus endpoint not found on {self.host}. Is 'uhttpd-mod-ubus' installed?"
                raise UbusPackageMissingError(
                    msg,
                ) from err
            if err.status == 403:
                msg = f"Access denied to ubus on {self.host}. Check RPC permissions or switch to LuCI RPC."
                raise UbusPermissionError(
                    msg,
                ) from err
            msg = f"HTTP error {err.status} from {self.host}"
            raise UbusError(msg) from err
        except aiohttp.ClientError as err:
            if not reauthenticated:
                _LOGGER.debug(
                    "Ubus connection error (%s), retrying after session reset",
                    err,
                )
                await self.disconnect()
                return await self._call(
                    ubus_object,
                    ubus_method,
                    params,
                    reauthenticated=True,
                )
            self._connected = False
            msg = f"Communication error: {err}"
            raise UbusError(msg) from err
        except Exception as err:
            if isinstance(err, (UbusError, asyncio.CancelledError)):
                raise
            msg = f"Unexpected error communicating with {self.host}: {err}"
            raise UbusError(msg) from err

        if "result" not in data:
            msg = f"Unexpected response: {data}"
            raise UbusError(msg)

        result = data["result"]

        if isinstance(result, list):
            code = result[0] if result else -1
            if code == 6:  # Permission denied / Session expired
                # This should have been handled by the reauth logic above,
                # but if we get here, it's a real permission issue
                msg = f"Access denied to ubus on {self.host} (code 6)"
                raise UbusPermissionError(msg)
            if code == 0:
                return result[1] if len(result) > 1 else {}
            msg = f"Ubus call failed with code {code}"
            raise UbusError(msg)

        return result

    async def _list_objects(self) -> list[str]:
        """List available ubus objects."""
        session = await self._ensure_session()
        if not self._connected:
            await self.connect()

        token = self._session_id
        # The ubus JSON-RPC 'list' method requires [token, "*"] to return all
        # objects. Using only [token] returns an empty dict on most firmwares.
        payload = self._build_request(
            "list",
            [token, "*"],
            request_id=UBUS_ID_CALL,
        )

        try:
            async with session.post(
                self._base_url,
                json=payload,
                headers={"Content-Type": "application/json"},
            ) as response:
                response.raise_for_status()
                data = await response.json()
        except Exception as err:
            _LOGGER.debug("Failed to list ubus objects: %s", err)
            return []

        result = data.get("result")
        if not result or not isinstance(result, list):
            return []

        # On success, result is a list with one dict: [{"object1": {...}, "object2": {...}}]
        return list(result[0].keys())

    async def _get_object_methods(self, object_name: str) -> dict[str, Any]:
        """Get methods for a specific ubus object."""
        session = await self._ensure_session()
        token = self._session_id
        payload = self._build_request(
            "list",
            [token, object_name],
            request_id=UBUS_ID_CALL,
        )
        try:
            async with session.post(
                self._base_url,
                json=payload,
                headers={"Content-Type": "application/json"},
            ) as response:
                response.raise_for_status()
                data = await response.json()
                result = data.get("result")
                if result and isinstance(result, list) and len(result) > 0:
                    return result[0].get(object_name, {})
        except Exception:
            pass
        return {}

    async def connect(self) -> bool:
        """Authenticate with the ubus RPC endpoint."""
        session = await self._ensure_session()
        payload = self._build_request(
            "call",
            [
                "00000000000000000000000000000000",
                "session",
                "login",
                {"username": self.username, "password": self.password},
            ],
            request_id=UBUS_ID_AUTH,
        )

        try:
            async with session.post(
                self._base_url,
                json=payload,
                headers={"Content-Type": "application/json"},
            ) as response:
                response.raise_for_status()
                data = await response.json()
        except TimeoutError as err:
            msg = f"Login timeout for {self.host}"
            raise UbusTimeoutError(msg) from err
        except aiohttp.ClientConnectorError as err:
            msg = f"Cannot connect to {self.host}: {err}"
            raise UbusConnectionError(msg) from err
        except aiohttp.ClientSSLError as err:
            msg = f"SSL error connecting to {self.host}: {err}"
            raise UbusSslError(msg) from err
        except aiohttp.ClientResponseError as err:
            if err.status == 404:
                msg = f"Ubus endpoint not found on {self.host}. Is 'uhttpd-mod-ubus' installed?"
                raise UbusPackageMissingError(
                    msg,
                ) from err
            msg = f"HTTP error {err.status} during login: {err}"
            raise UbusError(msg) from err
        except aiohttp.ClientError as err:
            msg = f"Cannot connect to {self.host}: {err}"
            raise UbusError(msg) from err

        result = data.get("result")
        if (
            result is None
            or (isinstance(result, list) and not result)
            or (isinstance(result, list) and result[0] != 0)
        ):
            _LOGGER.error("Ubus auth failed: %s", data)
            msg = f"Authentication failed for {self.username}@{self.host}. Check credentials."
            raise UbusAuthError(
                msg,
            )

        if isinstance(result, list) and len(result) > 1:
            self._session_id = result[1].get("ubus_rpc_session", "")
        else:
            msg = "No session ID in auth response"
            raise UbusAuthError(msg)

        self._connected = True
        _LOGGER.debug(
            "Authenticated with %s, session: %s...",
            self.host,
            self._session_id[:8],
        )
        return True

    async def disconnect(self) -> None:
        """Disconnect and cleanup."""
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None
        self._connected = False

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
        import re

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
                from .base import StorageUsage

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
                            except ValueError, IndexError:
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
                        vsz=int(parts[vsz_idx].rstrip("mGk"))
                        if parts[vsz_idx].rstrip("mGk").isdigit()
                        else 0,
                        cpu_usage=float(parts[cpu_idx].rstrip("%")),
                        command=" ".join(parts[cmd_idx:]),
                    )
                )
            except ValueError, IndexError:
                continue

            # Only keep top 10
            if len(resources.top_processes) >= 10:
                break

    async def get_external_ip(self) -> str | None:
        """Get the external IP address from the WAN interface."""
        status = await self._call("network.interface", "dump")
        for iface_data in status.get("interface", []):
            iface_name = iface_data.get("interface", "").lower()
            if iface_name in ["wan", "wan6", "wwan", "modem"]:
                ipv4_addrs = iface_data.get("ipv4-address", [])
                if ipv4_addrs:
                    return ipv4_addrs[0].get("address")
        return None

    async def get_wireless_interfaces(self) -> list[WirelessInterface]:
        """Get wireless interface information."""
        interfaces: list[WirelessInterface] = []
        iface_names: set[str] = set()

        # 1. Primary source: network.wireless status
        if self.packages.wireless is not False:
            try:
                wireless_data = await self._call("network.wireless", "status")
                if wireless_data and isinstance(wireless_data, dict):
                    for radio_name, radio_data in wireless_data.items():
                        if not isinstance(radio_data, dict):
                            continue

                        radio_interfaces = radio_data.get("interfaces", [])
                        for iface in radio_interfaces:
                            # Prefer the actual kernel interface name (ifname/device)
                            # over the UCI section name. On devices like the Velop WHW03
                            # that use phy*-ap* naming, the section field (e.g.
                            # "default_radio0") differs from the actual device name
                            # (e.g. "phy0-ap0"). Using section as the primary name
                            # prevents the iwinfo step from recognising the real device
                            # name as "already seen", causing duplicate entries.
                            section = iface.get("section", "")
                            ifname = (
                                iface.get("ifname")
                                or iface.get("device", "")
                            )
                            # Use the actual kernel name if available; fall back to
                            # the UCI section name only when no kernel name exists.
                            iface_name = ifname or section
                            if not iface_name:
                                continue

                            iface_config = iface.get("config", {})
                            wifi = WirelessInterface(
                                name=iface_name,
                                ssid=iface_config.get("ssid", ""),
                                mode=iface_config.get("mode", ""),
                                encryption=iface_config.get("encryption", ""),
                                enabled=not radio_data.get("disabled", False),
                                up=radio_data.get("up", False),
                                radio=radio_name,
                                htmode=radio_data.get("config", {}).get("htmode", ""),
                                hwmode=radio_data.get("config", {}).get("hwmode", ""),
                                txpower=radio_data.get("config", {}).get("txpower", 0),
                                mesh_id=iface_config.get("mesh_id", ""),
                                mesh_fwding=iface_config.get("mesh_fwding", False),
                                section=section,
                                ifname=ifname,
                            )
                            interfaces.append(wifi)
                            # Track both the kernel name and the UCI section name so
                            # the iwinfo step does not create a second entry for the
                            # same physical interface under a different name.
                            iface_names.add(iface_name)
                            if section and section != iface_name:
                                iface_names.add(section)
                            if ifname and ifname != iface_name:
                                iface_names.add(ifname)
            except UbusError:
                _LOGGER.debug(
                    "network.wireless status call failed, trying UCI fallback"
                )
            try:
                uci_wireless = await self._call("uci", "get", {"config": "wireless"})
                if (
                    uci_wireless
                    and isinstance(uci_wireless, dict)
                    and "values" in uci_wireless
                ):
                    vals = uci_wireless["values"]
                    for sect_name, sect_data in vals.items():
                        if sect_data.get(".type") != "wifi-iface":
                            continue

                        iface_name = sect_data.get("ifname") or sect_name
                        radio_name = sect_data.get("device", "")

                        # Get radio status to determine if enabled
                        radio_disabled = (
                            vals.get(radio_name, {}).get("disabled", "0") == "1"
                        )
                        iface_disabled = sect_data.get("disabled", "0") == "1"

                        wifi = WirelessInterface(
                            name=iface_name,
                            ssid=sect_data.get("ssid", ""),
                            mode=sect_data.get("mode", ""),
                            encryption=sect_data.get("encryption", ""),
                            enabled=not (radio_disabled or iface_disabled),
                            up=not (radio_disabled or iface_disabled),
                            radio=radio_name,
                        )
                        interfaces.append(wifi)
                        iface_names.add(iface_name)
            except Exception as e:
                _LOGGER.debug("UCI wireless fallback failed: %s", e)

        # 2. Supplement/Fallback: iwinfo devices
        # This is critical for devices like Velop where interfaces aren't always in network.wireless
        try:
            iw_devs = await self._call("iwinfo", "devices")
            candidates = []
            if isinstance(iw_devs, list):
                candidates = iw_devs
            elif isinstance(iw_devs, dict) and "devices" in iw_devs:
                candidates = iw_devs["devices"]

            for iface_name in candidates:
                if iface_name in iface_names:
                    continue

                # Found a new interface not in UCI status
                wifi = WirelessInterface(name=iface_name, enabled=True, up=True)
                interfaces.append(wifi)
                iface_names.add(iface_name)
        except UbusError:
            _LOGGER.debug("iwinfo devices call failed")

        # 3. Populate metrics for all discovered interfaces in parallel
        async def _fetch_metrics(wifi: WirelessInterface) -> None:
            try:
                iwinfo = await self._call("iwinfo", "info", {"device": wifi.name})
                if iwinfo:
                    if not wifi.ssid:
                        wifi.ssid = iwinfo.get("ssid", "")
                    wifi.mac_address = iwinfo.get("bssid", "").upper()
                    wifi.channel = iwinfo.get("channel", 0)
                    wifi.frequency = str(iwinfo.get("frequency", ""))

                    # Fallback: Infer from channel if frequency is missing or empty
                    if (
                        not wifi.frequency or wifi.frequency == "None"
                    ) and wifi.channel > 0:
                        if 1 <= wifi.channel <= 14:
                            wifi.frequency = "2.4 GHz"
                        elif 32 <= wifi.channel <= 177:
                            wifi.frequency = "5 GHz"
                        elif 182 <= wifi.channel <= 196:
                            wifi.frequency = "6 GHz"

                    wifi.signal = iwinfo.get("signal", 0)
                    wifi.noise = iwinfo.get("noise", 0)
                    wifi.bitrate = (
                        iwinfo.get("bitrate", 0) / 1000.0
                        if iwinfo.get("bitrate")
                        else 0.0
                    )
                    q_val = iwinfo.get("quality")
                    q_max = iwinfo.get("quality_max", 100)
                    if q_val is not None and q_max:
                        wifi.quality = round((q_val / q_max) * 100, 1)

                    if "hwmode" in iwinfo and not wifi.hwmode:
                        if isinstance(iwinfo["hwmode"], list):
                            wifi.hwmode = "/".join(iwinfo["hwmode"])
                        else:
                            wifi.hwmode = str(iwinfo["hwmode"])
                    if "htmode" in iwinfo and not wifi.htmode:
                        wifi.htmode = str(iwinfo["htmode"])

                # Association list
                assoc = await self._call("iwinfo", "assoclist", {"device": wifi.name})
                if assoc:
                    wifi.clients_count = len(assoc.get("results", []))

                if not wifi.clients_count:
                    with contextlib.suppress(Exception):
                        hostapd_clients = await self._call(
                            f"hostapd.{wifi.name}", "get_clients"
                        )
                        if hostapd_clients and isinstance(hostapd_clients, dict):
                            clients = hostapd_clients.get("clients", {})
                            count = sum(
                                1
                                for c in clients.values()
                                if isinstance(c, dict) and c.get("authorized", True)
                            )
                            if count > 0:
                                wifi.clients_count = count

            except UbusError:
                _LOGGER.debug(
                    "Failed to fetch detailed info for wifi interface %s", wifi.name
                )

        if interfaces:
            await asyncio.gather(*[_fetch_metrics(w) for w in interfaces])

        # 4. Deduplicate and clean up
        unique_ifaces: list[WirelessInterface] = []
        seen_keys: set[str] = set()

        for wifi in interfaces:
            # Skip interfaces that are clearly not operational or redundant placeholders
            if not wifi.mac_address and not wifi.ssid:
                _LOGGER.debug(
                    "Skipping non-operational wireless interface: %s", wifi.name
                )
                continue

            # Create a key for deduplication
            if wifi.mac_address:
                key = f"mac_{wifi.mac_address}"
            elif wifi.ssid and wifi.radio:
                key = f"ssid_radio_{wifi.ssid}_{wifi.radio}"
            elif wifi.section:
                key = f"section_{wifi.section}"
            else:
                key = f"name_{wifi.name}"

            if key not in seen_keys:
                unique_ifaces.append(wifi)
                seen_keys.add(key)
            else:
                # Merge data if this one has more info
                for existing in unique_ifaces:
                    if (
                        wifi.mac_address and existing.mac_address == wifi.mac_address
                    ) or (
                        wifi.ssid
                        and wifi.radio
                        and existing.ssid == wifi.ssid
                        and existing.radio == wifi.radio
                    ):
                        if not existing.ssid:
                            existing.ssid = wifi.ssid
                        if not existing.mac_address:
                            existing.mac_address = wifi.mac_address
                        if wifi.clients_count > 0:
                            existing.clients_count = wifi.clients_count
                        break

        return unique_ifaces

    async def get_upnp_mappings(self) -> list[UpnpMapping]:
        """Get active UPnP/NAT-PMP port mappings via ubus."""
        mappings: list[UpnpMapping] = []
        try:
            res = await self._call("upnp", "get_mappings")
            if not isinstance(res, dict) or "mappings" not in res:
                return mappings

            for m in res["mappings"]:
                mappings.append(
                    UpnpMapping(
                        protocol=m.get("protocol", "TCP").upper(),
                        external_port=int(m.get("ext_port", 0)),
                        internal_ip=m.get("int_addr", ""),
                        internal_port=int(m.get("int_port", 0)),
                        description=m.get("descr", ""),
                        enabled=bool(m.get("enabled", True)),
                    )
                )
        except UbusError:
            pass  # upnp object might not exist
        except Exception as err:
            _LOGGER.debug("Failed to fetch UPnP mappings: %s", err)

        return mappings

    async def get_wireguard_interfaces(self) -> list[WireGuardInterface]:
        """Get WireGuard VPN interface and peer information via ubus/CLI."""
        interfaces: list[WireGuardInterface] = []
        try:
            # 1. Discover WG interfaces via network.interface dump
            status = await self._call("network.interface", "dump")
            if not isinstance(status, dict):
                return interfaces

            wg_ifaces: dict[str, bool] = {}
            for iface_data in status.get("interface", []):
                if iface_data.get("proto") == "wireguard":
                    wg_ifaces[iface_data.get("interface")] = bool(iface_data.get("up"))

            if not wg_ifaces:
                return interfaces

            # 2. Fetch peer info via wg show dump
            # wg show all dump format:
            # interface public_key listen_port fwmark
            # peer_public_key preshared_key endpoint allowed_ips latest_handshake transfer_rx transfer_tx persistent_keepalive

            stdout = await self.execute_command("wg show all dump 2>/dev/null")
            if not stdout:
                return interfaces

            iface_map: dict[str, WireGuardInterface] = {}
            for line in stdout.splitlines():
                parts = line.split("\t")
                if len(parts) == 4:
                    # Interface line
                    ifname = parts[0]
                    if ifname not in wg_ifaces:
                        continue
                    iface = WireGuardInterface(
                        name=ifname,
                        enabled=wg_ifaces[ifname],
                        public_key=parts[1],
                        listen_port=int(parts[2]) if parts[2].isdigit() else 0,
                        fwmark=int(parts[3]) if parts[3].isdigit() else 0,
                    )
                    iface_map[ifname] = iface
                    interfaces.append(iface)
                elif len(parts) >= 8:
                    # Peer line
                    # Format: interface peer_public_key preshared_key endpoint allowed_ips latest_handshake transfer_rx transfer_tx persistent_keepalive
                    # Wait, 'wg show all dump' includes the interface name as the first part for peers too?
                    # Let's check 'wg show dump' output:
                    # Interface: interface public_key listen_port fwmark
                    # Peer: peer_public_key preshared_key endpoint allowed_ips latest_handshake transfer_rx transfer_tx persistent_keepalive
                    # If using 'all', it's:
                    # interface peer_public_key preshared_key endpoint allowed_ips latest_handshake transfer_rx transfer_tx persistent_keepalive

                    ifname = parts[0]
                    if ifname in iface_map:
                        peer = WireGuardPeer(
                            public_key=parts[1],
                            endpoint=parts[3] if parts[3] != "(none)" else "",
                            allowed_ips=parts[4].split(",")
                            if parts[4] != "(none)"
                            else [],
                            latest_handshake=int(parts[5]) if parts[5].isdigit() else 0,
                            transfer_rx=int(parts[6]) if parts[6].isdigit() else 0,
                            transfer_tx=int(parts[7]) if parts[7].isdigit() else 0,
                            persistent_keepalive=int(parts[8])
                            if len(parts) > 8 and parts[8].isdigit()
                            else 0,
                        )
                        iface_map[ifname].peers.append(peer)
        except Exception as err:
            _LOGGER.debug("Failed to fetch WireGuard interfaces: %s", err)

        return interfaces

    async def get_network_interfaces(self) -> list[NetworkInterface]:
        """Get network interface information."""
        interfaces: list[NetworkInterface] = []

        try:
            status = await self._call("network.interface", "dump")
        except UbusError:
            return interfaces

        # 2. Fetch all device statistics and link status in one call (efficient)
        device_stats = {}
        try:
            device_stats = await self._call("network.device", "status")
        except UbusError:
            _LOGGER.debug("Failed to fetch all network device stats")

        for iface_data in status.get("interface", []):
            iface = NetworkInterface(
                name=iface_data.get("interface", ""),
                up=iface_data.get("up", False),
                protocol=iface_data.get("proto", ""),
                device=iface_data.get("l3_device", iface_data.get("device", "")),
                uptime=iface_data.get("uptime", 0),
            )

            ipv4_addrs = iface_data.get("ipv4-address", [])
            if ipv4_addrs:
                iface.ipv4_address = ipv4_addrs[0].get("address", "")

            ipv6_addrs = iface_data.get("ipv6-address", [])
            if ipv6_addrs:
                iface.ipv6_address = ipv6_addrs[0].get("address", "")

            iface.dns_servers = iface_data.get("dns-server", [])
            iface.ipv6_prefix = [
                p.get("address", "") for p in iface_data.get("ipv6-prefix", [])
            ]
            iface.ipv6_prefix_assignment = iface_data.get("ipv6-prefix-assignment", [])

            # Apply statistics and link status
            dev_name = iface.device
            if dev_name and dev_name in device_stats:
                dev_status = device_stats[dev_name]
                iface.is_link_up = dev_status.get("link", False)
                iface.link_speed = dev_status.get("speed", 0)
                iface.link_duplex = "full" if dev_status.get("full_duplex") else "half"

                stats = dev_status.get("statistics", {})
                iface.rx_bytes = stats.get("rx_bytes", 0)
                iface.tx_bytes = stats.get("tx_bytes", 0)
                iface.rx_packets = stats.get("rx_packets", 0)
                iface.tx_packets = stats.get("tx_packets", 0)
                iface.rx_errors = stats.get("rx_errors", 0)
                iface.tx_errors = stats.get("tx_errors", 0)
                iface.rx_dropped = stats.get("rx_dropped", 0)
                iface.tx_dropped = stats.get("tx_dropped", 0)
                iface.collisions = stats.get("collisions", 0)
                iface.multicast = stats.get("multicast", 0)
                iface.mac_address = dev_status.get("macaddr", "")
                iface.speed = (
                    str(iface.link_speed)
                    if iface.link_speed
                    else dev_status.get("speed", "")
                )

            interfaces.append(iface)

        return interfaces

    async def get_connected_devices(self) -> list[ConnectedDevice]:
        """Get connected devices by combining DHCP leases, ARP, and wireless clients."""
        devices: dict[str, ConnectedDevice] = {}

        # 1. Start with initial device list from DHCP leases (active and static)
        await self._get_devices_from_dhcp(devices)
        await self._get_devices_from_static_leases(devices)

        # 2. Get wireless status for iwinfo and hostapd processing
        wireless_data: dict[str, Any] = {}
        if self.packages.wireless is not False:
            with contextlib.suppress(UbusError):
                wireless_data = await self._call("network.wireless", "status")

        # 3. Process wireless associations (iwinfo)
        if wireless_data:
            await self._process_iwinfo_assoc(devices, wireless_data)
        else:
            # Fallback: scan all interfaces for iwinfo if network.wireless is missing
            await self._process_iwinfo_fallback(devices)

        # 4. Process IP neighbor (ARP/NDP) findings
        await self._merge_neighbor_data(devices)

        # 5. Process wireless client details (hostapd)
        if wireless_data and self.packages.wireless is not False:
            await self._process_hostapd_clients(devices, wireless_data)

        # 6. Supplemental source: Bridge FDB (Forwarding Database)
        # This helps identifying which physical port a wired device is on
        await self._process_bridge_fdb(devices)

        # Always run fallback to ensure we catch any manually added or mesh interfaces
        if self.packages.wireless is not False:
            await self._process_hostapd_fallback(devices)

        # Final cleanup/standardization
        for dev in devices.values():
            if not dev.connection_type:
                dev.connection_type = "wireless" if dev.is_wireless else "wired"

        return list(devices.values())

    async def _get_devices_from_dhcp(self, devices: dict[str, ConnectedDevice]) -> None:
        """Populate initial device list from DHCP leases."""
        with contextlib.suppress(Exception):
            leases = await self.get_dhcp_leases()
            for lease in leases:
                mac = lease.mac.lower()
                devices[mac] = ConnectedDevice(
                    mac=mac,
                    ip=lease.ip,
                    hostname=lease.hostname,
                    is_wireless=False,
                    connected=False,
                )

    async def _process_iwinfo_assoc(
        self, devices: dict[str, ConnectedDevice], wireless_data: dict[str, Any]
    ) -> None:
        """Fetch and merge iwinfo association lists."""
        for radio_data in wireless_data.values():
            if not isinstance(radio_data, dict):
                continue
            for iface in radio_data.get("interfaces", []):
                ifname = iface.get("ifname") or iface.get("device", "")
                if not ifname:
                    continue
                with contextlib.suppress(UbusError):
                    assoc = await self._call("iwinfo", "assoclist", {"device": ifname})
                    for client in assoc.get("results", []):
                        mac = client.get("mac", "").lower()
                        dev = devices.setdefault(
                            mac, ConnectedDevice(mac=mac, connected=True)
                        )
                        dev.connected = True
                        dev.is_wireless = True
                        dev.interface = ifname
                        self._set_wireless_connection_type(dev, ifname)
                        dev.signal = client.get("signal", 0)
                        dev.noise = client.get("noise", 0)
                        dev.rx_rate = self._get_assoc_rate(client, "rx")
                        dev.tx_rate = self._get_assoc_rate(client, "tx")

    def _get_assoc_rate(self, client: dict[str, Any], direction: str) -> int:
        """Helper to safely extract wireless rate from assoclist data."""
        val = client.get(direction)
        if isinstance(val, dict):
            return val.get("rate", 0)
        return client.get(f"{direction}_rate", 0)

    async def _merge_neighbor_data(self, devices: dict[str, ConnectedDevice]) -> None:
        """Update devices with ARP/neighbor information."""
        with contextlib.suppress(Exception):
            neighbors = await self.get_ip_neighbors()
            # STALE is intentionally included: Linux kernels age ARP entries to
            # STALE very quickly (30-60 s).  A STALE entry means the device WAS
            # reachable and likely still is – it will transition back to REACHABLE
            # on the next unicast exchange.  Excluding STALE would cause wired
            # clients to disappear from the count even while actively using the
            # network.
            active_states = ("REACHABLE", "STALE", "DELAY", "PROBE", "PERMANENT")
            for neigh in neighbors:
                mac = neigh.mac.lower()
                if not mac:
                    continue
                if mac in devices:
                    dev = devices[mac]
                    dev.neighbor_state = dev.neighbor_state or neigh.state
                    dev.interface = dev.interface or neigh.interface
                    # Mark wired devices as connected when the kernel's ARP table
                    # shows a recent (/active) entry.
                    if not dev.is_wireless and neigh.state.upper() in active_states:
                        dev.connected = True
                else:
                    is_active = neigh.state.upper() in active_states
                    devices[mac] = ConnectedDevice(
                        mac=mac,
                        ip=neigh.ip,
                        interface=neigh.interface,
                        connected=is_active,
                        connection_type="wired",
                        neighbor_state=neigh.state,
                    )

    async def _process_hostapd_clients(
        self, devices: dict[str, ConnectedDevice], wireless_data: dict[str, Any]
    ) -> None:
        """Fetch and merge hostapd client details (bytes/counters)."""
        for radio_data in wireless_data.values():
            if not isinstance(radio_data, dict):
                continue
            for iface in radio_data.get("interfaces", []):
                ifname = iface.get("ifname", "")
                if not ifname:
                    continue
                with contextlib.suppress(UbusError):
                    hostapd_data = await self._call(f"hostapd.{ifname}", "get_clients")
                    self._merge_hostapd_clients(
                        devices, hostapd_data.get("clients", {}), ifname
                    )

    async def _get_devices_from_static_leases(
        self, devices: dict[str, ConnectedDevice]
    ) -> None:
        """Populate device list from static DHCP leases in UCI."""
        try:
            config = await self._call("uci", "get", {"config": "dhcp"})
            if not config or not isinstance(config, dict):
                return

            for _section, values in config.items():
                if values.get(".type") == "host":
                    macs = values.get("mac")
                    if not macs:
                        continue

                    # mac can be a space-separated string or a list
                    if isinstance(macs, str):
                        mac_list = macs.split()
                    else:
                        mac_list = macs

                    for mac in mac_list:
                        mac_lower = mac.lower()
                        if mac_lower not in devices:
                            devices[mac_lower] = ConnectedDevice(
                                mac=mac_lower,
                                ip=values.get("ip", ""),
                                hostname=values.get("name", ""),
                                is_wireless=False,
                                connected=False,
                            )
        except Exception:
            pass

    async def _process_bridge_fdb(self, devices: dict[str, ConnectedDevice]) -> None:
        """Fetch and merge bridge FDB (forwarding database) information."""
        try:
            # 1. Fetch all network devices to find bridges and members
            device_status = await self._call("network.device", "status")
            if not device_status or not isinstance(device_status, dict):
                return

            # 2. For each device, fetch its FDB if it's a bridge or has members
            for dev_name, dev_info in device_status.items():
                if not dev_info.get("up"):
                    continue

                try:
                    fdb = await self._call("network.device", "fdb", {"name": dev_name})
                    if fdb and isinstance(fdb, list):
                        for entry in fdb:
                            mac = entry.get("mac", "").lower()
                            if mac not in devices:
                                continue

                            dev = devices[mac]
                            # Only apply to wired devices or as supplemental info
                            port = entry.get("port", "")
                            if port:
                                dev.port = port
                                # If it's a wired device, we can improve its interface info
                                if not dev.is_wireless and not dev.interface:
                                    dev.interface = dev_name
                except Exception:
                    continue
        except Exception as err:
            _LOGGER.debug("Failed to fetch bridge FDB: %s", err)

    async def _process_iwinfo_fallback(
        self, devices: dict[str, ConnectedDevice]
    ) -> None:
        """Discover wireless interfaces from ubus object list and poll iwinfo."""
        with contextlib.suppress(Exception):
            objects = await self._list_objects()
            # On some devices, interfaces are named wlan0, wlan1, etc.
            # or have hostapd.wlan0 objects.
            candidates = set()
            for obj in objects:
                if obj.startswith("hostapd."):
                    candidates.add(obj.split(".", 1)[1])
                elif obj in ("iwinfo", "network.wireless"):
                    continue

            # Also try to discover candidates via iwinfo devices
            with contextlib.suppress(UbusError):
                iw_devs = await self._call("iwinfo", "devices")
                if isinstance(iw_devs, list):
                    candidates.update(iw_devs)
                elif isinstance(iw_devs, dict) and "devices" in iw_devs:
                    candidates.update(iw_devs["devices"])

            # Additional common names if nothing found
            if not candidates:
                candidates = {"wlan0", "wlan1", "wlan0-1", "wlan1-1", "ra0", "ra1", "rax0", "rax1"}

            for ifname in candidates:
                with contextlib.suppress(UbusError):
                    assoc = await self._call("iwinfo", "assoclist", {"device": ifname})
                    if not assoc:
                        continue
                    for client in assoc.get("results", []):
                        mac = client.get("mac", "").lower()
                        dev = devices.setdefault(
                            mac, ConnectedDevice(mac=mac, connected=True)
                        )
                        dev.connected = True
                        dev.is_wireless = True
                        dev.interface = ifname
                        self._set_wireless_connection_type(dev, ifname)
                        dev.signal = client.get("signal", 0)
                        dev.noise = client.get("noise", 0)

    async def _process_hostapd_fallback(
        self, devices: dict[str, ConnectedDevice]
    ) -> None:
        """Fallback: Discover and poll hostapd objects directly."""
        with contextlib.suppress(Exception):
            ubus_objects = await self._list_objects()
            for obj_name in ubus_objects:
                if obj_name.startswith("hostapd."):
                    ifname = obj_name.split(".", 1)[1]
                    with contextlib.suppress(UbusError):
                        hostapd_data = await self._call(obj_name, "get_clients")
                        self._merge_hostapd_clients(
                            devices, hostapd_data.get("clients", {}), ifname
                        )

    def _merge_hostapd_clients(
        self, devices: dict[str, ConnectedDevice], clients: dict[str, Any], ifname: str
    ) -> None:
        """Merge client data from hostapd into the devices dictionary."""
        for mac_addr, client_data in clients.items():
            mac = mac_addr.lower()
            dev = devices.setdefault(mac, ConnectedDevice(mac=mac, connected=True))
            dev.connected = True
            dev.is_wireless = True
            dev.interface = ifname
            self._set_wireless_connection_type(dev, ifname)

            bytes_data = client_data.get("bytes", {})
            if isinstance(bytes_data, dict):
                dev.rx_bytes = bytes_data.get("rx", 0)
                dev.tx_bytes = bytes_data.get("tx", 0)

    def _set_wireless_connection_type(self, dev: ConnectedDevice, ifname: str) -> None:
        """Determine specific wireless band from interface name."""
        if not dev.connection_type or dev.connection_type == "wired":
            dev.connection_type = "wireless"
            if "5g" in ifname.lower():
                dev.connection_type = "5GHz"
            elif "2g" in ifname.lower():
                dev.connection_type = "2.4GHz"

    async def check_permissions(self) -> OpenWrtPermissions:
        """Check user permissions via ubus session list and uci tests."""

        from .base import OpenWrtPermissions

        perms = OpenWrtPermissions()
        try:
            # 1. Try to get permissions from session list (definitive)
            if await self._check_perms_from_session(perms):
                return perms

            # 2. Fallback to manual probes (trial and error)
            await self._check_perms_from_probes(perms)

        except Exception as err:
            _LOGGER.debug("Error checking permissions via ubus: %s", err)
            if self.connected:
                # Default safety fallbacks
                perms.read_system = True
                perms.read_network = True

        return perms

    async def _check_perms_from_session(self, perms: OpenWrtPermissions) -> bool:
        """Fetch and parse ACLs from the current ubus session."""
        with contextlib.suppress(Exception):
            session_info = await self._call("session", "list")
            if not isinstance(session_info, dict):
                return False

            # Check for modern 'acls' or legacy 'values.access'
            acls_all = session_info.get("acls", {})
            acls_ubus = acls_all.get("ubus", {})
            acls_uci = acls_all.get("uci", {})

            # Legacy fallback
            access = session_info.get("values", {}).get("access", {})

            if not acls_ubus and not acls_uci and not access:
                return False

            def has_perm(obj: str, method: str) -> bool:
                # Check ubus structure
                for pattern in (obj, "*"):
                    obj_acls = acls_ubus.get(pattern, [])
                    if method in obj_acls or "*" in obj_acls:
                        return True

                # Also check if it's in a wildcard group like 'hostapd.*'
                for pattern, methods in acls_ubus.items():
                    if pattern.endswith(".*") and obj.startswith(pattern[:-1]):
                        if method in methods or "*" in methods:
                            return True

                # Check legacy/values structure with pattern matching
                for pattern, methods in access.items():
                    if (
                        pattern in ("*", obj)
                        or (pattern.endswith("*") and obj.startswith(pattern[:-1]))
                    ) and ("*" in methods or method in methods):
                        return True
                return False

            def has_uci(config: str, method: str) -> bool:
                # Check uci structure (modern)
                for pattern in (config, "*"):
                    config_acls = acls_uci.get(pattern, [])
                    if method in config_acls or "*" in config_acls:
                        return True

                # Check legacy structure (fallback)
                for pattern in ("uci", config, "*"):
                    obj_access = access.get(pattern, {})
                    if isinstance(obj_access, dict):
                        if method in obj_access or "*" in obj_access:
                            return True
                return False

            perms.read_system = has_perm("system", "info") or has_uci("system", "read")
            perms.write_system = has_perm("system", "reboot") or has_uci(
                "system", "write"
            )
            perms.read_network = has_perm("network", "status") or has_uci(
                "network", "read"
            )
            perms.write_network = has_perm("network.interface", "up") or has_uci(
                "network", "write"
            )
            perms.read_firewall = has_perm("firewall", "status") or has_uci(
                "firewall", "read"
            )
            perms.write_firewall = has_uci("firewall", "write")
            perms.read_wireless = has_perm("iwinfo", "info") or has_uci(
                "wireless", "read"
            )
            perms.write_wireless = has_uci("wireless", "write")
            perms.read_sqm = has_uci("sqm", "read")
            perms.write_sqm = has_uci("sqm", "write")
            perms.read_vpn = perms.read_network
            perms.write_vpn = perms.write_network
            perms.read_mwan = has_uci("mwan3", "read")
            perms.read_led = has_perm("led", "list") or has_uci("system", "read")
            perms.write_led = has_perm("led", "set") or has_uci("system", "write")
            perms.read_devices = has_perm("network", "status") or has_uci(
                "dhcp", "read"
            )
            perms.write_devices = has_perm("sys", "exec")
            perms.read_services = has_perm("service", "list")
            perms.write_services = has_perm("service", "list")
            perms.write_access_control = perms.write_firewall

            return True
        return False

    async def _check_perms_from_probes(self, perms: OpenWrtPermissions) -> None:
        """Identify permissions by attempting to call various methods."""

        async def can_call(obj: str, method: str, params: dict | None = None) -> bool:
            try:
                await self._call(obj, method, params)
                return True
            except UbusPermissionError:
                return False
            except Exception:
                return True

        perms.read_system = await can_call("system", "board")
        perms.write_system = await can_call("uci", "set", {"config": "system"})
        perms.read_network = await can_call("network.interface", "dump")
        perms.write_network = await can_call(
            "network.interface", "up", {"interface": "loopback"}
        )
        perms.read_firewall = await can_call("uci", "get", {"config": "firewall"})
        perms.write_firewall = await can_call("uci", "set", {"config": "firewall"})
        perms.read_wireless = await can_call("network.wireless", "status")
        perms.write_wireless = await can_call("uci", "set", {"config": "wireless"})
        perms.read_sqm = await can_call("uci", "get", {"config": "sqm"})
        perms.write_sqm = await can_call("uci", "set", {"config": "sqm"})
        perms.read_led = await can_call("uci", "get", {"config": "system"})
        perms.write_led = await can_call("uci", "set", {"config": "system"})
        perms.read_vpn = perms.read_network
        perms.read_mwan = await can_call("uci", "get", {"config": "mwan3"})
        perms.read_devices = await can_call("dhcp", "ipv4leases") or perms.read_network
        perms.write_devices = await can_call(
            "file", "exec", {"command": "/usr/bin/id"}
        ) or await can_call("file", "exec", {"command": "/bin/sh"})
        perms.write_access_control = perms.write_firewall
        perms.read_services = await can_call("service", "list")
        perms.write_services = await can_call("service", "list")

    async def check_packages(self) -> OpenWrtPackages:
        """Check installed packages."""
        packages = OpenWrtPackages()
        try:
            # Step 1: Check available ubus objects (very robust)
            objects = await self._list_objects()
            packages.iwinfo = "iwinfo" in objects
            packages.luci_mod_rpc = "luci-rpc" in objects
            if "mwan3" in objects:
                packages.mwan3 = True
            if "sqm" in objects:
                packages.sqm_scripts = True
            if "adblock" in objects:
                packages.adblock = True
            if "upnp" in objects:
                packages.miniupnpd = True
            if "nlbwmon" in objects:
                packages.nlbwmon = True
            if "pbr" in objects:
                packages.pbr = True
            if "dhcp" in objects:
                # Specifically check for ipv4leases method to avoid "Not found" on some setups
                try:
                    dhcp_methods = await self._get_object_methods("dhcp")
                    if "ipv4leases" in dhcp_methods:
                        packages.dhcp = True
                    else:
                        packages.dhcp = False
                except Exception:
                    packages.dhcp = (
                        True  # Fallback to True if check fails but object exists
                    )
            if "network.wireless" in objects or "iwinfo" in objects:
                packages.wireless = True
            # Check for hostapd objects as proof of wireless capability
            if any(obj.startswith("hostapd.") for obj in objects):
                packages.wireless = True
            if "lldp" in objects:
                packages.lldp = True

            # Step 2: Try executing a small script for remaining/all (fastest for root)
            # Index map (0-based):
            #  0: /etc/init.d/sqm          -> sqm_scripts
            #  1: /etc/init.d/mwan3        -> mwan3
            #  2: /usr/bin/iwinfo          -> iwinfo
            #  3: /usr/bin/etherwake       -> etherwake
            #  4: /usr/bin/wg              -> wireguard
            #  5: /usr/sbin/openvpn        -> openvpn
            #  6: luci-mod-rpc (lua)       -> luci_mod_rpc
            #  7: luci-mod-rpc (menu.d)    -> luci_mod_rpc
            #  8: asu (lua)                -> asu
            #  9: asu (menu.d)             -> asu
            # 10: /etc/init.d/adblock      -> adblock
            # 11: /etc/init.d/simple-adblock -> simple_adblock
            # 12: /etc/init.d/ban-ip       -> ban_ip
            # 13: /etc/init.d/miniupnpd   -> miniupnpd
            # 14: /etc/init.d/nlbwmon     -> nlbwmon
            # 15: /etc/init.d/pbr         -> pbr
            # 16: /etc/init.d/adguardhome -> adguardhome
            # 17: /etc/init.d/unbound     -> unbound
            # 18: /etc/config/sqm         -> sqm_scripts (fallback)
            try:
                cmd = (
                    "for f in /etc/init.d/sqm /etc/init.d/mwan3 /usr/bin/iwinfo "
                    "/usr/bin/etherwake /usr/bin/wg /usr/sbin/openvpn "
                    "/usr/lib/lua/luci/controller/rpc.lua "
                    "/usr/share/luci/menu.d/luci-mod-rpc.json "
                    "/usr/lib/lua/luci/controller/attendedsysupgrade.lua "
                    "/usr/share/luci/menu.d/luci-app-attendedsysupgrade.json "
                    "/etc/init.d/adblock "
                    "/etc/init.d/simple-adblock "
                    "/etc/init.d/ban-ip "
                    "/etc/init.d/miniupnpd "
                    "/etc/init.d/nlbwmon "
                    "/etc/init.d/pbr "
                    "/etc/init.d/adguardhome "
                    "/etc/init.d/unbound "
                    "/etc/config/sqm; do "
                    "if [ -f $f ] || [ -x $f ]; then echo 1; else echo 0; fi; done"
                )
                result = await self._call(
                    "file",
                    "exec",
                    {"command": "/bin/sh", "params": ["-c", cmd]},
                )
                out = result.get("stdout", "")
                results = out.strip().splitlines()

                def detect_status(idx: int) -> bool:
                    return len(results) > idx and results[idx].strip() == "1"

                if packages.sqm_scripts is not True:
                    packages.sqm_scripts = detect_status(0) or detect_status(18)
                if packages.mwan3 is not True:
                    packages.mwan3 = detect_status(1)
                if packages.iwinfo is not True:
                    packages.iwinfo = detect_status(2)
                if packages.etherwake is not True:
                    packages.etherwake = detect_status(3)
                if packages.wireguard is not True:
                    packages.wireguard = detect_status(4)
                if packages.openvpn is not True:
                    packages.openvpn = detect_status(5)
                if packages.luci_mod_rpc is not True:
                    packages.luci_mod_rpc = (
                        "luci-rpc" in objects or detect_status(6) or detect_status(7)
                    )
                if packages.asu is not True:
                    packages.asu = detect_status(8) or detect_status(9)
                if packages.adblock is not True:
                    packages.adblock = detect_status(10)
                if packages.simple_adblock is not True:
                    packages.simple_adblock = detect_status(11)
                if packages.ban_ip is not True:
                    packages.ban_ip = detect_status(12)
                if packages.miniupnpd is not True:
                    packages.miniupnpd = detect_status(13)
                if packages.nlbwmon is not True:
                    packages.nlbwmon = detect_status(14)
                if packages.pbr is not True:
                    packages.pbr = detect_status(15)
                if packages.adguardhome is not True:
                    packages.adguardhome = detect_status(16)
                if packages.unbound is not True:
                    packages.unbound = detect_status(17)

            except Exception as err:
                _LOGGER.debug("Package detection via RPC failed, falling back: %s", err)
                # Step 1: Re-use _list_objects() which uses the correct JSON-RPC
                # 'list' method (not a ubus object call).
                objects = await self._list_objects()
                self._check_packages_from_ubus(packages, objects)

                # Step 2: Check by UCI config exists
                await self._check_packages_from_uci(packages, objects)

            # Step 3: Check by file paths (last resort)
            await self._check_packages_from_files(packages)

            # Step 4: Final verification via full package list
            await self._check_packages_from_full_list(packages)

        except Exception as err:
            _LOGGER.debug("Package check failed: %s", err)

        self._ensure_all_packages_initialized(packages)
        return packages

    def _check_packages_from_ubus(
        self, packages: OpenWrtPackages, objects: list[str]
    ) -> None:
        """Identify packages based on existence of specific ubus objects."""
        packages.luci_mod_rpc = "luci" in objects
        packages.iwinfo = "iwinfo" in objects
        packages.mwan3 = "mwan3" in objects or "mwan3.status" in objects
        packages.sqm_scripts = "sqm" in objects
        packages.wireguard = "wg" in objects
        packages.asu = "attendedsysupgrade" in objects

    async def _check_packages_from_uci(
        self, packages: OpenWrtPackages, objects: list[str]
    ) -> None:
        """Identify packages based on existence of specific UCI configs."""
        configs = [
            ("sqm", "sqm_scripts"),
            ("mwan3", "mwan3"),
            ("openvpn", "openvpn"),
            ("attendedsysupgrade", "asu"),
        ]
        for config, attr in configs:
            if getattr(packages, attr) is not True:
                with contextlib.suppress(Exception):
                    await self._call("uci", "get", {"config": config})
                    setattr(packages, attr, True)

        if packages.wireguard is not True:
            with contextlib.suppress(Exception):
                res = await self._call("uci", "get", {"config": "network"})
                if (
                    res
                    and isinstance(res, dict)
                    and any(
                        v.get("proto") == "wireguard"
                        for v in res.values()
                        if isinstance(v, dict)
                    )
                ):
                    packages.wireguard = True

    async def _check_packages_from_files(self, packages: OpenWrtPackages) -> None:
        """Identify packages by probing specific filesystem paths."""
        # For each attribute we try multiple candidate paths (first match wins).
        # luci-app-attendedsysupgrade changed file locations across LuCI versions:
        #   - Legacy: controller lua file
        #   - Modern LuCI (menu.d): JSON menu descriptor
        #   - Always present: UCI config file created at install time
        check_list: list[tuple[str, str]] = [
            ("/usr/bin/etherwake", "etherwake"),
            ("/usr/bin/wg", "wireguard"),
            # ASU: try all known install paths
            ("/etc/config/attendedsysupgrade", "asu"),
            ("/usr/share/luci/menu.d/luci-app-attendedsysupgrade.json", "asu"),
            ("/usr/lib/lua/luci/controller/attendedsysupgrade.lua", "asu"),
            ("/etc/init.d/adblock", "adblock"),
            ("/etc/init.d/simple-adblock", "simple_adblock"),
            ("/etc/init.d/ban-ip", "ban_ip"),
        ]
        for path, attr in check_list:
            if getattr(packages, attr) is not True:
                with contextlib.suppress(Exception):
                    stat = await self._call("file", "stat", {"path": path})
                    if stat and isinstance(stat, dict) and "type" in stat:
                        setattr(packages, attr, True)

    async def _check_packages_from_full_list(self, packages: OpenWrtPackages) -> None:
        """Last resort: check against the full list of installed packages."""
        installed = await self.get_installed_packages()
        if not installed:
            return

        mapping = {
            "sqm_scripts": "sqm-scripts",
            "mwan3": "mwan3",
            "iwinfo": "iwinfo",
            "etherwake": "etherwake",
            "wireguard": "wireguard",
            "openvpn": "openvpn",
            "luci_mod_rpc": "luci-mod-rpc",
            "asu": "luci-app-attendedsysupgrade",
            "adblock": "adblock",
            "simple_adblock": "simple-adblock",
            "ban_ip": "ban-ip",
        }
        for attr, pkg_name in mapping.items():
            if getattr(packages, attr) is not True:
                if pkg_name in ["wireguard", "openvpn"]:
                    setattr(packages, attr, any(pkg_name in p for p in installed))
                else:
                    setattr(packages, attr, pkg_name in installed)

    def _ensure_all_packages_initialized(self, packages: OpenWrtPackages) -> None:
        """Ensure no package attributes remain as None (default to False)."""
        import dataclasses

        # Infer wireless support if iwinfo is present (crucial fallback for restricted ubus)
        if packages.wireless is None and packages.iwinfo:
            packages.wireless = True

        for field in dataclasses.fields(packages):
            if getattr(packages, field.name) is None:
                setattr(packages, field.name, False)

    async def get_local_macs(self) -> set[str]:
        """Get all MAC addresses belonging to the router's physical and virtual interfaces."""
        macs = set()
        with contextlib.suppress(Exception):
            status = await self._call("network.device", "status")
            if status and isinstance(status, dict):
                for dev_info in status.values():
                    if isinstance(dev_info, dict) and (mac := dev_info.get("macaddr")):
                        macs.add(mac.lower())
        return macs

    async def get_local_ips(self) -> set[str]:
        """Get all IP addresses belonging to the router."""
        ips = set()
        with contextlib.suppress(Exception):
            dump = await self._call("network.interface", "dump")
            if dump and isinstance(dump, dict) and (ifaces := dump.get("interface")):
                for iface in ifaces:
                    if not isinstance(iface, dict):
                        continue
                    # IPv4
                    for addr in iface.get("ipv4-address", []):
                        if isinstance(addr, dict) and (address := addr.get("address")):
                            ips.add(address)
                    # IPv6
                    for addr in iface.get("ipv6-address", []):
                        if isinstance(addr, dict) and (address := addr.get("address")):
                            ips.add(address)
        return ips

    async def get_ip_neighbors(self) -> list[IpNeighbor]:
        """Get IP neighbor (ARP/NDP) table."""
        neighbors: list[IpNeighbor] = []

        # 1. Try ubus network.device status
        await self._get_neighbors_ubus(neighbors)

        # 2. Try file.exec ip neigh show (more complete on many systems)
        await self._get_neighbors_ip_neigh(neighbors)

        # 3. Fallback to /proc/net/arp via file.read (passive)
        if not neighbors:
            await self._get_neighbors_proc_arp(neighbors)

        return neighbors

    async def _get_neighbors_ubus(self, neighbors: list[IpNeighbor]) -> None:
        """Fetch neighbors using 'network.device status' ubus call."""
        with contextlib.suppress(Exception):
            status = await self._call("network.device", "status")
            if status and isinstance(status, dict):
                for dev_name, dev_info in status.items():
                    if not isinstance(dev_info, dict):
                        continue
                    for neigh in dev_info.get("neighbors", []):
                        mac = neigh.get("lladdr")
                        ip = neigh.get("address")
                        if mac and ip:
                            neighbors.append(
                                IpNeighbor(
                                    ip=ip,
                                    mac=mac.lower(),
                                    interface=dev_name,
                                    state=neigh.get("state", "REACHABLE"),
                                ),
                            )

    async def _get_neighbors_ip_neigh(self, neighbors: list[IpNeighbor]) -> None:
        """Fetch neighbors using 'ip neigh show' via file.exec."""
        existing_macs = {n.mac.lower() for n in neighbors}
        with contextlib.suppress(Exception):
            content = await self.execute_command("ip neigh show")
            if content:
                for line in content.strip().split("\n"):
                    neigh = self._parse_ip_neigh_line(line)
                    if neigh and neigh.mac.lower() not in existing_macs:
                        neighbors.append(neigh)
                        existing_macs.add(neigh.mac.lower())

    def _parse_ip_neigh_line(self, line: str) -> IpNeighbor | None:
        """Parse a single line from 'ip neigh show' output."""
        parts = line.split()
        if len(parts) < 4:
            return None

        ip = parts[0]
        mac = ""
        interface = ""
        state = parts[-1]

        if "lladdr" in parts:
            idx = parts.index("lladdr")
            if len(parts) > idx + 1:
                mac = parts[idx + 1].upper()
        if "dev" in parts:
            idx = parts.index("dev")
            if len(parts) > idx + 1:
                interface = parts[idx + 1]

        if mac:
            return IpNeighbor(ip=ip, mac=mac, interface=interface, state=state)
        return None

    async def _get_neighbors_proc_arp(self, neighbors: list[IpNeighbor]) -> None:
        """Fetch neighbors from /proc/net/arp via file.read."""
        with contextlib.suppress(Exception):
            result = await self._call("file", "read", {"path": "/proc/net/arp"})
            content = result.get("data", "")
            if content:
                for line in content.strip().split("\n")[1:]:  # Skip header
                    parts = line.split()
                    if len(parts) >= 6:
                        neighbors.append(
                            IpNeighbor(
                                ip=parts[0],
                                mac=parts[3].upper(),
                                interface=parts[5],
                                state="REACHABLE",
                            ),
                        )

    async def get_dhcp_leases(self) -> list[DhcpLease]:
        """Get DHCP leases via ubus or file."""
        if self.dhcp_software == "none":
            return []

        leases: list[DhcpLease] = []

        # Try odhcpd via ubus
        if self.dhcp_software in ("auto", "odhcpd") and self.packages.dhcp is not False:
            try:
                # IPv4 leases
                result = await self._call("dhcp", "ipv4leases")
                for lease_data in result.get("device", {}).values():
                    # odhcpd can return list or dict per interface
                    lease_list = (
                        lease_data if isinstance(lease_data, list) else [lease_data]
                    )
                    for lease in lease_list:
                        if not isinstance(lease, dict):
                            continue
                        leases.append(
                            DhcpLease(
                                hostname=lease.get("hostname", ""),
                                mac=lease.get("mac", "").lower(),
                                ip=lease.get("ipaddr", ""),
                                expires=lease.get("expires", 0),
                                type="v4",
                            ),
                        )

                # IPv6 leases
                result_v6 = await self._call("dhcp", "ipv6leases")
                for lease_data in result_v6.get("device", {}).values():
                    lease_list = (
                        lease_data if isinstance(lease_data, list) else [lease_data]
                    )
                    for lease in lease_list:
                        if not isinstance(lease, dict):
                            continue
                        leases.append(
                            DhcpLease(
                                hostname=lease.get("hostname", ""),
                                mac=lease.get("mac", "").lower(),
                                ip=lease.get("ipaddr", ""),
                                expires=lease.get("expires", 0),
                                type="v6",
                                duid=lease.get("duid", ""),
                            ),
                        )

                if leases and self.dhcp_software == "odhcpd":
                    return leases
            except UbusError:
                if self.dhcp_software == "odhcpd":
                    _LOGGER.debug("Requested odhcpd but 'dhcp' ubus object not found")
                    return []

        # Parse dnsmasq leases from /tmp/dhcp.leases
        if self.dhcp_software in ("auto", "dnsmasq"):
            content = ""
            with contextlib.suppress(UbusError):
                # Priority 1: file.read (more robust/standard)
                result = await self._call("file", "read", {"path": "/tmp/dhcp.leases"})
                content = result.get("data", "")

            if not content:
                with contextlib.suppress(Exception):
                    # Priority 2: file.exec (original fallback)
                    content = await self.execute_command(
                        "cat /tmp/dhcp.leases 2>/dev/null",
                    )

            if content:
                for line in content.strip().split("\n"):
                    parts = line.split()
                    if len(parts) >= 4:
                        leases.append(
                            DhcpLease(
                                expires=int(parts[0]) if parts[0].isdigit() else 0,
                                mac=parts[1].lower(),
                                ip=parts[2],
                                hostname=parts[3] if parts[3] != "*" else "",
                            ),
                        )
            elif self.dhcp_software == "dnsmasq":
                _LOGGER.debug("Requested dnsmasq but could not read /tmp/dhcp.leases")

        return leases

    async def get_mwan_status(self) -> list[MwanStatus]:
        """Get MWAN3 multi-wan status."""
        statuses: list[MwanStatus] = []

        try:
            data = await self._call("mwan3", "status")
            interfaces = data.get("interfaces", {})
            for iface_name, iface_data in interfaces.items():
                statuses.append(
                    MwanStatus(
                        interface_name=iface_name,
                        status=iface_data.get("status", "unknown"),
                        online_ratio=float(iface_data.get("online", 0)),
                        uptime=iface_data.get("uptime", 0),
                        enabled=iface_data.get("enabled", False),
                    ),
                )
        except UbusError:
            _LOGGER.debug("MWAN3 not available (not installed or no permissions)")

        return statuses

    async def get_wps_status(self) -> WpsStatus:
        """Get WPS status from the first wireless interface."""
        if self.packages.wireless is False:
            return WpsStatus()

        try:
            wireless_data = await self._call("network.wireless", "status")
            for radio_data in wireless_data.values():
                if not isinstance(radio_data, dict):
                    continue
                for iface in radio_data.get("interfaces", []):
                    iface_name = iface.get("ifname", "")
                    if iface_name:
                        try:
                            result = await self._call(
                                f"hostapd.{iface_name}",
                                "wps_status",
                            )
                            return WpsStatus(
                                enabled=result.get("pbc_status", "") == "Active",
                                status=result.get("pbc_status", "Disabled"),
                            )
                        except UbusError:
                            continue
        except UbusError:
            pass

        return WpsStatus()

    async def set_wps(self, enabled: bool) -> bool:
        """Enable or disable WPS."""
        try:
            wireless_data = await self._call("network.wireless", "status")
            for radio_data in wireless_data.values():
                if not isinstance(radio_data, dict):
                    continue
                for iface in radio_data.get("interfaces", []):
                    iface_name = iface.get("ifname", "")
                    if iface_name:
                        method = "wps_start" if enabled else "wps_cancel"
                        await self._call(f"hostapd.{iface_name}", method)
                        return True
        except UbusError as err:
            _LOGGER.exception("Failed to set WPS: %s", err)
        return False

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

    async def get_services(self) -> list[ServiceInfo]:
        """Get init.d services via the rc ubus interface."""
        services: list[ServiceInfo] = []
        result = await self._call("rc", "list")
        for name, data in result.items():
            services.append(
                ServiceInfo(
                    name=name,
                    enabled=data.get("enabled", False),
                    running=data.get("running", False),
                ),
            )
        return services

    async def manage_service(self, name: str, action: str) -> bool:
        """Manage a system service (start/stop/restart/enable/disable)."""
        try:
            # 1. Try standard ubus rc.init (best practice)
            await self._call("rc", "init", {"name": name, "action": action})
            self._last_full_poll = 0
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
                self._last_full_poll = 0
                return True
            except Exception:
                # 3. Final fallback to shell execute_command
                try:
                    await self.execute_command(f"/etc/init.d/{name} {action}")
                    self._last_full_poll = 0
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

    async def set_wireless_enabled(self, interface: str, enabled: bool) -> bool:
        """Enable or disable a wireless radio via UCI."""
        try:
            action = "0" if enabled else "1"  # disabled=0 means enabled
            await self._call(
                "uci",
                "set",
                {
                    "config": "wireless",
                    "section": interface,
                    "values": {"disabled": action},
                },
            )
            await self._call("uci", "commit", {"config": "wireless"})
            await self._call("network.wireless", "notify")
            self._last_full_poll = 0
            return True
        except UbusError:
            return False

    async def set_firewall_rule_enabled(self, section_id: str, enabled: bool) -> bool:
        """Enable or disable a firewall rule via UCI."""
        try:
            action = "1" if enabled else "0"
            await self._call(
                "uci",
                "set",
                {
                    "config": "firewall",
                    "section": section_id,
                    "values": {"enabled": action},
                },
            )
            await self._call("uci", "commit", {"config": "firewall"})
            await self.execute_command("/etc/init.d/firewall reload")
            self._last_full_poll = 0
            return True
        except UbusError:
            return False

    async def get_firewall_rules(self) -> list[FirewallRule]:
        """Get general firewall rules via UCI."""
        rules: list[FirewallRule] = []
        config = await self._call("uci", "get", {"config": "firewall"})
        values = config.get("values", {})

        for section_id, section_data in values.items():
            if section_data.get(".type") != "rule":
                continue

            display_id = section_id
            if section_id.startswith("cfg"):
                rule_sects = [k for k, v in values.items() if v.get(".type") == "rule"]
                try:
                    idx = rule_sects.index(section_id)
                    display_id = f"@rule[{idx}]"
                except ValueError:
                    pass

            rules.append(
                FirewallRule(
                    name=section_data.get("name", display_id),
                    enabled=str(section_data.get("enabled", "1")) == "1",
                    section_id=display_id,
                    target=section_data.get("target", ""),
                    src=section_data.get("src", ""),
                    dest=section_data.get("dest", ""),
                ),
            )
        return rules

    async def get_firewall_redirects(self) -> list[FirewallRedirect]:
        """Get firewall port forwarding redirects via UCI."""
        redirects: list[FirewallRedirect] = []
        config = await self._call("uci", "get", {"config": "firewall"})
        vals = config.get("values", {})

        for section_id, redirect in vals.items():
            if redirect.get(".type") != "redirect":
                continue

            # Standardize section ID: Prefer named sections, fallback to anonymous index
            # if it looks like cfgXXXXXX
            display_id = section_id
            if section_id.startswith("cfg"):
                # Find the index of this redirect among all redirects
                redirect_sects = [
                    k for k, v in vals.items() if v.get(".type") == "redirect"
                ]
                try:
                    idx = redirect_sects.index(section_id)
                    display_id = f"@redirect[{idx}]"
                except ValueError:
                    pass

            redirects.append(
                FirewallRedirect(
                    name=redirect.get("name", "Unnamed Redirect"),
                    target_ip=redirect.get("dest_ip", ""),
                    target_port=redirect.get("dest_port", ""),
                    external_port=redirect.get("src_dport", ""),
                    protocol=redirect.get("proto", "tcp"),
                    enabled=redirect.get("enabled", "1") == "1",
                    section_id=display_id,
                )
            )
        return redirects

    async def set_firewall_redirect_enabled(
        self,
        section_id: str,
        enabled: bool,
    ) -> bool:
        """Enable or disable a firewall redirect via UCI."""
        try:
            value = "1" if enabled else "0"
            await self._call(
                "uci",
                "set",
                {
                    "config": "firewall",
                    "section": section_id,
                    "values": {"enabled": value},
                },
            )
            await self._call("uci", "commit", {"config": "firewall"})
            await self._call("service", "reloading", {"service": "firewall"})
            self._last_full_poll = 0
            return True
        except UbusError:
            return False

    async def get_access_control(self) -> list[AccessControl]:
        """Get list of access control rules via UCI firewall rules."""
        rules: list[AccessControl] = []
        config = await self._call("uci", "get", {"config": "firewall"})
        values = config.get("values", {})

        for section_id, section_data in values.items():
            if section_data.get(".type") != "rule":
                continue

            name = section_data.get("name", "")
            if not name.startswith("ha_acl_"):
                continue

            mac = section_data.get("src_mac", "").upper()
            if mac:
                rules.append(
                    AccessControl(
                        mac=mac,
                        name=name.replace("ha_acl_", ""),
                        blocked=str(section_data.get("enabled", "1")) == "1"
                        and section_data.get("target") in ("REJECT", "DROP"),
                        section_id=section_id,
                    ),
                )
        return rules

    async def set_access_control_blocked(self, mac: str, blocked: bool) -> bool:
        """Block or unblock a device's internet access via UCI firewall rule."""
        mac_upper = mac.upper()
        mac_safe = mac_upper.replace(":", "")
        rule_name = f"ha_acl_{mac_safe}"

        try:
            rules = await self.get_access_control()
            section_id = next((r.section_id for r in rules if r.mac == mac_upper), None)

            if blocked:
                if not section_id:
                    res = await self._call(
                        "uci",
                        "add",
                        {"config": "firewall", "type": "rule"},
                    )
                    section_id = res.get("section")
                    if not section_id:
                        return False

                    await self._call(
                        "uci",
                        "set",
                        {
                            "config": "firewall",
                            "section": section_id,
                            "values": {
                                "name": rule_name,
                                "src": "lan",
                                "dest": "wan",
                                "src_mac": mac_upper,
                                "target": "REJECT",
                                "enabled": "1",
                            },
                        },
                    )
                else:
                    await self._call(
                        "uci",
                        "set",
                        {
                            "config": "firewall",
                            "section": section_id,
                            "values": {"enabled": "1", "target": "REJECT"},
                        },
                    )
            elif section_id:
                await self._call(
                    "uci",
                    "set",
                    {
                        "config": "firewall",
                        "section": section_id,
                        "values": {"enabled": "0"},
                    },
                )

            await self._call("uci", "commit", {"config": "firewall"})
            await self._call("service", "reloading", {"service": "firewall"})
            self._last_full_poll = 0
            return True
        except UbusError:
            return False

    async def get_leds(self) -> list:
        """Get LEDs from /sys/class/leds via file.exec."""
        from .base import LedInfo

        leds: list[LedInfo] = []
        result = await self._call(
            "file",
            "exec",
            {
                "command": "/bin/sh",
                "params": [
                    "-c",
                    "for led in /sys/class/leds/*/; do "
                    'name=$(basename "$led"); '
                    'brightness=$(cat "$led/brightness" 2>/dev/null || echo 0); '
                    'max=$(cat "$led/max_brightness" 2>/dev/null || echo 255); '
                    'trigger=$(cat "$led/trigger" 2>/dev/null | tr " " "\\n" | grep "^\\[" | tr -d "[]" || echo none); '
                    'echo "$name|$brightness|$max|$trigger"; '
                    "done",
                ],
                "env": {},
            },
        )
        stdout = result.get("stdout", "")
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

    async def manage_interface(self, name: str, action: str) -> bool:
        """Manage a network interface (up/down/reconnect) via ubus."""
        try:
            if action in {"reconnect", "up"}:
                await self._call("network.interface", "up", {"interface": name})
            elif action == "down":
                await self._call("network.interface", "down", {"interface": name})
            return True
        except UbusError:
            return False

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
                ]
            ):
                _LOGGER.info(
                    "Ubus connection lost during sysupgrade - device is rebooting",
                )
                return
            _LOGGER.warning(
                "Sysupgrade command might have failed or disconnected: %s",
                err,
            )

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

    async def get_adblock_status(self) -> AdBlockStatus:
        """Get adblock status via ubus/uci."""
        from .base import AdBlockStatus

        status = AdBlockStatus()
        try:
            # Try ubus first
            res = await self._call("adblock", "status")
            if res:
                status.enabled = res.get("adblock_status") == "enabled"
                status.status = res.get("adblock_status", "disabled")
                status.version = res.get("adblock_version")
                status.blocked_domains = int(res.get("blocked_domains", 0))
                status.last_update = res.get("last_run")
                return status

            # Fallback to uci
            enabled = await self.execute_command("uci -q get adblock.global.enabled")
            status.enabled = enabled.strip() == "1"
            status.status = "enabled" if status.enabled else "disabled"
        except Exception:
            pass
        return status

    async def set_adblock_enabled(self, enabled: bool) -> bool:
        """Enable/disable adblock service."""
        val = "1" if enabled else "0"
        try:
            await self.execute_command(
                f"uci set adblock.global.enabled='{val}' && uci commit adblock",
            )
            action = "start" if enabled else "stop"
            await self.execute_command(f"/etc/init.d/adblock {action}")
            self._last_full_poll = 0
            return True
        except Exception:
            return False

    async def get_simple_adblock_status(self) -> SimpleAdBlockStatus:
        """Get simple-adblock status via uci."""
        from .base import SimpleAdBlockStatus

        status = SimpleAdBlockStatus()
        try:
            res = await self.execute_command("uci -q get simple-adblock.config.enabled")
            status.enabled = res.strip() == "1"
            status.status = "enabled" if status.enabled else "disabled"
            # Optional: try to count blocked domains if file exists
            count = await self.execute_command(
                "wc -l < /tmp/simple-adblock.blocked 2>/dev/null",
            )
            if count and count.strip().isdigit():
                status.blocked_domains = int(count.strip())
        except Exception:
            pass
        return status

    async def set_simple_adblock_enabled(self, enabled: bool) -> bool:
        """Enable/disable simple-adblock service."""
        val = "1" if enabled else "0"
        try:
            await self.execute_command(
                f"uci set simple-adblock.config.enabled='{val}' && uci commit simple-adblock",
            )
            action = "start" if enabled else "stop"
            await self.execute_command(f"/etc/init.d/simple-adblock {action}")
            self._last_full_poll = 0
            return True
        except Exception:
            return False

    async def get_banip_status(self) -> BanIpStatus:
        """Get ban-ip status."""
        from .base import BanIpStatus

        status = BanIpStatus()
        try:
            res = await self.execute_command("uci -q get ban-ip.config.enabled")
            status.enabled = res.strip() == "1"
            status.status = "enabled" if status.enabled else "disabled"
        except Exception:
            pass
        return status

    async def set_banip_enabled(self, enabled: bool) -> bool:
        """Enable/disable ban-ip service."""
        val = "1" if enabled else "0"
        try:
            await self.execute_command(
                f"uci set ban-ip.config.enabled='{val}' && uci commit ban-ip",
            )
            action = "start" if enabled else "stop"
            await self.execute_command(f"/etc/init.d/ban-ip {action}")
            self._last_full_poll = 0
            return True
        except Exception:
            return False

    async def get_sqm_status(self) -> list[SqmStatus]:
        """Get SQM status via uci ubus."""
        from .base import SqmStatus

        sqm_instances: list[SqmStatus] = []
        try:
            resp = await self._call("uci", "get", {"config": "sqm"})
            if not resp or not isinstance(resp, dict):
                return sqm_instances

            # Support both {"values": {...}} and direct {...}
            values = resp.get("values", resp)
            if not isinstance(values, dict):
                return sqm_instances

            for section_id, section_data in values.items():
                if (
                    isinstance(section_data, dict)
                    and section_data.get(".type") == "queue"
                ):
                    sqm = SqmStatus(
                        section_id=section_id,
                        name=str(section_data.get("name", section_id)),
                        enabled=section_data.get("enabled") == "1",
                        interface=section_data.get("interface", ""),
                        download=int(section_data.get("download", 0)),
                        upload=int(section_data.get("upload", 0)),
                        qdisc=section_data.get("qdisc", ""),
                        script=section_data.get("script", ""),
                    )
                    sqm_instances.append(sqm)
        except Exception as err:
            _LOGGER.debug("SQM status check failed: %s", err)
        return sqm_instances

    async def set_sqm_config(self, section_id: str, **kwargs: Any) -> bool:
        """Set SQM configuration via uci ubus."""
        try:
            for key, value in kwargs.items():
                val_str = (
                    "1" if value is True else "0" if value is False else str(value)
                )
                await self._call(
                    "uci",
                    "set",
                    {"config": "sqm", "section": section_id, "values": {key: val_str}},
                )
            await self._call("uci", "commit", {"config": "sqm"})
            await self._call(
                "file",
                "exec",
                {"command": "/etc/init.d/sqm", "params": ["reload"]},
            )
            self._last_full_poll = 0
            return True
        except UbusPermissionError as err:
            _LOGGER.debug("SQM config via ubus denied (permissions): %s", err)
            return False
        except Exception as err:
            _LOGGER.exception("Failed to set SQM config: %s", err)
            return False

    async def get_gateway_mac(self) -> str | None:
        """Get the default gateway MAC address via ubus and triangulation."""
        try:
            # 1. Get default gateway IP from network.interface dump
            gw_ip = await self._get_gateway_ip_from_ubus()
            if not gw_ip:
                return None

            # 2. Get MAC for that IP via ip neighbor
            return await self._get_mac_from_ip(gw_ip)
        except Exception as err:
            _LOGGER.debug("Failed to get gateway MAC via ubus: %s", err)
        return None

    async def _get_gateway_ip_from_ubus(self) -> str | None:
        """Find the default gateway IP from 'network.interface dump'."""
        status = await self._call("network.interface", "dump")
        interfaces = status.get("interface", [])

        # Priority 1: Common WAN interfaces
        wan_names = ("wan", "wan6", "wwan", "modem")
        for iface in interfaces:
            if iface.get("interface", "").lower() in wan_names:
                gw = self._extract_gateway_from_iface(iface)
                if gw:
                    return gw

        # Priority 2: Any interface with a gateway
        for iface in interfaces:
            gw = self._extract_gateway_from_iface(iface)
            if gw:
                return gw

        return None

    def _extract_gateway_from_iface(self, iface_data: dict[str, Any]) -> str | None:
        """Extract gateway IP from a single interface entry."""
        for addr in iface_data.get("ipv4-address", []):
            if addr.get("gateway"):
                return addr.get("gateway")
        return None

    async def _get_mac_from_ip(self, ip: str) -> str | None:
        """Get the MAC address for a specific IP from the ARP/neighbor table."""
        neigh_out = await self.execute_command(f"ip neigh show {ip} 2>/dev/null")
        if "lladdr" in neigh_out:
            parts = neigh_out.split()
            try:
                idx = parts.index("lladdr")
                if len(parts) > idx + 1:
                    return parts[idx + 1].upper()
            except ValueError, IndexError:
                pass
        return None

    async def get_lldp_neighbors(self) -> list[LldpNeighbor]:
        """Get LLDP neighbor information via ubus."""
        from .base import LldpNeighbor

        neighbors: list[LldpNeighbor] = []
        if self.packages.lldp is False:
            return neighbors

        try:
            # ubus call lldp show
            data = await self._call("lldp", "show")
            # Parse ubus lldp output structure
            interfaces = data.get("lldp", {}).get("interface", [])
            if isinstance(interfaces, list):
                for iface in interfaces:
                    name = iface.get("name")
                    neighs = iface.get("neighbor", [])
                    if isinstance(neighs, list):
                        for neigh in neighs:
                            neighbors.append(
                                LldpNeighbor(
                                    local_interface=name or "",
                                    neighbor_name=neigh.get("name", ""),
                                    neighbor_port=(
                                        neigh.get("port", {}).get("id", "")
                                        if isinstance(neigh.get("port"), dict)
                                        else ""
                                    ),
                                    neighbor_chassis=(
                                        neigh.get("chassis", {}).get(
                                            "id",
                                            "",
                                        )
                                        if isinstance(neigh.get("chassis"), dict)
                                        else ""
                                    ),
                                    neighbor_description=neigh.get("description", ""),
                                    neighbor_system_name=neigh.get("sysname", ""),
                                ),
                            )
        except Exception as err:
            _LOGGER.debug("Failed to get LLDP neighbors via ubus: %s", err)
        return neighbors

    async def perform_diagnostics(self) -> list[DiagnosticResult]:
        """Perform ubus-specific diagnostic checks."""
        results: list[DiagnosticResult] = []

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

    async def get_nlbwmon_data(self) -> dict[str, NlbwmonTraffic]:
        """Get bandwidth usage per MAC from nlbwmon."""
        result = await self._call("nlbwmon", "get_data", {"group_by": "mac"})
        if not result or "data" not in result:
            return {}

        traffic = {}
        for mac, data in result["data"].items():
            mac_upper = mac.upper()
            traffic[mac_upper] = NlbwmonTraffic(
                mac=mac_upper,
                rx_bytes=data.get("rx", 0),
                tx_bytes=data.get("tx", 0),
                rx_packets=data.get("rx_packets", 0),
            )
        return traffic

    async def get_wifi_credentials(self) -> list[WifiCredentials]:
        """Get wifi credentials via UCI."""
        try:
            uci = await self._call("uci", "get", {"config": "wireless"})
            if not uci or "values" not in uci:
                return []

            creds = []
            for key, val in uci["values"].items():
                if isinstance(val, dict) and val.get(".type") == "wifi-iface":
                    if val.get("mode") == "ap":
                        ssid = val.get("ssid")
                        key_val = val.get("key")
                        if ssid:
                            creds.append(
                                WifiCredentials(
                                    iface=key,
                                    ssid=ssid,
                                    encryption=val.get("encryption", "none"),
                                    key=key_val or "",
                                    hidden=bool(int(val.get("hidden", 0))),
                                )
                            )
            return creds
        except Exception as err:
            _LOGGER.debug("Failed to get wifi credentials via ubus: %s", err)
            return []

    async def trigger_wps_push(self, interface: str) -> bool:
        """Trigger WPS push button on a specific wireless interface via ubus."""
        try:
            # 1. Try direct guess: hostapd.interface
            obj = f"hostapd.{interface}"
            await self._call(obj, "wps_push")
            return True
        except Exception:
            try:
                # 2. List objects and find matching hostapd interface
                objects = await self._list_objects()
                for obj in objects:
                    if obj.startswith("hostapd.") and interface in obj:
                        await self._call(obj, "wps_push")
                        return True
                return False
            except Exception as err:
                _LOGGER.debug(
                    "Failed to trigger WPS push via ubus for %s: %s", interface, err
                )
                return False

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
            self._last_full_poll = 0
            return True
        except Exception:
            try:
                # Fallback to shell
                await self.execute_command(
                    f"echo {val} > /sys/class/leds/{name}/brightness"
                )
                self._last_full_poll = 0
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
