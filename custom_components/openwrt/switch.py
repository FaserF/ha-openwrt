"""Switch platform for OpenWrt integration."""

from __future__ import annotations

import logging
import re

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import (
    entity_registry as er,
)
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api.base import OpenWrtClient, ServiceInfo
from .const import (
    CONF_ENABLE_FIREWALL,
    CONF_ENABLE_LED,
    CONF_ENABLE_SERVICES,
    CONF_ENABLE_SQM,
    CONF_ENABLE_VPN,
    CONF_SKIP_RANDOM_MAC,
    CONF_TRACK_DEVICES,
    CONF_TRACK_WIRED,
    DATA_CLIENT,
    DATA_COORDINATOR,
    DEFAULT_SKIP_RANDOM_MAC,
    DEFAULT_TRACK_DEVICES,
    DEFAULT_TRACK_WIRED,
    DOMAIN,
)
from .coordinator import OpenWrtDataCoordinator
from .helpers import (
    _get_router_device_id,
    _lookup_device,
    format_ap_device_id,
    format_ap_name,
    format_radio_device_id,
    format_radio_name,
    normalize_band,
)
from .switches import (
    SERVICE_ICONS,
    OpenWrtAccessControlSwitch,
    OpenWrtAdBlockSwitch,
    OpenWrtBanIpSwitch,
    OpenWrtFirewallRuleSwitch,
    OpenWrtFirewallSwitch,
    OpenWrtLedSwitch,
    OpenWrtRadioSwitch,
    OpenWrtServiceSwitch,
    OpenWrtSimpleAdBlockSwitch,
    OpenWrtSqmSwitch,
    OpenWrtWireGuardSwitch,
    OpenWrtWirelessSwitch,
    OpenWrtWpsSwitch,
    _add_access_control_switches,
    _add_firewall_switches,
    _add_led_switches,
    _add_package_switches,
    _add_service_switches,
    _add_sqm_switches,
    _add_vpn_switches,
    _add_wireless_switches,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up switches."""
    coordinator: OpenWrtDataCoordinator = hass.data[DOMAIN][entry.entry_id][
        DATA_COORDINATOR
    ]
    client: OpenWrtClient = hass.data[DOMAIN][entry.entry_id][DATA_CLIENT]

    tracked_keys: set[str] = set()

    def _async_add_new_entities() -> None:
        """Add new entities."""
        if not coordinator.data:
            return

        perms = coordinator.data.permissions
        pkgs = coordinator.data.packages
        entities: list[SwitchEntity] = []

        if perms.write_wireless:
            _add_wireless_switches(coordinator, entry, client, entities, tracked_keys)

        if perms.write_services and entry.options.get(CONF_ENABLE_SERVICES, True):
            _add_service_switches(coordinator, entry, client, entities, tracked_keys)

        if perms.write_firewall and entry.options.get(CONF_ENABLE_FIREWALL, True):
            _add_firewall_switches(coordinator, entry, client, entities, tracked_keys)

        if perms.write_access_control:
            _add_access_control_switches(
                coordinator, entry, client, entities, tracked_keys
            )

        if (
            perms.write_sqm
            and pkgs.sqm_scripts is not False
            and entry.options.get(CONF_ENABLE_SQM, True)
        ):
            _add_sqm_switches(coordinator, entry, client, entities, tracked_keys)

        if perms.write_vpn and entry.options.get(CONF_ENABLE_VPN, True):
            _add_vpn_switches(coordinator, entry, client, entities, tracked_keys)

        if perms.write_led and entry.options.get(CONF_ENABLE_LED, True):
            _add_led_switches(coordinator, entry, client, entities, tracked_keys)

        _add_package_switches(coordinator, entry, client, entities, tracked_keys, pkgs)

        if entities:
            async_add_entities(entities)

    # Register listener and run initial discovery
    entry.async_on_unload(coordinator.async_add_listener(_async_add_new_entities))
    _async_add_new_entities()

    @callback
    def _async_cleanup_entities() -> None:
        """Clean up stale entities from the entity registry."""
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
            if ent.domain != "switch":
                continue

            unique_id = ent.unique_id
            # Cleanup access control switches if device tracking is disabled
            if "_access_" in unique_id:
                if not track_devices:
                    ent_reg.async_remove(ent.entity_id)
                    continue

                mac = unique_id.split("_access_")[-1].replace("_", ":").lower()
                # Remove wired access switches if wired tracking is disabled
                if not track_wired and mac in coordinator._device_history:
                    if not coordinator._device_history[mac].get("is_wireless"):
                        ent_reg.async_remove(ent.entity_id)
                        continue

            # Cleanup orphaned wireless switches (e.g. ghost radios) only if wireless interfaces are known
            if (
                "_wireless_" in unique_id
                and coordinator.data
                and coordinator.data.wireless_interfaces
            ):
                iface_name = unique_id.split("_wireless_")[-1]
                if not re.match(
                    r"^(?:wifinet\d+|@?wifi-iface\[\d+\])$", iface_name, re.IGNORECASE
                ) and not any(
                    w.name == iface_name
                    or w.section == iface_name
                    or (w.ifname and w.ifname == iface_name)
                    or (w.ssid and w.ssid == iface_name)
                    for w in coordinator.data.wireless_interfaces
                ):
                    ent_reg.async_remove(ent.entity_id)
                    continue

            if (
                "_radio_" in unique_id
                and coordinator.data
                and coordinator.data.wireless_interfaces
            ):
                radio = unique_id.split("_radio_")[-1]
                if not any(
                    wifi.radio == radio for wifi in coordinator.data.wireless_interfaces
                ):
                    ent_reg.async_remove(ent.entity_id)
                    continue

    hass.add_job(_async_cleanup_entities)


__all__ = [
    "CONF_ENABLE_FIREWALL",
    "CONF_ENABLE_LED",
    "CONF_ENABLE_SERVICES",
    "CONF_ENABLE_SQM",
    "CONF_ENABLE_VPN",
    "CONF_HOST",
    "CONF_SKIP_RANDOM_MAC",
    "CONF_TRACK_DEVICES",
    "CONF_TRACK_WIRED",
    "CoordinatorEntity",
    "DATA_CLIENT",
    "DATA_COORDINATOR",
    "DEFAULT_SKIP_RANDOM_MAC",
    "DEFAULT_TRACK_DEVICES",
    "DEFAULT_TRACK_WIRED",
    "DOMAIN",
    "DeviceInfo",
    "EntityCategory",
    "HomeAssistant",
    "HomeAssistantError",
    "OpenWrtAccessControlSwitch",
    "OpenWrtAdBlockSwitch",
    "OpenWrtBanIpSwitch",
    "OpenWrtClient",
    "OpenWrtDataCoordinator",
    "OpenWrtFirewallRuleSwitch",
    "OpenWrtFirewallSwitch",
    "OpenWrtLedSwitch",
    "OpenWrtRadioSwitch",
    "OpenWrtServiceSwitch",
    "OpenWrtSimpleAdBlockSwitch",
    "OpenWrtSqmSwitch",
    "OpenWrtWireGuardSwitch",
    "OpenWrtWirelessSwitch",
    "OpenWrtWpsSwitch",
    "SERVICE_ICONS",
    "ServiceInfo",
    "SwitchDeviceClass",
    "SwitchEntity",
    "_add_access_control_switches",
    "_add_firewall_switches",
    "_add_led_switches",
    "_add_package_switches",
    "_add_service_switches",
    "_add_sqm_switches",
    "_add_vpn_switches",
    "_add_wireless_switches",
    "_get_router_device_id",
    "_lookup_device",
    "async_setup_entry",
    "er",
    "format_ap_device_id",
    "format_ap_name",
    "format_radio_device_id",
    "format_radio_name",
    "normalize_band",
]
