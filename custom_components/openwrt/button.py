"""Button platform for OpenWrt integration."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from homeassistant.components.button import (
    ButtonDeviceClass,
    ButtonEntity,
    ButtonEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api.base import OpenWrtClient
from .const import DATA_CLIENT, DATA_COORDINATOR, DOMAIN
from .coordinator import OpenWrtDataCoordinator


@dataclass(frozen=True, kw_only=True)
class OpenWrtButtonDescription(ButtonEntityDescription):
    """Describe an OpenWrt button."""

    press_fn: Callable[[OpenWrtClient], Coroutine[Any, Any, Any]]


BUTTONS: tuple[OpenWrtButtonDescription, ...] = (
    OpenWrtButtonDescription(
        key="reboot",
        translation_key="reboot",
        device_class=ButtonDeviceClass.RESTART,
        entity_category=EntityCategory.CONFIG,
        press_fn=lambda client: client.reboot(),
    ),
    OpenWrtButtonDescription(
        key="wps_start",
        translation_key="wps_start",
        entity_category=EntityCategory.CONFIG,
        press_fn=lambda client: client.set_wps(True),
    ),
    OpenWrtButtonDescription(
        key="wps_cancel",
        translation_key="wps_cancel",
        entity_category=EntityCategory.CONFIG,
        press_fn=lambda client: client.set_wps(False),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up OpenWrt buttons."""
    coordinator: OpenWrtDataCoordinator = hass.data[DOMAIN][entry.entry_id][
        DATA_COORDINATOR
    ]
    client: OpenWrtClient = hass.data[DOMAIN][entry.entry_id][DATA_CLIENT]

    entities: list[OpenWrtButtonEntity] = []

    for description in BUTTONS:
        entities.append(OpenWrtButtonEntity(coordinator, entry, description, client))

    if coordinator.data:
        for service in coordinator.data.services:
            if not service.name:
                continue
            entities.append(
                OpenWrtButtonEntity(
                    coordinator,
                    entry,
                    OpenWrtButtonDescription(
                        key=f"restart_{service.name}",
                        translation_key="service_restart",
                        translation_placeholders={"service": service.name},
                        device_class=ButtonDeviceClass.RESTART,
                        entity_category=EntityCategory.CONFIG,
                        entity_registry_enabled_default=False,
                        press_fn=lambda c, n=service.name: c.manage_service(
                            n, "restart"
                        ),
                    ),
                    client,
                )
            )

        for iface in coordinator.data.network_interfaces:
            if iface.name in ("wan", "wan6"):
                entities.append(
                    OpenWrtButtonEntity(
                        coordinator,
                        entry,
                        OpenWrtButtonDescription(
                            key=f"reconnect_{iface.name}",
                            translation_key="interface_reconnect",
                            translation_placeholders={"interface": iface.name.upper()},
                            entity_category=EntityCategory.CONFIG,
                            press_fn=lambda c, n=iface.name: c.manage_interface(
                                n, "reconnect"
                            ),
                        ),
                        client,
                    )
                )

    async_add_entities(entities)


class OpenWrtButtonEntity(CoordinatorEntity[OpenWrtDataCoordinator], ButtonEntity):
    """Representation of an OpenWrt button."""

    entity_description: OpenWrtButtonDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: OpenWrtDataCoordinator,
        entry: ConfigEntry,
        description: OpenWrtButtonDescription,
        client: OpenWrtClient,
    ) -> None:
        """Initialize the button."""
        super().__init__(coordinator)
        self.entity_description = description
        self._client = client
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.data[CONF_HOST])},
        }

    async def async_press(self) -> None:
        """Handle the button press."""
        try:
            await self.entity_description.press_fn(self._client)
        except Exception as err:
            raise HomeAssistantError(
                f"Failed to execute {self.entity_description.key}: {err}"
            ) from err
        await self.coordinator.async_request_refresh()
