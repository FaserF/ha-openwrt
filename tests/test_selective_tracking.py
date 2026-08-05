"""Test selective device tracking in OpenWrt coordinator."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.openwrt.api.base import (
    ConnectedDevice,
    DhcpLease,
    OpenWrtData,
    SystemResources,
)
from custom_components.openwrt.const import (
    CONF_TRACKED_DEVICES,
    DATA_COORDINATOR,
    DOMAIN,
)
from custom_components.openwrt.coordinator import OpenWrtDataCoordinator


def _tracked_device_options(flow_result_selector) -> dict[str, str]:
    """Return the {value: label} mapping offered by the tracked devices selector."""
    options = flow_result_selector.call_args.kwargs["options"]
    return {opt["value"]: opt["label"] for opt in options}


@pytest.mark.asyncio
async def test_coordinator_selective_tracking() -> None:
    """Test that coordinator filters devices based on whitelist."""
    hass = MagicMock()
    hass.loop = MagicMock()
    hass.loop.time = MagicMock(return_value=123456789.0)

    config_entry = MagicMock()
    # Whitelist only device1
    config_entry.options = {CONF_TRACKED_DEVICES: ["00:bb:cc:dd:ee:01"]}
    config_entry.data = {"host": "192.168.1.1"}
    config_entry.entry_id = "test_entry"

    mock_client = AsyncMock()
    mock_client.connected = True

    # Mock data with two devices
    raw_data = OpenWrtData(
        system_resources=SystemResources(uptime=100),
        connected_devices=[
            ConnectedDevice(
                mac="00:bb:cc:dd:ee:01",
                hostname="device1",
                interface="br-lan",
                is_wireless=True,
            ),
            ConnectedDevice(
                mac="00:bb:cc:dd:ee:02",
                hostname="device2",
                interface="br-lan",
                is_wireless=True,
            ),
        ],
        dhcp_leases=[
            DhcpLease(mac="00:bb:cc:dd:ee:01", hostname="device1", ip="192.168.1.10"),
            DhcpLease(mac="00:bb:cc:dd:ee:02", hostname="device2", ip="192.168.1.11"),
        ],
        network_interfaces=[],
        wireless_interfaces=[],
    )
    mock_client.get_all_data.return_value = raw_data

    with patch("custom_components.openwrt.coordinator.storage.Store") as mock_store:
        mock_store.return_value.async_load = AsyncMock(return_value={})
        mock_store.return_value.async_save = AsyncMock()
        coordinator = OpenWrtDataCoordinator(hass, config_entry, mock_client)

    # Run update
    data = await coordinator._async_update_data()

    # Should only contain device1
    assert len(data.connected_devices) == 1
    assert data.connected_devices[0].mac == "00:bb:cc:dd:ee:01"

    assert len(data.dhcp_leases) == 1
    assert data.dhcp_leases[0].mac == "00:bb:cc:dd:ee:01"


@pytest.mark.asyncio
async def test_coordinator_no_whitelist() -> None:
    """Test that coordinator tracks all devices if no whitelist is configured."""
    hass = MagicMock()
    hass.loop = MagicMock()
    hass.loop.time = MagicMock(return_value=123456789.0)

    config_entry = MagicMock()
    config_entry.options = {}  # No whitelist
    config_entry.data = {"host": "192.168.1.1"}
    config_entry.entry_id = "test_entry"

    mock_client = AsyncMock()
    mock_client.connected = True

    raw_data = OpenWrtData(
        system_resources=SystemResources(uptime=100),
        connected_devices=[
            ConnectedDevice(
                mac="00:bb:cc:dd:ee:01",
                hostname="device1",
                interface="br-lan",
                is_wireless=True,
            ),
            ConnectedDevice(
                mac="00:bb:cc:dd:ee:02",
                hostname="device2",
                interface="br-lan",
                is_wireless=True,
            ),
        ],
        dhcp_leases=[
            DhcpLease(mac="00:bb:cc:dd:ee:01", hostname="device1", ip="192.168.1.10"),
            DhcpLease(mac="00:bb:cc:dd:ee:02", hostname="device2", ip="192.168.1.11"),
        ],
        network_interfaces=[],
        wireless_interfaces=[],
    )
    mock_client.get_all_data.return_value = raw_data

    with patch("custom_components.openwrt.coordinator.storage.Store") as mock_store:
        mock_store.return_value.async_load = AsyncMock(return_value={})
        mock_store.return_value.async_save = AsyncMock()
        coordinator = OpenWrtDataCoordinator(hass, config_entry, mock_client)

    # Run update
    data = await coordinator._async_update_data()

    # Should contain both devices
    assert len(data.connected_devices) == 2
    assert len(data.dhcp_leases) == 2


@pytest.mark.asyncio
async def test_coordinator_client_counts_ignore_whitelist() -> None:
    """Test that client counts reflect total occupancy, ignoring the whitelist."""
    hass = MagicMock()
    hass.loop = MagicMock()
    hass.loop.time = MagicMock(return_value=123456789.0)

    config_entry = MagicMock()
    # Whitelist only device1
    config_entry.options = {CONF_TRACKED_DEVICES: ["00:bb:cc:dd:ee:01"]}
    config_entry.data = {"host": "192.168.1.1"}
    config_entry.entry_id = "test_entry"

    mock_client = AsyncMock()
    mock_client.connected = True

    # Mock data with three devices:
    # 1. device1 (wireless, whitelisted)
    # 2. device2 (wireless, NOT whitelisted)
    # 3. device3 (wired, NOT whitelisted)
    raw_data = OpenWrtData(
        system_resources=SystemResources(uptime=100),
        connected_devices=[
            ConnectedDevice(
                mac="00:bb:cc:dd:ee:01",
                hostname="device1",
                interface="br-lan",
                is_wireless=True,
                connected=True,
            ),
            ConnectedDevice(
                mac="00:bb:cc:dd:ee:02",
                hostname="device2",
                interface="br-lan",
                is_wireless=True,
                connected=True,
            ),
            ConnectedDevice(
                mac="00:bb:cc:dd:ee:03",
                hostname="device3",
                interface="br-lan",
                is_wireless=False,
                connected=True,
            ),
        ],
        dhcp_leases=[],
        network_interfaces=[],
        wireless_interfaces=[],
    )
    mock_client.get_all_data.return_value = raw_data

    with patch("custom_components.openwrt.coordinator.storage.Store") as mock_store:
        mock_store.return_value.async_load = AsyncMock(return_value={})
        mock_store.return_value.async_save = AsyncMock()
        coordinator = OpenWrtDataCoordinator(hass, config_entry, mock_client)

    # Run update
    data = await coordinator._async_update_data()

    # 1. Verify tracking (uses connected_devices)
    # Only device1 should be tracked because of the whitelist
    assert len(data.connected_devices) == 1
    assert data.connected_devices[0].mac == "00:bb:cc:dd:ee:01"

    # 2. Verify client counts (uses all_connected_devices)
    # All 3 devices should be in all_connected_devices regardless of whitelist
    assert len(data.all_connected_devices) == 3

    # Check the counts as they would be used by sensors
    connected_count = sum(1 for d in data.all_connected_devices if d.connected)
    wireless_count = sum(
        1 for d in data.all_connected_devices if d.is_wireless and d.connected
    )
    wired_count = sum(
        1 for d in data.all_connected_devices if not d.is_wireless and d.connected
    )

    assert connected_count == 3
    assert wireless_count == 2
    assert wired_count == 1


def _make_options_flow(all_connected_devices, device_history, tracked):
    """Build an options flow wired to a coordinator with the given state."""
    from custom_components.openwrt.config_flow import OpenWrtOptionsFlow

    config_entry = MagicMock()
    config_entry.entry_id = "test_entry"
    config_entry.options = {CONF_TRACKED_DEVICES: tracked}

    coordinator = MagicMock()
    coordinator.data = OpenWrtData(all_connected_devices=all_connected_devices)
    coordinator._device_history = device_history

    hass = MagicMock()
    hass.data = {DOMAIN: {config_entry.entry_id: {DATA_COORDINATOR: coordinator}}}

    flow = OpenWrtOptionsFlow(config_entry)
    flow.hass = hass
    return flow


@pytest.mark.asyncio
async def test_options_flow_offers_untracked_devices() -> None:
    """Untracked devices must be selectable in the options flow.

    Regression test: candidates used to come from _device_history alone, which
    only ever receives whitelisted devices. The candidate list then equalled the
    current selection, so the multi-select rendered with nothing to pick and the
    tracked set could never grow.
    """
    flow = _make_options_flow(
        all_connected_devices=[
            ConnectedDevice(mac="00:BB:CC:DD:EE:01", hostname="device1"),
            ConnectedDevice(mac="00:BB:CC:DD:EE:02", hostname="device2"),
            ConnectedDevice(mac="00:BB:CC:DD:EE:03", hostname="device3"),
        ],
        # Only the whitelisted device ever made it into history.
        device_history={
            "00:bb:cc:dd:ee:01": {"initially_seen": 1.0, "last_seen": 2.0},
        },
        tracked=["00:bb:cc:dd:ee:01"],
    )

    with patch("custom_components.openwrt.config_flow.selector") as mock_selector:
        result = await flow.async_step_options_select_devices()

    assert result["step_id"] == "options_select_devices"

    options = _tracked_device_options(mock_selector.SelectSelectorConfig)
    assert set(options) == {
        "00:bb:cc:dd:ee:01",
        "00:bb:cc:dd:ee:02",
        "00:bb:cc:dd:ee:03",
    }
    # The two untracked devices are what makes the dropdown usable at all.
    assert set(options) - {"00:bb:cc:dd:ee:01"}

    # Hostnames come from the live device list, not the MAC-only history.
    assert options["00:bb:cc:dd:ee:02"] == "device2 (00:BB:CC:DD:EE:02)"


@pytest.mark.asyncio
async def test_options_flow_keeps_offline_and_selected_devices() -> None:
    """Offline history devices and stored selections stay selectable."""
    flow = _make_options_flow(
        all_connected_devices=[
            ConnectedDevice(mac="00:bb:cc:dd:ee:01", hostname="device1"),
        ],
        device_history={
            "00:bb:cc:dd:ee:01": {"hostname": "device1"},
            # Seen before, currently offline.
            "00:bb:cc:dd:ee:09": {"hostname": "laptop"},
            # Defensive: stored history is not guaranteed to hold dicts.
            "00:bb:cc:dd:ee:99": "corrupt",
        },
        # Tracked but never seen by this coordinator yet.
        tracked=["00:bb:cc:dd:ee:01", "00:bb:cc:dd:ee:aa"],
    )

    with patch("custom_components.openwrt.config_flow.selector") as mock_selector:
        await flow.async_step_options_select_devices()

    options = _tracked_device_options(mock_selector.SelectSelectorConfig)

    # Offline device keeps its stored hostname.
    assert options["00:bb:cc:dd:ee:09"] == "laptop (00:bb:cc:dd:ee:09)"
    # Every stored selection remains a valid option.
    assert "00:bb:cc:dd:ee:aa" in options
    assert "00:bb:cc:dd:ee:99" not in options


@pytest.mark.asyncio
async def test_options_flow_without_coordinator_data() -> None:
    """A coordinator that has not completed a refresh must not break the form."""
    flow = _make_options_flow(
        all_connected_devices=[],
        device_history={"00:bb:cc:dd:ee:01": {"hostname": "device1"}},
        tracked=["00:bb:cc:dd:ee:01"],
    )
    flow.hass.data[DOMAIN]["test_entry"][DATA_COORDINATOR].data = None

    with patch("custom_components.openwrt.config_flow.selector") as mock_selector:
        result = await flow.async_step_options_select_devices()

    assert result["step_id"] == "options_select_devices"
    options = _tracked_device_options(mock_selector.SelectSelectorConfig)
    assert options == {"00:bb:cc:dd:ee:01": "device1 (00:bb:cc:dd:ee:01)"}


@pytest.mark.asyncio
async def test_device_history_records_hostname() -> None:
    """History must persist hostnames so offline devices stay readable."""
    hass = MagicMock()
    hass.loop = MagicMock()
    hass.loop.time = MagicMock(return_value=123456789.0)

    config_entry = MagicMock()
    config_entry.options = {}
    config_entry.data = {"host": "192.168.1.1"}
    config_entry.entry_id = "test_entry"

    mock_client = AsyncMock()
    mock_client.connected = True
    mock_client.get_all_data.return_value = OpenWrtData(
        system_resources=SystemResources(uptime=100),
        connected_devices=[
            ConnectedDevice(
                mac="00:bb:cc:dd:ee:01",
                hostname="device1",
                interface="br-lan",
                is_wireless=True,
            ),
        ],
        dhcp_leases=[
            DhcpLease(mac="00:bb:cc:dd:ee:02", hostname="printer", ip="192.168.1.11"),
        ],
        network_interfaces=[],
        wireless_interfaces=[],
    )

    with patch("custom_components.openwrt.coordinator.storage.Store") as mock_store:
        mock_store.return_value.async_load = AsyncMock(return_value={})
        mock_store.return_value.async_save = AsyncMock()
        coordinator = OpenWrtDataCoordinator(hass, config_entry, mock_client)

    await coordinator._async_update_data()

    assert coordinator._device_history["00:bb:cc:dd:ee:01"]["hostname"] == "device1"
    assert coordinator._device_history["00:bb:cc:dd:ee:02"]["hostname"] == "printer"
