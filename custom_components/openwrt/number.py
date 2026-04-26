"""Number platform for OpenWrt integration.

Exposes configurable numeric parameters as number entities,
allowing direct dashboard control instead of requiring the options flow.
"""

from __future__ import annotations

import logging
from typing import Any, cast

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DATA_CLIENT, DATA_COORDINATOR, DOMAIN
from .coordinator import OpenWrtDataCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up OpenWrt number entities from a config entry."""
    coordinator: OpenWrtDataCoordinator = hass.data[DOMAIN][entry.entry_id][
        DATA_COORDINATOR
    ]

    tracked_keys: set[str] = set()

    def _async_add_new_entities() -> None:
        """Add new entities when devices are discovered."""
        if not coordinator.data:
            return

        new_entities: list[NumberEntity] = []
        perms = coordinator.data.permissions
        pkgs = coordinator.data.packages

        # TX Power per wireless interface
        if perms.write_wireless:
            for wifi in coordinator.data.wireless_interfaces:
                if wifi.name and wifi.txpower > 0:
                    key = f"txpower_{wifi.name}"
                    if key not in tracked_keys:
                        tracked_keys.add(key)
                        new_entities.append(
                            OpenWrtTxPowerNumber(
                                coordinator, entry, wifi.name, wifi.ssid
                            ),
                        )

        # SQM Limits
        if perms.write_sqm and pkgs.sqm_scripts is not False:
            for sqm in coordinator.data.sqm:
                if sqm.section_id:
                    for direction in ("download", "upload"):
                        key = f"sqm_{sqm.section_id}_{direction}"
                        if key not in tracked_keys:
                            tracked_keys.add(key)
                            if direction == "download":
                                new_entities.append(
                                    OpenWrtSqmDownloadNumber(
                                        coordinator,
                                        entry,
                                        sqm.section_id,
                                        sqm.name,
                                    ),
                                )
                            else:
                                new_entities.append(
                                    OpenWrtSqmUploadNumber(
                                        coordinator,
                                        entry,
                                        sqm.section_id,
                                        sqm.name,
                                    ),
                                )

        if new_entities:
            async_add_entities(new_entities)

    # Register listener and run initial discovery
    entry.async_on_unload(coordinator.async_add_listener(_async_add_new_entities))
    _async_add_new_entities()


class OpenWrtTxPowerNumber(CoordinatorEntity[OpenWrtDataCoordinator], NumberEntity):
    """Number entity for WiFi TX Power control."""

    _attr_has_entity_name = True
    _attr_mode = NumberMode.SLIDER
    _attr_native_min_value = 0
    _attr_native_max_value = 30
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "dBm"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False
    _attr_translation_key = "wifi_txpower_control"

    def __init__(
        self,
        coordinator: OpenWrtDataCoordinator,
        entry: ConfigEntry,
        iface_name: str,
        ssid: str,
    ) -> None:
        """Initialize the TX Power number entity."""
        super().__init__(coordinator)
        self._iface_name = iface_name
        self._entry = entry
        label = ssid or iface_name
        self._attr_name = f"{label} TX Power"
        self._attr_unique_id = f"{entry.entry_id}_txpower_{iface_name}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.unique_id}_ap_{iface_name}")},
            name=f"AP {label}",
            manufacturer="OpenWrt",
            model="Access Point",
            via_device=(DOMAIN, cast(str, entry.unique_id)),
        )

    @property
    def native_value(self) -> float | None:
        """Return the current TX power."""
        if self.coordinator.data:
            for wifi in self.coordinator.data.wireless_interfaces:
                if wifi.name == self._iface_name:
                    return wifi.txpower
        return None

    async def async_set_native_value(self, value: float) -> None:
        """Set the TX power via UCI."""
        client = self.hass.data[DOMAIN][self._entry.entry_id][DATA_CLIENT]
        txpower = int(value)

        # Find the radio for this interface
        radio = None
        if self.coordinator.data:
            for wifi in self.coordinator.data.wireless_interfaces:
                if wifi.name == self._iface_name:
                    radio = wifi.radio
                    break

        if radio:
            try:
                await client.execute_command(
                    f"uci set wireless.{radio}.txpower='{txpower}' && "
                    f"uci commit wireless && wifi reload",
                )
            except Exception as err:
                _LOGGER.exception(
                    "Failed to set TX power for %s: %s",
                    self._iface_name,
                    err,
                )
                raise

        await self.coordinator.async_request_refresh()


class OpenWrtSqmNumber(CoordinatorEntity[OpenWrtDataCoordinator], NumberEntity):
    """Base class for SQM number entities."""

    _attr_has_entity_name = True
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 0
    _attr_native_max_value = 1000000  # 1 Gbit/s
    _attr_native_step = 100
    _attr_native_unit_of_measurement = "kbit/s"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: OpenWrtDataCoordinator,
        entry: ConfigEntry,
        section_id: str,
        name: str,
    ) -> None:
        """Initialize the SQM number entity."""
        super().__init__(coordinator)
        self._section_id = section_id
        self._entry = entry
        self._sqm_name = name
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, cast(str, entry.unique_id or entry.data[CONF_HOST]))},
        )

    @property
    def native_value(self) -> float | None:
        """Return the current value."""
        if self.coordinator.data:
            for sqm in self.coordinator.data.sqm:
                if sqm.section_id == self._section_id:
                    return self._get_sqm_value(sqm)
        return None

    def _get_sqm_value(self, sqm: Any) -> int:
        """Get the specific SQM value (download or upload)."""
        raise NotImplementedError

    async def async_set_native_value(self, value: float) -> None:
        """Set the SQM limit via UCI."""
        client = self.hass.data[DOMAIN][self._entry.entry_id][DATA_CLIENT]
        try:
            await client.set_sqm_config(
                self._section_id,
                **{self._option_key: int(value)},
            )
        except Exception as err:
            _LOGGER.exception(
                "Failed to set SQM %s for %s: %s",
                self._option_key,
                self._sqm_name,
                err,
            )
            raise
        await self.coordinator.async_request_refresh()

    @property
    def _option_key(self) -> str:
        """UCI option key."""
        raise NotImplementedError


class OpenWrtSqmDownloadNumber(OpenWrtSqmNumber):
    """Number entity for SQM download limit."""

    _attr_translation_key = "sqm_download"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize."""
        super().__init__(*args, **kwargs)
        self._attr_name = f"SQM {self._sqm_name} Download Limit"
        self._attr_unique_id = f"{self._entry.entry_id}_sqm_{self._section_id}_download"

    def _get_sqm_value(self, sqm: Any) -> int:
        return sqm.download

    @property
    def _option_key(self) -> str:
        return "download"


class OpenWrtSqmUploadNumber(OpenWrtSqmNumber):
    """Number entity for SQM upload limit."""

    _attr_translation_key = "sqm_upload"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize."""
        super().__init__(*args, **kwargs)
        self._attr_name = f"SQM {self._sqm_name} Upload Limit"
        self._attr_unique_id = f"{self._entry.entry_id}_sqm_{self._section_id}_upload"

    def _get_sqm_value(self, sqm: Any) -> int:
        return sqm.upload

    @property
    def _option_key(self) -> str:
        return "upload"
