"""Image platform for OpenWrt integration."""

from __future__ import annotations

import io
from typing import cast

import qrcode
from homeassistant.components.image import ImageEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .api.base import WifiCredentials
from .const import DATA_COORDINATOR, DOMAIN
from .coordinator import OpenWrtDataCoordinator
from .helpers import format_ap_device_id, format_ap_name


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up OpenWrt image entities from a config entry."""
    coordinator: OpenWrtDataCoordinator = hass.data[DOMAIN][entry.entry_id][
        DATA_COORDINATOR
    ]

    @callback
    def _async_add_new_entities() -> None:
        if not coordinator.data:
            return

        new_entities: list[ImageEntity] = []
        tracked_keys = {
            entity.unique_id
            for entity in hass.data[DOMAIN][entry.entry_id].get("image_entities", [])
        }

        for cred in coordinator.data.wifi_credentials:
            unique_id = f"{entry.entry_id}_wifi_qr_{cred.iface}"
            if unique_id in tracked_keys:
                continue
            new_entities.append(OpenWrtWifiQrImage(hass, coordinator, entry, cred))

        if new_entities:
            async_add_entities(new_entities)
            if "image_entities" not in hass.data[DOMAIN][entry.entry_id]:
                hass.data[DOMAIN][entry.entry_id]["image_entities"] = []
            hass.data[DOMAIN][entry.entry_id]["image_entities"].extend(new_entities)

    entry.async_on_unload(coordinator.async_add_listener(_async_add_new_entities))
    _async_add_new_entities()


class OpenWrtWifiQrImage(ImageEntity):
    """Wi-Fi QR Code image entity."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: OpenWrtDataCoordinator,
        entry: ConfigEntry,
        cred: WifiCredentials,
    ) -> None:
        """Initialize the image entity."""
        super().__init__(hass)
        self.coordinator = coordinator
        self._entry = entry
        self._iface = cred.iface
        self._ssid = cred.ssid
        self._attr_unique_id = f"{entry.entry_id}_wifi_qr_{cred.iface}"
        self._attr_name = f"Wi-Fi QR Code ({cred.ssid})"
        router_id = cast(str, entry.unique_id or entry.data[CONF_HOST])
        stable_id = self.coordinator.interface_to_stable_id.get(cred.iface, cred.iface)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, format_ap_device_id(router_id, stable_id))},
            name=format_ap_name(cred.ssid),
            manufacturer="OpenWrt",
            model="Access Point",
            via_device=(DOMAIN, router_id),
        )
        self._attr_image_last_updated = dt_util.utcnow()

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self.coordinator.last_update_success

    async def async_image(self) -> bytes | None:
        """Return bytes of image."""
        if not self.coordinator.data:
            return None

        qr_string = ""
        for cred in self.coordinator.data.wifi_credentials:
            if cred.iface == self._iface:
                t = "WPA"
                if "wep" in cred.encryption.lower():
                    t = "WEP"
                elif (
                    "none" in cred.encryption.lower()
                    or "nopass" in cred.encryption.lower()
                ):
                    t = "nopass"
                h = "true" if cred.hidden else "false"
                qr_string = f"WIFI:S:{cred.ssid};T:{t};P:{cred.key};H:{h};;"
                break

        if not qr_string:
            return None

        # Generate QR code
        img = qrcode.make(qr_string)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
