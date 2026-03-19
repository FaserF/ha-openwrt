"""Unit tests for MAC address retrieval logic in OpenWrtClient."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.openwrt.api.base import (
    AccessControl,
    ConnectedDevice,
    DeviceInfo,
    DhcpLease,
    FirewallRedirect,
    FirewallRule,
    IpNeighbor,
    LatencyResult,
    LedInfo,
    MwanStatus,
    NetworkInterface,
    OpenWrtClient,
    OpenWrtPackages,
    OpenWrtPermissions,
    QModemInfo,
    ServiceInfo,
    SqmStatus,
    SystemResources,
    VpnInterface,
    WirelessInterface,
    WpsStatus,
)


class MockClient(OpenWrtClient):
    """A concrete implementation of OpenWrtClient for testing."""

    def __init__(self, host, username, password):
        super().__init__(host, username, password)

    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None:
        pass

    async def get_device_info(self) -> DeviceInfo:
        return DeviceInfo()

    async def get_system_resources(self) -> SystemResources:
        return SystemResources()

    async def get_network_interfaces(self) -> list[NetworkInterface]:
        return []

    async def get_connected_devices(self) -> list[ConnectedDevice]:
        return []

    async def get_wireless_interfaces(self) -> list[WirelessInterface]:
        return []

    async def get_dhcp_leases(self) -> list[DhcpLease]:
        return []

    async def get_ip_neighbors(self) -> list[IpNeighbor]:
        return []

    async def get_services(self) -> list[ServiceInfo]:
        return []

    async def manage_service(self, name: str, action: str) -> bool:
        return True

    async def reboot(self) -> bool:
        return True

    async def set_wireless_enabled(self, interface: str, enabled: bool) -> bool:
        return True

    async def get_firewall_redirects(self) -> list[FirewallRedirect]:
        return []

    async def get_firewall_rules(self) -> list[FirewallRule]:
        return []

    async def get_access_control(self) -> list[AccessControl]:
        return []

    async def get_sqm_status(self) -> list[SqmStatus]:
        return []

    async def set_sqm_config(self, section_id: str, **kwargs: Any) -> bool:
        return True

    async def set_firewall_rule_enabled(self, section_id: str, enabled: bool) -> bool:
        return True

    async def set_firewall_redirect_enabled(
        self, section_id: str, enabled: bool
    ) -> bool:
        return True

    async def check_packages(self) -> OpenWrtPackages:
        return MagicMock()

    async def check_permissions(self) -> OpenWrtPermissions:
        return MagicMock()

    async def execute_command(self, command: str) -> str:
        return ""

    async def get_installed_packages(self) -> list[str]:
        return []

    async def provision_user(self, username: str, password: str) -> tuple[bool, str | None]:
        return True, None

    async def get_external_ip(self) -> str | None:
        return None

    async def get_latency(self, target: str = "8.8.8.8") -> LatencyResult:
        return MagicMock()

    async def create_backup(self) -> str:
        return ""

    async def get_qmodem_info(self) -> QModemInfo:
        return MagicMock()

    async def get_wps_status(self) -> WpsStatus:
        return MagicMock()

    async def get_vpn_status(self) -> list[VpnInterface]:
        return []

    async def get_leds(self) -> list[LedInfo]:
        return []

    async def set_led(self, name: str, brightness: int) -> bool:
        return True

    async def install_firmware(self, url: str, keep_settings: bool = True) -> None:
        pass

    async def download_file(self, remote_path: str, local_path: str) -> bool:
        return True

    async def get_mwan_status(self) -> list[MwanStatus]:
        return []


@pytest.mark.asyncio
async def test_get_all_data_populates_mac_address() -> None:
    """Test that get_all_data automatically populates device_info.mac_address."""
    client = MockClient("192.168.1.1", "root", "pass")

    # Mock core data methods
    client.get_device_info = AsyncMock(return_value=DeviceInfo(hostname="OpenWrt"))
    client.get_system_resources = AsyncMock()
    client.get_network_interfaces = AsyncMock(
        return_value=[
            NetworkInterface(name="lo", mac_address="00:00:00:00:00:00"),
            NetworkInterface(name="br-lan", mac_address="AA:BB:CC:DD:EE:FF"),
            NetworkInterface(name="eth0", mac_address="11:22:33:44:55:66"),
        ]
    )
    client.get_connected_devices = AsyncMock(return_value=[])

    # Mock optional data methods to return empty lists/defaults
    client.get_wireless_interfaces = AsyncMock(return_value=[])
    client.get_dhcp_leases = AsyncMock(return_value=[])
    client.get_ip_neighbors = AsyncMock(return_value=[])
    client.get_mwan_status = AsyncMock(return_value=[])
    client.get_wps_status = AsyncMock()
    client.get_qmodem_info = AsyncMock()
    client.get_vpn_status = AsyncMock(return_value=[])
    client.get_latency = AsyncMock()
    client.get_external_ip = AsyncMock(return_value=None)
    client.get_services = AsyncMock(return_value=[])
    client.get_leds = AsyncMock(return_value=[])
    client.get_firewall_redirects = AsyncMock(return_value=[])
    client.get_firewall_rules = AsyncMock(return_value=[])
    client.get_access_control = AsyncMock(return_value=[])
    client.get_sqm_status = AsyncMock(return_value=[])
    client.check_packages = AsyncMock()
    client.check_permissions = AsyncMock()

    # Call get_all_data (this is a full poll by default since _poll_count starts at 0)
    data = await client.get_all_data()

    # Verify MAC address was populated from br-lan
    assert data.device_info.mac_address == "AA:BB:CC:DD:EE:FF"


@pytest.mark.asyncio
async def test_get_all_data_falls_back_to_eth0() -> None:
    """Test that get_all_data falls back to eth0 if br-lan is missing."""
    client = MockClient("192.168.1.1", "root", "pass")

    client.get_device_info = AsyncMock(return_value=DeviceInfo(hostname="OpenWrt"))
    client.get_system_resources = AsyncMock()
    client.get_network_interfaces = AsyncMock(
        return_value=[
            NetworkInterface(name="eth0", mac_address="11:22:33:44:55:66"),
        ]
    )
    client.get_connected_devices = AsyncMock(return_value=[])

    # Mock other methods...
    for method in [
        "get_wireless_interfaces",
        "get_dhcp_leases",
        "get_ip_neighbors",
        "get_mwan_status",
        "get_wps_status",
        "get_qmodem_info",
        "get_vpn_status",
        "get_latency",
        "get_external_ip",
        "get_services",
        "get_leds",
        "get_firewall_redirects",
        "get_firewall_rules",
        "get_access_control",
        "get_sqm_status",
        "check_packages",
        "check_permissions",
    ]:
        setattr(client, method, AsyncMock())

    data = await client.get_all_data()

    assert data.device_info.mac_address == "11:22:33:44:55:66"
