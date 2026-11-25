"""Tests for the OpenWrt get_raw_data service and refresh button."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.openwrt.api.base import (
    ConnectedDevice,
    DeviceInfo,
    NetworkInterface,
    OpenWrtData,
    SystemResources,
    WirelessInterface,
)
from custom_components.openwrt.buttons.services import BUTTONS
from custom_components.openwrt.const import (
    DATA_CLIENT,
    DATA_COORDINATOR,
    DOMAIN,
    SERVICE_GET_RAW_DATA,
)
from custom_components.openwrt.services import register_services


@pytest.mark.asyncio
async def test_get_raw_data_service(hass) -> None:
    """Test openwrt.get_raw_data returns cached router summary."""
    coordinator = MagicMock()
    coordinator.data = OpenWrtData(
        device_info=DeviceInfo(
            hostname="openwrt-router",
            model="GL.iNet MT6000",
            release_version="24.10.0",
            kernel_version="6.6.73",
        ),
        system_resources=SystemResources(
            uptime=3600,
            load_1min=0.12,
            memory_total=1000,
            memory_used=250,
        ),
        network_interfaces=[
            NetworkInterface(name="wan", up=True, ipv4_address="203.0.113.10"),
        ],
        wireless_interfaces=[
            WirelessInterface(name="wlan0", ssid="HomeNet", channel=36),
        ],
        all_connected_devices=[
            ConnectedDevice(mac="00:11:22:33:44:01", is_wireless=True, connected=True),
            ConnectedDevice(mac="00:11:22:33:44:02", is_wireless=False, connected=True),
        ],
        external_ip="203.0.113.10",
    )

    hass.data[DOMAIN] = {
        "entry_1": {DATA_COORDINATOR: coordinator, DATA_CLIENT: MagicMock()},
    }
    hass.services = MagicMock()
    registered = {}

    def _capture_register(domain, service, handler, **kwargs):
        registered[(domain, service)] = handler

    hass.services.async_register = _capture_register
    register_services(hass)

    assert (DOMAIN, SERVICE_GET_RAW_DATA) in registered
    handler = registered[(DOMAIN, SERVICE_GET_RAW_DATA)]
    call = MagicMock()
    call.data = {"entry_id": "entry_1"}
    result = await handler(call)

    assert result["device_name"] == "GL.iNet MT6000"
    assert result["firmware_version"] == "24.10.0"
    assert result["memory_usage_pct"] == 25.0
    assert result["wan_status"] == "up"
    assert result["wireless_ssids"] == "HomeNet"
    assert result["connected_devices_count"] == 2
    assert result["wireless_clients_count"] == 1


@pytest.mark.asyncio
async def test_refresh_button_in_buttons() -> None:
    """Test that the refresh static button is defined and callable."""
    refresh_desc = next((b for b in BUTTONS if b.key == "refresh"), None)
    assert refresh_desc is not None
    assert refresh_desc.translation_key == "refresh"
    await refresh_desc.press_fn(AsyncMock())
