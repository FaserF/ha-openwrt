"""Test the OpenWrt LuCI RPC API client."""

from unittest.mock import AsyncMock, patch

import pytest

from custom_components.openwrt.api.luci_rpc import (
    LuciRpcAuthError,
    LuciRpcClient,
)


@pytest.fixture
def luci_client() -> LuciRpcClient:
    """Fixture for LuCI RPC client."""
    return LuciRpcClient(host="192.168.1.1", username="root", password="password")


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
async def test_luci_connect_success(luci_client: LuciRpcClient):
    """Test successful connection and login."""
    with patch("aiohttp.ClientSession.post") as mock_post:
        # LuCI returns the token as result directly
        mock_post.return_value = MockResponse(
            200, {"id": 1, "result": "luci_test_token"}
        )

        await luci_client.connect()

        assert luci_client.connected is True
        assert luci_client._auth_token == "luci_test_token"


@pytest.mark.asyncio
async def test_luci_connect_auth_error(luci_client: LuciRpcClient):
    """Test auth error handling."""
    with patch("aiohttp.ClientSession.post") as mock_post:
        mock_post.return_value = MockResponse(
            200, {"id": 1, "error": {"message": "Invalid credentials"}}
        )

        with pytest.raises(LuciRpcAuthError):
            await luci_client.connect()


@pytest.mark.asyncio
async def test_luci_get_device_info(luci_client: LuciRpcClient):
    """Test fetching device info."""
    luci_client._auth_token = "luci_test_token"
    with patch.object(luci_client, "_rpc_call", new_callable=AsyncMock) as mock_call:

        def call_side_effect(*args, **kwargs):
            method = args[1]
            if method == "hostname":
                return "LuCI-Router"
            if method == "exec":
                cmd = args[2][0]
                if "openwrt_release" in cmd:
                    return "DISTRIB_RELEASE='25.12'\nDISTRIB_REVISION='luci-r3'\nDISTRIB_ARCH='arm/v8'\nDISTRIB_TARGET='arm/v8'"
            return ""

        mock_call.side_effect = call_side_effect

        info = await luci_client.get_device_info()
        assert info.hostname == "LuCI-Router"
        assert info.release_version == "25.12"
        assert info.architecture == "arm/v8"
