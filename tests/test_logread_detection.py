"""Tests for logread flag detection (-n vs -l)."""

from unittest.mock import AsyncMock, patch

import pytest

from custom_components.openwrt.api.ubus import UbusClient


@pytest.mark.asyncio
async def test_logread_detection_n():
    """Test detection of -n flag (default)."""
    client = UbusClient(host="192.168.1.1", username="root", password="password")
    client._connected = True

    with patch.object(client, "execute_command", new_callable=AsyncMock) as mock_exec:
        # Help doesn't contain -l
        mock_exec.return_value = "Usage: logread [-n count] [-f]"

        cmd = await client._get_logread_command(10)
        assert cmd == "/sbin/logread -n 10"
        assert client._logread_flag == "-n"

        # Subsequent calls should not run help again
        mock_exec.reset_mock()
        cmd2 = await client._get_logread_command(20)
        assert cmd2 == "/sbin/logread -n 20"
        mock_exec.assert_not_called()


@pytest.mark.asyncio
async def test_logread_detection_l():
    """Test detection of -l flag (modern OpenWrt)."""
    client = UbusClient(host="192.168.1.1", username="root", password="password")
    client._connected = True

    with patch.object(client, "execute_command", new_callable=AsyncMock) as mock_exec:
        # Help contains -l
        mock_exec.return_value = (
            "Options:\n -l <count> Got only the last 'count' messages"
        )

        cmd = await client._get_logread_command(15)
        assert cmd == "/sbin/logread -l 15"
        assert client._logread_flag == "-l"

        # Verify the help command was called
        mock_exec.assert_any_call("/sbin/logread --help 2>&1")


@pytest.mark.asyncio
async def test_logread_detection_l_alternative():
    """Test detection of -l flag with alternative help text."""
    client = UbusClient(host="192.168.1.1", username="root", password="password")
    client._connected = True

    with patch.object(client, "execute_command", new_callable=AsyncMock) as mock_exec:
        # Help contains -l but in different context
        mock_exec.return_value = (
            "logread: unrecognized option: --\nOptions: -l messages"
        )

        cmd = await client._get_logread_command(5)
        assert cmd == "/sbin/logread -l 5"
        assert client._logread_flag == "-l"


@pytest.mark.asyncio
async def test_ubus_no_direct_log_read():
    """Verify ubus client no longer issues direct log read proxy calls."""
    client = UbusClient(host="192.168.1.1", username="root", password="password")
    client._connected = True

    with (
        patch.object(client, "_call", new_callable=AsyncMock) as mock_call,
        patch.object(client, "execute_command", new_callable=AsyncMock) as mock_exec,
    ):
        mock_exec.return_value = "Options: -l count"

        await client.get_system_logs(10)

        # Ensure execute_command for logread is called
        mock_exec.assert_any_call("/sbin/logread -l 10")

        # Ensure _call("log", "read") was NEVER called
        for call in mock_call.call_args_list:
            args, _ = call
            assert not (args[0] == "log" and args[1] == "read")


@pytest.mark.asyncio
async def test_luci_rpc_no_direct_log_read():
    """Verify LuCI RPC client no longer issues direct log read proxy calls."""
    from custom_components.openwrt.api.luci_rpc import LuciRpcClient

    client = LuciRpcClient(host="192.168.1.1", username="root", password="password")
    client._connected = True

    with (
        patch.object(client, "_rpc_call", new_callable=AsyncMock) as mock_rpc_call,
        patch.object(client, "execute_command", new_callable=AsyncMock) as mock_exec,
    ):
        mock_exec.return_value = "Options: -l count"

        await client.get_system_logs(10)

        # Ensure execute_command for logread is called
        mock_exec.assert_any_call("/sbin/logread -l 10")

        # Ensure _rpc_call("ubus", "call", ["log", "read"]) was NEVER called
        for call in mock_rpc_call.call_args_list:
            args, _ = call
            if args[0] == "ubus" and args[1] == "call" and isinstance(args[2], list):
                assert not (args[2][0] == "log" and args[2][1] == "read")
