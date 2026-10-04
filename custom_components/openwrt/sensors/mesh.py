"""B.A.T.M.A.N. mesh sensors for OpenWrt."""

from __future__ import annotations

import logging

from homeassistant.components.sensor import (
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
)

from ..coordinator import OpenWrtData, OpenWrtDataCoordinator
from .base import OpenWrtSensorDescription, OpenWrtSensorEntity
from .device import OpenWrtDeviceSensor

_LOGGER = logging.getLogger(__name__)


def _get_batman_global_sensors(
    coordinator: OpenWrtDataCoordinator, entry: ConfigEntry
) -> list[OpenWrtSensorEntity]:
    """Get Batman-adv global sensors."""
    return [
        OpenWrtSensorEntity(
            coordinator,
            entry,
            OpenWrtSensorDescription(
                key="batman_originators_count",
                name="Batman Mesh Originators",
                translation_key="batman_originators_count",
                state_class=SensorStateClass.MEASUREMENT,
                icon="mdi:transit-connection-variant",
                entity_category=EntityCategory.DIAGNOSTIC,
                value_fn=lambda data: len(data.batman_originators),
            ),
        ),
        OpenWrtSensorEntity(
            coordinator,
            entry,
            OpenWrtSensorDescription(
                key="batman_neighbors_count",
                name="Batman Mesh Neighbors",
                translation_key="batman_neighbors_count",
                state_class=SensorStateClass.MEASUREMENT,
                icon="mdi:router-wireless",
                entity_category=EntityCategory.DIAGNOSTIC,
                value_fn=lambda data: len(data.batman_neighbors),
            ),
        ),
        OpenWrtSensorEntity(
            coordinator,
            entry,
            OpenWrtSensorDescription(
                key="batman_gateways_count",
                name="Batman Mesh Gateways",
                translation_key="batman_gateways_count",
                state_class=SensorStateClass.MEASUREMENT,
                icon="mdi:gateway",
                entity_category=EntityCategory.DIAGNOSTIC,
                value_fn=lambda data: len(data.batman_gateways),
            ),
        ),
    ]


def _create_batman_neighbor_sensors(
    coordinator: OpenWrtDataCoordinator,
    entry: ConfigEntry,
    mac: str,
) -> list[OpenWrtDeviceSensor]:
    """Create sensors for a specific Batman neighbor."""
    sensors = []

    # TQ (Transmit Quality) to this neighbor (if it's also an originator)
    def get_tq(data: OpenWrtData) -> int | None:
        for orig in data.batman_originators:
            if orig.mac == mac:
                return orig.tq
        return None

    def is_available(data: OpenWrtData) -> bool:
        return any(orig.mac == mac for orig in data.batman_originators)

    sensors.append(
        OpenWrtDeviceSensor(
            coordinator,
            entry,
            mac,
            SensorEntityDescription(
                key="batman_tq",
                name="Mesh Link Quality (TQ)",
                translation_key="batman_tq",
                native_unit_of_measurement=PERCENTAGE,
                state_class=SensorStateClass.MEASUREMENT,
                icon="mdi:signal-variant",
            ),
            value_fn=get_tq,
            available_fn=is_available,
        )
    )

    return sensors
