"""OpenWrt SSH client.

Communicates with OpenWrt via SSH using paramiko.
Supports both password and key-based authentication.
This is the most compatible method that works with any OpenWrt installation.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from typing import Any

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
            host,
            username,
            password,
            port,
            use_ssl,
            verify_ssl,
            dhcp_software,
        )
        self._ssh_key = ssh_key
        self._client: Any = None
        self._semaphore = asyncio.Semaphore(2)  # Limit concurrent SSH commands

    async def _exec(self, command: str, retry: bool = True) -> str:
        """Execute a command via SSH and return stdout."""
        loop = asyncio.get_event_loop()

        def _run() -> str:
            if self._client is None:
                msg = "Not connected"
                raise SshError(msg)
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
                    "SSH command '%s' returned %d: %s",
                    command,
                    exit_code,
                    error,
                )
            return output

        try:
            async with self._semaphore:
                return await loop.run_in_executor(None, _run)
        except Exception as err:
            _LOGGER.debug("SSH command failed, marking as disconnected: %s", err)
            self._connected = False
            if self._client:
                with contextlib.suppress(Exception):
                    self._client.close()
                self._client = None

            if retry:
                _LOGGER.debug("Attempting to reconnect and retry SSH command...")
                try:
                    if await self.connect():
                        return await self._exec(command, retry=False)
                except Exception as reconnect_err:
                    _LOGGER.debug(
                        "SSH reconnection failed during retry: %s",
                        reconnect_err,
                    )

            return ""

    async def execute_command(self, command: str) -> str:
        """Execute a command via SSH."""
        return await self._exec(command)

    async def provision_user(
        self,
        username: str,
        password: str,
    ) -> tuple[bool, str | None]:
        """Create a dedicated system user and configure RPC permissions via SSH."""
        # Use the harmonized provisioning script from base
        script = PROVISION_SCRIPT_TEMPLATE.format(username=username, password=password)
        try:
            output = await self._exec(script)

            if output is None:
                output = ""

            if output:
                _LOGGER.debug(
                    "Provisioning output for %s via SSH: %s",
                    username,
                    output,
                )

            if "Provisioning SUCCESS" in output:
                return True, None

            if "LOG: FAIL:" in output:
                fail_msg = output.split("LOG: FAIL:")[1].splitlines()[0].strip()
                _LOGGER.error("Provisioning failed via SSH: %s", fail_msg)
                return False, fail_msg

            # Empty output usually means permission denied
            if not output:
                _LOGGER.warning(
                    "Provisioning for %s returned empty output. "
                    "Ensure '%s' has appropriate execution rights.",
                    username,
                    self.username,
                )
                return (
                    False,
                    (
                        f"Provisioning failed: empty response from SSH. "
                        f"Ensure '{self.username}' has execution permission."
                    ),
                )

            return (
                False,
                "Provisioning script returned failure without specific error via SSH. Check router logs (logread).",
            )
        except Exception as err:
            _LOGGER.exception("Failed to provision user %s via SSH: %s", username, err)
            return False, str(err)

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
            output = await self._exec(cmd)
            if not output:
                return []
            packages: list[str] = []
            for line in output.splitlines():
                name = line.strip()
                if not name:
                    continue

                # Strip version for apk (package-version-release)
                # On OpenWrt/APK, 'apk info -q' sometimes includes the version suffix.
                # Standard pkg names don't have digits immediately after a dash
                # unless it's the version part.
                if "-" in name and any(c.isdigit() for c in name):
                    parts = name.split("-")
                    # Package names like "sqm-scripts" are ok, but "sqm-scripts-1.0" should be "sqm-scripts"
                    # We look for the first part that starts with a digit or the last two parts
                    for i in range(1, len(parts)):
                        if parts[i] and parts[i][0].isdigit():
                            name = "-".join(parts[:i])
                            break

                packages.append(name)
            return list(set(packages))
        except Exception:
            return []

    async def connect(self) -> bool:
        """Connect via SSH."""
        loop = asyncio.get_event_loop()

        def _connect() -> None:
            import io

            import paramiko  # type: ignore

            client = paramiko.SSHClient()
            client.load_system_host_keys()

            if self.verify_ssl:
                client.set_missing_host_key_policy(paramiko.RejectPolicy())
            else:
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
                msg = f"SSH auth failed for {self.username}@{self.host}. Check credentials/key."
                raise SshAuthError(
                    msg,
                ) from err
            except TimeoutError as err:
                msg = f"SSH connection timed out for {self.host}"
                raise SshTimeoutError(
                    msg,
                ) from err
            except (OSError, paramiko.SSHException) as err:
                err_str = str(err).lower()
                if "connection refused" in err_str:
                    msg = f"SSH connection refused on {self.host}:{self.port}. Is SSH enabled?"
                    raise SshConnectionError(
                        msg,
                    ) from err
                if "no route to host" in err_str:
                    msg = f"Host {self.host} is unreachable."
                    raise SshConnectionError(
                        msg,
                    ) from err
                msg = f"SSH connection failed: {err}"
                raise SshError(msg) from err
            except Exception as err:
                msg = f"SSH connection failed: {err}"
                raise SshError(msg) from err

            transport = client.get_transport()
            if transport:
                transport.set_keepalive(30)

            self._client = client

        try:
            await loop.run_in_executor(None, _connect)
            self._connected = True
            _LOGGER.debug("SSH connected to %s", self.host)
            return True
        except (
            SshError,
            SshAuthError,
        ):
            raise
        except Exception as err:
            msg = f"SSH connection error: {err}"
            raise SshError(msg) from err

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
            from .base import StorageUsage

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

    async def get_external_ip(self) -> str | None:
        """Get public/external IP address."""
        try:
            # 1. Try to find the default gateway interface
            route_info = await self.execute_command("ip route show default 2>/dev/null")
            wan_iface = None
            if route_info and "dev " in route_info:
                parts = route_info.split()
                try:
                    dev_idx = parts.index("dev")
                    wan_iface = parts[dev_idx + 1]
                except ValueError, IndexError:
                    pass

            # 2. Get interface dump
            status = await self._exec("ubus call network.interface dump 2>/dev/null")
            if status and status.startswith("{"):
                data = json.loads(status)
                for iface_data in data.get("interface", []):
                    iface_id = iface_data.get("interface", "").lower()
                    l3_dev = iface_data.get("l3_device", "").lower()

                    # Match by name or by the device found in routes
                    if iface_id in ["wan", "wan6", "wwan", "modem"] or (
                        wan_iface and (iface_id == wan_iface or l3_dev == wan_iface)
                    ):
                        ipv4_addrs = iface_data.get("ipv4-address", [])
                        if ipv4_addrs:
                            ip = ipv4_addrs[0].get("address")
                            # If it's a private IP, we might want to try an external check
                            if ip and not ip.startswith(
                                (
                                    "192.168.",
                                    "10.",
                                    "172.16.",
                                    "172.17.",
                                    "172.18.",
                                    "172.19.",
                                    "172.20.",
                                    "172.21.",
                                    "172.22.",
                                    "172.23.",
                                    "172.24.",
                                    "172.25.",
                                    "172.26.",
                                    "172.27.",
                                    "172.28.",
                                    "172.29.",
                                    "172.30.",
                                    "172.31.",
                                )
                            ):
                                return ip

                            # If we found a private IP but no public one yet, keep it as fallback
                            fallback_ip = ip

            # 3. Fallback: Try to get real public IP via external service if we only have a private one or none
            external_check = await self.execute_command(
                "curl -s http://icanhazip.com || wget -qO- http://icanhazip.com || curl -s https://api.ipify.org || wget -qO- https://api.ipify.org 2>/dev/null"
            )
            if external_check and "." in external_check:
                return external_check.strip().splitlines()[0]

            return fallback_ip if "fallback_ip" in locals() else None
        except Exception:  # noqa: BLE001
            return None

    async def get_wireless_interfaces(self) -> list[WirelessInterface]:
        """Get wireless interfaces via ubus iwinfo."""
        interfaces: list[WirelessInterface] = []
        if self.packages.wireless is False:
            return interfaces
        iface_names: set[str] = set()

        # 1. Primary source: network.wireless status
        try:
            wifi_json = await self._exec(
                "ubus call network.wireless status 2>/dev/null"
            )
            if wifi_json and wifi_json.strip().startswith("{"):
                data = json.loads(wifi_json)
                for radio_name, radio_data in data.items():
                    if not isinstance(radio_data, dict):
                        continue
                    for iface in radio_data.get("interfaces", []):
                        config = iface.get("config", {})
                        iface_name = (
                            iface.get("ifname")
                            or iface.get("section")
                            or iface.get("device", "")
                        )
                        if not iface_name or iface_name in iface_names:
                            continue

                        wifi = WirelessInterface(
                            name=iface_name,
                            ssid=config.get("ssid", ""),
                            mode=config.get("mode", ""),
                            encryption=config.get("encryption", ""),
                            enabled=not radio_data.get("disabled", False),
                            up=radio_data.get("up", False),
                            radio=radio_name,
                            band=WirelessInterface._band_from_raw(
                                radio_data.get("config", {}).get("band", "")
                                or radio_data.get("config", {}).get("hwmode", "")
                            ),
                            hwmode=radio_data.get("config", {}).get("hwmode", ""),
                            section=iface.get("section"),
                            ifname=iface.get("ifname"),
                        )
                        interfaces.append(wifi)
                        iface_names.add(iface_name)
                        if wifi.section and wifi.section != iface_name:
                            iface_names.add(wifi.section)
                        if wifi.ifname and wifi.ifname != iface_name:
                            iface_names.add(wifi.ifname)
        except Exception as err:
            _LOGGER.debug("Failed to get network.wireless status via SSH: %s", err)

        # 2. Supplement: iwinfo devices
        try:
            iw_devs_str = await self._exec("ubus call iwinfo devices 2>/dev/null")
            if iw_devs_str and iw_devs_str.strip().startswith("{"):
                iw_devs = json.loads(iw_devs_str).get("devices", [])
                for name in iw_devs:
                    if name not in iface_names:
                        wifi = WirelessInterface(name=name, enabled=True, up=True)
                        interfaces.append(wifi)
                        iface_names.add(name)
        except Exception as err:
            _LOGGER.debug("network.wireless status failed via SSH: %s", err)

        # 2. UCI fallback if no interfaces found via ubus
        if not interfaces:
            try:
                uci_wireless_str = await self._exec("uci export wireless 2>/dev/null")
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

                        iface_name = sect_data.get("ifname") or sect_name
                        radio_name = sect_data.get("device", "")
                        radio_disabled = (
                            sections.get(radio_name, {}).get("disabled", "0") == "1"
                        )
                        iface_disabled = sect_data.get("disabled", "0") == "1"

                        ifname_val = sect_data.get("ifname")
                        is_disabled = radio_disabled or iface_disabled

                        wifi = WirelessInterface(
                            name=iface_name,
                            ssid=sect_data.get("ssid", ""),
                            mode=sect_data.get("mode", ""),
                            encryption=sect_data.get("encryption", ""),
                            enabled=not is_disabled,
                            up=not is_disabled,
                            radio=radio_name,
                            hwmode=sections.get(radio_name, {}).get("hwmode", ""),
                            section=sect_name,
                            ifname=ifname_val or "",
                        )
                        # Only add if not explicitly disabled or if we have no other choice
                        if not is_disabled:
                            interfaces.append(wifi)
                            iface_names.add(iface_name)
                            if sect_name and sect_name != iface_name:
                                iface_names.add(sect_name)
                            if ifname_val and ifname_val != iface_name:
                                iface_names.add(ifname_val)
            except Exception as e:
                _LOGGER.debug("UCI wireless fallback failed via SSH: %s", e)

        # 3. Populate metrics via ubus iwinfo
        for wifi in interfaces:
            iface_name = wifi.name
            try:
                # Get basic info
                info_str = await self._exec(
                    f'ubus call iwinfo info \'{{"device":"{iface_name}"}}\' 2>/dev/null'
                )
                if info_str and info_str.strip().startswith("{"):
                    info = json.loads(info_str)
                    if not wifi.ssid:
                        wifi.ssid = info.get("ssid", "")
                    wifi.mac_address = info.get("bssid", "").upper()
                    wifi.channel = info.get("channel", 0)
                    wifi.frequency = str(info.get("frequency", ""))
                    # Re-resolve band from frequency if not already set
                    if not wifi.band and wifi.frequency:
                        wifi.band = WirelessInterface._band_from_raw(wifi.frequency)
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
                    assoc_str = await self._exec(
                        f'ubus call iwinfo assoclist \'{{"device":"{iface_name}"}}\' 2>/dev/null'
                    )
                    if assoc_str and assoc_str.strip().startswith("{"):
                        assoc = json.loads(assoc_str).get("results", [])
                        wifi.clients_count = len(assoc)

                    if not wifi.clients_count:
                        with contextlib.suppress(Exception):
                            hostapd_clients = await self._exec(
                                f"ubus call hostapd.{iface_name} get_clients 2>/dev/null"
                            )
                            if hostapd_clients and hostapd_clients.strip().startswith(
                                "{"
                            ):
                                hc = json.loads(hostapd_clients).get("clients", {})
                                wifi.clients_count = len(hc)
            except Exception as err:
                _LOGGER.debug(
                    "Failed to get iwinfo for %s via SSH: %s", iface_name, err
                )

        return interfaces

    async def get_upnp_mappings(self) -> list[UpnpMapping]:
        """Get active UPnP/NAT-PMP port mappings via SSH."""
        mappings: list[UpnpMapping] = []
        try:
            stdout = await self._exec("ubus call upnp get_mappings 2>/dev/null")
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
            _LOGGER.debug("Failed to fetch UPnP mappings via SSH: %s", err)

        return mappings

    async def get_wireguard_interfaces(self) -> list[WireGuardInterface]:
        """Get WireGuard VPN interface and peer information via SSH."""
        interfaces: list[WireGuardInterface] = []
        try:
            # 1. Discover WG interfaces via ubus call
            status_str = await self._exec(
                "ubus call network.interface dump 2>/dev/null"
            )
            if not status_str or not status_str.strip().startswith("{"):
                return interfaces

            status = json.loads(status_str)
            wg_ifaces: dict[str, bool] = {}
            for iface_data in status.get("interface", []):
                if iface_data.get("proto") == "wireguard":
                    wg_ifaces[iface_data.get("interface")] = bool(iface_data.get("up"))

            if not wg_ifaces:
                return interfaces

            # 2. Fetch peer info via wg show all dump
            stdout = await self._exec("wg show all dump 2>/dev/null")
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
                        enabled=wg_ifaces[ifname],
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
            _LOGGER.debug("Failed to fetch WireGuard interfaces via SSH: %s", err)

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
        except Exception:  # noqa: BLE001
            pass

        return interfaces

    async def get_connected_devices(self) -> list[ConnectedDevice]:
        """Get connected devices by combining DHCP, ARP and wireless station info."""
        devices: dict[str, ConnectedDevice] = {}

        # 1. DHCP Leases
        await self._add_dhcp_devices_ssh(devices)

        # 2. IP Neighbors
        await self._add_neighbor_devices_ssh(devices)

        # 3. Wireless Clients (iwinfo station dump)
        await self._add_wireless_devices_iwinfo_ssh(devices)

        # 4. Fallback to wireless clients via ubus (hostapd)
        if not any(d.is_wireless for d in devices.values()):
            await self._add_wireless_devices_ubus_ssh(devices)

        # 4. Supplemental source: Bridge FDB (Forwarding Database)
        await self._process_bridge_fdb(devices)

        return list(devices.values())

    async def _process_bridge_fdb(self, devices: dict[str, ConnectedDevice]) -> None:
        """Fetch and merge bridge FDB (forwarding database) information via SSH."""
        try:
            # 1. Fetch all network devices
            dev_status_str = await self._exec(
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
                    fdb_str = await self._exec(
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
            _LOGGER.debug("Failed to fetch bridge FDB via SSH: %s", err)

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
                        vsz=int(parts[vsz_idx].rstrip("mGk"))
                        if parts[vsz_idx].rstrip("mGk").isdigit()
                        else 0,
                        cpu_usage=float(parts[cpu_idx].rstrip("%")),
                        command=" ".join(parts[cmd_idx:]),
                    )
                )
            except ValueError, IndexError:
                continue

            if len(resources.top_processes) >= 10:
                break

    async def _add_dhcp_devices_ssh(self, devices: dict[str, ConnectedDevice]) -> None:
        """Add devices discovered via DHCP leases."""
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
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("DHCP device discovery failed (SSH): %s", err)

    async def _add_neighbor_devices_ssh(
        self, devices: dict[str, ConnectedDevice]
    ) -> None:
        """Add or update devices discovered via IP neighbors (ARP)."""
        try:
            neighbors = await self.get_ip_neighbors()
            for neigh in neighbors:
                mac = neigh.mac.lower()
                if not mac:
                    continue
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
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Neighbor device discovery failed (SSH): %s", err)

    async def _add_wireless_devices_iwinfo_ssh(
        self, devices: dict[str, ConnectedDevice]
    ) -> None:
        """Add or update wireless devices via iwinfo ubus calls."""
        if self.packages.wireless is False:
            return
        try:
            # Use get_wireless_interfaces to find active interfaces
            wireless_ifaces = await self.get_wireless_interfaces()
            for wifi_iface in wireless_ifaces:
                iface_name = wifi_iface.name
                # Use ubus call for JSON output over SSH
                assoc_str = await self._exec(
                    f'ubus call iwinfo assoclist \'{{"device":"{iface_name}"}}\' 2>/dev/null'
                )
                if assoc_str and assoc_str.strip().startswith("{"):
                    assoc = json.loads(assoc_str).get("results", [])
                    for client in assoc:
                        mac = client.get("mac", "").lower()
                        if not mac:
                            continue

                        dev = devices.setdefault(
                            mac, ConnectedDevice(mac=mac, connected=True)
                        )
                        dev.connected = True
                        dev.is_wireless = True
                        dev.interface = iface_name
                        dev.signal = client.get("signal", 0)

                        # Set connection type based on interface frequency/name
                        if "5g" in iface_name.lower() or (
                            wifi_iface.frequency and "5" in wifi_iface.frequency
                        ):
                            dev.connection_type = "5GHz"
                        elif "6g" in iface_name.lower() or (
                            wifi_iface.frequency and "6" in wifi_iface.frequency
                        ):
                            dev.connection_type = "6GHz"
                        elif "2g" in iface_name.lower() or (
                            wifi_iface.frequency and "2" in wifi_iface.frequency
                        ):
                            dev.connection_type = "2.4GHz"
                        else:
                            dev.connection_type = "wireless"
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("iwinfo wireless discovery failed (SSH): %s", err)

    async def _add_wireless_devices_ubus_ssh(
        self, devices: dict[str, ConnectedDevice]
    ) -> None:
        """Add or update wireless devices via ubus hostapd."""
        if self.packages.wireless is False:
            return
        try:
            cmd = "for obj in $(ubus list 'hostapd.*'); do echo \"$obj $(ubus call $obj get_clients)\"; done"
            stdout = await self._exec(cmd)
            for line in stdout.splitlines():
                if not (line := line.strip()):
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
                            mac_lower = mac.lower()
                            dev = devices.setdefault(
                                mac_lower,
                                ConnectedDevice(mac=mac_lower, connected=True),
                            )
                            dev.is_wireless = True
                            dev.interface = iface_name
                            dev.signal = info.get("signal", 0)
                            dev.connection_type = (
                                "5GHz"
                                if "5g" in iface_name.lower()
                                else (
                                    "2.4GHz"
                                    if "2g" in iface_name.lower()
                                    else "wireless"
                                )
                            )
                except json.JSONDecodeError, KeyError:
                    continue
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("ubus hostapd discovery failed (SSH): %s", err)

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

    async def get_services(self) -> list[ServiceInfo]:
        """Get init.d services."""
        services: list[ServiceInfo] = []

        ls_output = await self._exec("ls /etc/init.d/ 2>/dev/null")
        for svc_name in ls_output.strip().split("\n"):
            svc_name = svc_name.strip()
            if not svc_name:
                continue
            enabled = False
            running = False
            try:
                enabled_check = await self._exec(
                    f"/etc/init.d/{svc_name} enabled && echo yes || echo no",
                )
                enabled = "yes" in enabled_check
                running_check = await self._exec(
                    f"/etc/init.d/{svc_name} running && echo yes || echo no",
                )
                running = "yes" in running_check

                # Special handling for one-shot services that might be active but not "running"
                if (
                    not running
                    and svc_name in ("adblock", "simple-adblock", "sysctl")
                    and enabled
                ):
                    # For adblock, if ubus status says enabled, we consider it running
                    # but we don't want to duplicate too much logic here, so we just
                    # trust the 'enabled' state for these specific one-shot services
                    # if they are enabled at boot and it's a known one-shot service.
                    running = True
            except Exception:  # noqa: BLE001
                pass
            services.append(
                ServiceInfo(name=svc_name, enabled=enabled, running=running),
            )
        return services

    async def reboot(self) -> bool:
        """Reboot the device."""
        try:
            await self._exec("reboot")
            return True
        except Exception as err:
            _LOGGER.exception("Failed to reboot: %s", err)
            return False

    async def set_wireless_enabled(self, interface: str, enabled: bool) -> bool:
        """Enable/disable a wireless interface."""
        try:
            action = "0" if enabled else "1"
            await self._exec(f"uci set wireless.{interface}.disabled='{action}'")
            await self._exec("uci commit wireless")
            await self._exec("wifi reload")
            self._last_full_poll = 0
            return True
        except Exception as err:
            _LOGGER.exception("Failed to set wireless %s: %s", interface, err)
            return False

    async def get_leds(self) -> list:
        """Get LEDs from /sys/class/leds."""
        from .base import LedInfo

        leds: list[LedInfo] = []
        output = await self._exec(
            "for led in /sys/class/leds/*/; do "
            'name=$(basename "$led"); '
            'brightness=$(cat "$led/brightness" 2>/dev/null || echo 0); '
            'max=$(cat "$led/max_brightness" 2>/dev/null || echo 255); '
            'trigger=$(cat "$led/trigger" 2>/dev/null | tr " " "\\n" | grep "^\\[" | tr -d "[]" || echo none); '
            'echo "$name|$brightness|$max|$trigger"; '
            "done",
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
                    ),
                )
        return leds

    async def set_led(self, name: str, brightness: int) -> bool:
        """Set LED via SSH."""
        try:
            # First ensure trigger is set to none to allow manual control
            await self._exec(f"echo none > /sys/class/leds/{name}/trigger 2>/dev/null")
            # Write brightness
            await self._exec(
                f"echo {int(brightness)} > /sys/class/leds/{name}/brightness"
            )
            self._last_full_poll = 0
            return True
        except Exception as err:
            _LOGGER.exception("Failed to set LED %s: %s", name, err)
            return False

    async def check_permissions(self) -> OpenWrtPermissions:
        """Check user permissions via SSH.

        SSH access generally provides full root access, but we try to
        verify if common commands work to be safe.
        """
        from .base import OpenWrtPermissions

        perms = OpenWrtPermissions()
        is_root = self.username == "root"

        try:
            # Root always has full permissions
            if is_root:
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
                perms.write_system = True
                perms.write_network = True
                perms.write_firewall = True
                perms.write_wireless = True
                perms.write_sqm = True
                perms.write_led = True
                perms.write_vpn = True
                perms.write_access_control = True
                perms.write_devices = True
                perms.write_services = True
                return perms

            # 1. Check UCI read access (very common baseline for non-root)
            uci_check = await self._exec("uci show system 2>/dev/null | head -n 1")
            if uci_check.strip():
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

            # 2. Check UBUS access for critical features
            ubus_list = await self._exec("ubus list 2>/dev/null")
            if "network.wireless" in ubus_list or "iwinfo" in ubus_list:
                perms.read_wireless = True
            elif not perms.read_wireless:
                # If UCI and UBUS both fail for wireless, we might still have iwinfo CLI
                iwinfo_check = await self._exec("iwinfo 2>/dev/null")
                if "ESSID" in iwinfo_check:
                    perms.read_wireless = True

            # 3. Write permissions
            if is_root:
                perms.write_system = True
                perms.write_network = True
                perms.write_firewall = True
                perms.write_wireless = True
                perms.write_sqm = True
                perms.write_led = True
                perms.write_devices = True
                perms.write_services = True
                perms.write_access_control = True
                perms.write_vpn = True
            else:
                # Test write access with a dummy UCI change (without commit)
                try:
                    write_check = await self._exec(
                        "uci set system.@system[0].ha_test='1' 2>/dev/null && echo 1"
                    )
                    if write_check.strip() == "1":
                        perms.write_system = True
                        # Assume others follow if system is writable
                        perms.write_network = True
                        perms.write_firewall = True
                except Exception:
                    pass
        except Exception:
            if is_root:
                # Fallback for root if probes fail
                perms.read_system = True
                perms.read_network = True
                perms.write_system = True
                perms.write_network = True
        return perms

    async def check_packages(self) -> OpenWrtPackages:
        """Check installed packages via SSH probes."""
        packages = OpenWrtPackages()
        try:
            # Step 1: Check existence of binaries or init scripts
            await self._check_packages_from_files(packages)

            # Step 2: Fallback to full list check
            await self._check_packages_from_opkg(packages)

        except Exception as err:
            _LOGGER.debug("Package check failed via SSH: %s", err)

        self._ensure_all_packages_initialized(packages)
        return packages

    async def _check_packages_from_files(self, packages: OpenWrtPackages) -> None:
        """Identify packages by probing filesystem for binaries or scripts via SSH."""
        cmd = (
            "for f in /etc/init.d/sqm /etc/init.d/mwan3 /usr/bin/iwinfo "
            "/usr/bin/etherwake /usr/bin/wg /usr/sbin/openvpn "
            "/usr/lib/lua/luci/controller/rpc.lua "
            "/usr/share/luci/menu.d/luci-mod-rpc.json "
            "/usr/lib/lua/luci/controller/attendedsysupgrade.lua "
            "/usr/share/luci/menu.d/luci-app-attendedsysupgrade.json "
            "/etc/init.d/adblock /etc/init.d/simple-adblock /etc/init.d/ban-ip /etc/init.d/miniupnpd /etc/init.d/nlbwmon /etc/init.d/pbr /etc/init.d/adguardhome /etc/init.d/unbound /usr/lib/rpcd/led.so /etc/config/sqm /etc/init.d/odhcpd /etc/init.d/lldpd; do "
            "if [ -f $f ] || [ -x $f ]; then echo 1; else echo 0; fi; done"
        )
        out = await self._exec(cmd)
        results = out.strip().splitlines()

        def detect(idx: int) -> bool:
            return len(results) > idx and results[idx].strip() == "1"

        packages.sqm_scripts = detect(0) or detect(19)
        packages.mwan3 = detect(1)
        packages.iwinfo = detect(2)
        packages.etherwake = detect(3)
        packages.wireguard = detect(4)
        packages.openvpn = detect(5)
        packages.luci_mod_rpc = detect(6) or detect(7)
        packages.asu = detect(8) or detect(9)
        packages.adblock = detect(10)
        packages.simple_adblock = detect(11)
        packages.ban_ip = detect(12)
        packages.miniupnpd = detect(13)
        packages.nlbwmon = detect(14)
        packages.pbr = detect(15)
        packages.adguardhome = detect(16)
        packages.unbound = detect(17)

        packages.dhcp = detect(20)
        if packages.dhcp:
            # Specifically check for ipv4leases method
            dhcp_check = await self._exec("ubus list dhcp")
            if "ipv4leases" not in dhcp_check:
                packages.dhcp = False
        packages.lldp = detect(21)

        # Detect wireless via presence of iwinfo or ubus network.wireless
        if packages.iwinfo:
            packages.wireless = True
        else:
            # Last ditch check for wireless
            wifi_check = await self._exec("ubus list network.wireless")
            if wifi_check and "network.wireless" in wifi_check:
                packages.wireless = True

    async def _check_packages_from_opkg(self, packages: OpenWrtPackages) -> None:
        """Identify packages by checking the full installed package list."""
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
            "dhcp": "odhcpd",
            "lldp": "lldpd",
            "wireless": "iwinfo",
        }
        for attr, pkg_name in mapping.items():
            if getattr(packages, attr) is not True:
                if pkg_name in ("wireguard", "openvpn"):
                    setattr(packages, attr, any(pkg_name in p for p in installed))
                else:
                    setattr(packages, attr, pkg_name in installed)

    def _ensure_all_packages_initialized(self, packages: OpenWrtPackages) -> None:
        """Ensure no package attributes remain as None (default to False)."""
        import dataclasses

        for field in dataclasses.fields(packages):
            if getattr(packages, field.name) is None:
                setattr(packages, field.name, False)

    async def get_firewall_rules(self) -> list[FirewallRule]:
        """Get firewall rules via UCI over SSH."""
        from .base import FirewallRule

        rules: list[FirewallRule] = []
        output = await self._exec("uci show firewall")
        sections: dict[str, dict[str, str]] = {}
        for line in output.splitlines():
            if "=" not in line:
                continue
            key, val = line.split("=", 1)
            parts = key.split(".")
            if len(parts) == 2:
                section = parts[1]
                if section not in sections:
                    sections[section] = {}
                sections[section][".type"] = val.strip("'")
            elif len(parts) >= 3:
                section = parts[1]
                if section not in sections:
                    sections[section] = {}
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
                    ),
                )
        return rules

    async def set_firewall_rule_enabled(self, section_id: str, enabled: bool) -> bool:
        """Enable or disable a firewall rule via UCI over SSH."""
        try:
            val = "1" if enabled else "0"
            await self._exec(f"uci set firewall.{section_id}.enabled='{val}'")
            await self._exec("uci commit firewall")
            await self._exec("/etc/init.d/firewall reload")
            self._last_full_poll = 0
            return True
        except Exception as err:
            _LOGGER.exception("Failed to set firewall rule via SSH: %s", err)
            return False

    async def get_firewall_redirects(self) -> list[FirewallRedirect]:
        """Get firewall port forwarding redirects via UCI over SSH."""
        redirects: list[FirewallRedirect] = []
        output = await self._exec("uci show firewall")
        sections: dict[str, dict[str, str]] = {}
        for line in output.splitlines():
            if "=" not in line:
                continue
            key, val = line.split("=", 1)
            parts = key.split(".")
            if len(parts) == 2:
                section = parts[1]
                if section not in sections:
                    sections[section] = {}
                sections[section][".type"] = val.strip("'")
            elif len(parts) >= 3:
                section = parts[1]
                if section not in sections:
                    sections[section] = {}
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
                    ),
                )
        return redirects

    async def set_firewall_redirect_enabled(
        self,
        section_id: str,
        enabled: bool,
    ) -> bool:
        """Enable or disable a firewall redirect via UCI over SSH."""
        try:
            val = "1" if enabled else "0"
            await self._exec(f"uci set firewall.{section_id}.enabled='{val}'")
            await self._exec("uci commit firewall")
            await self._exec("/etc/init.d/firewall reload")
            self._last_full_poll = 0
            return True
        except Exception as err:
            _LOGGER.exception("Failed to set firewall redirect via SSH: %s", err)
            return False

    async def get_access_control(self) -> list[AccessControl]:
        """Get access control rules via UCI firewall rules over SSH."""
        rules: list[AccessControl] = []
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
                    ),
                )
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
                        f"uci set firewall.{section_id}.name='{rule_name}'",
                    )
                    await self._exec(f"uci set firewall.{section_id}.src='lan'")
                    await self._exec(f"uci set firewall.{section_id}.dest='wan'")
                    await self._exec(
                        f"uci set firewall.{section_id}.src_mac='{mac_upper}'",
                    )
                    await self._exec(f"uci set firewall.{section_id}.target='REJECT'")

                await self._exec(f"uci set firewall.{section_id}.enabled='1'")
            elif section_id:
                await self._exec(f"uci set firewall.{section_id}.enabled='0'")

            await self._exec("uci commit firewall")
            await self._exec("/etc/init.d/firewall reload")
            self._last_full_poll = 0
            return True
        except Exception as err:
            _LOGGER.exception("Failed to set access control via SSH: %s", err)
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
        except Exception as err:
            _LOGGER.exception("Failed to manage interface %s: %s", name, err)
            return False

    async def install_firmware(self, url: str, keep_settings: bool = True) -> None:
        """Install firmware from the given URL via SSH."""
        # Use sysupgrade for installation
        # Download to /tmp and then run sysupgrade
        keep = "" if keep_settings else "-n"
        cmd = (
            f"wget -O /tmp/firmware.bin '{url}' && sysupgrade {keep} /tmp/firmware.bin"
        )
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
                    "SSH connection lost during sysupgrade - device is likely rebooting",
                )
                return
            _LOGGER.warning(
                "Sysupgrade command might have failed or disconnected: %s",
                err,
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
            _LOGGER.exception("Failed to download file via SSH: %s", err)
        return False

    async def get_dhcp_leases(self) -> list[DhcpLease]:
        """Get DHCP leases via SSH."""
        if self.dhcp_software == "none":
            return []

        leases: list[DhcpLease] = []

        # 1. Try odhcpd via ubus
        if self.dhcp_software in ("auto", "odhcpd") and self.packages.dhcp is not False:
            await self._get_leases_odhcpd(leases)
            if leases and self.dhcp_software == "odhcpd":
                return leases

        # 2. Try dnsmasq via file
        if (
            self.dhcp_software in ("auto", "dnsmasq")
            and self.packages.dhcp is not False
        ):
            await self._get_leases_dnsmasq(leases)

        return leases

    async def _get_leases_odhcpd(self, leases: list[DhcpLease]) -> None:
        """Fetch DHCP leases from odhcpd via ubus over SSH."""
        with contextlib.suppress(Exception):
            # IPv4
            stdout = await self._exec("ubus call dhcp ipv4leases 2>/dev/null")
            if stdout and stdout.strip().startswith("{"):
                data = json.loads(stdout)
                for lease_data in data.get("device", {}).values():
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

            # IPv6
            stdout_v6 = await self._exec("ubus call dhcp ipv6leases 2>/dev/null")
            if stdout_v6 and stdout_v6.strip().startswith("{"):
                data_v6 = json.loads(stdout_v6)
                for lease_data in data_v6.get("device", {}).values():
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

    async def _get_leases_dnsmasq(self, leases: list[DhcpLease]) -> None:
        """Fetch DHCP leases from dnsmasq lease file via SSH."""
        with contextlib.suppress(Exception):
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
                        ),
                    )

    async def get_local_macs(self) -> set[str]:
        """Get all MAC addresses belonging to the router's physical and virtual interfaces."""
        macs = set()
        try:
            # Use 'ip link show' which is very reliable
            stdout = await self._exec("ip link show 2>/dev/null")
            if stdout:
                # Find lines like 'link/ether 00:11:22:33:44:55 ...'
                matches = re.finditer(r"link/ether\s+([0-9a-fA-F:]{17})", stdout)
                for match in matches:
                    macs.add(match.group(1).lower())
        except Exception:  # noqa: BLE001
            pass
        return macs

    async def get_local_ips(self) -> set[str]:
        """Get all IP addresses belonging to the router."""
        ips = set()
        try:
            # Use 'ip addr show'
            stdout = await self._exec("ip addr show 2>/dev/null")
            if stdout:
                # Find IPv4 and IPv6 addresses
                ipv4_matches = re.finditer(r"inet\s+([0-9.]+)/", stdout)
                for match in ipv4_matches:
                    ips.add(match.group(1))
                ipv6_matches = re.finditer(r"inet6\s+([0-9a-fA-F:]+)/", stdout)
                for match in ipv6_matches:
                    ips.add(match.group(1))
        except Exception:  # noqa: BLE001
            pass
        return ips

    async def get_adblock_status(self) -> AdBlockStatus:
        """Get adblock status via SSH."""
        from .base import AdBlockStatus

        status = AdBlockStatus()
        # 1. Try ubus first (provides more details)
        try:
            out = await self._exec("ubus call adblock status 2>/dev/null")
            if out:
                try:
                    res = json.loads(out)
                    if res and isinstance(res, dict) and res.get("adblock_status"):
                        status.enabled = res.get("adblock_status") == "enabled"
                        status.status = res.get("adblock_status", "disabled")
                        status.version = res.get("adblock_version")
                        # Handle formatted numbers like "57,861" or "57.861"
                        blocked = (
                            str(res.get("blocked_domains", 0))
                            .replace(",", "")
                            .replace(".", "")
                        )
                        try:
                            status.blocked_domains = int(float(blocked))
                        except ValueError, TypeError:
                            pass
                        status.last_update = res.get("last_run")
                        return status
                except json.JSONDecodeError:
                    pass
        except Exception as err:
            _LOGGER.debug("AdBlock ubus status failed (SSH): %s", err)

        # 2. Fallback to uci (basic status)
        try:
            enabled = await self._exec("uci -q get adblock.global.enabled")
            status.enabled = (enabled or "").strip() == "1"
            status.status = "enabled" if status.enabled else "disabled"
        except Exception as err:
            _LOGGER.debug("AdBlock UCI status failed (SSH): %s", err)

        return status

    async def get_adblock_fast_status(self) -> SimpleAdBlockStatus:
        """Get adblock-fast status via SSH."""
        from .base import SimpleAdBlockStatus

        status = SimpleAdBlockStatus()
        try:
            # Fallback to uci
            res = await self._exec("uci -q get adblock-fast.config.enabled")
            status.enabled = res.strip() == "1"
            status.status = "enabled" if status.enabled else "disabled"
            count = await self._exec("wc -l < /tmp/adblock-fast.blocked 2>/dev/null")
            if count and count.strip().isdigit():
                status.blocked_domains = int(count.strip())
        except Exception:
            pass
        return status

    async def set_adblock_fast_enabled(self, enabled: bool) -> bool:
        """Enable/disable adblock-fast service via SSH."""
        val = "1" if enabled else "0"
        try:
            await self._exec(
                f"uci set adblock-fast.config.enabled='{val}' && uci commit adblock-fast",
            )
            action = "start" if enabled else "stop"
            await self._exec(f"/etc/init.d/adblock-fast {action}")
            self._last_full_poll = 0
            return True
        except Exception:
            return False

    async def manage_service(self, name: str, action: str) -> bool:
        """Manage a system service (start/stop/restart/enable/disable) via SSH."""
        try:
            await self._exec(f"/etc/init.d/{name} {action}")
            self._last_full_poll = 0
            return True
        except Exception as err:
            _LOGGER.exception(
                "Failed to manage service %s (%s) via SSH: %s",
                name,
                action,
                err,
            )
            return False

    async def set_adblock_enabled(self, enabled: bool) -> bool:
        """Enable/disable adblock service via SSH."""
        val = "1" if enabled else "0"
        try:
            await self._exec(
                f"uci set adblock.global.enabled='{val}' && uci commit adblock",
            )
            action = "start" if enabled else "stop"
            await self._exec(f"/etc/init.d/adblock {action}")
            self._last_full_poll = 0
            return True
        except Exception:
            return False

    async def get_simple_adblock_status(self) -> SimpleAdBlockStatus:
        """Get simple-adblock status via SSH."""
        from .base import SimpleAdBlockStatus

        status = SimpleAdBlockStatus()
        try:
            res = await self._exec("uci -q get simple-adblock.config.enabled")
            status.enabled = res.strip() == "1"
            status.status = "enabled" if status.enabled else "disabled"
            count = await self._exec("wc -l < /tmp/simple-adblock.blocked 2>/dev/null")
            if count and count.strip().isdigit():
                status.blocked_domains = int(count.strip())
        except Exception:
            pass
        return status

    async def set_simple_adblock_enabled(self, enabled: bool) -> bool:
        """Enable/disable simple-adblock service via SSH."""
        val = "1" if enabled else "0"
        try:
            await self._exec(
                f"uci set simple-adblock.config.enabled='{val}' && uci commit simple-adblock",
            )
            action = "start" if enabled else "stop"
            await self._exec(f"/etc/init.d/simple-adblock {action}")
            self._last_full_poll = 0
            return True
        except Exception:
            return False

    async def get_banip_status(self) -> BanIpStatus:
        """Get ban-ip status via SSH."""
        from .base import BanIpStatus

        status = BanIpStatus()
        try:
            res = await self._exec("uci -q get ban-ip.config.enabled")
            status.enabled = res.strip() == "1"
            status.status = "enabled" if status.enabled else "disabled"
        except Exception:
            pass
        return status

    async def set_banip_enabled(self, enabled: bool) -> bool:
        """Enable/disable ban-ip service via SSH."""
        val = "1" if enabled else "0"
        try:
            await self._exec(
                f"uci set ban-ip.config.enabled='{val}' && uci commit ban-ip",
            )
            action = "start" if enabled else "stop"
            await self._exec(f"/etc/init.d/ban-ip {action}")
            self._last_full_poll = 0
            return True
        except Exception:
            return False

    async def get_sqm_status(self) -> list[SqmStatus]:
        """Get SQM status via SSH."""
        from .base import SqmStatus

        sqm_instances: list[SqmStatus] = []
        try:
            output = await self._exec("uci show sqm")
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
                        sections[section][parts[2]] = val.strip("'").strip('"')
                    else:
                        sections[section][".type"] = val.strip("'").strip('"')

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
                        ),
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
            self._last_full_poll = 0
            return True
        except Exception as err:
            _LOGGER.exception("Failed to set SQM config via SSH: %s", err)
            return False

    async def get_lldp_neighbors(self) -> list[LldpNeighbor]:
        """Get LLDP neighbor information via SSH."""
        neighbors: list[LldpNeighbor] = []

        try:
            # Method 1: ubus (preferred)
            await self._get_lldp_from_ubus(neighbors)
            if neighbors:
                return neighbors

            # Method 2: lldpcli
            await self._get_lldp_from_lldpcli(neighbors)

        except Exception as err:
            _LOGGER.debug("Failed to get LLDP neighbors via SSH: %s", err)
        return neighbors

    async def perform_diagnostics(self) -> list[DiagnosticResult]:
        """Perform SSH-specific diagnostic checks."""
        results: list[DiagnosticResult] = []

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
                    message=", ".join(msg)
                    if msg
                    else "No package manager (apk/opkg) detected. Firmware may be restricted or custom.",
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

    async def _get_lldp_from_ubus(self, neighbors: list[LldpNeighbor]) -> None:
        """Fetch LLDP neighbors from 'lldp show' ubus call via SSH."""
        if self.packages.lldp is False:
            return
        with contextlib.suppress(Exception):
            stdout = await self._exec("ubus call lldp show 2>/dev/null")
            if stdout and stdout.strip().startswith("{"):
                data = json.loads(stdout)
                interfaces = data.get("lldp", {}).get("interface", [])
                if isinstance(interfaces, list):
                    for iface in interfaces:
                        name = iface.get("name")
                        for neigh in iface.get("neighbor", []):
                            neighbors.append(
                                self._parse_ubus_lldp_neigh(name or "", neigh)
                            )

    def _parse_ubus_lldp_neigh(
        self, local_iface: str, neigh: dict[str, Any]
    ) -> LldpNeighbor:
        """Parse a single LLDP neighbor entry from ubus output."""
        from .base import LldpNeighbor

        return LldpNeighbor(
            local_interface=local_iface,
            neighbor_name=neigh.get("name", ""),
            neighbor_port=(
                neigh.get("port", {}).get("id", "")
                if isinstance(neigh.get("port"), dict)
                else ""
            ),
            neighbor_chassis=(
                neigh.get("chassis", {}).get("id", "")
                if isinstance(neigh.get("chassis"), dict)
                else ""
            ),
            neighbor_description=neigh.get("description", ""),
            neighbor_system_name=neigh.get("sysname", ""),
        )

    async def _get_lldp_from_lldpcli(self, neighbors: list[LldpNeighbor]) -> None:
        """Fetch LLDP neighbors using 'lldpcli show neighbors' via SSH."""
        with contextlib.suppress(Exception):
            stdout = await self._exec("lldpcli show neighbors -f json 2>/dev/null")
            if stdout and stdout.strip().startswith("{"):
                data = json.loads(stdout)
                interfaces = data.get("lldp", {}).get("interface", {})
                if isinstance(interfaces, dict):
                    for iface_name, iface_data in interfaces.items():
                        neighs = iface_data.get("neighbor", [])
                        if isinstance(neighs, dict):
                            neighs = [neighs]
                        for neigh in neighs if isinstance(neighs, list) else []:
                            neighbors.append(
                                self._parse_lldpcli_neigh(iface_name, neigh)
                            )

    def _parse_lldpcli_neigh(
        self, local_iface: str, neigh: dict[str, Any]
    ) -> LldpNeighbor:
        """Parse a single LLDP neighbor entry from lldpcli JSON output."""
        from .base import LldpNeighbor

        return LldpNeighbor(
            local_interface=local_iface,
            neighbor_name=neigh.get("name", ""),
            neighbor_port=(
                neigh.get("port", {}).get("id", {}).get("value", "")
                if isinstance(neigh.get("port"), dict)
                else ""
            ),
            neighbor_chassis=(
                neigh.get("chassis", {}).get("id", {}).get("value", "")
                if isinstance(neigh.get("chassis"), dict)
                else ""
            ),
            neighbor_description=neigh.get("description", ""),
            neighbor_system_name=neigh.get("sysname", ""),
        )

    async def get_nlbwmon_data(self) -> dict[str, NlbwmonTraffic]:
        """Get bandwidth usage per MAC from nlbwmon via SSH."""
        try:
            out = await self._exec("nlbw -c json -g mac")
            if not out:
                return {}

            import json

            result = json.loads(out)
            if not result or "data" not in result:
                return {}

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
            _LOGGER.debug("Failed to get nlbwmon data via SSH: %s", err)
            return {}

    async def get_wifi_credentials(self) -> list[WifiCredentials]:
        """Get wifi credentials via SSH."""
        try:
            # Try ubus first
            stdout = await self._exec(
                'ubus call uci get \'{"config":"wireless"}\' 2>/dev/null'
            )
            if stdout and stdout.startswith("{"):
                data = json.loads(stdout)
                creds = []
                for name, val in data.get("values", {}).items():
                    if val.get(".type") == "wifi-iface" and val.get("mode") == "ap":
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

            # Fallback to uci export
            stdout = await self._exec("uci export wireless 2>/dev/null")
            if not stdout:
                return []

            creds = []
            current_iface = None
            ssid = None
            key = None
            enc = None
            hidden = False

            for line in stdout.splitlines():
                line = line.strip()
                if line.startswith("config wifi-iface"):
                    if ssid:
                        creds.append(
                            WifiCredentials(
                                iface=current_iface or "",
                                ssid=ssid,
                                encryption=enc or "none",
                                key=key or "",
                                hidden=hidden,
                            )
                        )
                    parts = line.split()
                    current_iface = (
                        parts[-1].strip("'") if len(parts) > 2 else "unknown"
                    )
                    ssid = None
                    key = None
                    enc = None
                    hidden = False
                elif line.startswith("option ssid"):
                    parts = line.split("'")
                    if len(parts) > 1:
                        ssid = parts[1]
                elif line.startswith("option key"):
                    parts = line.split("'")
                    if len(parts) > 1:
                        key = parts[1]
                elif line.startswith("option encryption"):
                    parts = line.split("'")
                    if len(parts) > 1:
                        enc = parts[1]
                elif line.startswith("option hidden"):
                    parts = line.split("'")
                    if len(parts) > 1:
                        hidden = parts[1] == "1"

            if ssid:
                creds.append(
                    WifiCredentials(
                        iface=current_iface or "",
                        ssid=ssid,
                        encryption=enc or "none",
                        key=key or "",
                        hidden=hidden,
                    )
                )

            return creds
        except Exception as err:
            _LOGGER.debug("Failed to get wifi credentials via ssh: %s", err)
            return []

    async def get_mwan_status(self) -> list[MwanStatus]:
        """Get multi-wan status via SSH."""
        try:
            stdout = await self._exec("ubus call mwan3 status 2>/dev/null")
            if not stdout or not stdout.startswith("{"):
                return []

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
        except Exception as err:
            _LOGGER.debug("Failed to get mwan3 status via ssh: %s", err)
            return []

    async def trigger_wps_push(self, interface: str) -> bool:
        """Trigger WPS push button via SSH."""
        try:
            # hostapd_cli -i wlan0 wps_push
            await self.execute_command(f"hostapd_cli -i {interface} wps_push")
            return True
        except Exception as err:
            _LOGGER.debug(
                "Failed to trigger WPS push via ssh for %s: %s", interface, err
            )
            return False

    async def is_reboot_required(self) -> bool:
        """Check if reboot is required via SSH."""
        try:
            output = await self.execute_command(
                "[ -f /tmp/.reboot-needed ] || [ -f /var/run/reboot-required ] && echo 1"
            )
            return output.strip() == "1"
        except Exception:
            return False
