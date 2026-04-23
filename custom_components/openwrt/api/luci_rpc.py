"""OpenWrt LuCI RPC API client.

Communicates with OpenWrt via the LuCI web interface JSON-RPC API.
This is a fallback method when ubus HTTP is not available but the
LuCI web interface is installed.

Supports authentication via LuCI sysauth token.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
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
            host,
            username,
            password,
            port,
            use_ssl,
            verify_ssl,
            dhcp_software,
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
                ssl=self.verify_ssl if self.use_ssl else False,
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
                            endpoint,
                            method,
                            params,
                            reauthenticated=True,
                        )
                    msg = f"Access denied to LuCI RPC on {self.host}"
                    raise LuciRpcError(msg)

                if response.status == 404:
                    msg = f"LuCI RPC endpoint not found on {self.host}. Is 'luci-mod-rpc' installed?"
                    raise LuciRpcPackageMissingError(
                        msg,
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
                        msg = "LuCI RPC returned HTML instead of JSON. Is 'luci-mod-rpc' installed?"
                        raise LuciRpcPackageMissingError(
                            msg,
                        )
                    msg = f"Unexpected content type from LuCI RPC: {content_type}"
                    raise LuciRpcError(
                        msg,
                    )

                data = await response.json()
        except TimeoutError as err:
            msg = f"Timeout communicating with LuCI on {self.host}"
            raise LuciRpcTimeoutError(
                msg,
            ) from err
        except aiohttp.ClientConnectorError as err:
            msg = f"Cannot connect to LuCI on {self.host}: {err}"
            raise LuciRpcConnectionError(
                msg,
            ) from err
        except aiohttp.ClientSSLError as err:
            msg = f"SSL error connecting to LuCI on {self.host}: {err}"
            raise LuciRpcSslError(
                msg,
            ) from err
        except aiohttp.ClientError as err:
            if not reauthenticated:
                _LOGGER.debug(
                    "LuCI RPC connection error (%s), retrying after session reset",
                    err,
                )
                await self.disconnect()
                return await self._rpc_call(
                    endpoint,
                    method,
                    params,
                    reauthenticated=True,
                )
            self._connected = False
            msg = f"Communication error: {err}"
            raise LuciRpcError(msg) from err

        return data.get("result")

    async def execute_command(self, command: str) -> str:
        """Execute a command via LuCI RPC sys.exec."""
        try:
            # Wrap in /bin/sh -c to handle operators like && or >
            # We escape single quotes correctly for the shell.
            escaped_cmd = command.replace("'", "'\\''")
            return (
                await self._rpc_call(
                    "sys", "exec", [f"/bin/sh -c '{escaped_cmd}' 2>&1"]
                )
                or ""
            )
        except LuciRpcError as err:
            _LOGGER.debug("Command failed via LuCI RPC sys.exec: %s", err)
            return ""

    async def user_exists(self, username: str) -> bool:
        """Check if a system user exists on the device."""
        # 1. Try via LuCI RPC (often more restricted than ubus, but let's try reading passwd)
        try:
            res = await self.execute_command(
                f"grep -q '^{username}:' /etc/passwd && echo 'exists'",
            )
            if res and isinstance(res, str) and "exists" in res:
                return True
        except Exception:
            pass

        # 2. Fallback to base method
        return await super().user_exists(username)

    async def provision_user(
        self,
        username: str,
        password: str,
    ) -> tuple[bool, str | None]:
        """Create a dedicated system user and configure RPC permissions via LuCI RPC."""
        # Use the harmonized provisioning script from base
        script = PROVISION_SCRIPT_TEMPLATE.format(username=username, password=password)
        try:
            output = await self.execute_command(script)

            if output is None:
                output = ""

            if output:
                _LOGGER.debug(
                    "Provisioning output for %s via LuCI RPC: %s",
                    username,
                    output,
                )

            if "Provisioning SUCCESS" in output:
                return True, None

            if "LOG: FAIL:" in output:
                fail_msg = output.split("LOG: FAIL:")[1].splitlines()[0].strip()
                _LOGGER.error("Provisioning failed via LuCI RPC: %s", fail_msg)
                return False, fail_msg

            # Empty output usually means permission denied (sys.exec)
            if not output:
                _LOGGER.warning(
                    "Provisioning for %s returned empty output. "
                    "This typically means the current user ('%s') lacks "
                    "'sys.exec' RPC permission. "
                    "Provisioning must be run as 'root'.",
                    username,
                    self.username,
                )
                return (
                    False,
                    (
                        f"Provisioning failed: empty response from LuCI sys.exec. "
                        f"Ensure '{self.username}' has sys.exec permission, "
                        "or run provisioning as 'root'."
                    ),
                )

            return (
                False,
                "Provisioning script returned failure without specific error via LuCI RPC. Check router logs (logread).",
            )
        except LuciRpcError as err:
            msg = (
                f"Provisioning failed: '{self.username}' lacks 'sys.exec' RPC permission. "
                "Switch to 'root' or grant exec rights to this user."
            )
            _LOGGER.error("%s (%s)", msg, err)
            return False, msg
        except Exception as err:
            _LOGGER.exception(
                "Failed to provision user %s via LuCI RPC: %s", username, err
            )
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
                    msg = (
                        "LuCI RPC auth endpoint not found. Is 'luci-mod-rpc' installed?"
                    )
                    raise LuciRpcPackageMissingError(
                        msg,
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
                        msg = "LuCI Auth returned HTML instead of JSON. Is 'luci-mod-rpc' installed?"
                        raise LuciRpcPackageMissingError(
                            msg,
                        )
                    msg = f"Unexpected content type from LuCI Auth: {content_type}"
                    raise LuciRpcError(
                        msg,
                    )

                data = await response.json()
        except TimeoutError as err:
            msg = f"Login timeout for LuCI on {self.host}"
            raise LuciRpcTimeoutError(msg) from err
        except aiohttp.ClientConnectorError as err:
            msg = f"Cannot connect to LuCI: {err}"
            raise LuciRpcConnectionError(msg) from err
        except aiohttp.ClientSSLError as err:
            msg = f"SSL error connecting to LuCI: {err}"
            raise LuciRpcSslError(msg) from err
        except aiohttp.ClientError as err:
            msg = f"Cannot connect: {err}"
            raise LuciRpcError(msg) from err

        result = data.get("result")
        if (
            result is None
            or result == "null"
            or (isinstance(result, str) and not result)
        ):
            _LOGGER.error("LuCI RPC auth returned no token: %s", data)
            msg = f"Authentication failed for {self.username}@{self.host}. Check credentials."
            raise LuciRpcAuthError(
                msg,
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
                    "cat /tmp/sysinfo/model 2>/dev/null",
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

    async def get_lldp_neighbors(self) -> list[LldpNeighbor]:
        """Get LLDP neighbor information via LuCI RPC."""
        neighbors: list[LldpNeighbor] = []
        try:
            # Try ubus first (same as ubus client)
            out = await self.execute_command("ubus call lldp show 2>/dev/null")
            if out and out.strip().startswith("{"):
                data = json.loads(out)
                for neighbor_data in data.get("lldp", []):
                    for details in neighbor_data.values():
                        if not isinstance(details, dict):
                            continue
                        # details is a list of neighbors for this interface?
                        # Actually 'lldp show' structure varies, but let's try a common one

            # Fallback to lldpcli -f json
            out = await self.execute_command(
                "lldpcli show neighbors -f json 2>/dev/null",
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
        perms.write_vpn = perms.write_network
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
                if "upnp" in objects:
                    packages.miniupnpd = True
                if "nlbwmon" in objects:
                    packages.nlbwmon = True
                if "pbr" in objects:
                    packages.pbr = True
                if "led" in objects:
                    packages.rpcd_mod_led = True
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
                "/etc/init.d/ban-ip "
                "/etc/init.d/ban-ip "
                "/etc/init.d/miniupnpd "
                "/etc/init.d/nlbwmon "
                "/etc/init.d/pbr "
                "/etc/init.d/adguardhome "
                "/etc/init.d/unbound "
                "/usr/lib/rpcd/led.so; do "
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
                    packages.luci_mod_rpc = (
                        detect_status(6) or detect_status(7) or (len(objects) > 0)
                    )
                packages.asu = detect_status(8) or detect_status(9)
                packages.adblock = detect_status(10)
                packages.simple_adblock = detect_status(11)
                packages.ban_ip = detect_status(12)
                packages.rpcd_mod_led = detect_status(18)

                packages.miniupnpd = detect_status(13)
                packages.nlbwmon = detect_status(14)
                packages.pbr = detect_status(15)
                packages.adguardhome = detect_status(16)
                packages.unbound = detect_status(17)

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
                        elif attr == "asu":
                            setattr(
                                packages,
                                attr,
                                any(
                                    p in installed
                                    for p in [
                                        "luci-app-attendedsysupgrade",
                                        "attendedsysupgrade-common",
                                    ]
                                ),
                            )
                        else:
                            setattr(packages, attr, pkg in installed)

            # Final pass: Initialize remaining to False (to avoid staying at None)
            import dataclasses

            for field in dataclasses.fields(packages):
                if getattr(packages, field.name) is None:
                    setattr(packages, field.name, False)

        except Exception as err:
            _LOGGER.exception("Failed to check packages via LuCI RPC: %s", err)
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

    async def get_external_ip(self) -> str | None:
        """Get public/external IP address."""
        try:
            status = await self.execute_command("ubus call network.interface dump")
            if status:
                data = json.loads(status)
                if data and isinstance(data, dict):
                    for iface_data in data.get("interface", []):
                        iface_name = iface_data.get("interface", "").lower()
                        if iface_name in ["wan", "wan6", "wwan", "modem"]:
                            ipv4_addrs = iface_data.get("ipv4-address", [])
                            if ipv4_addrs:
                                return ipv4_addrs[0].get("address")
        except (
            LuciRpcError,
            json.JSONDecodeError,
        ):
            pass
        return None

    async def get_wireless_interfaces(self) -> list[WirelessInterface]:
        """Get wireless interfaces via ubus iwinfo and UCI."""
        interfaces: list[WirelessInterface] = []
        iface_names: set[str] = set()

        # 1. Primary source: network.wireless status (UCI state)
        try:
            wireless_data = await self.execute_command(
                "ubus call network.wireless status"
            )
            if wireless_data and wireless_data.strip().startswith("{"):
                data = json.loads(wireless_data)
                for radio_name, radio_data in data.items():
                    if not isinstance(radio_data, dict):
                        continue
                    for iface in radio_data.get("interfaces", []):
                        iface_name = (
                            iface.get("section")
                            or iface.get("ifname")
                            or iface.get("device", "")
                        )
                        if not iface_name or iface_name in iface_names:
                            continue

                        iface_config = iface.get("config", {})
                        # Prefer system ifname (e.g. phy1-ap0) over section name (e.g. default_radio1)
                        # because iwinfo info only works with system ifnames.
                        system_ifname = iface.get("ifname")

                        wifi = WirelessInterface(
                            name=system_ifname or iface_name,
                            ssid=iface_config.get("ssid", ""),
                            mode=iface_config.get("mode", ""),
                            encryption=iface_config.get("encryption", ""),
                            enabled=not radio_data.get("disabled", False),
                            up=radio_data.get("up", False),
                            radio=radio_name,
                            hwmode=radio_data.get("config", {}).get("hwmode", ""),
                        )
                        interfaces.append(wifi)
                        iface_names.add(wifi.name)
        except Exception as err:
            _LOGGER.debug(
                "network.wireless status failed via LuCI, trying UCI: %s", err
            )
            try:
                uci_wireless_str = await self.execute_command("uci export wireless")
                if uci_wireless_str:
                    sections: dict[str, dict[str, str]] = {}
                    current_section = ""
                    for line in uci_wireless_str.splitlines():
                        line = line.strip()
                        if line.startswith("config"):
                            parts = line.split()
                            if len(parts) >= 3:
                                current_section = parts[2].strip("'\"")
                                sections[current_section] = {".type": parts[1]}
                        elif line.startswith("option") and current_section:
                            parts = line.split(None, 2)
                            if len(parts) >= 3:
                                sections[current_section][parts[1]] = parts[2].strip(
                                    "'\""
                                )

                    for sect_name, sect_data in sections.items():
                        if sect_data.get(".type") != "wifi-iface":
                            continue

                        iface_name = sect_name
                        radio_name = sect_data.get("device", "")
                        radio_disabled = (
                            sections.get(radio_name, {}).get("disabled", "0") == "1"
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
                            hwmode=sections.get(radio_name, {}).get("hwmode", ""),
                        )
                        interfaces.append(wifi)
                        iface_names.add(iface_name)
            except Exception as e:
                _LOGGER.debug("UCI wireless fallback failed via LuCI: %s", e)

        # 2. Supplement/Fallback: iwinfo devices
        try:
            iw_devs_str = await self.execute_command("ubus call iwinfo devices")
            if iw_devs_str and iw_devs_str.strip().startswith("{"):
                iw_devs = json.loads(iw_devs_str).get("devices", [])
                for name in iw_devs:
                    if name not in iface_names:
                        wifi = WirelessInterface(name=name, enabled=True, up=True)
                        interfaces.append(wifi)
                        iface_names.add(name)
        except Exception:
            pass

        # 3. Populate metrics via ubus iwinfo info
        for wifi in interfaces:
            iface_name = wifi.name
            try:
                # Get basic info
                info_str = await self.execute_command(
                    f'ubus call iwinfo info \'{{"device":"{iface_name}"}}\''
                )
                if info_str and info_str.strip().startswith("{"):
                    info = json.loads(info_str)
                    if not wifi.ssid:
                        wifi.ssid = info.get("ssid", "")
                    wifi.mac_address = info.get("bssid", "").upper()
                    wifi.channel = info.get("channel", 0)
                    wifi.frequency = str(info.get("frequency", ""))
                    wifi.signal = info.get("signal", 0)
                    wifi.noise = info.get("noise", 0)
                    wifi.bitrate = (
                        (info.get("bitrate", 0) / 1000.0)
                        if info.get("bitrate")
                        else 0.0
                    )

                    # Quality
                    q_val = info.get("quality")
                    q_max = info.get("quality_max", 100)
                    if q_val is not None and q_max:
                        wifi.quality = round((q_val / q_max) * 100, 1)

                    # Association list for client count
                    assoc_str = await self.execute_command(
                        f'ubus call iwinfo assoclist \'{{"device":"{iface_name}"}}\''
                    )
                    if assoc_str and assoc_str.strip().startswith("{"):
                        assoc = json.loads(assoc_str).get("results", [])
                        wifi.clients_count = len(assoc)
            except Exception as err:
                _LOGGER.debug("Failed to get iwinfo for %s: %s", iface_name, err)

        # 4. Deduplicate and clean up
        # We group by Section ID (UCI), MAC address, or SSID+Frequency
        unique_ifaces: list[WirelessInterface] = []
        seen_keys: set[str] = set()

        for wifi in interfaces:
            # Skip interfaces that are clearly not operational or redundant placeholders
            # (No MAC and no SSID usually means a disabled/misconfigured UCI section)
            if (
                not wifi.mac_address
                and not wifi.ssid
                and wifi.mode.lower() in ("", "ap", "master")
            ):
                _LOGGER.debug(
                    "Skipping non-operational wireless interface: %s", wifi.name
                )
                continue

            # Create a key for deduplication
            # Priority 1: MAC address (BSSID)
            # Priority 2: SSID + Radio (for merging UCI sections with physical interfaces)
            # Priority 3: Section ID (UCI)
            # Priority 4: SSID + Frequency
            if wifi.mac_address:
                key = f"mac_{wifi.mac_address}"
            elif wifi.ssid and wifi.radio:
                key = f"ssid_radio_{wifi.ssid}_{wifi.radio}"
            elif wifi.section:
                key = f"section_{wifi.section}"
            elif wifi.ssid and wifi.frequency:
                key = f"ssid_freq_{wifi.ssid}_{wifi.frequency}"
            else:
                key = f"name_{wifi.name}"

            if key not in seen_keys:
                unique_ifaces.append(wifi)
                seen_keys.add(key)
            else:
                # Merge data if this one has more info
                existing = next(
                    i
                    for i in unique_ifaces
                    if (
                        (wifi.mac_address and i.mac_address == wifi.mac_address)
                        or (wifi.section and i.section == wifi.section)
                        or (
                            wifi.ssid
                            and wifi.frequency
                            and i.ssid == wifi.ssid
                            and i.frequency == wifi.frequency
                        )
                    )
                )
                # Prefer system name over UCI section name for existing.name
                if len(wifi.name) > len(existing.name) and not existing.mac_address:
                    existing.name = wifi.name

                if not existing.frequency and wifi.frequency:
                    existing.frequency = wifi.frequency
                if not existing.mac_address and wifi.mac_address:
                    existing.mac_address = wifi.mac_address
                if not existing.ssid and wifi.ssid:
                    existing.ssid = wifi.ssid
                if wifi.clients_count > existing.clients_count:
                    existing.clients_count = wifi.clients_count
                if wifi.ifname and not existing.ifname:
                    existing.ifname = wifi.ifname

        return unique_ifaces

    async def get_upnp_mappings(self) -> list[UpnpMapping]:
        """Get active UPnP/NAT-PMP port mappings via LuCI RPC."""
        mappings: list[UpnpMapping] = []
        try:
            stdout = await self.execute_command(
                "ubus call upnp get_mappings 2>/dev/null"
            )
            if not stdout or not stdout.strip().startswith("{"):
                return mappings

            res = json.loads(stdout)
            if "mappings" not in res:
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
        except Exception as err:
            _LOGGER.debug("Failed to fetch UPnP mappings via LuCI RPC: %s", err)

        return mappings

    async def get_wireguard_interfaces(self) -> list[WireGuardInterface]:
        """Get WireGuard VPN interface and peer information via LuCI RPC."""
        interfaces: list[WireGuardInterface] = []
        try:
            # 1. Discover WG interfaces via ubus call
            status_str = await self.execute_command(
                "ubus call network.interface dump 2>/dev/null"
            )
            if not status_str or not status_str.strip().startswith("{"):
                return interfaces

            status = json.loads(status_str)
            wg_ifaces = []
            for iface_data in status.get("interface", []):
                if iface_data.get("proto") == "wireguard":
                    wg_ifaces.append(iface_data.get("interface"))

            if not wg_ifaces:
                return interfaces

            # 2. Fetch peer info via wg show all dump
            stdout = await self.execute_command("wg show all dump 2>/dev/null")
            if not stdout:
                return interfaces

            iface_map: dict[str, WireGuardInterface] = {}
            for line in stdout.splitlines():
                parts = line.split("\t")
                if len(parts) == 4:
                    ifname = parts[0]
                    if ifname not in wg_ifaces:
                        continue
                    iface = WireGuardInterface(
                        name=ifname,
                        public_key=parts[1],
                        listen_port=int(parts[2]) if parts[2].isdigit() else 0,
                        fwmark=int(parts[3]) if parts[3].isdigit() else 0,
                    )
                    iface_map[ifname] = iface
                    interfaces.append(iface)
                elif len(parts) >= 8:
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
            _LOGGER.debug("Failed to fetch WireGuard interfaces via LuCI RPC: %s", err)

        return interfaces

    async def get_network_interfaces(self) -> list[NetworkInterface]:
        """Get network interfaces."""
        interfaces: list[NetworkInterface] = []

        try:
            dump = await self.execute_command(
                "ubus call network.interface dump 2>/dev/null"
            )
            if dump and dump.strip().startswith("{"):
                data = json.loads(dump)
                for iface_data in data.get("interface", []):
                    iface = NetworkInterface(
                        name=iface_data.get("interface", ""),
                        up=iface_data.get("up", False),
                        protocol=iface_data.get("proto", ""),
                        device=iface_data.get(
                            "l3_device",
                            iface_data.get("device", ""),
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
                                f"cat /sys/class/net/{iface.device}/address 2>/dev/null",
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
            leases_str = await self.execute_command("cat /tmp/dhcp.leases 2>/dev/null")
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
            arp = await self.execute_command("cat /proc/net/arp 2>/dev/null")
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
            iw_out = await self.execute_command(
                "iwinfo 2>/dev/null | grep -E '^[a-z0-9_-]+' | awk '{print $1}'"
            )
            if iw_out:
                ifaces = iw_out.strip().split()
                for iface in ifaces:
                    assoc = await self.execute_command(
                        f"iwinfo {iface} assoclist 2>/dev/null"
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
                                    iface,
                                    iface,
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
        stdout = await self.execute_command(cmd)
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
                                iface_name,
                                iface_name,
                            )
                            if not dev.signal:
                                dev.signal = info.get("signal", 0)

                            if "5g" in iface_name.lower():
                                dev.connection_type = "5GHz"
                            elif "2g" in iface_name.lower():
                                dev.connection_type = "2.4GHz"
                            elif not dev.connection_type:
                                dev.connection_type = "wireless"
                except (
                    json.JSONDecodeError,
                    KeyError,
                ):
                    continue

        # 5. Final refinement from IP neighbors (for states)
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

        # 5. Supplemental source: Bridge FDB (Forwarding Database)
        await self._process_bridge_fdb(devices)

        return list(devices.values())

    async def _process_bridge_fdb(self, devices: dict[str, ConnectedDevice]) -> None:
        """Fetch and merge bridge FDB (forwarding database) information via LuCI RPC."""
        try:
            # 1. Fetch all network devices
            dev_status_str = await self.execute_command(
                "ubus call network.device status 2>/dev/null"
            )
            if not dev_status_str or not dev_status_str.strip().startswith("{"):
                return

            device_status = json.loads(dev_status_str)

            # 2. For each active device, fetch its FDB
            for dev_name, dev_info in device_status.items():
                if not dev_info.get("up"):
                    continue

                try:
                    fdb_str = await self.execute_command(
                        f'ubus call network.device fdb \'{{"name":"{dev_name}"}}\' 2>/dev/null'
                    )
                    if fdb_str and fdb_str.strip().startswith("["):
                        fdb = json.loads(fdb_str)
                        for entry in fdb:
                            mac = entry.get("mac", "").lower()
                            if mac not in devices:
                                continue

                            dev = devices[mac]
                            port = entry.get("port", "")
                            if port:
                                dev.port = port
                                if not dev.is_wireless and not dev.interface:
                                    dev.interface = dev_name
                except Exception:
                    continue
        except Exception as err:
            _LOGGER.debug("Failed to fetch bridge FDB via LuCI RPC: %s", err)

    async def _get_wireless_mapping(self) -> tuple[dict[str, str], dict[str, str]]:
        """Get mapping of UCI sections to system names and vice-versa."""
        uci_to_sys: dict[str, str] = {}
        try:
            # Discovery of wireless interfaces via ubus
            wireless_status = await self._rpc_call(
                "sys",
                "exec",
                ["ubus call network.wireless status 2>/dev/null"],
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
                        "sys",
                        "exec",
                        ["ubus list 'hostapd.*'"],
                    )
                    if hostapd_list:
                        for obj in hostapd_list.splitlines():
                            if "." in obj:
                                iface = obj.split(".", 1)[1]
                                # Check if we can find this iface in wireless config via SSID
                                # We'll do this mapping in get_wireless_interfaces
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
                    "sys",
                    "exec",
                    ["ubus call dhcp ipv4leases 2>/dev/null"],
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
                                ),
                            )
                    if leases and self.dhcp_software == "odhcpd":
                        return leases
            except Exception:  # noqa: BLE001
                if self.dhcp_software == "odhcpd":
                    _LOGGER.debug(
                        "Requested odhcpd but 'ubus call dhcp' failed via LuCI RPC",
                    )
                    return []

        # Parse dnsmasq leases from /tmp/dhcp.leases
        if self.dhcp_software in ("auto", "dnsmasq"):
            try:
                leases_str = await self._rpc_call(
                    "sys",
                    "exec",
                    ["cat /tmp/dhcp.leases 2>/dev/null"],
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
                                ),
                            )
            except LuciRpcError:
                if self.dhcp_software == "dnsmasq":
                    _LOGGER.debug(
                        "Requested dnsmasq but cat /tmp/dhcp.leases failed via LuCI RPC",
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
                            ),
                        )
        except LuciRpcError:
            _LOGGER.debug("Cannot list LEDs via LuCI RPC")

        return leds

    async def get_local_macs(self) -> set[str]:
        """Get all MAC addresses belonging to the router's physical and virtual interfaces."""
        macs = set()
        try:
            status_str = await self._rpc_call(
                "sys",
                "exec",
                ["ubus call network.device status 2>/dev/null"],
            )
            if status_str and status_str.strip().startswith("{"):
                status = json.loads(status_str)
                if status and isinstance(status, dict):
                    for dev_info in status.values():
                        if isinstance(dev_info, dict) and (
                            mac := dev_info.get("macaddr")
                        ):
                            macs.add(mac.lower())
        except Exception:  # noqa: BLE001
            pass
        return macs

    async def get_local_ips(self) -> set[str]:
        """Get all IP addresses belonging to the router."""
        ips = set()
        try:
            dump_str = await self._rpc_call(
                "sys",
                "exec",
                ["ubus call network.interface dump 2>/dev/null"],
            )
            if dump_str and dump_str.strip().startswith("{"):
                dump = json.loads(dump_str)
                if (
                    dump
                    and isinstance(dump, dict)
                    and (ifaces := dump.get("interface"))
                ):
                    for iface in ifaces:
                        if not isinstance(iface, dict):
                            continue
                        # IPv4
                        for addr in iface.get("ipv4-address", []):
                            if (
                                isinstance(addr, dict)
                                and (address := addr.get("address"))
                                and address not in ips
                            ):
                                ips.add(address)
                        # IPv6
                        for addr in iface.get("ipv6-address", []):
                            if (
                                isinstance(addr, dict)
                                and (address := addr.get("address"))
                                and address not in ips
                            ):
                                ips.add(address)
        except Exception:  # noqa: BLE001
            pass
        return ips

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
        cmd = f"wget --no-check-certificate -O /tmp/firmware.bin '{url}' && sysupgrade {keep} /tmp/firmware.bin"
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
                    "LuCI RPC connection lost during sysupgrade - device is rebooting",
                )
            else:
                _LOGGER.exception("Failed to execute sysupgrade via LuCI RPC: %s", err)
                msg = f"sysupgrade execution failed: {err}"
                raise LuciRpcError(msg) from err

    async def download_file(self, remote_path: str, local_path: str) -> bool:
        """Download a file from the router via LuCI RPC file.read."""
        try:
            import base64

            # Try LuCI file.read first
            try:
                res = await self._rpc_call("file", "read", [remote_path])
                if res and isinstance(res, str):
                    with open(local_path, "wb") as f:
                        f.write(base64.b64decode(res))
                    return True
            except LuciRpcPackageMissingError:
                # Fallback to sys.exec if file read is not available
                # Fabian's AX3600 has openssl but no standalone base64 command
                cmd = f"openssl base64 -in {remote_path} || base64 {remote_path} || cat {remote_path} | base64"
                output = await self.execute_command(cmd)
                if output:
                    with open(local_path, "wb") as f:
                        f.write(
                            base64.b64decode(output.replace("\n", "").replace("\r", ""))
                        )
                    return True
        except Exception as err:
            _LOGGER.exception("Failed to download file via LuCI RPC: %s", err)
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
                "  apk info -q 2>/dev/null; "
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

    async def get_firewall_rules(self) -> list[FirewallRule]:
        """Get firewall rules via LuCI RPC UCI."""
        rules: list[FirewallRule] = []
        try:
            data = await self._rpc_call("uci", "get_all", ["firewall"])
            if not isinstance(data, dict):
                return []

            for section_id, val in data.items():
                if isinstance(val, dict) and val.get(".type") == "rule":
                    try:
                        enabled = bool(int(val.get("enabled", "1")))
                    except ValueError, TypeError:
                        enabled = True
                    rules.append(
                        FirewallRule(
                            section_id=section_id,
                            name=str(val.get("name") or section_id),
                            enabled=enabled,
                            src=val.get("src", ""),
                            dest=val.get("dest", ""),
                            target=val.get("target", "REJECT"),
                        )
                    )
            return rules
        except Exception as err:
            _LOGGER.debug("Failed to get firewall rules via luci_rpc: %s", err)
            return []

    async def set_firewall_rule_enabled(self, section_id: str, enabled: bool) -> bool:
        """Enable or disable a firewall rule via UCI over LuCI RPC."""
        try:
            val = "1" if enabled else "0"
            cmd = f"uci set firewall.{section_id}.enabled='{val}' && uci commit firewall && /etc/init.d/firewall reload"
            await self.execute_command(cmd)
            return True
        except Exception as err:
            _LOGGER.exception("Failed to set firewall rule via LuCI RPC: %s", err)
            return False

    async def get_firewall_redirects(self) -> list[FirewallRedirect]:
        """Get firewall port forwarding redirects via LuCI RPC UCI."""
        redirects: list[FirewallRedirect] = []
        try:
            data = await self._rpc_call("uci", "get_all", ["firewall"])
            if not isinstance(data, dict):
                return []

            for section_id, val in data.items():
                if isinstance(val, dict) and val.get(".type") == "redirect":
                    redirects.append(
                        FirewallRedirect(
                            section_id=section_id,
                            name=str(val.get("name") or section_id),
                            enabled=str(val.get("enabled", "1")) == "1",
                            external_port=val.get("src_dport", ""),
                            target_ip=val.get("dest_ip", ""),
                            target_port=val.get("dest_port", ""),
                            protocol=val.get("proto", "tcp"),
                        )
                    )
            return redirects
        except Exception as err:
            _LOGGER.debug("Failed to get firewall redirects via luci_rpc: %s", err)
            return []

    async def get_firewall_rules_uci_not_used(self) -> list[FirewallRule]:
        """Deprecated."""
        return []

    async def get_access_control(self) -> list[AccessControl]:
        """Get access control rules via LuCI RPC UCI."""
        rules: list[AccessControl] = []
        try:
            # OpenWrt Parental Control usually uses firewall rules with ha_acl_ prefix
            data = await self._rpc_call("uci", "get_all", ["firewall"])
            if not isinstance(data, dict):
                return []

            for _section_id, val in data.items():
                if (
                    isinstance(val, dict)
                    and val.get(".type") == "rule"
                    and val.get("name", "").startswith("ha_acl_")
                ):
                    rules.append(
                        AccessControl(
                            mac=val.get("src_mac", "").upper(),
                            name=val.get("name", "").replace("ha_acl_", ""),
                            blocked=(val.get("target") == "REJECT"),
                            section_id=_section_id,
                        )
                    )
            return rules
        except Exception as err:
            _LOGGER.debug("Failed to get access control via luci_rpc: %s", err)
            return []

    async def get_wps_status(self) -> WpsStatus:
        """Get WPS status via LuCI RPC."""
        try:
            # We check if hostapd is running and has wps enabled
            # This is hard via RPC, but we can check wireless config
            data = await self._rpc_call("uci", "get_all", ["wireless"])
            if not isinstance(data, dict):
                return WpsStatus()

            wps_enabled = False
            for val in data.values():
                if (
                    isinstance(val, dict)
                    and val.get(".type") == "wifi-iface"
                    and int(val.get("wps_pushbutton", "0")) == 1
                ):
                    wps_enabled = True
                    break

            return WpsStatus(enabled=wps_enabled)
        except Exception as err:
            _LOGGER.debug("Failed to get WPS status via luci_rpc: %s", err)
            return WpsStatus()

    async def set_firewall_redirect_enabled(
        self,
        section_id: str,
        enabled: bool,
    ) -> bool:
        """Enable or disable a firewall redirect via UCI over LuCI RPC."""
        try:
            val = "1" if enabled else "0"
            cmd = f"uci set firewall.{section_id}.enabled='{val}' && uci commit firewall && /etc/init.d/firewall reload"
            await self.execute_command(cmd)
            return True
        except Exception as err:
            _LOGGER.exception("Failed to set firewall redirect via LuCI RPC: %s", err)
            return False

    async def get_adblock_status(self) -> AdBlockStatus:
        """Get adblock status via LuCI RPC."""
        from .base import AdBlockStatus

        status = AdBlockStatus()
        try:
            # Try ubus via sys.exec
            out = await self._rpc_call(
                "sys",
                "exec",
                ["ubus call adblock status 2>/dev/null"],
            )
            if out:
                import json

                try:
                    res = json.loads(out)
                    status.enabled = res.get("adblock_status") == "enabled"
                    status.status = res.get("adblock_status", "disabled")
                    status.version = res.get("adblock_version")
                    status.blocked_domains = int(res.get("blocked_domains", 0))
                    status.last_update = res.get("last_run")
                    return status
                except json.JSONDecodeError:
                    pass

            # Fallback to uci
            enabled = await self._rpc_call(
                "sys",
                "exec",
                ["uci -q get adblock.global.enabled"],
            )
            status.enabled = enabled.strip() == "1"
            status.status = "enabled" if status.enabled else "disabled"
        except Exception:
            pass
        return status

    async def manage_service(self, name: str, action: str) -> bool:
        """Manage a system service (start/stop/restart/enable/disable) via LuCI RPC."""
        try:
            await self._rpc_call("sys", "exec", [f"/etc/init.d/{name} {action}"])
            return True
        except Exception as err:
            _LOGGER.exception(
                "Failed to manage service %s (%s) via LuCI RPC: %s",
                name,
                action,
                err,
            )
            return False

    async def set_adblock_enabled(self, enabled: bool) -> bool:
        """Enable/disable adblock service via LuCI RPC."""
        val = "1" if enabled else "0"
        try:
            await self._rpc_call(
                "sys",
                "exec",
                [f"uci set adblock.global.enabled='{val}' && uci commit adblock"],
            )
            action = "start" if enabled else "stop"
            await self._rpc_call("sys", "exec", [f"/etc/init.d/adblock {action}"])
            return True
        except Exception:
            return False

    async def get_simple_adblock_status(self) -> SimpleAdBlockStatus:
        """Get simple-adblock status via LuCI RPC."""
        from .base import SimpleAdBlockStatus

        status = SimpleAdBlockStatus()
        try:
            res = await self._rpc_call(
                "sys",
                "exec",
                ["uci -q get simple-adblock.config.enabled"],
            )
            status.enabled = res.strip() == "1"
            status.status = "enabled" if status.enabled else "disabled"
            count = await self._rpc_call(
                "sys",
                "exec",
                ["wc -l < /tmp/simple-adblock.blocked 2>/dev/null"],
            )
            if count and count.strip().isdigit():
                status.blocked_domains = int(count.strip())
        except Exception:
            pass
        return status

    async def set_simple_adblock_enabled(self, enabled: bool) -> bool:
        """Enable/disable simple-adblock service via LuCI RPC."""
        val = "1" if enabled else "0"
        try:
            await self._rpc_call(
                "sys",
                "exec",
                [
                    f"uci set simple-adblock.config.enabled='{val}' && uci commit simple-adblock",
                ],
            )
            action = "start" if enabled else "stop"
            await self._rpc_call(
                "sys",
                "exec",
                [f"/etc/init.d/simple-adblock {action}"],
            )
            return True
        except Exception:
            return False

    async def get_banip_status(self) -> BanIpStatus:
        """Get ban-ip status via LuCI RPC."""
        from .base import BanIpStatus

        status = BanIpStatus()
        try:
            res = await self._rpc_call(
                "sys",
                "exec",
                ["uci -q get ban-ip.config.enabled"],
            )
            status.enabled = res.strip() == "1"
            status.status = "enabled" if status.enabled else "disabled"
        except Exception:
            pass
        return status

    async def set_banip_enabled(self, enabled: bool) -> bool:
        """Enable/disable ban-ip service via LuCI RPC."""
        val = "1" if enabled else "0"
        try:
            await self._rpc_call(
                "sys",
                "exec",
                [f"uci set ban-ip.config.enabled='{val}' && uci commit ban-ip"],
            )
            action = "start" if enabled else "stop"
            await self._rpc_call("sys", "exec", [f"/etc/init.d/ban-ip {action}"])
            return True
        except Exception:
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
                        ),
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
            _LOGGER.exception("Failed to set SQM config via LuCI RPC: %s", err)
            return False

    async def get_system_logs(self, count: int = 10) -> list[str]:
        """Get recent system log entries via LuCI RPC."""
        try:
            # 1. Try via direct ubus call (More robust, avoids flag detection issues)
            try:
                # In LuCI RPC, ubus call is just another RPC call to 'ubus' endpoint
                res = await self._rpc_call(
                    "ubus", "call", ["log", "read", {"lines": int(count or 10)}]
                )
                if res and isinstance(res, dict) and "log" in res:
                    return [
                        entry.get("msg", "").strip()
                        for entry in res.get("log", [])
                        if entry.get("msg")
                    ]
            except Exception as err:
                _LOGGER.debug(
                    "Direct ubus log read via LuCI RPC failed, falling back to logread: %s",
                    err,
                )

            # 2. Fallback to via execute_command (sys.exec)
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

    async def get_nlbwmon_data(self) -> dict[str, NlbwmonTraffic]:
        """Get bandwidth usage per MAC from nlbwmon via LuCI RPC."""
        # Try ubus first if available
        try:
            result = await self._rpc_call(
                "ubus", "call", ["nlbwmon", "get_data", {"group_by": "mac"}]
            )
            if result and "data" in result:
                traffic = {}
                for mac, data in result["data"].items():
                    mac_upper = mac.upper()
                    traffic[mac_upper] = NlbwmonTraffic(
                        mac=mac_upper,
                        rx_bytes=data.get("rx", 0),
                        tx_bytes=data.get("tx", 0),
                        rx_packets=data.get("rx_packets", 0),
                        tx_packets=data.get("tx_packets", 0),
                    )
                return traffic
        except Exception:
            pass

        # Fallback to sys.exec
        try:
            out = await self._rpc_call("sys", "exec", ["nlbw -c json -g mac"])
            if out:
                import json

                result = json.loads(out)
                if result and "data" in result:
                    traffic = {}
                    for entry in result["data"]:
                        mac = entry.get("mac", "").upper()
                        if not mac:
                            continue
                        traffic[mac] = NlbwmonTraffic(
                            mac=mac,
                            rx_bytes=entry.get("rx", 0),
                            tx_bytes=entry.get("tx", 0),
                            rx_packets=entry.get("rx_packets", 0),
                            tx_packets=entry.get("tx_packets", 0),
                        )
                    return traffic
        except Exception as err:
            _LOGGER.debug("Failed to get nlbwmon data via LuCI RPC: %s", err)
        return {}

    async def get_wifi_credentials(self) -> list[WifiCredentials]:
        """Get wifi credentials via LuCI RPC."""
        try:
            # We use uci.get_all(["wireless"]) for LuCI RPC
            data = await self._rpc_call("uci", "get_all", ["wireless"])
            if not isinstance(data, dict):
                return []

            creds = []
            for name, val in data.items():
                if (
                    isinstance(val, dict)
                    and val.get(".type") == "wifi-iface"
                    and val.get("mode") == "ap"
                ):
                    creds.append(
                        WifiCredentials(
                            iface=name,
                            ssid=val.get("ssid", ""),
                            encryption=val.get("encryption", "none"),
                            key=val.get("key", ""),
                            hidden=bool(int(val.get("hidden", 0))),
                        )
                    )
            return creds
        except Exception as err:
            _LOGGER.debug("Failed to get wifi credentials via luci_rpc: %s", err)
            return []

    async def get_mwan_status(self) -> list[MwanStatus]:
        """Get multi-wan status via LuCI RPC."""
        try:
            # Try ubus first
            stdout = await self.execute_command("ubus call mwan3 status 2>/dev/null")
            if stdout and stdout.startswith("{"):
                result = json.loads(stdout)
                status_list = []
                for name, data in result.get("interfaces", {}).items():
                    status_list.append(
                        MwanStatus(
                            interface_name=name,
                            status=data.get("status", "unknown"),
                            online_ratio=float(data.get("online_ratio", 0.0)),
                            uptime=int(data.get("uptime", 0)),
                            enabled=bool(data.get("enabled", False)),
                            latency=data.get("latency"),
                            packet_loss=data.get("packet_loss"),
                        )
                    )
                return status_list
            return []
        except Exception as err:
            _LOGGER.debug("Failed to get mwan3 status via luci_rpc: %s", err)
            return []

    async def trigger_wps_push(self, interface: str) -> bool:
        """Trigger WPS push button via LuCI RPC."""
        try:
            # We use execute_command abstraction to call ubus
            await self.execute_command(f"ubus call hostapd.{interface} wps_push")
            return True
        except Exception as err:
            _LOGGER.debug(
                "Failed to trigger WPS push via luci_rpc for %s: %s", interface, err
            )
            return False

    async def set_led(self, name: str, enabled: bool) -> bool:
        """Enable or disable an LED via LuCI RPC."""
        try:
            val = 255 if enabled else 0
            await self.execute_command(
                f"echo {val} > /sys/class/leds/{name}/brightness"
            )
            return True
        except Exception as err:
            _LOGGER.debug("Failed to set LED %s via luci_rpc: %s", name, err)
            return False

    async def get_services(self) -> list[ServiceInfo]:
        """Get list of system services via ubus rc list."""
        services: list[ServiceInfo] = []
        try:
            # 'rc list' is more reliable as it shows both enabled and running state
            stdout = await self.execute_command("ubus call rc list 2>/dev/null")
            if stdout:
                # Find the first { and last } to extract JSON
                start = stdout.find("{")
                end = stdout.rfind("}")
                if start != -1 and end != -1:
                    try:
                        data = json.loads(stdout[start : end + 1])
                        for name, val in data.items():
                            if isinstance(val, dict):
                                services.append(
                                    ServiceInfo(
                                        name=name,
                                        enabled=val.get("enabled", False),
                                        running=val.get("running", False),
                                    )
                                )
                    except json.JSONDecodeError:
                        pass

            # Fallback to 'service list' if 'rc list' was empty
            if not services:
                stdout = await self.execute_command(
                    "ubus call service list 2>/dev/null"
                )
                if stdout:
                    start = stdout.find("{")
                    end = stdout.rfind("}")
                    if start != -1 and end != -1:
                        try:
                            data = json.loads(stdout[start : end + 1])
                            for name, val in data.items():
                                if isinstance(val, dict) and "instances" in val:
                                    running = any(
                                        inst.get("running", False)
                                        for inst in val.get("instances", {}).values()
                                    )
                                    services.append(
                                        ServiceInfo(name=name, running=running)
                                    )
                        except json.JSONDecodeError:
                            pass

            return services
        except Exception as err:
            _LOGGER.debug("Failed to get services via luci_rpc: %s", err)
            return []

    async def is_reboot_required(self) -> bool:
        """Check if reboot is required via LuCI RPC."""
        try:
            output = await self.execute_command(
                "[ -f /tmp/.reboot-needed ] || [ -f /var/run/reboot-required ] && echo 1"
            )
            return output.strip() == "1"
        except Exception:
            return False
