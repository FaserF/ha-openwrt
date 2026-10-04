"""WireGuard VPN sensors for OpenWrt."""

from __future__ import annotations

import logging
from typing import Any, cast

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_HOST,
    EntityCategory,
    UnitOfInformation,
)
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ..const import CONF_ENABLE_VPN, DOMAIN
from ..coordinator import OpenWrtDataCoordinator
from .base import OpenWrtSensorDescription, OpenWrtSensorEntity

_LOGGER = logging.getLogger(__name__)


def _async_setup_wireguard_sensors(
    coordinator: OpenWrtDataCoordinator,
    entry: ConfigEntry,
    entities: list[SensorEntity],
    tracked_keys: set[str],
) -> None:
    """Set up WireGuard sensors."""
    if not coordinator.data:
        return

    for wg in coordinator.data.wireguard_interfaces:
        # Interface level: Peer count
        key = f"wireguard_{wg.name}_peer_count"
        if key not in tracked_keys:
            tracked_keys.add(key)
            entities.append(
                OpenWrtSensorEntity(
                    coordinator,
                    entry,
                    OpenWrtSensorDescription(
                        key=key,
                        name=f"WireGuard {wg.name} Peer Count",
                        icon="mdi:account-group",
                        entity_category=EntityCategory.DIAGNOSTIC,
                        entity_registry_enabled_default=entry.options.get(
                            CONF_ENABLE_VPN, True
                        ),
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
            peer_key = f"wg_{wg.name}_{peer.public_key}"
            if peer_key not in tracked_keys:
                tracked_keys.add(peer_key)
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
        self._attr_entity_registry_enabled_default = entry.options.get(
            CONF_ENABLE_VPN, True
        )
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
