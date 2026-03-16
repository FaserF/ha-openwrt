"""Switch platform for OpenWrt integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api.base import OpenWrtClient
from .const import DATA_CLIENT, DATA_COORDINATOR, DOMAIN
from .coordinator import OpenWrtDataCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up OpenWrt switches."""
    coordinator: OpenWrtDataCoordinator = hass.data[DOMAIN][entry.entry_id][
        DATA_COORDINATOR
    ]
    client: OpenWrtClient = hass.data[DOMAIN][entry.entry_id][DATA_CLIENT]

    entities: list[SwitchEntity] = []

    if coordinator.data:
        perms = coordinator.data.permissions
        pkgs = coordinator.data.packages

        if perms.write_wireless:
            entities.append(OpenWrtWpsSwitch(coordinator, entry, client))
            for wifi in coordinator.data.wireless_interfaces:
                if wifi.name:
                    entities.append(
                        OpenWrtWirelessSwitch(
                            coordinator,
                            entry,
                            client,
                            wifi.name,
                            wifi.ssid,
                            wifi.frequency,
                        )
                    )

        if perms.write_services:
            for service in coordinator.data.services:
                if service.name:
                    entities.append(
                        OpenWrtServiceSwitch(coordinator, entry, client, service.name)
                    )

        if perms.write_firewall:
            for redirect in coordinator.data.firewall_redirects:
                if redirect.section_id:
                    entities.append(
                        OpenWrtFirewallSwitch(
                            coordinator,
                            entry,
                            client,
                            redirect.section_id,
                            redirect.name,
                        )
                    )

            for rule in coordinator.data.firewall_rules:
                # Only expose named rules to avoid clutter and potentially accidental system changes
                if rule.name and rule.section_id and not rule.name.startswith("cfg"):
                    entities.append(
                        OpenWrtFirewallRuleSwitch(
                            coordinator, entry, client, rule.section_id, rule.name
                        )
                    )

        if perms.write_access_control:
            for device in coordinator.data.connected_devices:
                if not device.mac:
                    continue

                # Determine initial device name and sanitize hostname
                dev_name = device.mac
                if device.hostname and device.hostname != "*":
                    router_hostname = ""
                    if coordinator.data.device_info:
                        router_hostname = coordinator.data.device_info.hostname

                    if device.hostname != router_hostname:
                        dev_name = device.hostname

                ac_rule_entry = next(
                    (r for r in coordinator.data.access_control if r.mac == device.mac),
                    None,
                )
                entities.append(
                    OpenWrtAccessControlSwitch(
                        coordinator,
                        entry,
                        client,
                        device.mac,
                        dev_name,
                        ac_rule_entry.section_id if ac_rule_entry else None,
                    )
                )

        if perms.write_sqm and pkgs.sqm_scripts is not False:
            for sqm in coordinator.data.sqm:
                if sqm.section_id:
                    entities.append(
                        OpenWrtSqmSwitch(
                            coordinator, entry, client, sqm.section_id, sqm.name
                        )
                    )

    async_add_entities(entities)


class OpenWrtWpsSwitch(CoordinatorEntity[OpenWrtDataCoordinator], SwitchEntity):
    """Switch to control WPS."""

    _attr_has_entity_name = True
    _attr_name = "WPS"
    _attr_translation_key = "wps"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_device_class = SwitchDeviceClass.SWITCH

    def __init__(
        self,
        coordinator: OpenWrtDataCoordinator,
        entry: ConfigEntry,
        client: OpenWrtClient,
    ) -> None:
        """Initialize the WPS switch."""
        super().__init__(coordinator)
        self._client = client
        self._attr_unique_id = f"{entry.entry_id}_wps"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.unique_id or entry.data[CONF_HOST])},
        }

    @property
    def is_on(self) -> bool | None:
        """Return WPS status."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.wps_status.enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable WPS."""
        try:
            await self._client.set_wps(True)
        except Exception as err:
            raise HomeAssistantError(f"Failed to enable WPS: {err}") from err
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable WPS."""
        try:
            await self._client.set_wps(False)
        except Exception as err:
            raise HomeAssistantError(f"Failed to disable WPS: {err}") from err
        await self.coordinator.async_request_refresh()


class OpenWrtWirelessSwitch(CoordinatorEntity[OpenWrtDataCoordinator], SwitchEntity):
    """Switch to enable/disable a wireless radio."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_device_class = SwitchDeviceClass.SWITCH

    def __init__(
        self,
        coordinator: OpenWrtDataCoordinator,
        entry: ConfigEntry,
        client: OpenWrtClient,
        iface_name: str,
        ssid: str,
        frequency: str = "",
    ) -> None:
        """Initialize the wireless switch."""
        super().__init__(coordinator)
        self._client = client
        self._iface_name = iface_name

        # Build a descriptive label: "SSID (Band)" or just "SSID" if frequency is missing
        band = ""
        if frequency:
            if frequency.startswith("2."):
                band = "2.4 GHz"
            elif frequency.startswith("5."):
                band = "5 GHz"
            elif frequency.startswith("6."):
                band = "6 GHz"
            else:
                band = frequency.replace(" GHz", "") + " GHz"

        label = ssid or iface_name
        name_label = f"{label} ({band})" if band else label

        self._attr_unique_id = f"{entry.entry_id}_wireless_{iface_name}"
        self._attr_name = f"Wireless {name_label}"
        self._attr_translation_key = "wireless_radio"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.unique_id}_ap_{iface_name}")},
            name=f"AP {name_label}",
            manufacturer="OpenWrt",
            model="Access Point",
            via_device=(DOMAIN, entry.unique_id),
        )

    @property
    def is_on(self) -> bool | None:
        """Return wireless interface status."""
        if self.coordinator.data is None:
            return None
        for wifi in self.coordinator.data.wireless_interfaces:
            if wifi.name == self._iface_name:
                return wifi.enabled
        return None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable the wireless interface."""
        try:
            await self._client.set_wireless_enabled(self._iface_name, True)
        except Exception as err:
            raise HomeAssistantError(
                f"Failed to enable wireless interface {self._iface_name}: {err}"
            ) from err
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the wireless interface."""
        try:
            await self._client.set_wireless_enabled(self._iface_name, False)
        except Exception as err:
            raise HomeAssistantError(
                f"Failed to disable wireless interface {self._iface_name}: {err}"
            ) from err
        await self.coordinator.async_request_refresh()


class OpenWrtServiceSwitch(CoordinatorEntity[OpenWrtDataCoordinator], SwitchEntity):
    """Switch to enable/disable a system service."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_device_class = SwitchDeviceClass.SWITCH
    _attr_entity_registry_enabled_default = False

    def __init__(
        self,
        coordinator: OpenWrtDataCoordinator,
        entry: ConfigEntry,
        client: OpenWrtClient,
        service_name: str,
    ) -> None:
        """Initialize the service switch."""
        super().__init__(coordinator)
        self._client = client
        self._service_name = service_name
        self._attr_unique_id = f"{entry.entry_id}_service_{service_name}"
        self._attr_name = f"Service {service_name}"
        self._attr_translation_key = "service_toggle"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.unique_id or entry.data[CONF_HOST])},
        }

    @property
    def is_on(self) -> bool | None:
        """Return service running status."""
        if self.coordinator.data is None:
            return None
        for service in self.coordinator.data.services:
            if service.name == self._service_name:
                return service.running
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        if self.coordinator.data is None:
            return {}
        for service in self.coordinator.data.services:
            if service.name == self._service_name:
                return {"enabled_at_boot": service.enabled}
        return {}

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Start the service."""
        try:
            await self._client.manage_service(self._service_name, "start")
        except Exception as err:
            raise HomeAssistantError(
                f"Failed to start service {self._service_name}: {err}"
            ) from err
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stop the service."""
        try:
            await self._client.manage_service(self._service_name, "stop")
        except Exception as err:
            raise HomeAssistantError(
                f"Failed to stop service {self._service_name}: {err}"
            ) from err
        await self.coordinator.async_request_refresh()


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
        self._attr_name = f"Port Forward {name}"
        self._attr_translation_key = "firewall_port_forward"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.unique_id or entry.data[CONF_HOST])},
        }

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
        """Enable the port forward."""
        try:
            await self._client.set_firewall_redirect_enabled(self._section_id, True)
        except Exception as err:
            raise HomeAssistantError(f"Failed to enable port forward: {err}") from err
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the port forward."""
        try:
            await self._client.set_firewall_redirect_enabled(self._section_id, False)
        except Exception as err:
            raise HomeAssistantError(f"Failed to disable port forward: {err}") from err
        await self.coordinator.async_request_refresh()


class OpenWrtAccessControlSwitch(
    CoordinatorEntity[OpenWrtDataCoordinator], SwitchEntity
):
    """Switch to block/unblock internet access for a device (Parental Control)."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_device_class = SwitchDeviceClass.SWITCH

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
        self._mac = mac
        self._attr_unique_id = f"{entry.entry_id}_access_{mac.replace(':', '_')}"
        self._attr_name = f"Internet Access {name}"
        self._attr_translation_key = "device_access"
        self._attr_device_info = {
            "connections": {("mac", mac)},
            "name": name,
            "via_device": (DOMAIN, entry.unique_id),
        }

    @property
    def is_on(self) -> bool | None:
        """Return access status (On = Not Blocked)."""
        if self.coordinator.data is None:
            return None
        rule = next(
            (r for r in self.coordinator.data.access_control if r.mac == self._mac),
            None,
        )
        if not rule:
            return True
        return not rule.blocked

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Unblock the device (Allow access)."""
        try:
            await self._client.set_access_control_blocked(self._mac, False)
        except Exception as err:
            raise HomeAssistantError(
                f"Failed to unblock device {self._mac}: {err}"
            ) from err
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Block the device (Restrict access)."""
        try:
            await self._client.set_access_control_blocked(self._mac, True)
        except Exception as err:
            raise HomeAssistantError(
                f"Failed to block device {self._mac}: {err}"
            ) from err
        await self.coordinator.async_request_refresh()


class OpenWrtFirewallRuleSwitch(
    CoordinatorEntity[OpenWrtDataCoordinator], SwitchEntity
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
        self._attr_name = f"Firewall Rule {name}"
        self._attr_translation_key = "firewall_rule"
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
            await self._client.set_firewall_rule_enabled(self._section_id, True)
        except Exception as err:
            raise HomeAssistantError(f"Failed to enable firewall rule: {err}") from err
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the firewall rule."""
        try:
            await self._client.set_firewall_rule_enabled(self._section_id, False)
        except Exception as err:
            raise HomeAssistantError(f"Failed to disable firewall rule: {err}") from err
        await self.coordinator.async_request_refresh()


class OpenWrtSqmSwitch(CoordinatorEntity[OpenWrtDataCoordinator], SwitchEntity):
    """Switch to enable/disable SQM."""

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
        """Initialize the SQM switch."""
        super().__init__(coordinator)
        self._client = client
        self._section_id = section_id
        self._attr_unique_id = f"{entry.entry_id}_sqm_{section_id}"
        self._attr_name = f"SQM {name}"
        self._attr_translation_key = "sqm_enabled"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.unique_id or entry.data[CONF_HOST])},
        }

    @property
    def is_on(self) -> bool | None:
        """Return SQM enabled status."""
        if self.coordinator.data is None:
            return None
        for sqm in self.coordinator.data.sqm:
            if sqm.section_id == self._section_id:
                return sqm.enabled
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        if self.coordinator.data is None:
            return {}
        for sqm in self.coordinator.data.sqm:
            if sqm.section_id == self._section_id:
                return {
                    "interface": sqm.interface,
                    "download_limit": sqm.download,
                    "upload_limit": sqm.upload,
                    "qdisc": sqm.qdisc,
                    "script": sqm.script,
                }
        return {}

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable SQM."""
        try:
            await self._client.set_sqm_config(self._section_id, enabled=True)
        except Exception as err:
            raise HomeAssistantError(f"Failed to enable SQM: {err}") from err
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable SQM."""
        try:
            await self._client.set_sqm_config(self._section_id, enabled=False)
        except Exception as err:
            raise HomeAssistantError(f"Failed to disable SQM: {err}") from err
        await self.coordinator.async_request_refresh()
