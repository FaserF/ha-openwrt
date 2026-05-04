"""Tests for command-line redirection and error suppression."""

from unittest.mock import AsyncMock, patch

import pytest

from custom_components.openwrt.api.luci_rpc import LuciRpcClient


@pytest.fixture
def luci_client() -> LuciRpcClient:
    """Fixture for LuCI RPC client."""
    return LuciRpcClient(host="192.168.1.1", username="root", password="password")


@pytest.mark.asyncio
async def test_stderr_suppression_wireless(luci_client: LuciRpcClient):
    """Verify that wireless ubus commands use redirection in LuCI RPC."""
    luci_client._auth_token = "test_token"
    luci_client.packages.wireless = True
    
    with patch.object(luci_client, "_rpc_call", new_callable=AsyncMock) as mock_call:
        def side_effect(*args, **kwargs):
            method = args[1]
            if method == "exec":
                cmd = args[2][0]
                if "iwinfo devices" in cmd:
                    return '{"devices": ["wlan0"]}'
                if "iwinfo info" in cmd:
                    return '{"ssid": "Test", "bssid": "00:11:22:33:44:55"}'
                if "iwinfo assoclist" in cmd:
                    return '{"results": []}'
                if "hostapd" in cmd and "get_clients" in cmd:
                    return '{"clients": {}}'
            return ""

        mock_call.side_effect = side_effect

        await luci_client.get_wireless_interfaces()

        executed_cmds = [
            call.args[2][0] 
            for call in mock_call.call_args_list 
            if call.args[1] == "exec"
        ]

        wireless_cmds = [
            cmd for cmd in executed_cmds 
            if any(p in cmd for p in ["iwinfo", "hostapd"])
        ]
        
        assert len(wireless_cmds) > 0
        for cmd in wireless_cmds:
            assert "2>/dev/null" in cmd or "2>&1" in cmd


@pytest.mark.asyncio
async def test_stderr_suppression_logread(luci_client: LuciRpcClient):
    """Verify that logread probes use redirection in LuCI RPC."""
    luci_client._auth_token = "test_token"
    
    with patch.object(luci_client, "_rpc_call", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = "unrecognized option: n" # Simulated failure
        
        await luci_client.get_system_logs()
        
        executed_cmds = [
            call.args[2][0] 
            for call in mock_call.call_args_list 
            if call.args[1] == "exec"
        ]
        
        logread_probes = [cmd for cmd in executed_cmds if "logread" in cmd]
        assert len(logread_probes) > 0
        for cmd in logread_probes:
            if "help" in cmd or "-n 1" in cmd:
                assert "2>/dev/null" in cmd or "2>&1" in cmd
