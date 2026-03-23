"""Light platform for OpenWrt integration.

Exposes router LEDs as light entities, allowing users to toggle and
control the brightness of physical LEDs on the router.

All LED entities are disabled by default to prevent UI clutter.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ColorMode,
    LightEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DATA_CLIENT, DATA_COORDINATOR, DOMAIN
from .coordinator import OpenWrtDataCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up OpenWrt LED lights from a config entry."""
    coordinator: OpenWrtDataCoordinator = hass.data[DOMAIN][entry.entry_id][
        DATA_COORDINATOR
    ]

    entities: list[OpenWrtLedLight] = []

    if coordinator.data:
        perms = coordinator.data.permissions
        if perms.write_led and coordinator.data.leds:
            for led in coordinator.data.leds:
                entities.append(OpenWrtLedLight(coordinator, entry, led.name))

    async_add_entities(entities)


class OpenWrtLedLight(CoordinatorEntity[OpenWrtDataCoordinator], LightEntity):
    """Representation of an OpenWrt router LED."""

    _attr_has_entity_name = True
    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
    _attr_entity_registry_enabled_default = False

    def __init__(
        self,
        coordinator: OpenWrtDataCoordinator,
        entry: ConfigEntry,
        led_name: str,
    ) -> None:
        """Initialize the LED light entity."""
        super().__init__(coordinator)
        self._led_name = led_name
        self._attr_unique_id = f"{entry.entry_id}_led_{led_name}"
        self._attr_name = f"LED {led_name}"
        self._attr_translation_key = "led"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.unique_id or entry.data[CONF_HOST])},
        }
        self._entry = entry

    @property
    def is_on(self) -> bool | None:
        """Return true if LED is on."""
        if self.coordinator.data:
            for led in self.coordinator.data.leds:
                if led.name == self._led_name:
                    return led.brightness > 0
        return None

    @property
    def brightness(self) -> int | None:
        """Return the brightness of the LED (0-255)."""
        if self.coordinator.data:
            for led in self.coordinator.data.leds:
                if led.name == self._led_name:
                    return (
                        int(led.brightness / led.max_brightness * 255)
                        if led.max_brightness > 0
                        else 0
                    )
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra attributes like the trigger mode."""
        if self.coordinator.data:
            for led in self.coordinator.data.leds:
                if led.name == self._led_name:
                    return {"trigger": led.trigger}
        return None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the LED on."""
        client = self.hass.data[DOMAIN][self._entry.entry_id][DATA_CLIENT]
        brightness = kwargs.get(ATTR_BRIGHTNESS, 255)
        if self.coordinator.data:
            for led in self.coordinator.data.leds:
                if led.name == self._led_name:
                    hw_brightness = int(brightness / 255 * led.max_brightness)
                    try:
                        await client.set_led(self._led_name, hw_brightness)
                    except Exception as err:
                        msg = f"Failed to set LED {self._led_name}: {err}"
                        raise HomeAssistantError(
                            msg,
                        ) from err
                    break
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the LED off."""
        client = self.hass.data[DOMAIN][self._entry.entry_id][DATA_CLIENT]
        try:
            await client.set_led(self._led_name, 0)
        except Exception as err:
            msg = f"Failed to turn off LED {self._led_name}: {err}"
            raise HomeAssistantError(
                msg,
            ) from err
        await self.coordinator.async_request_refresh()
