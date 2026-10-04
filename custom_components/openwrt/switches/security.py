"""Firewall and access control (parental control) switches for OpenWrt."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, EntityCategory
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ..api.base import OpenWrtClient
from ..const import (
    CONF_SKIP_RANDOM_MAC,
    CONF_TRACK_DEVICES,
    CONF_TRACK_WIRED,
    DEFAULT_SKIP_RANDOM_MAC,
    DEFAULT_TRACK_DEVICES,
    DEFAULT_TRACK_WIRED,
    DOMAIN,
)
from ..coordinator import OpenWrtDataCoordinator
from ..helpers import (
    _get_router_device_id,
    is_random_mac,
    resolve_client_name,
)

_LOGGER = logging.getLogger(__name__)


class OpenWrtFirewallSwitch(CoordinatorEntity[OpenWrtDataCoordinator], SwitchEntity):
    """Switch to enable/disable a firewall port forward."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_device_class = SwitchDeviceClass.SWITCH

    def __init__(
        self,
        coordinator: OpenWrtDataCoordinator,
        entry: ConfigEntry,
        client: OpenWrtClient,
        section_id: str,
        name: str,
    ) -> None:
        """Initialize the firewall switch."""
        super().__init__(coordinator)
        self._client = client
        self._section_id = section_id
        self._attr_unique_id = f"{entry.entry_id}_firewall_{section_id}"
        self._attr_name = f"Port Forward: {name}"
        self._attr_translation_key = "firewall_port_forward"
        self._attr_translation_placeholders = {"name": name}
        if name.lower().startswith("allow"):
            self._attr_entity_registry_enabled_default = False
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.unique_id or entry.data[CONF_HOST])},
        }

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        if self.coordinator.data is None:
            return {}
        for redirect in self.coordinator.data.firewall_redirects:
            if redirect.section_id == self._section_id:
                return {
                    "external_port": redirect.external_port,
                    "target_ip": redirect.target_ip,
                    "target_port": redirect.target_port,
                    "protocol": redirect.protocol,
                }
        return {}

    @property
    def is_on(self) -> bool | None:
        """Return firewall redirect status."""
        if self.coordinator.data is None:
            return None
        for redirect in self.coordinator.data.firewall_redirects:
            if redirect.section_id == self._section_id:
                return redirect.enabled
        return None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable the firewall redirect."""
        try:
            await self._client.set_firewall_redirect_enabled(self._section_id, True)
            if self.coordinator.data:
                for redirect in self.coordinator.data.firewall_redirects:
                    if redirect.section_id == self._section_id:
                        redirect.enabled = True
            self.async_write_ha_state()
        except Exception as err:
            msg = f"Failed to enable firewall redirect {self._section_id}: {err}"
            raise HomeAssistantError(msg) from err
        self.coordinator.hass.async_create_task(
            self.coordinator.async_request_refresh()
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the firewall redirect."""
        try:
            await self._client.set_firewall_redirect_enabled(self._section_id, False)
            if self.coordinator.data:
                for redirect in self.coordinator.data.firewall_redirects:
                    if redirect.section_id == self._section_id:
                        redirect.enabled = False
            self.async_write_ha_state()
        except Exception as err:
            msg = f"Failed to disable firewall redirect {self._section_id}: {err}"
            raise HomeAssistantError(msg) from err
        self.coordinator.hass.async_create_task(
            self.coordinator.async_request_refresh()
        )


class OpenWrtFirewallRuleSwitch(
    CoordinatorEntity[OpenWrtDataCoordinator],
    SwitchEntity,
):
    """Switch to enable/disable a general firewall rule."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_device_class = SwitchDeviceClass.SWITCH

    def __init__(
        self,
        coordinator: OpenWrtDataCoordinator,
        entry: ConfigEntry,
        client: OpenWrtClient,
        section_id: str,
        name: str,
    ) -> None:
        """Initialize the firewall rule switch."""
        super().__init__(coordinator)
        self._client = client
        self._section_id = section_id
        self._attr_unique_id = f"{entry.entry_id}_firewall_rule_{section_id}"
        self._attr_name = f"Firewall Rule: {name}"
        self._attr_translation_key = "firewall_rule"
        self._attr_translation_placeholders = {"name": name}
        if name.lower().startswith("allow"):
            self._attr_entity_registry_enabled_default = False
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.unique_id or entry.data[CONF_HOST])},
        }

    @property
    def is_on(self) -> bool | None:
        """Return firewall rule status."""
        if self.coordinator.data is None:
            return None
        for rule in self.coordinator.data.firewall_rules:
            if rule.section_id == self._section_id:
                return rule.enabled
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        if self.coordinator.data is None:
            return {}
        for rule in self.coordinator.data.firewall_rules:
            if rule.section_id == self._section_id:
                return {
                    "target": rule.target,
                    "src": rule.src,
                    "dest": rule.dest,
                }
        return {}

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable the firewall rule."""
        try:
            success = await self._client.set_firewall_rule_enabled(
                self._section_id, True
            )
        except Exception as err:
            msg = f"Failed to enable firewall rule {self._section_id}: {err}"
            raise HomeAssistantError(msg) from err

        if not success:
            raise HomeAssistantError(
                f"Router rejected request to enable firewall rule {self._section_id} "
                "(no exception raised, but result was falsy)"
            )

        if self.coordinator.data:
            for rule in self.coordinator.data.firewall_rules:
                if rule.section_id == self._section_id:
                    rule.enabled = True
        self.async_write_ha_state()
        self.coordinator.hass.async_create_task(
            self.coordinator.async_request_refresh()
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the firewall rule."""
        try:
            success = await self._client.set_firewall_rule_enabled(
                self._section_id, False
            )
        except Exception as err:
            msg = f"Failed to disable firewall rule {self._section_id}: {err}"
            raise HomeAssistantError(msg) from err

        if not success:
            raise HomeAssistantError(
                f"Router rejected request to disable firewall rule {self._section_id} "
                "(no exception raised, but result was falsy)"
            )

        if self.coordinator.data:
            for rule in self.coordinator.data.firewall_rules:
                if rule.section_id == self._section_id:
                    rule.enabled = False
        self.async_write_ha_state()
        self.coordinator.hass.async_create_task(
            self.coordinator.async_request_refresh()
        )


def _add_firewall_switches(
    coordinator: OpenWrtDataCoordinator,
    entry: ConfigEntry,
    client: OpenWrtClient,
    entities: list[SwitchEntity],
    tracked_keys: set[str],
) -> None:
    """Add firewall switches."""
    for redirect in coordinator.data.firewall_redirects:
        if redirect.section_id:
            key = f"firewall_{redirect.section_id}"
            if key not in tracked_keys:
                tracked_keys.add(key)
                entities.append(
                    OpenWrtFirewallSwitch(
                        coordinator,
                        entry,
                        client,
                        redirect.section_id,
                        redirect.name,
                    ),
                )
    for rule in coordinator.data.firewall_rules:
        if rule.name and rule.section_id and not rule.name.startswith("cfg"):
            key = f"firewall_rule_{rule.section_id}"
            if key not in tracked_keys:
                tracked_keys.add(key)
                entities.append(
                    OpenWrtFirewallRuleSwitch(
                        coordinator,
                        entry,
                        client,
                        rule.section_id,
                        rule.name,
                    ),
                )


class OpenWrtAccessControlSwitch(
    CoordinatorEntity[OpenWrtDataCoordinator],
    SwitchEntity,
):
    """Switch to block/unblock internet access for a device (Parental Control)."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_device_class = SwitchDeviceClass.SWITCH
    _attr_entity_registry_enabled_default = False

    def __init__(
        self,
        coordinator: OpenWrtDataCoordinator,
        entry: ConfigEntry,
        client: OpenWrtClient,
        mac: str,
        name: str,
        section_id: str | None = None,
    ) -> None:
        """Initialize the access control switch."""
        super().__init__(coordinator)
        self._client = client
        self._mac = mac.lower()
        self._attr_unique_id = f"{entry.entry_id}_access_{self._mac.replace(':', '_')}"
        self._attr_translation_key = "device_access"

        if is_random_mac(self._mac):
            self._attr_entity_registry_enabled_default = False
        self._attr_device_info = DeviceInfo(
            connections={("mac", self._mac)},
            name=resolve_client_name(coordinator.hass, self._mac, name),
            via_device_id=_get_router_device_id(coordinator.hass, coordinator, entry),
        )

    @property
    def is_on(self) -> bool | None:
        """Return access status (On = Not Blocked)."""
        if self.coordinator.data is None:
            return None
        rule = next(
            (
                r
                for r in self.coordinator.data.access_control
                if r.mac and r.mac.lower() == self._mac
            ),
            None,
        )
        if not rule:
            return True
        return not rule.blocked

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Unblock the device (Allow access)."""
        try:
            success = await self._client.set_access_control_blocked(self._mac, False)
            if not success:
                msg = (
                    f"Router rejected the unblock request for {self._mac}; "
                    "check the Home Assistant logs (search for "
                    "'openwrt-access-control') for details."
                )
                raise HomeAssistantError(msg)
            if self.coordinator.data:
                for rule in self.coordinator.data.access_control:
                    if rule.mac and rule.mac.lower() == self._mac:
                        rule.blocked = False
            self.async_write_ha_state()
        except Exception as err:
            msg = f"Failed to unblock device {self._mac}: {err}"
            raise HomeAssistantError(msg) from err
        self.coordinator.hass.async_create_task(
            self.coordinator.async_request_refresh()
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Block the device (Restrict access)."""
        try:
            success = await self._client.set_access_control_blocked(self._mac, True)
            if not success:
                msg = (
                    f"Router rejected the block request for {self._mac}; "
                    "check the Home Assistant logs (search for "
                    "'openwrt-access-control') for details."
                )
                raise HomeAssistantError(msg)
            if self.coordinator.data:
                for rule in self.coordinator.data.access_control:
                    if rule.mac and rule.mac.lower() == self._mac:
                        rule.blocked = True
            self.async_write_ha_state()
        except Exception as err:
            msg = f"Failed to block device {self._mac}: {err}"
            raise HomeAssistantError(msg) from err
        self.coordinator.hass.async_create_task(
            self.coordinator.async_request_refresh()
        )


def _add_access_control_switches(
    coordinator: OpenWrtDataCoordinator,
    entry: ConfigEntry,
    client: OpenWrtClient,
    entities: list[SwitchEntity],
    tracked_keys: set[str],
) -> None:
    """Add access control switches."""
    router_hostname = (
        coordinator.data.device_info.hostname if coordinator.data.device_info else ""
    )
    track_devices = entry.options.get(
        CONF_TRACK_DEVICES,
        entry.data.get(CONF_TRACK_DEVICES, DEFAULT_TRACK_DEVICES),
    )
    if not track_devices:
        return

    track_wired = entry.options.get(
        CONF_TRACK_WIRED,
        entry.data.get(CONF_TRACK_WIRED, DEFAULT_TRACK_WIRED),
    )
    skip_random = entry.options.get(CONF_SKIP_RANDOM_MAC, DEFAULT_SKIP_RANDOM_MAC)

    for device in coordinator.data.connected_devices:
        if not device.mac:
            continue

        mac = device.mac.lower()
        if skip_random and is_random_mac(mac):
            continue

        if not track_wired and not device.is_wireless:
            continue

        key = f"access_{mac.replace(':', '_')}"
        if key not in tracked_keys:
            tracked_keys.add(key)
            dev_name = (
                device.hostname
                if device.hostname and device.hostname not in ("*", router_hostname)
                else device.mac
            )
            ac_rule = next(
                (
                    r
                    for r in coordinator.data.access_control
                    if r.mac and r.mac.lower() == device.mac.lower()
                ),
                None,
            )
            entities.append(
                OpenWrtAccessControlSwitch(
                    coordinator,
                    entry,
                    client,
                    device.mac.lower(),
                    dev_name,
                    ac_rule.section_id if ac_rule else None,
                ),
            )
