"""Tests for the read_file primitive."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.openwrt.api.ubus import UbusClient


@pytest.fixture
def ubus_client() -> UbusClient:
    return UbusClient(
        MagicMock(),
        MagicMock(),
        host="192.168.1.1",
        username="root",
        password="password",
    )


@pytest.mark.asyncio
async def test_ubus_read_file_returns_data(ubus_client: UbusClient):
    """ubus read_file returns the file contents from rpcd file.read."""
    with patch.object(ubus_client, "_call", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = {"data": "line1\nline2\n"}
        assert await ubus_client.read_file("/proc/x") == "line1\nline2\n"
        mock_call.assert_awaited_with("file", "read", {"path": "/proc/x"})


@pytest.mark.asyncio
async def test_ubus_read_file_missing_data_returns_none(ubus_client: UbusClient):
    """A response without a 'data' field yields None."""
    with patch.object(ubus_client, "_call", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = {}
        assert await ubus_client.read_file("/proc/x") is None


@pytest.mark.asyncio
async def test_ubus_read_file_error_returns_none(ubus_client: UbusClient):
    """A failing read (e.g. permission denied) yields None rather than raising."""
    with patch.object(ubus_client, "_call", new_callable=AsyncMock) as mock_call:
        mock_call.side_effect = Exception("access denied")
        assert await ubus_client.read_file("/etc/shadow") is None
