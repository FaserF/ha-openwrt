"""Base classes and entity descriptions for OpenWrt sensors."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from homeassistant.components.sensor import (
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ..api.base import OpenWrtData, StorageUsage
from ..const import DOMAIN
from ..coordinator import OpenWrtDataCoordinator


def _format_bytes(num_bytes: int) -> str:
    """Format bytes into human-readable string."""
    value = float(num_bytes)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if value < 1024 or unit == "TB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024
    return f"{num_bytes} B"


def _bytes_to_mb(value: int) -> float:
    """Convert bytes to MB."""
    return round(value / (1024 * 1024), 2)


@dataclass(frozen=True, kw_only=True)
class OpenWrtSensorDescription(SensorEntityDescription):
    """OpenWrt sensor description."""

    value_fn: Callable[[OpenWrtData], StateType | datetime]
    attrs_fn: Callable[[OpenWrtData], dict[str, Any]] | None = None
    available_fn: Callable[[OpenWrtData], bool] | None = None


@dataclass(frozen=True, kw_only=True)
class OpenWrtStorageSensorDescription(SensorEntityDescription):
    """OpenWrt storage sensor description."""

    value_fn: Callable[[StorageUsage], StateType | datetime]


class OpenWrtSensorEntity(CoordinatorEntity[OpenWrtDataCoordinator], SensorEntity):
    """Representation of an OpenWrt sensor."""

    entity_description: OpenWrtSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: OpenWrtDataCoordinator,
        entry: ConfigEntry,
        description: OpenWrtSensorDescription,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, cast(str, entry.unique_id or entry.data[CONF_HOST]))},
        )
        if hasattr(description, "entity_registry_enabled_default"):
            self._attr_entity_registry_enabled_default = (
                description.entity_registry_enabled_default
            )

    @property
    def native_value(self) -> StateType | datetime:
        """Return value."""
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return attributes."""
        if self.coordinator.data is None or not self.entity_description.attrs_fn:
            return None
        return self.entity_description.attrs_fn(self.coordinator.data)

    @property
    def available(self) -> bool:
        """Return availability."""
        if not self.coordinator.last_update_success:
            return False
        if self.entity_description.available_fn and self.coordinator.data:
            return self.entity_description.available_fn(self.coordinator.data)
        return True
