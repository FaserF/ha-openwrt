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
        assert cmd == "logread -n 10"
        assert client._logread_flag == "-n"

        # Subsequent calls should not run help again
        mock_exec.reset_mock()
        cmd2 = await client._get_logread_command(20)
        assert cmd2 == "logread -n 20"
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
        assert cmd == "logread -l 15"
        assert client._logread_flag == "-l"

        # Verify the help command was called
        mock_exec.assert_any_call("logread --help 2>&1")
