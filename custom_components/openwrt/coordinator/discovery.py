"""MQTT Device Discovery mixin for OpenWrt coordinator."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import TYPE_CHECKING

from homeassistant.core import callback
from homeassistant.helpers import (
    entity_registry as er,
)

from ..const import (
    CONF_MANUAL_TRACKED_DEVICES,
    CONF_MQTT_PRESENCE,
    CONF_TRACKED_DEVICES,
    DATA_COORDINATOR,
    DOMAIN,
)

if TYPE_CHECKING:
    from .base import CoordinatorBase

    _Base = CoordinatorBase
else:
    _Base = object

_LOGGER = logging.getLogger(__name__)


class DiscoveryMixin(_Base):
    """Mixin for MQTT device tracker discovery and cleanup."""

    async def _async_discovery_loop(self, clean: bool = False) -> None:
        """Loop through history and discover or cleanup devices for MQTT."""
        _LOGGER.debug("Starting MQTT discovery loop (clean=%s)", clean)

        # Check if MQTT integration is even available in HA
        mqtt_component_loaded = "mqtt" in self.hass.config.components

        if not mqtt_component_loaded:
            if not clean:
                _LOGGER.error(
                    "MQTT Presence Detection enabled but MQTT integration not found"
                )
            return

        mqtt_ready = self.hass.services.has_service("mqtt", "publish")

        if not mqtt_ready and not clean:
            # Only wait if we actually want to start discovery (not just cleaning up)
            _LOGGER.debug("Waiting for MQTT service...")
            for _ in range(12):
                if self.hass.services.has_service("mqtt", "publish"):
                    mqtt_ready = True
                    break
                await asyncio.sleep(5)

            if not mqtt_ready:
                _LOGGER.warning(
                    "MQTT service not available after 60s, operation aborted"
                )
                return

        if mqtt_ready:
            _LOGGER.debug(
                "%s MQTT discovery for %d devices",
                "Cleaning up" if clean else "Starting",
                len(self._device_history),
            )
            whitelist = self._async_get_tracked_devices_whitelist()
            for mac, hist_data in list(self._device_history.items()):
                if clean:
                    await self._async_discovery_mqtt_device_cleanup(mac)
                else:
                    if whitelist and mac not in whitelist:
                        await self._async_discovery_mqtt_device_cleanup(mac)
                        continue
                    await self._async_discovery_mqtt_device(
                        mac, hist_data.get("hostname") or mac
                    )

                # Small delay between discovery calls to avoid flooding
                await asyncio.sleep(0.05)

        # Global registry cleanup (independent of device history)
        if clean:
            if mqtt_ready:
                await self._async_global_registry_cleanup()
            self._mqtt_cleanup_done = True
            self._mqtt_presence_configured = False
            try:
                await self._store.async_save(
                    {
                        "devices": self._device_history,
                        "last_version": self._last_version,
                        "mqtt_cleanup_done": self._mqtt_cleanup_done,
                        "mqtt_presence_configured": self._mqtt_presence_configured,
                    }
                )
            except Exception as err:
                _LOGGER.warning("Could not persist MQTT cleanup state: %s", err)

        _LOGGER.debug("MQTT discovery loop finished")

    @callback
    def _async_get_tracked_devices_whitelist(self) -> set[str] | None:
        """Get the merged whitelist of tracked devices."""
        tracked_devices = self.config_entry.options.get(CONF_TRACKED_DEVICES, [])
        manual_devices_raw = self.config_entry.options.get(
            CONF_MANUAL_TRACKED_DEVICES, ""
        )

        whitelist = {
            d.lower() for d in tracked_devices if isinstance(d, str) and d.strip()
        }

        if manual_devices_raw:
            # Parse multi-line string of MAC addresses
            for line in manual_devices_raw.splitlines():
                mac = line.strip().lower()
                if mac:
                    # Basic MAC validation could be added here, but for now we just trust the user
                    whitelist.add(mac)

        return whitelist if whitelist else None

    async def _async_discovery_mqtt_device_cleanup(self, mac: str) -> None:
        """Remove active MQTT discovery message for a device tracker."""
        if not self.hass.services.has_service("mqtt", "publish"):
            if mac in self._mqtt_discovered:
                self._mqtt_discovered.remove(mac)
            return

        mac_safe = mac.replace(":", "_")
        mac_colons = mac.lower()

        # Cleanup current and primary legacy MQTT discovery topics
        discovery_topics = [
            f"homeassistant/device_tracker/openwrt_mqtt_{mac_safe}/config",
            f"homeassistant/device_tracker/openwrt_{mac_safe}/config",
        ]

        for topic in discovery_topics:
            _LOGGER.debug("Clearing MQTT discovery topic: %s", topic)
            try:
                await self.hass.services.async_call(
                    "mqtt",
                    "publish",
                    {
                        "topic": topic,
                        "payload": "",
                        "retain": True,
                    },
                )
            except Exception as err:
                _LOGGER.debug("Failed to clear topic %s: %s", topic, err)

        # Check if any other config entry is still tracking this device via MQTT presence
        other_entry_tracks_device = False
        domain_data = self.hass.data.get(DOMAIN, {})
        for entry_id, entry_data in domain_data.items():
            if entry_id == self.config_entry.entry_id or not isinstance(
                entry_data, dict
            ):
                continue
            other_coord = entry_data.get(DATA_COORDINATOR)
            if other_coord and hasattr(other_coord, "config_entry"):
                if other_coord.config_entry.options.get(CONF_MQTT_PRESENCE, False):
                    other_whitelist = other_coord._async_get_tracked_devices_whitelist()
                    if other_whitelist is None or mac in other_whitelist:
                        other_entry_tracks_device = True
                        break

        # Only clear status topics if no other config entry is tracking this device
        if not other_entry_tracks_device:
            status_topics = [
                f"presence/{mac_safe}",
                f"presence/{mac_safe}/attributes",
                f"presence/{mac_colons}",
                f"presence/{mac_colons}/attributes",
                f"openwrt/presence/{mac_safe}",
                f"openwrt/presence/{mac_safe}/attributes",
                f"openwrt/presence/{mac_colons}",
                f"openwrt/presence/{mac_colons}/attributes",
            ]
            for topic in status_topics:
                _LOGGER.debug("Clearing MQTT status topic: %s", topic)
                try:
                    await self.hass.services.async_call(
                        "mqtt",
                        "publish",
                        {
                            "topic": topic,
                            "payload": "",
                            "retain": True,
                        },
                    )
                except Exception:
                    pass

        if mac in self._mqtt_discovered:
            self._mqtt_discovered.remove(mac)

    async def _async_global_registry_cleanup(self) -> None:
        """Scan ALL entities for MQTT zombies and remove them."""
        _LOGGER.debug("Starting global MQTT registry cleanup")
        ent_reg = er.async_get(self.hass)
        entries = er.async_entries_for_config_entry(ent_reg, self.config_entry.entry_id)

        # 1. Collect all prefixes this router might have used in the past
        mac_safe = self.router_id.replace(":", "_")
        mac_no_colons = self.router_id.replace(":", "").lower()
        mac_6chars = mac_no_colons[-6:].upper()
        router_id_safe = re.sub(r"[^a-zA-Z0-9_-]", "_", str(self.router_id))

        router_prefixes = [
            f"openwrt_track_{router_id_safe}_",
            f"openwrt_track_{mac_safe}_",
            f"openwrt_track_{mac_6chars}_",
            f"{router_id_safe}_{mac_safe}_",
            f"openwrt_{mac_safe}_",
            f"openwrt_{mac_6chars}_",
        ]

        # 2. Collect all MACs from this router's history
        known_macs = set()
        for mac in self._device_history:
            known_macs.add(mac.replace(":", "_").lower())
            known_macs.add(mac.replace(":", "").lower())

        # 3. Check all entries in registry for this config entry
        for entry in entries:
            # We only care about device_tracker entities from MQTT
            if entry.domain == "device_tracker" and entry.platform == "mqtt":
                unique_id = entry.unique_id.lower()
                entity_id = entry.entity_id.lower()

                # Rule 1: Starts with our router-specific prefix?
                is_match = any(unique_id.startswith(p) for p in router_prefixes)

                # Rule 2: Contains one of our known MACs in a safe format?
                if not is_match:
                    for m in known_macs:
                        if len(m) < 8:  # Skip fragments
                            continue
                        if m in unique_id or m in entity_id:
                            is_match = True
                            break

                if is_match:
                    _LOGGER.debug(
                        "Removing zombie MQTT entity from registry: %s (unique_id=%s)",
                        entry.entity_id,
                        unique_id,
                    )
                    try:
                        ent_reg.async_remove(entry.entity_id)
                    except Exception as err:
                        _LOGGER.debug(
                            "Failed to remove entity %s: %s", entry.entity_id, err
                        )

    async def _async_discovery_mqtt_device(self, mac: str, hostname: str) -> None:
        """Send MQTT discovery message for a device tracker."""
        if not self.hass.services.has_service("mqtt", "publish"):
            return
        if mac in self._mqtt_discovered:
            return

        mac_safe = mac.replace(":", "_")
        discovery_topic = f"homeassistant/device_tracker/openwrt_mqtt_{mac_safe}/config"
        _LOGGER.debug(
            "Sending MQTT discovery for %s (%s) to %s", hostname, mac, discovery_topic
        )

        payload = {
            "name": f"{hostname} MQTT",
            "state_topic": f"presence/{mac_safe}",
            "json_attributes_topic": f"presence/{mac_safe}/attributes",
            "unique_id": f"openwrt_track_{mac_safe}",
            "payload_home": "home",
            "payload_not_home": "not_home",
            "source_type": "router",
            "device": {
                "connections": [["mac", mac]],
                "identifiers": [f"openwrt_{mac}"],
                "name": hostname,
            },
        }

        try:
            await self.hass.services.async_call(
                "mqtt",
                "publish",
                {
                    "topic": discovery_topic,
                    "payload": json.dumps(payload),
                    "retain": True,
                },
            )
            self._mqtt_discovered.add(mac)
            _LOGGER.info(
                "Sent MQTT discovery for %s (%s) to %s", hostname, mac, discovery_topic
            )
        except Exception as err:
            _LOGGER.error("Failed to send MQTT discovery for %s: %s", mac, err)
