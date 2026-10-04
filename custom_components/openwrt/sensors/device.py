"""Device-specific sensors for OpenWrt."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any, cast

from homeassistant.components.sensor import (
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.typing import UNDEFINED, StateType
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ..const import DOMAIN
from ..coordinator import OpenWrtData, OpenWrtDataCoordinator
from ..helpers import get_via_device_id, is_random_mac, resolve_client_name

_LOGGER = logging.getLogger(__name__)


class OpenWrtDeviceSensor(CoordinatorEntity[OpenWrtDataCoordinator], SensorEntity):
    """Representation of an OpenWrt per-device sensor (e.g. signal)."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: OpenWrtDataCoordinator,
        entry: ConfigEntry,
        mac: str,
        description: SensorEntityDescription,
        value_fn: Callable[[OpenWrtData], StateType],
        available_fn: Callable[[OpenWrtData], bool] | None = None,
        device_name: str | None = None,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self.entity_description = description
        self._mac = mac.lower()
        self._value_fn = value_fn
        self._available_fn = available_fn
        self._attr_unique_id = f"{entry.entry_id}_{self._mac}_{description.key}"
        self._attr_name = (
            cast(str, description.name) if description.name is not UNDEFINED else None
        )
        self._entry = entry
        self._initial_name = device_name or mac

        if is_random_mac(self._mac):
            self._attr_entity_registry_enabled_default = False
        elif hasattr(description, "entity_registry_enabled_default"):
            self._attr_entity_registry_enabled_default = (
                description.entity_registry_enabled_default
            )

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._mac)},
            connections={(dr.CONNECTION_NETWORK_MAC, self._mac)},
            name=resolve_client_name(
                self.coordinator.hass, self._mac, self._initial_name
            ),
            via_device_id=get_via_device_id(
                self.coordinator.hass, self.coordinator, self._entry, self._mac
            ),
        )

    @property
    def native_value(self) -> StateType | datetime:
        """Return value."""
        if self.coordinator.data is None:
            return None
        return self._value_fn(self.coordinator.data)

    @property
    def available(self) -> bool:
        """Return availability."""
        if not self.coordinator.last_update_success:
            return False
        if self._available_fn and self.coordinator.data:
            return self._available_fn(self.coordinator.data)
        return True

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return attributes."""
        if self.coordinator.data is None:
            return {}

        for device in self.coordinator.data.connected_devices:
            if device.mac and device.mac.lower() == self._mac:
                attrs: dict[str, Any] = {
                    "mac": device.mac,
                    "is_wireless": device.is_wireless,
                }
                if device.connection_type:
                    attrs["connection_type"] = device.connection_type
                if device.connection_info:
                    attrs["connection_info"] = device.connection_info
                if device.rx_bytes:
                    attrs["rx_bytes"] = device.rx_bytes
                if device.tx_bytes:
                    attrs["tx_bytes"] = device.tx_bytes
                if device.rx_rate:
                    attrs["rx_rate"] = device.rx_rate
                if device.tx_rate:
                    attrs["tx_rate"] = device.tx_rate
                if device.uptime:
                    attrs["uptime"] = device.uptime
                if device.interface:
                    attrs["interface"] = device.interface
                return attrs
        return {}


def _create_device_sensors(
    coordinator: OpenWrtDataCoordinator,
    entry: ConfigEntry,
    device: Any,
) -> list[OpenWrtDeviceSensor]:
    """Create sensors for a specific connected device."""
    dev_name = _get_device_display_name(coordinator, device)
    mac = device.mac.lower()

    descriptions = [
        (
            "signal",
            "Signal Strength",
            "device_signal",
            "dBm",
            lambda d, m=mac: next(
                (x.signal for x in d.connected_devices if x.mac and x.mac.lower() == m),
                None,
            ),
            lambda d, m=mac: any(
                x.mac and x.mac.lower() == m and x.is_wireless
                for x in d.connected_devices
            ),
        ),
        (
            "rx_rate",
            "RX Rate",
            "device_rx_rate",
            "Mbps",
            lambda d, m=mac: next(
                (
                    round(x.rx_rate / 1000, 1)
                    for x in d.connected_devices
                    if x.mac and x.mac.lower() == m
                ),
                None,
            ),
            lambda d, m=mac: any(
                x.mac and x.mac.lower() == m and x.is_wireless
                for x in d.connected_devices
            ),
        ),
        (
            "tx_rate",
            "TX Rate",
            "device_tx_rate",
            "Mbps",
            lambda d, m=mac: next(
                (
                    round(x.tx_rate / 1000, 1)
                    for x in d.connected_devices
                    if x.mac and x.mac.lower() == m
                ),
                None,
            ),
            lambda d, m=mac: any(
                x.mac and x.mac.lower() == m and x.is_wireless
                for x in d.connected_devices
            ),
        ),
        (
            "noise",
            "Noise Level",
            "device_noise",
            "dBm",
            lambda d, m=mac: next(
                (x.noise for x in d.connected_devices if x.mac and x.mac.lower() == m),
                None,
            ),
            lambda d, m=mac: any(
                x.mac and x.mac.lower() == m and x.is_wireless
                for x in d.connected_devices
            ),
        ),
    ]

    return [
        OpenWrtDeviceSensor(
            coordinator,
            entry,
            mac,
            SensorEntityDescription(
                key=f"device_{key}",
                name=name,
                translation_key=tkey,
                native_unit_of_measurement=unit,
                state_class=SensorStateClass.MEASUREMENT if unit else None,
                entity_category=EntityCategory.DIAGNOSTIC,
                entity_registry_enabled_default=False,
            ),
            v_fn,
            a_fn,
            dev_name,
        )
        for key, name, tkey, unit, v_fn, a_fn in descriptions
    ]


def _get_device_display_name(coordinator: OpenWrtDataCoordinator, device: Any) -> str:
    """Determine the display name for a device."""
    dev_name = device.mac
    if device.hostname and device.hostname != "*":
        router_hostname = ""
        if coordinator.data and coordinator.data.device_info:
            router_hostname = coordinator.data.device_info.hostname
        if device.hostname != router_hostname:
            dev_name = device.hostname
    return dev_name
