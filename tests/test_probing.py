"""Test the OpenWrt config flow probing logic."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from custom_components.openwrt.config_flow import OpenWrtConfigFlow
from custom_components.openwrt.const import CONNECTION_TYPE_UBUS, CONNECTION_TYPE_LUCI_RPC
import aiohttp

@pytest.fixture
def flow(hass):
    """Fixture for OpenWrtConfigFlow."""
    flow = OpenWrtConfigFlow()
    flow.hass = hass
    return flow

def create_mock_response(status=200, text="", headers=None):
    """Create a mock aiohttp response."""
    mock_res = MagicMock()
    mock_res.status = status
    mock_res.text = AsyncMock(return_value=text)
    mock_res.json = AsyncMock(return_value={})
    mock_res.headers = headers or {"Content-Type": "text/html"}
    mock_res.__aenter__ = AsyncMock(return_value=mock_res)
    mock_res.__aexit__ = AsyncMock(return_value=None)
    return mock_res

async def test_probe_openwrt_success_luci(flow, hass) -> None:
    """Test successful OpenWrt probe via LuCI."""
    mock_session = MagicMock()
    mock_session.get.return_value = create_mock_response(text="<html>LuCI - OpenWrt Control Board</html>")
    mock_session.post.return_value = create_mock_response(status=404)

    with patch("custom_components.openwrt.config_flow.async_get_clientsession", return_value=mock_session):
        assert await flow._async_probe_openwrt("192.168.1.1") == [CONNECTION_TYPE_LUCI_RPC]

async def test_probe_openwrt_success_asset(flow, hass) -> None:
    """Test successful OpenWrt probe via static asset."""
    def get_side_effect(url, **kwargs):
        if "luci-static" in url:
            return create_mock_response(status=200, text="Nothing here", headers={"Content-Type": "application/javascript"})
        return create_mock_response(status=404)

    mock_session = MagicMock()
    mock_session.get.side_effect = get_side_effect
    mock_session.post.return_value = create_mock_response(status=404)

    with patch("custom_components.openwrt.config_flow.async_get_clientsession", return_value=mock_session):
        assert await flow._async_probe_openwrt("192.168.1.1") == [CONNECTION_TYPE_LUCI_RPC]

async def test_probe_openwrt_exclusion_valetudo(flow, hass) -> None:
    """Test that Valetudo is excluded."""
    mock_session = MagicMock()
    mock_session.get.return_value = create_mock_response(text="<html>Welcome to Valetudo - Manual Control</html>")
    mock_session.post.return_value = create_mock_response(status=404)

    with patch("custom_components.openwrt.config_flow.async_get_clientsession", return_value=mock_session):
        assert await flow._async_probe_openwrt("192.168.1.1") == []

async def test_probe_openwrt_exclusion_vacuum(flow, hass) -> None:
    """Test that Dreame vacuum is excluded via hostname and ubus content."""
    mock_session = MagicMock()
    mock_session.get.return_value = create_mock_response(status=404)
    # Mock UBus response (200 OK but maybe it's a non-router)
    mock_session.post.return_value = create_mock_response(status=200, text='{"error": "access denied"}', headers={"Content-Type": "application/json"})

    with patch("custom_components.openwrt.config_flow.async_get_clientsession", return_value=mock_session):
        # Case 1: Excluded by hostname
        assert await flow._async_probe_openwrt("192.168.1.67", "dreame_vacuum_p2028.lan") == []
        
        # Case 2: Excluded by UBus text (if hostname not available)
        mock_session.post.return_value = create_mock_response(status=200, text='{"model": "dreame.vacuum.p2028"}', headers={"Content-Type": "application/json"})
        assert await flow._async_probe_openwrt("192.168.1.67") == []

        # Case 3: Excluded because UBus probe (405) didn't return JSON content type
        mock_session.post.return_value = create_mock_response(status=405, headers={"Content-Type": "text/html"})
        assert await flow._async_probe_openwrt("192.168.1.67") == []

async def test_probe_router_exclusion_logic(flow, hass) -> None:
    """Test that the high-level probe_router logic correctly excludes devices."""
    with (
        patch("socket.gethostbyaddr", return_value=("dreame.vacuum.local", [], [])),
        patch("custom_components.openwrt.config_flow.OpenWrtConfigFlow._async_check_reachable", return_value=True),
        patch("custom_components.openwrt.config_flow.OpenWrtConfigFlow._async_probe_openwrt", return_value=["ubus"]),
    ):
        # 1. Excluded via hinted hostname
        assert await flow._async_probe_router("192.168.1.67", "Valetudo WittyIdealisticSnake") is None
        
        # 2. Excluded via resolved hostname (reverse DNS)
        assert await flow._async_probe_router("192.168.1.67") is None

        # 3. Not excluded if safe
        with patch("socket.gethostbyaddr", return_value=("OpenWrt.local", [], [])):
            res = await flow._async_probe_router("192.168.1.1", "OpenWrt")
            assert res is not None
            assert res["hostname"] == "OpenWrt"

async def test_probe_openwrt_failure(flow, hass) -> None:
    """Test failed OpenWrt probe."""
    mock_session = MagicMock()
    mock_session.get.side_effect = aiohttp.ClientError()
    mock_session.post.side_effect = aiohttp.ClientError()

    with patch("custom_components.openwrt.config_flow.async_get_clientsession", return_value=mock_session):
        assert await flow._async_probe_openwrt("192.168.1.1") == []
