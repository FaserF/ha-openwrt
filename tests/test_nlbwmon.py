"""Test nlbwmon sensors."""
from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.openwrt.api.base import OpenWrtData
from custom_components.openwrt.sensor import OpenWrtNlbwmonTopHostsSensor


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
