"""Network, interface, and protocol sensors for OpenWrt."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    EntityCategory,
    UnitOfInformation,
)
from homeassistant.util import dt as dt_util

from ..api.base import NetworkInterface
from ..const import (
    CONF_ENABLE_VPN,
    CONF_MQTT_PRESENCE,
)
from ..coordinator import OpenWrtDataCoordinator
from .base import (
    OpenWrtSensorDescription,
    OpenWrtSensorEntity,
    _bytes_to_mb,
)
from .mwan import OpenWrtMwanMetricSensor

_LOGGER = logging.getLogger(__name__)


def _async_setup_network_sensors(
    coordinator: OpenWrtDataCoordinator,
    entry: ConfigEntry,
    entities: list[SensorEntity],
    pkgs: Any,
    tracked_keys: set[str],
) -> None:
    """Set up interface-specific network sensors."""
    if not coordinator.data:
        return

    # MWAN3 Metrics
    for mwan in coordinator.data.mwan_status:
        for metric in ("latency", "packet_loss"):
            key = f"mwan_{mwan.interface_name}_{metric}"
            if key not in tracked_keys:
                tracked_keys.add(key)
                entities.append(
                    OpenWrtMwanMetricSensor(
                        coordinator, entry, mwan.interface_name, metric, pkgs
                    )
                )

    # DHCP Lease Count
    key = "dhcp_lease_count"
    if key not in tracked_keys:
        tracked_keys.add(key)
        entities.append(
            OpenWrtSensorEntity(
                coordinator,
                entry,
                OpenWrtSensorDescription(
                    key=key,
                    name="DHCP Leases",
                    translation_key="dhcp_lease_count",
                    state_class=SensorStateClass.MEASUREMENT,
                    entity_category=EntityCategory.DIAGNOSTIC,
                    entity_registry_enabled_default=False,
                    value_fn=lambda data: len(data.dhcp_leases),
                ),
            )
        )

    # Create MQTT presence status sensor conditionally
    key = "mqtt_presence_status"
    if entry.options.get(CONF_MQTT_PRESENCE, False) and key not in tracked_keys:
        tracked_keys.add(key)
        entities.append(
            OpenWrtSensorEntity(
                coordinator,
                entry,
                OpenWrtSensorDescription(
                    key=key,
                    name="MQTT Presence Status",
                    translation_key="mqtt_presence_status",
                    value_fn=lambda data: data.mqtt_presence_status,
                    attrs_fn=lambda data: (
                        {"logs": data.mqtt_presence_logs}
                        if data.mqtt_presence_logs
                        else {}
                    ),
                    available_fn=lambda data: data.mqtt_presence_status is not None,
                    entity_category=EntityCategory.DIAGNOSTIC,
                    icon="mdi:home-search",
                ),
            )
        )

    # Add network interface sensors
    for iface in coordinator.data.network_interfaces:
        key = f"net_iface_{iface.name}"
        if key not in tracked_keys:
            tracked_keys.add(key)
            entities.extend(_create_net_sensors(coordinator, entry, iface))


def _create_net_sensors(
    coordinator: OpenWrtDataCoordinator,
    entry: ConfigEntry,
    iface: NetworkInterface,
) -> list[OpenWrtSensorEntity]:
    """Create sensors for a network interface."""
    sensors: list[OpenWrtSensorEntity] = []
    iface_name = iface.name

    # Traffic sensors (RX/TX)
    _create_net_traffic_sensors(coordinator, entry, iface_name, sensors)

    # Address sensors (IPv4/IPv6) - only create if interface has IP or protocol
    if iface.ipv4_address or iface.ipv6_address or iface.protocol:
        _create_net_address_sensors(coordinator, entry, iface, sensors)

    # Status sensors (Speed/Uptime)
    _create_net_status_sensors(coordinator, entry, iface_name, sensors)

    # Rate sensors (RX/TX rate)
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
    iface: NetworkInterface,
    sensors: list[OpenWrtSensorEntity],
) -> None:
    """Create address-related sensors (IPv4/IPv6) for an interface."""
    iface_name = iface.name

    # IPv4
    if iface.ipv4_address:
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
                    entity_registry_enabled_default=True,
                    value_fn=lambda data, n=iface_name: next(
                        (
                            i.ipv4_address
                            for i in data.network_interfaces
                            if i.name == n
                        ),
                        None,
                    ),
                    available_fn=lambda data, n=iface_name: any(
                        i.name == n and i.ipv4_address for i in data.network_interfaces
                    ),
                    attrs_fn=lambda data, n=iface_name: next(
                        (
                            {
                                "dns_servers": (
                                    ", ".join(i.dns_servers)
                                    if i.dns_servers
                                    else "none"
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
    if iface.ipv6_address:
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
                        (
                            i.ipv6_address
                            for i in data.network_interfaces
                            if i.name == n
                        ),
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
                device_class=SensorDeviceClass.TIMESTAMP,
                entity_category=EntityCategory.DIAGNOSTIC,
                entity_registry_enabled_default=False,
                value_fn=lambda data, n=iface_name: next(
                    (
                        (dt_util.utcnow() - timedelta(seconds=i.uptime)).replace(
                            second=0, microsecond=0
                        )
                        for i in data.network_interfaces
                        if i.name == n and i.uptime > 0
                    ),
                    None,
                ),
                available_fn=lambda data, n=iface_name: any(
                    i.name == n for i in data.network_interfaces
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
                entity_registry_enabled_default=entry.options.get(
                    CONF_ENABLE_VPN, True
                ),
                value_fn=lambda data, n=iface_name: next(
                    (
                        _bytes_to_mb(v.rx_bytes)
                        for v in data.vpn_interfaces
                        if v.name == n
                    ),
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
                translation_placeholders={"interface": iface_name},
                native_unit_of_measurement=UnitOfInformation.MEGABYTES,
                device_class=SensorDeviceClass.DATA_SIZE,
                state_class=SensorStateClass.TOTAL_INCREASING,
                entity_category=EntityCategory.DIAGNOSTIC,
                entity_registry_enabled_default=entry.options.get(
                    CONF_ENABLE_VPN, True
                ),
                value_fn=lambda data, n=iface_name: next(
                    (
                        _bytes_to_mb(v.tx_bytes)
                        for v in data.vpn_interfaces
                        if v.name == n
                    ),
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
                    entity_registry_enabled_default=entry.options.get(
                        CONF_ENABLE_VPN, True
                    ),
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
