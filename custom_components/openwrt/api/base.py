"""Base client interface for OpenWrt API communication."""

from __future__ import annotations

import abc
import logging
import re
from datetime import datetime
from typing import TYPE_CHECKING, Any

import aiohttp

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

from .client import (
    OpenWrtAggregatorMixin,
    OpenWrtClientHelpersMixin,
    OpenWrtMeshMixin,
    OpenWrtStatusMixin,
)
from .models import (
    AccessControl,
    AdBlockStatus,
    BanIpStatus,
    BatmanGateway,
    BatmanNeighbor,
    BatmanOriginator,
    ConnectedDevice,
    DeviceInfo,
    DhcpLease,
    DiagnosticResult,
    FirewallRedirect,
    FirewallRule,
    IpNeighbor,
    LatencyResult,
    LedInfo,
    LldpNeighbor,
    MwanPolicy,
    MwanPolicyInterface,
    MwanStatus,
    NetworkInterface,
    NlbwmonTraffic,
    OpenWrtData,
    OpenWrtPackages,
    OpenWrtPermissions,
    ProcessInfo,
    QModemInfo,
    ServiceInfo,
    SimpleAdBlockStatus,
    SqmStatus,
    StorageUsage,
    SystemResources,
    TailscalePeer,
    TailscaleStatus,
    UpnpMapping,
    UsbDevice,
    VpnInterface,
    WifiCredentials,
    WireGuardInterface,
    WireGuardPeer,
    WirelessInterface,
    WpsStatus,
    classify_service,
    parse_mwan_track_metrics,
)
from .provisioning import PROVISION_SCRIPT_TEMPLATE

__all__ = [
    "AccessControl",
    "AdBlockStatus",
    "BanIpStatus",
    "BatmanGateway",
    "BatmanNeighbor",
    "BatmanOriginator",
    "ConnectedDevice",
    "DeviceInfo",
    "DhcpLease",
    "DiagnosticResult",
    "FirewallRedirect",
    "FirewallRule",
    "IpNeighbor",
    "LatencyResult",
    "LedInfo",
    "LldpNeighbor",
    "MwanPolicy",
    "MwanPolicyInterface",
    "MwanStatus",
    "NetworkInterface",
    "NlbwmonTraffic",
    "OpenWrtClient",
    "OpenWrtData",
    "OpenWrtPackages",
    "OpenWrtPermissions",
    "PROVISION_SCRIPT_TEMPLATE",
    "ProcessInfo",
    "QModemInfo",
    "ServiceInfo",
    "SimpleAdBlockStatus",
    "SqmStatus",
    "StorageUsage",
    "SystemResources",
    "TailscalePeer",
    "TailscaleStatus",
    "UpnpMapping",
    "UsbDevice",
    "VpnInterface",
    "WifiCredentials",
    "WireGuardInterface",
    "WireGuardPeer",
    "WirelessInterface",
    "WpsStatus",
    "classify_service",
    "parse_mwan_track_metrics",
]


class OpenWrtClient(
    OpenWrtAggregatorMixin,
    OpenWrtStatusMixin,
    OpenWrtMeshMixin,
    OpenWrtClientHelpersMixin,
    abc.ABC,
):
    """Abstract base class for OpenWrt API clients."""

    def __init__(
        self,
        hass: HomeAssistant,
        session: aiohttp.ClientSession | None,
        host: str,
        username: str,
        password: str,
        port: int = 80,
        use_ssl: bool = False,
        verify_ssl: bool = False,
        dhcp_software: str = "auto",
        trust_stale_arp: bool = True,
        trust_bridge_fdb: bool = True,
    ) -> None:
        """Initialize the client."""
        self.hass = hass
        self.session = session
        self.host = host
        self.username = username
        self.password = password
        self.port = port
        self.use_ssl = use_ssl
        self.verify_ssl = verify_ssl
        self.dhcp_software = dhcp_software
        self.trust_stale_arp = trust_stale_arp
        self.trust_bridge_fdb = trust_bridge_fdb
        self._connected = False
        self._poll_count = 0
        self._cached_device_info: DeviceInfo | None = None
        self._cached_slow_data: dict[str, Any] = {}
        self._last_cpu_stats: tuple[int, int] | None = None
        self._logread_flag: str | None = None
        self.packages = OpenWrtPackages()
        self.coordinator: Any = None
        self._last_connect_error: Exception | None = None
        self._last_slow_poll_time = 0.0
        # Bumped by every invalidation, so one that lands while a slow poll
        # is in flight survives that poll's timestamp update.
        self._slow_poll_generation = 0
        self._last_medium_poll_time = 0.0
        self._cached_medium_data: dict[str, Any] = {}

    def _invalidate_slow_poll(self) -> None:
        """Make the next poll re-read the slow tier, e.g. after a router action."""
        self._last_slow_poll_time = 0.0
        self._slow_poll_generation += 1

    @property
    def connected(self) -> bool:
        """Return whether the client is connected."""
        return self._connected

    @abc.abstractmethod
    async def connect(self) -> bool:
        """Establish connection and authenticate."""
        raise NotImplementedError

    @abc.abstractmethod
    async def disconnect(self) -> None:
        """Disconnect from the device."""
        raise NotImplementedError

    @abc.abstractmethod
    async def get_device_info(self) -> DeviceInfo:
        """Get device information."""
        raise NotImplementedError

    @abc.abstractmethod
    async def get_system_resources(self) -> SystemResources:
        """Get system resource usage."""
        raise NotImplementedError

    @abc.abstractmethod
    async def check_permissions(self) -> OpenWrtPermissions:
        """Check what permissions the current user has."""
        raise NotImplementedError

    @abc.abstractmethod
    async def check_packages(self) -> OpenWrtPackages:
        """Check installed packages."""
        raise NotImplementedError

    async def user_exists(self, username: str) -> bool:
        """Check if a system user exists on the device."""
        # Try checking via /etc/passwd first as it's often more accessible
        try:
            # We use cat via execute_command, but subclasses might override this
            # to use a more direct file-read if available (e.g. ubus file.read)
            passwd = await self.execute_command("cat /etc/passwd 2>/dev/null")
            if passwd and f"{username}:" in passwd:
                return True
        except Exception:
            pass

        # Fallback to id -u
        try:
            output = await self.execute_command(f"id -u {username} 2>/dev/null")
            return output.strip().isdigit()
        except Exception:
            return False

    @abc.abstractmethod
    async def provision_user(
        self,
        username: str,
        password: str,
    ) -> tuple[bool, str | None]:
        """Create a dedicated system user and configure RPC permissions.

        Returns:
            (success, error_message)
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def get_wireless_interfaces(self) -> list[WirelessInterface]:
        """Get wireless interface information."""
        raise NotImplementedError

    async def get_lldp_neighbors(self) -> list[LldpNeighbor]:
        """Get LLDP neighbor information."""
        return []

    @abc.abstractmethod
    async def get_nlbwmon_data(self) -> dict[str, NlbwmonTraffic]:
        """Get bandwidth usage per MAC from nlbwmon."""

    @abc.abstractmethod
    async def get_wifi_credentials(self) -> list[WifiCredentials]:
        """Get Wi-Fi credentials for QR code generation."""

    @abc.abstractmethod
    async def get_local_macs(self) -> set[str]:
        """Get all MAC addresses belonging to the router's physical and virtual interfaces."""
        raise NotImplementedError

    @abc.abstractmethod
    async def get_local_ips(self) -> set[str]:
        """Get all IP addresses belonging to the router."""
        raise NotImplementedError

    @abc.abstractmethod
    async def get_upnp_mappings(self) -> list[UpnpMapping]:
        """Get active UPnP/NAT-PMP port mappings."""
        raise NotImplementedError

    @abc.abstractmethod
    async def get_wireguard_interfaces(self) -> list[WireGuardInterface]:
        """Get WireGuard VPN interface and peer information."""
        raise NotImplementedError

    @abc.abstractmethod
    async def get_network_interfaces(self) -> list[NetworkInterface]:
        """Get network interface information."""
        raise NotImplementedError

    @abc.abstractmethod
    async def get_connected_devices(self) -> list[ConnectedDevice]:
        """Get list of connected clients/devices."""
        raise NotImplementedError

    async def get_neighbors(self) -> list[dict[str, str]]:
        """Get neighbor (ARP/NDP) table entries."""
        return []

    @abc.abstractmethod
    async def get_dhcp_leases(self) -> list[DhcpLease]:
        """Get DHCP lease information."""
        raise NotImplementedError

    async def get_dnsmasq_lease_configs(self) -> list[tuple[str, str | None]]:
        """Get dnsmasq lease files and domains from UCI config."""
        return [("/tmp/dhcp.leases", None)]

    async def get_ip_neighbors(self) -> list[IpNeighbor]:
        """Get IP neighbor (ARP/NDP) table."""
        neighbors: list[IpNeighbor] = []

        # 1. Try ip neigh show
        await self._get_neighbors_ip_neigh(neighbors)

        # 2. Fallback to /proc/net/arp
        if not neighbors:
            await self._get_neighbors_proc_arp(neighbors)

        return neighbors

    async def _get_neighbors_ip_neigh(self, neighbors: list[IpNeighbor]) -> None:
        """Fetch neighbors using 'ip neigh show'."""
        try:
            content = await self.execute_command("ip neigh show 2>/dev/null")
            if content:
                for line in content.strip().split("\n"):
                    neigh = self._parse_ip_neigh_line(line)
                    if neigh:
                        neighbors.append(neigh)
        except Exception:
            pass

    @abc.abstractmethod
    async def reboot(self) -> bool:
        """Reboot the device."""
        raise NotImplementedError

    @abc.abstractmethod
    async def execute_command(self, command: str) -> str:
        """Execute a command on the device."""
        raise NotImplementedError

    async def file_exec(
        self, command: str, params: list[str] | None = None
    ) -> dict[str, Any]:
        """Execute a binary via rpcd file.exec. Returns {} if unsupported by this client."""
        return {}

    async def read_file(self, path: str) -> str | None:
        """Read a file's contents. None if unsupported by this client or on error."""
        return None

    async def _fetch_conntrack(self, resources: SystemResources) -> None:
        """Populate nf_conntrack count/max from /proc."""
        for attr, path in (
            ("conntrack_count", "/proc/sys/net/netfilter/nf_conntrack_count"),
            ("conntrack_max", "/proc/sys/net/netfilter/nf_conntrack_max"),
        ):
            data = await self.read_file(path)
            if not data:
                continue
            match = re.search(r"\d+", data)
            if match:
                setattr(resources, attr, int(match.group(0)))

    async def kick_device(self, mac_address: str, interface: str) -> bool:
        """Kick a wireless device from the network using hostapd."""
        cmd_ubus = f'ubus call hostapd.{interface} del_client \'{{"addr":"{mac_address}","reason":5,"deauth":true,"ban_time":60000}}\''
        try:
            output = await self.execute_command(cmd_ubus)
            if (
                output
                and "Method not found" not in output
                and "Not found" not in output
            ):
                return True
        except Exception:
            pass

        cmd_cli = f"hostapd_cli -i {interface} deauthenticate {mac_address}"
        try:
            output = await self.execute_command(cmd_cli)
            if output and "OK" in output:
                return True
        except Exception:
            pass

        return False

    async def get_mwan_status(self) -> tuple[list[MwanStatus], list[MwanPolicy]]:
        """Get MWAN3 status and policies (optional, may not be installed)."""
        return [], []

    async def get_wps_status(self) -> WpsStatus:
        """Get WPS status."""
        return WpsStatus()

    async def set_wps(self, enabled: bool) -> bool:
        """Enable or disable WPS."""
        return False

    async def trigger_wps_push(self, interface: str) -> bool:
        """Trigger WPS push button on a specific wireless interface."""
        return False

    async def set_led(self, name: str, brightness: int) -> bool:
        """Set LED brightness (0-255)."""
        return False

    async def get_system_logs(self, count: int = 10) -> list[str]:
        """Get recent system log entries."""
        return []

    async def get_dmesg_logs(self, count: int = 100) -> list[str]:
        """Get recent dmesg kernel log entries."""
        try:
            output = await self.execute_command(f"dmesg | tail -n {int(count or 100)}")
            if output:
                return [line.strip() for line in output.splitlines() if line.strip()]
        except Exception as err:
            _LOGGER.debug("Failed to retrieve dmesg logs: %s", err)
        return []

    async def get_upgradeable_packages(self) -> dict[str, str]:
        """Get a dictionary of upgradeable packages mapped to their latest versions."""
        try:
            script = (
                "if command -v apk >/dev/null 2>&1; then "
                "  apk list --upgradable 2>/dev/null | awk -F' ' '{print $1}'; "
                "elif command -v opkg >/dev/null 2>&1; then "
                "  opkg list-upgradable 2>/dev/null | awk '{print $1 \" \" $3}'; "
                "fi"
            )
            output = await self.execute_command(script)
            upgrades = {}
            if output:
                for line in output.strip().splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    parts = line.split()
                    if len(parts) == 1:
                        upgrades[parts[0]] = "latest"
                    elif len(parts) >= 2:
                        upgrades[parts[0]] = parts[1]
            return upgrades
        except Exception as err:
            _LOGGER.debug("Failed to check upgradeable packages: %s", err)
            return {}

    async def is_reboot_required(self) -> bool:
        """Check if the system requires a reboot."""
        return False

    async def get_services(self) -> list[ServiceInfo]:
        """Get list of system services."""
        return []

    async def manage_service(self, name: str, action: str) -> bool:
        """Manage a system service (start/stop/restart/enable/disable)."""
        return False

    async def set_wireless_enabled(self, interface: str, enabled: bool) -> bool:
        """Enable or disable a wireless interface."""
        return False

    async def set_radio_enabled(self, radio: str, enabled: bool) -> bool:
        """Physically enable or disable a wireless radio."""
        return await self.set_wireless_enabled(radio, enabled)

    async def set_wireless_network_enabled(
        self,
        interface: str,
        radio: str,
        enabled: bool,
        *,
        disable_radio: bool,
        ssid: str = "",
    ) -> bool:
        """Set an SSID and coordinate its physical radio."""
        if enabled and not await self.set_radio_enabled(radio, True):
            return False
        if not await self.set_wireless_enabled(interface, enabled):
            return False
        if disable_radio:
            return await self.set_radio_enabled(radio, False)
        return True

    async def manage_interface(self, name: str, action: str) -> bool:
        """Manage a network interface (up/down/reconnect)."""
        return False

    async def get_firewall_redirects(self) -> list[FirewallRedirect]:
        """Get firewall port forwarding redirects."""
        return []

    async def set_firewall_redirect_enabled(
        self,
        section_id: str,
        enabled: bool,
    ) -> bool:
        """Enable or disable a firewall redirect."""
        return False

    @abc.abstractmethod
    async def get_firewall_rules(self) -> list[FirewallRule]:
        """Get firewall rules."""
        raise NotImplementedError

    @abc.abstractmethod
    async def set_firewall_rule_enabled(self, section_id: str, enabled: bool) -> bool:
        """Enable or disable a firewall rule."""
        raise NotImplementedError

    async def get_access_control(self) -> list[AccessControl]:
        """Get list of access control rules."""
        return []

    async def set_access_control_blocked(self, mac: str, blocked: bool) -> bool:
        """Block or unblock a device's internet access."""
        return False

    async def get_external_ip(self) -> str | None:
        """Get public/external IP address."""
        return None

    async def get_leds(self) -> list[LedInfo]:
        """Get list of router LEDs."""
        return []

    async def get_sqm_status(self) -> list[SqmStatus]:
        """Get SQM status."""
        return []

    @abc.abstractmethod
    async def set_sqm_config(self, section_id: str, **kwargs: Any) -> bool:
        """Set SQM configuration and reload."""
        raise NotImplementedError

    @abc.abstractmethod
    async def install_firmware(
        self,
        url: str,
        keep_settings: bool = True,
        force: bool = False,
        sha256: str = "",
    ) -> None:
        """Install firmware from the given URL, verifying sha256 when given."""
        raise NotImplementedError

    @abc.abstractmethod
    async def get_installed_packages(self) -> list[str]:
        """Get a list of installed packages on the device."""
        raise NotImplementedError

    @abc.abstractmethod
    async def perform_diagnostics(self) -> list[DiagnosticResult]:
        """Perform a suite of diagnostic checks to identify configuration issues."""
        raise NotImplementedError

    async def get_adblock_status(self) -> AdBlockStatus:
        """Get status of the adblock package."""
        return AdBlockStatus()

    async def set_adblock_enabled(self, enabled: bool) -> bool:
        """Enable/disable the adblock service."""
        return False

    async def get_simple_adblock_status(self) -> SimpleAdBlockStatus:
        """Get status of the simple-adblock package."""
        return SimpleAdBlockStatus()

    async def set_simple_adblock_enabled(self, enabled: bool) -> bool:
        """Enable/disable the simple-adblock service."""
        return False

    async def get_adblock_fast_status(self) -> SimpleAdBlockStatus:
        """Get status of the adblock-fast package."""
        return SimpleAdBlockStatus()

    async def set_adblock_fast_enabled(self, enabled: bool) -> bool:
        """Enable/disable the adblock-fast service."""
        return False

    async def set_banip_enabled(self, enabled: bool) -> bool:
        """Enable/disable the banIP service (uci flag + init start/stop)."""
        val = "1" if enabled else "0"
        try:
            await self.execute_command(
                f"uci set banip.global.ban_enabled='{val}' && uci commit banip"
            )
            await self.execute_command(
                f"/etc/init.d/banip {'start' if enabled else 'stop'}"
            )
            return True
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("banip enable/disable failed: %s", err)
            return False

    async def create_backup(self) -> str:
        """Create a configuration backup on the router. Returns the backup file path on the router."""
        try:
            # Create backup filename with timestamp
            filename = f"backup-ha-{datetime.now().strftime('%Y%m%d-%H%M%S')}.tar.gz"
            path = f"/tmp/{filename}"

            # Run sysupgrade -b to create the backup
            await self.execute_command(f"sysupgrade -b {path}")

            # Verify file exists and return path
            check = await self.execute_command(f"ls {path}")
            if path in check:
                return path
            return ""
        except Exception as err:
            _LOGGER.exception("Backup creation failed: %s", err)
            raise

    @staticmethod
    def _write_bytes(local_path: str, data: bytes) -> None:
        """Write bytes to a file synchronously in a thread."""
        with open(local_path, "wb") as f:
            f.write(data)

    @abc.abstractmethod
    async def download_file(self, remote_path: str, local_path: str) -> bool:
        """Download a file from the router to a local path."""
        raise NotImplementedError
