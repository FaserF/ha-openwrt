"""LED and VPN switches for OpenWrt."""

from __future__ import annotations

import logging
from typing import Any, cast

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, EntityCategory
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ..api.base import OpenWrtClient
from ..const import DOMAIN
from ..coordinator import OpenWrtDataCoordinator

_LOGGER = logging.getLogger(__name__)


class OpenWrtWireGuardSwitch(CoordinatorEntity[OpenWrtDataCoordinator], SwitchEntity):
    """Switch to enable/disable a WireGuard interface."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_device_class = SwitchDeviceClass.SWITCH
    _attr_entity_registry_enabled_default = False
    _attr_icon = "mdi:vpn"

    def __init__(
        self,
        coordinator: OpenWrtDataCoordinator,
        entry: ConfigEntry,
        client: OpenWrtClient,
        iface_name: str,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._client = client
        self._iface_name = iface_name
        self._attr_unique_id = f"{entry.entry_id}_wg_switch_{iface_name}"
        self._attr_name = f"WireGuard: {iface_name}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.unique_id or entry.data[CONF_HOST])},
        }

    @property
    def is_on(self) -> bool | None:
        """Return status."""
        if not self.coordinator.data:
            return None
        for wg in self.coordinator.data.wireguard_interfaces:
            if wg.name == self._iface_name:
                return wg.enabled
        return None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable."""
        try:
            await self._client.execute_command(f"ifup {self._iface_name}")
            if self.coordinator.data:
                for wg in self.coordinator.data.wireguard_interfaces:
                    if wg.name == self._iface_name:
                        wg.enabled = True
            self.async_write_ha_state()
        except Exception as err:
            msg = f"Failed to enable WireGuard interface {self._iface_name}: {err}"
            raise HomeAssistantError(msg) from err
        self.coordinator.hass.async_create_task(
            self.coordinator.async_request_refresh()
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable."""
        try:
            await self._client.execute_command(f"ifdown {self._iface_name}")
            if self.coordinator.data:
                for wg in self.coordinator.data.wireguard_interfaces:
                    if wg.name == self._iface_name:
                        wg.enabled = False
            self.async_write_ha_state()
        except Exception as err:
            msg = f"Failed to disable WireGuard interface {self._iface_name}: {err}"
            raise HomeAssistantError(msg) from err
        self.coordinator.hass.async_create_task(
            self.coordinator.async_request_refresh()
        )


def _add_vpn_switches(
    coordinator: OpenWrtDataCoordinator,
    entry: ConfigEntry,
    client: OpenWrtClient,
    entities: list[SwitchEntity],
    tracked_keys: set[str],
) -> None:
    """Add VPN switches."""
    if not coordinator.data:
        return

    for vpn in coordinator.data.wireguard_interfaces:
        key = f"wg_switch_{vpn.name}"
        if key not in tracked_keys:
            tracked_keys.add(key)
            entities.append(
                OpenWrtWireGuardSwitch(coordinator, entry, client, vpn.name)
            )


class OpenWrtLedSwitch(CoordinatorEntity[OpenWrtDataCoordinator], SwitchEntity):
    """Switch to enable/disable an LED."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_device_class = SwitchDeviceClass.SWITCH
    _attr_entity_registry_enabled_default = False
    _attr_icon = "mdi:led-on"

    def __init__(
        self,
        coordinator: OpenWrtDataCoordinator,
        entry: ConfigEntry,
        client: OpenWrtClient,
        name: str,
    ) -> None:
        """Initialize the switch."""
        super().__init__(coordinator)
        self._client = client
        self._name = name
        self._attr_name = f"LED {name.replace('_', ' ').replace('-', ' ').title()}"
        self._attr_unique_id = f"{entry.entry_id}_led_{name}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, cast(str, entry.unique_id or entry.data[CONF_HOST]))},
        )

    @property
    def is_on(self) -> bool:
        """Return true if LED is on."""
        if not self.coordinator.data:
            return False
        for led in self.coordinator.data.leds:
            if led.name == self._name:
                return led.active
        return False

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the LED on."""
        try:
            await self._client.set_led(self._name, True)
            if self.coordinator.data:
                for led in self.coordinator.data.leds:
                    if led.name == self._name:
                        led.active = True
            self.async_write_ha_state()
        except Exception as err:
            raise HomeAssistantError(
                f"Failed to turn on LED {self._name}: {err}"
            ) from err
        self.coordinator.hass.async_create_task(
            self.coordinator.async_request_refresh()
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the LED off."""
        try:
            await self._client.set_led(self._name, False)
            if self.coordinator.data:
                for led in self.coordinator.data.leds:
                    if led.name == self._name:
                        led.active = False
            self.async_write_ha_state()
        except Exception as err:
            raise HomeAssistantError(
                f"Failed to turn off LED {self._name}: {err}"
            ) from err
        self.hass.async_create_task(self.coordinator.async_request_refresh())


def _add_led_switches(
    coordinator: OpenWrtDataCoordinator,
    entry: ConfigEntry,
    client: OpenWrtClient,
    entities: list[SwitchEntity],
    tracked_keys: set[str],
) -> None:
    """Add LED switches."""
    if not coordinator.data:
        return

    for led in coordinator.data.leds:
        key = f"led_{led.name}"
        if key not in tracked_keys:
            tracked_keys.add(key)
            entities.append(OpenWrtLedSwitch(coordinator, entry, client, led.name))
