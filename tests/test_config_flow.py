"""Test the OpenWrt config flow."""

from unittest.mock import AsyncMock, patch

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
    """Test successful user flow with permissions step."""
    from custom_components.openwrt.config_flow import OpenWrtConfigFlow
    from custom_components.openwrt.api.base import DeviceInfo, OpenWrtPermissions, OpenWrtPackages
    
    flow = OpenWrtConfigFlow()
    flow.hass = hass
    
    # User Step - Mock asyncio.open_connection to simulate reachable host
    mock_writer = AsyncMock()
    mock_writer.close.return_value = None
    mock_writer.wait_closed.return_value = None
    
    with patch("asyncio.open_connection", return_value=(AsyncMock(), mock_writer)):
        result = await flow.async_step_user(
            {
                "host": "192.168.1.1",
                "connection_type": "ubus",
            }
        )
    assert str(result["type"]).upper() == "FORM"
    assert result["step_id"] == "credentials"

    # Prepare mocks for credentials/permissions
    mock_client = AsyncMock()
    mock_client.connect.return_value = True
    mock_client.disconnect.return_value = None
    
    mock_device_info = DeviceInfo(
        hostname="OpenWrtTest",
        model="Generic",
        firmware_version="23.05",
        kernel_version="5.15",
        local_time="2023-01-01 00:00:00",
        uptime=3600,
    )
    mock_client.get_device_info.return_value = mock_device_info
    
    mock_perms = OpenWrtPermissions()
    mock_perms.read_system = True
    mock_perms.write_system = True
    mock_client.check_permissions.return_value = mock_perms
    
    mock_packages = OpenWrtPackages()
    mock_packages.sqm_scripts = True
    mock_packages.mwan3 = False
    mock_client.check_packages.return_value = mock_packages

    # Credentials Step -> Permissions Step (via _create_entry)
    # Patch BOTH the coordinator version AND the config_flow reference to be safe
    with patch("custom_components.openwrt.config_flow.create_client", return_value=mock_client), \
         patch("custom_components.openwrt.coordinator.create_client", return_value=mock_client):
        result2 = await flow.async_step_credentials(
            {
                "username": "root",
                "password": "password",
                "use_ssl": False,
            }
        )
    
    assert str(result2["type"]).upper() == "FORM"
    assert result2["step_id"] == "permissions"

    # Permissions Step -> Create Entry
    result3 = await flow.async_step_permissions({})

    assert str(result3["type"]).upper() == "CREATE_ENTRY"
    assert result3["title"] == "OpenWrtTest"
    assert result3["data"]["host"] == "192.168.1.1"


async def test_full_user_flow_with_check_errors(hass) -> None:
    """Test user flow when permission and package checks fail; should skip the permissions step."""
    from custom_components.openwrt.config_flow import OpenWrtConfigFlow
    from custom_components.openwrt.api.base import DeviceInfo
    
    flow = OpenWrtConfigFlow()
    flow.hass = hass
    
    # User Step
    mock_writer = AsyncMock()
    mock_writer.close.return_value = None
    mock_writer.wait_closed.return_value = None
    
    with patch("asyncio.open_connection", return_value=(AsyncMock(), mock_writer)):
        await flow.async_step_user({"host": "192.168.1.1", "connection_type": "ubus"})
        
    mock_client = AsyncMock()
    mock_client.connect.return_value = True
    mock_client.disconnect.return_value = None
    
    # Mock Device Info
    mock_client.get_device_info.return_value = DeviceInfo(
        hostname="OpenWrtTest",
        model="Generic",
    )
    
    # Simulate failed checks
    mock_client.check_permissions.side_effect = Exception("Permission Error")
    mock_client.check_packages.side_effect = Exception("Package Error")
    
    # Credentials Step - should skip permissions step entirely because permissions and packages are None
    with patch("custom_components.openwrt.config_flow.create_client", return_value=mock_client), \
         patch("custom_components.openwrt.coordinator.create_client", return_value=mock_client):
        result = await flow.async_step_credentials(
            {"username": "root", "password": "password", "use_ssl": False}
        )
    
    assert result["data"]["host"] == "192.168.1.1"


async def test_config_flow_default_connection_type(hass) -> None:
    """Test that the default connection type is LuCI RPC."""
    from custom_components.openwrt.config_flow import OpenWrtConfigFlow
    from custom_components.openwrt.const import CONNECTION_TYPE_LUCI_RPC
    
    flow = OpenWrtConfigFlow()
    flow.hass = hass
    
    result = await flow.async_step_user()
    assert result["type"] == "form"
    assert result["step_id"] == "user"
    
    # Check schema for default value
    schema = result["data_schema"]
    for key, value in schema.schema.items():
        if key == "connection_type":
            assert value.default() == CONNECTION_TYPE_LUCI_RPC
            break
    else:
        pytest.fail("connection_type not found in schema")
