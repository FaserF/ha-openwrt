"""Tests for Batman-adv mesh support."""

from unittest.mock import MagicMock, patch

import pytest

from custom_components.openwrt.api.luci_rpc import LuciRpcClient
from custom_components.openwrt.api.ssh import SshClient
from custom_components.openwrt.api.ubus import UbusClient

MOCK_BATMAN_O = """
 * 00:11:22:33:44:56    0.010s   (123) 00:11:22:33:44:56 [br-lan]
   00:11:22:33:44:57    0.050s   (45) 00:11:22:33:44:56 [br-lan]
"""

MOCK_BATMAN_N = """
[br-lan] 00:11:22:33:44:56    0.010s
"""

MOCK_BATMAN_GWL = """
* 00:11:22:33:44:56 (200) 00:11:22:33:44:56 [br-lan] 100/100
"""

MOCK_BATMAN_TG = """
 * 00:11:22:33:44:58   -1 [....]    0.000s 00:11:22:33:44:56 (0x1234)
   00:11:22:33:44:59   -1 [.P..]    0.010s 00:11:22:33:44:57 (0x8765)
"""


@pytest.mark.asyncio
async def test_ubus_get_batman_data():
    """Test get_batman_data via Ubus."""
    client = UbusClient(MagicMock(), MagicMock(), "192.168.1.1", "user", "pass")
    client.packages.batctl = True

    async def mock_execute(command):
        if "batctl o" in command:
            return MOCK_BATMAN_O
        if "batctl n" in command:
            return MOCK_BATMAN_N
        if "batctl gwl" in command:
            return MOCK_BATMAN_GWL
        if "batctl tg" in command:
            return MOCK_BATMAN_TG
        return ""

    with patch.object(client, "execute_command", side_effect=mock_execute):
        data = await client.get_batman_data()

        assert len(data["originators"]) == 2
        assert data["originators"][0].mac == "00:11:22:33:44:56"
        assert data["originators"][0].tq == 123
        assert data["originators"][1].mac == "00:11:22:33:44:57"
        assert data["originators"][1].tq == 45

        assert len(data["neighbors"]) == 1
        assert data["neighbors"][0].mac == "00:11:22:33:44:56"

        assert len(data["gateways"]) == 1
        assert data["gateways"][0].mac == "00:11:22:33:44:56"
        assert data["gateways"][0].is_selected is True

        assert len(data["translation_table"]) == 2
        assert data["translation_table"]["00:11:22:33:44:58"] == "00:11:22:33:44:56"
        assert data["translation_table"]["00:11:22:33:44:59"] == "00:11:22:33:44:57"
        assert data["mesh_active"] is True


@pytest.mark.asyncio
async def test_ssh_get_batman_data():
    """Test get_batman_data via SSH."""
    client = SshClient(MagicMock(), MagicMock(), "192.168.1.1", "user", "pass")
    client.packages.batctl = True

    async def mock_execute(command):
        if "batctl o" in command:
            return MOCK_BATMAN_O
        if "batctl n" in command:
            return MOCK_BATMAN_N
        if "batctl gwl" in command:
            return MOCK_BATMAN_GWL
        if "batctl tg" in command:
            return MOCK_BATMAN_TG
        return ""

    with patch.object(client, "execute_command", side_effect=mock_execute):
        data = await client.get_batman_data()

        assert len(data["originators"]) == 2
        assert data["originators"][0].mac == "00:11:22:33:44:56"
        assert data["originators"][0].tq == 123
        assert data["originators"][1].tq == 45
        assert data["translation_table"]["00:11:22:33:44:58"] == "00:11:22:33:44:56"
        assert data["mesh_active"] is True


@pytest.mark.asyncio
async def test_luci_get_batman_data():
    """Test get_batman_data via LuCI-RPC."""
    client = LuciRpcClient(MagicMock(), MagicMock(), "192.168.1.1", "user", "pass")
    client.packages.batctl = True

    async def mock_execute(command):
        if "batctl o" in command:
            return MOCK_BATMAN_O
        if "batctl n" in command:
            return MOCK_BATMAN_N
        if "batctl gwl" in command:
            return MOCK_BATMAN_GWL
        if "batctl tg" in command:
            return MOCK_BATMAN_TG
        return ""

    with patch.object(client, "execute_command", side_effect=mock_execute):
        data = await client.get_batman_data()

        assert len(data["originators"]) == 2
        assert data["originators"][0].mac == "00:11:22:33:44:56"
        assert data["originators"][0].tq == 123
        assert data["originators"][1].tq == 45
        assert data["translation_table"]["00:11:22:33:44:58"] == "00:11:22:33:44:56"
        assert data["mesh_active"] is True


@pytest.mark.parametrize("client_class", [UbusClient, SshClient, LuciRpcClient])
@pytest.mark.asyncio
async def test_get_all_data_batman_permissions_check(client_class) -> None:
    """Test that get_all_data correctly resolves permissions check for batman without AttributeError."""
    from unittest.mock import AsyncMock

    from custom_components.openwrt.api.base import (
        OpenWrtData,
        OpenWrtPackages,
        OpenWrtPermissions,
    )

    client = client_class(MagicMock(), MagicMock(), "192.168.1.1", "user", "pass")

    # Mock all core and slow task methods to avoid exceptions
    client.get_system_resources = AsyncMock()
    client.get_network_interfaces = AsyncMock(return_value=[])
    client.get_connected_devices = AsyncMock(return_value=[])
    client.get_local_macs = AsyncMock(return_value=set())
    client.get_local_ips = AsyncMock(return_value=set())
    client.get_device_info = AsyncMock()
    client.get_services = AsyncMock(return_value=[])
    client.get_leds = AsyncMock(return_value=[])
    client.get_firewall_redirects = AsyncMock(return_value=[])
    client.get_firewall_rules = AsyncMock(return_value=[])
    client.get_access_control = AsyncMock(return_value=[])
    client.get_sqm_status = AsyncMock(return_value=[])
    client.get_wireguard_interfaces = AsyncMock(return_value=[])
    client.check_packages = AsyncMock(return_value=OpenWrtPackages(batman_adv=True))
    client.check_permissions = AsyncMock(
        return_value=OpenWrtPermissions(read_batman=True)
    )
    client.is_reboot_required = AsyncMock(return_value=False)
    client.get_system_logs = AsyncMock(return_value=[])

    # Mock dynamic tasks methods
    client.get_ip_neighbors = AsyncMock(return_value=[])
    client.get_mwan_status = AsyncMock(return_value=[])
    client.get_qmodem_info = AsyncMock()
    client.get_vpn_status = AsyncMock(return_value=[])
    client.get_latency = AsyncMock()
    client.get_external_ip = AsyncMock()
    client.get_gateway_mac = AsyncMock()
    client.get_wifi_credentials = AsyncMock(return_value=[])
    client.get_dhcp_leases = AsyncMock(return_value=[])
    client.get_lldp_neighbors = AsyncMock(return_value=[])
    client.get_upnp_mappings = AsyncMock(return_value=[])
    client.get_wps_status = AsyncMock()

    # Mock low-level communication methods to prevent fallback warnings
    client._call = AsyncMock(return_value={})
    client._rpc_call = AsyncMock(return_value={})
    client.execute_command = AsyncMock(return_value="")
    client.read_file = AsyncMock(return_value=None)

    # Mock get_batman_data to return a dummy structure that populates OpenWrtData attributes
    dummy_batman = {
        "mesh_active": True,
        "originators": [],
        "neighbors": [],
        "gateways": [],
    }
    client.get_batman_data = AsyncMock(return_value=dummy_batman)

    # Set up a mock coordinator
    mock_coordinator = MagicMock()
    mock_coordinator.data = OpenWrtData()
    mock_coordinator.data.permissions = OpenWrtPermissions(read_batman=True)
    client.coordinator = mock_coordinator

    # This should not raise an AttributeError
    res = await client.get_all_data()
    assert res.batman_mesh_active is True
