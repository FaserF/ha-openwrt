"""Service-specific sensors (UPnP, Adblock, BanIP, Snort, SQM, LLDP, specialized) for OpenWrt."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ..const import CONF_ENABLE_SQM, DOMAIN
from ..coordinator import OpenWrtDataCoordinator
from .base import OpenWrtSensorDescription, OpenWrtSensorEntity
from .mesh import _create_batman_neighbor_sensors, _get_batman_global_sensors
from .mwan import _create_mwan_sensors
from .network import _create_vpn_sensors
from .qmodem import OpenWrtQModemSensorEntity, _get_qmodem_sensors

_LOGGER = logging.getLogger(__name__)


def _get_upnp_sensors() -> tuple[OpenWrtSensorDescription, ...]:
    """Get UPnP sensors."""
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
            entity_registry_enabled_default=False,
            value_fn=lambda data: data.ban_ip.banned_ips,
        ),
        OpenWrtSensorDescription(
            key="banip_blocked",
            name="Ban-IP Blocked Packets",
            translation_key="banip_blocked",
            icon="mdi:shield-remove-outline",
            native_unit_of_measurement="packets",
            state_class=SensorStateClass.TOTAL_INCREASING,
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
            value_fn=lambda data: data.ban_ip.blocked_packets,
            attrs_fn=lambda data: data.ban_ip.block_stats,
        ),
    )


class OpenWrtSnortSensor(CoordinatorEntity[OpenWrtDataCoordinator], SensorEntity):
    """Sensor showing the Snort IDS alert count, with the latest alert as attributes."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:shield-bug"
    _attr_name = "Snort Alerts"
    _attr_entity_registry_enabled_default = False

    def __init__(
        self,
        coordinator: OpenWrtDataCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_snort_alerts"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.router_id)},
        )

    @property
    def available(self) -> bool:
        """Return availability."""
        if not super().available or not self.coordinator.data:
            return False
        status = self.coordinator.data.snort_status
        return bool(status and status.get("installed"))

    @property
    def native_value(self) -> int | None:
        """Return native value."""
        if not self.coordinator.data:
            return None
        status = self.coordinator.data.snort_status
        if not status:
            return None
        return status.get("alert_count", 0)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        if not self.coordinator.data:
            return {}
        status = self.coordinator.data.snort_status
        if not status:
            return {}
        attrs: dict[str, Any] = {
            "running": status.get("running", False),
            "recent_alerts": status.get("recent_alerts", []),
        }
        last = status.get("last_alert")
        if isinstance(last, dict):
            attrs.update(
                {
                    "last_alert_message": last.get("message"),
                    "last_alert_time": last.get("timestamp"),
                    "last_alert_proto": last.get("proto"),
                    "last_alert_src": last.get("src"),
                    "last_alert_dst": last.get("dst"),
                    "last_alert_sid": last.get("sid"),
                    "last_alert_action": last.get("action"),
                }
            )
        return attrs


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


def _async_setup_specialized_sensors(
    coordinator: OpenWrtDataCoordinator,
    entry: ConfigEntry,
    entities: list[SensorEntity],
    perms: Any,
    pkgs: Any,
    tracked_keys: set[str],
) -> None:
    """Set up sensors for specialized services (VPN, MWAN, SQM, etc.)."""
    if not coordinator.data:
        return

    if perms.read_mwan and pkgs.mwan3 is not False:
        for mwan in coordinator.data.mwan_status:
            key = f"mwan_{mwan.interface_name}_main"
            if key not in tracked_keys:
                tracked_keys.add(key)
                entities.extend(
                    _create_mwan_sensors(coordinator, entry, mwan.interface_name)
                )

    if coordinator.data.qmodem_info.enabled:
        # QModem sensors don't have a stable key pattern in this helper, but they are relatively static
        key = "qmodem_info"
        if key not in tracked_keys:
            tracked_keys.add(key)
            for description in _get_qmodem_sensors():
                entities.append(
                    OpenWrtQModemSensorEntity(coordinator, entry, description)
                )

    if (
        perms.read_sqm
        and pkgs.sqm_scripts is not False
        and entry.options.get(CONF_ENABLE_SQM, True)
    ):
        for sqm in coordinator.data.sqm:
            if sqm.section_id:
                key = f"sqm_{sqm.section_id}"
                if key not in tracked_keys:
                    tracked_keys.add(key)
                    entities.extend(
                        _create_sqm_sensors(
                            coordinator, entry, sqm.section_id, sqm.name
                        )
                    )

    if perms.read_vpn:
        for vpn in coordinator.data.vpn_interfaces:
            if not vpn.name:
                continue
            if vpn.type == "wireguard" and pkgs.wireguard is False:
                continue
            if vpn.type == "openvpn" and pkgs.openvpn is False:
                continue
            key = f"vpn_{vpn.name}_traffic"
            if key not in tracked_keys:
                tracked_keys.add(key)
                entities.extend(
                    _create_vpn_sensors(coordinator, entry, vpn.name, vpn.type)
                )

    if coordinator.data.lldp_neighbors:
        for neighbor in coordinator.data.lldp_neighbors:
            if neighbor.local_interface:
                key = f"lldp_{neighbor.local_interface}_{neighbor.neighbor_chassis}"
                if key not in tracked_keys:
                    tracked_keys.add(key)
                    entities.extend(
                        _create_lldp_sensors(
                            coordinator, entry, neighbor.local_interface
                        )
                    )

    # WAN Latency
    key = "wan_latency"
    if key not in tracked_keys:
        tracked_keys.add(key)
        entities.append(
            OpenWrtSensorEntity(
                coordinator,
                entry,
                OpenWrtSensorDescription(
                    key=key,
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

    # Batman Mesh
    if perms.read_batman and (pkgs.batman_adv or pkgs.batctl):
        key = "batman_mesh_global"
        if key not in tracked_keys:
            tracked_keys.add(key)
            entities.extend(_get_batman_global_sensors(coordinator, entry))

        for mesh_neighbor in coordinator.data.batman_neighbors:
            key = f"batman_neighbor_{mesh_neighbor.mac}"
            if key not in tracked_keys:
                tracked_keys.add(key)
                entities.extend(
                    _create_batman_neighbor_sensors(
                        coordinator, entry, mesh_neighbor.mac
                    )
                )
