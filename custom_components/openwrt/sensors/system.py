"""System, storage, and temperature sensors for OpenWrt."""

from __future__ import annotations

import logging
from typing import Any

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
    UnitOfTemperature,
)
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ..const import DOMAIN
from ..coordinator import OpenWrtDataCoordinator
from .base import (
    OpenWrtSensorDescription,
    OpenWrtSensorEntity,
    _bytes_to_mb,
)

_LOGGER = logging.getLogger(__name__)


class OpenWrtTemperatureSensor(CoordinatorEntity[OpenWrtDataCoordinator], SensorEntity):
    """Temperature sensor for extra thermal zones."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(
        self, coordinator: OpenWrtDataCoordinator, entry: ConfigEntry, zone_name: str
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._zone_name = zone_name
        self._attr_unique_id = (
            f"{entry.entry_id}_temp_{zone_name.lower().replace(' ', '_')}"
        )
        self._attr_name = f"Temperature {zone_name}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.unique_id or entry.data[CONF_HOST])},
        )

    @property
    def native_value(self) -> float | None:
        """Return value."""
        if not self.coordinator.data:
            return None
        return self.coordinator.data.system_resources.temperatures.get(self._zone_name)


def _get_system_sensors() -> tuple[OpenWrtSensorDescription, ...]:
    """Get system sensors."""
    return (
        OpenWrtSensorDescription(
            key="cpu_usage",
            name="CPU Usage",
            translation_key="cpu_usage",
            native_unit_of_measurement=PERCENTAGE,
            state_class=SensorStateClass.MEASUREMENT,
            entity_category=EntityCategory.DIAGNOSTIC,
            suggested_display_precision=1,
            value_fn=lambda data: data.system_resources.cpu_usage,
        ),
        OpenWrtSensorDescription(
            key="public_ip",
            name="Public IP",
            translation_key="public_ip",
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=lambda data: data.external_ip,
        ),
        OpenWrtSensorDescription(
            key="memory_usage",
            name="Memory Usage",
            translation_key="memory_usage",
            native_unit_of_measurement=PERCENTAGE,
            state_class=SensorStateClass.MEASUREMENT,
            entity_category=EntityCategory.DIAGNOSTIC,
            suggested_display_precision=1,
            value_fn=lambda data: (
                round(
                    data.system_resources.memory_used
                    / data.system_resources.memory_total
                    * 100,
                    1,
                )
                if data.system_resources.memory_total > 0
                else 0
            ),
            attrs_fn=lambda data: {
                "total_mb": _bytes_to_mb(data.system_resources.memory_total),
                "used_mb": _bytes_to_mb(data.system_resources.memory_used),
                "free_mb": _bytes_to_mb(data.system_resources.memory_free),
                "buffered_mb": _bytes_to_mb(data.system_resources.memory_buffered),
                "cached_mb": _bytes_to_mb(data.system_resources.memory_cached),
            },
        ),
        OpenWrtSensorDescription(
            key="memory_used",
            name="Memory Used",
            translation_key="memory_used",
            native_unit_of_measurement=UnitOfInformation.MEGABYTES,
            device_class=SensorDeviceClass.DATA_SIZE,
            state_class=SensorStateClass.MEASUREMENT,
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
            value_fn=lambda data: _bytes_to_mb(data.system_resources.memory_used),
        ),
        OpenWrtSensorDescription(
            key="swap_usage",
            name="Swap Usage",
            translation_key="swap_usage",
            native_unit_of_measurement=PERCENTAGE,
            state_class=SensorStateClass.MEASUREMENT,
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
            value_fn=lambda data: (
                round(
                    data.system_resources.swap_used
                    / data.system_resources.swap_total
                    * 100,
                    1,
                )
                if data.system_resources.swap_total > 0
                else 0
            ),
            available_fn=lambda data: data.system_resources.swap_total > 0,
        ),
        OpenWrtSensorDescription(
            key="load_1min",
            name="System Load (1m)",
            translation_key="load_1min",
            state_class=SensorStateClass.MEASUREMENT,
            entity_category=EntityCategory.DIAGNOSTIC,
            suggested_display_precision=2,
            value_fn=lambda data: round(data.system_resources.load_1min, 2),
        ),
        OpenWrtSensorDescription(
            key="load_5min",
            name="System Load (5m)",
            translation_key="load_5min",
            state_class=SensorStateClass.MEASUREMENT,
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
            suggested_display_precision=2,
            value_fn=lambda data: round(data.system_resources.load_5min, 2),
        ),
        OpenWrtSensorDescription(
            key="load_15min",
            name="System Load (15m)",
            translation_key="load_15min",
            state_class=SensorStateClass.MEASUREMENT,
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
            suggested_display_precision=2,
            value_fn=lambda data: round(data.system_resources.load_15min, 2),
        ),
        OpenWrtSensorDescription(
            key="conntrack_count",
            name="Connection Tracking",
            translation_key="conntrack_count",
            state_class=SensorStateClass.MEASUREMENT,
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
            icon="mdi:table-network",
            native_unit_of_measurement="connections",
            value_fn=lambda data: data.system_resources.conntrack_count,
            available_fn=lambda data: data.system_resources.conntrack_max > 0,
            attrs_fn=lambda data: {
                "max": data.system_resources.conntrack_max,
                "usage_percent": (
                    round(
                        data.system_resources.conntrack_count
                        / data.system_resources.conntrack_max
                        * 100.0,
                        1,
                    )
                    if data.system_resources.conntrack_max > 0
                    else None
                ),
            },
        ),
        OpenWrtSensorDescription(
            key="uptime",
            name="Uptime",
            translation_key="uptime",
            device_class=SensorDeviceClass.TIMESTAMP,
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=lambda data: data.boot_time,
            attrs_fn=lambda data: {
                "days": data.system_resources.uptime // 86400,
                "hours": (data.system_resources.uptime % 86400) // 3600,
                "minutes": (data.system_resources.uptime % 3600) // 60,
            },
        ),
        OpenWrtSensorDescription(
            key="temperature",
            name="Temperature",
            translation_key="temperature",
            device_class=SensorDeviceClass.TEMPERATURE,
            native_unit_of_measurement=UnitOfTemperature.CELSIUS,
            state_class=SensorStateClass.MEASUREMENT,
            entity_category=EntityCategory.DIAGNOSTIC,
            suggested_display_precision=1,
            value_fn=lambda data: data.system_resources.temperature,
            available_fn=lambda data: data.system_resources.temperature is not None,
        ),
        OpenWrtSensorDescription(
            key="storage_usage",
            name="Storage Usage",
            translation_key="storage_usage",
            native_unit_of_measurement=PERCENTAGE,
            state_class=SensorStateClass.MEASUREMENT,
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
            value_fn=lambda data: (
                round(
                    data.system_resources.filesystem_used
                    / data.system_resources.filesystem_total
                    * 100,
                    1,
                )
                if data.system_resources.filesystem_total > 0
                else 0
            ),
            available_fn=lambda data: data.system_resources.filesystem_total > 0,
            attrs_fn=lambda data: {
                "total_mb": _bytes_to_mb(data.system_resources.filesystem_total),
                "used_mb": _bytes_to_mb(data.system_resources.filesystem_used),
                "free_mb": _bytes_to_mb(data.system_resources.filesystem_free),
            },
        ),
        OpenWrtSensorDescription(
            key="filesystem_free",
            name="Filesystem Free",
            translation_key="filesystem_free",
            native_unit_of_measurement=UnitOfInformation.MEGABYTES,
            device_class=SensorDeviceClass.DATA_SIZE,
            state_class=SensorStateClass.MEASUREMENT,
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
            suggested_display_precision=1,
            value_fn=lambda data: _bytes_to_mb(data.system_resources.filesystem_free),
            available_fn=lambda data: data.system_resources.filesystem_total > 0,
        ),
        OpenWrtSensorDescription(
            key="kernel_version",
            name="Kernel Version",
            translation_key="kernel_version",
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
            value_fn=lambda data: data.device_info.kernel_version,
        ),
        OpenWrtSensorDescription(
            key="architecture",
            name="Architecture",
            translation_key="architecture",
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
            value_fn=lambda data: data.device_info.architecture,
        ),
        OpenWrtSensorDescription(
            key="connected_clients",
            name="Connected Clients",
            translation_key="connected_clients",
            state_class=SensorStateClass.MEASUREMENT,
            value_fn=lambda data: sum(
                1 for d in data.all_connected_devices if d.connected
            ),
            attrs_fn=lambda data: {
                "wireless": sum(
                    1
                    for d in data.all_connected_devices
                    if d.is_wireless and d.connected
                ),
                "wired": sum(
                    1
                    for d in data.all_connected_devices
                    if not d.is_wireless and d.connected
                ),
            },
        ),
        OpenWrtSensorDescription(
            key="wireless_clients",
            name="Wireless Clients",
            translation_key="wireless_clients",
            state_class=SensorStateClass.MEASUREMENT,
            entity_registry_enabled_default=False,
            value_fn=lambda data: (
                cnt
                if (
                    cnt := sum(
                        1
                        for d in data.all_connected_devices
                        if d.is_wireless and d.connected
                    )
                )
                > 0
                else sum(
                    w.clients_count
                    for w in data.wireless_interfaces
                    if w.clients_count is not None
                )
            ),
        ),
        OpenWrtSensorDescription(
            key="neighbor_devices",
            name="Neighbor Devices",
            translation_key="neighbor_devices",
            state_class=SensorStateClass.MEASUREMENT,
            entity_registry_enabled_default=False,
            value_fn=lambda data: len(data.ip_neighbors),
            attrs_fn=lambda data: {
                "reachable": sum(
                    1 for n in data.ip_neighbors if n.state.upper() == "REACHABLE"
                ),
                "stale": sum(
                    1 for n in data.ip_neighbors if n.state.upper() == "STALE"
                ),
            },
        ),
        OpenWrtSensorDescription(
            key="system_logs",
            name="System Logs",
            translation_key="system_logs",
            icon="mdi:script-text",
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
            value_fn=lambda data: (
                "Error"
                if any(
                    e in line.lower()
                    for e in ["err", "fail", "crit", "alert", "emerg"]
                    for line in data.system_logs
                )
                else "OK"
            ),
            attrs_fn=lambda data: {
                "logs": "\n".join(data.system_logs),
                "log_count": len(data.system_logs),
            },
        ),
        OpenWrtSensorDescription(
            key="top_processes",
            name="Top Processes",
            translation_key="top_processes",
            icon="mdi:cpu-64-bit",
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
            value_fn=lambda data: len(data.system_resources.top_processes),
            attrs_fn=lambda data: {
                "processes": [
                    {
                        "pid": p.pid,
                        "user": p.user,
                        "cpu": f"{p.cpu_usage}%",
                        "vsz": f"{p.vsz}k",
                        "command": p.command,
                    }
                    for p in data.system_resources.top_processes
                ]
            },
        ),
        OpenWrtSensorDescription(
            key="usb_devices",
            name="USB Devices",
            icon="mdi:usb",
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
            value_fn=lambda data: (
                len(data.system_resources.usb_devices) if data.system_resources else 0
            ),
            attrs_fn=lambda data: {
                "devices": [
                    {
                        "id": dev.id,
                        "vendor_id": dev.vendor_id,
                        "product_id": dev.product_id,
                        "manufacturer": dev.manufacturer,
                        "product": dev.product,
                        "speed": dev.speed,
                    }
                    for dev in (
                        data.system_resources.usb_devices
                        if data.system_resources
                        else []
                    )
                ]
            },
        ),
    )


def _async_setup_system_sensors(
    coordinator: OpenWrtDataCoordinator,
    entry: ConfigEntry,
    entities: list[SensorEntity],
    pkgs: Any,
    tracked_keys: set[str],
    enable_load: bool = True,
) -> None:
    """Set up system-wide sensors."""
    for description in _get_system_sensors():
        if not enable_load and "load_" in description.key:
            continue
        if description.key not in tracked_keys:
            _LOGGER.debug("Adding system sensor: %s", description.key)
            tracked_keys.add(description.key)
            entities.append(OpenWrtSensorEntity(coordinator, entry, description))
        else:
            _LOGGER.debug("System sensor already tracked: %s", description.key)

    if coordinator.data and coordinator.data.system_resources.temperatures:
        for zone_name in coordinator.data.system_resources.temperatures:
            if zone_name.lower() == "system":
                continue
            key = f"temperature_{zone_name}"
            if key not in tracked_keys:
                tracked_keys.add(key)
                entities.append(OpenWrtTemperatureSensor(coordinator, entry, zone_name))

    if pkgs.adblock:
        from .services import _get_adblock_sensors

        for description in _get_adblock_sensors():
            if description.key not in tracked_keys:
                tracked_keys.add(description.key)
                entities.append(OpenWrtSensorEntity(coordinator, entry, description))

    if pkgs.simple_adblock:
        from .services import _get_simple_adblock_sensors

        for description in _get_simple_adblock_sensors():
            if description.key not in tracked_keys:
                tracked_keys.add(description.key)
                entities.append(OpenWrtSensorEntity(coordinator, entry, description))

    if pkgs.ban_ip:
        from .services import _get_banip_sensors

        for description in _get_banip_sensors():
            if description.key not in tracked_keys:
                tracked_keys.add(description.key)
                entities.append(OpenWrtSensorEntity(coordinator, entry, description))

    if pkgs.miniupnpd:
        from .services import _get_upnp_sensors

        for description in _get_upnp_sensors():
            if description.key not in tracked_keys:
                tracked_keys.add(description.key)
                entities.append(OpenWrtSensorEntity(coordinator, entry, description))
