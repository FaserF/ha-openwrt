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
    """Test successful user flow with permissions step."""
    from custom_components.openwrt.api.base import (
        DeviceInfo,
        OpenWrtPackages,
        OpenWrtPermissions,
    )
    from custom_components.openwrt.config_flow import OpenWrtConfigFlow

    flow = OpenWrtConfigFlow()
    flow.hass = hass

    # User Step - Mock asyncio.open_connection to simulate reachable host
    mock_writer = AsyncMock()
    mock_writer.close = MagicMock(return_value=None)
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

    # Credentials Step -> Provision User Step (when user is root)
    with (
        patch(
            "custom_components.openwrt.config_flow.create_client",
            return_value=mock_client,
        ),
        patch(
            "custom_components.openwrt.coordinator.create_client",
            return_value=mock_client,
        ),
    ):
        mock_client.user_exists.return_value = False
        result2 = await flow.async_step_credentials(
            {
                "username": "root",
                "password": "password",
                "use_ssl": False,
            }
        )

    assert str(result2["type"]).upper() == "FORM"
    assert result2["step_id"] == "provision_user"

    # Provision User Step (Skip) -> Permissions Step
    result_skip = await flow.async_step_provision_user({"mode": "skip"})
    assert str(result_skip["type"]).upper() == "FORM"
    assert result_skip["step_id"] == "permissions_ubus"

    # Permissions Step -> Packages Step
    result3 = await flow.async_step_permissions({})
    assert str(result3["type"]).upper() == "FORM"
    assert result3["step_id"] == "packages"

    # Packages Step -> Create Entry
    result4 = await flow.async_step_packages({})
    assert str(result4["type"]).upper() == "CREATE_ENTRY"
    assert result4["title"] == "OpenWrtTest"
    assert result4["data"]["host"] == "192.168.1.1"


async def test_full_user_flow_with_check_errors(hass) -> None:
    """Test user flow when permission and package checks fail; should skip the permissions step."""
    from custom_components.openwrt.api.base import DeviceInfo
    from custom_components.openwrt.config_flow import OpenWrtConfigFlow

    flow = OpenWrtConfigFlow()
    flow.hass = hass

    # User Step
    mock_writer = AsyncMock()
    mock_writer.close = MagicMock(return_value=None)
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

    # Credentials Step - when root, should go to provision_user
    with (
        patch(
            "custom_components.openwrt.config_flow.create_client",
            return_value=mock_client,
        ),
        patch(
            "custom_components.openwrt.coordinator.create_client",
            return_value=mock_client,
        ),
    ):
        mock_client.user_exists.return_value = False
        result = await flow.async_step_credentials(
            {"username": "root", "password": "password", "use_ssl": False}
        )

    assert result["step_id"] == "provision_user"

    # Provision User Step (Skip) -> Should skip permissions because of errors and go to Packages or result
    with patch("custom_components.openwrt.config_flow.asyncio.sleep"):
        result2 = await flow.async_step_provision_user({"mode": "skip"})

    assert str(result2["type"]).upper() == "CREATE_ENTRY"
    assert result2["data"]["host"] == "192.168.1.1"


async def test_config_flow_default_connection_type(hass) -> None:
    """Test that the default connection type is LuCI RPC."""
    from custom_components.openwrt.config_flow import OpenWrtConfigFlow
    from custom_components.openwrt.const import CONNECTION_TYPE_LUCI_RPC

    flow = OpenWrtConfigFlow()
    flow.hass = hass

    # Mock aiohttp session for discovery
    mock_session = MagicMock()
    mock_response = MagicMock()
    mock_response.text = AsyncMock(return_value="")
    mock_response.headers = {}

    # Properly mock the async context manager for get and post
    mock_session.get.return_value.__aenter__ = AsyncMock(return_value=mock_response)
    mock_session.get.return_value.__aexit__ = AsyncMock()
    mock_session.post.return_value.__aenter__ = AsyncMock(return_value=mock_response)
    mock_session.post.return_value.__aexit__ = AsyncMock()

    with patch(
        "custom_components.openwrt.config_flow.async_get_clientsession",
        return_value=mock_session,
    ):
        result = await flow.async_step_user()
    assert result["type"].lower() == "form"
    assert result["step_id"] == "user"

    # Check schema for default value
    schema = result["data_schema"]
    for key, _value in schema.schema.items():
        if key == "connection_type":
            assert key.default() == CONNECTION_TYPE_LUCI_RPC
            break
    else:
        pytest.fail("connection_type not found in schema")
