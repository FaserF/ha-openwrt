"""Tests for the System Logs diagnostic sensor."""

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

    with patch.object(client, "execute_command", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = "line 1\nline 2\nERROR: something failed\nline 4"

        logs = await client.get_system_logs(count=10)

        assert len(logs) == 4
        assert logs[2] == "ERROR: something failed"
        mock_exec.assert_any_call("logread -n 10")


@pytest.mark.asyncio
async def test_ssh_get_system_logs():
    """Test getting system logs via SSH."""
    client = SshClient(host="192.168.1.1", username="root", password="password")
    client._connected = True

    with patch.object(client, "_exec", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = "ssh log line 1\nssh log line 2"

        logs = await client.get_system_logs(count=5)

        assert len(logs) == 2
        assert "ssh log line 1" in logs
        mock_exec.assert_any_call("logread -n 5")


@pytest.mark.asyncio
async def test_luci_rpc_get_system_logs():
    """Test getting system logs via LuCI RPC."""
    client = LuciRpcClient(host="192.168.1.1", username="root", password="password")
    client._session_id = "test_token"
    client._connected = True

    with patch.object(client, "_rpc_call", new_callable=AsyncMock) as mock_rpc:
        mock_rpc.return_value = "luci log 1\nluci log 2"

        logs = await client.get_system_logs(count=20)

        assert len(logs) == 2
        mock_rpc.assert_any_call("sys", "exec", ["/bin/sh -c 'logread -n 20' 2>&1"])
