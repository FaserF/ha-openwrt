"""Tests for Service Management (Restart/Stop)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.openwrt.api.base import OpenWrtData, OpenWrtPackages
from custom_components.openwrt.api.luci_rpc import LuciRpcClient
from custom_components.openwrt.api.ssh import SshClient
from custom_components.openwrt.api.ubus import UbusClient, UbusError


@pytest.mark.asyncio
async def test_ubus_manage_service():
    """Test managing service via Ubus (rc.init)."""
    client = UbusClient(
        MagicMock(),
        MagicMock(),
        host="192.168.1.1",
        username="root",
        password="password",
    )
    client._connected = True

    with patch.object(client, "_call", new_callable=AsyncMock) as mock_call:
        success = await client.manage_service("dnsmasq", "restart")
        assert success is True
        mock_call.assert_called_with(
            "rc",
            "init",
            {"name": "dnsmasq", "action": "restart"},
        )


@pytest.mark.asyncio
async def test_ssh_manage_service():
    """Test managing service via SSH."""
    client = SshClient(
        MagicMock(),
        MagicMock(),
        host="192.168.1.1",
        username="root",
        password="password",
    )
    client._connected = True

    with patch.object(client, "_exec", new_callable=AsyncMock) as mock_exec:
        success = await client.manage_service("firewall", "stop")
        assert success is True
        mock_exec.assert_called_with("/etc/init.d/firewall stop")


@pytest.mark.asyncio
async def test_luci_rpc_manage_service():
    """Test managing service via LuCI RPC."""
    client = LuciRpcClient(
        MagicMock(),
        MagicMock(),
        host="192.168.1.1",
        username="root",
        password="password",
    )
    client._session_id = "test_token"
    client._connected = True

    with patch.object(client, "_rpc_call", new_callable=AsyncMock) as mock_rpc:
        success = await client.manage_service("network", "reload")
        assert success is True
        mock_rpc.assert_called_with("sys", "exec", ["/etc/init.d/network reload"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("client_cls", "transport"),
    [(UbusClient, "_call"), (SshClient, "_exec"), (LuciRpcClient, "_rpc_call")],
)
async def test_manage_service_forces_slow_poll(client_cls, transport):
    """A service action makes the next poll a slow one, so services are re-read."""
    client = client_cls(
        MagicMock(),
        MagicMock(),
        host="192.0.2.1",
        username="root",
        password="password",
    )
    client._session_id = "test_token"
    client._connected = True
    client._last_slow_poll_time = 1e12

    with patch.object(client, transport, new_callable=AsyncMock):
        assert await client.manage_service("adblock-fast", "stop") is True

    assert client._last_slow_poll_time == 0.0


@pytest.mark.asyncio
async def test_ubus_manage_service_fallback():
    """Test ubus manage_service fallback through all tiers."""
    client = UbusClient(
        MagicMock(),
        MagicMock(),
        host="192.168.1.1",
        username="root",
        password="password",
    )
    client._connected = True

    # Tier 1: rc.init fails -> Tier 2: file.exec fails -> Tier 3: execute_command
    with (
        patch.object(
            client,
            "_call",
            side_effect=[UbusError("rc fail"), Exception("file fail")],
        ) as mock_call,
        patch.object(
            client,
            "execute_command",
            new_callable=AsyncMock,
        ) as mock_exec,
    ):
        success = await client.manage_service("sqm", "start")
        assert success is True
        assert mock_call.call_count == 2
        mock_exec.assert_called_with("/etc/init.d/sqm start")


def _stub_fetchers(client: UbusClient) -> None:
    """Stub every fetch get_all_data makes."""
    for name in (
        "get_system_resources",
        "get_device_info",
        "get_qmodem_info",
        "get_latency",
        "get_external_ip",
        "get_gateway_mac",
        "get_wps_status",
    ):
        setattr(client, name, AsyncMock())
    for name in (
        "get_network_interfaces",
        "get_connected_devices",
        "get_services",
        "get_leds",
        "get_firewall_redirects",
        "get_firewall_rules",
        "get_access_control",
        "get_sqm_status",
        "get_wireguard_interfaces",
        "get_system_logs",
        "get_ip_neighbors",
        "get_vpn_status",
        "get_wifi_credentials",
        "get_dhcp_leases",
        "get_lldp_neighbors",
        "get_upnp_mappings",
    ):
        setattr(client, name, AsyncMock(return_value=[]))
    client.get_local_macs = AsyncMock(return_value=set())
    client.get_local_ips = AsyncMock(return_value=set())
    client.check_packages = AsyncMock(return_value=OpenWrtPackages())
    client.check_permissions = AsyncMock()
    client.is_reboot_required = AsyncMock(return_value=False)
    client._call = AsyncMock(return_value={})
    client.execute_command = AsyncMock(return_value="")
    client.read_file = AsyncMock(return_value=None)


@pytest.mark.asyncio
async def test_action_during_slow_poll_forces_another():
    """An action that lands while a slow poll is in flight is not lost."""
    client = UbusClient(
        MagicMock(),
        MagicMock(),
        host="192.0.2.1",
        username="root",
        password="password",
    )
    _stub_fetchers(client)
    client.coordinator = MagicMock()
    client.coordinator.data = OpenWrtData()

    async def services_read_during_an_action() -> list:
        client._invalidate_slow_poll()
        return []

    client.get_services = AsyncMock(side_effect=services_read_during_an_action)
    await client.get_all_data()
    assert client._last_slow_poll_time == 0.0

    client.get_services = AsyncMock(return_value=[])
    client.get_ip_neighbors = AsyncMock(return_value=[])
    await client.get_all_data()
    client.get_services.assert_awaited_once()
    # A slow poll also runs the medium tier, well inside its 180 s interval.
    client.get_ip_neighbors.assert_awaited_once()
    assert client._last_slow_poll_time > 0.0
