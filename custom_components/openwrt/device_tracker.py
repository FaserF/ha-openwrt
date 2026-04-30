"""Device tracker platform for OpenWrt integration.

Tracks connected devices (wireless and wired) using DHCP leases,
ARP tables, and wireless association lists.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any, cast

from homeassistant.components.device_tracker import (
    ScannerEntity,
    SourceType,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import (
    device_registry as dr,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ATTR_MANUFACTURER,
    CONF_CONSIDER_HOME,
    CONF_MQTT_PRESENCE,
    CONF_SKIP_RANDOM_MAC,
    CONF_TRACK_DEVICES,
    CONF_TRACK_WIRED,
    DATA_COORDINATOR,
    DEFAULT_CONSIDER_HOME,
    DEFAULT_SKIP_RANDOM_MAC,
    DEFAULT_TRACK_DEVICES,
    DEFAULT_TRACK_WIRED,
    DOMAIN,
)
from .coordinator import OpenWrtDataCoordinator
from .helpers import format_ap_device_id, is_random_mac
from .helpers.mac_vendor import get_mac_vendor_info

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up device tracker from config entry."""
    coordinator: OpenWrtDataCoordinator = hass.data[DOMAIN][entry.entry_id][
        DATA_COORDINATOR
    ]

    @callback
    def _async_cleanup_entities() -> None:
        """Clean up device trackers that should no longer be tracked."""
        ent_reg = er.async_get(hass)
        entries = er.async_entries_for_config_entry(ent_reg, entry.entry_id)

        track_devices = entry.options.get(
            CONF_TRACK_DEVICES,
            entry.data.get(CONF_TRACK_DEVICES, DEFAULT_TRACK_DEVICES),
        )
        track_wired = entry.options.get(
            CONF_TRACK_WIRED,
            entry.data.get(CONF_TRACK_WIRED, DEFAULT_TRACK_WIRED),
        )

        for ent in entries:
            if ent.domain != "device_tracker":
                continue

            # Remove ALL device trackers if disabled
            if not track_devices:
                ent_reg.async_remove(ent.entity_id)
                continue

            # Remove wired trackers if wired tracking is disabled
            # We identify them by their unique_id which ends with mac
            unique_id = ent.unique_id
            mac = unique_id.split("_")[-1].lower()

            # Check history to see if it's wired
            if not track_wired and mac in coordinator._device_history:
                if not coordinator._device_history[mac].get("is_wireless"):
                    ent_reg.async_remove(ent.entity_id)
                    continue

            # Remove if it belongs to the router itself or looks like an interface
            own_macs = (
                coordinator._get_own_macs(coordinator.data)
                if coordinator.data
                else set()
            )
            interface_regex = (
                r"^(wlan|eth|lan|wan|br-|radio|phy|veth|lo|bond|team)[0-9]*([.-].*)?$"
            )
            if mac in own_macs or re.match(interface_regex, mac):
                ent_reg.async_remove(ent.entity_id)
                continue

    hass.add_job(_async_cleanup_entities)

    if not entry.options.get(
        CONF_TRACK_DEVICES,
        entry.data.get(CONF_TRACK_DEVICES, DEFAULT_TRACK_DEVICES),
    ) or entry.options.get(CONF_MQTT_PRESENCE, False):
        if entry.options.get(CONF_MQTT_PRESENCE, False):
            _LOGGER.info(
                "MQTT Presence Detection enabled, skipping standard device trackers for %s",
                entry.data[CONF_HOST],
            )
        return

    track_wired = entry.options.get(
        CONF_TRACK_WIRED,
        entry.data.get(CONF_TRACK_WIRED, DEFAULT_TRACK_WIRED),
    )

    tracked_macs: set[str] = set()

    @callback
    def _async_add_new_devices() -> None:
        """Add new device tracker entities for newly discovered devices."""
        if coordinator.data is None:
            return

        perms = coordinator.data.permissions
        if not perms.read_network and not perms.read_wireless:
            return

        # Collect all unique MACs from both connected devices and DHCP leases
        unique_devices: dict[str, str | None] = {}
        for device in coordinator.data.connected_devices:
            if device.mac:
                unique_devices[device.mac.lower()] = device.hostname
        for lease in coordinator.data.dhcp_leases:
            if lease.mac:
                mac_lower = lease.mac.lower()
                if mac_lower not in unique_devices or not unique_devices[mac_lower]:
                    unique_devices[mac_lower] = lease.hostname

        new_entities: list[OpenWrtDeviceTracker] = []

        for mac, hostname in unique_devices.items():
            if mac in tracked_macs:
                continue

            # Check if it's wireless (from current data or history)
            is_wireless = False
            for device in coordinator.data.connected_devices:
                if device.mac and device.mac.lower() == mac:
                    is_wireless = device.is_wireless
                    break

            if not is_wireless and mac in coordinator._device_history:
                is_wireless = coordinator._device_history[mac].get("is_wireless", False)

            is_random = is_random_mac(mac)
            skip_random = entry.options.get(
                CONF_SKIP_RANDOM_MAC, DEFAULT_SKIP_RANDOM_MAC
            )

            _LOGGER.debug(
                "Evaluating device %s (hostname: %s): wireless=%s, random=%s, skip_random=%s, track_wired=%s",
                mac,
                hostname,
                is_wireless,
                is_random,
                skip_random,
                track_wired,
            )

            if is_random and skip_random:
                _LOGGER.debug(
                    "Skipping randomized MAC device %s (option enabled)",
                    mac,
                )
                continue

            if not track_wired and not is_wireless and not is_random:
                _LOGGER.debug(
                    "Skipping device %s (hostname: %s): not wireless, not random, and track_wired is False",
                    mac,
                    hostname,
                )
                continue

            _LOGGER.debug(
                "Adding/updating device tracker for %s (hostname: %s, wireless: %s, random: %s)",
                mac,
                hostname,
                is_wireless,
                is_random,
            )

            tracked_macs.add(mac)
            new_entities.append(OpenWrtDeviceTracker(coordinator, entry, mac, hostname))

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
        hostname: str | None = None,
    ) -> None:
        """Initialize the device tracker."""
        super().__init__(coordinator)
        self._mac = mac.lower()
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_tracker_{self._mac}"
        from .helpers import is_random_mac

        if is_random_mac(self._mac):
            self._attr_entity_registry_enabled_default = False

        # Initial device name fallback
        self._initial_name = hostname or mac
        self._consider_home = timedelta(
            seconds=entry.options.get(
                CONF_CONSIDER_HOME,
                entry.data.get(CONF_CONSIDER_HOME, DEFAULT_CONSIDER_HOME),
            ),
        )
        self._last_seen: datetime | None = None

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information."""
        router_id = cast(str, self._entry.unique_id or self._entry.data[CONF_HOST])
        via_device = (DOMAIN, router_id)
        if self.coordinator.data:
            for device in self.coordinator.data.connected_devices:
                if (
                    device.mac
                    and device.mac.lower() == self._mac
                    and device.is_wireless
                    and device.interface
                ):
                    stable_id = self.coordinator.interface_to_stable_id.get(
                        device.interface
                    )
                    if stable_id:
                        via_device = (
                            DOMAIN,
                            format_ap_device_id(router_id, stable_id),
                        )
                    break

        # Standard values for tracked devices
        manufacturer = ATTR_MANUFACTURER
        model = "Tracked device"

        # Try to identify manufacturer/model by MAC OUI
        if vendor_info := get_mac_vendor_info(self._mac):
            manufacturer, model = vendor_info

        return DeviceInfo(
            connections={(dr.CONNECTION_NETWORK_MAC, self._mac)},
            identifiers={(DOMAIN, self._mac)},
            name=self.name or self._initial_name,
            manufacturer=manufacturer,
            model=model,
            via_device=via_device,
        )

    @property
    def source_type(self) -> SourceType:
        """Return the source type."""
        return SourceType.ROUTER

    @property
    def is_connected(self) -> bool:
        """Return true if the device is connected."""
        device = self._get_device_data()
        connected = device.connected if device else False
        return self._check_consider_home(connected)

    def _get_device_data(self) -> Any | None:
        """Get data for this device from coordinator."""
        if not self.coordinator.data:
            return None
        return next(
            (
                d
                for d in self.coordinator.data.connected_devices
                if d.mac and d.mac.lower() == self._mac
            ),
            None,
        )

    def _check_consider_home(self, connected: bool) -> bool:
        """Apply consider_home logic: keep device home for a grace period."""
        now = datetime.now()
        if connected:
            self._last_seen = now
            return True

        # Not currently seen, check if within consider_home window
        return bool(self._last_seen and now - self._last_seen < self._consider_home)

    @property
    def mac_address(self) -> str:
        """Return the MAC address."""
        return self._mac

    @property
    def hostname(self) -> str | None:
        """Return the hostname."""
        device = self._get_device_data()
        return device.hostname if device else None

    @property
    def ip_address(self) -> str | None:
        """Return the IP address."""
        device = self._get_device_data()
        return device.ip if device else None

    @property
    def name(self) -> str:
        """Return the name of the device."""
        hostname = self.hostname
        if hostname and hostname != "*":
            # Avoid using the router's hostname as a generic fallback for other devices
            router_hostname = ""
            if self.coordinator.data and self.coordinator.data.device_info:
                router_hostname = self.coordinator.data.device_info.hostname

            if hostname != router_hostname:
                return hostname

        return self._mac

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        device = self._get_device_data()
        if not device:
            return {}

        attrs = {
            "mac": device.mac,
            "is_wireless": device.is_wireless,
            "connection_type": device.connection_type,
        }

        # Add historical seen data
        if device.mac.lower() in self.coordinator._device_history:
            history = self.coordinator._device_history[device.mac.lower()]
            attrs.update(
                {
                    "initially_seen": datetime.fromtimestamp(
                        history["initially_seen"]
                    ).isoformat(),
                    "last_seen": datetime.fromtimestamp(
                        history["last_seen"]
                    ).isoformat(),
                }
            )

        # Add optional metrics
        optional_metrics = {
            "interface": device.interface,
            "rx_bytes": device.rx_bytes,
            "tx_bytes": device.tx_bytes,
            "uptime": device.uptime,
            "neighbor_state": device.neighbor_state,
            "connection_info": device.connection_info,
        }
        if device.is_wireless:
            optional_metrics.update(
                {
                    "signal_strength": device.signal,
                    "rx_rate": device.rx_rate,
                    "tx_rate": device.tx_rate,
                }
            )

        attrs.update({k: v for k, v in optional_metrics.items() if v})
        return attrs
