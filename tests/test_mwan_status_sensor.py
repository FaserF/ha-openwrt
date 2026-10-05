"""Tests for the MWAN3 tracking status sensor."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from homeassistant.components.sensor import SensorDeviceClass

from custom_components.openwrt.api.base import MwanStatus, OpenWrtData
from custom_components.openwrt.sensor import _create_mwan_sensors
from custom_components.openwrt.sensors.base import OpenWrtSensorEntity


def _status_sensor(data: OpenWrtData, iface: str) -> OpenWrtSensorEntity:
    """Create the status sensor of one MWAN3 interface over the given data."""
    coordinator = MagicMock()
    coordinator.data = data
    entry = MagicMock()
    entry.entry_id = "test"
    return next(
        sensor
        for sensor in _create_mwan_sensors(coordinator, entry, iface)
        if sensor.entity_description.key == f"mwan_{iface}_status"
    )


def _data(*statuses: MwanStatus) -> OpenWrtData:
    """Return coordinator data holding the given MWAN3 interfaces."""
    data = OpenWrtData()
    data.mwan_status = list(statuses)
    return data


@pytest.mark.parametrize(
    "status", ["online", "offline", "connecting", "disconnecting", "disabled"]
)
def test_status_sensor_shows_each_tracking_state(status: str) -> None:
    """Every state mwan3track writes is shown and is one of the options."""
    sensor = _status_sensor(
        _data(MwanStatus(interface_name="wan", status=status)), "wan"
    )
    description = sensor.entity_description

    assert sensor.native_value == status
    # Home Assistant rejects an enum sensor whose value is not among its
    # options, or that has a unit or a state class.
    assert description.device_class == SensorDeviceClass.ENUM
    assert description.options is not None
    assert status in description.options
    assert description.native_unit_of_measurement is None
    assert description.state_class is None


def test_recovering_link_is_told_apart_from_a_dead_one() -> None:
    """A link in its recovery checks reads connecting, a dead one offline.

    The online binary sensor is off for both, so an automation that power
    cycles the modem after a long outage also fired while the line was fine.
    """
    data = _data(
        MwanStatus(interface_name="wan", status="connecting"),
        MwanStatus(interface_name="backup", status="offline"),
    )

    assert _status_sensor(data, "wan").native_value == "connecting"
    assert _status_sensor(data, "backup").native_value == "offline"


@pytest.mark.parametrize("status", ["unknown", "", "notracking"])
def test_unknown_or_unexpected_state_reads_unknown(status: str) -> None:
    """A state outside the options reads unknown instead of failing the update."""
    sensor = _status_sensor(
        _data(MwanStatus(interface_name="wan", status=status)), "wan"
    )

    assert sensor.native_value is None


def test_interface_missing_from_the_reply_reads_unknown() -> None:
    """An interface that drops out of the MWAN3 reply reads unknown."""
    data = _data(MwanStatus(interface_name="backup", status="online"))

    assert _status_sensor(data, "wan").native_value is None
