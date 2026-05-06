"""Binary sensor platform for OpenWrt integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api.base import OpenWrtData
from .const import DATA_COORDINATOR, DOMAIN
from .coordinator import OpenWrtDataCoordinator


@dataclass(frozen=True, kw_only=True)
class OpenWrtBinarySensorDescription(BinarySensorEntityDescription):
    """Describe an OpenWrt binary sensor."""

    is_on_fn: Callable[[OpenWrtData], bool | None]
    available_fn: Callable[[OpenWrtData], bool] | None = None


BINARY_SENSORS: tuple[OpenWrtBinarySensorDescription, ...] = (
    OpenWrtBinarySensorDescription(
        key="device_connected",
        translation_key="device_connected",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        is_on_fn=lambda data: True,  # If we get data, device is connected
    ),
    OpenWrtBinarySensorDescription(
        key="reboot_required",
        translation_key="reboot_required",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        is_on_fn=lambda data: data.reboot_required,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up OpenWrt binary sensors."""
    coordinator: OpenWrtDataCoordinator = hass.data[DOMAIN][entry.entry_id][
        DATA_COORDINATOR
    ]

    tracked_keys: set[str] = set()

    def _async_add_new_entities() -> None:
        """Add new entities when devices are discovered."""
        if not coordinator.data:
            return

        entities: list[OpenWrtBinarySensorEntity] = []
        perms = coordinator.data.permissions
        pkgs = coordinator.data.packages

        # Static binary sensors
        for description in BINARY_SENSORS:
            if description.key not in tracked_keys:
                tracked_keys.add(description.key)
                entities.append(
                    OpenWrtBinarySensorEntity(coordinator, entry, description)
                )

        # Dynamic binary sensors
        _async_setup_mwan_binary_sensors(
            coordinator, entry, entities, pkgs, tracked_keys
        )

        _async_setup_interface_binary_sensors(
            coordinator, entry, entities, tracked_keys
        )

        _async_setup_vpn_binary_sensors(
            coordinator, entry, entities, pkgs, tracked_keys
        )

        _async_setup_wireguard_peer_binary_sensors(
            coordinator, entry, entities, tracked_keys
        )

        # WPS Status
        key = "wps_active"
        if key not in tracked_keys:
            tracked_keys.add(key)
            entities.append(
                OpenWrtBinarySensorEntity(
                    coordinator,
                    entry,
                    OpenWrtBinarySensorDescription(
                        key=key,
                        translation_key="wps_active",
                        icon="mdi:wifi-sync",
                        entity_category=EntityCategory.DIAGNOSTIC,
                        entity_registry_enabled_default=False,
                        is_on_fn=lambda data: data.wps_status.enabled,
                    ),
                )
            )

        if perms.read_services:
            _async_setup_service_binary_sensors(
                coordinator, entry, entities, tracked_keys
            )

        # Batman Mesh Active
        if pkgs.batman_adv or pkgs.batctl:
            key = "batman_mesh_active"
            if key not in tracked_keys:
                tracked_keys.add(key)
                entities.append(
                    OpenWrtBinarySensorEntity(
                        coordinator,
                        entry,
                        OpenWrtBinarySensorDescription(
                            key=key,
                            translation_key="batman_mesh_active",
                            icon="mdi:transit-connection-variant",
                            entity_category=EntityCategory.DIAGNOSTIC,
                            is_on_fn=lambda data: data.batman_mesh_active,
                            available_fn=lambda data: data.permissions.read_batman,
                        ),
                    )
                )

        if entities:
            async_add_entities(entities)

    # Register listener and run initial discovery
    entry.async_on_unload(coordinator.async_add_listener(_async_add_new_entities))
    _async_add_new_entities()


def _async_setup_mwan_binary_sensors(
    coordinator: OpenWrtDataCoordinator,
    entry: ConfigEntry,
    entities: list[OpenWrtBinarySensorEntity],
    pkgs: Any,
    tracked_keys: set[str],
) -> None:
    """Set up MWAN3 binary sensors."""
    if pkgs.mwan3 is False:
        return
    for mwan in coordinator.data.mwan_status:
        key = f"mwan_{mwan.interface_name}_online"
        if key not in tracked_keys:
            tracked_keys.add(key)
            entities.append(
                OpenWrtBinarySensorEntity(
                    coordinator,
                    entry,
                    OpenWrtBinarySensorDescription(
                        key=key,
                        translation_key="mwan_online",
                        translation_placeholders={"interface": mwan.interface_name},
                        device_class=BinarySensorDeviceClass.CONNECTIVITY,
                        entity_category=EntityCategory.DIAGNOSTIC,
                        entity_registry_enabled_default=False,
                        is_on_fn=lambda data, n=mwan.interface_name: any(
                            m.status == "online"
                            for m in data.mwan_status
                            if m.interface_name == n
                        ),
                    ),
                ),
            )


def _async_setup_interface_binary_sensors(
    coordinator: OpenWrtDataCoordinator,
    entry: ConfigEntry,
    entities: list[OpenWrtBinarySensorEntity],
    tracked_keys: set[str],
) -> None:
    """Set up network interface binary sensors."""
    for iface in coordinator.data.network_interfaces:
        # Include physical interfaces (eth*), bridges (br-*), and WAN
        if iface.name.startswith(("eth", "br-", "wan")):
            key = f"interface_{iface.name}_up"
            if key not in tracked_keys:
                tracked_keys.add(key)
                entities.append(
                    OpenWrtBinarySensorEntity(
                        coordinator,
                        entry,
                        OpenWrtBinarySensorDescription(
                            key=key,
                            translation_key="interface_up",
                            translation_placeholders={
                                "interface": iface.name.upper(),
                            },
                            device_class=BinarySensorDeviceClass.CONNECTIVITY,
                            is_on_fn=lambda data, n=iface.name: any(
                                i.up for i in data.network_interfaces if i.name == n
                            ),
                        ),
                    ),
                )


def _async_setup_vpn_binary_sensors(
    coordinator: OpenWrtDataCoordinator,
    entry: ConfigEntry,
    entities: list[OpenWrtBinarySensorEntity],
    pkgs: Any,
    tracked_keys: set[str],
) -> None:
    """Set up VPN binary sensors."""
    for vpn in coordinator.data.vpn_interfaces:
        if not vpn.name:
            continue
        if vpn.type == "wireguard" and pkgs.wireguard is False:
            continue
        if vpn.type == "openvpn" and pkgs.openvpn is False:
            continue
        key = f"vpn_{vpn.name}_up"
        if key not in tracked_keys:
            tracked_keys.add(key)
            entities.append(
                OpenWrtBinarySensorEntity(
                    coordinator,
                    entry,
                    OpenWrtBinarySensorDescription(
                        key=key,
                        translation_key="vpn_up",
                        translation_placeholders={"interface": vpn.name},
                        device_class=BinarySensorDeviceClass.CONNECTIVITY,
                        entity_category=EntityCategory.DIAGNOSTIC,
                        entity_registry_enabled_default=False,
                        is_on_fn=lambda data, n=vpn.name: any(
                            v.up for v in data.vpn_interfaces if v.name == n
                        ),
                    ),
                ),
            )


def _async_setup_wireguard_peer_binary_sensors(
    coordinator: OpenWrtDataCoordinator,
    entry: ConfigEntry,
    entities: list[OpenWrtBinarySensorEntity],
    tracked_keys: set[str],
) -> None:
    """Set up WireGuard peer binary sensors."""
    if not coordinator.data:
        return

    import time

    for wg in coordinator.data.wireguard_interfaces:
        for peer in wg.peers:
            key = f"wireguard_{wg.name}_peer_{peer.public_key[:8]}_active"
            if key not in tracked_keys:
                tracked_keys.add(key)
                entities.append(
                    OpenWrtBinarySensorEntity(
                        coordinator,
                        entry,
                        OpenWrtBinarySensorDescription(
                            key=key,
                            translation_key="wireguard_peer_active",
                            translation_placeholders={
                                "interface": wg.name,
                                "peer": peer.public_key[:8],
                            },
                            device_class=BinarySensorDeviceClass.CONNECTIVITY,
                            entity_category=EntityCategory.DIAGNOSTIC,
                            entity_registry_enabled_default=False,
                            is_on_fn=lambda data, i=wg.name, p=peer.public_key: any(
                                (time.time() - peer_data.latest_handshake < 180)
                                for w in data.wireguard_interfaces
                                if w.name == i
                                for peer_data in w.peers
                                if peer_data.public_key == p
                                and peer_data.latest_handshake > 0
                            ),
                        ),
                    )
                )


class OpenWrtBinarySensorEntity(
    CoordinatorEntity[OpenWrtDataCoordinator],
    BinarySensorEntity,
):
    """Representation of an OpenWrt binary sensor."""

    entity_description: OpenWrtBinarySensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: OpenWrtDataCoordinator,
        entry: ConfigEntry,
        description: OpenWrtBinarySensorDescription,
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.unique_id or entry.data[CONF_HOST])},
        }

    @property
    def is_on(self) -> bool | None:
        """Return true if the binary sensor is on."""
        if self.coordinator.data is None:
            return None
        return self.entity_description.is_on_fn(self.coordinator.data)

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        if not self.coordinator.last_update_success:
            return False
        if self.entity_description.available_fn and self.coordinator.data:
            return self.entity_description.available_fn(self.coordinator.data)
        return True


def _async_setup_service_binary_sensors(
    coordinator: OpenWrtDataCoordinator,
    entry: ConfigEntry,
    entities: list[OpenWrtBinarySensorEntity],
    tracked_keys: set[str],
) -> None:
    """Set up service status binary sensors."""
    for service in coordinator.data.services:
        if not service.name:
            continue
        key = f"service_{service.name}_running"
        if key not in tracked_keys:
            tracked_keys.add(key)
            entities.append(
                OpenWrtBinarySensorEntity(
                    coordinator,
                    entry,
                    OpenWrtBinarySensorDescription(
                        key=key,
                        name=f"Service {service.name}",
                        translation_key="service_running",
                        translation_placeholders={"service": service.name},
                        device_class=BinarySensorDeviceClass.RUNNING,
                        entity_category=EntityCategory.DIAGNOSTIC,
                        entity_registry_enabled_default=False,
                        is_on_fn=lambda data, n=service.name: any(
                            s.running for s in data.services if s.name == n
                        ),
                    ),
                )
            )
