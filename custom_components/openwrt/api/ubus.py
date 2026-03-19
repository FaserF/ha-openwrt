"""OpenWrt ubus HTTP/HTTPS API client.

Communicates with OpenWrt via the ubus JSON-RPC interface exposed through
uhttpd. This is the recommended and most feature-complete connection method.

Requires packages on OpenWrt: uhttpd, uhttpd-mod-ubus, rpcd, rpcd-mod-iwinfo
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

from .base import (
    PROVISION_SCRIPT_TEMPLATE,
    AccessControl,
    ConnectedDevice,
    DeviceInfo,
    DhcpLease,
    FirewallRedirect,
    FirewallRule,
    IpNeighbor,
    LldpNeighbor,
    MwanStatus,
    NetworkInterface,
    OpenWrtClient,
    OpenWrtPackages,
    OpenWrtPermissions,
    ServiceInfo,
    SqmStatus,
    SystemResources,
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
            host, username, password, port, use_ssl, verify_ssl, dhcp_software
        )
        self._ubus_path = ubus_path
        self._session_id: str = "00000000000000000000000000000000"
        self._session: aiohttp.ClientSession | None = None

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
                ssl=self.verify_ssl if self.use_ssl else False
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

        try:
            async with session.post(
                self._base_url,
                json=payload,
                headers={"Content-Type": "application/json"},
            ) as response:
                response.raise_for_status()
                data = await response.json()
        except TimeoutError as err:
            raise UbusTimeoutError(f"Timeout communicating with {self.host}") from err
        except aiohttp.ClientConnectorError as err:
            raise UbusConnectionError(
                f"Cannot connect to {self.host}. Is the IP correct and uhttpd running?"
            ) from err
        except aiohttp.ClientSSLError as err:
            raise UbusSslError(
                f"SSL verification failed for {self.host}. Try disabling 'Verify SSL Certificate' if you use a self-signed one."
            ) from err
        except aiohttp.ClientResponseError as err:
            if err.status == 404:
                raise UbusPackageMissingError(
                    f"Ubus endpoint not found on {self.host}. Is 'uhttpd-mod-ubus' installed?"
                ) from err
            if err.status == 403:
                raise UbusPermissionError(
                    f"Access denied to ubus on {self.host}. Check RPC permissions or switch to LuCI RPC."
                ) from err
            raise UbusError(f"HTTP error {err.status} from {self.host}") from err
        except aiohttp.ClientError as err:
            if not reauthenticated:
                _LOGGER.debug(
                    "Ubus connection error (%s), retrying after session reset", err
                )
                if self._session and not self._session.closed:
                    await self._session.close()
                self._session = None
                return await self._call(
                    ubus_object, ubus_method, params, reauthenticated=True
                )
            self._connected = False
            raise UbusError(f"Communication error with {self.host}: {err}") from err

        if "result" not in data:
            raise UbusError(f"Unexpected response: {data}")

        result = data["result"]

        if isinstance(result, list):
            code = result[0] if result else -1
            if code == 6 and not reauthenticated:
                await self.connect()
                return await self._call(
                    ubus_object, ubus_method, params, reauthenticated=True
                )
            if code != 0:
                if code == 2:
                    raise UbusError(
                        f"RPC Error ({code}): Invalid command or object '{ubus_object}'"
                    )
                if code in (3, 6):
                    raise UbusPermissionError(
                        f"RPC Error ({code}): Access denied to '{ubus_object}.{ubus_method}'. Consider switching to LuCI RPC."
                    )
                raise UbusError(
                    f"ubus error code {code} for {ubus_object}.{ubus_method}"
                )
            return result[1] if len(result) > 1 else {}

        return result

    async def _list_objects(self) -> list[str]:
        """List available ubus objects."""
        session = await self._ensure_session()
        if not self._connected:
            await self.connect()

        token = self._session_id
        payload = self._build_request(
            "list",
            [token],
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

        if "result" not in data:
            return []

        # Result is a dict where keys are object names
        return list(data["result"].keys())

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
            raise UbusTimeoutError(f"Login timeout for {self.host}") from err
        except aiohttp.ClientConnectorError as err:
            raise UbusConnectionError(f"Cannot connect to {self.host}: {err}") from err
        except aiohttp.ClientSSLError as err:
            raise UbusSslError(f"SSL error connecting to {self.host}: {err}") from err
        except aiohttp.ClientResponseError as err:
            if err.status == 404:
                raise UbusPackageMissingError(
                    f"Ubus endpoint not found on {self.host}. Is 'uhttpd-mod-ubus' installed?"
                ) from err
            raise UbusError(f"HTTP error {err.status} during login: {err}") from err
        except aiohttp.ClientError as err:
            raise UbusError(f"Cannot connect to {self.host}: {err}") from err

        result = data.get("result")
        if (
            result is None
            or (isinstance(result, list) and not result)
            or (isinstance(result, list) and result[0] != 0)
        ):
            _LOGGER.error("Ubus auth failed: %s", data)
            raise UbusAuthError(
                f"Authentication failed for {self.username}@{self.host}. Check credentials."
            )

        if isinstance(result, list) and len(result) > 1:
            self._session_id = result[1].get("ubus_rpc_session", "")
        else:
            raise UbusAuthError("No session ID in auth response")

        self._connected = True
        _LOGGER.debug(
            "Authenticated with %s, session: %s...", self.host, self._session_id[:8]
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
        info.firmware_version = f"{info.release_version} ({info.release_revision})"
        info.target = release.get("target", data.get("board_name", ""))

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
                        "command": "cat /sys/class/net/br-lan/address 2>/dev/null || cat /sys/class/net/eth0/address 2>/dev/null"
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
            # Memory parsing
            mem = data.get("memory", {})
            resources.memory_total = mem.get("total", 0)
            resources.memory_free = mem.get("free", 0)
            resources.memory_buffered = mem.get("buffered", 0)
            resources.memory_cached = mem.get("cached", 0)
            resources.memory_used = (
                resources.memory_total
                - resources.memory_free
                - resources.memory_buffered
                - resources.memory_cached
            )

            # Swap parsing
            swap = data.get("swap", {})
            resources.swap_total = swap.get("total", 0)
            resources.swap_free = swap.get("free", 0)
            resources.swap_used = resources.swap_total - resources.swap_free

            resources.uptime = data.get("uptime", 0)

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
            if "disk" in data:
                disk = data["disk"]
                root = disk.get("root", disk.get("/", {}))
                if isinstance(root, dict) and root.get("total"):
                    resources.filesystem_total = root.get("total", 0)
                    resources.filesystem_used = root.get("used", 0)
                    resources.filesystem_free = root.get("total", 0) - root.get(
                        "used", 0
                    )

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

        # 2. Storage fallback via luci.getMountPoints
        if resources.filesystem_total == 0:
            try:
                mounts = await self._call("luci", "getMountPoints")
                if isinstance(mounts, dict) and "result" in mounts:
                    for mount in mounts["result"]:
                        if mount.get("mount") in ("/", "/overlay"):
                            resources.filesystem_total = mount.get("size", 0)
                            resources.filesystem_free = mount.get(
                                "free", 0
                            ) or mount.get("avail", 0)
                            resources.filesystem_used = (
                                resources.filesystem_total - resources.filesystem_free
                            )
                            break
            except Exception:
                pass

        # 3. CPU usage fallback from /proc/stat
        if resources.cpu_usage == 0.0:
            # Try Priority 2: file.read (more standard and less restricted than file.exec)
            file_read = results[2]
            if (
                not isinstance(file_read, Exception)
                and isinstance(file_read, dict)
                and file_read.get("data")
            ):
                resources.cpu_usage = self._calculate_cpu_usage(file_read["data"])

            # Try Priority 3: file.exec (original method)
            if resources.cpu_usage == 0.0:
                proc_stat = results[1]
                if not isinstance(proc_stat, Exception) and proc_stat:
                    resources.cpu_usage = self._calculate_cpu_usage(proc_stat)

        # 3. Temperature fetching
        try:
            # Try via ubus file first (more standard for rpc users)
            temp_paths = [
                "/sys/class/thermal/thermal_zone0/temp",
                "/sys/class/thermal/thermal_zone1/temp",
                "/sys/class/hwmon/hwmon0/temp1_input",
                "/sys/devices/virtual/thermal/thermal_zone0/temp",
            ]
            for path in temp_paths:
                try:
                    res = await self._call("file", "read", {"path": path})
                    temp_raw = res.get("data", "").strip()
                    import re

                    match = re.search(r"(\d+)", temp_raw)
                    if match:
                        temp = float(match.group(1))
                        if temp > 200:  # Usually millidegrees
                            temp /= 1000.0
                        if 0 < temp < 150:
                            resources.temperature = temp
                            break
                except UbusError:
                    continue

            # Fallback to execute_command if ubus file read failed
            if resources.temperature is None:
                for path in temp_paths:
                    try:
                        temp_raw = await self.execute_command(f"cat {path} 2>/dev/null")
                        if temp_raw:
                            match = re.search(r"(\d+)", temp_raw)
                            if match:
                                temp = float(match.group(1))
                                if temp > 200:
                                    temp /= 1000.0
                                if 0 < temp < 150:
                                    resources.temperature = temp
                                    break
                    except Exception:  # noqa: BLE001
                        continue
        except Exception:  # noqa: BLE001
            pass

        # 4. Filesystem fallback to df if ubus disk info was missing
        if resources.filesystem_total == 0:
            try:
                df_output = await self.execute_command(
                    "df /overlay 2>/dev/null || df / 2>/dev/null"
                )
                lines = df_output.strip().split("\n")
                if len(lines) >= 2:
                    parts = lines[1].split()
                    if len(parts) >= 4:
                        resources.filesystem_total = int(parts[1]) * 1024
                        resources.filesystem_used = int(parts[2]) * 1024
                        resources.filesystem_free = int(parts[3]) * 1024
            except Exception:  # noqa: BLE001
                pass

        return resources

    async def get_external_ip(self) -> str | None:
        """Get public/external IP address by checking the WAN interface."""
        try:
            status = await self._call("network.interface", "dump")
            for iface_data in status.get("interface", []):
                iface_name = iface_data.get("interface", "").lower()
                if iface_name in ["wan", "wan6", "wwan", "modem"]:
                    ipv4_addrs = iface_data.get("ipv4-address", [])
                    if ipv4_addrs:
                        return ipv4_addrs[0].get("address")
        except UbusError:
            pass
        return None

    async def get_wireless_interfaces(self) -> list[WirelessInterface]:
        """Get wireless interface information."""
        interfaces: list[WirelessInterface] = []

        try:
            wireless_data = await self._call("network.wireless", "status")
        except UbusError:
            return interfaces

        for _radio_name, radio_data in wireless_data.items():
            if not isinstance(radio_data, dict):
                continue

            radio_interfaces = radio_data.get("interfaces", [])
            for iface in radio_interfaces:
                iface_name = (
                    iface.get("ifname")
                    or iface.get("device")
                    or iface.get("section", "")
                )
                iface_config = iface.get("config", {})
                _iface_network = iface_config.get("network", [])

                wifi = WirelessInterface(
                    name=iface_name,
                    ssid=iface_config.get("ssid", ""),
                    mode=iface_config.get("mode", ""),
                    encryption=iface_config.get("encryption", ""),
                    enabled=not radio_data.get("disabled", False),
                    up=radio_data.get("up", False),
                    radio=_radio_name,
                    htmode=radio_data.get("config", {}).get("htmode", ""),
                    hwmode=radio_data.get("config", {}).get("hwmode", ""),
                    txpower=radio_data.get("config", {}).get("txpower", 0),
                    mesh_id=iface_config.get("mesh_id", ""),
                    mesh_fwding=iface_config.get("mesh_fwding", False),
                )

                try:
                    if iface_name:
                        iwinfo = await self._call(
                            "iwinfo", "info", {"device": iface_name}
                        )
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
                except UbusError:
                    pass

                try:
                    if iface_name:
                        clients = await self._call(
                            "iwinfo", "assoclist", {"device": iface_name}
                        )
                        wifi.clients_count = len(clients.get("results", []))
                except UbusError:
                    pass

                interfaces.append(wifi)

        return interfaces

    async def get_network_interfaces(self) -> list[NetworkInterface]:
        """Get network interface information."""
        interfaces: list[NetworkInterface] = []

        try:
            status = await self._call("network.interface", "dump")
        except UbusError:
            return interfaces

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

            dns_servers = iface_data.get("dns-server", [])
            iface.dns_servers = dns_servers

            dev_name = iface.device
            if dev_name:
                try:
                    dev_status = await self._call(
                        "network.device", "status", {"name": dev_name}
                    )
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
                    iface.speed = dev_status.get("speed", "")
                except UbusError:
                    pass

            interfaces.append(iface)

        return interfaces

    async def get_connected_devices(self) -> list[ConnectedDevice]:
        """Get connected devices by combining DHCP leases, ARP, and wireless clients."""
        devices: dict[str, ConnectedDevice] = {}

        try:
            leases = await self.get_dhcp_leases()
            for lease in leases:
                mac = lease.mac.lower()
                devices[mac] = ConnectedDevice(
                    mac=mac,
                    ip=lease.ip,
                    hostname=lease.hostname,
                    is_wireless=False,
                    connected=False,  # DHCP leases are just records, not proof of connectivity
                )
        except UbusError, Exception:  # noqa: BLE001
            pass

        # Fetch wireless_data once for both iwinfo and hostapd processing
        wireless_data: dict[str, Any] = {}
        try:
            wireless_data = await self._call("network.wireless", "status")
        except UbusError:
            pass

        if wireless_data:
            for _radio_name, radio_data in wireless_data.items():
                if not isinstance(radio_data, dict):
                    continue
                for iface in radio_data.get("interfaces", []):
                    iface_name = iface.get("ifname") or iface.get("device", "")
                    if not iface_name:
                        continue
                    try:
                        assoc = await self._call(
                            "iwinfo", "assoclist", {"device": iface_name}
                        )
                        for client in assoc.get("results", []):
                            mac = client.get("mac", "").lower()
                            if mac in devices:
                                dev = devices[mac]
                            else:
                                dev = ConnectedDevice(mac=mac, connected=True)
                                devices[mac] = dev

                            dev.is_wireless = True
                            dev.interface = iface_name
                            if (
                                not dev.connection_type
                                or dev.connection_type == "wired"
                            ):
                                if "5g" in iface_name.lower():
                                    dev.connection_type = "5GHz"
                                elif "2g" in iface_name.lower():
                                    dev.connection_type = "2.4GHz"
                                else:
                                    dev.connection_type = "wireless"
                            dev.signal = client.get("signal", 0)
                            dev.noise = client.get("noise", 0)
                            dev.rx_rate = (
                                client.get("rx", {}).get("rate", 0)
                                if isinstance(client.get("rx"), dict)
                                else client.get("rx_rate", 0)
                            )
                            dev.tx_rate = (
                                client.get("tx", {}).get("rate", 0)
                                if isinstance(client.get("tx"), dict)
                                else client.get("tx_rate", 0)
                            )
                    except UbusError:
                        pass

        try:
            neighbors = await self.get_ip_neighbors()
            for neigh in neighbors:
                mac = neigh.mac.lower()
                if not mac or mac in devices:
                    # Update existing device if it was found via DHCP but not neighbors
                    if mac in devices:
                        dev = devices[mac]
                        if not dev.neighbor_state:
                            dev.neighbor_state = neigh.state
                        if not dev.interface:
                            dev.interface = neigh.interface
                    continue

                # Consider connected only if state is active (REACHABLE, DELAY, PROBE)
                # STALE or FAILED means it was seen but is not currently active
                active_states = ("REACHABLE", "DELAY", "PROBE", "PERMANENT")
                is_active = neigh.state.upper() in active_states

                devices[mac] = ConnectedDevice(
                    mac=mac,
                    ip=neigh.ip,
                    interface=neigh.interface,
                    is_wireless=False,
                    connected=is_active,
                    connection_type="wired",
                    neighbor_state=neigh.state,
                )
        except Exception as neigh_err:  # noqa: BLE001
            _LOGGER.debug(
                "Error processing IP neighbors in get_connected_devices: %s", neigh_err
            )

        if wireless_data:
            for _radio_name, radio_data in wireless_data.items():
                if not isinstance(radio_data, dict):
                    continue
                for iface in radio_data.get("interfaces", []):
                    iface_name = iface.get("ifname", "")
                    if not iface_name:
                        continue
                    try:
                        hostapd_data = await self._call(
                            f"hostapd.{iface_name}", "get_clients"
                        )
                        for mac_addr, client_data in hostapd_data.get(
                            "clients", {}
                        ).items():
                            mac = mac_addr.lower()
                            if mac in devices:
                                dev = devices[mac]
                            else:
                                dev = ConnectedDevice(mac=mac, connected=True)
                                devices[mac] = dev
                            dev.is_wireless = True
                            dev.interface = iface_name
                            dev.rx_bytes = (
                                client_data.get("bytes", {}).get("rx", 0)
                                if isinstance(client_data.get("bytes"), dict)
                                else 0
                            )
                            dev.tx_bytes = (
                                client_data.get("bytes", {}).get("tx", 0)
                                if isinstance(client_data.get("bytes"), dict)
                                else 0
                            )
                            if (
                                not dev.connection_type
                                or dev.connection_type == "wired"
                            ):
                                dev.connection_type = "wireless"
                                if "5g" in iface_name.lower():
                                    dev.connection_type = "5GHz"
                                elif "2g" in iface_name.lower():
                                    dev.connection_type = "2.4GHz"

                    except UbusError:
                        pass
        else:
            # Fallback: list all hostapd objects directly
            try:
                ubus_objects = await self._call("ubus", "list")
                if isinstance(ubus_objects, dict):
                    for obj_name in ubus_objects:
                        if obj_name.startswith("hostapd."):
                            iface_name = obj_name.split(".", 1)[1]
                            try:
                                hostapd_data = await self._call(obj_name, "get_clients")
                                for mac_addr, client_data in hostapd_data.get(
                                    "clients", {}
                                ).items():
                                    mac = mac_addr.lower()
                                    if mac in devices:
                                        dev = devices[mac]
                                    else:
                                        dev = ConnectedDevice(mac=mac, connected=False)
                                        devices[mac] = dev
                                    dev.connected = True  # Wireless association
                                    dev.is_wireless = True
                                    dev.interface = iface_name
                                    dev.rx_bytes = (
                                        client_data.get("bytes", {}).get("rx", 0)
                                        if isinstance(client_data.get("bytes"), dict)
                                        else 0
                                    )
                                    dev.tx_bytes = (
                                        client_data.get("bytes", {}).get("tx", 0)
                                        if isinstance(client_data.get("bytes"), dict)
                                        else 0
                                    )
                                    if (
                                        not dev.connection_type
                                        or dev.connection_type == "wired"
                                    ):
                                        dev.connection_type = "wireless"
                                        if "5g" in iface_name.lower():
                                            dev.connection_type = "5GHz"
                                        elif "2g" in iface_name.lower():
                                            dev.connection_type = "2.4GHz"
                            except UbusError:
                                continue
            except Exception as list_err:
                _LOGGER.debug("Error listing ubus objects for fallback: %s", list_err)

        for dev in devices.values():
            if not dev.connection_type:
                dev.connection_type = "wireless" if dev.is_wireless else "wired"

        return list(devices.values())

    async def check_permissions(self) -> OpenWrtPermissions:
        """Check user permissions via ubus session list and uci tests."""
        import dataclasses

        from .base import OpenWrtPermissions

        perms = OpenWrtPermissions()
        try:
            # Check if we are root user - root usually has full access even if ACL list is empty or restricted
            if self.username == "root":
                for field in dataclasses.fields(perms):
                    if not field.name.startswith("_"):
                        setattr(perms, field.name, True)
                return perms

            # Check session access list - this is the most definitive way
            session_list = await self._call("session", "list")
            if session_list and ("acls" in session_list or "access" in session_list.get("values", {})):
                access = session_list.get("values", {}).get("access", {})

                def has_perm(obj: str, method: str) -> bool:
                    # Check in modern 'acls' structure if present
                    acls = session_list.get("acls", {})
                    if acls:
                        # Check ubus/uci/file objects
                        for section in ["ubus", "uci", "file"]:
                            if section in acls and isinstance(acls[section], dict):
                                for pattern, methods in acls[section].items():
                                    if (
                                        pattern == "*"
                                        or pattern == obj
                                        or (
                                            pattern.endswith("*")
                                            and obj.startswith(pattern[:-1])
                                        )
                                    ):
                                        if "*" in methods or method in methods:
                                            return True

                    # Fallback to legacy 'values.access' structure
                    access = session_list.get("values", {}).get("access", {})
                    for pattern, methods in access.items():
                        if (
                            pattern == "*"
                            or pattern == obj
                            or (pattern.endswith("*") and obj.startswith(pattern[:-1]))
                        ):
                            if "*" in methods or method in methods:
                                return True
                    return False

                perms.read_system = has_perm("system", "board") or has_perm(
                    "system", "read"
                )
                perms.write_system = has_perm("system", "reboot") or has_perm(
                    "system", "write"
                )
                perms.read_network = (
                    has_perm("network.interface", "dump")
                    or has_perm("network.interface", "read")
                    or has_perm("network", "read")
                )
                perms.write_network = (
                    has_perm("network.interface", "up")
                    or has_perm("network.interface", "write")
                    or has_perm("network", "write")
                )
                perms.read_firewall = has_perm("firewall", "read") or has_perm(
                    "uci", "read"
                )
                perms.write_firewall = has_perm("firewall", "write") or has_perm(
                    "uci", "write"
                )
                perms.read_wireless = (
                    has_perm("iwinfo", "read")
                    or has_perm("hostapd.*", "read")
                    or has_perm("network.wireless", "read")
                )
                perms.write_wireless = (
                    has_perm("iwinfo", "write")
                    or has_perm("hostapd.*", "write")
                    or has_perm("network.wireless", "write")
                )
                perms.read_services = (
                    has_perm("file", "read")
                    or has_perm("luci", "read")
                    or has_perm("service", "read")
                )
                perms.write_services = (
                    has_perm("file", "write")
                    or has_perm("luci", "write")
                    or has_perm("service", "write")
                )
                perms.read_sqm = has_perm("uci", "read") or has_perm("luci", "read")
                perms.write_sqm = has_perm("uci", "write") or has_perm("luci", "write")
                perms.read_vpn = has_perm("network.interface", "read") or has_perm(
                    "uci", "read"
                )
                perms.read_mwan = has_perm("uci", "read") or has_perm("file", "read")
                perms.read_led = has_perm("file", "read") or has_perm("uci", "read")
                perms.write_led = has_perm("file", "write") or has_perm("uci", "write")
                perms.read_devices = (
                    has_perm("network.interface", "read")
                    or has_perm("dhcp", "read")
                    or has_perm("file", "read")
                )
                perms.write_devices = has_perm("file", "exec") or has_perm(
                    "hostapd.*", "write"
                )
                perms.write_access_control = has_perm("uci", "write") or has_perm(
                    "firewall", "write"
                )

                # If we got definitive access list, we are done
                return perms

            # Fallback to manual probes
            async def can_call(
                obj: str, method: str, params: dict | None = None
            ) -> bool:
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
            perms.read_devices = (
                await can_call("dhcp", "ipv4leases") or perms.read_network
            )
            perms.write_devices = (
                await can_call("file", "exec", {"command": "/usr/bin/id"})
                or await can_call("file", "exec", {"command": "id"})
            )
            perms.write_access_control = perms.write_firewall
            perms.read_services = await can_call("service", "list")
            perms.write_services = await can_call("service", "list")

        except Exception as err:
            _LOGGER.debug("Error checking permissions via ubus: %s", err)
            if self.connected:
                perms.read_system = True
                perms.read_network = True

        return perms

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

            # Step 2: Try executing a small script for remaining/all (fastest for root)
            try:
                cmd = (
                    "for f in /etc/init.d/sqm /etc/init.d/mwan3 /usr/bin/iwinfo "
                    "/usr/bin/etherwake /usr/bin/wg /usr/sbin/openvpn "
                    "/www/cgi-bin/luci; do "
                    "if [ -f $f ] || [ -x $f ]; then echo 1; else echo 0; fi; done"
                )
                result = await self._call(
                    "file", "exec", {"command": "/bin/sh", "params": ["-c", cmd]}
                )
                out = result.get("stdout", "")
                results = out.strip().split("\n")

                def detect_status(idx: int) -> bool:
                    return len(results) > idx and results[idx].strip() == "1"

                if packages.sqm_scripts is not True:
                    packages.sqm_scripts = detect_status(0)
                if packages.mwan3 is not True:
                    packages.mwan3 = detect_status(1)
                if packages.iwinfo is not True:
                    packages.iwinfo = detect_status(2)
                packages.etherwake = detect_status(3)
                packages.wireguard = detect_status(4)
                packages.openvpn = detect_status(5)
                if packages.luci_mod_rpc is not True:
                    packages.luci_mod_rpc = detect_status(6)
            except Exception as err:
                _LOGGER.debug(
                    "Package check via file.exec failed (expected on restricted routers): %s",
                    err,
                )

            # Step 3: Check UCI configs for remaining packages (needs uci: ["*"])
            if packages.sqm_scripts is not True:
                try:
                    await self._call("uci", "get", {"config": "sqm"})
                    packages.sqm_scripts = True
                except Exception:
                    pass
            if packages.mwan3 is not True:
                try:
                    await self._call("uci", "get", {"config": "mwan3"})
                    packages.mwan3 = True
                except Exception:
                    pass
            if packages.openvpn is not True:
                try:
                    await self._call("uci", "get", {"config": "openvpn"})
                    packages.openvpn = True
                except Exception:
                    pass
            if packages.wireguard is not True:
                try:
                    res = await self._call("uci", "get", {"config": "network"})
                    # Look for wireguard interface sections
                    if res and isinstance(res, dict) and any(v.get("proto") == "wireguard" for v in res.values() if isinstance(v, dict)):
                        packages.wireguard = True
                    elif "wg" in objects: # objects from Step 1
                        packages.wireguard = True
                except Exception:
                    pass

            # Step 4: Fallback to file.stat (last resort, needs file: ["stat", "/path"])
            check_list = [
                ("/usr/bin/etherwake", "etherwake"),
                ("/usr/bin/wg", "wireguard"),
            ]
            for path, attr in check_list:
                if getattr(packages, attr) is not True:
                    try:
                        stat = await self._call("file", "stat", {"path": path})
                        if stat and "type" in stat:
                            setattr(packages, attr, True)
                    except Exception:
                        pass

            # Initialize remaining to False
            for attr in [
                "sqm_scripts",
                "mwan3",
                "iwinfo",
                "etherwake",
                "wireguard",
                "openvpn",
                "luci_mod_rpc",
            ]:
                if getattr(packages, attr) is None:
                    setattr(packages, attr, False)

        except Exception as err:
            _LOGGER.error("Failed to check packages via ubus: %s", err)
        return packages

    async def get_ip_neighbors(self) -> list[IpNeighbor]:
        """Get IP neighbor (ARP/NDP) table."""
        neighbors: list[IpNeighbor] = []

        # 1. Try ubus network.device status (more robust as it doesn't need file.exec)
        try:
            status = await self._call("network.device", "status")
            # This call returns a dictionary of devices. We iterate over them to find neighbors.
            for dev_name, dev_info in status.items():
                if not isinstance(dev_info, dict):
                    continue
                # Some OpenWrt versions show neighbors here
                neighbors_list = dev_info.get("neighbors", [])
                for neigh in neighbors_list:
                    mac = neigh.get("lladdr")
                    ip = neigh.get("address")
                    if mac and ip:
                        neighbors.append(
                            IpNeighbor(
                                ip=ip,
                                mac=mac.upper(),
                                interface=dev_name,
                                state=neigh.get("state", "REACHABLE"),
                            )
                        )
        except Exception:  # noqa: BLE001
            pass

        # 2. Try file.exec ip neigh show (only works if permissions allow)
        if not neighbors:
            try:
                result = await self._call(
                    "file", "exec", {"command": "ip", "params": ["neigh", "show"]}
                )
                content = result.get("stdout", "")

                for line in content.strip().split("\n"):
                    if not line:
                        continue
                    parts = line.split()
                    if len(parts) >= 4:
                        ip = parts[0]
                        mac = ""
                        interface = ""
                        state = parts[-1]

                        if "lladdr" in parts:
                            idx = parts.index("lladdr")
                            if len(parts) > idx + 1:
                                mac = parts[idx + 1]
                        if "dev" in parts:
                            idx = parts.index("dev")
                            if len(parts) > idx + 1:
                                interface = parts[idx + 1]

                        if mac:
                            neighbors.append(
                                IpNeighbor(
                                    ip=ip,
                                    mac=mac.upper(),
                                    interface=interface,
                                    state=state,
                                )
                            )
            except Exception:  # noqa: BLE001
                pass

        # 3. Fallback to /proc/net/arp via file.read (passive)
        if not neighbors:
            try:
                result = await self._call("file", "read", {"path": "/proc/net/arp"})
                content = result.get("data", "")
                if content:
                    lines = content.strip().split("\n")
                    # Skip header
                    for line in lines[1:]:
                        parts = line.split()
                        if len(parts) >= 6:
                            neighbors.append(
                                IpNeighbor(
                                    ip=parts[0],
                                    mac=parts[3].upper(),
                                    interface=parts[5],
                                    state="REACHABLE",
                                )
                            )
            except Exception as fallback_exc:  # noqa: BLE001
                _LOGGER.debug("Fallback to /proc/net/arp failed: %s", fallback_exc)

        return neighbors

    async def get_dhcp_leases(self) -> list[DhcpLease]:
        """Get DHCP leases via ubus or file."""
        if self.dhcp_software == "none":
            return []

        leases: list[DhcpLease] = []

        # Try odhcpd via ubus
        if self.dhcp_software in ("auto", "odhcpd"):
            try:
                result = await self._call("dhcp", "ipv4leases")
                for lease_data in result.get("dhcp_leases", []):
                    leases.append(
                        DhcpLease(
                            hostname=lease_data.get("hostname", ""),
                            mac=lease_data.get("mac", "").lower(),
                            ip=lease_data.get("ipaddr", ""),
                            expires=lease_data.get("expires", 0),
                        )
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
            try:
                # Priority 1: file.read (more robust/standard)
                result = await self._call("file", "read", {"path": "/tmp/dhcp.leases"})
                content = result.get("data", "")
            except UbusError:
                pass

            if not content:
                try:
                    # Priority 2: file.exec (original fallback)
                    content = await self.execute_command(
                        "cat /tmp/dhcp.leases 2>/dev/null"
                    )
                except Exception:
                    pass

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
                            )
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
                    )
                )
        except UbusError:
            _LOGGER.debug("MWAN3 not available (not installed or no permissions)")

        return statuses

    async def get_wps_status(self) -> WpsStatus:
        """Get WPS status from the first wireless interface."""
        try:
            wireless_data = await self._call("network.wireless", "status")
            for _radio_name, radio_data in wireless_data.items():
                if not isinstance(radio_data, dict):
                    continue
                for iface in radio_data.get("interfaces", []):
                    iface_name = iface.get("ifname", "")
                    if iface_name:
                        try:
                            result = await self._call(
                                f"hostapd.{iface_name}", "wps_status"
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
            for _radio_name, radio_data in wireless_data.items():
                if not isinstance(radio_data, dict):
                    continue
                for iface in radio_data.get("interfaces", []):
                    iface_name = iface.get("ifname", "")
                    if iface_name:
                        method = "wps_start" if enabled else "wps_cancel"
                        await self._call(f"hostapd.{iface_name}", method)
                        return True
        except UbusError as err:
            _LOGGER.error("Failed to set WPS: %s", err)

        return False

    async def get_services(self) -> list[ServiceInfo]:
        """Get init.d services via the rc ubus interface."""
        services: list[ServiceInfo] = []
        try:
            result = await self._call("rc", "list")
            for name, data in result.items():
                services.append(
                    ServiceInfo(
                        name=name,
                        enabled=data.get("enabled", False),
                        running=data.get("running", False),
                    )
                )
        except UbusError:
            _LOGGER.debug(
                "Cannot list services via rc ubus (missing permissions or package)"
            )

        return services

    async def manage_service(self, name: str, action: str) -> bool:
        """Manage (start/stop/restart/enable/disable) an init.d service."""
        try:
            await self._call("rc", "init", {"name": name, "action": action})
            return True
        except UbusPermissionError as err:
            _LOGGER.debug("Service %s via ubus denied (permissions): %s", action, err)
            return False
        except UbusError as err:
            _LOGGER.error("Failed to %s service %s: %s", action, name, err)
            return False

    async def get_installed_packages(self) -> list[str]:
        """Get a list of installed packages via opkg."""
        try:
            output = await self.execute_command("opkg list-installed | cut -d' ' -f1")
            return [line.strip() for line in output.splitlines() if line.strip()]
        except UbusError:
            _LOGGER.debug("Failed to list installed packages via Ubus")
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
            return True
        except UbusError:
            return False

    async def get_firewall_rules(self) -> list[FirewallRule]:
        """Get general firewall rules via UCI."""
        rules: list[FirewallRule] = []
        try:
            config = await self._call("uci", "get", {"config": "firewall"})
            values = config.get("values", {})

            for section_id, section_data in values.items():
                if section_data.get(".type") != "rule":
                    continue

                rules.append(
                    FirewallRule(
                        name=section_data.get("name", section_id),
                        enabled=str(section_data.get("enabled", "1")) == "1",
                        section_id=section_id,
                        target=section_data.get("target", ""),
                        src=section_data.get("src", ""),
                        dest=section_data.get("dest", ""),
                    )
                )
        except UbusError:
            pass
        return rules

    async def get_firewall_redirects(self) -> list[FirewallRedirect]:
        """Get firewall port forwarding redirects via UCI."""
        redirects: list[FirewallRedirect] = []
        try:
            config = await self._call("uci", "get", {"config": "firewall"})
            values = config.get("values", {})

            for section_id, section_data in values.items():
                if section_data.get(".type") != "redirect":
                    continue

                redirects.append(
                    FirewallRedirect(
                        name=section_data.get("name", section_id),
                        target_ip=section_data.get("dest_ip", ""),
                        target_port=section_data.get("dest_port", ""),
                        external_port=section_data.get("src_dport", ""),
                        protocol=section_data.get("proto", "tcp"),
                        enabled=str(section_data.get("enabled", "1")) == "1",
                        section_id=section_id,
                    )
                )
        except UbusError:
            pass
        return redirects

    async def set_firewall_redirect_enabled(
        self, section_id: str, enabled: bool
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
            return True
        except UbusError:
            return False

    async def get_access_control(self) -> list[AccessControl]:
        """Get list of access control rules via UCI firewall rules."""
        rules: list[AccessControl] = []
        try:
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
                        )
                    )
        except UbusError:
            pass
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
                        "uci", "add", {"config": "firewall", "type": "rule"}
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
            else:
                if section_id:
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
            return True
        except UbusError:
            return False

    async def get_leds(self) -> list:
        """Get LEDs from /sys/class/leds via file.exec."""
        from .base import LedInfo

        leds: list[LedInfo] = []
        try:
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
                        )
                    )
        except UbusError:
            _LOGGER.debug("Cannot list LEDs (missing file exec permission)")

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
        """Execute a command via ubus file.exec."""
        try:
            # Split command and params if needed, but ubus file.exec expects base command and params list
            # For simplicity, we wrap in shell
            res = await self._call(
                "file", "exec", {"command": "sh", "params": ["-c", command]}
            )
            if not res or not isinstance(res, dict):
                return ""
            return res.get("stdout", "")
        except UbusPermissionError as err:
            _LOGGER.debug(
                "Permission denied for command via ubus file.exec: %s (%s)", command, err
            )
            return ""
        except UbusError as err:
            _LOGGER.debug("Command failed via ubus file.exec: %s (%s)", command, err)
            return ""

    async def user_exists(self, username: str) -> bool:
        """Check if a system user exists on the device."""
        # 1. Try via ubus file.read (more robust/standard than exec)
        try:
            res = await self._call("file", "read", {"path": "/etc/passwd"})
            if res and "data" in res:
                if f"{username}:" in res["data"]:
                    return True
        except Exception:
            pass

        # 2. Fallback to base method (which uses execute_command)
        return await super().user_exists(username)

    async def provision_user(self, username: str, password: str) -> tuple[bool, str | None]:
        """Create a dedicated system user and configure RPC permissions via ubus."""
        # Use the harmonized provisioning script from base
        script = PROVISION_SCRIPT_TEMPLATE.format(username=username, password=password)
        try:
            output = await self.execute_command(script)
            if output:
                _LOGGER.debug("Provisioning output for %s: %s", username, output)

            if "Provisioning SUCCESS" in output:
                return True, None

            if "LOG: FAIL:" in output:
                fail_msg = output.split("LOG: FAIL:")[1].splitlines()[0].strip()
                _LOGGER.error("Provisioning failed: %s", fail_msg)
                return False, fail_msg

            return (
                False,
                "Provisioning script returned failure without specific error. Check router logs (logread).",
            )
        except Exception as err:
            _LOGGER.error("Failed to provision user %s via ubus: %s", username, err)
            return False, str(err)

    async def manage_interface(self, name: str, action: str) -> bool:
        """Manage a network interface (up/down/reconnect) via ubus."""
        try:
            if action == "reconnect":
                await self._call("network.interface", "up", {"interface": name})
            elif action == "up":
                await self._call("network.interface", "up", {"interface": name})
            elif action == "down":
                await self._call("network.interface", "down", {"interface": name})
            return True
        except UbusError:
            return False

    async def install_firmware(self, url: str) -> None:
        """Install firmware from the given URL via ubus."""
        cmd = f"wget -O /tmp/firmware.bin '{url}' && sysupgrade /tmp/firmware.bin"
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
                    "Ubus connection lost during sysupgrade - device is rebooting"
                )
                return
            _LOGGER.warning(
                "Sysupgrade command might have failed or disconnected: %s", err
            )

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
                "file", "exec", {"command": "/etc/init.d/sqm", "params": ["reload"]}
            )
            return True
        except UbusPermissionError as err:
            _LOGGER.debug("SQM config via ubus denied (permissions): %s", err)
            return False
        except Exception as err:
            _LOGGER.error("Failed to set SQM config: %s", err)
            return False

    async def get_gateway_mac(self) -> str | None:
        """Get the default gateway MAC address via ubus."""
        try:
            # 1. Get default gateway IP from network.interface dump
            status = await self._call("network.interface", "dump")
            gw_ip = None
            for iface_data in status.get("interface", []):
                # Look for wan and check ipv4-address
                if iface_data.get("interface", "").lower() in [
                    "wan",
                    "wan6",
                    "wwan",
                    "modem",
                ]:
                    ipv4_addrs = iface_data.get("ipv4-address", [])
                    for addr in ipv4_addrs:
                        if addr.get("gateway"):
                            gw_ip = addr.get("gateway")
                            break
                    if gw_ip:
                        break

            if not gw_ip:
                # Fallback: check all interfaces if no obvious WAN
                for iface_data in status.get("interface", []):
                    ipv4_addrs = iface_data.get("ipv4-address", [])
                    for addr in ipv4_addrs:
                        if addr.get("gateway"):
                            gw_ip = addr.get("gateway")
                            break
                    if gw_ip:
                        break

            if not gw_ip:
                return None

            # 2. Get MAC for that IP via ip neighbor (using execute_command fallback)
            neigh_out = await self.execute_command(f"ip neigh show {gw_ip} 2>/dev/null")
            if "lladdr" in neigh_out:
                neigh_parts = neigh_out.split()
                return neigh_parts[neigh_parts.index("lladdr") + 1].upper()
        except Exception as err:
            _LOGGER.debug("Failed to get gateway MAC via ubus: %s", err)
        return None

    async def get_lldp_neighbors(self) -> list[LldpNeighbor]:
        """Get LLDP neighbor information via ubus."""
        from .base import LldpNeighbor

        neighbors: list[LldpNeighbor] = []
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
                                    neighbor_port=neigh.get("port", {}).get("id", "")
                                    if isinstance(neigh.get("port"), dict)
                                    else "",
                                    neighbor_chassis=neigh.get("chassis", {}).get(
                                        "id", ""
                                    )
                                    if isinstance(neigh.get("chassis"), dict)
                                    else "",
                                    neighbor_description=neigh.get("description", ""),
                                    neighbor_system_name=neigh.get("sysname", ""),
                                )
                            )
        except Exception as err:
            _LOGGER.debug("Failed to get LLDP neighbors via ubus: %s", err)
        return neighbors
