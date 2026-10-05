"""Wireless radio, WPS, and WiFi interface switches for OpenWrt."""

from __future__ import annotations

import logging
import sys
from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, EntityCategory
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ..api.base import OpenWrtClient
from ..const import DOMAIN
from ..coordinator import OpenWrtDataCoordinator
from ..helpers import (
    _get_router_device_id,
    _lookup_device,
    format_ap_device_id,
    format_ap_name,
    format_radio_device_id,
    format_radio_name,
    normalize_band,
)

_LOGGER = logging.getLogger(__name__)


def _get_device_info(*args: Any, **kwargs: Any) -> Any:
    mod = sys.modules.get("custom_components.openwrt.switch")
    cls = getattr(mod, "DeviceInfo", DeviceInfo)
    return cls(*args, **kwargs)


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
            if self.coordinator.data:
                self.coordinator.data.wps_status.enabled = True
            self.async_write_ha_state()
        except Exception as err:
            msg = f"Failed to enable WPS: {err}"
            raise HomeAssistantError(msg) from err
        self.coordinator.hass.async_create_task(
            self.coordinator.async_request_refresh()
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable WPS."""
        try:
            await self._client.set_wps(False)
            if self.coordinator.data:
                self.coordinator.data.wps_status.enabled = False
            self.async_write_ha_state()
        except Exception as err:
            msg = f"Failed to disable WPS: {err}"
            raise HomeAssistantError(msg) from err
        self.coordinator.hass.async_create_task(
            self.coordinator.async_request_refresh()
        )


class OpenWrtRadioSwitch(CoordinatorEntity[OpenWrtDataCoordinator], SwitchEntity):
    """Switch to physically enable or disable a wireless radio."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_device_class = SwitchDeviceClass.SWITCH

    def __init__(
        self,
        coordinator: OpenWrtDataCoordinator,
        entry: ConfigEntry,
        client: OpenWrtClient,
        radio: str,
        band: str,
    ) -> None:
        """Initialize the physical radio switch."""
        super().__init__(coordinator)
        self._client = client
        self._radio = radio
        label = format_radio_name(radio, band)
        self._attr_name = "Physical radio"
        self._attr_unique_id = f"{entry.entry_id}_radio_{radio}"
        self._attr_device_info = _get_device_info(
            identifiers={
                (DOMAIN, format_radio_device_id(coordinator.router_id, radio))
            },
            name=label,
            manufacturer="OpenWrt",
            model="Wireless Radio",
            via_device_id=_get_router_device_id(coordinator.hass, coordinator, entry),
        )

    @property
    def is_on(self) -> bool | None:
        """Return the configured physical radio state."""
        if self.coordinator.data is None:
            return None
        for wifi in self.coordinator.data.wireless_interfaces:
            if wifi.radio == self._radio:
                return wifi.radio_enabled
        return None

    async def _async_set_enabled(self, enabled: bool) -> None:
        """Set the configured physical radio state."""
        try:
            if not await self._client.set_radio_enabled(self._radio, enabled):
                msg = f"OpenWrt rejected radio state change for {self._radio}"
                raise HomeAssistantError(msg)
            if self.coordinator.data:
                for wifi in self.coordinator.data.wireless_interfaces:
                    if wifi.radio == self._radio:
                        wifi.radio_enabled = enabled
                        wifi.enabled = enabled and wifi.interface_enabled
            self.async_write_ha_state()
        except HomeAssistantError:
            raise
        except Exception as err:
            msg = f"Failed to change physical radio {self._radio}: {err}"
            raise HomeAssistantError(msg) from err
        self.coordinator.hass.async_create_task(
            self.coordinator.async_request_refresh()
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Physically enable the radio."""
        await self._async_set_enabled(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Physically disable the radio."""
        await self._async_set_enabled(False)


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
        radio: str = "",
    ) -> None:
        """Initialize the wireless switch."""
        super().__init__(coordinator)
        self._client = client
        self._iface_name = iface_name
        self._ssid = ssid
        self._section_id = section_id
        self._radio = radio

        self._attr_unique_id = f"{entry.entry_id}_wireless_{section_id or iface_name}"
        self._attr_translation_key = "wireless_radio"

        band = normalize_band(frequency) if frequency else ""

        self._attr_translation_placeholders = {
            "ssid": ssid or iface_name,
            "band": band,
        }
        self._attr_name = f"SSID {ssid or iface_name}"
        stable_id = coordinator.interface_to_stable_id.get(
            iface_name, section_id if section_id else iface_name
        )

        name_label = format_ap_name(ssid or iface_name, frequency)

        via_device_id: str | None = None
        if radio:
            dev_reg = dr.async_get(coordinator.hass)
            radio_dev = _lookup_device(
                dev_reg,
                (DOMAIN, format_radio_device_id(coordinator.router_id, radio)),
                entry.entry_id,
            )
            via_device_id = radio_dev.id if radio_dev else None
        if via_device_id is None:
            via_device_id = _get_router_device_id(coordinator.hass, coordinator, entry)
        self._attr_device_info = _get_device_info(
            identifiers={
                (DOMAIN, format_ap_device_id(coordinator.router_id, stable_id))
            },
            name=name_label,
            manufacturer="OpenWrt",
            model="Wireless SSID",
            via_device_id=via_device_id,
        )

    @property
    def is_on(self) -> bool | None:
        """Return wireless interface status."""
        if self.coordinator.data is None:
            return None
        for wifi in self.coordinator.data.wireless_interfaces:
            if wifi.name == self._iface_name or (
                self._section_id and wifi.section == self._section_id
            ):
                return wifi.interface_enabled
        return None

    async def _async_set_network_enabled(self, enabled: bool) -> None:
        """Set the SSID and coordinate its physical radio."""
        disable_radio = False
        if not enabled and self._radio and self.coordinator.data:
            target = self._section_id or self._iface_name
            disable_radio = not any(
                wifi.radio == self._radio
                and (wifi.section or wifi.name) != target
                and wifi.interface_enabled
                for wifi in self.coordinator.data.wireless_interfaces
            )

        try:
            if self._radio:
                changed = await self._client.set_wireless_network_enabled(
                    self._section_id or self._iface_name,
                    self._radio,
                    enabled,
                    disable_radio=disable_radio,
                    ssid=self._ssid,
                )
            else:
                changed = await self._client.set_wireless_enabled(
                    self._section_id or self._iface_name,
                    enabled,
                )
            if not changed:
                msg = f"OpenWrt rejected wireless state change for {self._iface_name}"
                raise HomeAssistantError(msg)

            if self.coordinator.data:
                for wifi in self.coordinator.data.wireless_interfaces:
                    if wifi.name == self._iface_name or (
                        self._section_id and wifi.section == self._section_id
                    ):
                        wifi.interface_enabled = enabled
                        wifi.enabled = enabled
                    if self._radio and wifi.radio == self._radio:
                        if enabled:
                            wifi.radio_enabled = True
                        elif disable_radio:
                            wifi.radio_enabled = False
                        wifi.enabled = wifi.radio_enabled and wifi.interface_enabled
            self.async_write_ha_state()
        except HomeAssistantError:
            raise
        except Exception as err:
            msg = f"Failed to change wireless interface {self._iface_name}: {err}"
            raise HomeAssistantError(msg) from err
        self.coordinator.hass.async_create_task(
            self.coordinator.async_request_refresh()
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable wireless network."""
        await self._async_set_network_enabled(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable wireless network."""
        await self._async_set_network_enabled(False)


def _add_wireless_switches(
    coordinator: OpenWrtDataCoordinator,
    entry: ConfigEntry,
    client: OpenWrtClient,
    entities: list[SwitchEntity],
    tracked_keys: set[str],
) -> None:
    """Add wireless switches."""
    ent_reg = er.async_get(coordinator.hass)
    wps_uid = f"{entry.entry_id}_wps"
    if "wps" not in tracked_keys or not ent_reg.async_get_entity_id("switch", DOMAIN, wps_uid):
        tracked_keys.add("wps")
        entities.append(OpenWrtWpsSwitch(coordinator, entry, client))
    for wifi in coordinator.data.wireless_interfaces:
        if wifi.radio:
            key = f"radio_{wifi.radio}"
            radio_uid = f"{entry.entry_id}_radio_{wifi.radio}"
            if key not in tracked_keys or not ent_reg.async_get_entity_id("switch", DOMAIN, radio_uid):
                tracked_keys.add(key)
                entities.append(
                    OpenWrtRadioSwitch(
                        coordinator,
                        entry,
                        client,
                        wifi.radio,
                        wifi.band,
                    )
                )
    for wifi in coordinator.data.wireless_interfaces:
        if wifi.name:
            key = f"wireless_{wifi.section or wifi.name}"
            switch_uid = f"{entry.entry_id}_wireless_{wifi.section or wifi.name}"
            if key not in tracked_keys or not ent_reg.async_get_entity_id("switch", DOMAIN, switch_uid):
                tracked_keys.add(key)
                entities.append(
                    OpenWrtWirelessSwitch(
                        coordinator,
                        entry,
                        client,
                        wifi.name,
                        wifi.ssid,
                        wifi.frequency or wifi.band,
                        wifi.section,
                        wifi.radio,
                    ),
                )
