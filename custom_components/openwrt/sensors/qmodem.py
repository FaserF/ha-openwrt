"""QModem sensors for OpenWrt."""

from __future__ import annotations

import logging
from datetime import datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    EntityCategory,
    UnitOfTemperature,
)
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.typing import StateType

from ..const import DOMAIN
from ..coordinator import OpenWrtDataCoordinator
from ..helpers import _get_router_device_id
from .base import OpenWrtSensorDescription, OpenWrtSensorEntity

_LOGGER = logging.getLogger(__name__)


class OpenWrtQModemSensorEntity(OpenWrtSensorEntity):
    """Representation of an OpenWrt QModem sensor."""

    entity_description: OpenWrtSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: OpenWrtDataCoordinator,
        entry: ConfigEntry,
        description: OpenWrtSensorDescription,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator, entry, description)
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"

        manufacturer = coordinator.data.qmodem_info.manufacturer or "Unknown"
        revision = coordinator.data.qmodem_info.revision
        model = f"QModem {revision}" if revision else "QModem Device"

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.unique_id}_qmodem")},
            name=f"QModem ({entry.title})",
            manufacturer=manufacturer,
            model=model,
            via_device_id=_get_router_device_id(coordinator.hass, coordinator, entry),
        )

    @property
    def native_value(self) -> StateType | datetime:
        """Return value."""
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def available(self) -> bool:
        """Return availability."""
        if not self.coordinator.last_update_success:
            return False
        return not (
            self.coordinator.data and not self.coordinator.data.qmodem_info.enabled
        )


def _get_qmodem_sensors() -> tuple[OpenWrtSensorDescription, ...]:
    """Get QModem sensors."""
    return (
        OpenWrtSensorDescription(
            key="qmodem_manufacturer",
            name="Modem Manufacturer",
            translation_key="qmodem_manufacturer",
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
            value_fn=lambda data: data.qmodem_info.manufacturer,
        ),
        OpenWrtSensorDescription(
            key="qmodem_revision",
            name="Modem Revision",
            translation_key="qmodem_revision",
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
            value_fn=lambda data: data.qmodem_info.revision,
        ),
        OpenWrtSensorDescription(
            key="qmodem_temperature",
            name="Modem Temperature",
            translation_key="qmodem_temperature",
            device_class=SensorDeviceClass.TEMPERATURE,
            native_unit_of_measurement=UnitOfTemperature.CELSIUS,
            state_class=SensorStateClass.MEASUREMENT,
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=lambda data: data.qmodem_info.temperature,
        ),
        OpenWrtSensorDescription(
            key="qmodem_voltage",
            name="Modem Voltage",
            translation_key="qmodem_voltage",
            device_class=SensorDeviceClass.VOLTAGE,
            native_unit_of_measurement="mV",
            state_class=SensorStateClass.MEASUREMENT,
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=lambda data: data.qmodem_info.voltage,
        ),
        OpenWrtSensorDescription(
            key="qmodem_connect_status",
            name="Modem Connect Status",
            translation_key="qmodem_connect_status",
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=lambda data: data.qmodem_info.connect_status,
        ),
        OpenWrtSensorDescription(
            key="qmodem_sim_status",
            name="SIM Status",
            translation_key="qmodem_sim_status",
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=lambda data: data.qmodem_info.sim_status,
        ),
        OpenWrtSensorDescription(
            key="qmodem_isp",
            name="Internet Service Provider",
            translation_key="qmodem_isp",
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=lambda data: data.qmodem_info.isp,
        ),
        OpenWrtSensorDescription(
            key="qmodem_sim_slot",
            name="SIM Slot",
            translation_key="qmodem_sim_slot",
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=lambda data: data.qmodem_info.sim_slot,
        ),
        OpenWrtSensorDescription(
            key="qmodem_lte_rsrp",
            name="LTE RSRP",
            translation_key="qmodem_lte_rsrp",
            device_class=SensorDeviceClass.SIGNAL_STRENGTH,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement="dBm",
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=lambda data: data.qmodem_info.lte_rsrp,
        ),
        OpenWrtSensorDescription(
            key="qmodem_lte_rsrq",
            name="LTE RSRQ",
            translation_key="qmodem_lte_rsrq",
            device_class=SensorDeviceClass.SIGNAL_STRENGTH,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement="dB",
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=lambda data: data.qmodem_info.lte_rsrq,
        ),
        OpenWrtSensorDescription(
            key="qmodem_lte_rssi",
            name="LTE RSSI",
            translation_key="qmodem_lte_rssi",
            device_class=SensorDeviceClass.SIGNAL_STRENGTH,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement="dBm",
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=lambda data: data.qmodem_info.lte_rssi,
        ),
        OpenWrtSensorDescription(
            key="qmodem_lte_sinr",
            name="LTE SINR",
            translation_key="qmodem_lte_sinr",
            device_class=SensorDeviceClass.SIGNAL_STRENGTH,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement="dB",
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=lambda data: data.qmodem_info.lte_sinr,
        ),
        OpenWrtSensorDescription(
            key="qmodem_nr5g_rsrp",
            name="5G NR RSRP",
            translation_key="qmodem_nr5g_rsrp",
            device_class=SensorDeviceClass.SIGNAL_STRENGTH,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement="dBm",
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=lambda data: data.qmodem_info.nr5g_rsrp,
        ),
        OpenWrtSensorDescription(
            key="qmodem_nr5g_rsrq",
            name="5G NR RSRQ",
            translation_key="qmodem_nr5g_rsrq",
            device_class=SensorDeviceClass.SIGNAL_STRENGTH,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement="dB",
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=lambda data: data.qmodem_info.nr5g_rsrq,
        ),
        OpenWrtSensorDescription(
            key="qmodem_nr5g_sinr",
            name="5G NR SINR",
            translation_key="qmodem_nr5g_sinr",
            device_class=SensorDeviceClass.SIGNAL_STRENGTH,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement="dB",
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=lambda data: data.qmodem_info.nr5g_sinr,
        ),
        OpenWrtSensorDescription(
            key="qmodem_gps_latitude",
            name="Modem GPS Latitude",
            translation_key="qmodem_gps_latitude",
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=lambda data: data.qmodem_info.gps_latitude,
        ),
        OpenWrtSensorDescription(
            key="qmodem_gps_longitude",
            name="Modem GPS Longitude",
            translation_key="qmodem_gps_longitude",
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=lambda data: data.qmodem_info.gps_longitude,
        ),
        OpenWrtSensorDescription(
            key="qmodem_gps_last_update",
            name="Modem GPS Last Update",
            translation_key="qmodem_gps_last_update",
            device_class=SensorDeviceClass.TIMESTAMP,
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=lambda data: data.qmodem_info.gps_last_update,
        ),
        OpenWrtSensorDescription(
            key="qmodem_gps_last_update_attempted",
            name="Modem GPS Last Update Attempted",
            translation_key="qmodem_gps_last_update_attempted",
            device_class=SensorDeviceClass.TIMESTAMP,
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=lambda data: data.qmodem_info.gps_last_update_attempted,
        ),
        OpenWrtSensorDescription(
            key="qmodem_gps_last_update_successful",
            name="Modem GPS Last Update Successful",
            translation_key="qmodem_gps_last_update_successful",
            device_class=SensorDeviceClass.TIMESTAMP,
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=lambda data: data.qmodem_info.gps_last_update_successful,
        ),
        OpenWrtSensorDescription(
            key="qmodem_gps_last_update_ok",
            name="Modem GPS Last Update Status",
            translation_key="qmodem_gps_last_update_ok",
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=lambda data: (
                None
                if data.qmodem_info.gps_last_update_ok is None
                else ("OK" if data.qmodem_info.gps_last_update_ok else "Failed")
            ),
        ),
    )
