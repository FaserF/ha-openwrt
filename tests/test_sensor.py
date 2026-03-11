"""Test the OpenWrt sensor platform."""

from __future__ import annotations

from unittest.mock import MagicMock

from homeassistant.components.sensor import SensorStateClass
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant

from custom_components.openwrt.api.base import OpenWrtData, DeviceInfo, SystemResources
from custom_components.openwrt.sensor import SYSTEM_SENSORS, OpenWrtSensorEntity


def test_uptime_conversion() -> None:
    """Test that uptime is converted from seconds to minutes."""
    data = OpenWrtData(
        system_resources=SystemResources(
            uptime=120,
            memory_total=1000,
            memory_used=500,
            load_1min=0.1,
        ),
        connected_devices=[],
        network_interfaces=[],
        wireless_interfaces=[],
    )
    
    coordinator = MagicMock()
    coordinator.data = data
    entry = MagicMock()
    entry.entry_id = "test"
    
    # Find uptime description
    uptime_desc = next(d for d in SYSTEM_SENSORS if d.key == "uptime")
    
    # Check description
    assert uptime_desc.native_unit_of_measurement == UnitOfTime.MINUTES
    
    # Check value conversion via entity
    sensor = OpenWrtSensorEntity(coordinator, entry, uptime_desc)
    assert sensor.native_value == 2.0  # 120 / 60
    
    # Test decimal rounding
    data.system_resources.uptime = 125
    assert sensor.native_value == 2.1  # round(125/60, 1)


def test_sensor_english_names() -> None:
    """Test that system sensors have explicit English names."""
    # Check some key sensors in SYSTEM_SENSORS
    memory_usage = next(d for d in SYSTEM_SENSORS if d.key == "memory_usage")
    assert memory_usage.name == "Memory Usage"
    
    load_sensor = next(d for d in SYSTEM_SENSORS if d.key == "load_1min")
    assert load_sensor.name == "Load (1m)"
    
    uptime_sensor = next(d for d in SYSTEM_SENSORS if d.key == "uptime")
    assert uptime_sensor.name == "Uptime"
