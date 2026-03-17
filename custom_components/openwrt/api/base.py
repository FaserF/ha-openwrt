"""Base client interface for OpenWrt API communication."""

from __future__ import annotations

import abc
import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

_LOGGER = logging.getLogger(__name__)

PROVISION_SCRIPT_TEMPLATE = """
USER=$(cat <<'EOF'
{username}
EOF
)
PASS=$(cat <<'EOF'
{password}
EOF
)
ACL_FILE="/usr/share/rpcd/acl.d/homeassistant.json"

# Log to router syslog
logger -t ha-openwrt "Starting provisioning for user: $USER"

# Ensure /bin/ash is in /etc/shells (required for some LuCI setups)
if ! grep -q "^/bin/ash" /etc/shells; then
    echo "/bin/ash" >> /etc/shells
fi

# Create user if it doesn't exist
if ! id "$USER" >/dev/null 2>&1; then
    logger -t ha-openwrt "Creating system user $USER with ash shell"
    if command -v adduser >/dev/null 2>&1; then
        adduser -D -s /bin/ash "$USER" >/dev/null 2>&1 || echo "LOG: FAIL: adduser"
    elif command -v useradd >/dev/null 2>&1; then
        useradd -m -s /bin/ash "$USER" >/dev/null 2>&1 || echo "LOG: FAIL: useradd"
    else
        mkdir -p "/home/$USER"
        echo "$USER:x:1001:1001:HomeAssistant:/home/$USER:/bin/ash" >> /etc/passwd
        echo "$USER:x:1001:" >> /etc/group
    fi
else
    # Update existing user to have valid shell
    logger -t ha-openwrt "Updating existing user $USER to use /bin/ash"
    if command -v usermod >/dev/null 2>&1; then
        usermod -s /bin/ash "$USER" >/dev/null 2>&1 || true
    else
        sed -i "s|^$USER:.*-s /bin/false|$USER:x:$(id -u $USER):$(id -g $USER):HomeAssistant:/home/$USER:/bin/ash|" /etc/passwd 2>/dev/null || true
    fi
    mkdir -p "/home/$USER"
fi

# IMPORTANT: Ensure shadow compatibility. Some OpenWrt builds don't sync this automatically.
# 1. Force 'x' in passwd to trigger shadow usage
sed -i "s|^$USER:[^:]*:|$USER:x:|" /etc/passwd
# 2. Ensure entry exists in shadow
grep -q "^$USER:" /etc/shadow || echo "$USER:*::0:99999:7:::" >> /etc/shadow

# Set password using the most robust method for shadow synchronization
logger -t ha-openwrt "Updating password for $USER"
if ! ( (echo "$PASS"; sleep 1; echo "$PASS") | passwd "$USER" >/dev/null 2>&1 || printf "%s:%s\\n" "$USER" "$PASS" | chpasswd >/dev/null 2>&1 ); then
    echo "LOG: FAIL: password"
    exit 1
fi

# Create ACL file
logger -t ha-openwrt "Creating ACL file $ACL_FILE"
cat <<EOF > "$ACL_FILE"
{{
    "homeassistant": {{
        "description": "Home Assistant Integration",
        "read": {{
            "ubus": {{
                "system": ["*"],
                "network": ["*"],
                "network.interface": ["*"],
                "network.device": ["*"],
                "iwinfo": ["*"],
                "file": ["*"],
                "firewall": ["*"],
                "service": ["*"],
                "uci": ["*"],
                "session": ["*"],
                "hostapd.*": ["*"]
            }},
            "uci": ["*"]
        }},
        "write": {{
            "ubus": {{
                "system": ["reboot", "upgrade"],
                "network.interface": ["up", "down", "reconnect"],
                "network": ["*"],
                "firewall": ["*"],
                "service": ["*"],
                "uci": ["*"],
                "file": ["*"],
                "hostapd.*": ["*"]
            }},
            "uci": ["*"]
        }}
    }}
}}
EOF

chmod 644 "$ACL_FILE"

# Thorough cleanup of existing RPC/LuCI sections for this user
logger -t ha-openwrt "Cleaning up existing UCI RPC/LuCI sections for $USER"
for s in $(uci show rpcd 2>/dev/null | grep "=login" | cut -d. -f2 | cut -d= -f1); do
    [ "$(uci get rpcd.$s.username 2>/dev/null)" = "$USER" ] && uci delete rpcd.$s
done
for s in $(uci show luci 2>/dev/null | grep "=user" | cut -d. -f2 | cut -d= -f1); do
    [ "$(uci get luci.$s.username 2>/dev/null)" = "$USER" ] && uci delete luci.$s
done

# Configure UCI authorization.
# rpcd MUST use '$p$<user>' to delegate to system auth (passwd/shadow).
logger -t ha-openwrt "Configuring UCI authorization for $USER"
uci set luci.homeassistant=user
uci set luci.homeassistant.username="$USER"
uci set luci.homeassistant.password='*'
uci add_list luci.homeassistant.write="homeassistant"
uci add_list luci.homeassistant.read="homeassistant"

uci set rpcd.homeassistant=login
uci set rpcd.homeassistant.username="$USER"
uci set rpcd.homeassistant.password="\\$p\\$$USER"
uci add_list rpcd.homeassistant.read="homeassistant"
uci add_list rpcd.homeassistant.write="homeassistant"

uci commit luci
uci commit rpcd

echo "LOG: Provisioning SUCCESS"
logger -t ha-openwrt "Provisioning completed successfully for $USER. Scheduling service restarts."

# Background restart sequence
(
    sleep 3
    /etc/init.d/rpcd restart
    sleep 3
    if ! pgrep rpcd >/dev/null; then
        logger -t ha-openwrt "rpcd failed to restart, trying again..."
        /etc/init.d/rpcd start
    fi
    /etc/init.d/uhttpd restart
    logger -t ha-openwrt "Services restarted for ha-openwrt provisioning"
) >/dev/null 2>&1 &
"""


@dataclass
class DeviceInfo:
    """OpenWrt device information."""

    hostname: str = ""
    model: str = ""
    board_name: str = ""
    firmware_version: str = ""
    kernel_version: str = ""
    architecture: str = ""
    target: str = ""
    mac_address: str = ""
    gateway_mac: str | None = None
    uptime: int = 0
    local_time: str = ""
    release_distribution: str = "OpenWrt"
    release_version: str = ""
    release_revision: str = ""


@dataclass
class WirelessInterface:
    """Wireless interface information."""

    name: str = ""
    mac_address: str = ""
    ssid: str = ""
    mode: str = ""
    channel: int = 0
    frequency: str = ""
    signal: int = 0
    noise: int = 0
    bitrate: float = 0.0
    quality: float = 0.0
    hwmode: str = ""
    encryption: str = ""
    clients_count: int = 0
    enabled: bool = True
    up: bool = False
    radio: str = ""
    htmode: str = ""
    txpower: int = 0
    mesh_id: str = ""
    mesh_fwding: bool = False
    ifname: str = ""
    section: str = ""


@dataclass
class NetworkInterface:
    """Network interface information."""

    name: str = ""
    up: bool = False
    mac_address: str = ""
    ipv4_address: str = ""
    ipv6_address: str = ""
    rx_bytes: int = 0
    tx_bytes: int = 0
    rx_packets: int = 0
    tx_packets: int = 0
    rx_errors: int = 0
    tx_errors: int = 0
    rx_dropped: int = 0
    tx_dropped: int = 0
    collisions: int = 0
    multicast: int = 0
    rx_rate: float = 0.0
    tx_rate: float = 0.0
    speed: str = ""
    duplex: str = ""
    protocol: str = ""
    device: str = ""
    dns_servers: list[str] = field(default_factory=list)
    uptime: int = 0


@dataclass
class ConnectedDevice:
    """Connected device (client) information."""

    mac: str = ""
    ip: str = ""
    hostname: str = ""
    interface: str = ""
    connected_via: str = ""
    rx_bytes: int = 0
    tx_bytes: int = 0
    rx_rate: int = 0
    tx_rate: int = 0
    signal: int = 0
    noise: int = 0
    is_wireless: bool = False
    connected: bool = True
    connection_type: str = ""  # e.g. "wired", "2.4GHz", "5GHz", "6GHz"
    connection_info: str = ""  # e.g. "802.11ax", "1000Mbps"
    neighbor_state: str = ""
    uptime: int = 0


@dataclass
class SystemResources:
    """System resource information."""

    cpu_usage: float = 0.0
    memory_total: int = 0
    memory_used: int = 0
    memory_free: int = 0
    memory_buffered: int = 0
    memory_cached: int = 0
    swap_total: int = 0
    swap_used: int = 0
    swap_free: int = 0
    load_1min: float = 0.0
    load_5min: float = 0.0
    load_15min: float = 0.0
    uptime: int = 0
    processes: int = 0
    temperature: float | None = None
    filesystem_total: int = 0
    filesystem_used: int = 0
    filesystem_free: int = 0


@dataclass
class MwanStatus:
    """MWAN3 multi-wan status."""

    interface_name: str = ""
    status: str = ""
    online_ratio: float = 0.0
    uptime: int = 0
    enabled: bool = False


@dataclass
class LldpNeighbor:
    """LLDP neighbor information."""

    local_interface: str = ""
    neighbor_name: str = ""
    neighbor_port: str = ""
    neighbor_chassis: str = ""  # Often the MAC address
    neighbor_description: str = ""
    neighbor_system_name: str = ""


@dataclass
class DhcpLease:
    """DHCP lease entry."""

    hostname: str = ""
    mac: str = ""
    ip: str = ""
    expires: int = 0


@dataclass
class IpNeighbor:
    """IP neighbor (ARP/NDP) information."""

    ip: str = ""
    mac: str = ""
    interface: str = ""
    state: str = (
        ""  # REACHABLE, STALE, DELAY, PROBE, INCOMPLETE, FAILED, PERMANENT, NOARP
    )


@dataclass
class WpsStatus:
    """WPS status."""

    enabled: bool = False
    status: str = "disabled"


@dataclass
class QModemInfo:
    """Cellular modem information (QModem)."""

    enabled: bool = False
    manufacturer: str = ""
    revision: str = ""
    temperature: float | None = None
    voltage: int | None = None
    connect_status: str = ""
    sim_status: str = ""
    isp: str = ""
    sim_slot: str = ""
    lte_rsrp: int | None = None
    lte_rsrq: int | None = None
    lte_rssi: int | None = None
    lte_sinr: int | None = None
    nr5g_rsrp: int | None = None
    nr5g_rsrq: int | None = None
    nr5g_sinr: int | None = None


@dataclass
class ServiceInfo:
    """System service information."""

    name: str = ""
    enabled: bool = False
    running: bool = False


@dataclass
class LedInfo:
    """Router LED information."""

    name: str = ""
    brightness: int = 0
    max_brightness: int = 255
    trigger: str = ""
    active: bool = False


@dataclass
class FirewallRedirect:
    """Firewall port forwarding redirect."""

    name: str = ""
    target_ip: str = ""
    target_port: str = ""
    external_port: str = ""
    protocol: str = ""
    enabled: bool = True
    section_id: str = ""


@dataclass
class FirewallRule:
    """General firewall rule."""

    name: str = ""
    enabled: bool = True
    section_id: str = ""
    target: str = ""
    src: str = ""
    dest: str = ""


@dataclass
class AccessControl:
    """Device access control (Parental Control)."""

    mac: str = ""
    name: str = ""
    blocked: bool = False
    section_id: str = ""


@dataclass
class VpnInterface:
    """VPN tunnel interface information."""

    name: str = ""
    type: str = ""  # "wireguard", "openvpn"
    up: bool = False
    rx_bytes: int = 0
    tx_bytes: int = 0
    peers: int = 0
    latest_handshake: int = 0  # unix timestamp
    endpoint: str = ""
    public_key: str = ""


@dataclass
class SqmStatus:
    """SQM (Smart Queue Management) status."""

    name: str = ""
    enabled: bool = False
    interface: str = ""
    download: int = 0  # kbit/s
    upload: int = 0  # kbit/s
    qdisc: str = ""
    script: str = ""
    section_id: str = ""


@dataclass
class LatencyResult:
    """Network latency measurement result."""

    target: str = ""
    latency_ms: float | None = None
    packet_loss: float = 0.0  # percentage
    available: bool = False


@dataclass
class OpenWrtPermissions:
    """Permissions granted to the current user."""

    read_system: bool = False
    write_system: bool = False
    read_network: bool = False
    write_network: bool = False
    read_firewall: bool = False
    write_firewall: bool = False
    read_wireless: bool = False
    write_wireless: bool = False
    read_services: bool = False
    write_services: bool = False
    read_sqm: bool = False
    write_sqm: bool = False
    read_vpn: bool = False
    read_mwan: bool = False
    read_led: bool = False
    write_led: bool = False
    read_devices: bool = False
    write_devices: bool = False
    write_access_control: bool = False


@dataclass
class OpenWrtPackages:
    """Installed packages on the OpenWrt device. None means unknown."""

    sqm_scripts: bool | None = None
    mwan3: bool | None = None
    iwinfo: bool | None = None
    etherwake: bool | None = None
    wireguard: bool | None = None
    openvpn: bool | None = None


@dataclass
class OpenWrtData:
    """Aggregated data from an OpenWrt device."""

    device_info: DeviceInfo = field(default_factory=DeviceInfo)
    system_resources: SystemResources = field(default_factory=SystemResources)
    wireless_interfaces: list[WirelessInterface] = field(default_factory=list)
    network_interfaces: list[NetworkInterface] = field(default_factory=list)
    connected_devices: list[ConnectedDevice] = field(default_factory=list)
    dhcp_leases: list[DhcpLease] = field(default_factory=list)
    ip_neighbors: list[IpNeighbor] = field(default_factory=list)
    mwan_status: list[MwanStatus] = field(default_factory=list)
    wps_status: WpsStatus = field(default_factory=WpsStatus)
    services: list[ServiceInfo] = field(default_factory=list)
    leds: list[LedInfo] = field(default_factory=list)
    firewall_redirects: list[FirewallRedirect] = field(default_factory=list)
    firewall_rules: list[FirewallRule] = field(default_factory=list)
    access_control: list[AccessControl] = field(default_factory=list)
    vpn_interfaces: list[VpnInterface] = field(default_factory=list)
    latency: LatencyResult = field(default_factory=LatencyResult)
    external_ip: str | None = None
    firmware_upgradable: bool = False
    firmware_latest_version: str = ""
    firmware_current_version: str = ""
    firmware_release_url: str = ""
    firmware_checksum: str = ""
    is_custom_build: bool = False
    installed_packages: list[str] = field(default_factory=list)
    asu_supported: bool = False
    asu_update_available: bool = False
    asu_image_status: str = ""  # e.g. "available", "building", "failed"
    asu_image_url: str | None = None
    lldp_neighbors: list[LldpNeighbor] = field(default_factory=list)
    qmodem_info: QModemInfo = field(default_factory=QModemInfo)
    sqm: list[SqmStatus] = field(default_factory=list)
    permissions: OpenWrtPermissions = field(default_factory=OpenWrtPermissions)
    packages: OpenWrtPackages = field(default_factory=OpenWrtPackages)


class OpenWrtClient(abc.ABC):
    """Abstract base class for OpenWrt API clients."""

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
        """Initialize the client."""
        self.host = host
        self.username = username
        self.password = password
        self.port = port
        self.use_ssl = use_ssl
        self.verify_ssl = verify_ssl
        self.dhcp_software = dhcp_software
        self._connected = False
        self._poll_count = 0
        self._cached_device_info: DeviceInfo | None = None
        self._cached_slow_data: dict[str, Any] = {}
        self._last_cpu_stats: tuple[int, int] | None = None

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
        try:
            output = await self.execute_command(f"id -u {username} 2>/dev/null")
            return output.strip().isdigit()
        except Exception:
            return False

    @abc.abstractmethod
    async def provision_user(self, username: str, password: str) -> bool:
        """Create a dedicated system user and configure RPC permissions."""
        raise NotImplementedError

    @abc.abstractmethod
    async def get_wireless_interfaces(self) -> list[WirelessInterface]:
        """Get wireless interface information."""
        raise NotImplementedError

    def _calculate_cpu_usage(self, proc_stat: str) -> float:
        """Calculate CPU usage percentage from /proc/stat.

        Formula:
        Idle = idle + iowait
        NonIdle = user + nice + system + irq + softirq + steal
        Total = Idle + NonIdle

        Percentage = (Total - Idle) / Total
        """
        if not proc_stat:
            return 0.0

        try:
            # cpu  user nice system idle iowait irq softirq steal guest guest_nice
            line = proc_stat.splitlines()[0]
            parts = line.split()
            if len(parts) < 5:
                return 0.0

            # parts[1] is user, parts[2] is nice, etc.
            user = int(parts[1])
            nice = int(parts[2])
            system = int(parts[3])
            idle = int(parts[4])
            iowait = int(parts[5]) if len(parts) > 5 else 0
            irq = int(parts[6]) if len(parts) > 6 else 0
            softirq = int(parts[7]) if len(parts) > 7 else 0
            steal = int(parts[8]) if len(parts) > 8 else 0

            idle_time = idle + iowait
            non_idle_time = user + nice + system + irq + softirq + steal
            total_time = idle_time + non_idle_time

            if self._last_cpu_stats is None:
                self._last_cpu_stats = (total_time, idle_time)
                return 0.0

            prev_total, prev_idle = self._last_cpu_stats
            self._last_cpu_stats = (total_time, idle_time)

            total_diff = total_time - prev_total
            idle_diff = idle_time - prev_idle

            if total_diff <= 0:
                return 0.0

            cpu_usage = (total_diff - idle_diff) / total_diff
            return round(max(0.0, min(100.0, cpu_usage * 100.0)), 1)

        except ValueError, IndexError:
            return 0.0

    async def get_gateway_mac(self) -> str | None:
        """Get the default gateway MAC address."""
        return None

    async def get_lldp_neighbors(self) -> list[LldpNeighbor]:
        """Get LLDP neighbor information."""
        return []

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

    @abc.abstractmethod
    async def get_ip_neighbors(self) -> list[IpNeighbor]:
        """Get IP neighbor (ARP/NDP) table."""
        raise NotImplementedError

    @abc.abstractmethod
    async def reboot(self) -> bool:
        """Reboot the device."""
        raise NotImplementedError

    @abc.abstractmethod
    async def execute_command(self, command: str) -> str:
        """Execute a command on the device."""
        raise NotImplementedError

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

    async def get_mwan_status(self) -> list[MwanStatus]:
        """Get MWAN3 status (optional, may not be installed)."""
        return []

    async def get_wps_status(self) -> WpsStatus:
        """Get WPS status."""
        return WpsStatus()

    async def set_wps(self, enabled: bool) -> bool:
        """Enable or disable WPS."""
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

    async def manage_interface(self, name: str, action: str) -> bool:
        """Manage a network interface (up/down/reconnect)."""
        return False

    async def get_firewall_redirects(self) -> list[FirewallRedirect]:
        """Get firewall port forwarding redirects."""
        return []

    async def set_firewall_redirect_enabled(
        self, section_id: str, enabled: bool
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

    async def set_led(self, name: str, brightness: int) -> bool:
        """Set LED brightness (0=off, max=on)."""
        return False

    async def get_sqm_status(self) -> list[SqmStatus]:
        """Get SQM status."""
        return []

    @abc.abstractmethod
    async def set_sqm_config(self, section_id: str, **kwargs: Any) -> bool:
        """Set SQM configuration and reload."""
        raise NotImplementedError

    @abc.abstractmethod
    async def install_firmware(self, url: str) -> None:
        """Install firmware from the given URL."""
        raise NotImplementedError

    @abc.abstractmethod
    async def get_installed_packages(self) -> list[str]:
        """Get a list of installed packages on the device."""
        raise NotImplementedError

    async def get_vpn_status(self) -> list[VpnInterface]:
        """Get VPN tunnel status (WireGuard/OpenVPN)."""
        vpn_interfaces: list[VpnInterface] = []
        try:
            # Try WireGuard first
            output = await self.execute_command("wg show all dump 2>/dev/null")
            if output and "not found" not in output.lower():
                current_iface = ""
                for line in output.strip().splitlines():
                    parts = line.split("\t")
                    if len(parts) >= 4:
                        iface_name = parts[0]
                        if iface_name != current_iface:
                            current_iface = iface_name
                            # First line per interface is the interface itself
                            vpn = VpnInterface(
                                name=iface_name,
                                type="wireguard",
                            )
                            # Check if interface is up
                            ip_out = await self.execute_command(
                                f"ip link show {iface_name} 2>/dev/null"
                            )
                            vpn.up = bool(ip_out and "UP" in ip_out)

                            # Get RX/TX bytes
                            rx_out = await self.execute_command(
                                f"cat /sys/class/net/{iface_name}/statistics/rx_bytes 2>/dev/null"
                            )
                            tx_out = await self.execute_command(
                                f"cat /sys/class/net/{iface_name}/statistics/tx_bytes 2>/dev/null"
                            )
                            try:
                                vpn.rx_bytes = (
                                    int(rx_out.strip())
                                    if rx_out and rx_out.strip().isdigit()
                                    else 0
                                )
                                vpn.tx_bytes = (
                                    int(tx_out.strip())
                                    if tx_out and tx_out.strip().isdigit()
                                    else 0
                                )
                            except ValueError, AttributeError:
                                pass

                            vpn_interfaces.append(vpn)
                        else:
                            # Subsequent lines are peers
                            for vpn in vpn_interfaces:
                                if vpn.name == current_iface:
                                    vpn.peers += 1
                                    # parts[4] = latest-handshake
                                    if len(parts) > 4 and parts[4].isdigit():
                                        handshake = int(parts[4])
                                        if handshake > vpn.latest_handshake:
                                            vpn.latest_handshake = handshake
                                    break
        except Exception as err:
            _LOGGER.debug("WireGuard status check failed: %s", err)

        try:
            # Try OpenVPN
            output = await self.execute_command("pgrep -a openvpn 2>/dev/null")
            if output and "not found" not in output.lower() and output.strip():
                # OpenVPN is running – check interfaces
                tun_output = await self.execute_command(
                    "ip -br link show type tun 2>/dev/null"
                )
                if tun_output:
                    for line in tun_output.strip().splitlines():
                        parts = line.split()
                        if len(parts) >= 2:
                            iface_name = parts[0]
                            state = parts[1]
                            vpn = VpnInterface(
                                name=iface_name,
                                type="openvpn",
                                up=state == "UP",
                            )
                            # Get RX/TX bytes
                            rx_out = await self.execute_command(
                                f"cat /sys/class/net/{iface_name}/statistics/rx_bytes 2>/dev/null"
                            )
                            tx_out = await self.execute_command(
                                f"cat /sys/class/net/{iface_name}/statistics/tx_bytes 2>/dev/null"
                            )
                            try:
                                vpn.rx_bytes = (
                                    int(rx_out.strip())
                                    if rx_out and rx_out.strip().isdigit()
                                    else 0
                                )
                                vpn.tx_bytes = (
                                    int(tx_out.strip())
                                    if tx_out and tx_out.strip().isdigit()
                                    else 0
                                )
                            except ValueError, AttributeError:
                                pass
                            vpn_interfaces.append(vpn)
        except Exception as err:
            _LOGGER.debug("OpenVPN status check failed: %s", err)

        return vpn_interfaces

    async def get_latency(self, target: str = "8.8.8.8") -> LatencyResult:
        """Measure network latency via ping."""
        result = LatencyResult(target=target)
        try:
            output = await self.execute_command(f"ping -c 3 -W 2 {target} 2>/dev/null")
            if output:
                result.available = True
                # Parse avg from "min/avg/max/mdev = x/y/z/w ms"
                for line in output.splitlines():
                    if "min/avg/max" in line:
                        stats = line.split("=")[-1].strip().split("/")
                        if len(stats) >= 2:
                            result.latency_ms = round(float(stats[1]), 1)
                    if "packet loss" in line:
                        match = re.search(r"(\d+)%", line)
                        if match:
                            result.packet_loss = float(match.group(1))
        except Exception as err:
            _LOGGER.debug("Latency check failed: %s", err)
        return result

    async def create_backup(self) -> str:
        """Create a configuration backup on the router. Returns the backup file path."""
        try:
            output = await self.execute_command(
                "sysupgrade -b /tmp/backup-ha-$(date +%Y%m%d-%H%M%S).tar.gz && ls -t /tmp/backup-ha-*.tar.gz | head -1"
            )
            return output.strip() if output else ""
        except Exception as err:
            _LOGGER.error("Backup creation failed: %s", err)
            raise

    async def get_qmodem_info(self) -> QModemInfo:
        """Get cellular modem status from QModem's modem_ctrl ubus subsystem (if available)."""
        info = QModemInfo()
        try:
            output = await self.execute_command("ubus call modem_ctrl info")
            if not output or "Not found" in output or "Method not found" in output:
                return info

            try:
                data = json.loads(output)
            except json.JSONDecodeError:
                return info

            info_list = data.get("info", [])
            if not info_list:
                return info

            info.enabled = True

            for info_item in info_list:
                modem_info_list = info_item.get("modem_info", [])

                current_context = None
                lte_signals = {}
                nr5g_signals = {}

                for item in modem_info_list:
                    class_origin = item.get("class_origin", "")
                    item_key = item.get("key", "")
                    value = item.get("value", "")
                    item_type = item.get("type", "")

                    if item_key == "LTE":
                        current_context = "LTE"
                    elif item_key.startswith("NR"):
                        current_context = "NR5G"

                    if class_origin == "Base Information":
                        if item_key == "manufacturer":
                            info.manufacturer = str(value) if value else ""
                        elif item_key == "revision":
                            info.revision = str(value) if value else ""
                        elif item_key == "temperature":
                            match = re.search(r"(\d+)", str(value))
                            info.temperature = int(match.group(1)) if match else None
                        elif item_key == "voltage":
                            match = re.search(r"(\d+)", str(value))
                            info.voltage = int(match.group(1)) if match else None
                        elif item_key == "connect_status":
                            info.connect_status = str(value) if value else ""
                    elif class_origin == "SIM Information":
                        if item_key == "SIM Status":
                            info.sim_status = (
                                str(value).replace("\n", " ").strip() if value else ""
                            )
                        elif item_key == "ISP":
                            info.isp = (
                                str(value).replace("\n", " ").strip() if value else ""
                            )
                        elif item_key == "SIM Slot":
                            info.sim_slot = (
                                str(value).replace("\n", " ").strip() if value else ""
                            )

                    elif (
                        item_type == "progress_bar"
                        and class_origin == "Cell Information"
                    ):
                        if current_context == "LTE":
                            lte_signals[item_key] = value
                        elif current_context == "NR5G":
                            nr5g_signals[item_key] = value

                def extract_int(val: Any, pattern: str = r"(-?\d+)") -> int | None:
                    match = re.search(pattern, str(val))
                    return int(match.group(1)) if match else None

                if "RSRP" in lte_signals:
                    info.lte_rsrp = extract_int(lte_signals["RSRP"])
                if "RSRQ" in lte_signals:
                    info.lte_rsrq = extract_int(lte_signals["RSRQ"])
                if "RSSI" in lte_signals:
                    info.lte_rssi = extract_int(lte_signals["RSSI"])
                if "SINR" in lte_signals:
                    info.lte_sinr = extract_int(lte_signals["SINR"], r"(\d+)")

                if "RSRP" in nr5g_signals:
                    info.nr5g_rsrp = extract_int(nr5g_signals["RSRP"])
                if "RSRQ" in nr5g_signals:
                    info.nr5g_rsrq = extract_int(nr5g_signals["RSRQ"])
                if "SINR" in nr5g_signals:
                    info.nr5g_sinr = extract_int(nr5g_signals["SINR"], r"(\d+)")

        except Exception as err:
            _LOGGER.debug("Error retrieving QModem info: %s", err)

        return info

    async def get_all_data(self) -> OpenWrtData:
        """Get all data in one call.

        Core data (device_info, system_resources, network_interfaces, connected_devices)
        must succeed or raise an exception to trigger UpdateFailed in coordinator.
        Optional modules may fail gracefully.

        Slow-changing data (device_info, services, LEDs, firewall rules/redirects,
        access_control) is only fetched every SLOW_POLL_INTERVAL polls to reduce
        router load.
        """
        SLOW_POLL_INTERVAL = 10  # Fetch slow data every 10th poll

        data = OpenWrtData()
        self._poll_count = getattr(self, "_poll_count", 0) + 1
        is_full_poll = (
            self._poll_count % SLOW_POLL_INTERVAL == 1 or self._poll_count == 1
        )

        if is_full_poll:
            # Full poll: fetch device_info fresh
            core_results = await asyncio.gather(
                self.get_device_info(),
                self.get_system_resources(),
                self.get_network_interfaces(),
                self.get_connected_devices(),
            )
            (
                data.device_info,
                data.system_resources,
                data.network_interfaces,
                data.connected_devices,
            ) = core_results
            self._cached_device_info = data.device_info
        else:
            # Fast poll: reuse cached device_info, fetch dynamic core data
            core_results_fast = await asyncio.gather(
                self.get_system_resources(),
                self.get_network_interfaces(),
                self.get_connected_devices(),
            )
            data.system_resources, data.network_interfaces, data.connected_devices = (
                core_results_fast
            )
            data.device_info = getattr(self, "_cached_device_info", data.device_info)

        # Ensure router MAC address is populated from interfaces if missing
        if data.device_info and not data.device_info.mac_address:
            # Try to find a suitable MAC address (br-lan, lan, eth0, or first non-loopback)
            iface_map = {iface.name: iface for iface in data.network_interfaces}
            best_iface = (
                iface_map.get("br-lan")
                or iface_map.get("lan")
                or iface_map.get("eth0")
                or iface_map.get("eth1")
            )

            if not best_iface:
                # Fallback: first interface with a MAC that isn't loopback
                for iface in data.network_interfaces:
                    if iface.mac_address and iface.name != "lo":
                        best_iface = iface
                        break

            if best_iface and best_iface.mac_address:
                data.device_info.mac_address = best_iface.mac_address.upper()

        # Always-fresh optional data (changes every cycle)
        fast_optional_tasks = [
            self.get_wireless_interfaces(),
            self.get_dhcp_leases(),
            self.get_ip_neighbors(),
            self.get_mwan_status(),
            self.get_wps_status(),
            self.get_qmodem_info(),
            self.get_vpn_status(),
            self.get_latency(),
            self.get_external_ip(),
            self.get_gateway_mac(),
            self.get_lldp_neighbors(),
        ]

        fast_results = await asyncio.gather(
            *fast_optional_tasks, return_exceptions=True
        )

        def get_val(res: Any, default: Any, name: str) -> Any:
            if isinstance(res, Exception):
                _LOGGER.debug("Optional %s info failed: %s", name, res)
                return default
            return res

        data.wireless_interfaces = get_val(fast_results[0], [], "wireless")
        data.dhcp_leases = get_val(fast_results[1], [], "DHCP leases")
        data.ip_neighbors = get_val(fast_results[2], [], "IP neighbors")
        data.mwan_status = get_val(fast_results[3], [], "MWAN")
        data.wps_status = get_val(fast_results[4], WpsStatus(), "WPS")
        data.qmodem_info = get_val(fast_results[5], QModemInfo(), "QModem")
        data.vpn_interfaces = get_val(fast_results[6], [], "VPN status")
        data.latency = get_val(fast_results[7], LatencyResult(), "latency")
        data.external_ip = get_val(fast_results[8], None, "external IP")
        data.device_info.gateway_mac = get_val(fast_results[9], None, "gateway MAC")
        data.lldp_neighbors = get_val(fast_results[10], [], "LLDP neighbors")

        # Slow-changing optional data (services, LEDs, firewall, access control, packages, permissions)
        if is_full_poll:
            slow_optional_tasks = [
                self.get_services(),
                self.get_leds(),
                self.get_firewall_redirects(),
                self.get_firewall_rules(),
                self.get_access_control(),
                self.get_sqm_status(),
                self.check_packages(),
                self.check_permissions(),
            ]
            slow_results = await asyncio.gather(
                *slow_optional_tasks, return_exceptions=True
            )

            data.services = get_val(slow_results[0], [], "services")
            data.leds = get_val(slow_results[1], [], "LEDs")
            data.firewall_redirects = get_val(slow_results[2], [], "firewall redirects")
            data.firewall_rules = get_val(slow_results[3], [], "firewall rules")
            data.access_control = get_val(slow_results[4], [], "access control")
            data.sqm = get_val(slow_results[5], [], "SQM")
            data.packages = get_val(slow_results[6], OpenWrtPackages(), "packages")
            data.permissions = get_val(
                slow_results[7], OpenWrtPermissions(), "permissions"
            )

            # Cache slow results
            self._cached_slow_data = {
                "services": data.services,
                "leds": data.leds,
                "firewall_redirects": data.firewall_redirects,
                "firewall_rules": data.firewall_rules,
                "access_control": data.access_control,
                "sqm": data.sqm,
                "packages": data.packages,
                "permissions": data.permissions,
            }
            _LOGGER.debug(
                "Full poll cycle %d: refreshed slow-changing data", self._poll_count
            )
        else:
            # Reuse cached slow-changing data
            cached = getattr(self, "_cached_slow_data", {})
            data.services = cached.get("services", [])
            data.leds = cached.get("leds", [])
            data.firewall_redirects = cached.get("firewall_redirects", [])
            data.firewall_rules = cached.get("firewall_rules", [])
            data.access_control = cached.get("access_control", [])
            data.sqm = cached.get("sqm", [])
            data.packages = cached.get("packages", OpenWrtPackages())
            data.permissions = cached.get("permissions", OpenWrtPermissions())

        return data
