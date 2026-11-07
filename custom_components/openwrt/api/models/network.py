"""Network data models for OpenWrt."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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
    interface_enabled: bool = True
    up: bool = False
    radio: str = ""
    radio_enabled: bool = True
    htmode: str = ""
    txpower: int = 0
    txpower_offset: int = 0
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

    @staticmethod
    def _uci_disabled(value: Any) -> bool:
        """Return whether a UCI disabled option is active."""
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

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
    fdb_age: float | None = None
    uptime: int = 0


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
class NlbwmonTraffic:
    """Traffic statistics for a specific MAC address from nlbwmon."""

    mac: str = ""
    rx_bytes: int = 0
    tx_bytes: int = 0
    rx_packets: int = 0
    tx_packets: int = 0
