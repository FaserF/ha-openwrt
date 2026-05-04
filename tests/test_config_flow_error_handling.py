"""Tests for recent config flow and API fixes."""

from unittest.mock import MagicMock, patch

import pytest
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME

from custom_components.openwrt.api.luci_rpc import (
    LuciRpcClient,
    LuciRpcPackageMissingError,
)
from custom_components.openwrt.config_flow import OpenWrtConfigFlow
from custom_components.openwrt.const import (
    CONF_CONNECTION_TYPE,
    CONF_TRACK_DEVICES,
    CONNECTION_TYPE_UBUS,
)


class MockResponse:
    def __init__(self, status=200, json_data=None, text_data=None, headers=None):
        self.status = status
        self._json_data = json_data
        self._text_data = text_data
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    def raise_for_status(self):
        if self.status >= 400:
            msg = f"HTTP Error {self.status}"
            raise Exception(msg)

    async def json(self):
        return self._json_data

    async def text(self):
        return self._text_data


@pytest.fixture(autouse=True)
def bypass_setup_fixture():
    """Prevent setup."""
    with patch(
        "custom_components.openwrt.async_setup_entry",
        return_value=True,
    ):
        yield


async def test_is_excluded_logic(hass):
    """Test that non-OpenWrt devices are correctly excluded."""
    flow = OpenWrtConfigFlow()
    flow.hass = hass

    # Test OPNsense exclusion
    assert flow._is_excluded("10.0.0.1", "opnsense.local") is True
    assert flow._is_excluded("10.0.0.1", "OPNsense Firewall") is True

    # Test pfsense exclusion
    assert flow._is_excluded("10.0.0.1", "pfsense") is True

    # Test Proxmox/ESXi exclusion
    assert flow._is_excluded("10.0.0.1", "proxmox-ve") is True
    assert flow._is_excluded("10.0.0.1", "esxi-host") is True

    # Test allowed router
    assert flow._is_excluded("192.168.1.1", "OpenWrt") is False
    assert flow._is_excluded("192.168.1.1", "Router") is False


async def test_manual_entry_in_selection(hass):
    """Test that 'Manual Entry' option exists and works."""
    flow = OpenWrtConfigFlow()
    flow.hass = hass

    # Mock discovered routers
    flow._discovered_routers = [
        {
            "host": "192.168.1.1",
            "hostname": "OpenWrt",
            "capabilities": ["ubus"],
            "method": "ubus",
        },
    ]

    # Check selection form
    result = await flow.async_step_select_device()
    assert "manual" in result["data_schema"].schema["device"].container

    # Select manual entry
    result = await flow.async_step_select_device({"device": "manual"})
    assert result["type"].lower() == "form"
    assert result["step_id"] == "manual_entry"


async def test_create_entry_data_options_split(hass):
    """Test that config entry creation correctly splits data and options."""
    flow = OpenWrtConfigFlow()
    flow.hass = hass
    flow._data = {
        CONF_HOST: "192.168.1.1",
        CONF_USERNAME: "root",
        CONF_PASSWORD: "password",
        CONF_CONNECTION_TYPE: CONNECTION_TYPE_UBUS,
        CONF_TRACK_DEVICES: False,  # This should go to options
        "update_interval": 30,  # This should go to options
    }
    flow._device_info = {"hostname": "MyRouter", "mac_address": "AA:BB:CC:DD:EE:FF"}

    with patch(
        "homeassistant.config_entries.ConfigFlow.async_create_entry",
        return_value={"type": "create_entry"},
    ) as mock_create:
        await flow._create_entry()

        _args, kwargs = mock_create.call_args
        data = kwargs["data"]
        options = kwargs["options"]

        # Data should have connection info
        assert data[CONF_HOST] == "192.168.1.1"
        assert data[CONF_USERNAME] == "root"
        assert CONF_TRACK_DEVICES not in data

        # Options should have toggles
        assert options[CONF_TRACK_DEVICES] is False
        assert options["update_interval"] == 30


async def test_luci_rpc_html_response_handling(hass):
    """Test that LuciRpcClient handles HTML responses specifically (missing package)."""
    # Create a mock session that returns our mock response
    mock_resp = MockResponse(
        status=200,
        text_data="<html><body>Redirecting...</body></html>",
        headers={"Content-Type": "text/html; charset=utf-8"},
    )

    mock_session = MagicMock()
    # It must be a MagicMock that can be used as an async context manager if needed,
    # but here LuciRpcClient uses it directly for post()
    mock_session.post = MagicMock(return_value=mock_resp)
    mock_session.closed = False

    client = LuciRpcClient("192.168.1.1", "root", "password")
    client._session = mock_session

    # The _rpc_call should detect it's not JSON and raise LuciRpcPackageMissingError
    with pytest.raises(LuciRpcPackageMissingError):
        await client._rpc_call("sys", "info")


async def test_luci_probe_specific_strings(hass):
    """Test that the LuCI probe looks for specific strings."""
    flow = OpenWrtConfigFlow()
    flow.hass = hass

    mock_resp = MockResponse(
        status=200,
        text_data="<html><title>LuCI - OpenWrt</title></html>",
        headers={"Server": "uhttpd", "Content-Type": "text/html"},
    )

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_resp)
    mock_session.post = MagicMock(return_value=mock_resp)

    with patch(
        "custom_components.openwrt.config_flow.async_get_clientsession",
        return_value=mock_session,
    ):
        methods = await flow._async_probe_openwrt("192.168.1.1")
        assert "luci_rpc" in methods
