"""Device tracker platform for OpenWrt integration.

Tracks connected devices (wireless and wired) using DHCP leases,
ARP tables, and wireless association lists.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.device_tracker import (
    ScannerEntity,
    SourceType,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_CONSIDER_HOME,
    CONF_TRACK_DEVICES,
    CONF_TRACK_WIRED,
    DATA_COORDINATOR,
    DEFAULT_CONSIDER_HOME,
    DEFAULT_TRACK_DEVICES,
    DEFAULT_TRACK_WIRED,
    DOMAIN,
)
from .coordinator import OpenWrtDataCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up device tracker from config entry."""
    track_devices = entry.options.get(CONF_TRACK_DEVICES, DEFAULT_TRACK_DEVICES)
    if not track_devices:
        return

    coordinator: OpenWrtDataCoordinator = hass.data[DOMAIN][entry.entry_id][
        DATA_COORDINATOR
    ]
    track_wired = entry.options.get(CONF_TRACK_WIRED, DEFAULT_TRACK_WIRED)

    tracked_macs: set[str] = set()

    @callback
    def _async_add_new_devices() -> None:
        """Add new device tracker entities for newly discovered devices."""
        if coordinator.data is None:
            return

        new_entities: list[OpenWrtDeviceTracker] = []

        for device in coordinator.data.connected_devices:
            if not device.mac:
                continue
            if device.mac in tracked_macs:
                continue
            if not track_wired and not device.is_wireless:
                continue

            tracked_macs.add(device.mac)
            new_entities.append(OpenWrtDeviceTracker(coordinator, entry, device.mac))

        if new_entities:
            async_add_entities(new_entities)

    _LOGGER.debug(
        "Setting up device tracker for %s, found %d connected devices",
        entry.data[CONF_HOST],
        len(coordinator.data.connected_devices) if coordinator.data else 0,
    )
    _async_add_new_devices()

    entry.async_on_unload(coordinator.async_add_listener(_async_add_new_devices))


class OpenWrtDeviceTracker(CoordinatorEntity[OpenWrtDataCoordinator], ScannerEntity):
    """Representation of a tracked device on the OpenWrt router."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: OpenWrtDataCoordinator,
        entry: ConfigEntry,
        mac: str,
    ) -> None:
        """Initialize the device tracker."""
        super().__init__(coordinator)
        self._mac = mac
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_tracker_{mac.replace(':', '_')}"
        self._attr_device_info = DeviceInfo(
            connections={("mac", mac)},
            name=self.name,
            via_device=(DOMAIN, entry.data[CONF_HOST]),
        )
        self._consider_home = entry.options.get(
            CONF_CONSIDER_HOME, DEFAULT_CONSIDER_HOME
        )

    @property
    def source_type(self) -> SourceType:
        """Return the source type."""
        if self.coordinator.data:
            for device in self.coordinator.data.connected_devices:
                if device.mac == self._mac and device.is_wireless:
                    return SourceType.ROUTER
        return SourceType.ROUTER

    @property
    def is_connected(self) -> bool:
        """Return true if the device is connected."""
        if self.coordinator.data is None:
            return False
        return any(
            d.mac == self._mac and d.connected
            for d in self.coordinator.data.connected_devices
        )

    @property
    def mac_address(self) -> str:
        """Return the MAC address."""
        return self._mac

    @property
    def hostname(self) -> str | None:
        """Return the hostname."""
        if self.coordinator.data:
            for device in self.coordinator.data.connected_devices:
                if device.mac == self._mac:
                    return device.hostname or None
        return None

    @property
    def ip_address(self) -> str | None:
        """Return the IP address."""
        if self.coordinator.data:
            for device in self.coordinator.data.connected_devices:
                if device.mac == self._mac:
                    return device.ip or None
        return None

    @property
    def name(self) -> str:
        """Return the name of the device."""
        hostname = self.hostname
        if hostname:
            return hostname
        return self._mac

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        if self.coordinator.data is None:
            return {}

        for device in self.coordinator.data.connected_devices:
            if device.mac == self._mac:
                attrs: dict[str, Any] = {
                    "mac": device.mac,
                    "is_wireless": device.is_wireless,
                    "connection_type": device.connection_type,
                }
                if device.interface:
                    attrs["interface"] = device.interface
                if device.is_wireless:
                    if device.signal:
                        attrs["signal_strength"] = device.signal
                    if device.rx_rate:
                        attrs["rx_rate"] = device.rx_rate
                    if device.tx_rate:
                        attrs["tx_rate"] = device.tx_rate
                if device.rx_bytes:
                    attrs["rx_bytes"] = device.rx_bytes
                if device.tx_bytes:
                    attrs["tx_bytes"] = device.tx_bytes
                if device.uptime:
                    attrs["uptime"] = device.uptime
                return attrs

        return {}
