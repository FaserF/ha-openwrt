"""Test the OpenWrt diagnostics."""

from unittest.mock import MagicMock, patch

import pytest

from custom_components.openwrt.api.base import (
    ConnectedDevice,
    DeviceInfo,
    OpenWrtData,
    SystemResources,
)
from custom_components.openwrt.const import DATA_COORDINATOR, DOMAIN
from custom_components.openwrt.diagnostics import async_get_config_entry_diagnostics


class MockConfigEntry:
    """Mock config entry for testing."""

    def __init__(self) -> None:
        self.data = {}
        self.options = {}
        self.entry_id = "test_entry_id"
        self.unique_id = "test_entry_unique_id"


class MockCoordinator:
    """Mock coordinator for testing."""

    def __init__(self, data: OpenWrtData) -> None:
        self.data = data
        self.last_update_success = True


@pytest.mark.asyncio
async def test_diagnostics_client_counts() -> None:
    """Test that diagnostics reports total and wireless client counts correctly using all_connected_devices."""
    device_info = DeviceInfo(
        hostname="TestRouter",
        model="Linksys WHW03",
        mac_address="00:11:22:33:44:55",
    )
    system_resources = SystemResources()

    # 2 wireless clients and 1 wired client, all connected
    wireless1 = ConnectedDevice(
        mac="11:22:33:44:55:66", is_wireless=True, connected=True
    )
    wireless2 = ConnectedDevice(
        mac="22:33:44:55:66:77", is_wireless=True, connected=True
    )
    wired = ConnectedDevice(mac="33:44:55:66:77:88", is_wireless=False, connected=True)
    # 1 disconnected wireless lease
    disconnected_wireless = ConnectedDevice(
        mac="44:55:66:77:88:99", is_wireless=True, connected=False
    )

    data = OpenWrtData(
        device_info=device_info,
        system_resources=system_resources,
        connected_devices=[],  # Empty (e.g. no whitelisted/tracked devices)
        all_connected_devices=[wireless1, wireless2, wired, disconnected_wireless],
    )

    coordinator = MockCoordinator(data)
    entry = MockConfigEntry()

    hass = MagicMock()
    hass.data = {
        DOMAIN: {
            "test_entry_id": {
                DATA_COORDINATOR: coordinator,
            }
        }
    }

    mock_dev_reg = MagicMock()
    mock_dev_reg.devices = {}

    with (
        patch(
            "custom_components.openwrt.diagnostics.async_redact_data",
            side_effect=lambda data, redact_keys: data,
        ),
        patch(
            "homeassistant.helpers.device_registry.async_get", return_value=mock_dev_reg
        ),
        patch("homeassistant.helpers.entity_registry.async_get"),
        patch(
            "homeassistant.helpers.entity_registry.async_entries_for_config_entry",
            return_value=[],
        ),
    ):
        diag = await async_get_config_entry_diagnostics(hass, entry)

    # Should count only CONNECTED devices from all_connected_devices
    assert diag["connected_devices_count"] == 3
    # Should count only CONNECTED wireless devices from all_connected_devices
    assert diag["wireless_clients_count"] == 2

    # Sample should contain only connected devices
    sample_macs = [d["mac"] for d in diag["connected_devices_sample"]]
    assert len(sample_macs) == 3
    assert "11:22:33:44:55:66" in sample_macs
    assert "22:33:44:55:66:77" in sample_macs
    assert "33:44:55:66:77:88" in sample_macs
    assert "44:55:66:77:88:99" not in sample_macs
