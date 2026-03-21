"""Tests for Service Management (Restart/Stop)."""

from unittest.mock import AsyncMock, patch

import pytest

from custom_components.openwrt.api.luci_rpc import LuciRpcClient
from custom_components.openwrt.api.ssh import SshClient
from custom_components.openwrt.api.ubus import UbusClient, UbusError


@pytest.mark.asyncio
async def test_ubus_manage_service():
    """Test managing service via Ubus (rc.init)."""
    client = UbusClient(host="192.168.1.1", username="root", password="password")
    client._connected = True

    with patch.object(client, "_call", new_callable=AsyncMock) as mock_call:
        success = await client.manage_service("dnsmasq", "restart")
        assert success is True
        mock_call.assert_called_with(
            "rc", "init", {"name": "dnsmasq", "action": "restart"}
        )


@pytest.mark.asyncio
async def test_ssh_manage_service():
    """Test managing service via SSH."""
    client = SshClient(host="192.168.1.1", username="root", password="password")
    client._connected = True

    with patch.object(client, "_exec", new_callable=AsyncMock) as mock_exec:
        success = await client.manage_service("firewall", "stop")
        assert success is True
        mock_exec.assert_called_with("/etc/init.d/firewall stop")


@pytest.mark.asyncio
async def test_luci_rpc_manage_service():
    """Test managing service via LuCI RPC."""
    client = LuciRpcClient(host="192.168.1.1", username="root", password="password")
    client._session_id = "test_token"
    client._connected = True

    with patch.object(client, "_rpc_call", new_callable=AsyncMock) as mock_rpc:
        success = await client.manage_service("network", "reload")
        assert success is True
        mock_rpc.assert_called_with("sys", "exec", ["/etc/init.d/network reload"])


@pytest.mark.asyncio
async def test_ubus_manage_service_fallback():
    """Test ubus manage_service fallback through all tiers."""
    client = UbusClient(host="192.168.1.1", username="root", password="password")
    client._connected = True

    # Tier 1: rc.init fails -> Tier 2: file.exec fails -> Tier 3: execute_command
    with patch.object(
        client, "_call", side_effect=[UbusError("rc fail"), Exception("file fail")]
    ) as mock_call:
        with patch.object(
            client, "execute_command", new_callable=AsyncMock
        ) as mock_exec:
            success = await client.manage_service("sqm", "start")
            assert success is True
            assert mock_call.call_count == 2
            mock_exec.assert_called_with("/etc/init.d/sqm start")
