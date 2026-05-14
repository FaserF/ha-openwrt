"""Device tracker platform for OpenWrt integration.

Tracks connected devices (wireless and wired) using DHCP leases,
ARP tables, and wireless association lists.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any

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
from .helpers import get_via_device, is_random_mac
from .helpers.mac_vendor import get_mac_vendor_info

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up device tracker."""
    coordinator: OpenWrtDataCoordinator = hass.data[DOMAIN][entry.entry_id][
        DATA_COORDINATOR
    ]

    @callback
    def _async_cleanup_entities() -> None:
        """Clean up entities."""
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

        mqtt_enabled = entry.options.get(CONF_MQTT_PRESENCE, False)

        for ent in entries:
            if ent.domain != "device_tracker":
                continue

            # Remove ALL device trackers if disabled or MQTT presence is active
            if not track_devices or mqtt_enabled:
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
        """Add new devices."""
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

            # Determine if it's currently wireless on THIS node
            is_currently_wireless = False
            for device in coordinator.data.connected_devices:
                if device.mac and device.mac.lower() == mac:
                    is_currently_wireless = device.is_wireless
                    break

            # Determine if it's a known wireless device from history
            was_ever_wireless = coordinator._device_history.get(mac, {}).get(
                "is_wireless", False
            )

            # A device is considered wireless for entity classification if it is
            # currently wireless OR was ever known to be wireless.
            is_wireless = is_currently_wireless or was_ever_wireless

            is_random = is_random_mac(mac)
            skip_random = entry.options.get(
                CONF_SKIP_RANDOM_MAC, DEFAULT_SKIP_RANDOM_MAC
            )

            _LOGGER.debug(
                "Evaluating device %s (hostname: %s): currently_wireless=%s, was_ever_wireless=%s, random=%s, skip_random=%s, track_wired=%s",
                mac,
                hostname,
                is_currently_wireless,
                was_ever_wireless,
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

            # Skip if we don't track wired devices and this device is not wireless
            # (neither currently nor historically).
            if not track_wired and not is_wireless:
                _LOGGER.debug(
                    "Skipping device %s (hostname: %s): not wireless and track_wired is False",
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
        """Initialize."""
        super().__init__(coordinator)
        self._mac = mac.lower()
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_tracker_{self._mac}"

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

        # Track all created entity instances globally to enable cross-entry peer notification
        domain_data = coordinator.hass.data.setdefault(DOMAIN, {})
        all_trackers = domain_data.setdefault("all_trackers", {})
        trackers = all_trackers.setdefault(self._mac, [])
        if self not in trackers:
            trackers.append(self)

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info."""
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
            via_device=get_via_device(
                self.coordinator.hass, self.coordinator, self._entry, self._mac
            ),
        )

    @property
    def source_type(self) -> SourceType:
        """Return source type."""
        return SourceType.ROUTER

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        device = self._raw_get_device_data()
        domain_data = self.coordinator.hass.data.setdefault(DOMAIN, {})
        wireless_states = domain_data.setdefault("tracker_wireless_state", {})
        state_info = wireless_states.get(self._mac) or {}

        now = datetime.now()
        is_wireless = device.is_wireless if device else False
        connected = device.connected if device else False

        # Authority-based writing & Last-wireless-writer wins rule:
        # Only the instance that sees the device via active wireless association takes ownership.
        if device and is_wireless and connected:
            # We use the config entry title as the AP name, falling back to host
            ap_name = self.coordinator.config_entry.title or self._entry.data.get(
                CONF_HOST
            )
            state_info = {
                "owner_entry_id": self._entry.entry_id,
                "last_seen": now,
                "connected": True,
                "connected_ap": ap_name,
                "connected_ap_entry_id": self._entry.entry_id,
                "interface": device.interface,
                "signal_strength": device.signal,
                "connection_type": device.connection_type,
            }
            wireless_states[self._mac] = state_info
        elif state_info.get("owner_entry_id") == self._entry.entry_id:
            # If this instance was the wireless owner but no longer sees it connected wirelessly,
            # update the global state to away.
            state_info["connected"] = False
            wireless_states[self._mac] = state_info

        self.async_write_ha_state()

        # Notify all peer instances so the registered entity updates immediately in HA
        trackers = domain_data.get("all_trackers", {}).get(self._mac, [])
        for peer in trackers:
            if peer is not self:
                peer.async_write_ha_state()

    @property
    def is_connected(self) -> bool:
        """Return connection status."""
        domain_data = self.coordinator.hass.data.get(DOMAIN, {})
        wireless_states = domain_data.get("tracker_wireless_state", {})
        state_info = wireless_states.get(self._mac)

        # If a wireless owner has claimed this device, respect its connection state
        if state_info and "connected" in state_info:
            return self._check_consider_home(state_info["connected"])

        # Fallback for pure wired devices that are never seen wirelessly on any AP
        device = self._raw_get_device_data()
        if not device:
            return self._check_consider_home(False)

        track_wired = self._entry.options.get(
            CONF_TRACK_WIRED,
            self._entry.data.get(CONF_TRACK_WIRED, DEFAULT_TRACK_WIRED),
        )
        if not track_wired and not device.is_wireless:
            return self._check_consider_home(False)

        return self._check_consider_home(device.connected)

    def _raw_get_device_data(self) -> Any | None:
        """Get raw device data from own coordinator."""
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

    # Maintain backward compatibility with existing internal callers if any
    _get_device_data = _raw_get_device_data

    def _check_consider_home(self, connected: bool) -> bool:
        """Apply consider_home logic."""
        now = datetime.now()
        if connected:
            self._last_seen = now
            return True

        # Not currently seen, check if within consider_home window
        return bool(self._last_seen and now - self._last_seen < self._consider_home)

    @property
    def mac_address(self) -> str:
        """Return MAC."""
        # Intentionally return bare MAC so HA deduplicates and registers exactly 1 entity per device
        return self._mac

    @property
    def hostname(self) -> str | None:
        """Return hostname."""
        device = self._raw_get_device_data()
        return device.hostname if device else None

    @property
    def ip_address(self) -> str | None:
        """Return IP."""
        device = self._raw_get_device_data()
        return device.ip if device else None

    @property
    def name(self) -> str:
        """Return name."""
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
        """Return attributes."""
        domain_data = self.coordinator.hass.data.get(DOMAIN, {})
        wireless_states = domain_data.get("tracker_wireless_state", {})
        state_info = wireless_states.get(self._mac)

        device = self._raw_get_device_data()
        attrs: dict[str, Any] = {
            "mac": self._mac,
        }

        if device:
            attrs.update(
                {
                    "is_wireless": device.is_wireless,
                    "connection_type": device.connection_type,
                }
            )
        elif state_info:
            attrs.update(
                {
                    "is_wireless": True,
                    "connection_type": state_info.get("connection_type"),
                }
            )

        # Add AP attribution attributes from global wireless state
        if state_info and state_info.get("connected"):
            attrs.update(
                {
                    "connected_ap": state_info.get("connected_ap"),
                    "connected_ap_entry_id": state_info.get("connected_ap_entry_id"),
                    "interface": state_info.get("interface"),
                    "signal_strength": state_info.get("signal_strength"),
                }
            )
        else:
            attrs.update(
                {
                    "connected_ap": None,
                    "interface": device.interface if device else None,
                }
            )

        # Add historical seen data
        if self._mac in self.coordinator._device_history:
            history = self.coordinator._device_history[self._mac]
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
        optional_metrics = {}
        if device:
            optional_metrics.update(
                {
                    "port": device.port,
                    "fdb_age": device.fdb_age,
                    "rx_bytes": device.rx_bytes,
                    "tx_bytes": device.tx_bytes,
                    "uptime": device.uptime,
                    "neighbor_state": device.neighbor_state,
                    "connection_info": device.connection_info,
                }
            )
            if device.is_wireless:
                optional_metrics.update(
                    {
                        "signal_strength": device.signal,
                        "rx_rate": device.rx_rate,
                        "tx_rate": device.tx_rate,
                    }
                )
        elif state_info and state_info.get("connected"):
            optional_metrics["signal_strength"] = state_info.get("signal_strength")

        attrs.update(
            {k: v for k, v in optional_metrics.items() if v is not None and v != ""}
        )

        # Add Mesh info
        if (
            self.coordinator.data
            and self._mac in self.coordinator.data.batman_translation_table
        ):
            originator_mac = self.coordinator.data.batman_translation_table[self._mac]
            attrs["mesh_node"] = originator_mac
            if (
                originator_mac.lower()
                != self.coordinator.data.device_info.mac_address.lower()
            ):
                attrs["is_via_mesh"] = True

        return attrs
