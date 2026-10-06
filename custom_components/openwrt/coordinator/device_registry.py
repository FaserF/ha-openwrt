"""Home Assistant device registry mixin for OpenWrt coordinator."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from homeassistant.const import CONF_HOST
from homeassistant.helpers import (
    device_registry as dr,
)
from homeassistant.helpers import (
    entity_registry as er,
)

from .. import coordinator
from ..api.base import OpenWrtData
from ..const import (
    ATTR_MANUFACTURER,
    CONF_MQTT_PRESENCE,
    CONF_SKIP_RANDOM_MAC,
    CONF_TRACK_DEVICES,
    DEFAULT_SKIP_RANDOM_MAC,
    DEFAULT_TRACK_DEVICES,
    DOMAIN,
)
from ..helpers import (
    _lookup_device,
    format_ap_device_id,
    format_ap_name,
    format_ap_stable_id,
    format_radio_device_id,
    format_radio_name,
    is_random_mac,
    normalize_band,
)
from ..helpers.mac_vendor import get_mac_vendor_info

if TYPE_CHECKING:
    from .base import CoordinatorBase

    _Base = CoordinatorBase
else:
    _Base = object

_LOGGER = logging.getLogger(__name__)


class DeviceRegistryMixin(_Base):
    """Mixin for updating Home Assistant device registry with router, APs, radios and clients."""

    def _async_reparent_connected_devices(
        self,
        data: OpenWrtData,
        device_registry: dr.DeviceRegistry,
    ) -> None:
        """Move existing wireless client devices below their current SSID."""
        get_by_connection = getattr(
            device_registry, "async_get_device_by_connection", None
        )

        for connected in data.connected_devices:
            if (
                not connected.mac
                or not connected.connected
                or not connected.is_wireless
            ):
                continue

            mac = connected.mac.lower()
            client_device = _lookup_device(
                device_registry,
                (DOMAIN, mac),
                self.config_entry.entry_id,
            )
            if client_device is None:
                if get_by_connection is not None:
                    client_device = get_by_connection(
                        (dr.CONNECTION_NETWORK_MAC, mac), self.config_entry.entry_id
                    )
                elif hasattr(device_registry, "async_get_devices"):
                    conns = device_registry.async_get_devices(
                        connections={(dr.CONNECTION_NETWORK_MAC, mac)},
                        config_entry_id=self.config_entry.entry_id,
                    )
                    client_device = conns[0] if conns else None
                elif hasattr(device_registry.devices, "get_entry"):
                    client_device = device_registry.devices.get_entry(  # type: ignore[call-arg]
                        connections={(dr.CONNECTION_NETWORK_MAC, mac)},
                        config_entry_id=self.config_entry.entry_id,
                    )
                else:
                    client_device = device_registry.async_get_device(
                        connections={(dr.CONNECTION_NETWORK_MAC, mac)}
                    )
            if client_device is None:
                continue

            parent_identifier = coordinator.get_via_device(
                self.hass,
                self,
                self.config_entry,
                mac,
            )
            parent_device = _lookup_device(
                device_registry,
                parent_identifier,
                self.config_entry.entry_id,
            )
            if (
                parent_device is not None
                and client_device.via_device_id != parent_device.id
            ):
                device_registry.async_update_device(
                    client_device.id,
                    via_device_id=parent_device.id,
                )

    async def _async_update_device_registry(self, data: OpenWrtData) -> None:
        """Update the device registry with fresh device information."""
        if not data.device_info:
            return

        device_info = data.device_info
        device_registry = dr.async_get(self.hass)
        skip_random = self.config_entry.options.get(
            CONF_SKIP_RANDOM_MAC, DEFAULT_SKIP_RANDOM_MAC
        )

        # Identify gateway device for topology mapping
        via_device_id: str | None = None
        if device_info.gateway_mac:
            gw_mac = device_info.gateway_mac.lower()
            if hasattr(device_registry, "async_get_devices"):
                # The gateway is another config entry's device (the upstream
                # router, or another integration's), so search every entry.
                # async_get_device_by_connection only searches the one given.
                # Skip this entry's own devices: a client device it tracks for
                # the gateway's MAC is not the gateway, and would make a loop.
                entry_id = self.config_entry.entry_id
                gw_dev = next(
                    (
                        dev
                        for dev in device_registry.async_get_devices(
                            connections={(dr.CONNECTION_NETWORK_MAC, gw_mac)}
                        )
                        if getattr(dev, "config_entry_id", None) != entry_id
                        and entry_id not in getattr(dev, "config_entries", ())
                    ),
                    None,
                )
                if gw_dev:
                    via_device_id = gw_dev.id
            else:
                dev_devices = getattr(device_registry, "devices", None)
                devices_iterable = (
                    dev_devices.values()
                    if isinstance(dev_devices, dict)
                    else (dev_devices or ())
                )
                for item in devices_iterable:
                    dev = (
                        device_registry.async_get(item)
                        if isinstance(item, str)
                        else item
                    )
                    if not dev:
                        continue
                    if any(
                        conn[0] == dr.CONNECTION_NETWORK_MAC
                        and conn[1].lower() == gw_mac
                        for conn in dev.connections
                    ):
                        via_device_id = dev.id
                        break

        # Prefer MAC address for router identity to ensure consistency with legacy devices
        if device_info.mac_address:
            mac_id = dr.format_mac(device_info.mac_address)
            if (
                self.router_id != mac_id
                and self.router_id.replace(":", "").lower()
                != mac_id.replace(":", "").lower()
            ):
                _LOGGER.debug(
                    "Updating router identity for registry cleanup",
                )
                self.router_id = mac_id
                # Update config entry unique_id if it's missing or differs from normalized mac_id
                if (
                    not self.config_entry.unique_id
                    or self.config_entry.unique_id != mac_id
                ):
                    self.hass.config_entries.async_update_entry(
                        self.config_entry, unique_id=mac_id
                    )

        _LOGGER.debug(
            "Updating device registry entry for router: model=%s",
            device_info.model,
        )

        # Combine both MAC and IP identifiers to ensure stable device association
        # during migration and consistent lookup.
        identifiers = {(DOMAIN, self.router_id)}
        if self.router_id != self.config_entry.data[CONF_HOST]:
            identifiers.add((DOMAIN, self.config_entry.data[CONF_HOST]))
        # Ensure we always add the original unique_id to prevent duplicate unmapped devices
        if (
            self.config_entry.unique_id
            and self.config_entry.unique_id != self.router_id
        ):
            identifiers.add((DOMAIN, self.config_entry.unique_id))

        # Determine current name to prevent downgrading to less descriptive versions
        current_name = None
        existing_device = next(
            (
                dev
                for ident in identifiers
                if (
                    dev := _lookup_device(
                        device_registry, ident, self.config_entry.entry_id
                    )
                )
                is not None
            ),
            None,
        )

        if existing_device and existing_device.name:
            current_name = existing_device.name

        new_name = device_info.model or device_info.hostname or self.config_entry.title
        # If AX3600 is reported but Xiaomi AX3600 is currently set, stick with Xiaomi
        if (
            current_name
            and new_name
            and len(current_name) > len(new_name)
            and new_name.lower() in current_name.lower()
        ):
            new_name = current_name

        router_entry = device_registry.async_get_or_create(
            config_entry_id=self.config_entry.entry_id,
            identifiers=identifiers,
            connections=(
                {(dr.CONNECTION_NETWORK_MAC, device_info.mac_address.lower())}
                if device_info.mac_address
                else None
            ),
            manufacturer=device_info.release_distribution or ATTR_MANUFACTURER,
            model=device_info.model or device_info.board_name,
            name=new_name,
            sw_version=device_info.firmware_version,
            hw_version=device_info.board_name,
            via_device_id=via_device_id,  # type: ignore[call-arg]
            configuration_url=f"http://{self.config_entry.data[CONF_HOST]}",
        )

        # 2. Register physical radios and AP devices for wireless interfaces.
        radio_info: dict[str, str] = {}
        for wifi in data.wireless_interfaces:
            if not wifi.radio:
                continue
            band = (
                normalize_band(wifi.band or wifi.frequency)
                if wifi.band or wifi.frequency
                else ""
            )
            radio_info.setdefault(wifi.radio, band)

        radio_devices: dict[str, dr.DeviceEntry] = {}
        for radio, band in radio_info.items():
            label = format_radio_name(radio, band)
            manufacturer = device_info.release_distribution or ATTR_MANUFACTURER
            radio_device = device_registry.async_get_or_create(
                config_entry_id=self.config_entry.entry_id,
                identifiers={(DOMAIN, format_radio_device_id(self.router_id, radio))},
                name=label,
                manufacturer=manufacturer,
                model="Wireless Radio",
                via_device_id=router_entry.id,  # type: ignore[call-arg]
            )
            radio_devices[radio] = radio_device
            # async_get_or_create() preserves an existing device name. Explicitly
            # migrate names created by earlier integration versions while leaving a
            # user's name_by_user override untouched.
            if (
                radio_device.name != label
                or radio_device.manufacturer != manufacturer
                or radio_device.model != "Wireless Radio"
            ):
                device_registry.async_update_device(
                    radio_device.id,
                    name=label,
                    manufacturer=manufacturer,
                    model="Wireless Radio",
                )

        # Ensure stable_id is based on SSID and Band to prevent duplicates
        # for mesh routers that spawn multiple virtual interfaces per radio.
        ap_info: dict[str, tuple[str, str]] = {}

        for wifi in data.wireless_interfaces:
            # Skip interfaces without name or SSID
            if not wifi.name or not wifi.ssid:
                continue

            # Use the normalised band string ("2.4 GHz", "5 GHz", "6 GHz") rather
            # than the raw frequency in MHz. This groups all virtual interfaces on
            # the same radio+SSID combination under one stable AP device, even
            # when different channels are reported across updates.
            band = (
                normalize_band(wifi.band or wifi.frequency)
                if wifi.band or wifi.frequency
                else ""
            )
            label = format_ap_name(wifi.ssid, band)

            # Group virtual interfaces only within the same physical radio.
            stable_id = format_ap_stable_id(wifi.ssid, band, wifi.radio)
            self.interface_to_stable_id[wifi.name] = stable_id
            if wifi.section:
                self.interface_to_stable_id[wifi.section] = stable_id
            if wifi.ifname:
                self.interface_to_stable_id[wifi.ifname] = stable_id
            ap_info[stable_id] = (label, wifi.radio)

        for stable_id, (label, radio) in ap_info.items():
            ssid_device = device_registry.async_get_or_create(
                config_entry_id=self.config_entry.entry_id,
                identifiers={(DOMAIN, format_ap_device_id(self.router_id, stable_id))},
                name=label,
                manufacturer=device_info.release_distribution or ATTR_MANUFACTURER,
                model="Wireless SSID",
                via_device_id=radio_devices[radio].id  # type: ignore[call-arg]
                if radio and radio in radio_devices
                else router_entry.id,
            )
            if radio and radio in radio_devices:
                device_registry.async_update_device(
                    ssid_device.id,
                    name=label,
                    manufacturer=device_info.release_distribution or ATTR_MANUFACTURER,
                    model="Wireless SSID",
                    via_device_id=radio_devices[radio].id,
                )

        self._async_reparent_connected_devices(data, device_registry)

        # 3. Retroactively update manufacturer/model for already-registered tracked devices.
        # HA only writes manufacturer/model at first creation; subsequent coordinator polls
        # are ignored unless we call async_update_device() explicitly. This loop fixes all
        # devices that were registered before the OUI mapping was added or that were created
        # with the generic "OpenWrt" / "Tracked device" defaults.
        mac_pattern = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$", re.IGNORECASE)
        for dev in dr.async_entries_for_config_entry(
            device_registry, self.config_entry.entry_id
        ):
            # Skip the root router device itself and merged/auxiliary entries
            if (
                dev.via_device_id is None
                or dev.disabled_by is not None
                or dev.entry_type is not None
            ):
                continue

            # Only proceed if the device still has placeholder manufacturer/model
            if not (
                dev.manufacturer in (None, "OpenWrt", "by OpenWrt", "manufacturer")
                or dev.model in (None, "Tracked device", "model")
            ):
                continue

            for ident in dev.identifiers:
                if ident[0] != DOMAIN:
                    continue
                ident_str = str(ident[1])
                if not mac_pattern.match(ident_str):
                    continue

                vendor_info = get_mac_vendor_info(ident_str)
                if not vendor_info:
                    break

                new_manufacturer, new_model = vendor_info
                # Only write if the values differ from the current ones
                if dev.manufacturer != new_manufacturer or dev.model != new_model:
                    _LOGGER.debug(
                        "Updating tracked device %s: manufacturer %s -> %s, model %s -> %s",
                        ident_str,
                        dev.manufacturer,
                        new_manufacturer,
                        dev.model,
                        new_model,
                    )
                    device_registry.async_update_device(
                        dev.id,
                        manufacturer=new_manufacturer,
                        model=new_model,
                    )
                break

        # 4. Cleanup orphaned devices
        # We scan the ENTIRE registry for devices that belong to this router
        # but are no longer active. This catches ghosts from previous installations.
        active_identifiers = {(DOMAIN, self.router_id)}
        for stable_id in ap_info.keys():
            active_identifiers.add(
                (DOMAIN, format_ap_device_id(self.router_id, stable_id))
            )
        for radio in radio_info:
            active_identifiers.add(
                (DOMAIN, format_radio_device_id(self.router_id, radio))
            )

        # Kept for async_remove_config_entry_device, which must refuse to delete
        # a device this entry is still providing.
        self.active_device_identifiers = active_identifiers

        _LOGGER.debug(
            "Starting deep device registry cleanup for %s active identifiers",
            len(active_identifiers),
        )

        # A client device is created by its tracker entity, but outlives it: once
        # the entity is gone (removed by hand, or never recreated because the MAC
        # fell outside the whitelist) the device stays behind with nothing on it.
        # Only sweep these while a whitelist is in force -- without one every
        # client is fair game and the device will be refilled on the next poll.
        whitelist = None
        if self.config_entry.options.get(
            CONF_TRACK_DEVICES, DEFAULT_TRACK_DEVICES
        ) or self.config_entry.options.get(CONF_MQTT_PRESENCE, False):
            whitelist = self._async_get_tracked_devices_whitelist()
        entity_registry = er.async_get(self.hass)
        untracked_client_devices: list[dr.DeviceEntry] = []

        devices_to_remove = []
        # Iterate over all devices for this config entry
        for dev in dr.async_entries_for_config_entry(
            device_registry, self.config_entry.entry_id
        ):
            # Check if any identifier belonging to our domain matches this router
            is_ours = False
            is_tracked_device = False
            tracked_mac = None

            for ident in dev.identifiers:
                if ident[0] == DOMAIN:
                    ident_str = str(ident[1])
                    norm_ident = ident_str.replace(":", "").lower()
                    norm_router_id = self.router_id.replace(":", "").lower()
                    norm_host = str(self.config_entry.data.get(CONF_HOST, "")).lower()
                    norm_unique_id = (
                        str(self.config_entry.unique_id or "").replace(":", "").lower()
                    )

                    if (
                        ident_str == self.router_id
                        or ident_str == self.config_entry.data.get(CONF_HOST)
                        or ident_str == self.config_entry.unique_id
                        or ident_str.startswith(f"{self.router_id}_")
                        or norm_ident == norm_router_id
                        or (norm_host and norm_ident == norm_host)
                        or (norm_unique_id and norm_ident == norm_unique_id)
                        or norm_ident.startswith(f"{norm_router_id}_")
                        or (
                            norm_unique_id
                            and norm_ident.startswith(f"{norm_unique_id}_")
                        )
                    ):
                        is_ours = True
                    elif re.match(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$", ident_str):
                        is_tracked_device = True
                        tracked_mac = ident_str

            if not is_ours and not is_tracked_device:
                continue

            # If it's one of our currently active APs or the router itself, keep it
            is_active = False
            if is_ours:
                for ident in dev.identifiers:
                    if ident[0] == DOMAIN:
                        norm_id = str(ident[1]).replace(":", "").lower()
                        if any(
                            str(act[1]).replace(":", "").lower() == norm_id
                            for act in active_identifiers
                        ):
                            is_active = True
                            break
            if is_active:
                continue

            if (
                whitelist
                and is_tracked_device
                and tracked_mac
                and tracked_mac.lower() not in whitelist
                and not any(
                    ent.config_entry_id == self.config_entry.entry_id
                    for ent in er.async_entries_for_device(
                        entity_registry, dev.id, include_disabled_entities=True
                    )
                )
            ):
                _LOGGER.info(
                    "Removing untracked client device '%s' (id: %s): not in "
                    "tracked_devices and no entities left",
                    dev.name,
                    dev.id,
                )
                untracked_client_devices.append(dev)
                continue

            # Identify if this is an Access Point device (old or new style)
            # We also check the model and name as a fallback for old/migrated installations
            raw_name = getattr(dev, "name", None)
            dev_name: str = str(raw_name) if isinstance(raw_name, str) else ""
            if not dev_name:
                mock_name = getattr(dev, "_mock_name", None)
                if isinstance(mock_name, str):
                    dev_name = mock_name
            is_ap_related = (
                any(
                    "_ap_" in str(ident[1])
                    for ident in dev.identifiers
                    if ident[0] == DOMAIN
                )
                or dev.model in ("Access Point", "Wireless SSID")
                or dev_name.startswith("AP ")
            )
            # Ghost names are unconfigured or legacy placeholder names (e.g., default_radio0, wifinet0, or bare 'radio').
            # Legitimate SSIDs containing those words (e.g., 'SSID my_wifinet') and physical radios ('radio0', '2.4 GHz')
            # must not be treated as ghosts.
            is_ghost_name = bool(
                re.match(
                    r"^(?:(?:SSID|AP)\s+)?(default_radio\d*|wifinet\d*|radio)(?:\s+\([^)]+\))?$",
                    dev_name,
                    re.IGNORECASE,
                )
            )

            # Identify if this is a randomized MAC device and skip_random is enabled
            is_random_tracked = False
            if skip_random and is_tracked_device and tracked_mac:
                if is_random_mac(tracked_mac):
                    is_random_tracked = True

            # Outage & reboot resilience guard:
            # 1. Never purge legitimate AP or radio devices when wireless data is empty (reboot / wifi restart).
            # 2. Never purge legitimate named SSIDs or physical radios; only remove ghost names.
            if is_ap_related or dev.model in (
                "Access Point",
                "Wireless SSID",
                "Wireless Radio",
            ):
                if not is_ghost_name and not (data.wireless_interfaces and ap_info):
                    continue

            # 3. Do not remove a physical radio device solely because another AP is active.
            #    Preserve legitimate physical radio devices (e.g., radio0, 2.4 GHz).
            if dev.model == "Wireless Radio" or any(
                "_radio_" in str(ident[1])
                for ident in dev.identifiers
                if ident[0] == DOMAIN
            ):
                if not is_ghost_name:
                    continue

            if is_ap_related or is_ghost_name or is_random_tracked:
                _LOGGER.info(
                    "Removing orphaned/ghost/randomized device '%s' (id: %s, identifiers: %s)",
                    dev_name or dev.id,
                    dev.id,
                    dev.identifiers,
                )

                # If it's a tracked device, we might only want to remove our config entry from it
                # if other integrations also track it. But async_remove_device is simpler and
                # usually what the user wants for randomized MACs to clear them out.
                devices_to_remove.append(dev.id)

        # Get the ID of our main router device to use as a fallback for orphans
        router_dev = _lookup_device(
            device_registry,
            (DOMAIN, self.router_id),
            self.config_entry.entry_id,
        )
        router_dev_id = router_dev.id if router_dev else None

        # Build a mapping of via_device_id to find children efficiently without nested loops
        via_map: dict[str, list[Any]] = {}
        if router_dev_id:
            for other_dev in dr.async_entries_for_config_entry(
                device_registry, self.config_entry.entry_id
            ):
                if other_dev and other_dev.via_device_id:
                    via_map.setdefault(other_dev.via_device_id, []).append(other_dev)

        for dev_id in devices_to_remove:
            # Before removing, check if any other devices are connected via this one
            # and redirect them to the router if possible using our pre-built map.
            if router_dev_id and dev_id in via_map:
                for child in via_map[dev_id]:
                    _LOGGER.info(
                        "Redirecting device '%s' via_device_id to router before removing ghost AP",
                        child.name,
                    )
                    device_registry.async_update_device(
                        child.id, via_device_id=router_dev_id
                    )

            device_registry.async_remove_device(dev_id)

        # Since HA 2026.9 a device belongs to exactly one config entry, so ours can
        # simply be removed. Before that the same MAC could be one device shared with
        # another router or integration; there, only detach this entry from it.
        for dev in untracked_client_devices:
            if set(dev.config_entries) - {self.config_entry.entry_id}:
                device_registry.async_update_device(
                    dev.id, remove_config_entry_id=self.config_entry.entry_id
                )
            else:
                device_registry.async_remove_device(dev.id)
