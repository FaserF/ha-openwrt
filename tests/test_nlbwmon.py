# Project: ha-openwrt
# File: tests/test_nlbwmon.py
# Coverage: 100%

"""Test nlbwmon sensors."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.openwrt.api.base import OpenWrtData
from custom_components.openwrt.sensor import (
    OpenWrtNlbwmonRxSensor,
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
    """Test that nlbwmon per-client sensor device_info aligns with global device mapping."""
    coordinator = MagicMock()
    entry = MagicMock()
    entry.entry_id = "test_entry"
    mac = "AA:BB:CC:DD:EE:FF"

    with patch(
        "custom_components.openwrt.sensor.DeviceInfo",
        side_effect=lambda **kwargs: kwargs,
    ):
        sensor = OpenWrtNlbwmonRxSensor(coordinator, entry, mac, "Test Host")

        device_info = sensor.device_info
        assert any(ident[1] == mac.lower() for ident in device_info["identifiers"])


class _DevicesWithoutMapping:
    """Device collection that, like HA 2026.9+, may only be iterated."""

    def __init__(self, devices: list[MagicMock]) -> None:
        self._devices = devices

    def __iter__(self):
        return iter(self._devices)

    def __getattr__(self, name: str):
        raise AssertionError(f"registry.devices.{name} is deprecated mapping access")


async def test_nlbwmon_top_hosts_iterates_registry_without_mapping_access() -> None:
    """Hostnames come from the registry without touching its mapping attributes."""
    import json

    from homeassistant.helpers import device_registry as dr

    from custom_components.openwrt.coordinator import OpenWrtDataCoordinator

    mac = "00:00:5E:00:53:21"
    entry = MagicMock()
    entry.entry_id = "test_entry_id"
    entry.data = {"host": "192.0.2.1"}
    entry.options = {}
    client = MagicMock()
    client.file_exec = AsyncMock(
        return_value={
            "stdout": json.dumps(
                {
                    "columns": ["ip", "mac", "rx_bytes", "tx_bytes", "conns"],
                    "data": [["192.0.2.21", mac.lower(), 1000, 500, 3]],
                }
            ),
            "stderr": "",
        }
    )
    client.get_dhcp_leases = AsyncMock(return_value=[])
    coordinator = OpenWrtDataCoordinator(MagicMock(), entry, client)

    device = MagicMock(
        name_by_user=None, connections={(dr.CONNECTION_NETWORK_MAC, mac.lower())}
    )
    device.name = "Test laptop"
    registry = MagicMock()
    registry.devices = _DevicesWithoutMapping([device])

    data = OpenWrtData()
    with patch(
        "custom_components.openwrt.coordinator.features.dr.async_get",
        return_value=registry,
    ):
        await coordinator._async_fetch_nlbwmon_top_hosts_data(data)

    assert data.nlbwmon_top_hosts["top_hosts"][0]["hostname"] == "Test laptop"
