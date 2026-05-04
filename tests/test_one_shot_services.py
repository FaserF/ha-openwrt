"""Tests for one-shot service handling (Issue #30)."""

import json
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.openwrt.api.luci_rpc import LuciRpcClient
from custom_components.openwrt.api.ssh import SshClient
from custom_components.openwrt.api.ubus import UbusClient


@pytest.mark.asyncio
async def test_ubus_sysctl_one_shot() -> None:
    """Test that sysctl is reported as running via Ubus if exit_code is 0."""
    client = UbusClient(host="192.168.1.1", username="root", password="password")
    client._connected = True

    with patch.object(client, "_call", new_callable=AsyncMock) as mock_call:
        # Mock rc list
        mock_call.return_value = {
            "sysctl": {"enabled": True, "running": False, "exit_code": 0}
        }

        services = await client.get_services()
        sysctl = next((s for s in services if s.name == "sysctl"), None)
        assert sysctl is not None
        assert sysctl.running is True


@pytest.mark.asyncio
async def test_luci_rpc_sysctl_one_shot() -> None:
    """Test that sysctl is reported as running via LuCI RPC if exit_code is 0."""
    client = LuciRpcClient(host="192.168.1.1", username="root", password="password")
    client._auth_token = "test_token"
    client._connected = True

    with patch.object(client, "execute_command", new_callable=AsyncMock) as mock_exec:
        # 1. rc list fails
        # 2. service list succeeds
        service_data = {
            "sysctl": {"instances": {"sysctl": {"running": False, "exit_code": 0}}}
        }

        def side_effect(cmd: str) -> str:
            if "rc list" in cmd:
                return ""
            if "service list" in cmd:
                return json.dumps(service_data)
            return ""

        mock_exec.side_effect = side_effect

        services = await client.get_services()
        sysctl = next((s for s in services if s.name == "sysctl"), None)
        assert sysctl is not None
        assert sysctl.running is True


@pytest.mark.asyncio
async def test_ssh_sysctl_one_shot() -> None:
    """Test that sysctl is reported as running via SSH if enabled."""
    client = SshClient(host="192.168.1.1", username="root", password="password")
    client._connected = True

    with patch.object(client, "_exec", new_callable=AsyncMock) as mock_exec:

        def side_effect(cmd: str) -> str:
            if "ls /etc/init.d/" in cmd:
                return "sysctl\ndnsmasq"
            if "sysctl enabled" in cmd:
                return "yes"
            if "sysctl running" in cmd:
                return "no"
            return ""

        mock_exec.side_effect = side_effect

        services = await client.get_services()
        sysctl = next((s for s in services if s.name == "sysctl"), None)
        assert sysctl is not None
        assert sysctl.running is True
