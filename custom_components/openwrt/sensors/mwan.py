"""MWAN3 multi-WAN sensors for OpenWrt."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_HOST,
    PERCENTAGE,
    EntityCategory,
)
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ..const import DOMAIN
from ..coordinator import OpenWrtDataCoordinator
from .base import OpenWrtSensorDescription, OpenWrtSensorEntity

_LOGGER = logging.getLogger(__name__)


def _create_mwan_sensors(
    coordinator: OpenWrtDataCoordinator,
    entry: ConfigEntry,
    iface_name: str,
) -> list[OpenWrtSensorEntity]:
    """Create sensors for an MWAN3 interface."""
    return [
        OpenWrtSensorEntity(
            coordinator,
            entry,
            OpenWrtSensorDescription(
                key=f"mwan_{iface_name}_ratio",
                translation_key="mwan_ratio",
                translation_placeholders={"interface": iface_name},
                name=f"MWAN {iface_name} Online Ratio",
                native_unit_of_measurement=PERCENTAGE,
                state_class=SensorStateClass.MEASUREMENT,
                suggested_display_precision=2,
                value_fn=lambda data, n=iface_name: next(
                    (
                        m.boot_online_ratio * 100
                        for m in data.mwan_status
                        if m.interface_name == n and m.boot_online_ratio is not None
                    ),
                    None,
                ),
                attrs_fn=lambda data, n=iface_name: next(
                    (
                        {
                            "coverage_start": (
                                m.coverage_start.isoformat()
                                if m.coverage_start
                                else None
                            )
                        }
                        for m in data.mwan_status
                        if m.interface_name == n
                    ),
                    {},
                ),
            ),
        ),
    ]


class OpenWrtMwanMetricSensor(CoordinatorEntity[OpenWrtDataCoordinator], SensorEntity):
    """MWAN3 metric sensor (latency, packet loss)."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: OpenWrtDataCoordinator,
        entry: ConfigEntry,
        iface: str,
        metric: str,
        pkgs: Any = None,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._iface = iface
        self._metric = metric
        self._attr_unique_id = f"{entry.entry_id}_mwan_{iface}_{metric}"
        self._attr_name = f"MWAN {iface} {metric.replace('_', ' ').title()}"
        self._attr_entity_registry_enabled_default = bool(pkgs and pkgs.mwan3 is True)
        if metric == "latency":
            self._attr_native_unit_of_measurement = "ms"
            self._attr_icon = "mdi:timer-outline"
        else:
            self._attr_native_unit_of_measurement = "%"
            self._attr_icon = "mdi:packet-stack"

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.unique_id or entry.data[CONF_HOST])},
        )

    @property
    def native_value(self) -> float | None:
        """Return native value."""
        if not self.coordinator.data:
            return None
        for m in self.coordinator.data.mwan_status:
            if m.interface_name == self._iface:
                return getattr(m, self._metric)
        return None
