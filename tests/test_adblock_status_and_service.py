"""Tests for AdBlock status detection and one-shot service handling (Issue #30)."""

import json
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.openwrt.api.luci_rpc import LuciRpcClient
from custom_components.openwrt.api.ubus import UbusClient


@pytest.mark.asyncio
async def test_ubus_adblock_formatted_domains() -> None:
    """Test parsing AdBlock blocked_domains with commas (Issue #30)."""
    client = UbusClient(host="192.168.1.1", username="root", password="password")
    client._connected = True

    with patch.object(client, "_call", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = {
            "adblock_status": "enabled",
            "adblock_version": "4.5.5-r1",
            "blocked_domains": "57,861",
            "last_run": "mode: start, date/time: 02/05/2026 11:08:55",
        }

        status = await client.get_adblock_status()
        assert status.enabled is True
        assert status.blocked_domains == 57861
        assert status.version == "4.5.5-r1"


@pytest.mark.asyncio
async def test_ubus_adblock_exception_fallback() -> None:
    """Test that AdBlock status falls back to UCI if ubus call raises an exception (Issue #30)."""
    client = UbusClient(host="192.168.1.1", username="root", password="password")
    client._connected = True

    with patch.object(client, "_call", new_callable=AsyncMock) as mock_call:
        mock_call.side_effect = Exception("Object not found")

        with patch.object(
            client, "execute_command", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = "1"

            status = await client.get_adblock_status()
            assert status.enabled is True
            assert status.status == "enabled"
            mock_exec.assert_any_call("uci -q get adblock.global.enabled")


@pytest.mark.asyncio
async def test_luci_rpc_adblock_service_one_shot() -> None:
    """Test that AdBlock service is considered running if exit_code is 0 (Issue #30)."""
    client = LuciRpcClient(host="192.168.1.1", username="root", password="password")
    client._auth_token = "test_token"
    client._connected = True

    with patch.object(client, "execute_command", new_callable=AsyncMock) as mock_exec:
        # Simulate 'ubus call service list' result
        service_data = {
            "adblock": {"instances": {"adblock": {"running": False, "exit_code": 0}}}
        }

        def side_effect(cmd: str) -> str:
            if "rc list" in cmd:
                return ""
            if "service list" in cmd:
                return json.dumps(service_data)
            return ""

        mock_exec.side_effect = side_effect

        services = await client.get_services()
        adblock_svc = next((s for s in services if s.name == "adblock"), None)
        assert adblock_svc is not None
        assert adblock_svc.running is True


@pytest.mark.asyncio
async def test_luci_rpc_adblock_formatted_domains() -> None:
    """Test LuCI RPC parsing of AdBlock status with formatted numbers."""
    client = LuciRpcClient(host="192.168.1.1", username="root", password="password")
    client._auth_token = "test_token"
    client._connected = True

    with patch.object(client, "_rpc_call", new_callable=AsyncMock) as mock_rpc:
        mock_rpc.return_value = json.dumps(
            {
                "adblock_status": "enabled",
                "blocked_domains": "1.234.567",  # Test another common separator
            }
        )

        status = await client.get_adblock_status()
        assert status.blocked_domains == 1234567


@pytest.mark.asyncio
async def test_luci_rpc_adblock_formatted_domains_dot() -> None:
    """Test LuCI RPC parsing of AdBlock status with dot as thousands separator."""
    client = LuciRpcClient(host="192.168.1.1", username="root", password="password")
    client._auth_token = "test_token"
    client._connected = True

    with patch.object(client, "_rpc_call", new_callable=AsyncMock) as mock_rpc:
        mock_rpc.return_value = json.dumps(
            {"adblock_status": "enabled", "blocked_domains": "1.234"}
        )

        status = await client.get_adblock_status()
        assert status.blocked_domains == 1234
