"""OpenWrt LuCI RPC API client.

Communicates with OpenWrt via the LuCI web interface JSON-RPC API.
This is a fallback method when ubus HTTP is not available but the
LuCI web interface is installed.

Supports authentication via LuCI sysauth token.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import aiohttp

from .base import (
    PROVISION_SCRIPT_TEMPLATE,
    ConnectedDevice,
    DeviceInfo,
    DhcpLease,
    FirewallRedirect,
    FirewallRule,
    IpNeighbor,
    LldpNeighbor,
    NetworkInterface,
    OpenWrtClient,
    OpenWrtPackages,
    OpenWrtPermissions,
    SqmStatus,
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
        dhcp_software: str = "auto",
    ) -> None:
        """Initialize the LuCI RPC client."""
        super().__init__(
            host, username, password, port, use_ssl, verify_ssl, dhcp_software
        )
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

                # Check content type to ensure it's JSON
                content_type = response.headers.get("Content-Type", "").lower()
                if "application/json" not in content_type:
                    text = await response.text()
                    if "<html" in text.lower():
                        _LOGGER.debug(
                            "Received HTML instead of JSON from LuCI RPC: %s",
                            text[:200],
                        )
                        raise LuciRpcPackageMissingError(
                            "LuCI RPC returned HTML instead of JSON. Is 'luci-mod-rpc' installed?"
                        )
                    raise LuciRpcError(
                        f"Unexpected content type from LuCI RPC: {content_type}"
                    )

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
            if not reauthenticated:
                _LOGGER.debug(
                    "LuCI RPC connection error (%s), retrying after session reset", err
                )
                await self.disconnect()
                return await self._rpc_call(
                    endpoint, method, params, reauthenticated=True
                )
            self._connected = False
            raise LuciRpcError(f"Communication error: {err}") from err

        return data.get("result")

    async def execute_command(self, command: str) -> str:
        """Execute a command via LuCI RPC sys.exec."""
        try:
            return await self._rpc_call("sys", "exec", [command]) or ""
        except LuciRpcError as err:
            _LOGGER.debug("Command failed via LuCI RPC sys.exec: %s (%s)", command, err)
            return ""

    async def user_exists(self, username: str) -> bool:
        """Check if a system user exists on the device."""
        # 1. Try via LuCI RPC (often more restricted than ubus, but let's try reading passwd)
        try:
            res = await self.execute_command(
                f"grep -q '^{username}:' /etc/passwd && echo 'exists'"
            )
            if res and isinstance(res, str) and "exists" in res:
                return True
        except Exception:
            pass

        # 2. Fallback to base method
        return await super().user_exists(username)

    async def provision_user(
        self, username: str, password: str
    ) -> tuple[bool, str | None]:
        """Create a dedicated system user and configure RPC permissions via LuCI RPC."""
        # Use the harmonized provisioning script from base
        script = PROVISION_SCRIPT_TEMPLATE.format(username=username, password=password)
        try:
            output = await self.execute_command(script)
            if output:
                _LOGGER.debug(
                    "Provisioning output for %s via LuCI RPC: %s", username, output
                )

            if "Provisioning SUCCESS" in output:
                return True, None

            if "LOG: FAIL:" in output:
                fail_msg = output.split("LOG: FAIL:")[1].splitlines()[0].strip()
                _LOGGER.error("Provisioning failed via LuCI RPC: %s", fail_msg)
                return False, fail_msg

            return (
                False,
                "Provisioning script returned failure without specific error via LuCI RPC. Check router logs (logread).",
            )
        except Exception as err:
            _LOGGER.error("Failed to provision user %s via LuCI RPC: %s", username, err)
            return False, str(err)

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

                # Check content type to ensure it's JSON
                content_type = response.headers.get("Content-Type", "").lower()
                if "application/json" not in content_type:
                    text = await response.text()
                    if "<html" in text.lower():
                        _LOGGER.debug(
                            "Received HTML instead of JSON from LuCI Auth: %s",
                            text[:200],
                        )
                        raise LuciRpcPackageMissingError(
                            "LuCI Auth returned HTML instead of JSON. Is 'luci-mod-rpc' installed?"
                        )
                    raise LuciRpcError(
                        f"Unexpected content type from LuCI Auth: {content_type}"
                    )

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

        # Populate model and hardware info from system.board if available
        try:
            board_out = await self.execute_command("ubus call system board")
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
                    "cat /tmp/sysinfo/model 2>/dev/null"
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
                "else cat /sys/class/net/*/address | grep -v '00:00:00:00:00:00' | head -n 1; fi"
            )
            if mac_out and isinstance(mac_out, str) and ":" in mac_out:
                info.mac_address = mac_out.strip().lower()
        except Exception:
            pass

        # If MAC is still missing, try a different approach (ifconfig/ip)
        if not info.mac_address:
            try:
                ip_addr_out = await self.execute_command(
                    "ip addr show br-lan || ip addr show lan || ip addr show eth0"
                )
                if "link/ether" in ip_addr_out:
                    mac = ip_addr_out.split("link/ether")[1].strip().split()[0]
                    info.mac_address = mac.lower()
            except Exception:
                pass

        return info

    async def get_gateway_mac(self) -> str | None:
        """Get the MAC address of the default gateway via LuCI RPC."""
        try:
            # 1. Get the default gateway IP
            route_out = await self.execute_command("ip -4 route show | grep default")
            if not route_out:
                return None

            # Example: default via 192.168.1.1 dev eth0 proto static
            parts = route_out.split()
            if "via" not in parts:
                return None

            gw_ip = parts[parts.index("via") + 1]

            # 2. Get the MAC from ARP/Neighbor table
            neigh_out = await self.execute_command(f"ip neigh show {gw_ip}")
            if not neigh_out:
                return None

            # Example: 192.168.1.1 dev eth0 lladdr 00:11:22:33:44:55 REACHABLE
            if "lladdr" in neigh_out:
                mac = neigh_out.split("lladdr")[1].strip().split()[0]
                return mac.lower()
        except Exception:
            pass
        return None

    async def get_lldp_neighbors(self) -> list[LldpNeighbor]:
        """Get LLDP neighbor information via LuCI RPC."""
        neighbors: list[LldpNeighbor] = []
        try:
            # Try ubus first (same as ubus client)
            out = await self.execute_command("ubus call lldp show 2>/dev/null")
            if out and out.strip().startswith("{"):
                data = json.loads(out)
                for neighbor_data in data.get("lldp", []):
                    for _interface_name, details in neighbor_data.items():
                        if not isinstance(details, dict):
                            continue
                        # details is a list of neighbors for this interface?
                        # Actually 'lldp show' structure varies, but let's try a common one
                        pass

            # Fallback to lldpcli -f json
            out = await self.execute_command(
                "lldpcli show neighbors -f json 2>/dev/null"
            )
            if out and out.strip().startswith("{"):
                data = json.loads(out)
                # Parse lldpcli json output (complex nested structure)
                # lldp -> neighbor -> [ { interface: { name: "...", neighbor: [...] } } ]
                lldp = data.get("lldp", {})
                for entry in lldp.get("interface", []):
                    local_iface = None
                    for iface_name, iface_data in entry.items():
                        local_iface = iface_name
                        for neighbor in iface_data.get("neighbor", []):
                            n = LldpNeighbor(local_interface=local_iface)
                            n.neighbor_name = neighbor.get("name", "")
                            n.neighbor_description = neighbor.get("descr", "")
                            n.neighbor_system_name = neighbor.get("sysname", "")

                            port = neighbor.get("port", [{}])[0]
                            n.neighbor_port = port.get("id", {}).get("value", "")

                            chassis = neighbor.get("chassis", [{}])[0]
                            n.neighbor_chassis = chassis.get("id", {}).get("value", "")

                            neighbors.append(n)
        except Exception:
            pass
        return neighbors

    async def check_permissions(self) -> OpenWrtPermissions:
        """Check what permissions the current user has."""
        from .base import OpenWrtPermissions

        perms = OpenWrtPermissions()

        async def can_read_uci(config: str) -> bool:
            try:
                await self._rpc_call("uci", "get_all", [config])
                return True
            except LuciRpcError as err:
                return "Access denied" not in str(err)

        async def can_write_uci(config: str) -> bool:
            try:
                # Calling set with missing args to test permission before validation
                await self._rpc_call("uci", "set", [config])
                return True
            except LuciRpcError as err:
                return "Access denied" not in str(err)

        perms.read_system = await can_read_uci("system")
        perms.write_system = await can_write_uci("system")
        perms.read_network = await can_read_uci("network")
        perms.write_network = await can_write_uci("network")
        perms.read_firewall = await can_read_uci("firewall")
        perms.write_firewall = await can_write_uci("firewall")
        perms.read_wireless = await can_read_uci("wireless")
        perms.write_wireless = await can_write_uci("wireless")
        perms.read_sqm = await can_read_uci("sqm")
        perms.write_sqm = await can_write_uci("sqm")
        perms.read_vpn = perms.read_network
        perms.read_mwan = await can_read_uci("mwan3")
        perms.read_led = perms.read_system
        perms.write_led = perms.write_system
        perms.read_devices = await can_read_uci("dhcp") or perms.read_network

        try:
            await self._rpc_call("sys", "exec", ["ls"])
            perms.read_services = True
            perms.write_services = True
            perms.write_devices = True
        except LuciRpcError as err:
            denied = "Access denied" in str(err)
            perms.read_services = not denied
            perms.write_services = not denied
            perms.write_devices = not denied

        perms.write_access_control = perms.write_firewall
        return perms

    async def check_packages(self) -> OpenWrtPackages:
        """Check installed packages with multiple fallbacks."""
        packages = OpenWrtPackages()
        try:
            # Step 1: Check via ubus call (if ubus is available via sys.exec)
            try:
                ubus_list = await self.execute_command("ubus list")
                objects = ubus_list.splitlines() if ubus_list else []

                if "sqm" in objects:
                    packages.sqm_scripts = True
                if "mwan3" in objects:
                    packages.mwan3 = True
                if "luci" in objects or "luci-rpc" in objects:
                    packages.luci_mod_rpc = True
            except Exception:
                pass

            # Step 2: Check via file existence (sys.exec)
            cmd = (
                "for f in /etc/init.d/sqm /etc/init.d/mwan3 /usr/bin/iwinfo "
                "/usr/bin/etherwake /usr/bin/wg /usr/sbin/openvpn "
                "/usr/lib/lua/luci/controller/rpc.lua "
                "/usr/share/luci/menu.d/luci-mod-rpc.json "
                "/usr/lib/lua/luci/controller/attendedsysupgrade.lua "
                "/usr/share/luci/menu.d/luci-app-attendedsysupgrade.json "
                "/etc/init.d/adblock "
                "/etc/init.d/simple-adblock "
                "/etc/init.d/ban-ip; do "
                "if [ -f $f ] || [ -x $f ]; then echo 1; else echo 0; fi; done"
            )
            out = await self._rpc_call("sys", "exec", [cmd])
            if out:
                results = out.strip().splitlines()

                def detect_status(idx: int) -> bool:
                    return len(results) > idx and results[idx].strip() == "1"

                if packages.sqm_scripts is not True:
                    packages.sqm_scripts = detect_status(0)
                if packages.mwan3 is not True:
                    packages.mwan3 = detect_status(1)
                packages.iwinfo = detect_status(2)
                packages.etherwake = detect_status(3)
                packages.wireguard = detect_status(4)
                packages.openvpn = detect_status(5)
                if packages.luci_mod_rpc is not True:
                    packages.luci_mod_rpc = detect_status(6) or (len(objects) > 0)
                packages.asu = detect_status(7) or detect_status(8)
                packages.adblock = detect_status(9)
                packages.simple_adblock = detect_status(10)
                packages.ban_ip = detect_status(11)

            # Step 3: Check UCI configs for remaining packages (very robust fallback)
            if packages.sqm_scripts is not True:
                try:
                    res = await self._rpc_call("uci", "get_all", ["sqm"])
                    if res and isinstance(res, dict):
                        packages.sqm_scripts = True
                except Exception:
                    pass

            if packages.mwan3 is not True:
                try:
                    res = await self._rpc_call("uci", "get_all", ["mwan3"])
                    if res and isinstance(res, dict):
                        packages.mwan3 = True
                except Exception:
                    pass

            if packages.openvpn is not True:
                try:
                    res = await self._rpc_call("uci", "get_all", ["openvpn"])
                    if res and isinstance(res, dict):
                        packages.openvpn = True
                except Exception:
                    pass

            if packages.wireguard is not True:
                try:
                    res = await self._rpc_call("uci", "get_all", ["network"])
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
                except Exception:
                    pass

            # Step 4: Fallback to get_installed_packages (full list check)
            installed = await self.get_installed_packages()
            if installed:
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
                for attr, pkg in mapping.items():
                    if getattr(packages, attr) is not True:
                        if pkg in ["wireguard", "openvpn"]:
                            setattr(
                                packages,
                                attr,
                                any(pkg in p for p in installed),
                            )
                        else:
                            setattr(packages, attr, pkg in installed)

            # Final pass: Initialize remaining to False (to avoid staying at None)
            import dataclasses

            for field in dataclasses.fields(packages):
                if getattr(packages, field.name) is None:
                    setattr(packages, field.name, False)

        except Exception as err:
            _LOGGER.error("Failed to check packages via LuCI RPC: %s", err)
            # Ensure no None values are returned
            import dataclasses

            for field in dataclasses.fields(packages):
                if getattr(packages, field.name) is None:
                    setattr(packages, field.name, False)

        return packages

    async def get_system_resources(self) -> SystemResources:
        """Get system resource usage."""
        resources = SystemResources()

        # Fetch basic system stats
        cmds = [
            "cat /proc/meminfo",
            "cat /proc/loadavg",
            "cat /proc/uptime",
            "cat /proc/stat",
            "df /overlay 2>/dev/null || df / 2>/dev/null",
            "ubus call system info 2>/dev/null",
            "ubus call luci getMountPoints 2>/dev/null",
        ]

        # Parallel execution via Luci RPC
        results = await asyncio.gather(
            *[self._rpc_call("sys", "exec", [cmd]) for cmd in cmds],
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
            resources.memory_used = (
                resources.memory_total
                - resources.memory_free
                - resources.memory_buffered
                - resources.memory_cached
            )
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
            try:
                resources.uptime = int(float(uptime_str.strip().split()[0]))
            except ValueError, IndexError:
                pass

        # 4. System Info (Memory fallback and CPU/Disk)
        sys_info = results[5]
        if isinstance(sys_info, str) and sys_info.strip().startswith("{"):
            try:
                data = json.loads(sys_info)
                # Fallback memory if proc/meminfo failed
                if resources.memory_total == 0:
                    mem = data.get("memory", {})
                    resources.memory_total = mem.get("total", 0)
                    resources.memory_used = mem.get("total", 0) - mem.get("free", 0)

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

                # Disk info
                if "disk" in data:
                    disk = data["disk"]
                    root = disk.get("root", disk.get("/", {}))
                    if isinstance(root, dict) and root.get("total"):
                        resources.filesystem_total = root.get("total", 0)
                        resources.filesystem_used = root.get("used", 0)
                        resources.filesystem_free = root.get("total", 0) - root.get(
                            "used", 0
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
                                    "free", 0
                                ) or mount.get("avail", 0)
                                resources.filesystem_used = (
                                    resources.filesystem_total
                                    - resources.filesystem_free
                                )
                                break
                except Exception:
                    pass

        # 6. Storage fallback via df
        if resources.filesystem_total == 0:
            df_out = results[4]
            if isinstance(df_out, str) and df_out:
                lines = df_out.strip().split("\n")
                if len(lines) >= 2:
                    parts = lines[1].split()
                    if len(parts) >= 4:
                        try:
                            resources.filesystem_total = int(parts[1]) * 1024
                            resources.filesystem_used = int(parts[2]) * 1024
                            resources.filesystem_free = int(parts[3]) * 1024
                        except ValueError, IndexError:
                            pass

        # 7. CPU usage fallback from /proc/stat
        if resources.cpu_usage == 0.0:
            proc_stat = results[3]
            if isinstance(proc_stat, str) and proc_stat:
                resources.cpu_usage = self._calculate_cpu_usage(proc_stat)

        # 8. Thermal
        try:
            for zone in range(3):
                temp_raw = await self._rpc_call(
                    "sys",
                    "exec",
                    [f"cat /sys/class/thermal/thermal_zone{zone}/temp 2>/dev/null"],
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

    async def get_external_ip(self) -> str | None:
        """Get public/external IP address."""
        try:
            status = await self._rpc_call(
                "sys", "exec", ["ubus call network.interface dump"]
            )
            if status:
                data = json.loads(status)
                if data and isinstance(data, dict):
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

        uci_to_sys, sys_to_uci = await self._get_wireless_mapping()

        try:
            wireless_config = await self._rpc_call("uci", "get_all", ["wireless"])
            if isinstance(wireless_config, dict):
                for section, values in wireless_config.items():
                    if isinstance(values, dict) and values.get(".type") == "wifi-iface":
                        # Fallback: Find system name by SSID if reverse mapping is missing
                        iface_name = uci_to_sys.get(section)
                        if not iface_name:
                            ssid = values.get("ssid", "")
                            if ssid:
                                try:
                                    match_out = await self._rpc_call(
                                        "sys",
                                        "exec",
                                        [
                                            f"iwinfo 2>/dev/null | grep -F '\"{ssid}\"' | awk '{{print $1}}'"
                                        ],
                                    )
                                    if match_out:
                                        iface_name = match_out.strip().splitlines()[0]
                                except Exception:
                                    pass

                        sys_name = iface_name or section
                        wifi = WirelessInterface(
                            name=section,
                            ifname=sys_name,
                            section=section,
                            ssid=values.get("ssid", ""),
                            mode=values.get("mode", ""),
                            encryption=values.get("encryption", ""),
                            enabled=values.get("disabled", "0") != "1",
                        )

                        # Fetch metrics via iwinfo
                        if iface_name:
                            try:
                                iw_info = await self._rpc_call(
                                    "sys",
                                    "exec",
                                    [f"iwinfo {iface_name} info 2>/dev/null"],
                                )
                                if iw_info:
                                    for line in iw_info.splitlines():
                                        if "Channel:" in line:
                                            try:
                                                wifi.channel = int(
                                                    line.split("Channel:")[1]
                                                    .strip()
                                                    .split()[0]
                                                )
                                            except ValueError, IndexError:
                                                pass
                                        elif "Access Point:" in line:
                                            try:
                                                wifi.mac_address = (
                                                    line.split("Access Point:")[1]
                                                    .strip()
                                                    .upper()
                                                )
                                            except IndexError:
                                                pass
                                        elif "Signal:" in line:
                                            try:
                                                wifi.signal = int(
                                                    line.split("Signal:")[1]
                                                    .strip()
                                                    .split()[0]
                                                )
                                            except ValueError, IndexError:
                                                pass
                                        elif "Noise:" in line:
                                            try:
                                                wifi.noise = int(
                                                    line.split("Noise:")[1]
                                                    .strip()
                                                    .split()[0]
                                                )
                                            except ValueError, IndexError:
                                                pass
                                        elif "Bit Rate:" in line:
                                            try:
                                                wifi.bitrate = float(
                                                    line.split("Bit Rate:")[1]
                                                    .strip()
                                                    .split()[0]
                                                )
                                            except ValueError, IndexError:
                                                pass
                                        elif "Frequency:" in line:
                                            try:
                                                wifi.frequency = line.split(
                                                    "Frequency:"
                                                )[1].strip()
                                            except IndexError:
                                                pass

                                    # Fallback: Extract frequency from Channel line if still missing
                                    if not wifi.frequency and "Channel:" in iw_info:
                                        for line in iw_info.splitlines():
                                            if (
                                                "Channel:" in line
                                                and "(" in line
                                                and "GHz)" in line
                                            ):
                                                try:
                                                    # Extract "2.462 GHz" from "Channel: 11 (2.462 GHz)"
                                                    wifi.frequency = (
                                                        line.split("(")[1]
                                                        .split(")")[0]
                                                        .strip()
                                                    )
                                                except IndexError, ValueError:
                                                    pass

                                    # Fallback 2: Infer from channel number
                                    if not wifi.frequency and wifi.channel > 0:
                                        if 1 <= wifi.channel <= 14:
                                            wifi.frequency = "2.4 GHz"
                                        elif 32 <= wifi.channel <= 177:
                                            wifi.frequency = "5 GHz"
                                        elif (
                                            1 <= wifi.channel <= 233
                                        ):  # 6GHz channels overlap but usually 1-233
                                            # This is a bit ambiguous without more info, but 5GHz/6GHz are higher
                                            # We already handled 1-14 as 2.4GHz.
                                            pass

                                assoc = await self._rpc_call(
                                    "sys",
                                    "exec",
                                    [f"iwinfo {iface_name} assoclist 2>/dev/null"],
                                )
                                if assoc and "No information" not in assoc:
                                    wifi.clients_count = len(
                                        [
                                            line
                                            for line in assoc.strip().splitlines()
                                            if line.strip() and ":" in line.split()[0]
                                        ]
                                    )

                                # Fallback: hostapd ubus call
                                if wifi.clients_count == 0:
                                    hostapd_out = await self._rpc_call(
                                        "sys",
                                        "exec",
                                        [
                                            f"ubus call hostapd.{iface_name} get_clients 2>/dev/null"
                                        ],
                                    )
                                    if hostapd_out and hostapd_out.strip().startswith(
                                        "{"
                                    ):
                                        try:
                                            h_data = json.loads(hostapd_out)
                                            if "clients" in h_data:
                                                wifi.clients_count = len(
                                                    h_data["clients"]
                                                )
                                        except Exception:
                                            pass
                            except Exception:
                                pass

                        interfaces.append(wifi)

                    # Store mapping for other calls
                    self._sys_to_uci = sys_to_uci
                    self._uci_to_sys = uci_to_sys

        except LuciRpcError:
            pass

        return interfaces

    async def get_network_interfaces(self) -> list[NetworkInterface]:
        """Get network interfaces."""
        interfaces: list[NetworkInterface] = []

        try:
            dump = await self._rpc_call(
                "sys", "exec", ["ubus call network.interface dump 2>/dev/null"]
            )
            if dump and dump.strip().startswith("{"):
                data = json.loads(dump)
                for iface_data in data.get("interface", []):
                    iface = NetworkInterface(
                        name=iface_data.get("interface", ""),
                        up=iface_data.get("up", False),
                        protocol=iface_data.get("proto", ""),
                        device=iface_data.get(
                            "l3_device", iface_data.get("device", "")
                        ),
                        uptime=iface_data.get("uptime", 0),
                    )
                    ipv4 = iface_data.get("ipv4-address", [])
                    if ipv4:
                        iface.ipv4_address = ipv4[0].get("address", "")
                    ipv6 = iface_data.get("ipv6-address", [])
                    if ipv6:
                        iface.ipv6_address = ipv6[0].get("address", "")
                    iface.dns_servers = iface_data.get("dns-server", [])
                    interfaces.append(iface)

            if interfaces:
                return interfaces
        except Exception:  # noqa: BLE001
            pass

        # Fallback to UCI config if ubus dump fails
        net_config = await self._rpc_call("uci", "get_all", ["network"])
        if isinstance(net_config, dict):
            for section, values in net_config.items():
                if isinstance(values, dict) and values.get(".type") == "interface":
                    iface = NetworkInterface(
                        name=section,
                        protocol=values.get("proto", ""),
                        device=str(values.get("device", values.get("ifname", ""))),
                    )
                    # Try to get MAC if possible
                    if iface.device:
                        try:
                            mac = await self.execute_command(
                                f"cat /sys/class/net/{iface.device}/address 2>/dev/null"
                            )
                            if mac and ":" in mac:
                                iface.mac_address = mac.strip().lower()
                        except Exception:
                            pass
                    interfaces.append(iface)

        return interfaces

    async def get_connected_devices(self) -> list[ConnectedDevice]:
        """Get connected devices by combining DHCP, ARP and wireless station info via sys.exec."""
        # Ensure mapping is available
        await self._get_wireless_mapping()
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
                            connected=False,  # DHCP alone is not proof of connectivity
                            is_wireless=False,
                            connection_type="wired",
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
                                    connected=False,  # Neighbors alone might be stale
                                    is_wireless=False,
                                    connection_type="wired",
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
                            if (
                                len(parts) >= 1
                                and parts[0].count(":") == 5
                                and len(parts[0]) == 17
                            ):
                                mac = parts[0].lower()
                                if mac in devices:
                                    dev = devices[mac]
                                else:
                                    dev = ConnectedDevice(mac=mac, connected=False)
                                    devices[mac] = dev

                                    dev.connected = True  # Wireless association

                                dev.is_wireless = True
                                if (
                                    not dev.connection_type
                                    or dev.connection_type == "wired"
                                ):
                                    if "5g" in iface.lower():
                                        dev.connection_type = "5GHz"
                                    elif "2g" in iface.lower():
                                        dev.connection_type = "2.4GHz"
                                    else:
                                        dev.connection_type = "wireless"
                                # Map system interface name to UCI section if possible
                                dev.interface = getattr(self, "_sys_to_uci", {}).get(
                                    iface, iface
                                )
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

        # 4. Fallback: Discovery of all hostapd objects
        cmd = "for obj in $(ubus list 'hostapd.*'); do echo \"$obj $(ubus call $obj get_clients)\"; done"
        stdout = await self._rpc_call("sys", "exec", [cmd])
        if stdout:
            for line in stdout.splitlines():
                if not line.strip():
                    continue
                parts = line.split(" ", 1)
                if len(parts) < 2:
                    continue
                obj_name, data_str = parts
                iface_name = obj_name.split(".", 1)[1] if "." in obj_name else obj_name
                try:
                    data = json.loads(data_str)
                    if data and isinstance(data, dict) and "clients" in data:
                        for mac, info in data["clients"].items():
                            mac = mac.lower()
                            if mac in devices:
                                dev = devices[mac]
                            else:
                                dev = ConnectedDevice(mac=mac, connected=False)
                                devices[mac] = dev

                                dev.connected = True  # Wireless association

                            dev.is_wireless = True
                            # Map system interface name to UCI section if possible
                            dev.interface = getattr(self, "_sys_to_uci", {}).get(
                                iface_name, iface_name
                            )
                            if not dev.signal:
                                dev.signal = info.get("signal", 0)

                            if "5g" in iface_name.lower():
                                dev.connection_type = "5GHz"
                            elif "2g" in iface_name.lower():
                                dev.connection_type = "2.4GHz"
                            elif not dev.connection_type:
                                dev.connection_type = "wireless"
                except json.JSONDecodeError, KeyError:
                    continue

        # 4. Final refinement from IP neighbors (for states)
        try:
            active_states = ("REACHABLE", "DELAY", "PROBE", "PERMANENT")
            neighbors = await self.get_ip_neighbors()
            for neigh in neighbors:
                mac = neigh.mac.lower()
                if mac in devices:
                    dev = devices[mac]
                    if neigh.state.upper() in active_states:
                        dev.connected = True
                    if not dev.neighbor_state:
                        dev.neighbor_state = neigh.state
                    if not dev.interface:
                        dev.interface = neigh.interface
                else:
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
        except Exception:
            pass

        return list(devices.values())

    async def _get_wireless_mapping(self) -> tuple[dict[str, str], dict[str, str]]:
        """Get mapping of UCI sections to system names and vice-versa."""
        uci_to_sys: dict[str, str] = {}
        try:
            # Discovery of wireless interfaces via ubus
            wireless_status = await self._rpc_call(
                "sys", "exec", ["ubus call network.wireless status 2>/dev/null"]
            )
            if wireless_status:
                try:
                    ws_data = json.loads(wireless_status)
                    for radio_data in ws_data.values():
                        if not isinstance(radio_data, dict):
                            continue
                        for iface in radio_data.get("interfaces", []):
                            if "section" in iface and "ifname" in iface:
                                uci_to_sys[iface["section"]] = iface["ifname"]
                except Exception:
                    pass

            # Fallback: Discovery of all hostapd objects via ubus
            if not uci_to_sys:
                try:
                    hostapd_list = await self._rpc_call(
                        "sys", "exec", ["ubus list 'hostapd.*'"]
                    )
                    if hostapd_list:
                        for obj in hostapd_list.splitlines():
                            if "." in obj:
                                iface = obj.split(".", 1)[1]
                                # Check if we can find this iface in wireless config via SSID
                                # We'll do this mapping in get_wireless_interfaces
                                pass
                except Exception:
                    pass
        except LuciRpcError:
            pass

        sys_to_uci = {v: k for k, v in uci_to_sys.items()}
        self._uci_to_sys = uci_to_sys
        self._sys_to_uci = sys_to_uci
        return uci_to_sys, sys_to_uci

    async def kick_device(self, mac_address: str, interface: str) -> bool:
        """Kick a device, mapping UCI section back to system name if needed."""
        sys_iface = getattr(self, "_uci_to_sys", {}).get(interface, interface)
        return await super().kick_device(mac_address, sys_iface)

    async def get_dhcp_leases(self) -> list[DhcpLease]:
        """Get DHCP leases via LuCI RPC."""
        if self.dhcp_software == "none":
            return []

        leases: list[DhcpLease] = []

        # Try odhcpd via ubus call over sys.exec if enabled
        if self.dhcp_software in ("auto", "odhcpd"):
            try:
                stdout = await self._rpc_call(
                    "sys", "exec", ["ubus call dhcp ipv4leases 2>/dev/null"]
                )
                if stdout and stdout.strip().startswith("{"):
                    data = json.loads(stdout)
                    if data and isinstance(data, dict):
                        for lease_data in data.get("dhcp_leases", []):
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
            except Exception:  # noqa: BLE001
                if self.dhcp_software == "odhcpd":
                    _LOGGER.debug(
                        "Requested odhcpd but 'ubus call dhcp' failed via LuCI RPC"
                    )
                    return []

        # Parse dnsmasq leases from /tmp/dhcp.leases
        if self.dhcp_software in ("auto", "dnsmasq"):
            try:
                leases_str = await self._rpc_call(
                    "sys", "exec", ["cat /tmp/dhcp.leases 2>/dev/null"]
                )
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
                if self.dhcp_software == "dnsmasq":
                    _LOGGER.debug(
                        "Requested dnsmasq but cat /tmp/dhcp.leases failed via LuCI RPC"
                    )

        return leases

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

    async def get_ip_neighbors(self) -> list[IpNeighbor]:
        """Get IP neighbor (ARP/NDP) table via sys.exec."""
        neighbors: list[IpNeighbor] = []
        try:
            output = await self._rpc_call("sys", "exec", ["ip neigh show"])
            if output:
                for line in output.strip().splitlines():
                    parts = line.split()
                    if len(parts) >= 4:
                        ip = parts[0]
                        mac = ""
                        interface = ""
                        state = parts[-1]

                        if "dev" in parts:
                            idx = parts.index("dev")
                            if idx + 1 < len(parts):
                                interface = parts[idx + 1]
                        if "lladdr" in parts:
                            idx = parts.index("lladdr")
                            if idx + 1 < len(parts):
                                mac = parts[idx + 1].lower()

                        if mac:
                            neighbors.append(
                                IpNeighbor(
                                    ip=ip,
                                    mac=mac,
                                    interface=interface,
                                    state=state,
                                )
                            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Failed to get IP neighbors via LuCI RPC: %s", err)
        return neighbors

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

    async def install_firmware(self, url: str, keep_settings: bool = True) -> None:
        """Install firmware from the given URL via LuCI RPC."""
        keep = "" if keep_settings else "-n"
        cmd = (
            f"wget -O /tmp/firmware.bin '{url}' && sysupgrade {keep} /tmp/firmware.bin"
        )
        try:
            _LOGGER.info("Initiating firmware installation via LuCI RPC from: %s", url)
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
                    "LuCI RPC connection lost during sysupgrade - device is rebooting"
                )
            else:
                _LOGGER.error("Failed to execute sysupgrade via LuCI RPC: %s", err)
                raise LuciRpcError(f"sysupgrade execution failed: {err}") from err

    async def download_file(self, remote_path: str, local_path: str) -> bool:
        """Download a file from the router via LuCI RPC file.read."""
        try:
            import base64

            # LuCI file.read returns base64 encoded data
            res = await self._rpc_call("file", "read", [remote_path])
            if res and isinstance(res, str):
                with open(local_path, "wb") as f:
                    f.write(base64.b64decode(res))
                return True
        except Exception as err:
            _LOGGER.error("Failed to download file via LuCI RPC: %s", err)
        return False

    async def get_installed_packages(self) -> list[str]:
        """Get a list of installed packages via apk or opkg."""
        try:
            # Try apk first (modern OpenWrt), fallback to opkg
            cmd = "apk info 2>/dev/null || opkg list-installed | cut -d' ' -f1"
            output = await self.execute_command(cmd)
            return [line.strip() for line in output.splitlines() if line.strip()]
        except LuciRpcError:
            _LOGGER.debug("Failed to list installed packages via LuCI RPC")
            return []

    async def get_firewall_rules(self) -> list[FirewallRule]:
        """Get general firewall rules via UCI over LuCI RPC."""
        rules: list[FirewallRule] = []
        try:
            output = await self.execute_command("uci show firewall")
            sections: dict[str, dict[str, str]] = {}
            for line in output.splitlines():
                if "=" not in line:
                    continue
                key, val = line.split("=", 1)
                parts = key.split(".")
                if len(parts) >= 2:
                    section = parts[1]
                    if section not in sections:
                        sections[section] = {}
                    if len(parts) >= 3:
                        sections[section][parts[2]] = val.strip("'")

            for section_id, data in sections.items():
                if data.get(".type") == "rule":
                    rules.append(
                        FirewallRule(
                            name=data.get("name", section_id),
                            enabled=data.get("enabled", "1") == "1",
                            section_id=section_id,
                            target=data.get("target", ""),
                            src=data.get("src", ""),
                            dest=data.get("dest", ""),
                        )
                    )
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Failed to get firewall rules via LuCI RPC: %s", err)
        return rules

    async def set_firewall_rule_enabled(self, section_id: str, enabled: bool) -> bool:
        """Enable or disable a firewall rule via UCI over LuCI RPC."""
        try:
            val = "1" if enabled else "0"
            cmd = f"uci set firewall.{section_id}.enabled='{val}' && uci commit firewall && /etc/init.d/firewall reload"
            await self.execute_command(cmd)
            return True
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Failed to set firewall rule via LuCI RPC: %s", err)
            return False

    async def get_firewall_redirects(self) -> list[FirewallRedirect]:
        """Get firewall port forwarding redirects via UCI over LuCI RPC."""
        redirects: list[FirewallRedirect] = []
        try:
            output = await self.execute_command("uci show firewall")
            sections: dict[str, dict[str, str]] = {}
            for line in output.splitlines():
                if "=" not in line:
                    continue
                key, val = line.split("=", 1)
                parts = key.split(".")
                if len(parts) >= 2:
                    section = parts[1]
                    if section not in sections:
                        sections[section] = {}
                    if len(parts) >= 3:
                        sections[section][parts[2]] = val.strip("'")

            for section_id, data in sections.items():
                if data.get(".type") == "redirect":
                    redirects.append(
                        FirewallRedirect(
                            name=data.get("name", section_id),
                            target_ip=data.get("dest_ip", ""),
                            target_port=data.get("dest_port", ""),
                            external_port=data.get("src_dport", ""),
                            protocol=data.get("proto", "tcp"),
                            enabled=data.get("enabled", "1") == "1",
                            section_id=section_id,
                        )
                    )
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Failed to get firewall redirects via LuCI RPC: %s", err)
        return redirects

    async def set_firewall_redirect_enabled(
        self, section_id: str, enabled: bool
    ) -> bool:
        """Enable or disable a firewall redirect via UCI over LuCI RPC."""
        try:
            val = "1" if enabled else "0"
            cmd = f"uci set firewall.{section_id}.enabled='{val}' && uci commit firewall && /etc/init.d/firewall reload"
            await self.execute_command(cmd)
            return True
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Failed to set firewall redirect via LuCI RPC: %s", err)
            return False

    async def get_sqm_status(self) -> list[SqmStatus]:
        """Get SQM status via LuCI RPC."""
        from .base import SqmStatus

        sqm_instances: list[SqmStatus] = []
        try:
            resp = await self._rpc_call("uci", "get_all", ["sqm"])
            # Fallback to shell if permission denied or failed
            if not resp or (
                isinstance(resp, list)
                and len(resp) > 1
                and resp[1] == "Permission denied"
            ):
                try:
                    shell_out = await self.execute_command("uci show sqm 2>/dev/null")
                    if shell_out:
                        # Parse uci output
                        sections: dict[str, dict[str, Any]] = {}
                        for line in shell_out.strip().split("\n"):
                            if "=" not in line:
                                continue
                            key, val = line.split("=", 1)
                            parts = key.split(".")
                            if len(parts) >= 2:
                                section = parts[1]
                                if section not in sections:
                                    sections[section] = {}
                                if len(parts) == 2:
                                    sections[section][".type"] = val.strip("'")
                                elif len(parts) == 3:
                                    sections[section][parts[2]] = val.strip("'")
                        values_dict = sections
                    else:
                        values_dict = {}
                except Exception:
                    values_dict = {}
            else:
                values_dict = resp.get("values", resp) if isinstance(resp, dict) else {}

            if not isinstance(values_dict, dict):
                return sqm_instances

            for section_id, values in values_dict.items():
                if isinstance(values, dict) and values.get(".type") == "queue":
                    sqm_instances.append(
                        SqmStatus(
                            section_id=section_id,
                            name=values.get("name", section_id),
                            enabled=values.get("enabled") == "1",
                            interface=values.get("interface", ""),
                            download=int(values.get("download", "0")),
                            upload=int(values.get("upload", "0")),
                            qdisc=values.get("qdisc", ""),
                            script=values.get("script", ""),
                        )
                    )
        except Exception:
            pass
        return sqm_instances

    async def set_sqm_config(self, section_id: str, **kwargs: Any) -> bool:
        """Set SQM configuration via LuCI RPC."""
        try:
            for key, value in kwargs.items():
                val_str = (
                    "1" if value is True else "0" if value is False else str(value)
                )
                await self._rpc_call("uci", "set", ["sqm", section_id, key, val_str])
            await self._rpc_call("uci", "commit", ["sqm"])
            await self._rpc_call("sys", "exec", ["/etc/init.d/sqm reload"])
            return True
        except Exception as err:
            _LOGGER.error("Failed to set SQM config via LuCI RPC: %s", err)
            return False
