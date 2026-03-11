"""OpenWrt LuCI RPC API client.

Communicates with OpenWrt via the LuCI web interface JSON-RPC API.
This is a fallback method when ubus HTTP is not available but the
LuCI web interface is installed.

Supports authentication via LuCI sysauth token.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import aiohttp

from .base import (
    ConnectedDevice,
    DeviceInfo,
    DhcpLease,
    NetworkInterface,
    OpenWrtClient,
    SystemResources,
    WirelessInterface,
)

_LOGGER = logging.getLogger(__name__)


class LuciRpcError(Exception):
    """Error communicating with LuCI RPC."""


class LuciRpcAuthError(LuciRpcError):
    """Authentication error."""


class LuciRpcTimeoutError(LuciRpcError):
    """Connection or request timeout."""


class LuciRpcConnectionError(LuciRpcError):
    """TCP connection failure (e.g. refused, unreachable)."""


class LuciRpcSslError(LuciRpcError):
    """SSL/TLS verification failure."""


class LuciRpcPackageMissingError(LuciRpcError):
    """Required package missing (e.g. 404 on /cgi-bin/luci/rpc)."""


class LuciRpcClient(OpenWrtClient):
    """Client for OpenWrt LuCI JSON-RPC API."""

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        port: int = 80,
        use_ssl: bool = False,
        verify_ssl: bool = False,
    ) -> None:
        """Initialize the LuCI RPC client."""
        super().__init__(host, username, password, port, use_ssl, verify_ssl)
        self._auth_token: str = ""
        self._session: aiohttp.ClientSession | None = None
        self._rpc_id: int = 0

    @property
    def _base_url(self) -> str:
        """Return base URL for LuCI."""
        scheme = "https" if self.use_ssl else "http"
        return f"{scheme}://{self.host}:{self.port}"

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

    async def _rpc_call(
        self,
        endpoint: str,
        method: str,
        params: list[Any] | None = None,
        reauthenticated: bool = False,
    ) -> Any:
        """Make a LuCI JSON-RPC call."""
        session = await self._ensure_session()
        self._rpc_id += 1

        url = f"{self._base_url}/cgi-bin/luci/rpc/{endpoint}"
        if self._auth_token:
            url += f"?auth={self._auth_token}"

        payload = {
            "id": self._rpc_id,
            "method": method,
            "params": params or [],
        }

        try:
            async with session.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json"},
            ) as response:
                if response.status == 403:
                    if self._auth_token and not reauthenticated:
                        self._auth_token = ""
                        await self.connect()
                        return await self._rpc_call(
                            endpoint, method, params, reauthenticated=True
                        )
                    raise LuciRpcError(f"Access denied to LuCI RPC on {self.host}")

                if response.status == 404:
                    raise LuciRpcPackageMissingError(
                        f"LuCI RPC endpoint not found on {self.host}. Is 'luci-mod-rpc' installed?"
                    )

                response.raise_for_status()
                data = await response.json()
        except TimeoutError as err:
            raise LuciRpcTimeoutError(
                f"Timeout communicating with LuCI on {self.host}"
            ) from err
        except aiohttp.ClientConnectorError as err:
            raise LuciRpcConnectionError(
                f"Cannot connect to LuCI on {self.host}: {err}"
            ) from err
        except aiohttp.ClientSSLError as err:
            raise LuciRpcSslError(
                f"SSL error connecting to LuCI on {self.host}: {err}"
            ) from err
        except aiohttp.ClientError as err:
            self._connected = False
            raise LuciRpcError(f"Communication error: {err}") from err

        if "error" in data and data["error"] is not None:
            raise LuciRpcError(f"RPC error: {data['error']}")

        return data.get("result")

    async def connect(self) -> bool:
        """Authenticate with LuCI."""
        session = await self._ensure_session()
        self._rpc_id += 1

        url = f"{self._base_url}/cgi-bin/luci/rpc/auth"
        payload = {
            "id": self._rpc_id,
            "method": "login",
            "params": [self.username, self.password],
        }

        try:
            async with session.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json"},
            ) as response:
                if response.status == 404:
                    raise LuciRpcPackageMissingError(
                        "LuCI RPC auth endpoint not found. Is 'luci-mod-rpc' installed?"
                    )
                response.raise_for_status()
                data = await response.json()
        except TimeoutError as err:
            raise LuciRpcTimeoutError(f"Login timeout for LuCI on {self.host}") from err
        except aiohttp.ClientConnectorError as err:
            raise LuciRpcConnectionError(f"Cannot connect to LuCI: {err}") from err
        except aiohttp.ClientSSLError as err:
            raise LuciRpcSslError(f"SSL error connecting to LuCI: {err}") from err
        except aiohttp.ClientError as err:
            raise LuciRpcError(f"Cannot connect: {err}") from err

        result = data.get("result")
        if (
            result is None
            or result == "null"
            or (isinstance(result, str) and not result)
        ):
            _LOGGER.error("LuCI RPC auth returned no token: %s", data)
            raise LuciRpcAuthError(
                f"Authentication failed for {self.username}@{self.host}. Check credentials."
            )

        self._auth_token = result
        self._connected = True
        _LOGGER.debug("Authenticated with LuCI on %s", self.host)
        return True

    async def disconnect(self) -> None:
        """Disconnect and cleanup."""
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None
        self._connected = False

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
                "sys", "exec", ["cat /etc/openwrt_release"]
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

        return info

    async def get_system_resources(self) -> SystemResources:
        """Get system resource usage."""
        resources = SystemResources()

        meminfo = await self._rpc_call("sys", "exec", ["cat /proc/meminfo"])
        if meminfo:
            for line in meminfo.strip().split("\n"):
                parts = line.split()
                if len(parts) >= 2:
                    key = parts[0].rstrip(":")
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
            resources.memory_used = (
                resources.memory_total
                - resources.memory_free
                - resources.memory_buffered
                - resources.memory_cached
            )
            resources.swap_used = resources.swap_total - resources.swap_free

        try:
            loadavg = await self._rpc_call("sys", "exec", ["cat /proc/loadavg"])
            if loadavg:
                parts = loadavg.strip().split()
                if len(parts) >= 3:
                    resources.load_1min = float(parts[0])
                    resources.load_5min = float(parts[1])
                    resources.load_15min = float(parts[2])
        except LuciRpcError:
            pass

        try:
            uptime_str = await self._rpc_call("sys", "exec", ["cat /proc/uptime"])
            if uptime_str:
                resources.uptime = int(float(uptime_str.strip().split()[0]))
        except LuciRpcError:
            pass

        # Thermal
        try:
            for zone in range(3):
                temp = await self._rpc_call(
                    "sys",
                    "exec",
                    [f"cat /sys/class/thermal/thermal_zone{zone}/temp 2>/dev/null"],
                )
                if temp and temp.strip().isdigit():
                    resources.temperature = float(temp.strip()) / 1000.0
                    break
        except LuciRpcError:
            pass

        # Storage
        try:
            df = await self._rpc_call(
                "sys", "exec", ["df /overlay 2>/dev/null || df / 2>/dev/null"]
            )
            if df:
                lines = df.strip().split("\n")
                if len(lines) >= 2:
                    parts = lines[1].split()
                    if len(parts) >= 4:
                        resources.filesystem_total = int(parts[1]) * 1024
                        resources.filesystem_used = int(parts[2]) * 1024
                        resources.filesystem_free = int(parts[3]) * 1024
        except LuciRpcError:
            pass

        return resources

    async def get_external_ip(self) -> str | None:
        """Get public/external IP address."""
        try:
            status = await self._rpc_call(
                "sys", "exec", ["ubus call network.interface dump"]
            )
            if status:
                data = json.loads(status)
                for iface_data in data.get("interface", []):
                    iface_name = iface_data.get("interface", "").lower()
                    if iface_name in ["wan", "wan6", "wwan", "modem"]:
                        ipv4_addrs = iface_data.get("ipv4-address", [])
                        if ipv4_addrs:
                            return ipv4_addrs[0].get("address")
        except LuciRpcError, json.JSONDecodeError:
            pass
        return None

    async def get_wireless_interfaces(self) -> list[WirelessInterface]:
        """Get wireless interfaces via iwinfo."""
        interfaces: list[WirelessInterface] = []

        try:
            await self._rpc_call(
                "sys",
                "exec",
                ["iwinfo | grep -E 'ESSID|Channel|Signal|Noise|Bit Rate'"],
            )
        except LuciRpcError:
            pass

        try:
            wireless_config = await self._rpc_call("uci", "get_all", ["wireless"])
            if isinstance(wireless_config, dict):
                for section, values in wireless_config.items():
                    if isinstance(values, dict) and values.get(".type") == "wifi-iface":
                        interfaces.append(
                            WirelessInterface(
                                name=section,
                                ssid=values.get("ssid", ""),
                                mode=values.get("mode", ""),
                                encryption=values.get("encryption", ""),
                                enabled=values.get("disabled", "0") != "1",
                            )
                        )
        except LuciRpcError:
            pass

        return interfaces

    async def get_network_interfaces(self) -> list[NetworkInterface]:
        """Get network interfaces."""
        interfaces: list[NetworkInterface] = []

        net_config = await self._rpc_call("uci", "get_all", ["network"])
        if isinstance(net_config, dict):
            for section, values in net_config.items():
                if isinstance(values, dict) and values.get(".type") == "interface":
                    iface = NetworkInterface(
                        name=section,
                        protocol=values.get("proto", ""),
                        device=str(values.get("device", values.get("ifname", ""))),
                    )
                    interfaces.append(iface)

        return interfaces

    async def get_connected_devices(self) -> list[ConnectedDevice]:
        """Get connected devices by combining DHCP, ARP and wireless station info via sys.exec."""
        devices: dict[str, ConnectedDevice] = {}

        # 1. DHCP Leases
        try:
            leases_str = await self._rpc_call(
                "sys", "exec", ["cat /tmp/dhcp.leases 2>/dev/null"]
            )
            if leases_str:
                for line in leases_str.strip().split("\n"):
                    parts = line.split()
                    if len(parts) >= 4:
                        mac = parts[1].lower()
                        devices[mac] = ConnectedDevice(
                            mac=mac,
                            ip=parts[2],
                            hostname=parts[3] if parts[3] != "*" else "",
                            connected=True,
                            is_wireless=False,
                        )
        except LuciRpcError:
            pass

        # 2. ARP Neighbors
        try:
            arp = await self._rpc_call("sys", "exec", ["cat /proc/net/arp 2>/dev/null"])
            if arp:
                lines = arp.strip().split("\n")
                if len(lines) > 1:
                    for line in lines[1:]:
                        parts = line.split()
                        if len(parts) >= 4:
                            mac = parts[3].lower()
                            if not mac or mac == "00:00:00:00:00:00":
                                continue
                            if mac not in devices:
                                devices[mac] = ConnectedDevice(
                                    mac=mac,
                                    ip=parts[0],
                                    connected=True,
                                    is_wireless=False,
                                )
        except LuciRpcError:
            pass

        # 3. Wireless Clients (iwinfo station dump)
        try:
            # Get wireless interfaces first
            iw_out = await self._rpc_call(
                "sys",
                "exec",
                ["iwinfo 2>/dev/null | grep -E '^[a-z0-9_-]+' | awk '{print $1}'"],
            )
            if iw_out:
                ifaces = iw_out.strip().split()
                for iface in ifaces:
                    assoc = await self._rpc_call(
                        "sys", "exec", [f"iwinfo {iface} assoclist 2>/dev/null"]
                    )
                    if assoc:
                        for line in assoc.strip().split("\n"):
                            if not line.strip() or "No information" in line:
                                continue
                            parts = line.split()
                            if len(parts) >= 1 and ":" in parts[0]:
                                mac = parts[0].lower()
                                if mac in devices:
                                    dev = devices[mac]
                                else:
                                    dev = ConnectedDevice(mac=mac, connected=True)
                                    devices[mac] = dev

                                dev.is_wireless = True
                                dev.interface = iface
                                if len(parts) >= 2:
                                    dev.signal = (
                                        int(parts[1])
                                        if parts[1].lstrip("-").isdigit()
                                        else 0
                                    )

                                if "5g" in iface.lower():
                                    dev.connection_type = "5GHz"
                                elif "2g" in iface.lower():
                                    dev.connection_type = "2.4GHz"
                                else:
                                    dev.connection_type = "wireless"
        except LuciRpcError:
            pass

        return list(devices.values())

    async def get_dhcp_leases(self) -> list[DhcpLease]:
        """Get DHCP leases."""
        leases: list[DhcpLease] = []

        try:
            leases_str = await self._rpc_call("sys", "exec", ["cat /tmp/dhcp.leases"])
            if leases_str:
                for line in leases_str.strip().split("\n"):
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
        except LuciRpcError:
            pass

        return leases

    async def reboot(self) -> bool:
        """Reboot the device."""
        try:
            await self._rpc_call("sys", "reboot")
            return True
        except LuciRpcError as err:
            _LOGGER.error("Failed to reboot: %s", err)
            return False

    async def execute_command(self, command: str) -> str:
        """Execute a command via LuCI RPC."""
        try:
            result = await self._rpc_call("sys", "exec", [command])
            return result or ""
        except LuciRpcError as err:
            _LOGGER.error("Failed to execute command: %s", err)
            return f"Error: {err}"

    async def install_firmware(self, url: str) -> None:
        """Install firmware from the given URL via LuCI RPC sys.exec."""
        try:
            download_cmd = f"wget -O /tmp/firmware.bin '{url}'"
            _LOGGER.info("Downloading firmware via LuCI RPC: %s", download_cmd)

            cmd = f'sh -c "{download_cmd} && nohup sysupgrade /tmp/firmware.bin > /dev/null 2>&1 &"'
            await self._rpc_call("sys", "exec", [cmd])
        except LuciRpcError as err:
            _LOGGER.error("Failed to initiate firmware upgrade via LuCI RPC: %s", err)
            raise LuciRpcError(f"Upgrade failed: {err}") from err

    async def get_leds(self) -> list:
        """Get LEDs from /sys/class/leds via sys.exec."""
        from .base import LedInfo

        leds: list[LedInfo] = []
        try:
            cmd = (
                "for led in /sys/class/leds/*/; do "
                'name=$(basename "$led"); '
                'brightness=$(cat "$led/brightness" 2>/dev/null || echo 0); '
                'max=$(cat "$led/max_brightness" 2>/dev/null || echo 255); '
                'trigger=$(cat "$led/trigger" 2>/dev/null | tr " " "\\n" | grep "^\\[" | tr -d "[]" || echo none); '
                'echo "$name|$brightness|$max|$trigger"; '
                "done"
            )
            output = await self._rpc_call("sys", "exec", [cmd])
            if output:
                for line in output.strip().splitlines():
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
        except LuciRpcError:
            _LOGGER.debug("Cannot list LEDs via LuCI RPC")

        return leds

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

    async def execute_command(self, command: str) -> str:
        """Execute a command via LuCI RPC (sys.exec)."""
        try:
            return await self._rpc_call("sys", "exec", [command]) or ""
        except LuciRpcError as err:
            _LOGGER.error("Failed to execute command via LuCI RPC: %s", err)
            raise

    async def set_wireless_enabled(self, interface: str, enabled: bool) -> bool:
        """Enable or disable a wireless radio via UCI."""
        try:
            action = "0" if enabled else "1"
            cmd = (
                f"uci set wireless.{interface}.disabled={action} && "
                "uci commit wireless && "
                "wifi reload"
            )
            await self.execute_command(cmd)
            return True
        except Exception:
            return False

    async def manage_interface(self, name: str, action: str) -> bool:
        """Manage a network interface via LuCI RPC."""
        try:
            if action == "reconnect":
                await self.execute_command(f"ifdown {name} && ifup {name}")
            elif action == "up":
                await self.execute_command(f"ifup {name}")
            elif action == "down":
                await self.execute_command(f"ifdown {name}")
            return True
        except Exception:
            return False

    async def install_firmware(self, url: str) -> None:
        """Install firmware from the given URL via LuCI RPC."""
        cmd = f"wget -O /tmp/firmware.bin '{url}' && sysupgrade /tmp/firmware.bin"
        try:
            _LOGGER.info("Initiating firmware installation via LuCI RPC from: %s", url)
            await self.execute_command(cmd)
        except Exception as err:
            # If it's a connection error, it's likely the router rebooting
            err_msg = str(err).lower()
            if any(
                msg in err_msg
                for msg in ["connection reset", "broken pipe", "closed", "eof", "timeout"]
            ):
                _LOGGER.info("LuCI RPC connection lost during sysupgrade - device is rebooting")
            else:
                _LOGGER.error("Failed to execute sysupgrade via LuCI RPC: %s", err)
                raise LuciRpcError(f"sysupgrade execution failed: {err}") from err

    async def get_installed_packages(self) -> list[str]:
        """Get a list of installed packages via opkg."""
        try:
            output = await self.execute_command("opkg list-installed | cut -d' ' -f1")
            return [line.strip() for line in output.splitlines() if line.strip()]
        except LuciRpcError:
            _LOGGER.debug("Failed to list installed packages via LuCI RPC")
            return []
