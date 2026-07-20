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

        # TX Power is configured on the physical radio, not on an SSID interface.
        if perms.write_wireless:
            for wifi in coordinator.data.wireless_interfaces:
                if wifi.radio and wifi.txpower >= 0:
                    key = f"txpower_{wifi.radio}"
                    if key not in tracked_keys:
                        tracked_keys.add(key)
                        new_entities.append(
                            OpenWrtTxPowerNumber(
                                coordinator,
                                entry,
                                wifi.radio,
                                wifi.band,
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

    def _async_cleanup_entities() -> None:
        """Remove orphaned number entities when radios/interfaces vanish."""
        from homeassistant.helpers import entity_registry as er

        ent_reg = er.async_get(hass)
        entries = er.async_entries_for_config_entry(ent_reg, entry.entry_id)

        for ent in entries:
            if ent.domain != "number":
                continue

            unique_id = ent.unique_id
            if "_txpower_" in unique_id and coordinator.data:
                radio = unique_id.split("_txpower_")[-1]
                if not any(
                    w.radio == radio
                    for w in coordinator.data.wireless_interfaces
                ):
                    ent_reg.async_remove(ent.entity_id)

    hass.add_job(_async_cleanup_entities)


class OpenWrtTxPowerNumber(CoordinatorEntity[OpenWrtDataCoordinator], NumberEntity):
    """Number entity for WiFi TX Power control."""

    _attr_has_entity_name = True
    _attr_mode = NumberMode.SLIDER
    _attr_native_min_value = 0
    _attr_native_max_value = 30
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "dBm"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "wifi_txpower_control"

    def __init__(
        self,
        coordinator: OpenWrtDataCoordinator,
        entry: ConfigEntry,
        radio: str,
        band: str,
    ) -> None:
        """Initialize the TX Power number entity."""
        super().__init__(coordinator)
        self._radio = radio
        self._entry = entry
        label = f"{radio} ({band})" if band else radio
        self._attr_name = f"{label} TX Power"
        self._attr_unique_id = f"{entry.entry_id}_txpower_{radio}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.unique_id}_radio_{radio}")},
            name=f"Radio {label}",
            manufacturer="OpenWrt",
            model="Wireless Radio",
            via_device=(DOMAIN, cast(str, entry.unique_id)),
        )

    @property
    def native_max_value(self) -> float:
        """Return the maximum supported TX power limit."""
        if self.coordinator.data:
            for wifi in self.coordinator.data.wireless_interfaces:
                if wifi.radio == self._radio:
                    if wifi.txpower_offset and wifi.txpower_offset > 0:
                        return float(wifi.txpower_offset)
                    if wifi.txpower > 30:
                        return float(wifi.txpower)
        return 30.0

    @property
    def native_value(self) -> float | None:
        """Return the current TX power."""
        if self.coordinator.data:
            for wifi in self.coordinator.data.wireless_interfaces:
                if wifi.radio == self._radio and wifi.txpower >= 0:
                    return wifi.txpower
        return None

    async def async_set_native_value(self, value: float) -> None:
        """Set the TX power via UCI."""
        client = self.hass.data[DOMAIN][self._entry.entry_id][DATA_CLIENT]
        txpower = int(value)

        try:
            await client.execute_command(
                f"uci set wireless.{self._radio}.txpower='{txpower}' && "
                f"uci commit wireless && wifi reload",
            )
        except Exception as err:
            _LOGGER.exception(
                "Failed to set TX power for %s: %s",
                self._radio,
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
