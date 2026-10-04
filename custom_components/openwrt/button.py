"""Button platform for OpenWrt integration."""

from __future__ import annotations

import logging
import re

from homeassistant.components.button import (
    ButtonDeviceClass,
    ButtonEntity,
    ButtonEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api.base import OpenWrtClient
from .buttons import (
    BUTTONS,
    OpenWrtButtonDescription,
    OpenWrtButtonEntity,
    OpenWrtKickButton,
    OpenWrtWakeOnLanButton,
    _add_device_buttons,
    _add_extra_service_buttons,
    _add_interface_buttons,
    _add_service_buttons,
    _add_static_buttons,
    _add_wireless_buttons,
    _get_unique_devices,
)
from .const import (
    DATA_CLIENT,
    DATA_COORDINATOR,
    DOMAIN,
)
from .coordinator import OpenWrtDataCoordinator
from .helpers import get_via_device_id, is_random_mac, resolve_client_name

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up buttons."""
    coordinator: OpenWrtDataCoordinator = hass.data[DOMAIN][entry.entry_id][
        DATA_COORDINATOR
    ]
    client: OpenWrtClient = hass.data[DOMAIN][entry.entry_id][DATA_CLIENT]

    tracked_keys: set[str] = set()

    def _async_add_new_entities() -> None:
        """Add new entities."""
        if not coordinator.data:
            return

        new_entities: list[ButtonEntity] = []

        # Static buttons
        _add_static_buttons(coordinator, entry, client, tracked_keys, new_entities)

        # Service buttons
        if coordinator.data.permissions.read_services:
            _add_service_buttons(coordinator, entry, client, tracked_keys, new_entities)

        # Interface buttons
        _add_interface_buttons(coordinator, entry, client, tracked_keys, new_entities)

        # Wireless buttons (WPS Push)
        _add_wireless_buttons(coordinator, entry, client, tracked_keys, new_entities)

        # Extra service buttons (AdBlock, etc.)
        _add_extra_service_buttons(
            coordinator, entry, client, tracked_keys, new_entities
        )

        # Device-specific buttons (WoL, Kick)
        _add_device_buttons(coordinator, entry, client, tracked_keys, new_entities)

        if new_entities:
            async_add_entities(new_entities)

    # Register listener and run initial discovery
    entry.async_on_unload(coordinator.async_add_listener(_async_add_new_entities))
    _async_add_new_entities()

    @callback
    def _async_cleanup_entities() -> None:
        ent_reg = er.async_get(hass)
        entries = er.async_entries_for_config_entry(ent_reg, entry.entry_id)
        own_macs = (
            coordinator._get_own_macs(coordinator.data) if coordinator.data else set()
        )
        interface_regex = (
            r"^(wlan|eth|lan|wan|br-|radio|phy|veth|lo|bond|team)[0-9]*([.-].*)?$"
        )
        active_interfaces = (
            {wifi.name for wifi in coordinator.data.wireless_interfaces if wifi.name}
            if coordinator.data
            else set()
        )

        for ent in entries:
            if ent.domain != "button":
                continue

            unique_id = ent.unique_id
            # Cleanup WoL/Kick buttons
            if "_wol" in unique_id or "_kick" in unique_id:
                parts = unique_id.split("_")
                mac = ""
                if "_wol" in unique_id:
                    mac = unique_id.split("_")[-2].lower()
                elif "_kick" in unique_id:
                    for i, part in enumerate(parts):
                        if part == "kick" and i > 0:
                            mac = parts[i - 1].lower()
                            break

                if mac:
                    # Remove if it belongs to the router itself
                    if mac in own_macs:
                        _LOGGER.debug(
                            "Removing button entity for router's own interface: %s",
                            ent.entity_id,
                        )
                        ent_reg.async_remove(ent.entity_id)
                        continue

                    # Remove if the "MAC" looks like an interface name (migration/old bug)
                    if re.match(interface_regex, mac):
                        _LOGGER.debug(
                            "Removing legacy button entity with interface name: %s",
                            ent.entity_id,
                        )
                        ent_reg.async_remove(ent.entity_id)
                        continue

                # Existing cleanup: WoL buttons for wireless devices
                if "_wol" in unique_id and mac in coordinator._device_history:
                    if coordinator._device_history[mac].get("is_wireless"):
                        _LOGGER.debug(
                            "Removing WoL button for wireless device: %s", ent.entity_id
                        )
                        ent_reg.async_remove(ent.entity_id)
                        continue

                # Cleanup: Kick buttons with legacy interface names
                if "_kick_" in unique_id:
                    iface = unique_id.split("_kick_")[-1]
                    if iface and active_interfaces and iface not in active_interfaces:
                        _LOGGER.debug(
                            "Removing legacy kick button with old interface name '%s': %s",
                            iface,
                            ent.entity_id,
                        )
                        ent_reg.async_remove(ent.entity_id)
                        continue

    hass.add_job(_async_cleanup_entities)
    _async_add_new_entities()


__all__ = [
    "BUTTONS",
    "ButtonDeviceClass",
    "ButtonEntity",
    "ButtonEntityDescription",
    "CONF_HOST",
    "CoordinatorEntity",
    "DeviceInfo",
    "EntityCategory",
    "HomeAssistantError",
    "OpenWrtButtonDescription",
    "OpenWrtButtonEntity",
    "OpenWrtKickButton",
    "OpenWrtWakeOnLanButton",
    "_add_device_buttons",
    "_add_extra_service_buttons",
    "_add_interface_buttons",
    "_add_service_buttons",
    "_add_static_buttons",
    "_add_wireless_buttons",
    "_get_unique_devices",
    "async_setup_entry",
    "dr",
    "er",
    "get_via_device_id",
    "is_random_mac",
    "resolve_client_name",
]
