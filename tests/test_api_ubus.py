"""Test the OpenWrt Ubus API client."""

from unittest.mock import AsyncMock, patch

import pytest

from custom_components.openwrt.api.ubus import UbusClient


@pytest.fixture
def ubus_client() -> UbusClient:
    """Fixture for Ubus client."""
    return UbusClient(host="192.168.1.1", username="ha-user", password="password")


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


@pytest.mark.asyncio
async def test_ubus_get_sqm_status(ubus_client: UbusClient):
    """Test fetching SQM status via ubus."""
    ubus_client._session_id = "test_token"
    with patch.object(ubus_client, "_call", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = {
            "eth0": {
                ".type": "queue",
                ".name": "eth0",
                "enabled": "1",
                "interface": "wan",
                "download": "100000",
                "upload": "50000",
                "qdisc": "fq_codel",
                "script": "simple.qos",
            }
        }

        status = await ubus_client.get_sqm_status()
        assert len(status) == 1
        assert status[0].section_id == "eth0"
        assert status[0].enabled is True
        assert status[0].download == 100000


@pytest.mark.asyncio
async def test_ubus_set_sqm_config(ubus_client: UbusClient):
    """Test setting SQM config via ubus."""
    ubus_client._session_id = "test_token"
    with patch.object(ubus_client, "_call", new_callable=AsyncMock) as mock_call:
        await ubus_client.set_sqm_config("eth0", enabled=False, download=200000)
        
        # Should call uci set (at least twice) and commit
        assert mock_call.call_count >= 3
        
        # Check if enabled was set
        mock_call.assert_any_call("uci", "set", {"config": "sqm", "section": "eth0", "values": {"enabled": "0"}})
        # Check if download was set
        mock_call.assert_any_call("uci", "set", {"config": "sqm", "section": "eth0", "values": {"download": "200000"}})
        # Check commit
        mock_call.assert_any_call("uci", "commit", {"config": "sqm"})


@pytest.mark.asyncio
async def test_ubus_check_permissions(ubus_client: UbusClient):
    """Test checking permissions via ubus."""
    ubus_client._session_id = "test_token"
    from custom_components.openwrt.api.ubus import UbusPermissionError
    
    # Mock ubus 'session' list and 'uci' calls
    with patch.object(ubus_client, "_call", new_callable=AsyncMock) as mock_call:
        def side_effect(obj, method, params=None):
            if obj == "session" and method == "list":
                # Return restricted permissions via session list
                return {"values": {"access": {"system": {"read": True, "write": True}}}}
            if obj == "uci" and method == "get":
                raise UbusPermissionError("Access denied")
            return {}
        
        mock_call.side_effect = side_effect
        
        perms = await ubus_client.check_permissions()
        assert perms.read_system is True
        assert perms.write_system is True
        assert perms.read_network is False
