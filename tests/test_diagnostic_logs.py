"""Tests for the System Logs diagnostic sensor."""

import json
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.openwrt.api.luci_rpc import LuciRpcClient
from custom_components.openwrt.api.ssh import SshClient
from custom_components.openwrt.api.ubus import UbusClient


@pytest.mark.asyncio
async def test_ubus_get_system_logs():
    """Test getting system logs via Ubus."""
    client = UbusClient(host="192.168.1.1", username="root", password="password")
    client._connected = True

    # 1. Test direct ubus call success
    with patch.object(client, "_call", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = {"log": [{"msg": "line 1"}, {"msg": "line 2"}]}

        logs = await client.get_system_logs(count=10)

        assert len(logs) == 2
        assert logs[0] == "line 1"
        mock_call.assert_called_with("log", "read", {"lines": 10})

    # 2. Test fallback to logread
    with patch.object(client, "_call", new_callable=AsyncMock) as mock_call, \
         patch.object(client, "execute_command", new_callable=AsyncMock) as mock_exec:

        mock_call.return_value = None  # Ubus fails
        mock_exec.return_value = "Usage: logread [-n count]" # Help for detection

        # Second call to execute_command will be the actual logread
        mock_exec.side_effect = ["Usage: logread [-n count]", "fallback line 1\nfallback line 2"]

        logs = await client.get_system_logs(count=10)

        assert len(logs) == 2
        assert logs[0] == "fallback line 1"
        mock_exec.assert_any_call("/sbin/logread -n 10")


@pytest.mark.asyncio
async def test_ssh_get_system_logs():
    """Test getting system logs via SSH."""
    client = SshClient(host="192.168.1.1", username="root", password="password")
    client._connected = True

    with patch.object(client, "_exec", new_callable=AsyncMock) as mock_exec:
        # Success via ubus call log read
        mock_exec.return_value = json.dumps({"log": [{"msg": "ssh log line 1"}, {"msg": "ssh log line 2"}]})

        logs = await client.get_system_logs(count=5)

        assert len(logs) == 2
        assert "ssh log line 1" in logs
        mock_exec.assert_any_call('ubus call log read \'{"lines": 5}\'')


@pytest.mark.asyncio
async def test_luci_rpc_get_system_logs():
    """Test getting system logs via LuCI RPC."""
    client = LuciRpcClient(host="192.168.1.1", username="root", password="password")
    client._session_id = "test_token"
    client._connected = True

    with patch.object(client, "_rpc_call", new_callable=AsyncMock) as mock_rpc:
        # Success via direct ubus call
        mock_rpc.return_value = {"log": [{"msg": "luci log 1"}, {"msg": "luci log 2"}]}

        logs = await client.get_system_logs(count=20)

        assert len(logs) == 2
        mock_rpc.assert_any_call("ubus", "call", ["log", "read", {"lines": 20}])
