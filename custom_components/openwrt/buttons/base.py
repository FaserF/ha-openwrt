"""Button entities for OpenWrt."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any, cast

from homeassistant.components.button import (
    ButtonEntity,
    ButtonEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ..api.base import OpenWrtClient
from ..const import DOMAIN
from ..coordinator import OpenWrtDataCoordinator
from ..helpers import get_via_device_id, is_random_mac, resolve_client_name


@dataclass(frozen=True, kw_only=True)
class OpenWrtButtonDescription(ButtonEntityDescription):
    """OpenWrt button description."""

    press_fn: Callable[[OpenWrtClient], Coroutine[Any, Any, Any]]


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
        """Initialize."""
        super().__init__(coordinator)
        self.entity_description = description
        self._client = client
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, cast(str, entry.unique_id or entry.data[CONF_HOST]))},
        )

    async def async_press(self) -> None:
        """Handle press."""
        try:
            await self.entity_description.press_fn(self._client)
        except Exception as err:
            msg = f"Failed to execute {self.entity_description.key}: {err}"
            raise HomeAssistantError(msg) from err
        await self.coordinator.async_request_refresh()


class OpenWrtWakeOnLanButton(CoordinatorEntity[OpenWrtDataCoordinator], ButtonEntity):
    """Representation of an OpenWrt Wake on LAN button."""

    _attr_has_entity_name = True
    _attr_name = "Wake on LAN"
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
        """Initialize."""
        super().__init__(coordinator)
        self._client = client
        self._mac = mac.lower()
        self._interface = interface
        self._attr_unique_id = f"{entry.entry_id}_{self._mac}_wol"
        self._entry = entry
        self._initial_name = name

        if is_random_mac(self._mac):
            self._attr_entity_registry_enabled_default = False

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info."""
        return DeviceInfo(
            connections={(dr.CONNECTION_NETWORK_MAC, self._mac)},
            name=resolve_client_name(
                self.coordinator.hass, self._mac, self._initial_name
            ),
            via_device_id=get_via_device_id(
                self.coordinator.hass, self.coordinator, self._entry, self._mac
            ),
        )

    async def async_press(self) -> None:
        """Press button."""
        command = f"ether-wake {self._mac}"
        if self._interface:
            command = f"ether-wake -i {self._interface} {self._mac}"

        try:
            output = await self._client.execute_command(command)
            if output and "not found" in output.lower():
                command = command.replace("ether-wake", "etherwake")
                await self._client.execute_command(command)
        except Exception as err:
            if "not found" in str(err).lower():
                msg = (
                    "Wake on LAN command (ether-wake/etherwake) not found on router. "
                    "Please install the 'etherwake' package on OpenWrt."
                )
                raise HomeAssistantError(msg) from err
            msg = f"Failed to send WoL packet: {err}"
            raise HomeAssistantError(msg) from err


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
        """Initialize."""
        super().__init__(coordinator)
        self._client = client
        self._mac = mac.lower()
        self._interface = interface
        self._attr_unique_id = f"{entry.entry_id}_{self._mac}_kick_{interface}"
        self._entry = entry
        self._initial_name = hostname

        if is_random_mac(self._mac):
            self._attr_entity_registry_enabled_default = False

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info."""
        return DeviceInfo(
            connections={(dr.CONNECTION_NETWORK_MAC, self._mac)},
            name=resolve_client_name(
                self.coordinator.hass, self._mac, self._initial_name
            ),
            via_device_id=get_via_device_id(
                self.coordinator.hass, self.coordinator, self._entry, self._mac
            ),
        )

    async def async_press(self) -> None:
        """Disconnect device."""
        try:
            success = await self._client.kick_device(self._mac, self._interface)
            if not success:
                msg = f"Failed to disconnect {self._mac} from {self._interface}. Ensure hostapd is running."
                raise HomeAssistantError(msg)
        except Exception as err:
            msg = f"Failed to execute device kick: {err}"
            raise HomeAssistantError(msg) from err
        await self.coordinator.async_request_refresh()
