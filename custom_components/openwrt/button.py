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
from homeassistant.helpers.device_registry import DeviceInfo
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

    entities: list[ButtonEntity] = []

    if coordinator.data:
        perms = coordinator.data.permissions
        pkgs = coordinator.data.packages

        for description in BUTTONS:
            if description.key == "reboot" and not perms.write_system:
                continue
            if (
                description.key in ("wps_start", "wps_cancel")
                and not perms.write_wireless
            ):
                continue
            if description.key == "create_backup" and not perms.write_system:
                continue
            entities.append(
                OpenWrtButtonEntity(coordinator, entry, description, client)
            )
        if perms.read_services:
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
                # Determine initial device name and sanitize hostname
                dev_name = device.mac
                if device.hostname and device.hostname != "*":
                    router_hostname = ""
                    if coordinator.data.device_info:
                        router_hostname = coordinator.data.device_info.hostname

                    if device.hostname != router_hostname:
                        dev_name = device.hostname

                is_wireless = device.is_wireless
                if not is_wireless and device.interface:
                    iface_lower = device.interface.lower()
                    if (
                        "wlan" in iface_lower
                        or "ap" in iface_lower
                        or "radio" in iface_lower
                    ):
                        is_wireless = True

                if pkgs.etherwake is not False and not is_wireless:
                    entities.append(
                        OpenWrtWakeOnLanButton(
                            coordinator,
                            entry,
                            client,
                            device.mac,
                            dev_name,
                            device.interface,
                        )
                    )
                if (
                    perms.read_wireless
                    and device.is_wireless
                    and device.interface
                    and pkgs.iwinfo is not False
                ):
                    entities.append(
                        OpenWrtKickButton(
                            coordinator,
                            entry,
                            client,
                            device.mac,
                            device.interface,
                            dev_name,
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
            "identifiers": {(DOMAIN, entry.unique_id or entry.data[CONF_HOST])},
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
    _attr_translation_key = "wake_on_lan"

    def __init__(
        self,
        coordinator: OpenWrtDataCoordinator,
        entry: ConfigEntry,
        client: OpenWrtClient,
        mac: str,
        name: str,
        interface: str | None = None,
    ) -> None:
        """Initialize the button."""
        super().__init__(coordinator)
        self._client = client
        self._mac = mac
        self._interface = interface
        self._attr_unique_id = f"{entry.entry_id}_{mac}_wol"
        self._entry = entry
        self._initial_name = name

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information."""
        via_device = (DOMAIN, self._entry.unique_id)
        if self.coordinator.data:
            for device in self.coordinator.data.connected_devices:
                if device.mac == self._mac and device.is_wireless and device.interface:
                    via_device = (
                        DOMAIN,
                        f"{self._entry.unique_id}_ap_{device.interface}",
                    )
                    break

        return DeviceInfo(
            connections={("mac", self._mac)},
            name=self._initial_name,
            via_device=via_device,
        )

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
    _attr_translation_key = "kick_device"
    _attr_entity_registry_enabled_default = False

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
        self._entry = entry
        self._initial_name = hostname

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information."""
        via_device = (DOMAIN, self._entry.unique_id)
        if self.coordinator.data:
            for device in self.coordinator.data.connected_devices:
                if device.mac == self._mac and device.is_wireless and device.interface:
                    via_device = (
                        DOMAIN,
                        f"{self._entry.unique_id}_ap_{device.interface}",
                    )
                    break

        return DeviceInfo(
            connections={("mac", self._mac)},
            name=self._initial_name,
            via_device=via_device,
        )

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
