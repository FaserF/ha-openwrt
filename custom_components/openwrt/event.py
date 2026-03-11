"""Event platform for OpenWrt integration.

Fires events when new devices connect to the network or existing
devices disconnect. Uses the HA event entity model (2023.8+).
"""

from __future__ import annotations

import logging

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DATA_COORDINATOR, DOMAIN
from .coordinator import OpenWrtDataCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up OpenWrt event entities from a config entry."""
    coordinator: OpenWrtDataCoordinator = hass.data[DOMAIN][entry.entry_id][
        DATA_COORDINATOR
    ]

    async_add_entities([OpenWrtNewDeviceEvent(coordinator, entry)])


class OpenWrtNewDeviceEvent(
    CoordinatorEntity[OpenWrtDataCoordinator], EventEntity
):
    """Event entity that fires when a new device connects to the network."""

    _attr_has_entity_name = True
    _attr_name = "New Device"
    _attr_translation_key = "new_device"
    _attr_device_class = EventDeviceClass.BUTTON
    _attr_event_types = ["new_device_connected", "device_connected", "device_disconnected"]

    def __init__(
        self,
        coordinator: OpenWrtDataCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the event entity."""
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_new_device_event"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.data[CONF_HOST])},
        }
        self._known_macs: set[str] = set()
        self._initialized = False

        # Populate initial known MACs from current data
        if coordinator.data:
            for device in coordinator.data.connected_devices:
                if device.mac:
                    self._known_macs.add(device.mac)
            self._initialized = True

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        if self.coordinator.data is None:
            super()._handle_coordinator_update()
            return

        current_macs: set[str] = set()
        for device in self.coordinator.data.connected_devices:
            if device.mac and device.connected:
                current_macs.add(device.mac)

        if self._initialized:
            # Check for new devices
            new_macs = current_macs - self._known_macs
            for mac in new_macs:
                device_info = next(
                    (d for d in self.coordinator.data.connected_devices if d.mac == mac),
                    None,
                )
                if device_info:
                    # Determine if it's truly new (never seen before)
                    event_type = "new_device_connected"
                    self._trigger_event(
                        event_type,
                        {
                            "mac": mac,
                            "hostname": device_info.hostname or "unknown",
                            "ip": device_info.ip or "unknown",
                            "is_wireless": device_info.is_wireless,
                            "connection_type": device_info.connection_type,
                            "interface": device_info.interface,
                        },
                    )
                    _LOGGER.debug(
                        "New device event: %s (%s) connected",
                        device_info.hostname,
                        mac,
                    )

            # Check for disconnected devices
            gone_macs = self._known_macs - current_macs
            for mac in gone_macs:
                self._trigger_event(
                    "device_disconnected",
                    {"mac": mac},
                )

        self._known_macs = current_macs
        self._initialized = True
        super()._handle_coordinator_update()
