"""Switch platform for OpenWrt integration."""

from __future__ import annotations

import logging
from typing import Any, cast

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

from .api.base import OpenWrtClient
from .const import (
    CONF_TRACK_DEVICES,
    CONF_TRACK_WIRED,
    DATA_CLIENT,
    DATA_COORDINATOR,
    DEFAULT_TRACK_DEVICES,
    DEFAULT_TRACK_WIRED,
    DOMAIN,
)
from .coordinator import OpenWrtDataCoordinator
from .helpers import format_ap_device_id, format_ap_name

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
            _add_wireless_switches(coordinator, entry, client, entities)

        if perms.write_services:
            _add_service_switches(coordinator, entry, client, entities)

        if perms.write_firewall:
            _add_firewall_switches(coordinator, entry, client, entities)

        if perms.write_access_control:
            _add_access_control_switches(coordinator, entry, client, entities)

        if perms.write_sqm and pkgs.sqm_scripts is not False:
            _add_sqm_switches(coordinator, entry, client, entities)

        if perms.write_vpn:
            _add_vpn_switches(coordinator, entry, client, entities)

        if perms.write_led:
            _add_led_switches(coordinator, entry, client, entities)

        _add_package_switches(coordinator, entry, client, entities, pkgs)

    async_add_entities(entities)

    @callback
    def _async_cleanup_entities() -> None:
        """Clean up orphaned or incorrect entities."""
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
            # Cleanup access control switches if settings changed
            if "_access_control_" in unique_id:
                if not track_devices:
                    ent_reg.async_remove(ent.entity_id)
                    continue

                mac = unique_id.split("_access_control_")[-1].lower()
                if not track_wired and mac in coordinator._device_history:
                    if not coordinator._device_history[mac].get("is_wireless"):
                        ent_reg.async_remove(ent.entity_id)
                        continue

    hass.add_job(_async_cleanup_entities)


def _add_vpn_switches(
    coordinator: OpenWrtDataCoordinator,
    entry: ConfigEntry,
    client: OpenWrtClient,
    entities: list[SwitchEntity],
) -> None:
    """Add VPN related switches."""
    if not coordinator.data:
        return

    for vpn in coordinator.data.wireguard_interfaces:
        entities.append(OpenWrtWireGuardSwitch(coordinator, entry, client, vpn.name))


def _add_led_switches(
    coordinator: OpenWrtDataCoordinator,
    entry: ConfigEntry,
    client: OpenWrtClient,
    entities: list[SwitchEntity],
) -> None:
    """Add LED switches."""
    if not coordinator.data:
        return

    for led in coordinator.data.leds:
        entities.append(OpenWrtLedSwitch(coordinator, entry, client, led.name))


class OpenWrtWireGuardSwitch(CoordinatorEntity[OpenWrtDataCoordinator], SwitchEntity):
    """Switch to enable/disable a WireGuard interface."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_device_class = SwitchDeviceClass.SWITCH
    _attr_entity_registry_enabled_default = False
    _attr_icon = "mdi:vpn"

    def __init__(
        self,
        coordinator: OpenWrtDataCoordinator,
        entry: ConfigEntry,
        client: OpenWrtClient,
        iface_name: str,
    ) -> None:
        """Initialize the WireGuard switch."""
        super().__init__(coordinator)
        self._client = client
        self._iface_name = iface_name
        self._attr_unique_id = f"{entry.entry_id}_wg_switch_{iface_name}"
        self._attr_name = f"WireGuard: {iface_name}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.unique_id or entry.data[CONF_HOST])},
        }

    @property
    def is_on(self) -> bool | None:
        """Return WireGuard interface status."""
        if not self.coordinator.data:
            return None
        for wg in self.coordinator.data.wireguard_interfaces:
            if wg.name == self._iface_name:
                return wg.enabled
        return None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable the WireGuard interface."""
        try:
            # We use ifup to bring up the interface
            await self._client.execute_command(f"ifup {self._iface_name}")
        except Exception as err:
            msg = f"Failed to enable WireGuard interface {self._iface_name}: {err}"
            raise HomeAssistantError(msg) from err
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the WireGuard interface."""
        try:
            # We use ifdown to bring down the interface
            await self._client.execute_command(f"ifdown {self._iface_name}")
        except Exception as err:
            msg = f"Failed to disable WireGuard interface {self._iface_name}: {err}"
            raise HomeAssistantError(msg) from err
        await self.coordinator.async_request_refresh()


def _add_wireless_switches(
    coordinator: OpenWrtDataCoordinator,
    entry: ConfigEntry,
    client: OpenWrtClient,
    entities: list[SwitchEntity],
) -> None:
    """Add wireless-related switches."""
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
                    wifi.section,
                ),
            )


SERVICE_ICONS = {
    "pbr": "mdi:router-network",
    "adguardhome": "mdi:shield-check",
    "unbound": "mdi:dns",
    "stubby": "mdi:dns-lock",
    "sqm": "mdi:speedometer",
    "wireguard": "mdi:vpn",
    "openvpn": "mdi:vpn",
    "miniupnpd": "mdi:folder-network",
}


def _add_service_switches(
    coordinator: OpenWrtDataCoordinator,
    entry: ConfigEntry,
    client: OpenWrtClient,
    entities: list[SwitchEntity],
) -> None:
    """Add switches for system services."""
    for service in coordinator.data.services:
        if service.name:
            icon = SERVICE_ICONS.get(service.name)
            entities.append(
                OpenWrtServiceSwitch(
                    coordinator, entry, client, service.name, icon=icon
                )
            )


def _add_firewall_switches(
    coordinator: OpenWrtDataCoordinator,
    entry: ConfigEntry,
    client: OpenWrtClient,
    entities: list[SwitchEntity],
) -> None:
    """Add firewall-related switches (redirects and rules)."""
    for redirect in coordinator.data.firewall_redirects:
        if redirect.section_id:
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
            entities.append(
                OpenWrtFirewallRuleSwitch(
                    coordinator,
                    entry,
                    client,
                    rule.section_id,
                    rule.name,
                ),
            )


def _add_access_control_switches(
    coordinator: OpenWrtDataCoordinator,
    entry: ConfigEntry,
    client: OpenWrtClient,
    entities: list[SwitchEntity],
) -> None:
    """Add access control (blocking) switches for devices."""
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

    for device in coordinator.data.connected_devices:
        if not device.mac:
            continue
        if not track_wired and not device.is_wireless:
            continue
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


def _add_sqm_switches(
    coordinator: OpenWrtDataCoordinator,
    entry: ConfigEntry,
    client: OpenWrtClient,
    entities: list[SwitchEntity],
) -> None:
    """Add SQM QoS switches."""
    for sqm in coordinator.data.sqm:
        if sqm.section_id:
            entities.append(
                OpenWrtSqmSwitch(coordinator, entry, client, sqm.section_id, sqm.name)
            )


def _add_package_switches(
    coordinator: OpenWrtDataCoordinator,
    entry: ConfigEntry,
    client: OpenWrtClient,
    entities: list[SwitchEntity],
    pkgs: Any,
) -> None:
    """Add package-specific toggle switches."""
    if pkgs.adblock:
        entities.append(OpenWrtAdBlockSwitch(coordinator, entry, client))
    if pkgs.simple_adblock:
        entities.append(OpenWrtSimpleAdBlockSwitch(coordinator, entry, client))
    if pkgs.ban_ip:
        entities.append(OpenWrtBanIpSwitch(coordinator, entry, client))


class OpenWrtAdBlockSwitch(CoordinatorEntity[OpenWrtDataCoordinator], SwitchEntity):
    """Switch to enable/disable AdBlock."""

    _attr_has_entity_name = True
    _attr_name = "AdBlock"
    _attr_translation_key = "adblock"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_device_class = SwitchDeviceClass.SWITCH

    def __init__(
        self,
        coordinator: OpenWrtDataCoordinator,
        entry: ConfigEntry,
        client: OpenWrtClient,
    ) -> None:
        """Initialize the adblock switch."""
        super().__init__(coordinator)
        self._client = client
        self._attr_unique_id = f"{entry.entry_id}_adblock"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.unique_id or entry.data[CONF_HOST])},
        }

    @property
    def is_on(self) -> bool | None:
        """Return adblock status."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.adblock.enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable AdBlock."""
        try:
            await self._client.set_adblock_enabled(True)
        except Exception as err:
            msg = f"Failed to enable AdBlock: {err}"
            raise HomeAssistantError(msg) from err
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable AdBlock."""
        try:
            await self._client.set_adblock_enabled(False)
        except Exception as err:
            msg = f"Failed to disable AdBlock: {err}"
            raise HomeAssistantError(msg) from err
        await self.coordinator.async_request_refresh()


class OpenWrtSimpleAdBlockSwitch(
    CoordinatorEntity[OpenWrtDataCoordinator],
    SwitchEntity,
):
    """Switch to enable/disable Simple AdBlock."""

    _attr_has_entity_name = True
    _attr_name = "Simple AdBlock"
    _attr_translation_key = "simple_adblock"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_device_class = SwitchDeviceClass.SWITCH

    def __init__(
        self,
        coordinator: OpenWrtDataCoordinator,
        entry: ConfigEntry,
        client: OpenWrtClient,
    ) -> None:
        """Initialize the simple-adblock switch."""
        super().__init__(coordinator)
        self._client = client
        self._attr_unique_id = f"{entry.entry_id}_simple_adblock"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.unique_id or entry.data[CONF_HOST])},
        }

    @property
    def is_on(self) -> bool | None:
        """Return simple-adblock status."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.simple_adblock.enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable Simple AdBlock."""
        try:
            await self._client.set_simple_adblock_enabled(True)
        except Exception as err:
            msg = f"Failed to enable Simple AdBlock: {err}"
            raise HomeAssistantError(msg) from err
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable Simple AdBlock."""
        try:
            await self._client.set_simple_adblock_enabled(False)
        except Exception as err:
            msg = f"Failed to disable Simple AdBlock: {err}"
            raise HomeAssistantError(
                msg,
            ) from err
        await self.coordinator.async_request_refresh()


class OpenWrtBanIpSwitch(CoordinatorEntity[OpenWrtDataCoordinator], SwitchEntity):
    """Switch to enable/disable Ban-IP."""

    _attr_has_entity_name = True
    _attr_name = "Ban-IP"
    _attr_translation_key = "banip"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_device_class = SwitchDeviceClass.SWITCH

    def __init__(
        self,
        coordinator: OpenWrtDataCoordinator,
        entry: ConfigEntry,
        client: OpenWrtClient,
    ) -> None:
        """Initialize the ban-ip switch."""
        super().__init__(coordinator)
        self._client = client
        self._attr_unique_id = f"{entry.entry_id}_banip"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.unique_id or entry.data[CONF_HOST])},
        }

    @property
    def is_on(self) -> bool | None:
        """Return ban-ip status."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.ban_ip.enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable Ban-IP."""
        try:
            await self._client.set_banip_enabled(True)
        except Exception as err:
            msg = f"Failed to enable Ban-IP: {err}"
            raise HomeAssistantError(msg) from err
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable Ban-IP."""
        try:
            await self._client.set_banip_enabled(False)
        except Exception as err:
            msg = f"Failed to disable Ban-IP: {err}"
            raise HomeAssistantError(msg) from err
        await self.coordinator.async_request_refresh()


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
            msg = f"Failed to enable WPS: {err}"
            raise HomeAssistantError(msg) from err
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable WPS."""
        try:
            await self._client.set_wps(False)
        except Exception as err:
            msg = f"Failed to disable WPS: {err}"
            raise HomeAssistantError(msg) from err
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
        section_id: str | None = None,
    ) -> None:
        """Initialize the wireless switch."""
        super().__init__(coordinator)
        self._client = client
        self._iface_name = iface_name

        # Build descriptive labels
        self._attr_unique_id = f"{entry.entry_id}_wireless_{iface_name}"
        self._attr_translation_key = "wireless_radio"

        # Calculate band for placeholders
        band = ""
        if frequency:
            freq_str = str(frequency).lower()
            if "2.4" in freq_str or (
                freq_str.replace(".", "").isdigit() and 2000 <= float(freq_str) <= 3000
            ):
                band = "2.4 GHz"
            elif "5" in freq_str or (
                freq_str.replace(".", "").isdigit() and 4900 <= float(freq_str) <= 5900
            ):
                band = "5 GHz"
            elif "6" in freq_str or (
                freq_str.replace(".", "").isdigit() and 5900 < float(freq_str) <= 7200
            ):
                band = "6 GHz"

        self._attr_translation_placeholders = {
            "ssid": ssid or iface_name,
            "band": band,
        }
        # Use section ID as stable identifier if available
        stable_id = section_id if section_id else iface_name
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, format_ap_device_id(entry, stable_id))},
            name=format_ap_name(ssid or iface_name, frequency),
            manufacturer="OpenWrt",
            model="Access Point",
            via_device=(DOMAIN, cast(str, entry.unique_id)),
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
            msg = f"Failed to enable wireless interface {self._iface_name}: {err}"
            raise HomeAssistantError(
                msg,
            ) from err
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the wireless interface."""
        try:
            await self._client.set_wireless_enabled(self._iface_name, False)
        except Exception as err:
            msg = f"Failed to disable wireless interface {self._iface_name}: {err}"
            raise HomeAssistantError(
                msg,
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
        name: str | None = None,
        icon: str | None = None,
    ) -> None:
        """Initialize the service switch."""
        super().__init__(coordinator)
        self._client = client
        self._service_name = service_name
        self._attr_unique_id = f"{entry.entry_id}_service_{service_name}"
        self._attr_name = name or service_name
        if icon:
            self._attr_icon = icon
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
            msg = f"Failed to start service {self._service_name}: {err}"
            raise HomeAssistantError(
                msg,
            ) from err
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stop the service."""
        try:
            await self._client.manage_service(self._service_name, "stop")
        except Exception as err:
            msg = f"Failed to stop service {self._service_name}: {err}"
            raise HomeAssistantError(
                msg,
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
        self._attr_name = f"Port Forward: {name}"
        self._attr_translation_key = "firewall_port_forward"
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
        """Enable the port forward."""
        try:
            await self._client.set_firewall_redirect_enabled(self._section_id, True)
        except Exception as err:
            msg = f"Failed to enable port forward: {err}"
            raise HomeAssistantError(msg) from err
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the port forward."""
        try:
            await self._client.set_firewall_redirect_enabled(self._section_id, False)
        except Exception as err:
            msg = f"Failed to disable port forward: {err}"
            raise HomeAssistantError(msg) from err
        await self.coordinator.async_request_refresh()


class OpenWrtAccessControlSwitch(
    CoordinatorEntity[OpenWrtDataCoordinator],
    SwitchEntity,
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
        self._mac = mac.lower()
        self._attr_unique_id = f"{entry.entry_id}_access_{self._mac.replace(':', '_')}"
        self._attr_name = "Internet Access"
        self._attr_translation_key = "device_access"
        self._attr_device_info = DeviceInfo(
            connections={("mac", self._mac)},
            name=name,
            via_device=(DOMAIN, cast(str, entry.unique_id)),
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
            await self._client.set_access_control_blocked(self._mac, False)
        except Exception as err:
            msg = f"Failed to unblock device {self._mac}: {err}"
            raise HomeAssistantError(
                msg,
            ) from err
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Block the device (Restrict access)."""
        try:
            await self._client.set_access_control_blocked(self._mac, True)
        except Exception as err:
            msg = f"Failed to block device {self._mac}: {err}"
            raise HomeAssistantError(
                msg,
            ) from err
        await self.coordinator.async_request_refresh()


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
            await self._client.set_firewall_rule_enabled(self._section_id, True)
        except Exception as err:
            msg = f"Failed to enable firewall rule: {err}"
            raise HomeAssistantError(msg) from err
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the firewall rule."""
        try:
            await self._client.set_firewall_rule_enabled(self._section_id, False)
        except Exception as err:
            msg = f"Failed to disable firewall rule: {err}"
            raise HomeAssistantError(msg) from err
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
        self._attr_name = name
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
            msg = f"Failed to enable SQM: {err}"
            raise HomeAssistantError(msg) from err
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable SQM."""
        try:
            await self._client.set_sqm_config(self._section_id, enabled=False)
        except Exception as err:
            msg = f"Failed to disable SQM: {err}"
            raise HomeAssistantError(msg) from err
        await self.coordinator.async_request_refresh()


class OpenWrtLedSwitch(CoordinatorEntity[OpenWrtDataCoordinator], SwitchEntity):
    """Switch to enable/disable an LED."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_device_class = SwitchDeviceClass.SWITCH
    _attr_entity_registry_enabled_default = False
    _attr_icon = "mdi:led-on"

    def __init__(
        self,
        coordinator: OpenWrtDataCoordinator,
        entry: ConfigEntry,
        client: OpenWrtClient,
        name: str,
    ) -> None:
        """Initialize the switch."""
        super().__init__(coordinator)
        self._client = client
        self._name = name
        self._attr_name = f"LED {name.replace('_', ' ').replace('-', ' ').title()}"
        self._attr_unique_id = f"{entry.entry_id}_led_{name}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, cast(str, entry.unique_id or entry.data[CONF_HOST]))},
        )

    @property
    def is_on(self) -> bool:
        """Return true if LED is on."""
        if not self.coordinator.data:
            return False
        for led in self.coordinator.data.leds:
            if led.name == self._name:
                return led.active
        return False

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the LED on."""
        try:
            await self._client.set_led(self._name, True)
            await self.coordinator.async_request_refresh()
        except Exception as err:
            raise HomeAssistantError(
                f"Failed to turn on LED {self._name}: {err}"
            ) from err

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the LED off."""
        try:
            await self._client.set_led(self._name, False)
            await self.coordinator.async_request_refresh()
        except Exception as err:
            raise HomeAssistantError(
                f"Failed to turn off LED {self._name}: {err}"
            ) from err
