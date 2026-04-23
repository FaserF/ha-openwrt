"""Image platform for OpenWrt integration."""

from __future__ import annotations

import io
import logging
from typing import TYPE_CHECKING, cast

from homeassistant.components.image import ImageEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

if TYPE_CHECKING:
    from .api.base import WifiCredentials
    from .coordinator import OpenWrtDataCoordinator


_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up OpenWrt image entities from a config entry."""
    # Local imports to prevent top-level ModuleNotFoundError issues
    from .const import DATA_COORDINATOR, DOMAIN

    coordinator: OpenWrtDataCoordinator = hass.data[DOMAIN][entry.entry_id][
        DATA_COORDINATOR
    ]

    _LOGGER.debug("Setting up OpenWrt image platform for %s", entry.title)

    @callback
    def _async_add_new_entities() -> None:
        if not coordinator.data:
            _LOGGER.debug("No data in coordinator yet for %s", entry.title)
            return

        _LOGGER.debug(
            "Checking for new Wi-Fi QR entities for %s (found %d credentials)",
            entry.title,
            len(coordinator.data.wifi_credentials),
        )

        new_entities: list[ImageEntity] = []
        tracked_keys = {
            entity.unique_id
            for entity in hass.data[DOMAIN][entry.entry_id].get("image_entities", [])
        }

        # Group credentials by SSID to avoid duplicates
        # Map: (ssid, key) -> WifiCredentials
        unique_creds: dict[tuple[str, str], WifiCredentials] = {}
        for cred in coordinator.data.wifi_credentials:
            if not cred.ssid:
                continue
            key = (cred.ssid, cred.key)
            if key not in unique_creds:
                unique_creds[key] = cred

        for (ssid, _password), cred in unique_creds.items():
            # Use SSID-based unique_id for grouping
            unique_id = f"{entry.entry_id}_wifi_qr_{ssid.replace(' ', '_')}"
            if unique_id in tracked_keys:
                continue
            _LOGGER.debug(
                "Adding new grouped Wi-Fi QR entity for %s: %s", entry.title, ssid
            )
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

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: OpenWrtDataCoordinator,
        entry: ConfigEntry,
        cred: WifiCredentials,
    ) -> None:
        """Initialize the image entity."""
        # Local imports for helper functions
        from .const import DOMAIN

        super().__init__(hass)
        self.coordinator = coordinator
        self._entry = entry
        self._iface = cred.iface
        self._ssid = cred.ssid
        self._attr_unique_id = f"{entry.entry_id}_wifi_qr_{cred.ssid.replace(' ', '_')}"
        self._attr_name = f"Wi-Fi QR Code ({cred.ssid})"
        router_id = cast(str, entry.unique_id or entry.data[CONF_HOST])
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, router_id)},
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
        import segno

        qr = segno.make(qr_string)
        buf = io.BytesIO()
        qr.save(buf, kind="png", border=2, scale=10)
        return buf.getvalue()
