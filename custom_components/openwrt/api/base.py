"""Base client interface for OpenWrt API communication."""

from __future__ import annotations

import abc
import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

_LOGGER = logging.getLogger(__name__)

PROVISION_SCRIPT_TEMPLATE = """
# Dedicated Home Assistant User Provisioning Script
# This script creates a system user, sets a password, and configures RPC/LuCI ACLs.

USER=$(cat <<'EOF'
{username}
EOF
)
PASS=$(cat <<'EOF'
{password}
EOF
)

# Use absolute paths for core utilities to ensure they are found
LOGGER="/usr/bin/logger"
[ -x "$LOGGER" ] || LOGGER="logger"
UCI="/sbin/uci"
[ -x "$UCI" ] || UCI="uci"

# Log start
$LOGGER -t ha-openwrt "Starting provisioning for user: $USER"

# Create a safe section name (alphanumeric only)
SECTION=$(echo "$USER" | tr -cd '[:alnum:]' | tr '[:upper:]' '[:lower:]')
[ -n "$SECTION" ] || SECTION="homeassistant"

# Ensure /bin/ash is in /etc/shells (required for some LuCI setups)
if ! grep -q "^/bin/ash" /etc/shells 2>/dev/null; then
    echo "/bin/ash" >> /etc/shells
fi

# Create user if it doesn't exist
if ! id "$USER" >/dev/null 2>&1; then
    $LOGGER -t ha-openwrt "Creating system user $USER"
    if command -v adduser >/dev/null 2>&1; then
        adduser -D -s /bin/ash "$USER" >/dev/null 2>&1 || echo "LOG: FAIL: adduser failed"
    elif command -v useradd >/dev/null 2>&1; then
        useradd -m -s /bin/ash "$USER" >/dev/null 2>&1 || echo "LOG: FAIL: useradd failed"
    else
        # Manual fallback for minimal systems
        mkdir -p "/home/$USER"
        if ! echo "$USER:x:1001:1001:HomeAssistant:/home/$USER:/bin/ash" >> /etc/passwd; then
            echo "ANALYSIS: PASSWD_WRITE_FAILED"
            echo "LOG: FAIL: could not write to /etc/passwd"
            exit 1
        fi
        echo "$USER:x:1001:" >> /etc/group
    fi
else
    # Update existing user to have valid shell
    $LOGGER -t ha-openwrt "Updating existing user $USER to use /bin/ash"
    if command -v usermod >/dev/null 2>&1; then
        usermod -s /bin/ash "$USER" >/dev/null 2>&1 || true
    else
        uid=$(id -u "$USER" 2>/dev/null)
        gid=$(id -g "$USER" 2>/dev/null)
        sed -i "s|^$USER:.*|$USER:x:$uid:$gid:HomeAssistant:/home/$USER:/bin/ash|" /etc/passwd 2>/dev/null || true
    fi
    mkdir -p "/home/$USER"
fi

# IMPORTANT: Ensure shadow compatibility.
# 1. Force 'x' in passwd to trigger shadow usage
sed -i "s|^$USER:[^:]*:|$USER:x:|" /etc/passwd
# 2. Ensure entry exists in shadow if shadow exists
if [ -f /etc/shadow ]; then
    grep -q "^$USER:" /etc/shadow || echo "$USER:*::0:99999:7:::" >> /etc/shadow
fi

# Set password using the most robust method
$LOGGER -t ha-openwrt "Updating password for $USER"
if command -v chpasswd >/dev/null 2>&1; then
    if ! printf "%s:%s\\n" "$USER" "$PASS" | chpasswd >/dev/null 2>&1; then
        echo "ANALYSIS: CHPASSWD_FAILED"
        echo "LOG: FAIL: chpasswd failed"
        exit 1
    fi
else
    # Fallback to passwd with piped input
    if ! (echo "$PASS"; sleep 1; echo "$PASS") | passwd "$USER" >/dev/null 2>&1; then
        echo "ANALYSIS: PASSWD_BINARY_FAILED"
        echo "LOG: FAIL: passwd failed"
        exit 1
    fi
fi

# Create ACL file
ACL_FILE="/usr/share/rpcd/acl.d/homeassistant.json"
$LOGGER -t ha-openwrt "Creating ACL file $ACL_FILE"
mkdir -p "$(dirname "$ACL_FILE")"
if ! cat <<EOF > "$ACL_FILE"
{{
    "homeassistant": {{
        "description": "Home Assistant Integration",
        "read": {{
            "ubus": {{
                "system": ["info", "board", "logread", "upgrade"],
                "log": ["read"],
                "network": ["*"],
                "network.interface": ["dump", "status"],
                "network.device": ["status"],
                "network.wireless": ["status"],
                "iwinfo": ["info", "assoclist", "txpowerlist", "scan", "devices"],
                "file": ["read", "stat", "list"],
                "firewall": ["status"],
                "rc": ["list"],
                "service": ["list"],
                "uci": ["get", "state"],
                "session": ["list"],
                "hostapd.*": ["get_clients"],
                "attendedsysupgrade": ["*"]
            }},
            "uci": ["*"],
            "file": {{
                "/etc/config/*": ["read", "stat"],
                "/etc/passwd": ["read"],
                "/etc/group": ["read"],
                "/etc/shadow": ["read"],
                "/etc/shells": ["read"],
                "/usr/bin/iwinfo": ["read", "stat", "exec"],
                "/usr/bin/etherwake": ["read", "stat", "exec"],
                "/usr/bin/wg": ["read", "stat", "exec"],
                "/usr/sbin/openvpn": ["read", "stat", "exec"],
                "/usr/bin/id": ["read", "stat", "exec"],
                "/bin/sh": ["read", "stat", "exec"],
                "/bin/ash": ["read", "stat", "exec"],
                "/bin/ls": ["read", "stat", "exec"],
                "/sbin/apk": ["read", "stat", "exec"],
                "/bin/opkg": ["read", "stat", "exec"],
                "/sbin/logread": ["read", "stat"],
                "/bin/cat": ["read", "stat", "exec"],
                "/bin/grep": ["read", "stat", "exec"],
                "/usr/bin/awk": ["read", "stat", "exec"],
                "/bin/df": ["read", "stat", "exec"],
                "/sbin/ip": ["read", "stat", "exec"],
                "/usr/sbin/ip": ["read", "stat", "exec"],
                "/bin/ubus": ["read", "stat", "exec"],
                "/bin/ping": ["read", "stat", "exec"],
                "/usr/bin/ping": ["read", "stat", "exec"],
                "/usr/bin/uptime": ["read", "stat", "exec"],
                "/proc/stat": ["read"],
                "/proc/meminfo": ["read"],
                "/proc/net/arp": ["read"],
                "/proc/net/dev": ["read"],
                "/tmp/dhcp.leases": ["read"],
                "/sys/class/thermal/*": ["read"]
            }}
        }},
        "write": {{
            "ubus": {{
                "system": ["reboot", "upgrade"],
                "network.interface": ["up", "down", "reconnect"],
                "network": ["*"],
                "firewall": ["*"],
                "rc": ["*"],
                "service": ["*"],
                "uci": ["*"],
                "file": ["exec"],
                "hostapd.*": ["*"]
            }},
            "uci": ["*"],
            "file": {{
                "/bin/sh": ["exec"],
                "/bin/ash": ["exec"],
                "/usr/bin/id": ["exec"],
                "/sbin/apk": ["exec"],
                "/bin/opkg": ["exec"]
            }}
        }}
    }}
}}
EOF
then
    echo "ANALYSIS: ACL_WRITE_FAILED"
    echo "LOG: FAIL: could not create ACL file $ACL_FILE"
    exit 1
fi

chmod 644 "$ACL_FILE"

# Thorough cleanup of existing RPC/LuCI sections for this user
$LOGGER -t ha-openwrt "Cleaning up existing UCI RPC/LuCI sections for $USER"
for s in $($UCI show rpcd 2>/dev/null | grep "=login" | cut -d. -f2 | cut -d= -f1); do
    [ "$($UCI get rpcd.$s.username 2>/dev/null)" = "$USER" ] && $UCI delete rpcd.$s
done
for s in $($UCI show luci 2>/dev/null | grep "=user" | cut -d. -f2 | cut -d= -f1); do
    [ "$($UCI get luci.$s.username 2>/dev/null)" = "$USER" ] && $UCI delete luci.$s
done

# Configure UCI authorization.
$LOGGER -t ha-openwrt "Configuring UCI authorization for $USER (section: $SECTION)"
$UCI set luci.$SECTION=user
$UCI set luci.$SECTION.username="$USER"
$UCI set luci.$SECTION.password='*'
$UCI add_list luci.$SECTION.write="homeassistant"
$UCI add_list luci.$SECTION.read="homeassistant"

$UCI set rpcd.$SECTION=login
$UCI set rpcd.$SECTION.username="$USER"
$UCI set rpcd.$SECTION.password="\\$p\\$$USER"
$UCI add_list rpcd.$SECTION.read="homeassistant"
$UCI add_list rpcd.$SECTION.write="homeassistant"

if ! ($UCI commit luci && $UCI commit rpcd); then
    echo "ANALYSIS: UCI_COMMIT_FAILED"
    echo "LOG: FAIL: uci commit failed"
    exit 1
fi

$LOGGER -t ha-openwrt "Provisioning SUCCESS for $USER"
echo "ANALYSIS: SUCCESS"
echo "LOG: Provisioning SUCCESS"

# Asynchronously restart services to apply changes without breaking the current session
(
    sleep 3
    $LOGGER -t ha-openwrt "Restarting rpcd to apply ACL changes"
    /etc/init.d/rpcd restart
    sleep 2
    $LOGGER -t ha-openwrt "Restarting uhttpd to apply LuCI changes"
    /etc/init.d/uhttpd restart
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
    band: str = ""  # 2.4 GHz, 5 GHz, 6 GHz
    width: str = ""  # 20 MHz, 40 MHz, 80 MHz, 160 MHz, 320 MHz
    standard: str = ""  # 802.11n/ac/ax/be

    @staticmethod
    def _band_from_raw(raw: str) -> str:
        """Normalise raw band/frequency/hwmode strings to a human-readable band.

        Handles:
        - OpenWrt new-style short strings: '2g', '5g', '6g'
        - Legacy hwmode strings: 'b', 'g', 'bg', 'a', 'ac', 'ax'
        - Raw frequency values in MHz: '2412', '5180', '6135'
        - Already normalised strings: '2.4 GHz', '5 GHz', '6 GHz'
        """
        if not raw:
            return ""
        s = str(raw).lower().strip()
        # Already normalised
        if "ghz" in s:
            if "2.4" in s:
                return "2.4 GHz"
            if "6" in s:
                return "6 GHz"
            if "5" in s:
                return "5 GHz"
        # Short OpenWrt band keys: 2g, 5g, 6g
        if s in ("2g", "2.4g"):
            return "2.4 GHz"
        if s == "5g":
            return "5 GHz"
        if s == "6g":
            return "6 GHz"
        # Legacy hwmode strings
        if any(x in s for x in ("11b", "11g", "bg", "bgn")):
            return "2.4 GHz"
        if any(x in s for x in ("11a", "ac", "11ac")):
            return "5 GHz"
        if "11ax" in s or "ax" in s:
            return ""  # ax is ambiguous without frequency
        # Raw numeric frequency in MHz or GHz
        digits = s.replace(".", "", 1)
        if digits.isdigit():
            freq = float(s)
            if 2000 <= freq <= 3000:
                return "2.4 GHz"
            if 4900 <= freq <= 5900:
                return "5 GHz"
            if 5900 < freq <= 7200:
                return "6 GHz"
            # Frequency given in GHz (e.g. '2.412')
            if 2.0 <= freq <= 3.0:
                return "2.4 GHz"
            if 4.9 <= freq <= 5.9:
                return "5 GHz"
            if 5.9 < freq <= 7.2:
                return "6 GHz"
        return ""

    def __post_init__(self) -> None:
        """Post-process wireless data."""
        if not self.band:
            # Prefer frequency (most accurate), then hwmode, then the radio's band field
            for raw in (self.frequency, self.hwmode):
                resolved = self._band_from_raw(raw)
                if resolved:
                    self.band = resolved
                    break


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
    is_link_up: bool = False
    link_speed: int = 0
    link_duplex: str = ""
    protocol: str = ""
    device: str = ""
    dns_servers: list[str] = field(default_factory=list)
    ipv6_prefix: list[str] = field(default_factory=list)
    ipv6_prefix_assignment: list[dict[str, Any]] = field(default_factory=list)
    uptime: int = 0


@dataclass
class ConnectedDevice:
    """Connected device (client) information."""

    mac: str = ""
    ip: str = ""
    hostname: str = ""
    interface: str = ""
    port: str = ""
    port_description: str = ""
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
class StorageUsage:
    """Detailed storage usage information for a mount point."""

    mount_point: str = ""
    device: str = ""
    filesystem: str = ""
    total: int = 0
    used: int = 0
    free: int = 0
    percent: float = 0.0


@dataclass
class SystemResources:
    """System resource information."""

    cpu_usage: float = 0.0
    memory_total: int = 0
    memory_available: int = 0
    memory_available_percent: float = 0.0
    memory_used: int = 0
    memory_used_percent: float = 0.0
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
    temperatures: dict[str, float] = field(default_factory=dict)
    cpu_frequency: float | None = None
    filesystem_total: int = 0
    filesystem_used: int = 0
    filesystem_free: int = 0
    storage: list[StorageUsage] = field(default_factory=list)
    usb_devices: list[UsbDevice] = field(default_factory=list)
    top_processes: list[ProcessInfo] = field(default_factory=list)


@dataclass
class ProcessInfo:
    """Process information."""

    pid: int = 0
    user: str = ""
    command: str = ""
    cpu_usage: float = 0.0
    memory_usage: float = 0.0
    vsz: int = 0
    rss: int = 0


@dataclass
class UsbDevice:
    """USB device information."""

    id: str = ""  # Bus:Device e.g. 001:002
    vendor_id: str = ""
    product_id: str = ""
    manufacturer: str = ""
    product: str = ""
    serial: str = ""
    speed: str = ""  # e.g. 480M, 5G
    class_name: str = ""


@dataclass
class MwanStatus:
    """MWAN3 multi-wan status."""

    interface_name: str = ""
    status: str = ""
    online_ratio: float = 0.0
    uptime: int = 0
    enabled: bool = False
    latency: float | None = None
    packet_loss: float | None = None


@dataclass
class LldpNeighbor:
    """LLDP neighbor information."""

    local_interface: str = ""
    neighbor_name: str = ""
    neighbor_port: str = ""
    neighbor_id: str = ""
    neighbor_management_address: str = ""
    neighbor_chassis: str = ""  # Often the MAC address
    neighbor_description: str = ""
    neighbor_system_name: str = ""


@dataclass
class WifiCredentials:
    """Wi-Fi credentials for QR code generation."""

    iface: str = ""
    ssid: str = ""
    encryption: str = ""
    key: str = ""
    hidden: bool = False


@dataclass
class DhcpLease:
    """DHCP lease entry."""

    hostname: str = ""
    mac: str = ""
    ip: str = ""
    expires: int = 0
    type: str = "v4"  # v4 or v6
    duid: str = ""  # DHCPv6 DUID


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
class WireGuardInterface:
    """WireGuard VPN interface information."""

    name: str = ""
    enabled: bool = True
    public_key: str = ""
    listen_port: int = 0
    fwmark: int = 0
    peers: list[WireGuardPeer] = field(default_factory=list)


@dataclass
class WireGuardPeer:
    """WireGuard VPN peer information."""

    public_key: str = ""
    endpoint: str = ""
    allowed_ips: list[str] = field(default_factory=list)
    latest_handshake: int = 0
    transfer_rx: int = 0
    transfer_tx: int = 0
    persistent_keepalive: int = 0


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
class AdBlockStatus:
    """Status of the adblock package."""

    enabled: bool = False
    status: str = "disabled"
    version: str | None = None
    blocked_domains: int = 0
    last_update: str | None = None


@dataclass
class SimpleAdBlockStatus:
    """Status of the simple-adblock package."""

    enabled: bool = False
    status: str = "disabled"
    version: str | None = None
    blocked_domains: int = 0


@dataclass
class BanIpStatus:
    """Status of the ban-ip package."""

    enabled: bool = False
    status: str = "disabled"
    version: str | None = None
    banned_ips: int = 0


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
class UpnpMapping:
    """UPnP/NAT-PMP port mapping."""

    protocol: str = ""  # TCP/UDP
    external_port: int = 0
    internal_ip: str = ""
    internal_port: int = 0
    description: str = ""
    enabled: bool = True


@dataclass
class FirewallRule:
    """General firewall rule."""

    name: str = ""
    enabled: bool = True
    section_id: str = ""
    target: str = ""
    src: str = ""
    dest: str = ""
    src_mac: str = ""


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
    available: bool = True


@dataclass
class NlbwmonTraffic:
    """Traffic statistics for a specific MAC address from nlbwmon."""

    mac: str = ""
    rx_bytes: int = 0
    tx_bytes: int = 0
    rx_packets: int = 0
    tx_packets: int = 0


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
    write_vpn: bool = False
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
    luci_mod_rpc: bool | None = None
    asu: bool | None = None
    adblock: bool | None = None
    adblock_fast: bool | None = None
    simple_adblock: bool | None = None
    ban_ip: bool | None = None
    miniupnpd: bool | None = None
    nlbwmon: bool | None = None
    pbr: bool | None = None
    adguardhome: bool | None = None
    unbound: bool | None = None

    dhcp: bool | None = None
    wireless: bool | None = None
    lldp: bool | None = None


@dataclass
class OpenWrtData:
    """Aggregated data from an OpenWrt device."""

    device_info: DeviceInfo = field(default_factory=DeviceInfo)
    local_macs: set[str] = field(default_factory=set)
    local_ips: set[str] = field(default_factory=set)
    system_resources: SystemResources = field(default_factory=SystemResources)
    wireless_interfaces: list[WirelessInterface] = field(default_factory=list)
    network_interfaces: list[NetworkInterface] = field(default_factory=list)
    connected_devices: list[ConnectedDevice] = field(default_factory=list)
    dhcp_leases: list[DhcpLease] = field(default_factory=list)
    reboot_required: bool = False
    system_logs: list[str] = field(default_factory=list)
    ip_neighbors: list[IpNeighbor] = field(default_factory=list)
    mwan_status: list[MwanStatus] = field(default_factory=list)
    wps_status: WpsStatus = field(default_factory=WpsStatus)
    adblock: AdBlockStatus = field(default_factory=AdBlockStatus)
    adblock_fast: SimpleAdBlockStatus = field(default_factory=SimpleAdBlockStatus)
    simple_adblock: SimpleAdBlockStatus = field(default_factory=SimpleAdBlockStatus)
    ban_ip: BanIpStatus = field(default_factory=BanIpStatus)
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
    firmware_install_url: str = ""
    firmware_checksum: str = ""
    is_custom_build: bool = False
    installed_packages: list[str] = field(default_factory=list)
    asu_supported: bool = False
    asu_update_available: bool = False
    asu_image_status: str = ""  # e.g. "available", "building", "failed"
    asu_image_url: str | None = None
    lldp_neighbors: list[LldpNeighbor] = field(default_factory=list)
    qmodem_info: QModemInfo = field(default_factory=QModemInfo)
    wireguard_interfaces: list[WireGuardInterface] = field(default_factory=list)
    upnp_mappings: list[UpnpMapping] = field(default_factory=list)
    nlbwmon_traffic: dict[str, NlbwmonTraffic] = field(default_factory=dict)
    wifi_credentials: list[WifiCredentials] = field(default_factory=list)
    sqm: list[SqmStatus] = field(default_factory=list)
    packages: OpenWrtPackages = field(default_factory=OpenWrtPackages)
    permissions: OpenWrtPermissions = field(default_factory=OpenWrtPermissions)


@dataclass
class DiagnosticResult:
    """Result of a single diagnostic check."""

    name: str
    status: str  # "PASS", "FAIL", "WARN", "INFO"
    message: str
    details: str | None = None
    remedy: str | None = None


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
        self._logread_flag: str | None = None
        self.packages = OpenWrtPackages()
        self.coordinator: Any = None

    async def _get_logread_command(self, count: int) -> str:
        """Resolve the correct logread command (detecting -n vs -l)."""
        if self._logread_flag is None:
            # Default to -n as it's the legacy/standard OpenWrt behavior
            self._logread_flag = "-n"
            try:
                # Test which flag is supported by running help
                help_out = await self.execute_command("/sbin/logread --help 2>&1")
                if help_out:
                    # Look for -l (modern BusyBox/OpenWrt 25+)
                    # Use a flexible regex to handle tabs, spaces, and different placeholders
                    if "-l" in help_out:
                        self._logread_flag = "-l"
                        _LOGGER.debug(
                            "Detected logread -l support (modern OpenWrt/BusyBox)"
                        )
                    elif "-n" in help_out:
                        self._logread_flag = "-n"
                        _LOGGER.debug("Confirmed logread -n support (standard OpenWrt)")
                    else:
                        _LOGGER.debug(
                            "Unknown logread help format, falling back to %s",
                            self._logread_flag,
                        )
                else:
                    _LOGGER.debug("logread --help returned no output, using default -n")
            except Exception as err:
                _LOGGER.debug(
                    "Could not verify logread flag support, defaulting to -n: %s", err
                )

        # Use absolute path and ensure count is an integer
        return f"/sbin/logread {self._logread_flag} {int(count or 10)}"

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

    def _calculate_cpu_usage(self, proc_stat: str) -> float:
        """Calculate CPU usage percentage from /proc/stat.

        Formula:
        Idle = idle + iowait
        NonIdle = user + nice + system + irq + softirq + steal
        Total = Idle + NonIdle

        Percentage = (Total - Idle) / Total
        """
        if not proc_stat or not isinstance(proc_stat, str):
            return 0.0

        # Basic validation: must start with 'cpu ' and have enough parts
        if not proc_stat.startswith("cpu "):
            _LOGGER.debug(
                "Invalid /proc/stat data received for CPU calculation: %s...",
                proc_stat[:50],
            )
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

        except (ValueError, IndexError) as err:
            _LOGGER.debug("Error parsing /proc/stat for CPU usage: %s", err)
            return 0.0

    async def get_gateway_mac(self) -> str | None:
        """Get the default gateway MAC address."""
        try:
            # 1. Get default gateway IP
            route_out = await self.execute_command("ip route show default 2>/dev/null")
            if not route_out:
                return None

            # Example: default via 192.168.178.1 dev eth0 proto static
            parts = route_out.split()
            if "via" not in parts:
                return None

            gw_ip = parts[parts.index("via") + 1]

            # 2. Get MAC for that IP
            neigh_out = await self.execute_command(f"ip neigh show {gw_ip} 2>/dev/null")
            if "lladdr" in neigh_out:
                neigh_parts = neigh_out.split()
                return neigh_parts[neigh_parts.index("lladdr") + 1].upper()
        except Exception as err:
            _LOGGER.debug("Failed to get gateway MAC: %s", err)
        return None

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

    def _parse_ip_neigh_line(self, line: str) -> IpNeighbor | None:
        """Parse a single line from 'ip neigh show'."""
        parts = line.split()
        if len(parts) < 4:
            return None

        ip, mac, interface, state = parts[0], "", "", parts[-1]

        # Filter out invalid or inactive states
        # LuCI typically only considers REACHABLE, DELAY, PROBE, and PERMANENT as active.
        # STALE means the device was seen recently but hasn't responded to the last probe.
        if state in ("FAILED", "INCOMPLETE", "STALE"):
            return None

        if "lladdr" in parts:
            idx = parts.index("lladdr")
            if len(parts) > idx + 1:
                mac = parts[idx + 1].lower()
        if "dev" in parts:
            idx = parts.index("dev")
            if len(parts) > idx + 1:
                interface = parts[idx + 1]

        if mac:
            return IpNeighbor(ip=ip, mac=mac, interface=interface, state=state)
        return None

    async def _get_neighbors_proc_arp(self, neighbors: list[IpNeighbor]) -> None:
        """Fetch neighbors from /proc/net/arp."""
        try:
            content = await self.execute_command("cat /proc/net/arp 2>/dev/null")
            if content:
                lines = content.strip().split("\n")
                if len(lines) > 1:
                    for line in lines[1:]:
                        parts = line.split()
                        if len(parts) >= 4:
                            neighbors.append(
                                IpNeighbor(
                                    ip=parts[0],
                                    mac=parts[3].lower(),
                                    interface=parts[5] if len(parts) > 5 else "",
                                    state="REACHABLE",
                                ),
                            )
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

    async def trigger_wps_push(self, interface: str) -> bool:
        """Trigger WPS push button on a specific wireless interface."""
        return False

    async def set_led(self, name: str, brightness: int) -> bool:
        """Set LED brightness (0-255)."""
        return False

    async def get_system_logs(self, count: int = 10) -> list[str]:
        """Get recent system log entries."""
        return []

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
    async def install_firmware(self, url: str, keep_settings: bool = True) -> None:
        """Install firmware from the given URL."""
        raise NotImplementedError

    @abc.abstractmethod
    async def get_installed_packages(self) -> list[str]:
        """Get a list of installed packages on the device."""
        raise NotImplementedError

    @abc.abstractmethod
    async def perform_diagnostics(self) -> list[DiagnosticResult]:
        """Perform a suite of diagnostic checks to identify configuration issues."""
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
                                f"ip link show {iface_name} 2>/dev/null",
                            )
                            vpn.up = bool(ip_out and "UP" in ip_out)

                            # Get RX/TX bytes
                            rx_out = await self.execute_command(
                                f"cat /sys/class/net/{iface_name}/statistics/rx_bytes 2>/dev/null",
                            )
                            tx_out = await self.execute_command(
                                f"cat /sys/class/net/{iface_name}/statistics/tx_bytes 2>/dev/null",
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
                            except (
                                ValueError,
                                AttributeError,
                            ):
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
                                        vpn.latest_handshake = max(
                                            vpn.latest_handshake, handshake
                                        )
                                    break
        except Exception as err:
            _LOGGER.debug("WireGuard status check failed: %s", err)

        try:
            # Try OpenVPN
            output = await self.execute_command("pgrep -a openvpn 2>/dev/null")
            if output and "not found" not in output.lower() and output.strip():
                # OpenVPN is running – check interfaces
                tun_output = await self.execute_command(
                    "ip -br link show type tun 2>/dev/null",
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
                                f"cat /sys/class/net/{iface_name}/statistics/rx_bytes 2>/dev/null",
                            )
                            tx_out = await self.execute_command(
                                f"cat /sys/class/net/{iface_name}/statistics/tx_bytes 2>/dev/null",
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
                            except (
                                ValueError,
                                AttributeError,
                            ):
                                pass
                            vpn_interfaces.append(vpn)
        except Exception as err:
            _LOGGER.debug("OpenVPN status check failed: %s", err)

        return vpn_interfaces

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

    async def get_banip_status(self) -> BanIpStatus:
        """Get status of the ban-ip package."""
        return BanIpStatus()

    async def set_banip_enabled(self, enabled: bool) -> bool:
        """Enable/disable the ban-ip service."""
        return False

    async def get_latency(self, target: str = "8.8.8.8") -> LatencyResult:
        """Measure network latency via ping."""
        result = LatencyResult(target=target)
        try:
            output = await self.execute_command(f"ping -c 3 -W 2 {target}")
            if output:
                # Only mark as available if we actually got output
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

    @abc.abstractmethod
    async def download_file(self, remote_path: str, local_path: str) -> bool:
        """Download a file from the router to a local path."""
        raise NotImplementedError

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

    async def get_all_data(self, is_full_poll: bool = False) -> OpenWrtData:
        """Fetch all data from OpenWrt in parallel blocks with robust fallbacks."""
        # Force full poll every 30 cycles or if we have no cached device info yet
        if self._cached_device_info is None or self._poll_count % 30 == 0:
            is_full_poll = True

        self._poll_count += 1
        data = (self.coordinator.data if self.coordinator else None) or OpenWrtData()

        def get_val(res: Any, default: Any, name: str = "") -> Any:
            """Safely get value from gather result, keeping previous data on failure."""
            if isinstance(res, Exception):
                if name:
                    _LOGGER.debug("Fetch of %s failed: %s", name, res)
                return default
            return res if res is not None else default

        # 1. Core data (Fast block) - Essential for basic functionality and trackers
        core_tasks = [
            self.get_system_resources(),
            self.get_network_interfaces(),
            self.get_connected_devices(),
            self.get_local_macs(),
            self.get_local_ips(),
        ]
        core_results = await asyncio.gather(*core_tasks, return_exceptions=True)

        data.system_resources = get_val(
            core_results[0], data.system_resources, "system_resources"
        )
        data.network_interfaces = get_val(
            core_results[1], data.network_interfaces, "network_interfaces"
        )
        data.connected_devices = get_val(
            core_results[2], data.connected_devices, "connected_devices"
        )
        data.local_macs = get_val(core_results[3], data.local_macs, "local_macs")
        data.local_ips = get_val(core_results[4], data.local_ips, "local_ips")

        # 2. Slow-changing optional data (Full Poll) - Reduces router load
        if is_full_poll:
            slow_optional_tasks = {
                "device_info": self.get_device_info(),
                "services": self.get_services(),
                "leds": self.get_leds(),
                "firewall_redirects": self.get_firewall_redirects(),
                "firewall_rules": self.get_firewall_rules(),
                "access_control": self.get_access_control(),
                "sqm": self.get_sqm_status(),
                "wireguard": self.get_wireguard_interfaces(),
                "packages": self.check_packages(),
                "permissions": self.check_permissions(),
                "reboot_required": self.is_reboot_required(),
                "system_logs": self.get_system_logs(count=10),
            }

            keys = list(slow_optional_tasks.keys())
            slow_results = await asyncio.gather(
                *[slow_optional_tasks[k] for k in keys], return_exceptions=True
            )
            slow_map = dict(zip(keys, slow_results, strict=False))

            data.device_info = get_val(
                slow_map["device_info"], data.device_info, "device_info"
            )
            data.services = get_val(slow_map["services"], data.services, "services")
            data.leds = get_val(slow_map["leds"], data.leds, "LEDs")
            data.firewall_redirects = get_val(
                slow_map["firewall_redirects"],
                data.firewall_redirects,
                "firewall redirects",
            )
            data.firewall_rules = get_val(
                slow_map["firewall_rules"], data.firewall_rules, "firewall rules"
            )
            data.access_control = get_val(
                slow_map["access_control"], data.access_control, "access control"
            )
            data.sqm = get_val(slow_map["sqm"], data.sqm, "SQM")
            data.wireguard_interfaces = get_val(
                slow_map["wireguard"], data.wireguard_interfaces, "wireguard"
            )
            data.packages = get_val(slow_map["packages"], data.packages, "packages")
            data.permissions = get_val(
                slow_map["permissions"], data.permissions, "permissions"
            )
            data.reboot_required = get_val(
                slow_map["reboot_required"], data.reboot_required, "reboot required"
            )
            data.system_logs = get_val(
                slow_map["system_logs"], data.system_logs, "system logs"
            )

            self._cached_device_info = data.device_info
            self._cached_slow_data = {
                k: data.__dict__.get(k)
                for k in [
                    "services",
                    "leds",
                    "firewall_redirects",
                    "firewall_rules",
                    "access_control",
                    "sqm",
                    "wireguard_interfaces",
                    "packages",
                    "permissions",
                    "reboot_required",
                    "system_logs",
                ]
            }
        else:
            # Reuse cached data on fast polls
            if self._cached_device_info:
                data.device_info = self._cached_device_info

            cached = getattr(self, "_cached_slow_data", {})
            for k, v in cached.items():
                if hasattr(data, k) and v is not None:
                    setattr(data, k, v)

        # 3. Dynamic always-fresh data (Dynamic block)
        dynamic_tasks = {
            "ip_neighbors": self.get_ip_neighbors(),
            "mwan": self.get_mwan_status(),
            "qmodem": self.get_qmodem_info(),
            "vpn": self.get_vpn_status(),
            "latency": self.get_latency(),
            "external_ip": self.get_external_ip(),
            "gateway_mac": self.get_gateway_mac(),
            "wifi_credentials": self.get_wifi_credentials(),
        }

        if data.packages.wireless is not False:
            dynamic_tasks["wireless"] = self.get_wireless_interfaces()
            dynamic_tasks["wps"] = self.get_wps_status()
        if data.packages.dhcp is not False:
            dynamic_tasks["dhcp"] = self.get_dhcp_leases()
        if data.packages.lldp is not False:
            dynamic_tasks["lldp"] = self.get_lldp_neighbors()
        if data.packages.miniupnpd is not False:
            dynamic_tasks["upnp"] = self.get_upnp_mappings()
        if data.packages.adblock:
            dynamic_tasks["adblock"] = self.get_adblock_status()
        if data.packages.simple_adblock:
            dynamic_tasks["simple_adblock"] = self.get_simple_adblock_status()
        if data.packages.ban_ip:
            dynamic_tasks["ban_ip"] = self.get_banip_status()

        dyn_keys = list(dynamic_tasks.keys())
        dyn_results = await asyncio.gather(
            *[dynamic_tasks[k] for k in dyn_keys], return_exceptions=True
        )
        dyn_map = dict(zip(dyn_keys, dyn_results, strict=False))

        data.ip_neighbors = get_val(
            dyn_map.get("ip_neighbors"), data.ip_neighbors, "IP neighbors"
        )
        data.mwan_status = get_val(dyn_map.get("mwan"), data.mwan_status, "MWAN")
        data.qmodem_info = get_val(dyn_map.get("qmodem"), data.qmodem_info, "modem")
        data.vpn_interfaces = get_val(dyn_map.get("vpn"), data.vpn_interfaces, "VPN")
        data.latency = get_val(dyn_map.get("latency"), data.latency, "latency")
        data.external_ip = get_val(
            dyn_map.get("external_ip"), data.external_ip, "external IP"
        )
        if data.device_info:
            data.device_info.gateway_mac = get_val(
                dyn_map.get("gateway_mac"), data.device_info.gateway_mac, "gateway MAC"
            )
        data.wifi_credentials = get_val(
            dyn_map.get("wifi_credentials"), data.wifi_credentials, "WiFi credentials"
        )

        if "wireless" in dyn_map:
            data.wireless_interfaces = get_val(
                dyn_map["wireless"], data.wireless_interfaces, "wireless"
            )
        if "wps" in dyn_map:
            data.wps_status = get_val(dyn_map["wps"], data.wps_status, "WPS")
        if "dhcp" in dyn_map:
            data.dhcp_leases = get_val(dyn_map["dhcp"], data.dhcp_leases, "DHCP")
        if "lldp" in dyn_map:
            data.lldp_neighbors = get_val(dyn_map["lldp"], data.lldp_neighbors, "LLDP")
        if "upnp" in dyn_map:
            data.upnp_mappings = get_val(dyn_map["upnp"], data.upnp_mappings, "UPnP")
        if "adblock" in dyn_map:
            data.adblock = get_val(dyn_map["adblock"], data.adblock, "adblock")
        if "simple_adblock" in dyn_map:
            data.simple_adblock = get_val(
                dyn_map["simple_adblock"], data.simple_adblock, "simple adblock"
            )
        if "ban_ip" in dyn_map:
            data.ban_ip = get_val(dyn_map["ban_ip"], data.ban_ip, "ban-ip")

        # Populate MAC address for device info if missing
        if data.device_info and not data.device_info.mac_address:
            # Try br-lan first, then eth0, then anything with a valid MAC
            mac_map = {
                iface.name: iface.mac_address
                for iface in data.network_interfaces
                if iface.mac_address and iface.mac_address != "00:00:00:00:00:00"
            }
            if "br-lan" in mac_map:
                data.device_info.mac_address = mac_map["br-lan"]
            elif "eth0" in mac_map:
                data.device_info.mac_address = mac_map["eth0"]
            elif mac_map:
                # Pick the first non-zero MAC
                data.device_info.mac_address = next(iter(mac_map.values()))

        return data
