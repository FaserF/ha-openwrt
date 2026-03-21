"""Tests for AdBlock, Simple AdBlock, and Ban-IP management."""

from unittest.mock import AsyncMock, patch
import pytest
import json
from custom_components.openwrt.api.ubus import UbusClient
from custom_components.openwrt.api.ssh import SshClient
from custom_components.openwrt.api.luci_rpc import LuciRpcClient
from custom_components.openwrt.api.base import (
    AdBlockStatus,
    SimpleAdBlockStatus,
    BanIpStatus,
)

@pytest.mark.asyncio
async def test_ubus_adblock_status():
    """Test getting AdBlock status via Ubus."""
    client = UbusClient(host="192.168.1.1", username="root", password="password")
    client._connected = True
    
    # Mock ubus call
    with patch.object(client, "_call", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = {
            "adblock_status": "enabled",
            "adblock_version": "4.1.5",
            "blocked_domains": 12345,
            "last_run": "2026-03-21 12:00:00"
        }
        
        status = await client.get_adblock_status()
        assert status.enabled is True
        assert status.status == "enabled"
        assert status.version == "4.1.5"
        assert status.blocked_domains == 12345
        mock_call.assert_called_with("adblock", "status")

@pytest.mark.asyncio
async def test_ubus_adblock_toggle():
    """Test toggling AdBlock via Ubus (using execute_command fallback/uci)."""
    client = UbusClient(host="192.168.1.1", username="root", password="password")
    client._connected = True
    
    with patch.object(client, "execute_command", new_callable=AsyncMock) as mock_exec:
        # Turn ON
        success = await client.set_adblock_enabled(True)
        assert success is True
        mock_exec.assert_any_call("uci set adblock.global.enabled='1' && uci commit adblock")
        mock_exec.assert_any_call("/etc/init.d/adblock start")
        
        # Turn OFF
        success = await client.set_adblock_enabled(False)
        assert success is True
        mock_exec.assert_any_call("uci set adblock.global.enabled='0' && uci commit adblock")
        mock_exec.assert_any_call("/etc/init.d/adblock stop")

@pytest.mark.asyncio
async def test_ssh_simple_adblock_status():
    """Test getting Simple AdBlock status via SSH."""
    client = SshClient(host="192.168.1.1", username="root", password="password")
    client._connected = True
    
    with patch.object(client, "_exec", new_callable=AsyncMock) as mock_exec:
        def side_effect(cmd):
            if "uci -q get simple-adblock.config.enabled" in cmd:
                return "1"
            if "wc -l < /tmp/simple-adblock.blocked" in cmd:
                return "5000"
            return ""
        
        mock_exec.side_effect = side_effect
        
        status = await client.get_simple_adblock_status()
        assert status.enabled is True
        assert status.blocked_domains == 5000

@pytest.mark.asyncio
async def test_luci_rpc_banip_status():
    """Test getting Ban-IP status via LuCI RPC."""
    client = LuciRpcClient(host="192.168.1.1", username="root", password="password")
    client._session_id = "test_token"
    client._connected = True
    
    with patch.object(client, "_rpc_call", new_callable=AsyncMock) as mock_rpc:
        mock_rpc.return_value = "1"
        
        status = await client.get_banip_status()
        assert status.enabled is True
        mock_rpc.assert_called_with("sys", "exec", ["uci -q get ban-ip.config.enabled"])

@pytest.mark.asyncio
async def test_ssh_adblock_status_ubus_failover():
    """Test getting AdBlock status via SSH with ubus failover."""
    client = SshClient(host="192.168.1.1", username="root", password="password")
    client._connected = True
    
    with patch.object(client, "_exec", new_callable=AsyncMock) as mock_exec:
        def side_effect(cmd):
            if "ubus call adblock status" in cmd:
                return "" # simulate fail
            if "uci -q get adblock.global.enabled" in cmd:
                return "1"
            return ""
        
        mock_exec.side_effect = side_effect
        
        status = await client.get_adblock_status()
        assert status.enabled is True
        assert status.status == "enabled"
