# Project: ha-openwrt
# File: tests/test_nlbwmon.py
# Coverage: 100%

"""Test nlbwmon sensors."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from custom_components.openwrt.api.base import OpenWrtData
from custom_components.openwrt.sensor import (
    OpenWrtNlbwmonSensor,
    OpenWrtNlbwmonTopHostsSensor,
)


def test_nlbwmon_top_hosts_sensor() -> None:
    """Test the nlbwmon top hosts sensor."""
    coordinator = MagicMock()
    entry = MagicMock()
    entry.entry_id = "test_entry"
    coordinator.router_id = "test_router"

    sensor = OpenWrtNlbwmonTopHostsSensor(coordinator, entry)

    # Test with no data
    coordinator.data = None
    assert sensor.native_value is None
    assert sensor.extra_state_attributes == {}

    # Test with empty nlbwmon data
    coordinator.data = OpenWrtData(nlbwmon_top_hosts={})
    assert sensor.native_value is None
    assert sensor.extra_state_attributes == {}

    # Test with valid nlbwmon data
    nlbwmon_data = {
        "host_count": 2,
        "total_rx_bytes": 1048576,
        "total_tx_bytes": 524288,
        "top_hosts": [
            {"mac": "00:11:22:33:44:55", "rx_bytes": 800000, "tx_bytes": 400000},
            {"mac": "AA:BB:CC:DD:EE:FF", "rx_bytes": 248576, "tx_bytes": 124288},
        ],
    }
    coordinator.data = OpenWrtData(nlbwmon_top_hosts=nlbwmon_data)

    assert sensor.native_value == 2
    attrs = sensor.extra_state_attributes
    assert attrs["host_count"] == 2
    assert attrs["total_download"] == "1.00 MB"
    assert attrs["total_upload"] == "512.00 KB"
    assert len(attrs["top_hosts"]) == 2


def test_nlbwmon_client_sensor_device_info() -> None:
    """Test that nlbwmon per-client sensor device_info is instance-scoped."""
    coordinator = MagicMock()
    entry = MagicMock()
    entry.entry_id = "test_entry"
    mac = "AA:BB:CC:DD:EE:FF"

    with patch(
        "custom_components.openwrt.sensor.DeviceInfo",
        side_effect=lambda **kwargs: kwargs,
    ):
        sensor = OpenWrtNlbwmonSensor(coordinator, entry, mac, "Test Host")

        device_info = sensor.device_info
        assert any(
            ident[1] == f"test_entry_{mac.lower()}"
            for ident in device_info["identifiers"]
        )
