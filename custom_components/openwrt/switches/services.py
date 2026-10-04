"""Service and package switches for OpenWrt (system services, adblock, banip, SQM)."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, EntityCategory
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ..api.base import OpenWrtClient, ServiceInfo
from ..const import DOMAIN
from ..coordinator import OpenWrtDataCoordinator

_LOGGER = logging.getLogger(__name__)

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
    def _service(self) -> ServiceInfo | None:
        """Return the coordinator entry for this service."""
        if self.coordinator.data is None:
            return None
        for service in self.coordinator.data.services:
            if service.name == self._service_name:
                return service
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        service = self._service
        if service is None:
            return {}
        return {"enabled_at_boot": service.enabled, "one_shot": service.one_shot}

    async def _async_manage(self, action: str) -> None:
        """Run one service action, treating a False result as a failure."""
        try:
            ok = await self._client.manage_service(self._service_name, action)
        except Exception as err:
            msg = f"Failed to {action} service {self._service_name}: {err}"
            raise HomeAssistantError(msg) from err
        if not ok:
            msg = f"Failed to {action} service {self._service_name}"
            raise HomeAssistantError(msg)

    async def _async_set_service(self, turn_on: bool) -> None:
        """Apply the requested state, honouring one-shot semantics."""
        service = self._service
        action = "start" if turn_on else "stop"

        if service is not None and service.one_shot:
            await self._async_manage("enable" if turn_on else "disable")

        await self._async_manage(action)

        if service is not None:
            service.running = turn_on
            if service.one_shot:
                service.enabled = turn_on
        self.async_write_ha_state()
        self.coordinator.hass.async_create_task(
            self.coordinator.async_request_refresh()
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Start the service."""
        await self._async_set_service(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stop the service."""
        await self._async_set_service(False)


def _add_service_switches(
    coordinator: OpenWrtDataCoordinator,
    entry: ConfigEntry,
    client: OpenWrtClient,
    entities: list[SwitchEntity],
    tracked_keys: set[str],
) -> None:
    """Add service switches."""
    for service in coordinator.data.services:
        if service.name:
            key = f"service_{service.name}"
            if key not in tracked_keys:
                tracked_keys.add(key)
                icon = SERVICE_ICONS.get(service.name)
                entities.append(
                    OpenWrtServiceSwitch(
                        coordinator, entry, client, service.name, icon=icon
                    )
                )


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
        """Initialize."""
        super().__init__(coordinator)
        self._client = client
        self._attr_unique_id = f"{entry.entry_id}_adblock"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.unique_id or entry.data[CONF_HOST])},
        }

    @property
    def is_on(self) -> bool | None:
        """Return status."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.adblock.enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable."""
        try:
            await self._client.set_adblock_enabled(True)
            if self.coordinator.data:
                self.coordinator.data.adblock.enabled = True
            self.async_write_ha_state()
        except Exception as err:
            msg = f"Failed to enable AdBlock: {err}"
            raise HomeAssistantError(msg) from err
        self.coordinator.hass.async_create_task(
            self.coordinator.async_request_refresh()
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable."""
        try:
            await self._client.set_adblock_enabled(False)
            if self.coordinator.data:
                self.coordinator.data.adblock.enabled = False
            self.async_write_ha_state()
        except Exception as err:
            msg = f"Failed to disable AdBlock: {err}"
            raise HomeAssistantError(msg) from err
        self.coordinator.hass.async_create_task(
            self.coordinator.async_request_refresh()
        )


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
            if self.coordinator.data:
                self.coordinator.data.simple_adblock.enabled = True
            self.async_write_ha_state()
        except Exception as err:
            msg = f"Failed to enable Simple AdBlock: {err}"
            raise HomeAssistantError(msg) from err
        self.coordinator.hass.async_create_task(
            self.coordinator.async_request_refresh()
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable Simple AdBlock."""
        try:
            await self._client.set_simple_adblock_enabled(False)
            if self.coordinator.data:
                self.coordinator.data.simple_adblock.enabled = False
            self.async_write_ha_state()
        except Exception as err:
            msg = f"Failed to disable Simple AdBlock: {err}"
            raise HomeAssistantError(msg) from err
        self.coordinator.hass.async_create_task(
            self.coordinator.async_request_refresh()
        )


class OpenWrtBanIpSwitch(CoordinatorEntity[OpenWrtDataCoordinator], SwitchEntity):
    """Switch to enable/disable Ban-IP."""

    _attr_has_entity_name = True
    _attr_name = "Ban-IP"
    _attr_translation_key = "banip"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_device_class = SwitchDeviceClass.SWITCH
    _attr_entity_registry_enabled_default = False

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
            if self.coordinator.data:
                self.coordinator.data.ban_ip.enabled = True
            self.async_write_ha_state()
        except Exception as err:
            msg = f"Failed to enable Ban-IP: {err}"
            raise HomeAssistantError(msg) from err
        self.coordinator.hass.async_create_task(
            self.coordinator.async_request_refresh()
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable Ban-IP."""
        try:
            await self._client.set_banip_enabled(False)
            if self.coordinator.data:
                self.coordinator.data.ban_ip.enabled = False
            self.async_write_ha_state()
        except Exception as err:
            msg = f"Failed to disable Ban-IP: {err}"
            raise HomeAssistantError(msg) from err
        self.coordinator.hass.async_create_task(
            self.coordinator.async_request_refresh()
        )


def _add_package_switches(
    coordinator: OpenWrtDataCoordinator,
    entry: ConfigEntry,
    client: OpenWrtClient,
    entities: list[SwitchEntity],
    tracked_keys: set[str],
    pkgs: Any,
) -> None:
    """Add package switches."""
    if pkgs.adblock and "adblock" not in tracked_keys:
        tracked_keys.add("adblock")
        entities.append(OpenWrtAdBlockSwitch(coordinator, entry, client))
    if pkgs.simple_adblock and "simple_adblock" not in tracked_keys:
        tracked_keys.add("simple_adblock")
        entities.append(OpenWrtSimpleAdBlockSwitch(coordinator, entry, client))
    if pkgs.ban_ip and "banip" not in tracked_keys:
        tracked_keys.add("banip")
        entities.append(OpenWrtBanIpSwitch(coordinator, entry, client))


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
            if self.coordinator.data:
                for sqm in self.coordinator.data.sqm:
                    if sqm.section_id == self._section_id:
                        sqm.enabled = True
            self.async_write_ha_state()
        except Exception as err:
            raise HomeAssistantError(f"Failed to manage SQM: {err}") from err
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable SQM."""
        try:
            await self._client.set_sqm_config(self._section_id, enabled=False)
            if self.coordinator.data:
                for sqm in self.coordinator.data.sqm:
                    if sqm.section_id == self._section_id:
                        sqm.enabled = False
            self.async_write_ha_state()
        except Exception as err:
            raise HomeAssistantError(f"Failed to manage SQM: {err}") from err
        await self.coordinator.async_request_refresh()


def _add_sqm_switches(
    coordinator: OpenWrtDataCoordinator,
    entry: ConfigEntry,
    client: OpenWrtClient,
    entities: list[SwitchEntity],
    tracked_keys: set[str],
) -> None:
    """Add SQM switches."""
    for sqm in coordinator.data.sqm:
        if sqm.section_id:
            key = f"sqm_{sqm.section_id}"
            if key not in tracked_keys:
                tracked_keys.add(key)
                entities.append(
                    OpenWrtSqmSwitch(
                        coordinator, entry, client, sqm.section_id, sqm.name
                    )
                )
