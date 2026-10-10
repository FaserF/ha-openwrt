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
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api.base import OpenWrtData
from .const import (
    CONF_ENABLE_SERVICES,
    CONF_ENABLE_VPN,
    DATA_COORDINATOR,
    DOMAIN,
)
from .coordinator import OpenWrtDataCoordinator
from .sensors.tailscale import find_tailscale_peer, tailscale_entities_enabled


@dataclass(frozen=True, kw_only=True)
class OpenWrtBinarySensorDescription(BinarySensorEntityDescription):
    """OpenWrt binary sensor description."""

    is_on_fn: Callable[[OpenWrtData], bool | None]
    available_fn: Callable[[OpenWrtData], bool] | None = None
    attrs_fn: Callable[[OpenWrtData], dict[str, Any]] | None = None


BINARY_SENSORS: tuple[OpenWrtBinarySensorDescription, ...] = (
    OpenWrtBinarySensorDescription(
        key="device_connected",
        name="Connected",
        translation_key="device_connected",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        is_on_fn=lambda data: True,  # If we get data, device is connected
    ),
    OpenWrtBinarySensorDescription(
        key="reboot_required",
        name="Reboot Required",
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
    """Set up binary sensors."""
    coordinator: OpenWrtDataCoordinator = hass.data[DOMAIN][entry.entry_id][
        DATA_COORDINATOR
    ]

    tracked_keys: set[str] = set()

    def _async_add_new_entities() -> None:
        """Add new entities."""
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

        if entry.options.get(CONF_ENABLE_VPN, True):
            _async_setup_vpn_binary_sensors(
                coordinator, entry, entities, pkgs, tracked_keys
            )

        if entry.options.get(CONF_ENABLE_VPN, True):
            _async_setup_wireguard_peer_binary_sensors(
                coordinator, entry, entities, tracked_keys
            )

        _async_setup_tailscale_binary_sensors(
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
                        name="WPS Active",
                        translation_key="wps_active",
                        icon="mdi:wifi-sync",
                        entity_category=EntityCategory.DIAGNOSTIC,
                        entity_registry_enabled_default=False,
                        is_on_fn=lambda data: data.wps_status.enabled,
                    ),
                )
            )

        if perms.read_services and entry.options.get(CONF_ENABLE_SERVICES, True):
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
                            name="Batman Mesh Active",
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

    async def _async_cleanup_entities() -> None:
        """Clean up orphaned binary sensors."""
        ent_reg = er.async_get(hass)
        entries = er.async_entries_for_config_entry(ent_reg, entry.entry_id)

        for ent in entries:
            if ent.domain != "binary_sensor":
                continue
            unique_id = ent.unique_id
            if (
                unique_id.startswith(f"{entry.entry_id}_interface_")
                and coordinator.data
            ):
                # e.g. entry_id_interface_br-lan_up
                found = any(
                    f"_interface_{i.name}_up" in unique_id
                    for i in coordinator.data.network_interfaces
                )
                if not found:
                    ent_reg.async_remove(ent.entity_id)
            elif unique_id.startswith(f"{entry.entry_id}_tsvpn_peer_"):
                ts = coordinator.data.tailscale if coordinator.data else None
                # Only trust a complete peer list from a running daemon;
                # never prune during outages or while logged out.
                if ts is None or not ts.daemon_running or ts.backend_state != "running":
                    continue
                node_id = unique_id.removeprefix(
                    f"{entry.entry_id}_tsvpn_peer_"
                ).removesuffix("_online")
                if not any(p.node_id == node_id for p in ts.peers):
                    ent_reg.async_remove(ent.entity_id)

    hass.add_job(_async_cleanup_entities)

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
                        name=f"MWAN {mwan.interface_name} Online",
                        translation_key="mwan_online",
                        translation_placeholders={"interface": mwan.interface_name},
                        device_class=BinarySensorDeviceClass.CONNECTIVITY,
                        entity_category=EntityCategory.DIAGNOSTIC,
                        entity_registry_enabled_default=bool(pkgs.mwan3 is True),
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
                            name=f"{iface.name.upper()} Connected",
                            translation_key="interface_up",
                            translation_placeholders={
                                "interface": iface.name.upper(),
                            },
                            device_class=BinarySensorDeviceClass.CONNECTIVITY,
                            is_on_fn=lambda data, n=iface.name: any(
                                i.up
                                for i in data.network_interfaces
                                if i.name == n or i.device == n
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
                        name=f"VPN {vpn.name} Connected",
                        translation_key="vpn_up",
                        translation_placeholders={"interface": vpn.name},
                        device_class=BinarySensorDeviceClass.CONNECTIVITY,
                        entity_category=EntityCategory.DIAGNOSTIC,
                        entity_registry_enabled_default=entry.options.get(
                            CONF_ENABLE_VPN, True
                        ),
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
                            name=f"WireGuard {wg.name} Peer {peer.public_key[:8]} Active",
                            translation_key="wireguard_peer_active",
                            translation_placeholders={
                                "interface": wg.name,
                                "peer": peer.public_key[:8],
                            },
                            device_class=BinarySensorDeviceClass.CONNECTIVITY,
                            entity_category=EntityCategory.DIAGNOSTIC,
                            entity_registry_enabled_default=entry.options.get(
                                CONF_ENABLE_VPN, True
                            ),
                            is_on_fn=lambda data, i=wg.name, p=peer.public_key: any(
                                (time.time() - peer_data.latest_handshake < 600)
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
        """Initialize."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.unique_id or entry.data[CONF_HOST])},
        }

    @property
    def is_on(self) -> bool | None:
        """Return status."""
        if self.coordinator.data is None:
            return None
        return self.entity_description.is_on_fn(self.coordinator.data)

    @property
    def available(self) -> bool:
        """Return availability."""
        if not self.coordinator.last_update_success:
            return False
        if self.entity_description.available_fn and self.coordinator.data:
            return self.entity_description.available_fn(self.coordinator.data)
        return True

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return attributes."""
        if self.coordinator.data is None or not self.entity_description.attrs_fn:
            return None
        return self.entity_description.attrs_fn(self.coordinator.data)


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


def _tailscale_peer_attrs(data: OpenWrtData, node_id: str) -> dict[str, Any]:
    peer = find_tailscale_peer(data, node_id)
    if peer is None:
        return {}
    return {
        "hostname": peer.hostname,
        "os": peer.os,
        "ip_addresses": peer.ip_addresses,
        "connection": peer.connection,
        "relay": peer.relay,
        "last_seen": peer.last_seen.isoformat() if peer.last_seen else None,
        "exit_node": peer.exit_node,
    }


def _async_setup_tailscale_binary_sensors(
    coordinator: OpenWrtDataCoordinator,
    entry: ConfigEntry,
    entities: list[OpenWrtBinarySensorEntity],
    tracked_keys: set[str],
) -> None:
    """Set up Tailscale VPN binary sensors (router status and peers)."""
    data = coordinator.data
    if not tailscale_entities_enabled(data, entry) or data.tailscale is None:
        return

    def ts_available(d: OpenWrtData) -> bool:
        return d.tailscale is not None

    descriptions = [
        OpenWrtBinarySensorDescription(
            key="tsvpn_connected",
            name="Tailscale VPN Connected",
            translation_key="tsvpn_connected",
            device_class=BinarySensorDeviceClass.CONNECTIVITY,
            is_on_fn=lambda d: bool(
                d.tailscale
                and d.tailscale.backend_state == "running"
                and d.tailscale.self_node.online
            ),
            available_fn=ts_available,
        ),
        OpenWrtBinarySensorDescription(
            key="tsvpn_health",
            name="Tailscale VPN Problem",
            translation_key="tsvpn_health",
            device_class=BinarySensorDeviceClass.PROBLEM,
            is_on_fn=lambda d: bool(
                d.tailscale and (d.tailscale.health or d.tailscale.needs_login)
            ),
            attrs_fn=lambda d: {
                "messages": d.tailscale.health if d.tailscale else [],
                "needs_login": bool(d.tailscale and d.tailscale.needs_login),
            },
            available_fn=ts_available,
        ),
    ]

    # Peers are keyed by StableNodeID (hostnames change, node keys rotate) and
    # disabled by default to keep large tailnets out of the registry.
    for peer in data.tailscale.peers:
        descriptions.append(
            OpenWrtBinarySensorDescription(
                key=f"tsvpn_peer_{peer.node_id}_online",
                name=f"Tailscale Peer {peer.hostname or peer.node_id}",
                translation_key="tsvpn_peer_online",
                translation_placeholders={"peer": peer.hostname or peer.node_id},
                device_class=BinarySensorDeviceClass.CONNECTIVITY,
                entity_registry_enabled_default=False,
                is_on_fn=lambda d, n=peer.node_id: bool(
                    (p := find_tailscale_peer(d, n)) and p.online
                ),
                attrs_fn=lambda d, n=peer.node_id: _tailscale_peer_attrs(d, n),
                available_fn=lambda d, n=peer.node_id: (
                    find_tailscale_peer(d, n) is not None
                ),
            )
        )

    for description in descriptions:
        if description.key not in tracked_keys:
            tracked_keys.add(description.key)
            entities.append(OpenWrtBinarySensorEntity(coordinator, entry, description))
