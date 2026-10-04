"""Bandwidth and traffic monitoring sensors (nlbwmon) for OpenWrt."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfInformation
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .. import sensor
from ..const import DOMAIN
from ..coordinator import OpenWrtDataCoordinator
from ..helpers import get_via_device_id, resolve_client_name
from .base import _format_bytes

_LOGGER = logging.getLogger(__name__)


def _create_nlbwmon_sensors(
    coordinator: OpenWrtDataCoordinator,
    entry: ConfigEntry,
    device: Any,
) -> list[SensorEntity]:
    """Create nlbwmon sensors for a device."""
    if not coordinator.data or not coordinator.data.packages.nlbwmon:
        return []
    return [
        OpenWrtNlbwmonRxSensor(
            coordinator,
            entry,
            device.mac.lower(),
            device.hostname or device.mac,
        ),
        OpenWrtNlbwmonTxSensor(
            coordinator,
            entry,
            device.mac.lower(),
            device.hostname or device.mac,
        ),
    ]


class OpenWrtNlbwmonTopHostsSensor(
    CoordinatorEntity[OpenWrtDataCoordinator], SensorEntity
):
    """Sensor showing count and ranked list of top bandwidth hosts via nlbwmon."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:network-outline"
    _attr_name = "Top Bandwidth Hosts"

    def __init__(
        self,
        coordinator: OpenWrtDataCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_top_bandwidth_hosts"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info."""
        return sensor.DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.router_id)},
        )

    @property
    def native_value(self) -> int | None:
        """Return native value."""
        if not self.coordinator.data:
            return None
        hosts = self.coordinator.data.nlbwmon_top_hosts
        if not hosts:
            return None
        return hosts.get("host_count", 0)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        if not self.coordinator.data:
            return {}
        hosts = self.coordinator.data.nlbwmon_top_hosts
        if not hosts:
            return {}
        return {
            "host_count": hosts.get("host_count", 0),
            "total_download": _format_bytes(hosts.get("total_rx_bytes", 0)),
            "total_upload": _format_bytes(hosts.get("total_tx_bytes", 0)),
            "top_hosts": hosts.get("top_hosts", []),
        }


class OpenWrtNlbwmonRxSensor(CoordinatorEntity[OpenWrtDataCoordinator], SensorEntity):
    """Sensor for client download usage (Rx) from nlbwmon."""

    _attr_has_entity_name = True
    _attr_entity_registry_enabled_default = False
    _attr_icon = "mdi:download"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = UnitOfInformation.MEGABYTES
    _attr_device_class = SensorDeviceClass.DATA_SIZE
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(
        self,
        coordinator: OpenWrtDataCoordinator,
        entry: ConfigEntry,
        mac: str,
        name: str,
    ) -> None:
        """Initialize the nlbwmon Rx sensor."""
        super().__init__(coordinator)
        self._mac = mac.upper()
        self._entry = entry
        self._initial_name = name
        self._attr_name = "Traffic Rx"
        self._attr_unique_id = f"{entry.entry_id}_nlbwmon_rx_{mac.replace(':', '_')}"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information."""
        return sensor.DeviceInfo(
            identifiers={(DOMAIN, self._mac.lower())},
            connections={(dr.CONNECTION_NETWORK_MAC, self._mac.lower())},
            name=resolve_client_name(
                self.coordinator.hass, self._mac, self._initial_name
            ),
            via_device_id=get_via_device_id(
                self.coordinator.hass, self.coordinator, self._entry, self._mac
            ),
        )

    @property
    def native_value(self) -> float | None:
        """Return the Rx bandwidth usage in MB."""
        if not self.coordinator.data:
            return None
        traffic = self.coordinator.data.nlbwmon_traffic.get(self._mac)
        if not traffic:
            return None
        return round(traffic.rx_bytes / (1024 * 1024), 2)


class OpenWrtNlbwmonTxSensor(CoordinatorEntity[OpenWrtDataCoordinator], SensorEntity):
    """Sensor for client upload usage (Tx) from nlbwmon."""

    _attr_has_entity_name = True
    _attr_entity_registry_enabled_default = False
    _attr_icon = "mdi:upload"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = UnitOfInformation.MEGABYTES
    _attr_device_class = SensorDeviceClass.DATA_SIZE
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(
        self,
        coordinator: OpenWrtDataCoordinator,
        entry: ConfigEntry,
        mac: str,
        name: str,
    ) -> None:
        """Initialize the nlbwmon Tx sensor."""
        super().__init__(coordinator)
        self._mac = mac.upper()
        self._entry = entry
        self._initial_name = name
        self._attr_name = "Traffic Tx"
        self._attr_unique_id = f"{entry.entry_id}_nlbwmon_tx_{mac.replace(':', '_')}"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information."""
        return sensor.DeviceInfo(
            identifiers={(DOMAIN, self._mac.lower())},
            connections={(dr.CONNECTION_NETWORK_MAC, self._mac.lower())},
            name=resolve_client_name(
                self.coordinator.hass, self._mac, self._initial_name
            ),
            via_device_id=get_via_device_id(
                self.coordinator.hass, self.coordinator, self._entry, self._mac
            ),
        )

    @property
    def native_value(self) -> float | None:
        """Return the Tx bandwidth usage in MB."""
        if not self.coordinator.data:
            return None
        traffic = self.coordinator.data.nlbwmon_traffic.get(self._mac)
        if not traffic:
            return None
        return round(traffic.tx_bytes / (1024 * 1024), 2)
