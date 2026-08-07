"""Test the OpenWrt coordinator."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.openwrt.api.base import OpenWrtData, SystemResources
from custom_components.openwrt.coordinator import OpenWrtDataCoordinator


@pytest.mark.asyncio
async def test_coordinator_raises_update_failed_on_timeout() -> None:
    """Test that coordinator raises UpdateFailed when update fails with timeout."""
    hass = MagicMock()
    hass.loop = MagicMock()
    hass.loop.time = MagicMock(return_value=123456789.0)
    config_entry = MagicMock()
    config_entry.options = {}
    config_entry.data = {
        "host": "192.168.1.1",
        "username": "root",
        "password": "password",
    }
    config_entry.entry_id = "test_entry"

    mock_client = AsyncMock()
    mock_client.connected = True

    with patch("custom_components.openwrt.coordinator.storage.Store") as mock_store:
        mock_store.return_value.async_load = AsyncMock(return_value={})
        coordinator = OpenWrtDataCoordinator(hass, config_entry, mock_client)

    # Set initial data
    initial_data = OpenWrtData(
        system_resources=SystemResources(uptime=100),
        connected_devices=[],
        network_interfaces=[],
        wireless_interfaces=[],
    )
    coordinator.data = initial_data

    async def get_all_data_err(*args, **kwargs):
        msg = "Connection timed out"
        raise TimeoutError(msg)

    async def connect_err(*args, **kwargs):
        msg = "Reconnect failed"
        raise Exception(msg)

    mock_client.get_all_data = get_all_data_err
    mock_client.connect = connect_err

    # Run update - should raise UpdateFailed and set client connected to False
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()

    assert mock_client._connected is False


@pytest.mark.asyncio
async def test_coordinator_update_failed_on_new_install() -> None:
    """Test that coordinator raises UpdateFailed if no stale data is available."""
    hass = MagicMock()
    hass.loop = MagicMock()
    hass.loop.time = MagicMock(return_value=123456789.0)
    config_entry = MagicMock()
    config_entry.options = {}
    config_entry.data = {
        "host": "192.168.1.1",
        "username": "root",
        "password": "password",
    }
    config_entry.entry_id = "test_entry"

    mock_client = AsyncMock()
    mock_client.connected = True

    with patch("custom_components.openwrt.coordinator.storage.Store") as mock_store:
        mock_store.return_value.async_load = AsyncMock(return_value={})
        coordinator = OpenWrtDataCoordinator(hass, config_entry, mock_client)
    coordinator.data = None

    async def get_all_data_err(*args, **kwargs):
        msg = "Connection timed out"
        raise TimeoutError(msg)

    async def connect_err(*args, **kwargs):
        msg = "Reconnect failed"
        raise Exception(msg)

    mock_client.get_all_data = get_all_data_err
    mock_client.connect = connect_err

    # Run update - should raise UpdateFailed
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


@pytest.mark.asyncio
async def test_coordinator_reverse_dns_resolution(hass: HomeAssistant) -> None:
    """Test that coordinator resolves reverse DNS if option is enabled."""
    import socket

    from custom_components.openwrt.api.base import ConnectedDevice, DhcpLease
    from custom_components.openwrt.const import CONF_REVERSE_DNS

    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.options = {CONF_REVERSE_DNS: True}
    entry.data = {"host": "192.168.1.1"}

    mock_client = AsyncMock()
    coordinator = OpenWrtDataCoordinator(hass, entry, mock_client)

    data = OpenWrtData()
    data.connected_devices = [
        ConnectedDevice(mac="aa:bb:cc:dd:ee:ff", ip="192.168.1.100", hostname=""),
        ConnectedDevice(
            mac="11:22:33:44:55:66", ip="192.168.1.101", hostname="Existing"
        ),
    ]
    data.dhcp_leases = [
        DhcpLease(mac="22:33:44:55:66:77", ip="192.168.1.102", hostname="*"),
    ]

    def mock_gethostbyaddr(ip):
        if ip == "192.168.1.100":
            return ("resolved-device.local", [], [ip])
        if ip == "192.168.1.102":
            return ("resolved-lease.local", [], [ip])
        raise socket.herror()

    async def mock_async_add_executor_job(func, *args):
        return func(*args)

    with patch.object(
        hass, "async_add_executor_job", side_effect=mock_async_add_executor_job
    ):
        with patch("socket.gethostbyaddr", side_effect=mock_gethostbyaddr):
            await coordinator._async_resolve_reverse_dns(data)

    assert data.connected_devices[0].hostname == "resolved-device.local"
    assert data.connected_devices[1].hostname == "Existing"
    assert data.dhcp_leases[0].hostname == "resolved-lease.local"


@pytest.mark.asyncio
async def test_coordinator_cleanup_orphaned_ap_devices() -> None:
    """Test that orphaned AP devices (model='Access Point' or name starting with 'AP ') are removed."""
    from custom_components.openwrt.api.base import WirelessInterface
    from custom_components.openwrt.const import DOMAIN

    hass = MagicMock()
    entry = MagicMock()
    entry.entry_id = "test_entry_id"
    entry.unique_id = "94:83:c4:ac:7a:13"
    entry.data = {"host": "192.168.1.1"}
    entry.options = {}

    mock_client = AsyncMock()
    coordinator = OpenWrtDataCoordinator(hass, entry, mock_client)
    coordinator.router_id = "94:83:c4:ac:7a:13"

    dev_registry = MagicMock()
    dev_router = MagicMock(id="dev_router")
    dev_registry.async_get_device.return_value = dev_router

    # Mock 2 AP devices: 1 active, 1 orphaned/ghost from old installation
    dev_active_ap = MagicMock(
        id="dev_active_ap",
        name="AP GL-MT6000-a11 (2.4 GHz)",
        model="Access Point",
        config_entries={entry.entry_id},
        identifiers={(DOMAIN, format_ap_device_id("94:83:c4:ac:7a:13", "GL-MT6000-a11_2.4 GHz"))},
        via_device_id="dev_router",
        disabled_by=None,
        entry_type=None,
    )
    dev_orphan_ap = MagicMock(
        id="dev_orphan_ap",
        name="AP GL-MT6000-a11 (2.4 GHz)",
        model="Access Point",
        config_entries={entry.entry_id},
        identifiers={(DOMAIN, "94:83:c4:ac:7a:13_ap_ra0")},
        via_device_id="dev_router",
        disabled_by=None,
        entry_type=None,
    )

    dev_registry.devices = {
        "dev_active_ap": dev_active_ap,
        "dev_orphan_ap": dev_orphan_ap,
    }

    data = OpenWrtData()
    data.wireless_interfaces = [
        WirelessInterface(
            name="ra0",
            ssid="GL-MT6000-a11",
            band="2.4 GHz",
        )
    ]

    with patch(
        "homeassistant.helpers.device_registry.async_get", return_value=dev_registry
    ):
        await coordinator._async_update_device_registry(data)

    # Orphaned AP device (dev_orphan_ap) should be removed
    dev_registry.async_remove_device.assert_called_once_with("dev_orphan_ap")
