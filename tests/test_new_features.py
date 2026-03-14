"""Tests for the new features added to the OpenWrt integration."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from custom_components.openwrt.api.base import (
    ConnectedDevice,
    DhcpLease,
    LatencyResult,
    NetworkInterface,
    OpenWrtData,
    SystemResources,
    VpnInterface,
    WirelessInterface,
)


def _make_data(**kwargs) -> OpenWrtData:
    """Create a default OpenWrtData with overrides."""
    defaults = {
        "system_resources": SystemResources(
            uptime=120, memory_total=1000, memory_used=500, load_1min=0.1
        ),
        "connected_devices": [],
        "network_interfaces": [],
        "wireless_interfaces": [],
    }
    defaults.update(kwargs)
    return OpenWrtData(**defaults)


def _make_coordinator(data: OpenWrtData) -> MagicMock:
    """Create a mock coordinator with given data."""
    coordinator = MagicMock()
    coordinator.data = data
    return coordinator


def _make_entry() -> MagicMock:
    """Create a mock config entry."""
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.data = {"host": "192.168.1.1"}
    entry.options = {}
    return entry


# ----- VPN Data Model Tests -----


class TestVpnInterface:
    """Test VPN interface data model."""

    def test_defaults(self) -> None:
        """Test VPN interface defaults."""
        vpn = VpnInterface()
        assert vpn.name == ""
        assert vpn.type == ""
        assert vpn.up is False
        assert vpn.rx_bytes == 0
        assert vpn.tx_bytes == 0
        assert vpn.peers == 0
        assert vpn.latest_handshake == 0

    def test_wireguard(self) -> None:
        """Test WireGuard VPN interface."""
        vpn = VpnInterface(
            name="wg0",
            type="wireguard",
            up=True,
            peers=3,
            rx_bytes=1024000,
            tx_bytes=2048000,
            latest_handshake=1700000000,
        )
        assert vpn.name == "wg0"
        assert vpn.type == "wireguard"
        assert vpn.up is True
        assert vpn.peers == 3
        assert vpn.rx_bytes == 1024000
        assert vpn.tx_bytes == 2048000

    def test_openvpn(self) -> None:
        """Test OpenVPN interface."""
        vpn = VpnInterface(name="tun0", type="openvpn", up=True)
        assert vpn.type == "openvpn"
        assert vpn.up is True


# ----- Latency Data Model Tests -----


class TestLatencyResult:
    """Test latency result data model."""

    def test_defaults(self) -> None:
        """Test latency defaults."""
        result = LatencyResult()
        assert result.target == ""
        assert result.latency_ms is None
        assert result.packet_loss == 0.0
        assert result.available is False

    def test_successful_ping(self) -> None:
        """Test successful latency measurement."""
        result = LatencyResult(
            target="8.8.8.8",
            latency_ms=12.5,
            packet_loss=0.0,
            available=True,
        )
        assert result.latency_ms == 12.5
        assert result.available is True

    def test_packet_loss(self) -> None:
        """Test latency with packet loss."""
        result = LatencyResult(
            target="8.8.8.8",
            latency_ms=50.3,
            packet_loss=33.0,
            available=True,
        )
        assert result.packet_loss == 33.0


# ----- OpenWrtData Tests (new fields) -----


class TestOpenWrtDataNewFields:
    """Test that OpenWrtData has the new fields."""

    def test_vpn_default(self) -> None:
        """Test VPN interfaces default to empty list."""
        data = OpenWrtData()
        assert data.vpn_interfaces == []

    def test_latency_default(self) -> None:
        """Test latency default."""
        data = OpenWrtData()
        assert data.latency.target == ""
        assert data.latency.latency_ms is None
        assert data.latency.available is False

    def test_vpn_populated(self) -> None:
        """Test VPN interfaces populated."""
        data = OpenWrtData(
            vpn_interfaces=[
                VpnInterface(name="wg0", type="wireguard", up=True, peers=2),
                VpnInterface(name="tun0", type="openvpn", up=False),
            ]
        )
        assert len(data.vpn_interfaces) == 2
        assert data.vpn_interfaces[0].peers == 2

    def test_latency_populated(self) -> None:
        """Test latency data populated."""
        data = OpenWrtData(
            latency=LatencyResult(target="8.8.8.8", latency_ms=12.5, available=True)
        )
        assert data.latency.latency_ms == 12.5


# ----- Quick Win Tests -----


class TestQuickWinSensors:
    """Test that quick-win sensors expose already-collected data."""

    def test_network_interface_ipv6(self) -> None:
        """Test IPv6 address is available in network interface model."""
        iface = NetworkInterface(
            name="wan",
            ipv4_address="192.168.1.1",
            ipv6_address="2001:db8::1",
        )
        assert iface.ipv6_address == "2001:db8::1"

    def test_network_interface_speed_duplex(self) -> None:
        """Test speed and duplex fields in network interface model."""
        iface = NetworkInterface(name="lan", speed="1000", duplex="full")
        assert iface.speed == "1000"
        assert iface.duplex == "full"

    def test_network_interface_dns_servers(self) -> None:
        """Test DNS servers field in network interface model."""
        iface = NetworkInterface(name="wan", dns_servers=["8.8.8.8", "8.8.4.4"])
        assert len(iface.dns_servers) == 2

    def test_network_interface_uptime(self) -> None:
        """Test uptime field in network interface model."""
        iface = NetworkInterface(name="wan", uptime=3600)
        assert iface.uptime == 3600

    def test_wireless_noise_encryption_frequency(self) -> None:
        """Test noise, encryption, frequency fields in wireless interface model."""
        wifi = WirelessInterface(
            name="wlan0",
            noise=-90,
            encryption="WPA3-SAE",
            frequency="5GHz",
        )
        assert wifi.noise == -90
        assert wifi.encryption == "WPA3-SAE"
        assert wifi.frequency == "5GHz"

    def test_connected_device_connection_info(self) -> None:
        """Test connection_info field in connected device model."""
        device = ConnectedDevice(
            mac="AA:BB:CC:DD:EE:FF",
            connection_info="802.11ax",
            rx_bytes=1024000,
            tx_bytes=2048000,
            uptime=600,
        )
        assert device.connection_info == "802.11ax"
        assert device.rx_bytes == 1024000


# ----- DHCP Lease Tests -----


class TestDhcpLeaseCount:
    """Test DHCP lease count sensor data availability."""

    def test_dhcp_leases_count(self) -> None:
        """Test that DHCP lease count can be derived from data model."""
        data = OpenWrtData(
            dhcp_leases=[
                DhcpLease(hostname="pc1", mac="AA:BB:CC:DD:EE:01", ip="192.168.1.10"),
                DhcpLease(hostname="phone", mac="AA:BB:CC:DD:EE:02", ip="192.168.1.11"),
                DhcpLease(
                    hostname="laptop", mac="AA:BB:CC:DD:EE:03", ip="192.168.1.12"
                ),
            ]
        )
        assert len(data.dhcp_leases) == 3


# ----- Backup API Tests -----


class TestBackupApi:
    """Test backup API method exists."""

    @pytest.mark.asyncio
    async def test_create_backup_method_exists(self) -> None:
        """Test that create_backup method exists on OpenWrtClient."""
        from custom_components.openwrt.api.base import OpenWrtClient

        assert hasattr(OpenWrtClient, "create_backup")


# ----- VPN API Tests -----


class TestVpnApi:
    """Test VPN API methods."""

    @pytest.mark.asyncio
    async def test_get_vpn_status_method_exists(self) -> None:
        """Test that get_vpn_status method exists."""
        from custom_components.openwrt.api.base import OpenWrtClient

        assert hasattr(OpenWrtClient, "get_vpn_status")

    @pytest.mark.asyncio
    async def test_get_latency_method_exists(self) -> None:
        """Test that get_latency method exists."""
        from custom_components.openwrt.api.base import OpenWrtClient

        assert hasattr(OpenWrtClient, "get_latency")


# ----- Event Platform Tests -----


class TestEventPlatform:
    """Test event platform."""

    def test_event_module_importable(self) -> None:
        """Test that event module is importable."""
        from custom_components.openwrt import event  # noqa: F401


# ----- Number Platform Tests -----


class TestNumberPlatform:
    """Test number platform."""

    def test_number_module_importable(self) -> None:
        """Test that number module is importable."""
        from custom_components.openwrt import number  # noqa: F401


# ----- Const Tests -----


class TestConstUpdates:
    """Test constant updates."""

    def test_platforms_include_event(self) -> None:
        """Test that PLATFORMS includes event."""
        from custom_components.openwrt.const import PLATFORMS

        assert "event" in PLATFORMS

    def test_platforms_include_number(self) -> None:
        """Test that PLATFORMS includes number."""
        from custom_components.openwrt.const import PLATFORMS

        assert "number" in PLATFORMS

    def test_backup_service_constant(self) -> None:
        """Test that SERVICE_BACKUP constant exists."""
        from custom_components.openwrt.const import SERVICE_BACKUP

        assert SERVICE_BACKUP == "create_backup"


# ----- Coordinator Syntax Fix Test -----


class TestCoordinatorSyntaxFix:
    """Test coordinator syntax fix."""

    def test_version_comparison_import(self) -> None:
        """Test that coordinator module compiles without syntax error."""
        from custom_components.openwrt import coordinator  # noqa: F401
