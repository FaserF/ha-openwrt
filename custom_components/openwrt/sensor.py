"""Sensor platform for OpenWrt integration.

Provides comprehensive system, network, and wireless monitoring sensors.
All entities are grouped under the router device.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

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
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api.base import OpenWrtData
from .const import DATA_COORDINATOR, DOMAIN
from .coordinator import OpenWrtDataCoordinator


@dataclass(frozen=True, kw_only=True)
class OpenWrtSensorDescription(SensorEntityDescription):
    """Describe an OpenWrt sensor."""

    value_fn: Callable[[OpenWrtData], StateType]
    attrs_fn: Callable[[OpenWrtData], dict[str, Any]] | None = None
    available_fn: Callable[[OpenWrtData], bool] | None = None


def _bytes_to_mb(value: int) -> float:
    """Convert bytes to megabytes."""
    return round(value / (1024 * 1024), 2)


SYSTEM_SENSORS: tuple[OpenWrtSensorDescription, ...] = (
    OpenWrtSensorDescription(
        key="public_ip",
        name="Public IP",
        translation_key="public_ip",
        icon="mdi:earth",
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
        key="firmware_version",
        name="Firmware Version",
        translation_key="firmware_version",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.device_info.firmware_version,
        attrs_fn=lambda data: {
            "distribution": data.device_info.release_distribution,
            "version": data.device_info.release_version,
            "revision": data.device_info.release_revision,
            "target": data.device_info.target,
            "architecture": data.device_info.architecture,
            "kernel": data.device_info.kernel_version,
            "is_custom_build": data.is_custom_build,
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
        value_fn=lambda data: len(data.connected_devices),
        attrs_fn=lambda data: {
            "wireless": sum(1 for d in data.connected_devices if d.is_wireless),
            "wired": sum(1 for d in data.connected_devices if not d.is_wireless),
        },
    ),
    OpenWrtSensorDescription(
        key="wireless_clients",
        name="Wireless Clients",
        translation_key="wireless_clients",
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        value_fn=lambda data: sum(1 for d in data.connected_devices if d.is_wireless),
    ),
)


QMODEM_SENSORS: tuple[OpenWrtSensorDescription, ...] = (
    OpenWrtSensorDescription(
        key="qmodem_manufacturer",
        name="Modem Manufacturer",
        translation_key="qmodem_manufacturer",
        icon="mdi:sim",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.qmodem_info.manufacturer,
    ),
    OpenWrtSensorDescription(
        key="qmodem_revision",
        name="Modem Revision",
        translation_key="qmodem_revision",
        icon="mdi:sim",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.qmodem_info.revision,
    ),
    OpenWrtSensorDescription(
        key="qmodem_temperature",
        name="Modem Temperature",
        translation_key="qmodem_temperature",
        icon="mdi:thermometer",
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
        icon="mdi:flash",
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
        icon="mdi:cellphone-wireless",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.qmodem_info.connect_status,
    ),
    OpenWrtSensorDescription(
        key="qmodem_sim_status",
        name="SIM Status",
        translation_key="qmodem_sim_status",
        icon="mdi:sim",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.qmodem_info.sim_status,
    ),
    OpenWrtSensorDescription(
        key="qmodem_isp",
        name="Internet Service Provider",
        translation_key="qmodem_isp",
        icon="mdi:network-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.qmodem_info.isp,
    ),
    OpenWrtSensorDescription(
        key="qmodem_sim_slot",
        name="SIM Slot",
        translation_key="qmodem_sim_slot",
        icon="mdi:sim",
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
        icon="mdi:signal-cellular-3",
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
        icon="mdi:signal-cellular-3",
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
        icon="mdi:signal-cellular-3",
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
        icon="mdi:signal-cellular-3",
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
        icon="mdi:signal-5g",
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
        icon="mdi:signal-5g",
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
        icon="mdi:signal-5g",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.qmodem_info.nr5g_sinr,
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

    entities: list[OpenWrtSensorEntity] = []

    for description in SYSTEM_SENSORS:
        entities.append(OpenWrtSensorEntity(coordinator, entry, description))

    if coordinator.data:
        for wifi in coordinator.data.wireless_interfaces:
            if not wifi.name:
                continue
            entities.extend(
                _create_wifi_sensors(coordinator, entry, wifi.name, wifi.ssid)
            )

        for iface in coordinator.data.network_interfaces:
            if not iface.name or iface.name == "loopback":
                continue
            entities.extend(_create_net_sensors(coordinator, entry, iface.name))

        for mwan in coordinator.data.mwan_status:
            entities.extend(
                _create_mwan_sensors(coordinator, entry, mwan.interface_name)
            )

        if coordinator.data.qmodem_info.enabled:
            for description in QMODEM_SENSORS:
                entities.append(OpenWrtQModemSensorEntity(coordinator, entry, description))

        # DHCP Lease Count sensor
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

        # Latency sensor
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

        # VPN sensors (dynamic per interface)
        for vpn in coordinator.data.vpn_interfaces:
            if not vpn.name:
                continue
            entities.extend(
                _create_vpn_sensors(coordinator, entry, vpn.name, vpn.type)
            )

    tracked_macs: set[str] = set()

    @callback
    def _async_add_device_sensors() -> None:
        """Add sensors for newly discovered devices."""
        if coordinator.data is None:
            return

        new_entities: list[OpenWrtDeviceSensor] = []
        for device in coordinator.data.connected_devices:
            if not device.mac or device.mac in tracked_macs:
                continue

            tracked_macs.add(device.mac)
            new_entities.append(
                OpenWrtDeviceSensor(
                    coordinator,
                    entry,
                    device.mac,
                    SensorEntityDescription(
                        key=f"device_{device.mac}_signal",
                        name="Signal Strength",
                        translation_key="device_signal",
                        native_unit_of_measurement="dBm",
                        state_class=SensorStateClass.MEASUREMENT,
                        entity_category=EntityCategory.DIAGNOSTIC,
                        entity_registry_enabled_default=False,
                    ),
                    lambda data, m=device.mac: next(
                        (d.signal for d in data.connected_devices if d.mac == m), None
                    ),
                    lambda data, m=device.mac: any(
                        d.mac == m and d.is_wireless for d in data.connected_devices
                    ),
                )
            )
            new_entities.append(
                OpenWrtDeviceSensor(
                    coordinator,
                    entry,
                    device.mac,
                    SensorEntityDescription(
                        key=f"device_{device.mac}_rx_rate",
                        name="RX Rate",
                        translation_key="device_rx_rate",
                        native_unit_of_measurement="Mbps",
                        state_class=SensorStateClass.MEASUREMENT,
                        entity_category=EntityCategory.DIAGNOSTIC,
                        entity_registry_enabled_default=False,
                    ),
                    lambda data, m=device.mac: next(
                        (
                            round(d.rx_rate / 1000, 1)
                            for d in data.connected_devices
                            if d.mac == m
                        ),
                        None,
                    ),
                    lambda data, m=device.mac: any(
                        d.mac == m and d.rx_rate > 0 for d in data.connected_devices
                    ),
                )
            )
            new_entities.append(
                OpenWrtDeviceSensor(
                    coordinator,
                    entry,
                    device.mac,
                    SensorEntityDescription(
                        key=f"device_{device.mac}_connection_type",
                        name="Connection Type",
                        translation_key="device_connection_type",
                        entity_category=EntityCategory.DIAGNOSTIC,
                        entity_registry_enabled_default=False,
                    ),
                    lambda data, m=device.mac: next(
                        (
                            d.connection_type
                            for d in data.connected_devices
                            if d.mac == m
                        ),
                        None,
                    ),
                )
            )

        if new_entities:
            async_add_entities(new_entities)

    _async_add_device_sensors()
    entry.async_on_unload(coordinator.async_add_listener(_async_add_device_sensors))

    async_add_entities(entities)


def _create_wifi_sensors(
    coordinator: OpenWrtDataCoordinator,
    entry: ConfigEntry,
    iface_name: str,
    ssid: str,
) -> list[OpenWrtSensorEntity]:
    """Create sensors for a wireless interface."""
    label = ssid or iface_name
    sensors = []

    sensors.append(
        OpenWrtSensorEntity(
            coordinator,
            entry,
            OpenWrtSensorDescription(
                key=f"wifi_{iface_name}_clients",
                translation_key="wifi_clients",
                name=f"{label} Clients",
                state_class=SensorStateClass.MEASUREMENT,
                value_fn=lambda data, n=iface_name: next(
                    (w.clients_count for w in data.wireless_interfaces if w.name == n),
                    0,
                ),
            ),
        )
    )

    sensors.append(
        OpenWrtSensorEntity(
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
                    (w.signal for w in data.wireless_interfaces if w.name == n), None
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
        )
    )

    sensors.append(
        OpenWrtSensorEntity(
            coordinator,
            entry,
            OpenWrtSensorDescription(
                key=f"wifi_{iface_name}_channel",
                translation_key="wifi_channel",
                name=f"{label} Channel",
                entity_category=EntityCategory.DIAGNOSTIC,
                value_fn=lambda data, n=iface_name: next(
                    (w.channel for w in data.wireless_interfaces if w.name == n), None
                ),
            ),
        )
    )

    sensors.append(
        OpenWrtSensorEntity(
            coordinator,
            entry,
            OpenWrtSensorDescription(
                key=f"wifi_{iface_name}_txpower",
                translation_key="wifi_txpower",
                name=f"{label} TX Power",
                native_unit_of_measurement="dBm",
                entity_category=EntityCategory.DIAGNOSTIC,
                entity_registry_enabled_default=False,
                value_fn=lambda data, n=iface_name: next(
                    (w.txpower for w in data.wireless_interfaces if w.name == n), None
                ),
            ),
        )
    )

    sensors.append(
        OpenWrtSensorEntity(
            coordinator,
            entry,
            OpenWrtSensorDescription(
                key=f"wifi_{iface_name}_htmode",
                translation_key="wifi_htmode",
                name=f"{label} HT Mode",
                entity_category=EntityCategory.DIAGNOSTIC,
                entity_registry_enabled_default=False,
                value_fn=lambda data, n=iface_name: next(
                    (w.htmode for w in data.wireless_interfaces if w.name == n), None
                ),
            ),
        )
    )

    return sensors


def _create_net_sensors(
    coordinator: OpenWrtDataCoordinator,
    entry: ConfigEntry,
    iface_name: str,
) -> list[OpenWrtSensorEntity]:
    """Create sensors for a network interface."""
    sensors = []

    sensors.append(
        OpenWrtSensorEntity(
            coordinator,
            entry,
            OpenWrtSensorDescription(
                key=f"net_{iface_name}_rx",
                name=f"{iface_name} RX",
                translation_key="net_rx",
                native_unit_of_measurement=UnitOfInformation.MEGABYTES,
                device_class=SensorDeviceClass.DATA_SIZE,
                state_class=SensorStateClass.TOTAL_INCREASING,
                entity_category=EntityCategory.DIAGNOSTIC,
                entity_registry_enabled_default=False,
                value_fn=lambda data, n=iface_name: next(
                    (
                        _bytes_to_mb(i.rx_bytes)
                        for i in data.network_interfaces
                        if i.name == n
                    ),
                    0,
                ),
                attrs_fn=lambda data, n=iface_name: next(
                    (
                        {
                            "errors": i.rx_errors,
                            "dropped": i.rx_dropped,
                            "multicast": i.multicast,
                            "packets": i.rx_packets,
                        }
                        for i in data.network_interfaces
                        if i.name == n
                    ),
                    {},
                ),
            ),
        )
    )

    sensors.append(
        OpenWrtSensorEntity(
            coordinator,
            entry,
            OpenWrtSensorDescription(
                key=f"net_{iface_name}_tx",
                name=f"{iface_name} TX",
                translation_key="net_tx",
                native_unit_of_measurement=UnitOfInformation.MEGABYTES,
                device_class=SensorDeviceClass.DATA_SIZE,
                state_class=SensorStateClass.TOTAL_INCREASING,
                entity_category=EntityCategory.DIAGNOSTIC,
                entity_registry_enabled_default=False,
                value_fn=lambda data, n=iface_name: next(
                    (
                        _bytes_to_mb(i.tx_bytes)
                        for i in data.network_interfaces
                        if i.name == n
                    ),
                    0,
                ),
                attrs_fn=lambda data, n=iface_name: next(
                    (
                        {
                            "errors": i.tx_errors,
                            "dropped": i.tx_dropped,
                            "collisions": i.collisions,
                            "packets": i.tx_packets,
                        }
                        for i in data.network_interfaces
                        if i.name == n
                    ),
                    {},
                ),
            ),
        )
    )

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
                        {"dns_servers": ", ".join(i.dns_servers) if i.dns_servers else "none"}
                        for i in data.network_interfaces
                        if i.name == n
                    ),
                    {},
                ),
            ),
        )
    )

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

    sensors.append(
        OpenWrtSensorEntity(
            coordinator,
            entry,
            OpenWrtSensorDescription(
                key=f"net_{iface_name}_rx_rate",
                name=f"{iface_name} RX Rate",
                translation_key="net_rx_rate",
                native_unit_of_measurement="Mbps",
                state_class=SensorStateClass.MEASUREMENT,
                entity_registry_enabled_default=False,
                value_fn=lambda data, n=iface_name: next(
                    (i.rx_rate for i in data.network_interfaces if i.name == n),
                    0.0,
                ),
            ),
        )
    )
    sensors.append(
        OpenWrtSensorEntity(
            coordinator,
            entry,
            OpenWrtSensorDescription(
                key=f"net_{iface_name}_tx_rate",
                name=f"{iface_name} TX Rate",
                translation_key="net_tx_rate",
                native_unit_of_measurement="Mbps",
                state_class=SensorStateClass.MEASUREMENT,
                entity_registry_enabled_default=False,
                value_fn=lambda data, n=iface_name: next(
                    (i.tx_rate for i in data.network_interfaces if i.name == n),
                    0.0,
                ),
            ),
        )
    )

    return sensors


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
                native_unit_of_measurement=UnitOfInformation.MEGABYTES,
                device_class=SensorDeviceClass.DATA_SIZE,
                state_class=SensorStateClass.TOTAL_INCREASING,
                entity_category=EntityCategory.DIAGNOSTIC,
                entity_registry_enabled_default=False,
                value_fn=lambda data, n=iface_name: next(
                    (_bytes_to_mb(v.rx_bytes) for v in data.vpn_interfaces if v.name == n),
                    0,
                ),
            ),
        )
    )

    sensors.append(
        OpenWrtSensorEntity(
            coordinator,
            entry,
            OpenWrtSensorDescription(
                key=f"vpn_{iface_name}_tx",
                name=f"{label} TX",
                translation_key="vpn_tx",
                native_unit_of_measurement=UnitOfInformation.MEGABYTES,
                device_class=SensorDeviceClass.DATA_SIZE,
                state_class=SensorStateClass.TOTAL_INCREASING,
                entity_category=EntityCategory.DIAGNOSTIC,
                entity_registry_enabled_default=False,
                value_fn=lambda data, n=iface_name: next(
                    (_bytes_to_mb(v.tx_bytes) for v in data.vpn_interfaces if v.name == n),
                    0,
                ),
            ),
        )
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
                        (v.peers for v in data.vpn_interfaces if v.name == n), 0,
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
            )
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
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.data[CONF_HOST])},
        }

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
    ) -> None:
        """Initialize the device sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._mac = mac
        self._value_fn = value_fn
        self._available_fn = available_fn
        self._attr_unique_id = f"{entry.entry_id}_{mac}_{description.key}"
        self._attr_device_info = {
            "connections": {("mac", mac)},
            "via_device": (DOMAIN, entry.data[CONF_HOST]),
        }

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
    def name(self) -> str | None:
        """Return the name of the entity."""
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        if self.coordinator.data is None:
            return {}

        for device in self.coordinator.data.connected_devices:
            if device.mac == self._mac:
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
                if device.uptime:
                    attrs["uptime"] = device.uptime
                if device.interface:
                    attrs["interface"] = device.interface
                return attrs

        return {}


class OpenWrtQModemSensorEntity(CoordinatorEntity[OpenWrtDataCoordinator], SensorEntity):
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
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"

        manufacturer = coordinator.data.qmodem_info.manufacturer or "Unknown"
        revision = coordinator.data.qmodem_info.revision
        model = f"QModem {revision}" if revision else "QModem Device"

        self._attr_device_info = {
            "identifiers": {(DOMAIN, f"{entry.data[CONF_HOST]}_qmodem")},
            "name": f"QModem ({entry.data[CONF_HOST]})",
            "manufacturer": manufacturer,
            "model": model,
            "via_device": (DOMAIN, entry.data[CONF_HOST]),
        }

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
        if self.coordinator.data and not self.coordinator.data.qmodem_info.enabled:
            return False
        return True
