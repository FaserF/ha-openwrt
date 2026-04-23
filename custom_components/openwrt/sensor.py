"""Sensor platform for OpenWrt integration.

Provides comprehensive system, network, and wireless monitoring sensors.
All entities are grouped under the router device.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_HOST,
    PERCENTAGE,
    EntityCategory,
    UnitOfInformation,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import (
    device_registry as dr,
)
from homeassistant.helpers import (
    entity_registry as er,
)
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import UNDEFINED, StateType
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api.base import OpenWrtData, StorageUsage
from .const import (
    CONF_TRACK_DEVICES,
    CONF_TRACK_WIRED,
    DATA_COORDINATOR,
    DEFAULT_TRACK_DEVICES,
    DEFAULT_TRACK_WIRED,
    DOMAIN,
)
from .coordinator import OpenWrtDataCoordinator
from .helpers import format_ap_device_id, format_ap_name


@dataclass(frozen=True, kw_only=True)
class OpenWrtSensorDescription(SensorEntityDescription):
    """Describe an OpenWrt sensor."""

    value_fn: Callable[[OpenWrtData], StateType]
    attrs_fn: Callable[[OpenWrtData], dict[str, Any]] | None = None
    available_fn: Callable[[OpenWrtData], bool] | None = None


@dataclass(frozen=True, kw_only=True)
class OpenWrtStorageSensorDescription(SensorEntityDescription):
    """Describe an OpenWrt storage sensor."""

    value_fn: Callable[[StorageUsage], StateType]


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
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, cast(str, entry.unique_id or entry.data[CONF_HOST]))},
        )

    @property
    def native_value(self) -> StateType:
        """Return the sensor value."""
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra state attributes."""
        if self.coordinator.data is None or not self.entity_description.attrs_fn:
            return None
        return self.entity_description.attrs_fn(self.coordinator.data)

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        if not super().available:
            return False
        if self.entity_description.available_fn and self.coordinator.data:
            return self.entity_description.available_fn(self.coordinator.data)
        return True


class OpenWrtWifiSensorEntity(OpenWrtSensorEntity):
    """Representation of an OpenWrt WiFi sensor."""

    def __init__(
        self,
        coordinator: OpenWrtDataCoordinator,
        entry: ConfigEntry,
        description: OpenWrtSensorDescription,
        iface_name: str,
        ssid: str,
        frequency: str = "",
        section_id: str | None = None,
    ) -> None:
        """Initialize the WiFi sensor."""
        super().__init__(coordinator, entry, description)

        name_label = format_ap_name(ssid or iface_name, frequency)

        # Use section ID as stable identifier if available
        stable_id = section_id if section_id else iface_name
        router_id = cast(str, entry.unique_id or entry.data[CONF_HOST])
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, format_ap_device_id(router_id, stable_id))},
            name=name_label,
            manufacturer="OpenWrt",
            model="Access Point",
            via_device=(DOMAIN, router_id),
        )
        self._attr_translation_placeholders = {"iface": iface_name}


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
        """Initialize the storage sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._mount_point = mount_point
        self._attr_unique_id = f"{entry.entry_id}_{description.key}_{mount_point}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, cast(str, entry.unique_id or entry.data[CONF_HOST]))},
        )
        self._attr_translation_placeholders = {"mount": mount_point}

    @property
    def native_value(self) -> StateType:
        """Return the sensor value."""
        if (
            not self.coordinator.data
            or not self.coordinator.data.system_resources.storage
        ):
            return None
        for usage in self.coordinator.data.system_resources.storage:
            if usage.mount_point == self._mount_point:
                return self.entity_description.value_fn(usage)
        return None


class OpenWrtQModemSensorEntity(OpenWrtSensorEntity):
    """Representation of an OpenWrt QModem sensor."""

    entity_description: OpenWrtSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: OpenWrtDataCoordinator,
        entry: ConfigEntry,
        description: OpenWrtSensorDescription,
    ) -> None:
        """Initialize the QModem sensor."""
        super().__init__(coordinator, entry, description)
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"

        manufacturer = coordinator.data.qmodem_info.manufacturer or "Unknown"
        revision = coordinator.data.qmodem_info.revision
        model = f"QModem {revision}" if revision else "QModem Device"

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.unique_id}_qmodem")},
            name=f"QModem ({entry.unique_id})",
            manufacturer=manufacturer,
            model=model,
            via_device=(DOMAIN, cast(str, entry.unique_id)),
        )

    @property
    def native_value(self) -> StateType:
        """Return the sensor value."""
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        if not super().available:
            return False
        return not (
            self.coordinator.data and not self.coordinator.data.qmodem_info.enabled
        )


class OpenWrtDeviceSensor(CoordinatorEntity[OpenWrtDataCoordinator], SensorEntity):
    """Representation of an OpenWrt per-device sensor (e.g. signal)."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: OpenWrtDataCoordinator,
        entry: ConfigEntry,
        mac: str,
        description: SensorEntityDescription,
        value_fn: Callable[[OpenWrtData], StateType],
        available_fn: Callable[[OpenWrtData], bool] | None = None,
        device_name: str | None = None,
    ) -> None:
        """Initialize the device sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._mac = mac.lower()
        self._value_fn = value_fn
        self._available_fn = available_fn
        self._attr_unique_id = f"{entry.entry_id}_{self._mac}_{description.key}"
        self._attr_name = (
            cast(str, description.name) if description.name is not UNDEFINED else None
        )
        self._entry = entry
        self._initial_name = device_name or mac

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information."""
        via_device = (DOMAIN, cast(str, self._entry.unique_id))
        if self.coordinator.data:
            for device in self.coordinator.data.connected_devices:
                if (
                    device.mac
                    and device.mac.lower() == self._mac
                    and device.is_wireless
                    and device.interface
                ):
                    via_device = (
                        DOMAIN,
                        format_ap_device_id(
                            cast(str, self._entry.unique_id), device.interface
                        ),
                    )
                    break

        return DeviceInfo(
            identifiers={(DOMAIN, self._mac)},
            connections={(dr.CONNECTION_NETWORK_MAC, self._mac)},
            name=self._initial_name,
            via_device=via_device,
        )

    @property
    def native_value(self) -> StateType:
        """Return the value of the sensor."""
        if self.coordinator.data is None:
            return None
        return self._value_fn(self.coordinator.data)

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        if not super().available:
            return False
        if self._available_fn and self.coordinator.data:
            return self._available_fn(self.coordinator.data)
        return True

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        if self.coordinator.data is None:
            return {}

        for device in self.coordinator.data.connected_devices:
            if device.mac and device.mac.lower() == self._mac:
                attrs: dict[str, Any] = {
                    "mac": device.mac,
                    "is_wireless": device.is_wireless,
                }
                if device.connection_type:
                    attrs["connection_type"] = device.connection_type
                if device.connection_info:
                    attrs["connection_info"] = device.connection_info
                if device.rx_bytes:
                    attrs["rx_bytes"] = device.rx_bytes
                if device.tx_bytes:
                    attrs["tx_bytes"] = device.tx_bytes
                if device.rx_rate:
                    attrs["rx_rate"] = device.rx_rate
                if device.tx_rate:
                    attrs["tx_rate"] = device.tx_rate
                if device.uptime:
                    attrs["uptime"] = device.uptime
                if device.interface:
                    attrs["interface"] = device.interface
                return attrs
        return {}


def _bytes_to_mb(value: int) -> float:
    """Convert bytes to megabytes."""
    return round(value / (1024 * 1024), 2)


def _get_system_sensors() -> tuple[OpenWrtSensorDescription, ...]:
    """Get system sensors descriptions."""
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
            name="Load (1m)",
            translation_key="load_1min",
            state_class=SensorStateClass.MEASUREMENT,
            entity_category=EntityCategory.DIAGNOSTIC,
            suggested_display_precision=2,
            value_fn=lambda data: round(data.system_resources.load_1min, 2),
        ),
        OpenWrtSensorDescription(
            key="load_5min",
            name="Load (5m)",
            translation_key="load_5min",
            state_class=SensorStateClass.MEASUREMENT,
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
            suggested_display_precision=2,
            value_fn=lambda data: round(data.system_resources.load_5min, 2),
        ),
        OpenWrtSensorDescription(
            key="load_15min",
            name="Load (15m)",
            translation_key="load_15min",
            state_class=SensorStateClass.MEASUREMENT,
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
            suggested_display_precision=2,
            value_fn=lambda data: round(data.system_resources.load_15min, 2),
        ),
        OpenWrtSensorDescription(
            key="uptime",
            name="Uptime",
            translation_key="uptime",
            device_class=SensorDeviceClass.DURATION,
            native_unit_of_measurement=UnitOfTime.MINUTES,
            state_class=SensorStateClass.TOTAL_INCREASING,
            entity_category=EntityCategory.DIAGNOSTIC,
            suggested_display_precision=1,
            value_fn=lambda data: round(data.system_resources.uptime / 60, 1),
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
            value_fn=lambda data: sum(1 for d in data.connected_devices if d.connected),
            attrs_fn=lambda data: {
                "wireless": sum(
                    1 for d in data.connected_devices if d.is_wireless and d.connected
                ),
                "wired": sum(
                    1
                    for d in data.connected_devices
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
            value_fn=lambda data: sum(
                1 for d in data.connected_devices if d.is_wireless and d.connected
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


def _get_upnp_sensors() -> tuple[OpenWrtSensorDescription, ...]:
    """Get UPnP sensor descriptions."""
    return (
        OpenWrtSensorDescription(
            key="upnp_mappings",
            name="UPnP Mappings",
            translation_key="upnp_mappings",
            icon="mdi:folder-network",
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
            value_fn=lambda data: len(data.upnp_mappings),
            attrs_fn=lambda data: {
                "mappings": [
                    {
                        "protocol": m.protocol,
                        "external_port": m.external_port,
                        "internal_ip": m.internal_ip,
                        "internal_port": m.internal_port,
                        "description": m.description,
                    }
                    for m in data.upnp_mappings
                ]
            },
        ),
    )


def _get_qmodem_sensors() -> tuple[OpenWrtSensorDescription, ...]:
    """Get QModem sensors descriptions."""
    return (
        OpenWrtSensorDescription(
            key="qmodem_manufacturer",
            name="Modem Manufacturer",
            translation_key="qmodem_manufacturer",
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
            value_fn=lambda data: data.qmodem_info.manufacturer,
        ),
        OpenWrtSensorDescription(
            key="qmodem_revision",
            name="Modem Revision",
            translation_key="qmodem_revision",
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
            value_fn=lambda data: data.qmodem_info.revision,
        ),
        OpenWrtSensorDescription(
            key="qmodem_temperature",
            name="Modem Temperature",
            translation_key="qmodem_temperature",
            device_class=SensorDeviceClass.TEMPERATURE,
            native_unit_of_measurement=UnitOfTemperature.CELSIUS,
            state_class=SensorStateClass.MEASUREMENT,
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=lambda data: data.qmodem_info.temperature,
        ),
        OpenWrtSensorDescription(
            key="qmodem_voltage",
            name="Modem Voltage",
            translation_key="qmodem_voltage",
            device_class=SensorDeviceClass.VOLTAGE,
            native_unit_of_measurement="mV",
            state_class=SensorStateClass.MEASUREMENT,
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=lambda data: data.qmodem_info.voltage,
        ),
        OpenWrtSensorDescription(
            key="qmodem_connect_status",
            name="Modem Connect Status",
            translation_key="qmodem_connect_status",
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=lambda data: data.qmodem_info.connect_status,
        ),
        OpenWrtSensorDescription(
            key="qmodem_sim_status",
            name="SIM Status",
            translation_key="qmodem_sim_status",
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=lambda data: data.qmodem_info.sim_status,
        ),
        OpenWrtSensorDescription(
            key="qmodem_isp",
            name="Internet Service Provider",
            translation_key="qmodem_isp",
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=lambda data: data.qmodem_info.isp,
        ),
        OpenWrtSensorDescription(
            key="qmodem_sim_slot",
            name="SIM Slot",
            translation_key="qmodem_sim_slot",
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=lambda data: data.qmodem_info.sim_slot,
        ),
        OpenWrtSensorDescription(
            key="qmodem_lte_rsrp",
            name="LTE RSRP",
            translation_key="qmodem_lte_rsrp",
            device_class=SensorDeviceClass.SIGNAL_STRENGTH,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement="dBm",
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=lambda data: data.qmodem_info.lte_rsrp,
        ),
        OpenWrtSensorDescription(
            key="qmodem_lte_rsrq",
            name="LTE RSRQ",
            translation_key="qmodem_lte_rsrq",
            device_class=SensorDeviceClass.SIGNAL_STRENGTH,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement="dB",
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=lambda data: data.qmodem_info.lte_rsrq,
        ),
        OpenWrtSensorDescription(
            key="qmodem_lte_rssi",
            name="LTE RSSI",
            translation_key="qmodem_lte_rssi",
            device_class=SensorDeviceClass.SIGNAL_STRENGTH,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement="dBm",
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=lambda data: data.qmodem_info.lte_rssi,
        ),
        OpenWrtSensorDescription(
            key="qmodem_lte_sinr",
            name="LTE SINR",
            translation_key="qmodem_lte_sinr",
            device_class=SensorDeviceClass.SIGNAL_STRENGTH,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement="dB",
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=lambda data: data.qmodem_info.lte_sinr,
        ),
        OpenWrtSensorDescription(
            key="qmodem_nr5g_rsrp",
            name="5G NR RSRP",
            translation_key="qmodem_nr5g_rsrp",
            device_class=SensorDeviceClass.SIGNAL_STRENGTH,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement="dBm",
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=lambda data: data.qmodem_info.nr5g_rsrp,
        ),
        OpenWrtSensorDescription(
            key="qmodem_nr5g_rsrq",
            name="5G NR RSRQ",
            translation_key="qmodem_nr5g_rsrq",
            device_class=SensorDeviceClass.SIGNAL_STRENGTH,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement="dB",
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=lambda data: data.qmodem_info.nr5g_rsrq,
        ),
        OpenWrtSensorDescription(
            key="qmodem_nr5g_sinr",
            name="5G NR SINR",
            translation_key="qmodem_nr5g_sinr",
            device_class=SensorDeviceClass.SIGNAL_STRENGTH,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement="dB",
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=lambda data: data.qmodem_info.nr5g_sinr,
        ),
    )


def _get_adblock_sensors() -> tuple[OpenWrtSensorDescription, ...]:
    """Get adblock sensor descriptions."""
    return (
        OpenWrtSensorDescription(
            key="adblock_status",
            name="AdBlock Status",
            translation_key="adblock_status",
            icon="mdi:shield-check",
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=lambda data: data.adblock.status,
        ),
        OpenWrtSensorDescription(
            key="adblock_blocked",
            name="AdBlock Blocked Domains",
            translation_key="adblock_blocked",
            icon="mdi:shield-search",
            state_class=SensorStateClass.MEASUREMENT,
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=lambda data: data.adblock.blocked_domains,
        ),
    )


def _get_simple_adblock_sensors() -> tuple[OpenWrtSensorDescription, ...]:
    """Get simple-adblock sensor descriptions."""
    return (
        OpenWrtSensorDescription(
            key="simple_adblock_blocked",
            name="Simple AdBlock Blocked Domains",
            translation_key="simple_adblock_blocked",
            icon="mdi:shield-search",
            state_class=SensorStateClass.MEASUREMENT,
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=lambda data: data.simple_adblock.blocked_domains,
        ),
    )


def _get_banip_sensors() -> tuple[OpenWrtSensorDescription, ...]:
    """Get ban-ip sensor descriptions."""
    return (
        OpenWrtSensorDescription(
            key="banip_banned",
            name="Ban-IP Banned IPs",
            translation_key="banip_banned",
            icon="mdi:ip-network-outline",
            state_class=SensorStateClass.MEASUREMENT,
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=lambda data: data.ban_ip.banned_ips,
        ),
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up OpenWrt sensors from a config entry."""
    coordinator: OpenWrtDataCoordinator = hass.data[DOMAIN][entry.entry_id][
        DATA_COORDINATOR
    ]

    entities: list[SensorEntity] = []

    if coordinator.data:
        perms = coordinator.data.permissions
        pkgs = coordinator.data.packages

        if perms.read_system:
            _async_setup_system_sensors(coordinator, entry, entities, pkgs)
            _async_setup_storage_sensors(coordinator, entry, entities)

        if perms.read_vpn and pkgs.wireguard is not False:
            _async_setup_wireguard_sensors(coordinator, entry, entities)

        if perms.read_wireless and pkgs.iwinfo is not False:
            _async_setup_wireless_sensors(coordinator, entry, entities)

        if perms.read_network:
            _async_setup_network_sensors(coordinator, entry, entities)
            if pkgs.nlbwmon:
                for device in coordinator.data.connected_devices:
                    if device.mac:
                        entities.extend(
                            _create_nlbwmon_sensors(coordinator, entry, device)
                        )

        _async_setup_specialized_sensors(coordinator, entry, entities, perms, pkgs)

    async_add_entities(entities)

    # Dynamic device tracking sensors
    tracked_macs: set[str] = set()

    @callback
    def _async_add_device_sensors() -> None:
        """Add sensors for newly discovered devices."""
        if coordinator.data is None or not coordinator.data.permissions.read_wireless:
            return

        if coordinator.data.packages.iwinfo is False:
            return

        track_devices = entry.options.get(
            CONF_TRACK_DEVICES,
            entry.data.get(CONF_TRACK_DEVICES, DEFAULT_TRACK_DEVICES),
        )
        if not track_devices:
            return

        track_wired = entry.options.get(
            CONF_TRACK_WIRED,
            entry.data.get(CONF_TRACK_WIRED, DEFAULT_TRACK_WIRED),
        )

        new_entities: list[SensorEntity] = []
        for device in coordinator.data.connected_devices:
            if not device.mac:
                continue
            mac = device.mac.lower()
            if mac in tracked_macs:
                continue

            if not track_wired and not device.is_wireless:
                continue

            tracked_macs.add(mac)
            new_entities.extend(_create_device_sensors(coordinator, entry, device))
            new_entities.extend(_create_nlbwmon_sensors(coordinator, entry, device))

        if new_entities:
            async_add_entities(new_entities)

    _async_add_device_sensors()
    entry.async_on_unload(coordinator.async_add_listener(_async_add_device_sensors))

    @callback
    def _async_cleanup_entities() -> None:
        """Clean up orphaned or old-format entities."""
        ent_reg = er.async_get(hass)
        entries = er.async_entries_for_config_entry(ent_reg, entry.entry_id)

        track_devices = entry.options.get(
            CONF_TRACK_DEVICES,
            entry.data.get(CONF_TRACK_DEVICES, DEFAULT_TRACK_DEVICES),
        )
        track_wired = entry.options.get(
            CONF_TRACK_WIRED,
            entry.data.get(CONF_TRACK_WIRED, DEFAULT_TRACK_WIRED),
        )

        for ent in entries:
            if ent.domain != "sensor":
                continue

            unique_id = ent.unique_id

            # 1. Cleanup by settings
            if "_device_" in unique_id:
                if not track_devices:
                    ent_reg.async_remove(ent.entity_id)
                    continue

                # Identify MAC from unique_id
                parts = unique_id.split("_")
                if len(parts) >= 4:
                    mac = parts[1]
                    # Pattern check for old format
                    if f"_device_{mac}_" in unique_id:
                        ent_reg.async_remove(ent.entity_id)
                        continue

                    # Wired cleanup
                    if not track_wired and mac in coordinator._device_history:
                        if not coordinator._device_history[mac].get("is_wireless"):
                            ent_reg.async_remove(ent.entity_id)
                            continue

    hass.add_job(_async_cleanup_entities)


def _create_nlbwmon_sensors(
    coordinator: OpenWrtDataCoordinator,
    entry: ConfigEntry,
    device: Any,
) -> list[OpenWrtNlbwmonSensor]:
    """Create nlbwmon sensors for a device."""
    if not coordinator.data or not coordinator.data.packages.nlbwmon:
        return []
    return [
        OpenWrtNlbwmonSensor(
            coordinator,
            entry,
            device.mac.lower(),
            device.hostname or device.mac,
        )
    ]


class OpenWrtNlbwmonSensor(CoordinatorEntity[OpenWrtDataCoordinator], SensorEntity):
    """Sensor for per-client bandwidth usage from nlbwmon."""

    _attr_has_entity_name = True
    _attr_entity_registry_enabled_default = False
    _attr_icon = "mdi:transfer"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = UnitOfInformation.MEGABYTES
    _attr_device_class = SensorDeviceClass.DATA_SIZE
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(
        self,
        coordinator: OpenWrtDataCoordinator,
        entry: ConfigEntry,
        mac: str,
        name: str,
    ) -> None:
        """Initialize the nlbwmon sensor."""
        super().__init__(coordinator)
        self._mac = mac.upper()
        self._attr_unique_id = f"{entry.entry_id}_nlbwmon_{mac.replace(':', '_')}"
        self._attr_name = f"{name} Traffic"
        self._attr_device_info = DeviceInfo(
            connections={("mac", mac)},
            via_device=(DOMAIN, cast(str, entry.unique_id)),
        )

    @property
    def native_value(self) -> float | None:
        """Return the total bandwidth usage in MB."""
        if not self.coordinator.data:
            return None
        traffic = self.coordinator.data.nlbwmon_traffic.get(self._mac)
        if not traffic:
            return None
        return round((traffic.rx_bytes + traffic.tx_bytes) / (1024 * 1024), 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        if not self.coordinator.data:
            return {}
        traffic = self.coordinator.data.nlbwmon_traffic.get(self._mac)
        if not traffic:
            return {}
        return {
            "rx_bytes": traffic.rx_bytes,
            "tx_bytes": traffic.tx_bytes,
            "rx_packets": traffic.rx_packets,
            "tx_packets": traffic.tx_packets,
        }


def _async_setup_wireguard_sensors(
    coordinator: OpenWrtDataCoordinator,
    entry: ConfigEntry,
    entities: list[SensorEntity],
) -> None:
    """Set up WireGuard sensors."""
    if not coordinator.data:
        return

    for wg in coordinator.data.wireguard_interfaces:
        # Interface level: Peer count
        entities.append(
            OpenWrtSensorEntity(
                coordinator,
                entry,
                OpenWrtSensorDescription(
                    key=f"wireguard_{wg.name}_peer_count",
                    name=f"WireGuard {wg.name} Peer Count",
                    icon="mdi:account-group",
                    entity_category=EntityCategory.DIAGNOSTIC,
                    entity_registry_enabled_default=False,
                    value_fn=lambda data, n=wg.name: next(
                        (
                            len(w.peers)
                            for w in data.wireguard_interfaces
                            if w.name == n
                        ),
                        0,
                    ),
                ),
            )
        )

        # Peer level: Data usage
        for peer in wg.peers:
            entities.append(
                OpenWrtWireGuardPeerSensor(
                    coordinator,
                    entry,
                    wg.name,
                    peer.public_key,
                )
            )


class OpenWrtWireGuardPeerSensor(
    CoordinatorEntity[OpenWrtDataCoordinator], SensorEntity
):
    """Sensor for a specific WireGuard peer."""

    _attr_has_entity_name = True
    _attr_entity_registry_enabled_default = False
    _attr_icon = "mdi:vpn"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = UnitOfInformation.MEGABYTES
    _attr_device_class = SensorDeviceClass.DATA_SIZE
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(
        self,
        coordinator: OpenWrtDataCoordinator,
        entry: ConfigEntry,
        iface_name: str,
        public_key: str,
    ) -> None:
        """Initialize the WireGuard peer sensor."""
        super().__init__(coordinator)
        self._iface_name = iface_name
        self._public_key = public_key
        self._attr_unique_id = f"{entry.entry_id}_wg_{iface_name}_{public_key}"
        self._attr_name = f"WireGuard {iface_name} Peer {public_key[:8]}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, cast(str, entry.unique_id or entry.data[CONF_HOST]))},
        )

    @property
    def native_value(self) -> float | None:
        """Return the total data transfer in MB."""
        if not self.coordinator.data:
            return None
        for wg in self.coordinator.data.wireguard_interfaces:
            if wg.name == self._iface_name:
                for peer in wg.peers:
                    if peer.public_key == self._public_key:
                        return round(
                            (peer.transfer_rx + peer.transfer_tx) / (1024 * 1024), 2
                        )
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return peer attributes."""
        if not self.coordinator.data:
            return {}
        for wg in self.coordinator.data.wireguard_interfaces:
            if wg.name == self._iface_name:
                for peer in wg.peers:
                    if peer.public_key == self._public_key:
                        return {
                            "public_key": peer.public_key,
                            "endpoint": peer.endpoint,
                            "allowed_ips": peer.allowed_ips,
                            "latest_handshake": peer.latest_handshake,
                            "rx_bytes": peer.transfer_rx,
                            "tx_bytes": peer.transfer_tx,
                            "persistent_keepalive": peer.persistent_keepalive,
                        }
        return {}


def _async_setup_system_sensors(
    coordinator: OpenWrtDataCoordinator,
    entry: ConfigEntry,
    entities: list[SensorEntity],
    pkgs: Any,
) -> None:
    """Set up system sensors."""
    for description in _get_system_sensors():
        entities.append(OpenWrtSensorEntity(coordinator, entry, description))

    # Add multi-zone temperature sensors
    if coordinator.data and coordinator.data.system_resources.temperatures:
        for zone_name in coordinator.data.system_resources.temperatures:
            if zone_name.lower() == "system":
                continue
            entities.append(OpenWrtTemperatureSensor(coordinator, entry, zone_name))

    if pkgs.adblock:
        for description in _get_adblock_sensors():
            entities.append(OpenWrtSensorEntity(coordinator, entry, description))

    if pkgs.simple_adblock:
        for description in _get_simple_adblock_sensors():
            entities.append(OpenWrtSensorEntity(coordinator, entry, description))

    if pkgs.ban_ip:
        for description in _get_banip_sensors():
            entities.append(OpenWrtSensorEntity(coordinator, entry, description))

    if pkgs.miniupnpd:
        for description in _get_upnp_sensors():
            entities.append(OpenWrtSensorEntity(coordinator, entry, description))


def _async_setup_storage_sensors(
    coordinator: OpenWrtDataCoordinator,
    entry: ConfigEntry,
    entities: list[SensorEntity],
) -> None:
    """Set up storage usage sensors."""
    if not coordinator.data or not coordinator.data.system_resources:
        return
    if not coordinator.data.system_resources.storage:
        return

    storage_descriptions = [
        OpenWrtStorageSensorDescription(
            key="storage_total",
            translation_key="mount_storage_total",
            native_unit_of_measurement=UnitOfInformation.BYTES,
            device_class=SensorDeviceClass.DATA_SIZE,
            state_class=SensorStateClass.MEASUREMENT,
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
            value_fn=lambda usage: usage.total,
        ),
        OpenWrtStorageSensorDescription(
            key="storage_used",
            translation_key="mount_storage_used",
            native_unit_of_measurement=UnitOfInformation.BYTES,
            device_class=SensorDeviceClass.DATA_SIZE,
            state_class=SensorStateClass.MEASUREMENT,
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
            value_fn=lambda usage: usage.used,
        ),
        OpenWrtStorageSensorDescription(
            key="storage_free",
            translation_key="mount_storage_free",
            native_unit_of_measurement=UnitOfInformation.BYTES,
            device_class=SensorDeviceClass.DATA_SIZE,
            state_class=SensorStateClass.MEASUREMENT,
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
            value_fn=lambda usage: usage.free,
        ),
        OpenWrtStorageSensorDescription(
            key="storage_usage",
            translation_key="mount_storage_usage",
            native_unit_of_measurement=PERCENTAGE,
            state_class=SensorStateClass.MEASUREMENT,
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
            value_fn=lambda usage: usage.percent,
        ),
    ]

    for usage in coordinator.data.system_resources.storage:
        if usage.filesystem in ("devtmpfs", "proc", "sysfs", "debugfs", "pstore"):
            continue
        if usage.filesystem == "squashfs" and usage.mount_point == "/rom":
            continue

        for description in storage_descriptions:
            entities.append(
                OpenWrtStorageSensor(coordinator, entry, description, usage.mount_point)
            )


def _async_setup_wireless_sensors(
    coordinator: OpenWrtDataCoordinator,
    entry: ConfigEntry,
    entities: list[SensorEntity],
) -> None:
    """Set up interface-specific wireless sensors."""
    if not coordinator.data:
        return
    for wifi in coordinator.data.wireless_interfaces:
        if not wifi.name:
            continue
        entities.extend(
            _create_wifi_sensors(
                coordinator,
                entry,
                wifi.name,
                wifi.ssid,
                wifi.mode,
                wifi.frequency,
                wifi.section,
            )
        )


def _async_setup_network_sensors(
    coordinator: OpenWrtDataCoordinator,
    entry: ConfigEntry,
    entities: list[SensorEntity],
) -> None:
    """Set up interface-specific network sensors."""
    if not coordinator.data:
        return

    # MWAN3 Metrics
    for mwan in coordinator.data.mwan_status:
        entities.append(
            OpenWrtMwanMetricSensor(coordinator, entry, mwan.interface_name, "latency")
        )
        entities.append(
            OpenWrtMwanMetricSensor(
                coordinator, entry, mwan.interface_name, "packet_loss"
            )
        )

    # DHCP Lease Count
    entities.append(
        OpenWrtSensorEntity(
            coordinator,
            entry,
            OpenWrtSensorDescription(
                key="dhcp_lease_count",
                name="DHCP Leases",
                translation_key="dhcp_lease_count",
                state_class=SensorStateClass.MEASUREMENT,
                entity_category=EntityCategory.DIAGNOSTIC,
                entity_registry_enabled_default=False,
                value_fn=lambda data: len(data.dhcp_leases),
            ),
        )
    )


def _async_setup_specialized_sensors(
    coordinator: OpenWrtDataCoordinator,
    entry: ConfigEntry,
    entities: list[SensorEntity],
    perms: Any,
    pkgs: Any,
) -> None:
    """Set up sensors for specialized services (VPN, MWAN, SQM, etc.)."""
    if not coordinator.data:
        return

    if perms.read_mwan and pkgs.mwan3 is not False:
        for mwan in coordinator.data.mwan_status:
            entities.extend(
                _create_mwan_sensors(coordinator, entry, mwan.interface_name)
            )

    if coordinator.data.qmodem_info.enabled:
        for description in _get_qmodem_sensors():
            entities.append(OpenWrtQModemSensorEntity(coordinator, entry, description))

    if perms.read_sqm and pkgs.sqm_scripts is not False:
        for sqm in coordinator.data.sqm:
            if sqm.section_id:
                entities.extend(
                    _create_sqm_sensors(coordinator, entry, sqm.section_id, sqm.name)
                )

    if perms.read_vpn:
        for vpn in coordinator.data.vpn_interfaces:
            if not vpn.name:
                continue
            if vpn.type == "wireguard" and pkgs.wireguard is False:
                continue
            if vpn.type == "openvpn" and pkgs.openvpn is False:
                continue
            entities.extend(_create_vpn_sensors(coordinator, entry, vpn.name, vpn.type))

    if coordinator.data.lldp_neighbors:
        for neighbor in coordinator.data.lldp_neighbors:
            if neighbor.local_interface:
                entities.extend(
                    _create_lldp_sensors(coordinator, entry, neighbor.local_interface)
                )

    # WAN Latency
    entities.append(
        OpenWrtSensorEntity(
            coordinator,
            entry,
            OpenWrtSensorDescription(
                key="wan_latency",
                name="WAN Latency",
                translation_key="wan_latency",
                native_unit_of_measurement="ms",
                state_class=SensorStateClass.MEASUREMENT,
                suggested_display_precision=1,
                entity_registry_enabled_default=False,
                value_fn=lambda data: data.latency.latency_ms,
                available_fn=lambda data: data.latency.available,
                attrs_fn=lambda data: {
                    "target": data.latency.target,
                    "packet_loss": data.latency.packet_loss,
                },
            ),
        )
    )


def _create_device_sensors(
    coordinator: OpenWrtDataCoordinator,
    entry: ConfigEntry,
    device: Any,
) -> list[OpenWrtDeviceSensor]:
    """Create sensors for a specific connected device."""
    dev_name = _get_device_display_name(coordinator, device)
    mac = device.mac.lower()

    descriptions = [
        (
            "signal",
            "Signal Strength",
            "device_signal",
            "dBm",
            lambda d, m=mac: next(
                (x.signal for x in d.connected_devices if x.mac == m), None
            ),
            lambda d, m=mac: any(
                x.mac == m and x.is_wireless for x in d.connected_devices
            ),
        ),
        (
            "rx_rate",
            "RX Rate",
            "device_rx_rate",
            "Mbps",
            lambda d, m=mac: next(
                (round(x.rx_rate / 1000, 1) for x in d.connected_devices if x.mac == m),
                None,
            ),
            lambda d, m=mac: any(
                x.mac == m and x.rx_rate > 0 for x in d.connected_devices
            ),
        ),
        (
            "tx_rate",
            "TX Rate",
            "device_tx_rate",
            "Mbps",
            lambda d, m=mac: next(
                (round(x.tx_rate / 1000, 1) for x in d.connected_devices if x.mac == m),
                None,
            ),
            lambda d, m=mac: any(
                x.mac == m and x.tx_rate > 0 for x in d.connected_devices
            ),
        ),
        (
            "noise",
            "Noise Level",
            "device_noise",
            "dBm",
            lambda d, m=mac: next(
                (x.noise for x in d.connected_devices if x.mac == m), None
            ),
            lambda d, m=mac: any(
                x.mac == m and x.is_wireless for x in d.connected_devices
            ),
        ),
    ]

    return [
        OpenWrtDeviceSensor(
            coordinator,
            entry,
            mac,
            SensorEntityDescription(
                key=f"device_{key}",
                name=name,
                translation_key=tkey,
                native_unit_of_measurement=unit,
                state_class=SensorStateClass.MEASUREMENT if unit else None,
                entity_category=EntityCategory.DIAGNOSTIC,
                entity_registry_enabled_default=False,
            ),
            v_fn,
            a_fn,
            dev_name,
        )
        for key, name, tkey, unit, v_fn, a_fn in descriptions
    ]


def _get_device_display_name(coordinator: OpenWrtDataCoordinator, device: Any) -> str:
    """Determine the display name for a device."""
    dev_name = device.mac
    if device.hostname and device.hostname != "*":
        router_hostname = ""
        if coordinator.data and coordinator.data.device_info:
            router_hostname = coordinator.data.device_info.hostname
        if device.hostname != router_hostname:
            dev_name = device.hostname
    return dev_name


def _create_wifi_sensors(
    coordinator: OpenWrtDataCoordinator,
    entry: ConfigEntry,
    iface_name: str,
    ssid: str,
    mode: str,
    frequency: str = "",
    section_id: str | None = None,
) -> list[OpenWrtWifiSensorEntity]:
    """Create sensors for a wireless interface."""
    sensors: list[OpenWrtWifiSensorEntity] = []

    # 1. Base configuration sensors
    _create_wifi_base_sensors(
        coordinator, entry, iface_name, ssid, frequency, section_id, sensors
    )

    # 2. Station-specific quality sensors (STA/Mesh/etc)
    if mode.lower() not in ("ap", "master", "access point"):
        _create_wifi_station_sensors(
            coordinator, entry, iface_name, ssid, frequency, section_id, sensors
        )

    return sensors


def _create_wifi_base_sensors(
    coordinator: OpenWrtDataCoordinator,
    entry: ConfigEntry,
    iface_name: str,
    ssid: str,
    frequency: str,
    section_id: str | None,
    sensors: list[OpenWrtWifiSensorEntity],
) -> None:
    """Create basic WiFi sensors (Clients, Channel, Power, etc.)."""
    label = ssid or iface_name

    # Clients
    sensors.append(
        OpenWrtWifiSensorEntity(
            coordinator,
            entry,
            OpenWrtSensorDescription(
                key=f"wifi_{iface_name}_clients",
                translation_key="wifi_clients",
                name=f"{label} Clients",
                state_class=SensorStateClass.MEASUREMENT,
                value_fn=lambda data, n=iface_name: sum(
                    1
                    for d in data.connected_devices
                    if d.is_wireless and d.connected and d.interface == n
                ),
            ),
            iface_name,
            ssid,
            frequency,
        )
    )

    # Generic descriptions for simple interface lookups
    desc_map = {
        "channel": ("Channel", "wifi_channel", EntityCategory.DIAGNOSTIC, True),
        "txpower": ("TX Power", "wifi_txpower", EntityCategory.DIAGNOSTIC, False),
        "htmode": ("HT Mode", "wifi_htmode", EntityCategory.DIAGNOSTIC, False),
        "hwmode": ("Hardware Mode", "wifi_hwmode", EntityCategory.DIAGNOSTIC, False),
    }

    for key, (name, tkey, cat, enabled) in desc_map.items():
        sensors.append(
            OpenWrtWifiSensorEntity(
                coordinator,
                entry,
                OpenWrtSensorDescription(
                    key=f"wifi_{iface_name}_{key}",
                    translation_key=tkey,
                    name=f"{label} {name}",
                    native_unit_of_measurement="dBm" if key == "txpower" else None,
                    entity_category=cat,
                    entity_registry_enabled_default=enabled,
                    value_fn=lambda data, n=iface_name, k=key: next(
                        (
                            getattr(w, k)
                            for w in data.wireless_interfaces
                            if w.name == n
                        ),
                        None,
                    ),
                ),
                iface_name,
                ssid,
                frequency,
                section_id,
            )
        )


def _create_wifi_station_sensors(
    coordinator: OpenWrtDataCoordinator,
    entry: ConfigEntry,
    iface_name: str,
    ssid: str,
    frequency: str,
    section_id: str | None,
    sensors: list[OpenWrtWifiSensorEntity],
) -> None:
    """Create quality sensors for WiFi station interfaces."""
    label = ssid or iface_name

    # Signal
    sensors.append(
        OpenWrtWifiSensorEntity(
            coordinator,
            entry,
            OpenWrtSensorDescription(
                key=f"wifi_{iface_name}_signal",
                translation_key="wifi_signal",
                name=f"{label} Signal",
                native_unit_of_measurement="dBm",
                device_class=SensorDeviceClass.SIGNAL_STRENGTH,
                state_class=SensorStateClass.MEASUREMENT,
                entity_category=EntityCategory.DIAGNOSTIC,
                value_fn=lambda data, n=iface_name: next(
                    (w.signal for w in data.wireless_interfaces if w.name == n),
                    None,
                ),
                available_fn=lambda data, n=iface_name: any(
                    w.name == n and w.signal != 0 for w in data.wireless_interfaces
                ),
                attrs_fn=lambda data, n=iface_name: next(
                    (
                        {
                            "noise": w.noise,
                            "encryption": w.encryption,
                            "frequency": w.frequency,
                        }
                        for w in data.wireless_interfaces
                        if w.name == n
                    ),
                    {},
                ),
            ),
            iface_name,
            ssid,
            frequency,
        )
    )

    # Simple station sensors
    sta_map = {
        "quality": (
            "Signal Quality",
            "wifi_quality",
            PERCENTAGE,
            SensorStateClass.MEASUREMENT,
        ),
        "bitrate": ("Bitrate", "wifi_bitrate", "Mbps", SensorStateClass.MEASUREMENT),
        "noise": ("Noise Level", "wifi_noise", "dBm", SensorStateClass.MEASUREMENT),
    }

    for key, (name, tkey, unit, sclass) in sta_map.items():
        sensors.append(
            OpenWrtWifiSensorEntity(
                coordinator,
                entry,
                OpenWrtSensorDescription(
                    key=f"wifi_{iface_name}_{key}",
                    translation_key=tkey,
                    name=f"{label} {name}",
                    native_unit_of_measurement=unit,
                    device_class=(
                        SensorDeviceClass.DATA_RATE
                        if key == "bitrate"
                        else (
                            SensorDeviceClass.SIGNAL_STRENGTH
                            if key == "noise"
                            else None
                        )
                    ),
                    state_class=sclass,
                    entity_category=EntityCategory.DIAGNOSTIC,
                    entity_registry_enabled_default=False,
                    value_fn=lambda data, n=iface_name, k=key: next(
                        (
                            getattr(w, k)
                            for w in data.wireless_interfaces
                            if w.name == n
                        ),
                        None,
                    ),
                ),
                iface_name,
                ssid,
                frequency,
            )
        )


def _create_sqm_sensors(
    coordinator: OpenWrtDataCoordinator,
    entry: ConfigEntry,
    section_id: str,
    name: str,
) -> list[OpenWrtSensorEntity]:
    """Create diagnostic sensors for an SQM instance."""
    sensors = []

    # SQM Interface
    sensors.append(
        OpenWrtSensorEntity(
            coordinator,
            entry,
            OpenWrtSensorDescription(
                key=f"sqm_{section_id}_interface",
                translation_key="sqm_interface",
                name=f"SQM {name} Interface",
                entity_category=EntityCategory.DIAGNOSTIC,
                entity_registry_enabled_default=False,
                value_fn=lambda data, sid=section_id: next(
                    (s.interface for s in data.sqm if s.section_id == sid),
                    None,
                ),
            ),
        ),
    )

    # SQM Qdisc
    sensors.append(
        OpenWrtSensorEntity(
            coordinator,
            entry,
            OpenWrtSensorDescription(
                key=f"sqm_{section_id}_qdisc",
                translation_key="sqm_qdisc",
                name=f"SQM {name} Qdisc",
                entity_category=EntityCategory.DIAGNOSTIC,
                entity_registry_enabled_default=False,
                value_fn=lambda data, sid=section_id: next(
                    (s.qdisc for s in data.sqm if s.section_id == sid),
                    None,
                ),
            ),
        ),
    )

    # SQM Script
    sensors.append(
        OpenWrtSensorEntity(
            coordinator,
            entry,
            OpenWrtSensorDescription(
                key=f"sqm_{section_id}_script",
                translation_key="sqm_script",
                name=f"SQM {name} Script",
                entity_category=EntityCategory.DIAGNOSTIC,
                entity_registry_enabled_default=False,
                value_fn=lambda data, sid=section_id: next(
                    (s.script for s in data.sqm if s.section_id == sid),
                    None,
                ),
            ),
        ),
    )

    return sensors


def _create_net_sensors(
    coordinator: OpenWrtDataCoordinator,
    entry: ConfigEntry,
    iface_name: str,
) -> list[OpenWrtSensorEntity]:
    """Create sensors for a network interface."""
    sensors: list[OpenWrtSensorEntity] = []

    # 1. Traffic sensors (RX/TX)
    _create_net_traffic_sensors(coordinator, entry, iface_name, sensors)

    # 2. Address sensors (IPv4/IPv6)
    _create_net_address_sensors(coordinator, entry, iface_name, sensors)

    # 3. Status sensors (Speed/Uptime)
    _create_net_status_sensors(coordinator, entry, iface_name, sensors)

    # 4. Rate sensors (RX/TX rate)
    _create_net_rate_sensors(coordinator, entry, iface_name, sensors)

    return sensors


def _create_net_traffic_sensors(
    coordinator: OpenWrtDataCoordinator,
    entry: ConfigEntry,
    iface_name: str,
    sensors: list[OpenWrtSensorEntity],
) -> None:
    """Create traffic-related sensors (RX/TX) for an interface."""
    for direction in ("rx", "tx"):
        sensors.append(
            OpenWrtSensorEntity(
                coordinator,
                entry,
                OpenWrtSensorDescription(
                    key=f"net_{iface_name}_{direction}",
                    name=f"{iface_name} {direction.upper()}",
                    translation_key=f"net_{direction}",
                    translation_placeholders={"interface": iface_name},
                    native_unit_of_measurement=UnitOfInformation.MEGABYTES,
                    device_class=SensorDeviceClass.DATA_SIZE,
                    state_class=SensorStateClass.TOTAL_INCREASING,
                    entity_category=EntityCategory.DIAGNOSTIC,
                    entity_registry_enabled_default=False,
                    value_fn=lambda data, n=iface_name, d=direction: next(
                        (
                            _bytes_to_mb(getattr(i, f"{d}_bytes"))
                            for i in data.network_interfaces
                            if i.name == n
                        ),
                        0,
                    ),
                    attrs_fn=lambda data, n=iface_name, d=direction: next(
                        (
                            {
                                "errors": getattr(i, f"{d}_errors"),
                                "dropped": getattr(i, f"{d}_dropped"),
                                "packets": getattr(i, f"{d}_packets"),
                                **(
                                    {"multicast": i.multicast}
                                    if d == "rx"
                                    else {"collisions": i.collisions}
                                ),
                            }
                            for i in data.network_interfaces
                            if i.name == n
                        ),
                        {},
                    ),
                ),
            )
        )


def _create_net_address_sensors(
    coordinator: OpenWrtDataCoordinator,
    entry: ConfigEntry,
    iface_name: str,
    sensors: list[OpenWrtSensorEntity],
) -> None:
    """Create address-related sensors (IPv4/IPv6) for an interface."""
    # IPv4
    sensors.append(
        OpenWrtSensorEntity(
            coordinator,
            entry,
            OpenWrtSensorDescription(
                key=f"net_{iface_name}_ipv4",
                name=f"{iface_name} IPv4 Address",
                translation_key="net_ipv4",
                translation_placeholders={"interface": iface_name},
                entity_category=EntityCategory.DIAGNOSTIC,
                value_fn=lambda data, n=iface_name: next(
                    (i.ipv4_address for i in data.network_interfaces if i.name == n),
                    None,
                ),
                attrs_fn=lambda data, n=iface_name: next(
                    (
                        {
                            "dns_servers": (
                                ", ".join(i.dns_servers) if i.dns_servers else "none"
                            )
                        }
                        for i in data.network_interfaces
                        if i.name == n
                    ),
                    {},
                ),
            ),
        )
    )
    # IPv6
    sensors.append(
        OpenWrtSensorEntity(
            coordinator,
            entry,
            OpenWrtSensorDescription(
                key=f"net_{iface_name}_ipv6",
                name=f"{iface_name} IPv6 Address",
                translation_key="net_ipv6",
                translation_placeholders={"interface": iface_name},
                entity_category=EntityCategory.DIAGNOSTIC,
                entity_registry_enabled_default=False,
                value_fn=lambda data, n=iface_name: next(
                    (i.ipv6_address for i in data.network_interfaces if i.name == n),
                    None,
                ),
                available_fn=lambda data, n=iface_name: any(
                    i.name == n and i.ipv6_address for i in data.network_interfaces
                ),
            ),
        )
    )


def _create_net_status_sensors(
    coordinator: OpenWrtDataCoordinator,
    entry: ConfigEntry,
    iface_name: str,
    sensors: list[OpenWrtSensorEntity],
) -> None:
    """Create status-related sensors (Speed/Uptime) for an interface."""
    # Speed
    sensors.append(
        OpenWrtSensorEntity(
            coordinator,
            entry,
            OpenWrtSensorDescription(
                key=f"net_{iface_name}_speed",
                name=f"{iface_name} Link Speed",
                translation_key="net_speed",
                translation_placeholders={"interface": iface_name},
                entity_category=EntityCategory.DIAGNOSTIC,
                entity_registry_enabled_default=False,
                value_fn=lambda data, n=iface_name: next(
                    (i.speed for i in data.network_interfaces if i.name == n),
                    None,
                ),
                attrs_fn=lambda data, n=iface_name: next(
                    (
                        {"duplex": i.duplex}
                        for i in data.network_interfaces
                        if i.name == n
                    ),
                    {},
                ),
                available_fn=lambda data, n=iface_name: any(
                    i.name == n and i.speed for i in data.network_interfaces
                ),
            ),
        )
    )
    # Uptime
    sensors.append(
        OpenWrtSensorEntity(
            coordinator,
            entry,
            OpenWrtSensorDescription(
                key=f"net_{iface_name}_uptime",
                name=f"{iface_name} Uptime",
                translation_key="net_uptime",
                translation_placeholders={"interface": iface_name},
                device_class=SensorDeviceClass.DURATION,
                native_unit_of_measurement=UnitOfTime.MINUTES,
                state_class=SensorStateClass.TOTAL_INCREASING,
                entity_category=EntityCategory.DIAGNOSTIC,
                entity_registry_enabled_default=False,
                value_fn=lambda data, n=iface_name: next(
                    (
                        round(i.uptime / 60, 1)
                        for i in data.network_interfaces
                        if i.name == n
                    ),
                    None,
                ),
                available_fn=lambda data, n=iface_name: any(
                    i.name == n and i.uptime > 0 for i in data.network_interfaces
                ),
            ),
        )
    )


def _create_net_rate_sensors(
    coordinator: OpenWrtDataCoordinator,
    entry: ConfigEntry,
    iface_name: str,
    sensors: list[OpenWrtSensorEntity],
) -> None:
    """Create traffic rate sensors (RX/TX Mbps) for an interface."""
    for direction in ("rx", "tx"):
        sensors.append(
            OpenWrtSensorEntity(
                coordinator,
                entry,
                OpenWrtSensorDescription(
                    key=f"net_{iface_name}_{direction}_rate",
                    name=f"{iface_name} {direction.upper()} Rate",
                    translation_key=f"net_{direction}_rate",
                    translation_placeholders={"interface": iface_name},
                    native_unit_of_measurement="Mbps",
                    state_class=SensorStateClass.MEASUREMENT,
                    entity_registry_enabled_default=False,
                    value_fn=lambda data, n=iface_name, d=direction: next(
                        (
                            getattr(i, f"{d}_rate")
                            for i in data.network_interfaces
                            if i.name == n
                        ),
                        0.0,
                    ),
                ),
            )
        )


def _create_vpn_sensors(
    coordinator: OpenWrtDataCoordinator,
    entry: ConfigEntry,
    iface_name: str,
    vpn_type: str,
) -> list[OpenWrtSensorEntity]:
    """Create sensors for a VPN interface."""
    label = f"VPN {iface_name}"
    sensors: list[OpenWrtSensorEntity] = []

    sensors.append(
        OpenWrtSensorEntity(
            coordinator,
            entry,
            OpenWrtSensorDescription(
                key=f"vpn_{iface_name}_rx",
                name=f"{label} RX",
                translation_key="vpn_rx",
                translation_placeholders={"interface": iface_name},
                native_unit_of_measurement=UnitOfInformation.MEGABYTES,
                device_class=SensorDeviceClass.DATA_SIZE,
                state_class=SensorStateClass.TOTAL_INCREASING,
                entity_category=EntityCategory.DIAGNOSTIC,
                entity_registry_enabled_default=False,
                value_fn=lambda data, n=iface_name: next(
                    (
                        _bytes_to_mb(v.rx_bytes)
                        for v in data.vpn_interfaces
                        if v.name == n
                    ),
                    0,
                ),
            ),
        ),
    )

    sensors.append(
        OpenWrtSensorEntity(
            coordinator,
            entry,
            OpenWrtSensorDescription(
                key=f"vpn_{iface_name}_tx",
                name=f"{label} TX",
                translation_key="vpn_tx",
                translation_placeholders={"interface": iface_name},
                native_unit_of_measurement=UnitOfInformation.MEGABYTES,
                device_class=SensorDeviceClass.DATA_SIZE,
                state_class=SensorStateClass.TOTAL_INCREASING,
                entity_category=EntityCategory.DIAGNOSTIC,
                entity_registry_enabled_default=False,
                value_fn=lambda data, n=iface_name: next(
                    (
                        _bytes_to_mb(v.tx_bytes)
                        for v in data.vpn_interfaces
                        if v.name == n
                    ),
                    0,
                ),
            ),
        ),
    )

    if vpn_type == "wireguard":
        sensors.append(
            OpenWrtSensorEntity(
                coordinator,
                entry,
                OpenWrtSensorDescription(
                    key=f"vpn_{iface_name}_peers",
                    name=f"{label} Peers",
                    translation_key="vpn_peers",
                    state_class=SensorStateClass.MEASUREMENT,
                    entity_registry_enabled_default=False,
                    value_fn=lambda data, n=iface_name: next(
                        (v.peers for v in data.vpn_interfaces if v.name == n),
                        0,
                    ),
                    attrs_fn=lambda data, n=iface_name: next(
                        (
                            {"latest_handshake": v.latest_handshake, "type": v.type}
                            for v in data.vpn_interfaces
                            if v.name == n
                        ),
                        {},
                    ),
                ),
            ),
        )

    return sensors


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
                value_fn=lambda data, n=iface_name: next(
                    (
                        m.online_ratio * 100
                        for m in data.mwan_status
                        if m.interface_name == n
                    ),
                    0,
                ),
            ),
        ),
    ]


def _create_lldp_sensors(
    coordinator: OpenWrtDataCoordinator,
    entry: ConfigEntry,
    local_interface: str,
) -> list[OpenWrtSensorEntity]:
    """Create sensors for an LLDP neighbor."""
    return [
        OpenWrtSensorEntity(
            coordinator,
            entry,
            OpenWrtSensorDescription(
                key=f"lldp_{local_interface}_neighbor",
                name=f"LLDP Neighbor on {local_interface}",
                translation_key="lldp_neighbor",
                entity_category=EntityCategory.DIAGNOSTIC,
                value_fn=lambda data, i=local_interface: next(
                    (
                        n.neighbor_name or n.neighbor_system_name or n.neighbor_chassis
                        for n in data.lldp_neighbors
                        if n.local_interface == i
                    ),
                    None,
                ),
                attrs_fn=lambda data, i=local_interface: next(
                    (
                        {
                            "local_interface": n.local_interface,
                            "neighbor_name": n.neighbor_name,
                            "neighbor_port": n.neighbor_port,
                            "neighbor_chassis": n.neighbor_chassis,
                            "neighbor_description": n.neighbor_description,
                            "neighbor_system_name": n.neighbor_system_name,
                        }
                        for n in data.lldp_neighbors
                        if n.local_interface == i
                    ),
                    {},
                ),
            ),
        ),
    ]


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
        if not self.coordinator.data:
            return None
        return self.coordinator.data.system_resources.temperatures.get(self._zone_name)


class OpenWrtMwanMetricSensor(CoordinatorEntity[OpenWrtDataCoordinator], SensorEntity):
    """MWAN3 metric sensor (latency, packet loss)."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(
        self,
        coordinator: OpenWrtDataCoordinator,
        entry: ConfigEntry,
        iface: str,
        metric: str,
    ) -> None:
        super().__init__(coordinator)
        self._iface = iface
        self._metric = metric
        self._attr_unique_id = f"{entry.entry_id}_mwan_{iface}_{metric}"
        self._attr_name = f"MWAN {iface} {metric.replace('_', ' ').title()}"
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
        if not self.coordinator.data:
            return None
        for m in self.coordinator.data.mwan_status:
            if m.interface_name == self._iface:
                return getattr(m, self._metric)
        return None
