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
        name="Reboot Router",
        translation_key="reboot",
        device_class=ButtonDeviceClass.RESTART,
        entity_category=EntityCategory.CONFIG,
        press_fn=lambda client: client.reboot(),
    ),
    OpenWrtButtonDescription(
        key="wps_start",
        name="Start WPS",
        translation_key="wps_start",
        entity_category=EntityCategory.CONFIG,
        press_fn=lambda client: client.set_wps(True),
    ),
    OpenWrtButtonDescription(
        key="wps_cancel",
        name="Cancel WPS",
        translation_key="wps_cancel",
        entity_category=EntityCategory.CONFIG,
        press_fn=lambda client: client.set_wps(False),
    ),
    OpenWrtButtonDescription(
        key="create_backup",
        name="Create Backup",
        translation_key="create_backup",
        entity_category=EntityCategory.CONFIG,
        press_fn=lambda client: client.create_backup(),
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
                        name=f"Restart {service.name}",
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
                            name=f"Reconnect {iface.name.upper()}",
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

        # Add Wake on LAN and Kick buttons for each device
        for device in coordinator.data.connected_devices:
            if device.mac:
                entities.append(
                    OpenWrtWakeOnLanButton(
                        coordinator,
                        entry,
                        client,
                        device.mac,
                        device.interface,
                    )
                )
                if device.is_wireless and device.interface:
                    entities.append(
                        OpenWrtKickButton(
                            coordinator,
                            entry,
                            client,
                            device.mac,
                            device.interface,
                            device.hostname or device.mac,
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


class OpenWrtWakeOnLanButton(CoordinatorEntity[OpenWrtDataCoordinator], ButtonEntity):
    """Representation of an OpenWrt Wake on LAN button."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:desktop-classic"
    _attr_translation_key = "wake_on_lan"

    def __init__(
        self,
        coordinator: OpenWrtDataCoordinator,
        entry: ConfigEntry,
        client: OpenWrtClient,
        mac: str,
        interface: str | None = None,
    ) -> None:
        """Initialize the button."""
        super().__init__(coordinator)
        self._client = client
        self._mac = mac
        self._interface = interface
        self._attr_unique_id = f"{entry.entry_id}_{mac}_wol"
        self._attr_device_info = {
            "connections": {("mac", mac)},
            "via_device": (DOMAIN, entry.data[CONF_HOST]),
        }

    async def async_press(self) -> None:
        """Press the button."""
        # Use ether-wake with optional interface
        # We try both names as some distros use one or the other
        command = f"ether-wake {self._mac}"
        if self._interface:
            command = f"ether-wake -i {self._interface} {self._mac}"

        try:
            output = await self._client.execute_command(command)
            if output and "not found" in output.lower():
                # Try etherwake (without hyphen)
                command = command.replace("ether-wake", "etherwake")
                await self._client.execute_command(command)
        except Exception as err:
            if "not found" in str(err).lower():
                raise HomeAssistantError(
                    "Wake on LAN command (ether-wake/etherwake) not found on router. "
                    "Please install the 'etherwake' package on OpenWrt."
                ) from err
            raise HomeAssistantError(f"Failed to send WoL packet: {err}") from err


class OpenWrtKickButton(CoordinatorEntity[OpenWrtDataCoordinator], ButtonEntity):
    """Representation of an OpenWrt kick device button."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:account-cancel"
    _attr_translation_key = "kick_device"

    def __init__(
        self,
        coordinator: OpenWrtDataCoordinator,
        entry: ConfigEntry,
        client: OpenWrtClient,
        mac: str,
        interface: str,
        hostname: str,
    ) -> None:
        """Initialize the button."""
        super().__init__(coordinator)
        self._client = client
        self._mac = mac
        self._interface = interface
        self._attr_name = f"Disconnect {hostname}"
        self._attr_unique_id = f"{entry.entry_id}_{mac}_kick"
        self._attr_device_info = {
            "connections": {("mac", mac)},
            "via_device": (DOMAIN, entry.data[CONF_HOST]),
        }

    async def async_press(self) -> None:
        """Press the button to disconnect the device."""
        try:
            success = await self._client.kick_device(self._mac, self._interface)
            if not success:
                raise HomeAssistantError(
                    f"Failed to disconnect {self._mac} from {self._interface}. Ensure hostapd is running."
                )
        except Exception as err:
            raise HomeAssistantError(f"Failed to execute device kick: {err}") from err
        await self.coordinator.async_request_refresh()
