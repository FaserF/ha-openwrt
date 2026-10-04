"""Storage sensors for OpenWrt."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import cast

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_HOST,
    PERCENTAGE,
    EntityCategory,
    UnitOfInformation,
)
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ..const import DOMAIN
from ..coordinator import OpenWrtDataCoordinator
from .base import (
    OpenWrtStorageSensorDescription,
    _bytes_to_mb,
)

_LOGGER = logging.getLogger(__name__)


class OpenWrtStorageSensor(CoordinatorEntity[OpenWrtDataCoordinator], SensorEntity):
    """Sensor for a specific storage mount point."""

    entity_description: OpenWrtStorageSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: OpenWrtDataCoordinator,
        entry: ConfigEntry,
        description: OpenWrtStorageSensorDescription,
        mount_point: str,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self.entity_description = description
        self._mount_point = mount_point
        self._attr_unique_id = f"{entry.entry_id}_{description.key}_{mount_point}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, cast(str, entry.unique_id or entry.data[CONF_HOST]))},
        )
        self._attr_translation_placeholders = {"mount": mount_point}

    @property
    def native_value(self) -> StateType | datetime:
        """Return value."""
        if (
            not self.coordinator.data
            or not self.coordinator.data.system_resources.storage
        ):
            return None
        for usage in self.coordinator.data.system_resources.storage:
            if usage.mount_point == self._mount_point:
                return self.entity_description.value_fn(usage)
        return None


def _async_setup_storage_sensors(
    coordinator: OpenWrtDataCoordinator,
    entry: ConfigEntry,
    entities: list[SensorEntity],
    tracked_keys: set[str],
) -> None:
    """Set up storage sensors for each mount point."""
    if not coordinator.data or not coordinator.data.system_resources:
        return
    if not coordinator.data.system_resources.storage:
        return

    storage_descriptions = [
        OpenWrtStorageSensorDescription(
            key="storage_total",
            translation_key="mount_storage_total",
            native_unit_of_measurement=UnitOfInformation.MEGABYTES,
            device_class=SensorDeviceClass.DATA_SIZE,
            state_class=SensorStateClass.MEASUREMENT,
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
            suggested_display_precision=1,
            value_fn=lambda usage: _bytes_to_mb(usage.total),
        ),
        OpenWrtStorageSensorDescription(
            key="storage_used",
            translation_key="mount_storage_used",
            native_unit_of_measurement=UnitOfInformation.MEGABYTES,
            device_class=SensorDeviceClass.DATA_SIZE,
            state_class=SensorStateClass.MEASUREMENT,
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
            suggested_display_precision=1,
            value_fn=lambda usage: _bytes_to_mb(usage.used),
        ),
        OpenWrtStorageSensorDescription(
            key="storage_free",
            translation_key="mount_storage_free",
            native_unit_of_measurement=UnitOfInformation.MEGABYTES,
            device_class=SensorDeviceClass.DATA_SIZE,
            state_class=SensorStateClass.MEASUREMENT,
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
            suggested_display_precision=1,
            value_fn=lambda usage: _bytes_to_mb(usage.free),
        ),
        OpenWrtStorageSensorDescription(
            key="storage_usage",
            translation_key="mount_storage_usage",
            native_unit_of_measurement=PERCENTAGE,
            state_class=SensorStateClass.MEASUREMENT,
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
            suggested_display_precision=1,
            value_fn=lambda usage: usage.percent,
        ),
    ]

    for usage in coordinator.data.system_resources.storage:
        if usage.filesystem in ("devtmpfs", "proc", "sysfs", "debugfs", "pstore"):
            continue
        if usage.filesystem == "squashfs" and usage.mount_point == "/rom":
            continue

        for description in storage_descriptions:
            key = f"storage_{usage.mount_point}_{description.key}"
            if key not in tracked_keys:
                tracked_keys.add(key)
                entities.append(
                    OpenWrtStorageSensor(
                        coordinator, entry, description, usage.mount_point
                    )
                )
