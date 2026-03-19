"""OpenWrt SSH client.

Communicates with OpenWrt via SSH using paramiko.
Supports both password and key-based authentication.
This is the most compatible method that works with any OpenWrt installation.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

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
    NetworkInterface,
    OpenWrtClient,
    OpenWrtPackages,
    OpenWrtPermissions,
    ServiceInfo,
    SqmStatus,
    SystemResources,
    WirelessInterface,
)

_LOGGER = logging.getLogger(__name__)


class SshError(Exception):
    """Error communicating via SSH."""


class SshAuthError(SshError):
    """Authentication error."""


class SshTimeoutError(SshError):
    """Connection or request timeout."""


class SshConnectionError(SshError):
    """TCP connection failure (e.g. refused, unreachable)."""


class SshKeyError(SshError):
    """SSH key parsing or authentication failure."""


class SshClient(OpenWrtClient):
    """Client for OpenWrt via SSH (paramiko)."""

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        port: int = 22,
        use_ssl: bool = False,
        verify_ssl: bool = False,
        ssh_key: str | None = None,
        dhcp_software: str = "auto",
    ) -> None:
        """Initialize the SSH client."""
        super().__init__(
            host, username, password, port, use_ssl, verify_ssl, dhcp_software
        )
        self._ssh_key = ssh_key
        self._client: Any = None

    async def _exec(self, command: str, retry: bool = True) -> str:
        """Execute a command via SSH and return stdout."""

        loop = asyncio.get_event_loop()

        def _run() -> str:
            if self._client is None:
                raise SshError("Not connected")
            _stdin, stdout, stderr = self._client.exec_command(command, timeout=15)
            # Read streams to prevent blocking
            out_bytes = stdout.read()
            err_bytes = stderr.read()
            # Wait for exit status
            exit_code = stdout.channel.recv_exit_status()
            output = out_bytes.decode("utf-8", errors="replace")
            error = err_bytes.decode("utf-8", errors="replace")
            if exit_code != 0 and error:
                _LOGGER.debug(
                    "SSH command '%s' returned %d: %s", command, exit_code, error
                )
            return output

        try:
            return await loop.run_in_executor(None, _run)
        except Exception as err:
            _LOGGER.debug("SSH command failed, marking as disconnected: %s", err)
            self._connected = False
            if self._client:
                try:
                    self._client.close()
                except Exception:
                    pass
                self._client = None

            if retry:
                _LOGGER.debug("Attempting to reconnect and retry SSH command...")
                try:
                    if await self.connect():
                        return await self._exec(command, retry=False)
                except Exception as reconnect_err:
                    _LOGGER.debug(
                        "SSH reconnection failed during retry: %s", reconnect_err
                    )

            return ""

    async def execute_command(self, command: str) -> str:
        """Execute a command via SSH."""
        return await self._exec(command)

    async def provision_user(self, username: str, password: str) -> tuple[bool, str | None]:
        """Create a dedicated system user and configure RPC permissions via SSH."""
        # Use the harmonized provisioning script from base
        script = PROVISION_SCRIPT_TEMPLATE.format(username=username, password=password)
        try:
            output = await self._exec(script)
            if output:
                _LOGGER.debug("Provisioning output for %s via SSH: %s", username, output)

            if "Provisioning SUCCESS" in output:
                return True, None

            if "LOG: FAIL:" in output:
                fail_msg = output.split("LOG: FAIL:")[1].splitlines()[0].strip()
                _LOGGER.error("Provisioning failed via SSH: %s", fail_msg)
                return False, fail_msg

            return (
                False,
                "Provisioning script returned failure without specific error via SSH. Check router logs (logread).",
            )
        except Exception as err:
            _LOGGER.error("Failed to provision user %s via SSH: %s", username, err)
            return False, str(err)

    async def get_installed_packages(self) -> list[str]:
        """Get a list of installed packages."""
        try:
            output = await self._exec("opkg list-installed")
            packages = []
            for line in output.splitlines():
                parts = line.split(" - ")
                if parts:
                    packages.append(parts[0].strip())
            return packages
        except Exception:
            return []

    async def connect(self) -> bool:
        """Connect via SSH."""
        loop = asyncio.get_event_loop()

        def _connect() -> None:
            import io

            import paramiko  # type: ignore

            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

            connect_kwargs: dict[str, Any] = {
                "hostname": self.host,
                "port": self.port,
                "username": self.username,
                "timeout": 10,
                "allow_agent": False,
                "look_for_keys": False,
            }

            if self._ssh_key:
                key_file = io.StringIO(self._ssh_key)
                try:
                    pkey = paramiko.RSAKey.from_private_key(key_file)
                except Exception:
                    key_file.seek(0)
                    try:
                        pkey = paramiko.Ed25519Key.from_private_key(key_file)
                    except Exception:
                        key_file.seek(0)
                        pkey = paramiko.ECDSAKey.from_private_key(key_file)
                connect_kwargs["pkey"] = pkey
            else:
                connect_kwargs["password"] = self.password

            try:
                client.connect(**connect_kwargs)
            except paramiko.AuthenticationException as err:
                raise SshAuthError(
                    f"SSH auth failed for {self.username}@{self.host}. Check credentials/key."
                ) from err
            except TimeoutError as err:
                raise SshTimeoutError(
                    f"SSH connection timed out for {self.host}"
                ) from err
            except (OSError, paramiko.SSHException) as err:
                err_str = str(err).lower()
                if "connection refused" in err_str:
                    raise SshConnectionError(
                        f"SSH connection refused on {self.host}:{self.port}. Is SSH enabled?"
                    ) from err
                if "no route to host" in err_str:
                    raise SshConnectionError(
                        f"Host {self.host} is unreachable."
                    ) from err
                raise SshError(f"SSH connection failed: {err}") from err
            except Exception as err:
                raise SshError(f"SSH connection failed: {err}") from err

            transport = client.get_transport()
            if transport:
                transport.set_keepalive(30)

            self._client = client

        try:
            await loop.run_in_executor(None, _connect)
            self._connected = True
            _LOGGER.debug("SSH connected to %s", self.host)
            return True
        except SshError, SshAuthError:
            raise
        except Exception as err:
            raise SshError(f"SSH connection error: {err}") from err

    async def disconnect(self) -> None:
        """Disconnect SSH."""
        if self._client:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._client.close)
            self._client = None
        self._connected = False

    async def get_device_info(self) -> DeviceInfo:
        """Get device information."""
        info = DeviceInfo()

        board_json = await self._exec(
            "ubus call system board 2>/dev/null || cat /etc/board.json 2>/dev/null"
        )
        if board_json and board_json.strip() and board_json.strip().startswith("{"):
            data = json.loads(board_json)
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
            info.target = release.get("target", "")
            info.firmware_version = f"{info.release_version} ({info.release_revision})"

        if not info.hostname:
            try:
                info.hostname = (
                    await self._exec("uci get system.@system[0].hostname")
                ).strip()
            except Exception:  # noqa: BLE001
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
                "else cat /sys/class/net/*/address | grep -v '00:00:00:00:00:00' | head -n 1; fi"
            )
            if mac_out and isinstance(mac_out, str) and ":" in mac_out:
                info.mac_address = mac_out.strip().lower()
        except Exception:
            pass

        # If MAC is still missing, try a different approach (ifconfig/ip)
        if not info.mac_address:
            try:
                ip_addr_out = await self._exec(
                    "ip addr show br-lan || ip addr show lan || ip addr show eth0"
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
            "df /overlay 2>/dev/null || df / 2>/dev/null",
        ]

        results = await asyncio.gather(
            *[self._exec(cmd) for cmd in cmds], return_exceptions=True
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
            try:
                lines = df_output.strip().split("\n")
                if len(lines) >= 2:
                    parts = lines[1].split()
                    if len(parts) >= 4:
                        resources.filesystem_total = int(parts[1]) * 1024
                        resources.filesystem_used = int(parts[2]) * 1024
                        resources.filesystem_free = int(parts[3]) * 1024
            except ValueError, IndexError:
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

        # 6. Thermal
        for thermal_path in [
            "/sys/class/thermal/thermal_zone0/temp",
            "/sys/class/thermal/thermal_zone1/temp",
            "/sys/devices/virtual/thermal/thermal_zone0/temp",
            "/sys/devices/virtual/thermal/thermal_zone1/temp",
            "/sys/class/hwmon/hwmon0/temp1_input",
        ]:
            try:
                temp = await self._exec(f"cat {thermal_path} 2>/dev/null")
                if not temp:
                    continue
                temp_clean = temp.strip().strip("'").strip('"')
                if not temp_clean or not temp_clean.isdigit():
                    continue
                temp_val = int(temp_clean)
                if temp_val > 1000:
                    resources.temperature = temp_val / 1000.0
                else:
                    resources.temperature = float(temp_val)
                break
            except ValueError, Exception:  # noqa: BLE001
                continue

        return resources

    async def get_external_ip(self) -> str | None:
        """Get public/external IP address."""
        try:
            status = await self._exec("ubus call network.interface dump 2>/dev/null")
            if status and status.startswith("{"):
                data = json.loads(status)
                for iface_data in data.get("interface", []):
                    iface_name = iface_data.get("interface", "").lower()
                    if iface_name in ["wan", "wan6", "wwan", "modem"]:
                        ipv4_addrs = iface_data.get("ipv4-address", [])
                        if ipv4_addrs:
                            return ipv4_addrs[0].get("address")
        except Exception:  # noqa: BLE001
            pass
        return None

    async def get_wireless_interfaces(self) -> list[WirelessInterface]:
        """Get wireless interfaces."""
        interfaces: list[WirelessInterface] = []

        try:
            wifi_json = await self._exec(
                "ubus call network.wireless status 2>/dev/null"
            )
            if wifi_json and wifi_json.strip().startswith("{"):
                data = json.loads(wifi_json)
                for _radio_name, radio_data in data.items():
                    if not isinstance(radio_data, dict):
                        continue
                    for iface in radio_data.get("interfaces", []):
                        config = iface.get("config", {})
                        iface_name = iface.get("ifname", "")
                        wifi = WirelessInterface(
                            name=iface_name,
                            ssid=config.get("ssid", ""),
                            mode=config.get("mode", ""),
                            encryption=config.get("encryption", ""),
                            enabled=not radio_data.get("disabled", False),
                            up=radio_data.get("up", False),
                        )

                        if iface_name:
                            try:
                                iwinfo = await self._exec(
                                    f"iwinfo {iface_name} info 2>/dev/null"
                                )
                                for line in iwinfo.split("\n"):
                                    line = line.strip()
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
                                    elif "Frequency:" in line:
                                        try:
                                            wifi.frequency = line.split("Frequency:")[
                                                1
                                            ].strip()
                                        except IndexError:
                                            pass

                                # Fallback: Extract frequency from Channel line if still missing
                                if not wifi.frequency and iwinfo:
                                    for line in iwinfo.splitlines():
                                        if (
                                            "Channel:" in line
                                            and "(" in line
                                            and "GHz)" in line
                                        ):
                                            try:
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

                                assoclist = await self._exec(
                                    f"iwinfo {iface_name} assoclist 2>/dev/null"
                                )
                                if assoclist.strip():
                                    wifi.clients_count = len(
                                        [
                                            line_item
                                            for line_item in assoclist.strip().split(
                                                "\n"
                                            )
                                            if line_item.strip()
                                            and ":" in line_item.split()[0]
                                            if line_item.split()
                                        ]
                                    )
                            except Exception:  # noqa: BLE001
                                pass

                        interfaces.append(wifi)
        except Exception:  # noqa: BLE001
            pass

        return interfaces

    async def get_network_interfaces(self) -> list[NetworkInterface]:
        """Get network interfaces."""
        interfaces: list[NetworkInterface] = []

        try:
            dump = await self._exec("ubus call network.interface dump 2>/dev/null")
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
        except Exception:  # noqa: BLE001
            pass

        return interfaces

    async def get_connected_devices(self) -> list[ConnectedDevice]:
        """Get connected devices by combining DHCP, ARP and wireless station info."""
        devices: dict[str, ConnectedDevice] = {}

        # 1. DHCP Leases
        try:
            leases = await self.get_dhcp_leases()
            for lease in leases:
                mac = lease.mac.lower()
                devices[mac] = ConnectedDevice(
                    mac=mac,
                    ip=lease.ip,
                    hostname=lease.hostname,
                    connected=True,
                    is_wireless=False,
                    connection_type="wired",
                )
        except Exception:  # noqa: BLE001
            pass

        # 2. IP Neighbors
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

                devices[mac] = ConnectedDevice(
                    mac=mac,
                    ip=neigh.ip,
                    interface=neigh.interface,
                    connected=True,
                    is_wireless=False,
                    connection_type="wired",
                    neighbor_state=neigh.state,
                )
        except Exception as neigh_err:  # noqa: BLE001
            _LOGGER.debug(
                "Error processing IP neighbors in get_connected_devices (SSH): %s",
                neigh_err,
            )

        # 3. Wireless Clients (iwinfo station dump)
        try:
            # Get wireless interfaces first
            iw_out = await self._exec(
                "iwinfo 2>/dev/null | grep -E '^[a-z0-9_-]+' | awk '{print $1}'"
            )
            ifaces = iw_out.strip().split()
            for iface in ifaces:
                assoc = await self._exec(f"iwinfo {iface} assoclist 2>/dev/null")
                for line in assoc.strip().split("\n"):
                    if not line.strip() or "No information" in line:
                        continue
                    # Parsing: MAC  Signal  Noise  RX_Rate  TX_Rate
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
                            dev = ConnectedDevice(mac=mac, connected=True)
                            devices[mac] = dev

                        dev.is_wireless = True
                        dev.interface = iface
                        if len(parts) >= 2:
                            dev.signal = (
                                int(parts[1]) if parts[1].lstrip("-").isdigit() else 0
                            )

                        if "5g" in iface.lower():
                            dev.connection_type = "5GHz"
                        elif "2g" in iface.lower():
                            dev.connection_type = "2.4GHz"
                        else:
                            dev.connection_type = "wireless"

        except Exception:  # noqa: BLE001
            pass

        # Fallback to ubus if iwinfo is missing or returns nothing
        if not any(d.is_wireless for d in devices.values()):
            try:
                # Find all hostapd objects and get clients
                cmd = "for obj in $(ubus list 'hostapd.*'); do echo \"$obj $(ubus call $obj get_clients)\"; done"
                stdout = await self._exec(cmd)  # Changed from _exec_command to _exec
                for line in stdout.splitlines():
                    if not line.strip():
                        continue
                    parts = line.split(" ", 1)
                    if len(parts) < 2:
                        continue
                    obj_name, data_str = parts
                    iface_name = obj_name.split(".", 1)[1]
                    try:
                        data = json.loads(data_str)
                        if data and isinstance(data, dict) and "clients" in data:
                            for mac, info in data["clients"].items():
                                # Create ConnectedDevice object from ubus data
                                if mac.lower() not in devices:
                                    dev = ConnectedDevice(
                                        mac=mac.lower(), connected=True
                                    )
                                    devices[mac.lower()] = dev
                                else:
                                    dev = devices[mac.lower()]

                                dev.is_wireless = True
                                dev.interface = iface_name
                                dev.signal = info.get("signal", 0)
                                # ubus hostapd get_clients doesn't directly provide 2.4/5GHz info
                                # We can infer from interface name if it contains '2g' or '5g'
                                if "5g" in iface_name.lower():
                                    dev.connection_type = "5GHz"
                                elif "2g" in iface_name.lower():
                                    dev.connection_type = "2.4GHz"
                                else:
                                    dev.connection_type = "wireless"

                    except Exception:
                        continue
            except Exception:
                pass

        return list(devices.values())

    async def get_services(self) -> list[ServiceInfo]:
        """Get init.d services."""
        services: list[ServiceInfo] = []

        try:
            ls_output = await self._exec("ls /etc/init.d/ 2>/dev/null")
            for svc_name in ls_output.strip().split("\n"):
                svc_name = svc_name.strip()
                if not svc_name:
                    continue
                enabled = False
                running = False
                try:
                    enabled_check = await self._exec(
                        f"/etc/init.d/{svc_name} enabled && echo yes || echo no"
                    )
                    enabled = "yes" in enabled_check
                    running_check = await self._exec(
                        f"/etc/init.d/{svc_name} running && echo yes || echo no"
                    )
                    running = "yes" in running_check
                except Exception:  # noqa: BLE001
                    pass
                services.append(
                    ServiceInfo(name=svc_name, enabled=enabled, running=running)
                )
        except Exception:  # noqa: BLE001
            pass

        return services

    async def manage_service(self, name: str, action: str) -> bool:
        """Manage a service."""
        try:
            await self._exec(f"/etc/init.d/{name} {action}")
            return True
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Failed to %s service %s: %s", action, name, err)
            return False

    async def reboot(self) -> bool:
        """Reboot the device."""
        try:
            await self._exec("reboot")
            return True
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Failed to reboot: %s", err)
            return False

    async def set_wireless_enabled(self, interface: str, enabled: bool) -> bool:
        """Enable/disable a wireless interface."""
        try:
            action = "0" if enabled else "1"
            await self._exec(f"uci set wireless.{interface}.disabled='{action}'")
            await self._exec("uci commit wireless")
            await self._exec("wifi reload")
            return True
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Failed to set wireless %s: %s", interface, err)
            return False

    async def get_leds(self) -> list:
        """Get LEDs from /sys/class/leds."""
        from .base import LedInfo

        leds: list[LedInfo] = []
        try:
            output = await self._exec(
                "for led in /sys/class/leds/*/; do "
                'name=$(basename "$led"); '
                'brightness=$(cat "$led/brightness" 2>/dev/null || echo 0); '
                'max=$(cat "$led/max_brightness" 2>/dev/null || echo 255); '
                'trigger=$(cat "$led/trigger" 2>/dev/null | tr " " "\\n" | grep "^\\[" | tr -d "[]" || echo none); '
                'echo "$name|$brightness|$max|$trigger"; '
                "done"
            )
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
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Cannot list LEDs via SSH")

        return leds

    async def set_led(self, name: str, brightness: int) -> bool:
        """Set LED brightness via SSH."""
        try:
            await self._exec(f"echo {brightness} > /sys/class/leds/{name}/brightness")
            return True
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Failed to set LED %s: %s", name, err)
            return False

    async def check_permissions(self) -> OpenWrtPermissions:
        """Check user permissions via SSH.

        SSH access generally provides full root access, but we try to
        verify if common commands work to be safe.
        """
        from .base import OpenWrtPermissions

        perms = OpenWrtPermissions()

        try:
            # First check if we are root
            id_out = await self._exec("id -u")
            if id_out.strip() == "0":
                # Root has all permissions
                for attr in perms.__dict__:
                    if not attr.startswith("_"):
                        setattr(perms, attr, True)
                return perms

            # Test uci read access for non-root
            await self._exec("uci get system.@system[0] 2>/dev/null")
            perms.read_system = True
            perms.read_network = True
            perms.read_firewall = True
            perms.read_wireless = True
            perms.read_sqm = True
            perms.read_led = True
            perms.read_vpn = True
            perms.read_mwan = True
            perms.read_devices = True
            perms.read_services = True

            # Test write access (we won't actually write, but SSH usually has full rights if it can read)
            perms.write_system = True
            perms.write_network = True
            perms.write_firewall = True
            perms.write_wireless = True
            perms.write_sqm = True
            perms.write_led = True
            perms.write_devices = True
            perms.write_services = True
            perms.write_access_control = True
        except Exception:
            pass
        return perms

    async def check_packages(self) -> OpenWrtPackages:
        """Check installed packages."""
        packages = OpenWrtPackages()
        try:
            # Check packages using a single fast SSH command if possible,
            # or multiple if needed. Here we check existence of binaries or init scripts.
            cmd = (
                "for f in /etc/init.d/sqm /etc/init.d/mwan3 /usr/bin/iwinfo "
                "/usr/bin/etherwake /usr/bin/wg /usr/sbin/openvpn "
                "/usr/lib/lua/luci/controller/rpc.lua "
                "/usr/lib/lua/luci/controller/attendedsysupgrade.lua; do "
                "if [ -f $f ] || [ -x $f ]; then echo 1; else echo 0; fi; done"
            )
            out = await self._exec(cmd)
            results = out.strip().splitlines()

            def detect_status(idx: int) -> bool:
                return len(results) > idx and results[idx].strip() == "1"

            packages.sqm_scripts = detect_status(0)
            packages.mwan3 = detect_status(1)
            packages.iwinfo = detect_status(2)
            packages.etherwake = detect_status(3)
            packages.wireguard = detect_status(4)
            packages.openvpn = detect_status(5)
            packages.luci_mod_rpc = detect_status(6)
            packages.asu = detect_status(7)
        except Exception as err:
            _LOGGER.error("Failed to check packages via SSH: %s", err)
            # Initialize to False if we failed (to avoid staying at None)
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
        return packages

    async def get_firewall_rules(self) -> list[FirewallRule]:
        """Get firewall rules via UCI over SSH."""
        from .base import FirewallRule

        rules: list[FirewallRule] = []
        try:
            output = await self._exec("uci show firewall")
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
            _LOGGER.error("Failed to get firewall rules via SSH: %s", err)
        return rules

    async def set_firewall_rule_enabled(self, section_id: str, enabled: bool) -> bool:
        """Enable or disable a firewall rule via UCI over SSH."""
        try:
            val = "1" if enabled else "0"
            await self._exec(f"uci set firewall.{section_id}.enabled='{val}'")
            await self._exec("uci commit firewall")
            await self._exec("/etc/init.d/firewall reload")
            return True
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Failed to set firewall rule via SSH: %s", err)
            return False

    async def get_firewall_redirects(self) -> list[FirewallRedirect]:
        """Get firewall port forwarding redirects via UCI over SSH."""
        redirects: list[FirewallRedirect] = []
        try:
            output = await self._exec("uci show firewall")
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
            _LOGGER.error("Failed to get firewall redirects via SSH: %s", err)
        return redirects

    async def set_firewall_redirect_enabled(
        self, section_id: str, enabled: bool
    ) -> bool:
        """Enable or disable a firewall redirect via UCI over SSH."""
        try:
            val = "1" if enabled else "0"
            await self._exec(f"uci set firewall.{section_id}.enabled='{val}'")
            await self._exec("uci commit firewall")
            await self._exec("/etc/init.d/firewall reload")
            return True
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Failed to set firewall redirect via SSH: %s", err)
            return False

    async def get_access_control(self) -> list[AccessControl]:
        """Get access control rules via UCI firewall rules over SSH."""
        rules: list[AccessControl] = []
        try:
            output = await self._exec("uci show firewall")
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
                if data.get(".type") != "rule":
                    continue
                name = data.get("name", "")
                if not name.startswith("ha_acl_"):
                    continue

                mac = data.get("src_mac", "").upper()
                if mac:
                    rules.append(
                        AccessControl(
                            mac=mac,
                            name=name.replace("ha_acl_", ""),
                            blocked=data.get("enabled", "1") == "1"
                            and data.get("target") in ("REJECT", "DROP"),
                            section_id=section_id,
                        )
                    )
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Failed to get access control via SSH: %s", err)
        return rules

    async def set_access_control_blocked(self, mac: str, blocked: bool) -> bool:
        """Block or unblock internet access for a MAC via SSH."""
        mac_upper = mac.upper()
        mac_safe = mac_upper.replace(":", "")
        rule_name = f"ha_acl_{mac_safe}"
        try:
            rules = await self.get_access_control()
            section_id = next((r.section_id for r in rules if r.mac == mac_upper), None)

            if blocked:
                if not section_id:
                    await self._exec("uci add firewall rule")
                    await self._exec(f"uci set firewall.{rule_name}=rule")
                    section_id = rule_name
                    await self._exec(
                        f"uci set firewall.{section_id}.name='{rule_name}'"
                    )
                    await self._exec(f"uci set firewall.{section_id}.src='lan'")
                    await self._exec(f"uci set firewall.{section_id}.dest='wan'")
                    await self._exec(
                        f"uci set firewall.{section_id}.src_mac='{mac_upper}'"
                    )
                    await self._exec(f"uci set firewall.{section_id}.target='REJECT'")

                await self._exec(f"uci set firewall.{section_id}.enabled='1'")
            else:
                if section_id:
                    await self._exec(f"uci set firewall.{section_id}.enabled='0'")

            await self._exec("uci commit firewall")
            await self._exec("/etc/init.d/firewall reload")
            return True
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Failed to set access control via SSH: %s", err)
            return False

    async def manage_interface(self, name: str, action: str) -> bool:
        """Manage a network interface (up/down/reconnect) via SSH."""
        try:
            if action == "reconnect":
                await self._exec(f"ifdown {name} && ifup {name}")
            elif action == "up":
                await self._exec(f"ifup {name}")
            elif action == "down":
                await self._exec(f"ifdown {name}")
            return True
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Failed to manage interface %s: %s", name, err)
            return False

    async def install_firmware(self, url: str, keep_settings: bool = True) -> None:
        """Install firmware from the given URL via SSH."""
        # Use sysupgrade for installation
        # Download to /tmp and then run sysupgrade
        keep = "" if keep_settings else "-n"
        cmd = f"wget -O /tmp/firmware.bin '{url}' && sysupgrade {keep} /tmp/firmware.bin"
        try:
            _LOGGER.info("Initiating firmware installation via SSH from: %s", url)
            # We expect this to eventually fail or disconnect as the router reboots
            await self._exec(cmd)
        except Exception as err:
            # If it's a connection error, it's likely the router rebooting
            err_msg = str(err).lower()
            if any(
                msg in err_msg
                for msg in ["connection reset", "broken pipe", "closed", "eof"]
            ):
                _LOGGER.info(
                    "SSH connection lost during sysupgrade - device is likely rebooting"
                )
                return
            _LOGGER.warning(
                "Sysupgrade command might have failed or disconnected: %s", err
            )

    async def download_file(self, remote_path: str, local_path: str) -> bool:
        """Download a file from the router via SSH using cat (fallback for SCP)."""
        try:
            # Using cat and reading back the result. For larger files this might be slow,
            # but for backups (~some KB) it should be fine.
            content = await self._exec(f"cat {remote_path}")
            if content:
                # We need to be careful with binary data over SSH exec
                # If the file is a .tar.gz, it's binary.
                # Let's try base64 to be safe if it's binary.
                b64_content = await self._exec(f"base64 {remote_path}")
                import base64
                with open(local_path, "wb") as f:
                    f.write(base64.b64decode(b64_content))
                return True
        except Exception as err:
            _LOGGER.error("Failed to download file via SSH: %s", err)
        return False

    async def get_dhcp_leases(self) -> list[DhcpLease]:
        """Get DHCP leases via SSH."""
        if self.dhcp_software == "none":
            return []

        leases: list[DhcpLease] = []

        # Try odhcpd via ubus over SSH
        if self.dhcp_software in ("auto", "odhcpd"):
            try:
                stdout = await self._exec("ubus call dhcp ipv4leases 2>/dev/null")
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
                        "Requested odhcpd but 'ubus call dhcp' failed via SSH"
                    )
                    return []

        # Try dnsmasq via file over SSH
        if self.dhcp_software in ("auto", "dnsmasq"):
            try:
                content = await self._exec("cat /tmp/dhcp.leases 2>/dev/null")
                for line in content.strip().split("\n"):
                    parts = line.split()
                    if len(parts) >= 4:
                        leases.append(
                            DhcpLease(
                                expires=int(parts[0]) if parts[0].isdigit() else 0,
                                mac=parts[1].lower(),
                                ip=parts[2],
                                hostname=parts[3] if parts[3] != "*" else "",
                                connected=True,
                                is_wireless=False,
                                connection_type="wired",
                            )
                        )
            except Exception:  # noqa: BLE001
                if self.dhcp_software == "dnsmasq":
                    _LOGGER.debug(
                        "Requested dnsmasq but cat /tmp/dhcp.leases failed via SSH"
                    )

        return leases

    async def get_ip_neighbors(self) -> list[IpNeighbor]:
        """Get IP neighbor (ARP/NDP) table via SSH."""
        neighbors: list[IpNeighbor] = []
        try:
            # Try ip neigh show first (supports IPv6 and states)
            content = await self._exec("ip neigh show 2>/dev/null")
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

        # Fallback to /proc/net/arp
        if not neighbors:
            try:
                content = await self._exec("cat /proc/net/arp 2>/dev/null")
                lines = content.strip().split("\n")
                if len(lines) > 1:
                    for line in lines[1:]:
                        parts = line.split()
                        if len(parts) >= 4:
                            neighbors.append(
                                IpNeighbor(
                                    ip=parts[0],
                                    mac=parts[3].upper(),
                                    interface=parts[5] if len(parts) > 5 else "",
                                    state="REACHABLE" if parts[2] != "0x0" else "STALE",
                                )
                            )
            except Exception:  # noqa: BLE001
                pass

        return neighbors

    async def get_sqm_status(self) -> list[SqmStatus]:
        """Get SQM status via SSH."""

        sqm_instances: list[SqmStatus] = []
        try:
            output = await self._exec("uci show sqm 2>/dev/null")
            if not output:
                return sqm_instances

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
                    if len(parts) == 2:
                        sections[section][".type"] = val.strip("'")
                    elif len(parts) >= 3:
                        sections[section][parts[2]] = val.strip("'")

            for section_id, data in sections.items():
                if data.get(".type") == "queue":
                    sqm_instances.append(
                        SqmStatus(
                            section_id=section_id,
                            name=data.get("name", section_id),
                            enabled=data.get("enabled") == "1",
                            interface=data.get("interface", ""),
                            download=int(data.get("download", "0")),
                            upload=int(data.get("upload", "0")),
                            qdisc=data.get("qdisc", ""),
                            script=data.get("script", ""),
                        )
                    )
        except Exception as err:
            _LOGGER.debug("Failed to get SQM status via SSH: %s", err)
        return sqm_instances

    async def set_sqm_config(self, section_id: str, **kwargs: Any) -> bool:
        """Set SQM configuration via SSH."""
        try:
            for key, value in kwargs.items():
                val_str = (
                    "1" if value is True else "0" if value is False else str(value)
                )
                await self._exec(f"uci set sqm.{section_id}.{key}='{val_str}'")
            await self._exec("uci commit sqm")
            await self._exec("/etc/init.d/sqm reload")
            return True
        except Exception as err:
            _LOGGER.error("Failed to set SQM config via SSH: %s", err)
            return False

    async def get_gateway_mac(self) -> str | None:
        """Get the default gateway MAC address via SSH."""
        try:
            # 1. Get default gateway IP
            route_out = await self._exec("ip route show default 2>/dev/null")
            if not route_out:
                return None

            # Example: default via 192.168.178.1 dev eth0 proto static
            parts = route_out.split()
            if "via" not in parts:
                return None

            gw_ip = parts[parts.index("via") + 1]

            # 2. Get MAC for that IP
            neigh_out = await self._exec(f"ip neigh show {gw_ip} 2>/dev/null")
            if "lladdr" in neigh_out:
                neigh_parts = neigh_out.split()
                return neigh_parts[neigh_parts.index("lladdr") + 1].upper()
        except Exception as err:
            _LOGGER.debug("Failed to get gateway MAC via SSH: %s", err)
        return None

    async def get_lldp_neighbors(self) -> list[LldpNeighbor]:
        """Get LLDP neighbor information via SSH."""
        from .base import LldpNeighbor

        neighbors: list[LldpNeighbor] = []
        try:
            # Method 1: ubus (preferred)
            stdout = await self._exec("ubus call lldp show 2>/dev/null")
            if stdout and stdout.strip().startswith("{"):
                data = json.loads(stdout)
                # Parse ubus lldp output structure
                # {"lldp": {"interface": [{"name": "eth0", "neighbor": [{...}]}]}}
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
                                        neighbor_port=neigh.get("port", {}).get(
                                            "id", ""
                                        )
                                        if isinstance(neigh.get("port"), dict)
                                        else "",
                                        neighbor_chassis=neigh.get("chassis", {}).get(
                                            "id", ""
                                        )
                                        if isinstance(neigh.get("chassis"), dict)
                                        else "",
                                        neighbor_description=neigh.get(
                                            "description", ""
                                        ),
                                        neighbor_system_name=neigh.get("sysname", ""),
                                    )
                                )
                if neighbors:
                    return neighbors

            # Method 2: lldpcli json
            stdout = await self._exec("lldpcli show neighbors -f json 2>/dev/null")
            if stdout and stdout.strip().startswith("{"):
                data = json.loads(stdout)
                # lldpcli structure: {"lldp": {"interface": {"eth0": {"neighbor": {...}}}}}
                interfaces = data.get("lldp", {}).get("interface", {})
                if isinstance(interfaces, dict):
                    for iface_name, iface_data in interfaces.items():
                        neighs = iface_data.get("neighbor", [])
                        if isinstance(neighs, dict):
                            neighs = [neighs]
                        if isinstance(neighs, list):
                            for neigh in neighs:
                                neighbors.append(
                                    LldpNeighbor(
                                        local_interface=iface_name,
                                        neighbor_name=neigh.get("name", ""),
                                        neighbor_port=neigh.get("port", {})
                                        .get("id", {})
                                        .get("value", "")
                                        if isinstance(neigh.get("port"), dict)
                                        else "",
                                        neighbor_chassis=neigh.get("chassis", {})
                                        .get("id", {})
                                        .get("value", "")
                                        if isinstance(neigh.get("chassis"), dict)
                                        else "",
                                        neighbor_description=neigh.get(
                                            "description", ""
                                        ),
                                        neighbor_system_name=neigh.get("sysname", ""),
                                    )
                                )
        except Exception as err:
            _LOGGER.debug("Failed to get LLDP neighbors via SSH: %s", err)
        return neighbors
