"""Binary sensor platform for OpenWrt integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

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
        name="Connected",
        translation_key="device_connected",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        is_on_fn=lambda data: True,  # If we get data, device is connected
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

    entities: list[OpenWrtBinarySensorEntity] = []

    if coordinator.data:
        perms = coordinator.data.permissions
        pkgs = coordinator.data.packages

        for description in BINARY_SENSORS:
            entities.append(OpenWrtBinarySensorEntity(coordinator, entry, description))
        if perms.read_mwan and pkgs.mwan3 is not False:
            for mwan in coordinator.data.mwan_status:
                entities.append(
                    OpenWrtBinarySensorEntity(
                        coordinator,
                        entry,
                        OpenWrtBinarySensorDescription(
                            key=f"mwan_{mwan.interface_name}_online",
                            name=f"MWAN {mwan.interface_name} Online",
                            translation_key="mwan_online",
                            translation_placeholders={"interface": mwan.interface_name},
                            device_class=BinarySensorDeviceClass.CONNECTIVITY,
                            is_on_fn=lambda data, n=mwan.interface_name: any(
                                m.status == "online"
                                for m in data.mwan_status
                                if m.interface_name == n
                            ),
                        ),
                    )
                )

        if perms.read_network:
            for iface in coordinator.data.network_interfaces:
                if iface.name in ("wan", "wan6"):
                    entities.append(
                        OpenWrtBinarySensorEntity(
                            coordinator,
                            entry,
                            OpenWrtBinarySensorDescription(
                                key=f"interface_{iface.name}_up",
                                name=f"{iface.name.upper()} Connected",
                                translation_key="interface_up",
                                translation_placeholders={
                                    "interface": iface.name.upper()
                                },
                                device_class=BinarySensorDeviceClass.CONNECTIVITY,
                                is_on_fn=lambda data, n=iface.name: any(
                                    i.up for i in data.network_interfaces if i.name == n
                                ),
                            ),
                        )
                    )

        # VPN tunnel binary sensors
        if perms.read_vpn:
            for vpn in coordinator.data.vpn_interfaces:
                if not vpn.name:
                    continue
                if vpn.type == "wireguard" and pkgs.wireguard is False:
                    continue
                if vpn.type == "openvpn" and pkgs.openvpn is False:
                    continue
                entities.append(
                    OpenWrtBinarySensorEntity(
                        coordinator,
                        entry,
                        OpenWrtBinarySensorDescription(
                            key=f"vpn_{vpn.name}_up",
                            name=f"VPN {vpn.name} Connected",
                            translation_key="vpn_up",
                            translation_placeholders={"interface": vpn.name},
                            device_class=BinarySensorDeviceClass.CONNECTIVITY,
                            entity_category=EntityCategory.DIAGNOSTIC,
                            entity_registry_enabled_default=False,
                            is_on_fn=lambda data, n=vpn.name: any(
                                v.up for v in data.vpn_interfaces if v.name == n
                            ),
                        ),
                    )
                )

    async_add_entities(entities)


class OpenWrtBinarySensorEntity(
    CoordinatorEntity[OpenWrtDataCoordinator], BinarySensorEntity
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
        if not super().available:
            return False
        if self.entity_description.available_fn and self.coordinator.data:
            return self.entity_description.available_fn(self.coordinator.data)
        return True
