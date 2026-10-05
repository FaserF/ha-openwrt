"""Wireless sensors for OpenWrt."""

from __future__ import annotations

import logging

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.helpers import device_registry as dr, entity_registry as er

from ..const import DOMAIN
from ..coordinator import OpenWrtDataCoordinator
from ..helpers import (
    _get_router_device_id,
    _lookup_device,
    format_ap_device_id,
    format_ap_name,
    format_radio_device_id,
)
from .base import OpenWrtSensorDescription, OpenWrtSensorEntity, _get_device_info

_LOGGER = logging.getLogger(__name__)


class OpenWrtWifiSensorEntity(OpenWrtSensorEntity):
    """Representation of an OpenWrt WiFi sensor."""

    def __init__(
        self,
        coordinator: OpenWrtDataCoordinator,
        entry: ConfigEntry,
        description: OpenWrtSensorDescription,
        iface_name: str,
        ssid: str,
        frequency: str = "",
        section_id: str | None = None,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator, entry, description)

        name_label = format_ap_name(ssid or iface_name, frequency)

        # Ensure sensors are grouped under the correct AP device
        stable_id = coordinator.interface_to_stable_id.get(iface_name, iface_name)

        wifi = next(
            (
                item
                for item in coordinator.data.wireless_interfaces
                if item.name == iface_name
            ),
            None,
        )
        via_device_id: str | None = None
        if wifi and wifi.radio:
            dev_reg = dr.async_get(coordinator.hass)
            radio_dev = _lookup_device(
                dev_reg,
                (DOMAIN, format_radio_device_id(coordinator.router_id, wifi.radio)),
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
        self._attr_translation_placeholders = {"iface": iface_name}


def _create_wifi_sensors(
    coordinator: OpenWrtDataCoordinator,
    entry: ConfigEntry,
    iface_name: str,
    ssid: str,
    mode: str,
    frequency: str = "",
    section_id: str | None = None,
    ifname: str | None = None,
) -> list[OpenWrtWifiSensorEntity]:
    """Create sensors for a wireless interface."""
    sensors: list[OpenWrtWifiSensorEntity] = []

    # Base configuration sensors
    _create_wifi_base_sensors(
        coordinator, entry, iface_name, ssid, frequency, section_id, ifname, sensors
    )

    # Station-specific quality sensors (STA/Mesh/etc)
    if mode.lower() not in ("ap", "master", "access point"):
        _create_wifi_station_sensors(
            coordinator, entry, iface_name, ssid, frequency, section_id, ifname, sensors
        )

    return sensors


def _create_wifi_base_sensors(
    coordinator: OpenWrtDataCoordinator,
    entry: ConfigEntry,
    iface_name: str,
    ssid: str,
    frequency: str,
    section_id: str | None,
    ifname: str | None,
    sensors: list[OpenWrtWifiSensorEntity],
) -> None:
    """Create basic WiFi sensors (Clients, Channel, Power, etc.)."""
    label = ssid or iface_name

    # Clients
    sensors.append(
        OpenWrtWifiSensorEntity(
            coordinator,
            entry,
            OpenWrtSensorDescription(
                key=f"wifi_{section_id or iface_name}_clients",
                translation_key="wifi_clients",
                name=f"{label} Clients",
                state_class=SensorStateClass.MEASUREMENT,
                value_fn=lambda data, n=iface_name, s=section_id, i=ifname: sum(
                    1
                    for d in data.all_connected_devices
                    if d.is_wireless
                    and d.connected
                    and (
                        d.interface == n
                        or (s and d.interface == s)
                        or (i and d.interface == i)
                    )
                ),
            ),
            iface_name,
            ssid,
            frequency,
        )
    )

    # Generic descriptions for simple interface lookups
    desc_map = {
        "channel": ("Channel", "wifi_channel", EntityCategory.DIAGNOSTIC, True),
        "txpower": ("TX Power", "wifi_txpower", EntityCategory.DIAGNOSTIC, False),
        "htmode": ("HT Mode", "wifi_htmode", EntityCategory.DIAGNOSTIC, False),
        "hwmode": ("Hardware Mode", "wifi_hwmode", EntityCategory.DIAGNOSTIC, False),
    }

    for key, (name, tkey, cat, enabled) in desc_map.items():
        sensors.append(
            OpenWrtWifiSensorEntity(
                coordinator,
                entry,
                OpenWrtSensorDescription(
                    key=f"wifi_{section_id or iface_name}_{key}",
                    translation_key=tkey,
                    name=f"{label} {name}",
                    native_unit_of_measurement="dBm" if key == "txpower" else None,
                    entity_category=cat,
                    entity_registry_enabled_default=enabled,
                    value_fn=lambda data, n=iface_name, s=section_id, i=ifname, k=key: (
                        next(
                            (
                                getattr(w, k)
                                for w in data.wireless_interfaces
                                if w.name == n
                                or (s and w.section == s)
                                or (i and w.ifname == i)
                            ),
                            None,
                        )
                    ),
                ),
                iface_name,
                ssid,
                frequency,
                section_id,
            )
        )


def _create_wifi_station_sensors(
    coordinator: OpenWrtDataCoordinator,
    entry: ConfigEntry,
    iface_name: str,
    ssid: str,
    frequency: str,
    section_id: str | None,
    ifname: str | None,
    sensors: list[OpenWrtWifiSensorEntity],
) -> None:
    """Create quality sensors for WiFi station interfaces."""
    label = ssid or iface_name

    # Signal
    sensors.append(
        OpenWrtWifiSensorEntity(
            coordinator,
            entry,
            OpenWrtSensorDescription(
                key=f"wifi_{section_id or iface_name}_signal",
                translation_key="wifi_signal",
                name=f"{label} Signal",
                native_unit_of_measurement="dBm",
                device_class=SensorDeviceClass.SIGNAL_STRENGTH,
                state_class=SensorStateClass.MEASUREMENT,
                entity_category=EntityCategory.DIAGNOSTIC,
                value_fn=lambda data, n=iface_name, s=section_id, i=ifname: next(
                    (
                        w.signal
                        for w in data.wireless_interfaces
                        if w.name == n
                        or (s and w.section == s)
                        or (i and w.ifname == i)
                    ),
                    None,
                ),
                available_fn=lambda data, n=iface_name, s=section_id, i=ifname: any(
                    (w.name == n or (s and w.section == s) or (i and w.ifname == i))
                    and w.signal != 0
                    for w in data.wireless_interfaces
                ),
                attrs_fn=lambda data, n=iface_name, s=section_id, i=ifname: next(
                    (
                        {
                            "noise": w.noise,
                            "encryption": w.encryption,
                            "frequency": w.frequency,
                        }
                        for w in data.wireless_interfaces
                        if w.name == n
                        or (s and w.section == s)
                        or (i and w.ifname == i)
                    ),
                    {},
                ),
            ),
            iface_name,
            ssid,
            frequency,
            section_id,
        )
    )

    # Simple station sensors
    sta_map = {
        "quality": (
            "Signal Quality",
            "wifi_quality",
            PERCENTAGE,
            SensorStateClass.MEASUREMENT,
        ),
        "bitrate": ("Bitrate", "wifi_bitrate", "Mbps", SensorStateClass.MEASUREMENT),
        "noise": ("Noise Level", "wifi_noise", "dBm", SensorStateClass.MEASUREMENT),
    }

    for key, (name, tkey, unit, sclass) in sta_map.items():
        sensors.append(
            OpenWrtWifiSensorEntity(
                coordinator,
                entry,
                OpenWrtSensorDescription(
                    key=f"wifi_{section_id or iface_name}_{key}",
                    translation_key=tkey,
                    name=f"{label} {name}",
                    native_unit_of_measurement=unit,
                    device_class=(
                        SensorDeviceClass.DATA_RATE
                        if key == "bitrate"
                        else (
                            SensorDeviceClass.SIGNAL_STRENGTH
                            if key == "noise"
                            else None
                        )
                    ),
                    state_class=sclass,
                    entity_category=EntityCategory.DIAGNOSTIC,
                    entity_registry_enabled_default=False,
                    value_fn=lambda data, n=iface_name, s=section_id, i=ifname, k=key: (
                        next(
                            (
                                getattr(w, k)
                                for w in data.wireless_interfaces
                                if w.name == n
                                or (s and w.section == s)
                                or (i and w.ifname == i)
                            ),
                            None,
                        )
                    ),
                ),
                iface_name,
                ssid,
                frequency,
            )
        )


def _async_setup_wireless_sensors(
    coordinator: OpenWrtDataCoordinator,
    entry: ConfigEntry,
    entities: list[SensorEntity],
    tracked_keys: set[str],
) -> None:
    """Set up interface-specific wireless sensors."""
    if not coordinator.data or not coordinator.data.wireless_interfaces:
        return
    ent_reg = er.async_get(coordinator.hass)
    for wifi in coordinator.data.wireless_interfaces:
        if not wifi.name:
            continue
        # Use signal as a representative key for the group of sensors created by _create_wifi_sensors
        key = f"wifi_{wifi.section or wifi.name}_signal"
        uid_clients = f"{entry.entry_id}_wifi_{wifi.section or wifi.name}_clients"
        if key not in tracked_keys or not ent_reg.async_get_entity_id("sensor", DOMAIN, uid_clients):
            tracked_keys.add(key)
            entities.extend(
                _create_wifi_sensors(
                    coordinator,
                    entry,
                    wifi.name,
                    wifi.ssid,
                    wifi.mode,
                    wifi.frequency,
                    wifi.section,
                    wifi.ifname,
                )
            )
