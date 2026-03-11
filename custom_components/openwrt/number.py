"""Number platform for OpenWrt integration.

Exposes configurable numeric parameters as number entities,
allowing direct dashboard control instead of requiring the options flow.
"""

from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, EntityCategory
from homeassistant.core import HomeAssistant
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
    """Set up OpenWrt number entities from a config entry."""
    coordinator: OpenWrtDataCoordinator = hass.data[DOMAIN][entry.entry_id][
        DATA_COORDINATOR
    ]

    entities: list[NumberEntity] = []

    # TX Power per wireless interface
    if coordinator.data:
        for wifi in coordinator.data.wireless_interfaces:
            if wifi.name and wifi.txpower > 0:
                entities.append(
                    OpenWrtTxPowerNumber(coordinator, entry, wifi.name, wifi.ssid)
                )

    async_add_entities(entities)


class OpenWrtTxPowerNumber(
    CoordinatorEntity[OpenWrtDataCoordinator], NumberEntity
):
    """Number entity for WiFi TX Power control."""

    _attr_has_entity_name = True
    _attr_mode = NumberMode.SLIDER
    _attr_native_min_value = 0
    _attr_native_max_value = 30
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "dBm"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False
    _attr_translation_key = "wifi_txpower_control"

    def __init__(
        self,
        coordinator: OpenWrtDataCoordinator,
        entry: ConfigEntry,
        iface_name: str,
        ssid: str,
    ) -> None:
        """Initialize the TX Power number entity."""
        super().__init__(coordinator)
        self._iface_name = iface_name
        self._entry = entry
        label = ssid or iface_name
        self._attr_name = f"{label} TX Power"
        self._attr_unique_id = f"{entry.entry_id}_txpower_{iface_name}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.data[CONF_HOST])},
        }

    @property
    def native_value(self) -> float | None:
        """Return the current TX power."""
        if self.coordinator.data:
            for wifi in self.coordinator.data.wireless_interfaces:
                if wifi.name == self._iface_name:
                    return wifi.txpower
        return None

    async def async_set_native_value(self, value: float) -> None:
        """Set the TX power via UCI."""
        client = self.hass.data[DOMAIN][self._entry.entry_id][DATA_CLIENT]
        txpower = int(value)

        # Find the radio for this interface
        radio = None
        if self.coordinator.data:
            for wifi in self.coordinator.data.wireless_interfaces:
                if wifi.name == self._iface_name:
                    radio = wifi.radio
                    break

        if radio:
            try:
                await client.execute_command(
                    f"uci set wireless.{radio}.txpower='{txpower}' && "
                    f"uci commit wireless && wifi reload"
                )
            except Exception as err:
                _LOGGER.error("Failed to set TX power for %s: %s", self._iface_name, err)
                raise

        await self.coordinator.async_request_refresh()
