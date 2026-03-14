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


@pytest.mark.asyncio
async def test_luci_get_sqm_status(luci_client: LuciRpcClient):
    """Test fetching SQM status via LuCI RPC."""
    luci_client._auth_token = "luci_test_token"
    with patch.object(luci_client, "_rpc_call", new_callable=AsyncMock) as mock_call:
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

        status = await luci_client.get_sqm_status()
        assert len(status) == 1
        assert status[0].section_id == "eth0"
        assert status[0].enabled is True
        assert status[0].download == 100000


@pytest.mark.asyncio
async def test_luci_set_sqm_config(luci_client: LuciRpcClient):
    """Test setting SQM config via LuCI RPC."""
    luci_client._auth_token = "luci_test_token"
    with patch.object(luci_client, "_rpc_call", new_callable=AsyncMock) as mock_call:
        await luci_client.set_sqm_config("eth0", enabled=False, download=200000)

        # Check if calls were made
        assert mock_call.call_count >= 3

        # Check if enabled was set
        mock_call.assert_any_call("uci", "set", ["sqm", "eth0", "enabled", "0"])
        # Check if download was set
        mock_call.assert_any_call("uci", "set", ["sqm", "eth0", "download", "200000"])
        # Check commit
        mock_call.assert_any_call("uci", "commit", ["sqm"])


@pytest.mark.asyncio
async def test_luci_provision_user(luci_client: LuciRpcClient):
    """Test user provisioning via LuCI RPC."""
    luci_client._auth_token = "luci_test_token"
    with patch.object(
        luci_client, "execute_command", new_callable=AsyncMock
    ) as mock_exec:
        mock_exec.return_value = "LOG: Provisioning SUCCESS"

        success = await luci_client.provision_user("homeassistant", "new-password")

        assert success is True
        script = mock_exec.call_args[0][0]
        assert "USER=$(cat <<'EOF'\nhomeassistant\nEOF\n)" in script
        assert "PASS=$(cat <<'EOF'\nnew-password\nEOF\n)" in script
        assert "uci set rpcd.homeassistant=login" in script
        assert 'uci set rpcd.homeassistant.password="\\$p\\$$USER"' in script
        assert "/etc/init.d/rpcd restart" in script
