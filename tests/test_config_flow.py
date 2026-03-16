"""Test the OpenWrt config flow."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Mocking constants to ensure consistency
CONF_HOST = "host"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONNECTION_TYPE_UBUS = "ubus"


@pytest.fixture(autouse=True)
def bypass_setup_fixture():
    """Prevent setup."""
    with patch(
        "custom_components.openwrt.async_setup_entry",
        return_value=True,
    ):
        yield


async def test_full_user_flow(hass) -> None:
    """Test successful user flow with discovery and permissions."""
    from custom_components.openwrt.api.base import (
        DeviceInfo,
        OpenWrtPackages,
        OpenWrtPermissions,
    )
    from custom_components.openwrt.config_flow import OpenWrtConfigFlow

    flow = OpenWrtConfigFlow()
    flow.hass = hass

    # 1. Welcome Step
    result = await flow.async_step_user()
    assert result["type"].lower() == "form"
    assert result["step_id"] == "user"

    # 2. Discovery Step (started by submitting Welcome)
    # Mocking discovery to find one router (192.168.1.1)
    mock_writer = AsyncMock()
    mock_writer.close = MagicMock()
    mock_writer.wait_closed = AsyncMock()
    
    async def side_effect(host, port):
        if host == "192.168.1.1":
            return AsyncMock(), mock_writer
        raise ConnectionRefusedError()

    with (
        patch("asyncio.open_connection", side_effect=side_effect),
        patch("socket.gethostbyaddr", return_value=("OpenWrt.local", [], [])),
        patch(
            "custom_components.openwrt.config_flow.OpenWrtConfigFlow._async_probe_openwrt",
            side_effect=lambda host, hostname=None: ["ubus"] if host == "192.168.1.1" else [],
        ),
        patch(
            "custom_components.openwrt.config_flow.OpenWrtConfigFlow._async_check_reachable",
            side_effect=lambda host, conn_type: host == "192.168.1.1",
        ),
        patch("homeassistant.components.network.async_get_adapters", return_value=[]),
    ):
        result = await flow.async_step_user({"next": True})
    
    assert result["type"].lower() == "form"
    assert result["step_id"] == "credentials"
    assert "OpenWrt.local" in result["description_placeholders"]["auto_detected_info"]

    # 3. Credentials Step
    mock_client = AsyncMock()
    mock_client.connect.return_value = True
    mock_client.disconnect.return_value = None
    mock_client.get_device_info.return_value = DeviceInfo(hostname="OpenWrtTest", mac_address="AA:BB:CC:DD:EE:FF")
    mock_client.check_permissions.return_value = OpenWrtPermissions(read_system=True)
    mock_client.check_packages.return_value = OpenWrtPackages(sqm_scripts=True)

    with (
        patch("custom_components.openwrt.config_flow.create_client", return_value=mock_client),
        patch("custom_components.openwrt.coordinator.create_client", return_value=mock_client),
    ):
        mock_client.user_exists.return_value = False
        result = await flow.async_step_credentials(
            {"username": "root", "password": "password", "use_ssl": False}
        )

    assert result["step_id"] == "provision_user"

    # 4. Provision -> Permissions
    result = await flow.async_step_provision_user({"mode": "skip"})
    assert result["step_id"] == "permissions_ubus"

    # 5. Permissions -> Packages
    result = await flow.async_step_permissions({})
    assert result["step_id"] == "packages"

    # 6. Packages -> Create Entry
    result = await flow.async_step_packages({})
    assert result["type"].lower() == "create_entry"
    assert result["title"] == "OpenWrtTest"
    assert flow.unique_id == "aa:bb:cc:dd:ee:ff"


async def test_full_user_flow_with_check_errors(hass) -> None:
    """Test user flow when permission and package checks fail."""
    from custom_components.openwrt.api.base import DeviceInfo
    from custom_components.openwrt.config_flow import OpenWrtConfigFlow

    flow = OpenWrtConfigFlow()
    flow.hass = hass

    # 1. Welcome
    await flow.async_step_user()
    
    # 2. Discovery -> Credentials
    mock_writer = AsyncMock()
    mock_writer.close = MagicMock()
    mock_writer.wait_closed = AsyncMock()

    async def side_effect(host, port):
        if host == "192.168.1.1":
            return AsyncMock(), mock_writer
        raise ConnectionRefusedError()

    with (
        patch("asyncio.open_connection", side_effect=side_effect),
        patch("socket.gethostbyaddr", side_effect=Exception()),
        patch(
            "custom_components.openwrt.config_flow.OpenWrtConfigFlow._async_probe_openwrt",
            side_effect=lambda host, hostname=None: ["ubus"] if host == "192.168.1.1" else [],
        ),
        patch(
            "custom_components.openwrt.config_flow.OpenWrtConfigFlow._async_check_reachable",
            side_effect=lambda host, conn_type: host == "192.168.1.1",
        ),
        patch("homeassistant.components.network.async_get_adapters", return_value=[]),
    ):
        result = await flow.async_step_user({"next": True})

    # 3. Credentials
    mock_client = AsyncMock()
    mock_client.connect.return_value = True
    mock_client.disconnect.return_value = None
    mock_client.get_device_info.return_value = DeviceInfo(hostname="OpenWrtTest", mac_address="AA:BB:CC:DD:EE:FF")
    mock_client.check_permissions.side_effect = Exception("Permission Error")
    mock_client.check_packages.side_effect = Exception("Package Error")

    with (
        patch("custom_components.openwrt.config_flow.create_client", return_value=mock_client),
        patch("custom_components.openwrt.coordinator.create_client", return_value=mock_client),
    ):
        mock_client.user_exists.return_value = False
        result = await flow.async_step_credentials(
            {"username": "root", "password": "password", "use_ssl": False}
        )

    assert result["step_id"] == "provision_user"

    # 4. Provision -> Entry (skips permissions/packages because of errors)
    with patch("custom_components.openwrt.config_flow.asyncio.sleep"):
        result = await flow.async_step_provision_user({"mode": "skip"})

    assert result["type"].lower() == "create_entry"
    assert flow.unique_id == "aa:bb:cc:dd:ee:ff"


async def test_config_flow_default_connection_type(hass) -> None:
    """Test that the default connection type is LuCI RPC."""
    from custom_components.openwrt.config_flow import OpenWrtConfigFlow
    from custom_components.openwrt.const import CONNECTION_TYPE_LUCI_RPC

    flow = OpenWrtConfigFlow()
    flow.hass = hass

    # 1. Welcome
    await flow.async_step_user()
    
    # 2. Discovery (submitted Welcome, mock no routers found)
    with (
        patch("asyncio.open_connection", side_effect=ConnectionRefusedError()),
        patch("homeassistant.components.network.async_get_adapters", return_value=[]),
    ):
        result = await flow.async_step_user({"next": True})
    
    # Should land in manual_entry (fallback for no discovery)
    assert result["type"].lower() == "form"
    assert result["step_id"] == "manual_entry"

    # Check schema for default connection type
    schema = result["data_schema"]
    for key, _value in schema.schema.items():
        if key == "connection_type":
            assert key.default() == CONNECTION_TYPE_LUCI_RPC
            break
    else:
        pytest.fail("connection_type not found in schema")


async def test_multi_router_selection(hass) -> None:
    """Test selection screen when multiple routers are found."""
    from custom_components.openwrt.config_flow import OpenWrtConfigFlow

    flow = OpenWrtConfigFlow()
    flow.hass = hass

    # 1. Welcome
    await flow.async_step_user()

    # 2. Discovery -> Finds 2 routers
    mock_writer = AsyncMock()
    mock_writer.close = MagicMock()
    mock_writer.wait_closed = AsyncMock()

    async def side_effect(host, port):
        # Allow two hosts to be reachable
        if host in ["192.168.1.1", "192.168.0.1"]:
            return AsyncMock(), mock_writer
        raise ConnectionRefusedError()

    with (
        patch("asyncio.open_connection", side_effect=side_effect),
        patch("socket.gethostbyaddr", side_effect=[("Router1", [], []), ("Router2", [], [])]),
        patch(
            "custom_components.openwrt.config_flow.OpenWrtConfigFlow._async_probe_openwrt",
            return_value=[CONNECTION_TYPE_UBUS],
        ),
        patch(
            "custom_components.openwrt.config_flow.OpenWrtConfigFlow._async_check_reachable",
            return_value=True,
        ),
        patch("homeassistant.components.network.async_get_adapters", return_value=[]),
    ):
        result = await flow.async_step_user({"next": True})

    assert result["type"].lower() == "form"
    assert result["step_id"] == "select_device"

    # 3. Selection Step
    # The label is now more complex: "Router1 (192.168.1.1) - UBUS [Available: ubus, ssh]"
    # But for the form submission, we only need the value (IP)
    result = await flow.async_step_select_device({"device": "192.168.1.1"})
    assert result["step_id"] == "credentials"
    assert result["description_placeholders"]["host"] == "192.168.1.1"


async def test_dhcp_discovery(hass) -> None:
    """Test successful DHCP discovery."""
    from custom_components.openwrt.config_flow import OpenWrtConfigFlow
    from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo

    flow = OpenWrtConfigFlow()
    flow.hass = hass
    flow.context = {}

    discovery_info = MagicMock()
    discovery_info.ip = "192.168.1.1"
    discovery_info.hostname = "OpenWrt"
    discovery_info.macaddress = "D4:BC:52:12:34:56"

    with (
        patch("custom_components.openwrt.config_flow.OpenWrtConfigFlow._async_probe_router") as mock_probe,
        patch("custom_components.openwrt.config_flow.OpenWrtConfigFlow.async_set_unique_id") as mock_set_uid,
    ):
        mock_probe.return_value = {
            "host": "192.168.1.1",
            "hostname": "OpenWrt",
            "capabilities": ["ubus", "ssh"],
            "method": "ubus"
        }
        result = await flow.async_step_dhcp(discovery_info)

    assert result["type"].lower() == "form"
    assert result["step_id"] == "ssdp_confirm"
    mock_set_uid.assert_called_with("d4:bc:52:12:34:56")
    mock_probe.assert_called_with("192.168.1.1", "OpenWrt")

    # Follow through confirmation
    result = await flow.async_step_ssdp_confirm({})
    assert result["step_id"] == "credentials"


async def test_zeroconf_discovery(hass) -> None:
    """Test successful Zeroconf discovery."""
    from custom_components.openwrt.config_flow import OpenWrtConfigFlow
    from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

    flow = OpenWrtConfigFlow()
    flow.hass = hass
    flow.context = {}

    discovery_info = MagicMock()
    discovery_info.host = "192.168.1.1"
    discovery_info.hostname = "OpenWrt.local."
    discovery_info.type = "_luci._tcp.local."
    discovery_info.name = "OpenWrt._luci._tcp.local."
    discovery_info.properties = {}

    with (
        patch("custom_components.openwrt.config_flow.OpenWrtConfigFlow._async_probe_router") as mock_probe,
        patch("custom_components.openwrt.config_flow.OpenWrtConfigFlow.async_set_unique_id") as mock_set_uid,
    ):
        mock_probe.return_value = {
            "host": "192.168.1.1",
            "hostname": "OpenWrt",
            "capabilities": ["ubus"],
            "method": "ubus"
        }
        result = await flow.async_step_zeroconf(discovery_info)

    assert result["type"].lower() == "form"
    assert result["step_id"] == "ssdp_confirm"
    mock_set_uid.assert_called_with("192.168.1.1")
    mock_probe.assert_called_with("192.168.1.1", "OpenWrt._luci._tcp.local.")

    # Follow through confirmation
    result = await flow.async_step_ssdp_confirm({})
    assert result["step_id"] == "credentials"
