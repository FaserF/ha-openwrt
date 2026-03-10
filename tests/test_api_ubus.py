"""Test the OpenWrt Ubus API client."""

from unittest.mock import AsyncMock, patch

import pytest

from custom_components.openwrt.api.ubus import UbusClient


@pytest.fixture
def ubus_client() -> UbusClient:
    """Fixture for Ubus client."""
    return UbusClient(host="192.168.1.1", username="root", password="password")


class MockResponse:
    def __init__(self, status, json_data):
        self.status = status
        self._json_data = json_data

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    def raise_for_status(self):
        pass

    async def json(self):
        return self._json_data


@pytest.mark.asyncio
async def test_ubus_connect_success(ubus_client: UbusClient):
    """Test successful connection and login."""
    with patch("aiohttp.ClientSession.post") as mock_post:
        mock_post.return_value = MockResponse(
            200,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": [0, {"ubus_rpc_session": "test_token"}],
            },
        )

        await ubus_client.connect()

        assert ubus_client.connected is True
        assert ubus_client._session_id == "test_token"


@pytest.mark.asyncio
async def test_ubus_connect_auth_error(ubus_client: UbusClient):
    """Test auth error handling."""
    with patch("aiohttp.ClientSession.post") as mock_post:
        mock_post.return_value = MockResponse(
            200,
            {"jsonrpc": "2.0", "id": 1, "result": [5, {"message": "Access denied"}]},
        )

        from custom_components.openwrt.api.ubus import UbusAuthError

        with pytest.raises(UbusAuthError):
            await ubus_client.connect()


@pytest.mark.asyncio
async def test_ubus_get_device_info(ubus_client: UbusClient):
    """Test fetching device info."""
    ubus_client._session_id = "test_token"
    with patch.object(ubus_client, "_call", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = {
            "model": "Test Router",
            "release": {
                "distribution": "OpenWrt",
                "version": "25.12",
                "revision": "r1",
                "target": "test/target",
            },
        }

        info = await ubus_client.get_device_info()
        assert info.model == "Test Router"
        assert info.release_version == "25.12"
        assert info.architecture == ""
