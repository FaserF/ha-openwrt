"""OpenWrt ubus HTTP/HTTPS API client.

Communicates with OpenWrt via the ubus JSON-RPC interface exposed through
uhttpd. This is the recommended and most feature-complete connection method.

Requires packages on OpenWrt: uhttpd, uhttpd-mod-ubus, rpcd, rpcd-mod-iwinfo
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

from .base import (
    AccessControl,
    ConnectedDevice,
    DeviceInfo,
    DhcpLease,
    FirewallRedirect,
    MwanStatus,
    NetworkInterface,
    OpenWrtClient,
    ServiceInfo,
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
    """Insufficient RPC permissions (e.g. 403 or ACL error)."""


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
    ) -> None:
        """Initialize the ubus client."""
        super().__init__(host, username, password, port, use_ssl, verify_ssl)
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
                    f"Access denied to ubus on {self.host}. Check RPC permissions."
                ) from err
            raise UbusError(f"HTTP error {err.status} from {self.host}") from err
        except aiohttp.ClientError as err:
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
                if code == 3:
                    raise UbusPermissionError(
                        f"RPC Error ({code}): Access denied to '{ubus_object}.{ubus_method}'"
                    )
                raise UbusError(
                    f"ubus error code {code} for {ubus_object}.{ubus_method}"
                )
            return result[1] if len(result) > 1 else {}

        return result

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
        info.model = data.get("model", data.get("board_name", ""))
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

        return info

    async def get_system_resources(self) -> SystemResources:
        """Get system resource usage."""
        data = await self._call("system", "info")
        memory = data.get("memory", {})
        swap = data.get("swap", {})
        load = data.get("load", [0, 0, 0])

        resources = SystemResources(
            memory_total=memory.get("total", 0),
            memory_used=memory.get("total", 0)
            - memory.get("free", 0)
            - memory.get("buffered", 0)
            - memory.get("cached", 0),
            memory_free=memory.get("free", 0),
            memory_buffered=memory.get("buffered", 0),
            memory_cached=memory.get("cached", 0),
            swap_total=swap.get("total", 0),
            swap_used=swap.get("total", 0) - swap.get("free", 0),
            swap_free=swap.get("free", 0),
            load_1min=load[0] / 65536.0 if len(load) > 0 else 0.0,
            load_5min=load[1] / 65536.0 if len(load) > 1 else 0.0,
            load_15min=load[2] / 65536.0 if len(load) > 2 else 0.0,
            uptime=data.get("uptime", 0),
        )

        try:
            # First try ubus system info disk
            fs_data = await self._call("system", "info")
            if "disk" in fs_data:
                disk = fs_data["disk"]
                root = disk.get("root", disk.get("/", {}))
                if isinstance(root, dict) and root.get("total"):
                    resources.filesystem_total = root.get("total", 0)
                    resources.filesystem_used = root.get("used", 0)
                    resources.filesystem_free = root.get("total", 0) - root.get(
                        "used", 0
                    )

            # Fallback to df if ubus disk info is missing
            if resources.filesystem_total == 0:
                # Busybox df -k returns blocks in 1K
                result = await self._call(
                    "file", "exec", {"command": "df", "params": ["-k"]}
                )
                stdout = result.get("stdout", "")
                for line in stdout.strip().split("\n"):
                    parts = line.split()
                    # Match overlay or mount point /
                    if len(parts) >= 6 and (parts[5] == "/" or parts[0] == "overlay"):
                        try:
                            resources.filesystem_total = int(parts[1]) * 1024
                            resources.filesystem_used = int(parts[2]) * 1024
                            resources.filesystem_free = int(parts[3]) * 1024
                            break
                        except (ValueError, IndexError):
                            continue
        except (UbusError, ValueError, IndexError):
            pass

        # Temperature fetching
        try:
            # Try various common paths for thermal sensors
            temp_paths = [
                "/sys/class/thermal/thermal_zone0/temp",
                "/sys/class/thermal/thermal_zone1/temp",
                "/sys/class/thermal/thermal_zone2/temp",
                "/sys/class/hwmon/hwmon0/temp1_input",
                "/sys/class/hwmon/hwmon0/device/temp1_input",
                "/sys/devices/virtual/thermal/thermal_zone0/temp",
                "/sys/devices/virtual/thermal/thermal_zone1/temp",
                "/sys/devices/virtual/thermal/thermal_zone2/temp",
            ]
            for path in temp_paths:
                try:
                    res = await self._call("file", "read", {"path": path})
                    temp_raw = res.get("data", "").strip()
                    if temp_raw.isdigit():
                        temp = float(temp_raw)
                        if temp > 200:  # Usually millidegrees
                            temp /= 1000.0
                        if 0 < temp < 150:  # Sanity check
                            resources.temperature = temp
                            break
                except UbusError:
                    continue

            # Fallback to execute_command if file.read failed or missing paths
            if resources.temperature is None:
                try:
                    # Some devices need explicit shell cat
                    out = await self.execute_command(
                        "cat /sys/class/thermal/thermal_zone*/temp 2>/dev/null"
                    )
                    if out:
                        temp_raw = out.split("\n")[0].strip()
                        if temp_raw.isdigit():
                            temp = float(temp_raw)
                            if temp > 200:
                                temp /= 1000.0
                            if 0 < temp < 150:
                                resources.temperature = temp
                except Exception:  # noqa: BLE001
                    pass
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
                    txpower=radio_data.get("config", {}).get("txpower", 0),
                    mesh_id=iface_config.get("mesh_id", ""),
                    mesh_fwding=iface_config.get("mesh_fwding", False),
                )

                try:
                    if iface_name:
                        iwinfo = await self._call(
                            "iwinfo", "info", {"device": iface_name}
                        )
                        wifi.channel = iwinfo.get("channel", 0)
                        wifi.frequency = str(iwinfo.get("frequency", ""))
                        wifi.signal = iwinfo.get("signal", 0)
                        wifi.noise = iwinfo.get("noise", 0)
                        wifi.bitrate = (
                            iwinfo.get("bitrate", 0) / 1000.0
                            if iwinfo.get("bitrate")
                            else 0.0
                        )
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
                    connected=True,
                )
        except (UbusError, Exception):  # noqa: BLE001
            pass

        try:
            wireless_data = await self._call("network.wireless", "status")
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
        except UbusError:
            pass

        try:
            neighbors = await self.get_neighbors()
            for neigh in neighbors:
                mac = neigh.get("mac", "").lower()
                if not mac or mac in devices:
                    continue
                devices[mac] = ConnectedDevice(
                    mac=mac,
                    ip=neigh.get("ip", ""),
                    is_wireless=False,
                    connected=True,
                    connection_type="wired",
                )
        except Exception:  # noqa: BLE001
            pass

        try:
            wireless_data = await self._call("network.wireless", "status")
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
        except Exception as err:
            _LOGGER.debug("Error fetching wireless status: %s", err)
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
                                continue
            except Exception as list_err:
                _LOGGER.debug("Error listing ubus objects for fallback: %s", list_err)

        for dev in devices.values():
            if not dev.connection_type:
                dev.connection_type = "wireless" if dev.is_wireless else "wired"

        return list(devices.values())

    async def get_neighbors(self) -> list[dict[str, str]]:
        """Get neighbor (ARP) table by reading /proc/net/arp."""
        neighbors: list[dict[str, str]] = []
        try:
            # First try file.read
            try:
                result = await self._call("file", "read", {"path": "/proc/net/arp"})
                content = result.get("data", "")
            except UbusError:
                # Fallback to file.exec if read is restricted
                result = await self._call(
                    "file", "exec", {"command": "cat", "params": ["/proc/net/arp"]}
                )
                content = result.get("stdout", "")

            lines = content.strip().split("\n")
            if len(lines) > 1:
                for line in lines[1:]:  # Skip header
                    parts = line.split()
                    if len(parts) >= 4:
                        neighbors.append(
                            {
                                "ip": parts[0],
                                "mac": parts[3].lower(),
                                "interface": parts[5] if len(parts) > 5 else "",
                            }
                        )
        except UbusError:
            pass
        return neighbors

    async def get_dhcp_leases(self) -> list[DhcpLease]:
        """Get DHCP leases via ubus or file."""
        leases: list[DhcpLease] = []

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
            return leases
        except UbusError:
            pass

        try:
            result = await self._call(
                "file",
                "read",
                {
                    "path": "/tmp/dhcp.leases",
                },
            )
            content = result.get("data", "")
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
        except UbusError:
            pass

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
        except UbusError as err:
            _LOGGER.error("Failed to %s service %s: %s", action, name, err)
            return False

    async def reboot(self) -> bool:
        """Reboot the device."""
        try:
            await self._call("system", "reboot")
            return True
        except UbusError as err:
            _LOGGER.error("Failed to reboot: %s", err)
            return False

    async def execute_command(self, command: str) -> str:
        """Execute a command via ubus file.exec."""
        try:
            parts = command.split()
            result = await self._call(
                "file",
                "exec",
                {
                    "command": parts[0],
                    "params": parts[1:] if len(parts) > 1 else [],
                    "env": {},  # Explicitly empty env to satisfy parser
                },
            )
            return result.get("stdout", "") + result.get("stderr", "")
        except UbusError as err:
            _LOGGER.error("Failed to execute command: %s", err)
            return f"Error: {err}"

    async def install_firmware(self, url: str) -> None:
        """Install firmware from the given URL via Ubus."""
        try:
            download_cmd = f"wget -O /tmp/firmware.bin '{url}'"
            _LOGGER.info("Downloading firmware via Ubus: %s", download_cmd)
            await self._call(
                "file",
                "exec",
                {
                    "command": "/bin/sh",
                    "params": [
                        "-c",
                        f"{download_cmd} && sysupgrade /tmp/firmware.bin > /dev/null 2>&1 &",
                    ],
                    "env": {},
                },
            )
        except UbusError as err:
            _LOGGER.error("Failed to initiate firmware upgrade via Ubus: %s", err)
            raise UbusError(f"Upgrade failed: {err}") from err

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

    async def manage_interface(self, name: str, action: str) -> bool:
        """Manage a network interface (up/down/reconnect) via ubus."""
        try:
            if action == "reconnect":
                await self._call(f"network.interface.{name}", "down")
                await self._call(f"network.interface.{name}", "up")
            else:
                await self._call(f"network.interface.{name}", action)
            return True
        except UbusError:
            return False

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
        """Execute a command via ubus (file.exec)."""
        try:
            result = await self._call(
                "file", "exec", {"command": "/bin/sh", "params": ["-c", command]}
            )
            return result.get("stdout", "")
        except UbusError as err:
            _LOGGER.error("Failed to execute command via ubus: %s", err)
            raise

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
                for msg in ["connection reset", "broken pipe", "closed", "eof", "timeout"]
            ):
                _LOGGER.info("Ubus connection lost during sysupgrade - device is rebooting")
                return
            _LOGGER.warning("Sysupgrade command might have failed or disconnected: %s", err)
