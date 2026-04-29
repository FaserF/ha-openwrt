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

from .const import (
    CONF_TRACK_DEVICES,
    DATA_COORDINATOR,
    DEFAULT_TRACK_DEVICES,
    DOMAIN,
)
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

    track_devices = entry.options.get(
        CONF_TRACK_DEVICES,
        entry.data.get(CONF_TRACK_DEVICES, DEFAULT_TRACK_DEVICES),
    )

    if coordinator.data and track_devices:
        perms = coordinator.data.permissions
        if perms.read_network or perms.read_wireless:
            async_add_entities([OpenWrtNewDeviceEvent(coordinator, entry)])


class OpenWrtNewDeviceEvent(CoordinatorEntity[OpenWrtDataCoordinator], EventEntity):
    """Event entity that fires when a new device connects to the network."""

    _attr_has_entity_name = True
    _attr_name = "New Device"
    _attr_translation_key = "new_device"
    _attr_device_class = EventDeviceClass.BUTTON
    _attr_event_types = [
        "new_device_connected",
        "device_connected",
        "device_disconnected",
    ]

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
            "identifiers": {(DOMAIN, entry.unique_id or entry.data[CONF_HOST])},
        }
        # mac -> last_seen_connected_timestamp
        self._connected_macs: dict[str, float] = {}
        self._initialized = False

        # Populate initial connected MACs
        if coordinator.data:
            current_time = coordinator.hass.loop.time()
            for device in coordinator.data.connected_devices:
                if device.mac and device.connected:
                    self._connected_macs[device.mac] = current_time
            self._initialized = True

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        if self.coordinator.data is None:
            super()._handle_coordinator_update()
            return

        current_time = self.coordinator.hass.loop.time()
        current_connected: set[str] = set()
        for device in self.coordinator.data.connected_devices:
            if device.mac and device.connected:
                current_connected.add(device.mac)

        if not self._initialized:
            for mac in current_connected:
                self._connected_macs[mac] = current_time
            self._initialized = True
            super()._handle_coordinator_update()
            return

        # 1. New connections
        for mac in current_connected:
            if mac not in self._connected_macs:
                device_info = next(
                    (
                        d
                        for d in self.coordinator.data.connected_devices
                        if d.mac == mac
                    ),
                    None,
                )
                if device_info:
                    # Determine if it's truly new or just reconnected
                    is_truly_new = mac not in self.coordinator._device_history
                    event_type = (
                        "new_device_connected" if is_truly_new else "device_connected"
                    )

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
                        "%s event: %s (%s) connected",
                        event_type.replace("_", " ").title(),
                        device_info.hostname,
                        mac,
                    )

            # Update last seen timestamp
            self._connected_macs[mac] = current_time

        # 2. Disconnections (with 60s grace period to handle polling glitches)
        disconnection_threshold = 60
        gone_macs = [
            mac
            for mac, last_seen in self._connected_macs.items()
            if mac not in current_connected
            and (current_time - last_seen) > disconnection_threshold
        ]

        for mac in gone_macs:
            self._trigger_event(
                "device_disconnected",
                {"mac": mac},
            )
            _LOGGER.debug("Device disconnected event: %s", mac)
            del self._connected_macs[mac]

        super()._handle_coordinator_update()
