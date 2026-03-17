"""Test the OpenWrt device tracker platform."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.components.device_tracker import SourceType
from homeassistant.const import CONF_HOST
from homeassistant.helpers import (
    device_registry as dr,
)

from custom_components.openwrt.api.base import ConnectedDevice, OpenWrtData
from custom_components.openwrt.const import (
    CONF_CONSIDER_HOME,
)
from custom_components.openwrt.device_tracker import OpenWrtDeviceTracker


@pytest.fixture
def mock_coordinator():
    """Mock coordinator."""
    coordinator = MagicMock()
    coordinator.data = OpenWrtData()
    return coordinator


@pytest.fixture
def mock_config_entry():
    """Mock config entry."""
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.unique_id = "11:22:33:44:55:66"
    entry.data = {CONF_HOST: "192.168.1.1"}
    entry.options = {CONF_CONSIDER_HOME: 20}  # 20 seconds for testing
    return entry


def test_device_tracker_init(mock_coordinator, mock_config_entry) -> None:
    """Test device tracker initialization."""
    mac = "AA:BB:CC:DD:EE:FF"
    tracker = OpenWrtDeviceTracker(mock_coordinator, mock_config_entry, mac)

    assert tracker.unique_id == f"test_entry_tracker_{mac.lower()}"
    assert tracker.mac_address == mac.lower()
    assert tracker.source_type == SourceType.ROUTER
    assert tracker._consider_home == timedelta(seconds=20)


def test_device_tracker_is_connected_logic(mock_coordinator, mock_config_entry) -> None:
    """Test is_connected with consider_home logic."""
    mac = "aa:bb:cc:dd:ee:ff"
    tracker = OpenWrtDeviceTracker(mock_coordinator, mock_config_entry, mac)

    # 1. Initially not connected
    mock_coordinator.data.connected_devices = []
    assert tracker.is_connected is False

    # 2. Device appears
    # Ensure mac is lowercased as real coordinator would do
    mock_coordinator.data.connected_devices = [ConnectedDevice(mac=mac.lower(), connected=True)]
    assert tracker.is_connected is True
    assert tracker._last_seen is not None
    last_seen_initial = tracker._last_seen

    # 3. Device disappears, should stay connected due to consider_home
    mock_coordinator.data.connected_devices = []
    assert tracker.is_connected is True

    # 4. Advance time but stay within 20s window
    with patch("custom_components.openwrt.device_tracker.datetime") as mock_datetime:
        now = last_seen_initial + timedelta(seconds=10)
        mock_datetime.now.return_value = now
        assert tracker.is_connected is True

        # 5. Advance time beyond 20s window
        now = last_seen_initial + timedelta(seconds=25)
        mock_datetime.now.return_value = now
        assert tracker.is_connected is False


def test_device_tracker_attributes(mock_coordinator, mock_config_entry) -> None:
    """Test device tracker attributes."""
    mac = "aa:bb:cc:dd:ee:ff"
    tracker = OpenWrtDeviceTracker(mock_coordinator, mock_config_entry, mac)

    mock_coordinator.data.connected_devices = [
        ConnectedDevice(
            mac=mac.lower(),
            ip="192.168.1.100",
            hostname="my-phone",
            interface="br-lan",
            connected=True,
            connection_type="wired",
            neighbor_state="REACHABLE",
            uptime=3600,
        )
    ]

    assert tracker.hostname == "my-phone"
    assert tracker.ip_address == "192.168.1.100"
    assert tracker.name == "my-phone"

    attrs = tracker.extra_state_attributes
    assert attrs["mac"] == mac.lower()
    assert attrs["connection_type"] == "wired"
    assert attrs["neighbor_state"] == "REACHABLE"
    assert attrs["interface"] == "br-lan"
    assert attrs["uptime"] == 3600


def test_device_tracker_stable_device_info(mock_coordinator, mock_config_entry) -> None:
    """Test that device_info uses stable entry.unique_id (MAC)."""
    mac = "aa:bb:cc:dd:ee:ff"

    with patch("custom_components.openwrt.device_tracker.DeviceInfo", side_effect=lambda **kwargs: kwargs):
        tracker = OpenWrtDeviceTracker(mock_coordinator, mock_config_entry, mac)

        # Change entry host IP
        mock_config_entry.data[CONF_HOST] = "192.168.1.200"

        device_info = tracker.device_info
        # via_device should be the router's stable unique_id (MAC), not the host IP
        # We check the second part of the tuple as DOMAIN might be mocked
        assert device_info["via_device"][1] == "11:22:33:44:55:66"
        assert (dr.CONNECTION_NETWORK_MAC, mac.lower()) in device_info["connections"]
