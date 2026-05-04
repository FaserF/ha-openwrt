"""Tests for package and capability detection in OpenWrt integration."""

from unittest.mock import AsyncMock, patch

import pytest

from custom_components.openwrt.api.luci_rpc import LuciRpcClient
from custom_components.openwrt.api.ssh import SshClient
from custom_components.openwrt.api.ubus import UbusClient


@pytest.mark.asyncio
async def test_ubus_package_detection_extended():
    """Test extended package detection via Ubus."""
    client = UbusClient(host="192.168.1.1", username="root", password="password")
    client._session_id = "test_token"
    client._connected = True

    with (
        patch.object(client, "_list_objects", new_callable=AsyncMock) as mock_list,
        patch.object(client, "_call", new_callable=AsyncMock) as mock_call,
        patch.object(
            client,
            "get_installed_packages",
            new_callable=AsyncMock,
        ) as mock_pkg,
    ):
        mock_list.return_value = ["adblock", "luci-rpc", "sqm"]
        mock_pkg.return_value = []

        def call_side_effect(obj, method, params=None):
            if obj == "file" and method == "exec":
                return {"stdout": "1\n0\n1\n0\n1\n0\n0\n0\n0\n0\n1\n0\n1\n"}
            if obj == "system" and method == "info":
                return {"release": {"distribution": "OpenWrt", "version": "23.05"}}
            return {}

        mock_call.side_effect = call_side_effect

        packages = await client.check_packages()

        assert packages.sqm_scripts is True
        assert packages.adblock is True
        assert packages.iwinfo is True
        assert packages.wireguard is True
        assert packages.ban_ip is True


@pytest.mark.asyncio
async def test_ssh_package_detection_extended():
    """Test extended package detection via SSH."""
    client = SshClient(host="192.168.1.1", username="root", password="password")
    client._connected = True

    with (
        patch.object(client, "_exec", new_callable=AsyncMock) as mock_exec,
        patch.object(
            client,
            "get_installed_packages",
            new_callable=AsyncMock,
        ) as mock_pkg,
    ):
        mock_exec.return_value = "0\n0\n0\n0\n0\n1\n0\n0\n0\n0\n0\n1\n0\n"
        mock_pkg.return_value = ["sqm-scripts"]

        packages = await client.check_packages()

        assert packages.simple_adblock is True
        assert packages.openvpn is True
        assert packages.sqm_scripts is True


@pytest.mark.asyncio
async def test_luci_rpc_package_detection_extended():
    """Test extended package detection via LuCI RPC."""
    client = LuciRpcClient(host="192.168.1.1", username="root", password="password")
    client._session_id = "test_token"
    client._connected = True

    with (
        patch.object(client, "_rpc_call", new_callable=AsyncMock) as mock_rpc,
        patch.object(client, "execute_command", new_callable=AsyncMock) as mock_exec,
        patch.object(
            client,
            "get_installed_packages",
            new_callable=AsyncMock,
        ) as mock_pkg,
    ):
        mock_rpc.return_value = "0\n0\n0\n0\n0\n0\n0\n0\n0\n0\n1\n0\n0\n"
        mock_exec.return_value = "mwan3"
        mock_pkg.return_value = ["etherwake"]

        packages = await client.check_packages()

        assert packages.adblock is True
        assert packages.mwan3 is True
        assert packages.etherwake is True


@pytest.mark.asyncio
async def test_packages_wireless_inference_from_iwinfo() -> None:
    """Test that packages.wireless is inferred from iwinfo if network.wireless is missing."""
    client = UbusClient("192.168.1.1", "root", "pass")
    client._list_objects = AsyncMock(return_value=["iwinfo", "system", "uci"])
    client._get_object_methods = AsyncMock(return_value=["assoclist"])
    client._call = AsyncMock(return_value={})

    packages = await client.check_packages()
    assert packages.iwinfo is True
    assert packages.wireless is True


@pytest.mark.asyncio
async def test_packages_wireless_inference_from_hostapd() -> None:
    """Test that packages.wireless is inferred from hostapd.* objects."""
    client = UbusClient("192.168.1.1", "root", "pass")
    client._list_objects = AsyncMock(return_value=["hostapd.wlan0", "system"])
    client._call = AsyncMock(return_value={})

    packages = await client.check_packages()
    assert packages.wireless is True


@pytest.mark.asyncio
async def test_packages_wireless_inference_from_full_list() -> None:
    """Test that packages.wireless is inferred if only the package name matches in step 4."""
    client = UbusClient("192.168.1.1", "root", "pass")
    client._list_objects = AsyncMock(return_value=[])
    client._call = AsyncMock(return_value={})
    client.get_installed_packages = AsyncMock(return_value=["iwinfo", "base-files"])

    packages = await client.check_packages()
    assert packages.iwinfo is True
    assert packages.wireless is True
