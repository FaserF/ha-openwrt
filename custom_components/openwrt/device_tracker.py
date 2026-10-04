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
    CONF_TRUST_STALE_ARP,
    DATA_COORDINATOR,
    DEFAULT_CONSIDER_HOME,
    DEFAULT_SKIP_RANDOM_MAC,
    DEFAULT_TRACK_DEVICES,
    DEFAULT_TRACK_WIRED,
    DOMAIN,
)
from .coordinator import OpenWrtDataCoordinator
from .device_trackers import OpenWrtDeviceTracker, compute_device_tracker_attrs
from .helpers import get_via_device_id, is_random_mac, resolve_client_name
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
        whitelist = coordinator._async_get_tracked_devices_whitelist()

        for ent in entries:
            if ent.domain != "device_tracker":
                continue

            # Remove ALL device trackers if disabled or MQTT presence is active
            if not track_devices or mqtt_enabled:
                ent_reg.async_remove(ent.entity_id)
                continue

            unique_id = ent.unique_id
            mac = (
                unique_id.removeprefix("openwrt_tracker_").lower()
                if unique_id.startswith("openwrt_tracker_")
                else unique_id.split("_")[-1].lower()
            )

            # Remove if not in whitelist when whitelist is active
            if whitelist is not None and mac not in whitelist:
                ent_reg.async_remove(ent.entity_id)
                continue

            # Remove wired trackers if wired tracking is disabled
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

        whitelist = coordinator._async_get_tracked_devices_whitelist()

        # Collect all unique MACs from connected devices, DHCP leases, persistent history, and registry
        unique_devices: dict[str, str | None] = {}
        for device in coordinator.data.connected_devices:
            if device.mac:
                unique_devices[device.mac.lower()] = device.hostname
        for lease in coordinator.data.dhcp_leases:
            if lease.mac:
                mac_lower = lease.mac.lower()
                if mac_lower not in unique_devices or not unique_devices[mac_lower]:
                    unique_devices[mac_lower] = lease.hostname

        # Include historical tracked devices so offline devices persist across restarts
        for mac, hist in coordinator._device_history.items():
            mac_lower = mac.lower()
            if whitelist is not None and mac_lower not in whitelist:
                continue
            if mac_lower not in unique_devices or not unique_devices[mac_lower]:
                unique_devices[mac_lower] = hist.get("hostname")

        # Include any explicit whitelist entries
        if whitelist is not None:
            for mac in whitelist:
                mac_lower = mac.lower()
                if mac_lower not in unique_devices:
                    unique_devices[mac_lower] = None

        # Include any existing registered entities for this config entry
        ent_reg = er.async_get(hass)
        entries = er.async_entries_for_config_entry(ent_reg, entry.entry_id)
        for ent in entries:
            if ent.domain == "device_tracker" and ent.unique_id.startswith(
                "openwrt_tracker_"
            ):
                mac_lower = ent.unique_id.removeprefix("openwrt_tracker_").lower()
                if whitelist is not None and mac_lower not in whitelist:
                    continue
                if mac_lower not in unique_devices:
                    unique_devices[mac_lower] = None

        # An access point has no DHCP data, so fall back to the hostname another
        # config entry resolved. Without this its trackers are named by MAC.
        shared_hostnames = hass.data.get(DOMAIN, {}).get("hostname_registry", {})
        for mac_lower, hostname in unique_devices.items():
            if not hostname or hostname == "*":
                unique_devices[mac_lower] = shared_hostnames.get(mac_lower) or hostname

        own_macs = (
            coordinator._get_own_macs(coordinator.data) if coordinator.data else set()
        )
        interface_regex = (
            r"^(wlan|eth|lan|wan|br-|radio|phy|veth|lo|bond|team)[0-9]*([.-].*)?$"
        )

        new_entities: list[OpenWrtDeviceTracker] = []

        for mac, hostname in unique_devices.items():
            if mac in tracked_macs:
                continue

            if mac in own_macs or re.match(interface_regex, mac):
                continue

            # Determine if it's currently wireless on THIS node
            is_currently_wireless = False
            for device in coordinator.data.connected_devices:
                if device.mac and device.mac.lower() == mac:
                    if device.is_wireless:
                        is_currently_wireless = True
                        break

            # Determine if it's a known wireless device from history
            domain_data = hass.data.setdefault(DOMAIN, {})
            wireless_history = domain_data.setdefault("wireless_history", {})
            registered_macs = domain_data.setdefault("registered_macs", set())

            was_ever_wireless = wireless_history.get(
                mac, False
            ) or coordinator._device_history.get(mac, {}).get("is_wireless", False)

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

            # Check if this MAC has already been registered by ANY integration instance
            if mac in registered_macs:
                # Add to local tracked_macs so we don't evaluate it again on this instance
                tracked_macs.add(mac)
                continue

            _LOGGER.debug(
                "Adding/updating device tracker for %s (hostname: %s, wireless: %s, random: %s)",
                mac,
                hostname,
                is_wireless,
                is_random,
            )

            tracked_macs.add(mac)
            registered_macs.add(mac)
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


__all__ = [
    "ATTR_MANUFACTURER",
    "AddEntitiesCallback",
    "Any",
    "CONF_CONSIDER_HOME",
    "CONF_HOST",
    "CONF_MQTT_PRESENCE",
    "CONF_SKIP_RANDOM_MAC",
    "CONF_TRACK_DEVICES",
    "CONF_TRACK_WIRED",
    "CONF_TRUST_STALE_ARP",
    "ConfigEntry",
    "CoordinatorEntity",
    "DATA_COORDINATOR",
    "DEFAULT_CONSIDER_HOME",
    "DEFAULT_SKIP_RANDOM_MAC",
    "DEFAULT_TRACK_DEVICES",
    "DEFAULT_TRACK_WIRED",
    "DOMAIN",
    "DeviceInfo",
    "HomeAssistant",
    "OpenWrtDataCoordinator",
    "OpenWrtDeviceTracker",
    "ScannerEntity",
    "SourceType",
    "_LOGGER",
    "async_setup_entry",
    "callback",
    "compute_device_tracker_attrs",
    "datetime",
    "dr",
    "er",
    "get_mac_vendor_info",
    "get_via_device_id",
    "is_random_mac",
    "logging",
    "re",
    "resolve_client_name",
    "timedelta",
]
