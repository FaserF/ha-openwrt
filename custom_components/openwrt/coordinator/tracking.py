"""Device tracking mixin for OpenWrt coordinator."""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.const import CONF_HOST
from homeassistant.core import callback

from ..api.base import ConnectedDevice, OpenWrtData
from ..const import (
    CONF_CONSIDER_HOME,
    CONF_FORCE_WIRELESS_MACS,
    CONF_MQTT_PRESENCE,
    CONF_SKIP_RANDOM_MAC,
    CONF_TRACK_DEVICES,
    DEFAULT_CONSIDER_HOME,
    DEFAULT_SKIP_RANDOM_MAC,
    DEFAULT_TRACK_DEVICES,
    DOMAIN,
)
from ..helpers import is_random_mac

if TYPE_CHECKING:
    from .base import CoordinatorBase

    _Base = CoordinatorBase
else:
    _Base = object

_LOGGER = logging.getLogger(__name__)


def _clean_hostname_entry(mac: Any, name: Any) -> tuple[str, str] | None:
    """Normalize one MAC -> hostname pair, or return None if unusable.

    Consumers look the registry up by lowercase MAC, so a stored uppercase key
    would silently never resolve. dnsmasq's "*" placeholder is not a name.
    """
    if not isinstance(mac, str) or not isinstance(name, str):
        return None
    mac_lower = mac.strip().lower()
    name = name.strip()
    if not name or name == "*":
        return None
    if not re.fullmatch(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", mac_lower):
        return None
    return mac_lower, name


class TrackingMixin(_Base):
    """Mixin for device filtering, hostname registry, and presence tracking."""

    def _get_own_macs(self, data: OpenWrtData) -> set[str]:
        """Collect all MAC addresses belonging to the router itself."""
        own_macs = {m.lower() for m in data.local_macs if m}
        if data.device_info.mac_address:
            own_macs.add(data.device_info.mac_address.lower())
        for iface in data.network_interfaces:
            if iface.mac_address:
                own_macs.add(iface.mac_address.lower())
        for wifi_iface in data.wireless_interfaces:
            if wifi_iface.mac_address:
                own_macs.add(wifi_iface.mac_address.lower())
        return own_macs

    async def _async_load_hostname_registry(self) -> None:
        """Load the shared MAC -> hostname registry, once per HA run."""
        domain_data = self.hass.data.setdefault(DOMAIN, {})
        if "hostname_registry" in domain_data:
            return

        # Claim the slot synchronously before the first await, so a concurrent
        # entry setup cannot load it a second time and swap out the dict that
        # other code is already holding a reference to.
        registry: dict[str, str] = {}
        domain_data["hostname_registry"] = registry

        try:
            stored = await self._hostname_store.async_load()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Could not load shared hostname registry: %s", err)
            return

        if isinstance(stored, dict):
            for mac, name in stored.items():
                if cleaned := _clean_hostname_entry(mac, name):
                    registry[cleaned[0]] = cleaned[1]
            _LOGGER.debug("Loaded %d shared hostnames", len(registry))

    async def _async_filter_and_track_devices(self, data: OpenWrtData) -> None:
        """Filter and track devices."""
        await self._async_load_hostname_registry()
        hostname_registry: dict[str, str] = self.hass.data[DOMAIN]["hostname_registry"]
        registry_dirty = False

        # Load history if needed
        if not self._device_history:
            stored_data = await self._store.async_load()
            if stored_data:
                # History is written in two shapes: an envelope with a "devices"
                # key, and a bare mapping. Decode both, as _async_setup does --
                # assigning the envelope raw would make "devices" and
                # "last_version" look like MAC addresses.
                loaded = stored_data
                if isinstance(stored_data, dict) and "devices" in stored_data:
                    loaded = stored_data.get("devices", {})
                if isinstance(loaded, dict):
                    self._device_history = {
                        mac: hist
                        for mac, hist in loaded.items()
                        if isinstance(hist, dict)
                    }

                # Seed the shared registry from this entry's persisted names too,
                # so a fresh install with no shared store still benefits.
                for mac, hist in self._device_history.items():
                    cleaned = _clean_hostname_entry(mac, hist.get("hostname"))
                    if cleaned and cleaned[0] not in hostname_registry:
                        hostname_registry[cleaned[0]] = cleaned[1]
                        registry_dirty = True

        # Stabilize wireless connection states (prevent flapping to 0 due to transient packet drops or RPC glitches)
        if self.data and self.data.all_connected_devices:
            prev_wireless = {
                d.mac.lower(): d
                for d in self.data.all_connected_devices
                if d.mac and d.is_wireless and d.connected
            }
            if prev_wireless:
                current_time = time.time()
                consider_home = self.config_entry.options.get(
                    CONF_CONSIDER_HOME,
                    self.config_entry.data.get(
                        CONF_CONSIDER_HOME, DEFAULT_CONSIDER_HOME
                    ),
                )

                # Update last seen time for currently connected wireless devices
                for device in data.connected_devices:
                    if device.mac:
                        mac_lower = device.mac.lower()
                        if device.connected and device.is_wireless:
                            self._wireless_last_seen[mac_lower] = current_time

                # For any device that was previously connected wireless:
                # If it's now missing or marked disconnected, keep it connected if within the consider_home window
                current_macs = {d.mac.lower() for d in data.connected_devices if d.mac}
                for mac_lower, prev_dev in prev_wireless.items():
                    if mac_lower not in self._wireless_last_seen:
                        self._wireless_last_seen[mac_lower] = current_time

                    last_seen = self._wireless_last_seen[mac_lower]
                    if current_time - last_seen < consider_home:
                        if mac_lower in current_macs:
                            device = next(
                                d
                                for d in data.connected_devices
                                if d.mac and d.mac.lower() == mac_lower
                            )
                            if not device.connected or not device.is_wireless:
                                device.connected = True
                                device.is_wireless = True
                                device.interface = (
                                    device.interface or prev_dev.interface
                                )
                                device.connection_type = (
                                    device.connection_type or prev_dev.connection_type
                                )
                                device.signal = device.signal or prev_dev.signal
                                device.noise = device.noise or prev_dev.noise
                                device.rx_rate = device.rx_rate or prev_dev.rx_rate
                                device.tx_rate = device.tx_rate or prev_dev.tx_rate
                        else:
                            # Restore missing device as connected
                            restored_device = ConnectedDevice(
                                mac=prev_dev.mac,
                                ip=prev_dev.ip,
                                hostname=prev_dev.hostname,
                                connected=True,
                                is_wireless=True,
                                interface=prev_dev.interface,
                                connection_type=prev_dev.connection_type,
                                signal=prev_dev.signal,
                                noise=prev_dev.noise,
                                rx_rate=prev_dev.rx_rate,
                                tx_rate=prev_dev.tx_rate,
                            )
                            data.connected_devices.append(restored_device)

        own_macs = self._get_own_macs(data)
        own_ips = data.local_ips
        current_time = int(time.time())
        history_updated = False
        skip_random = self.config_entry.options.get(
            CONF_SKIP_RANDOM_MAC, DEFAULT_SKIP_RANDOM_MAC
        )

        whitelist = None
        if self.config_entry.options.get(
            CONF_TRACK_DEVICES, DEFAULT_TRACK_DEVICES
        ) or self.config_entry.options.get(CONF_MQTT_PRESENCE, False):
            whitelist = self._async_get_tracked_devices_whitelist()

        # Load forced wireless MACs
        forced_wireless = set()
        forced_wireless_raw = self.config_entry.options.get(
            CONF_FORCE_WIRELESS_MACS, ""
        )
        if forced_wireless_raw:
            for line in forced_wireless_raw.splitlines():
                mac_f = line.strip().lower()
                if mac_f:
                    forced_wireless.add(mac_f)

        # MAC -> hostname map shared by every config entry in this HA instance.
        # A dumb AP sees associations but runs no DHCP server, so on its own it
        # can only ever label a client by MAC; this lets it borrow the name the
        # DHCP router already resolved.

        # all_devices: passes internal filters but ignores the tracking whitelist.
        # Used by the Connected Clients / Wireless Clients count sensors so they
        # always reflect total router occupancy, not just the selected tracked set.
        # filtered_devices: additionally requires whitelist membership; used for
        # device_tracker entities and history.
        all_devices: list = []
        filtered_devices = []
        for device in data.connected_devices:
            if not device.mac:
                continue
            mac = device.mac.lower()
            # 1. Filter out router's own interfaces (always)
            if mac in own_macs:
                continue

            # Filter out randomized MACs if option is set
            if is_random_mac(mac):
                if skip_random:
                    _LOGGER.debug(
                        "Skipping randomized MAC device (option enabled): %s", mac
                    )
                    continue
                _LOGGER.debug(
                    "Keeping randomized MAC device (option disabled): %s", mac
                )

            # Filter out router's own IP addresses
            if device.ip and device.ip in own_ips:
                continue

            # Filter out internal interface names masquerading as hostnames
            if device.hostname:
                hostname = device.hostname.lower()
                # Enhanced regex to catch more interface-like names (wlan0, eth0.1, br-lan, etc.)
                if re.match(
                    r"^(wlan|eth|lan|wan|br-|radio|phy|veth|lo|bond|team)[0-9]*([.-].*)?$",
                    hostname,
                ):
                    continue

            # Filter if hostname is identical to the interface name (likely self-reported neighbor)
            if (
                device.interface
                and device.hostname
                and device.interface.lower() == device.hostname.lower()
            ):
                continue

            # Device passes internal filters — count it in the totals regardless of whitelist
            all_devices.append(device)

            # Publish the hostname to the registry shared by every config entry.
            # Deliberately before the whitelist check: an AP with no DHCP server
            # of its own can only ever name a client from what another router
            # learned, so the registry has to cover devices this entry does not
            # track itself.
            if cleaned := _clean_hostname_entry(mac, device.hostname):
                if hostname_registry.get(cleaned[0]) != cleaned[1]:
                    hostname_registry[cleaned[0]] = cleaned[1]
                    registry_dirty = True

            # Filter by whitelist if configured
            if whitelist and mac not in whitelist:
                _LOGGER.debug(
                    "Skipping device %s: not in tracked_devices whitelist", mac
                )
                if self.config_entry.options.get(CONF_MQTT_PRESENCE, False):
                    if (
                        mac in self._mqtt_discovered
                        or mac.lower() in self._mqtt_discovered
                    ):
                        await self._async_discovery_mqtt_device_cleanup(mac)
                continue

            # Handle MQTT Discovery if enabled. Prefer a name another entry
            # resolved -- _mqtt_discovered suppresses a later republish, so a
            # MAC published now would stick.
            if self.config_entry.options.get(CONF_MQTT_PRESENCE, False):
                await self._async_discovery_mqtt_device(
                    mac, hostname_registry.get(mac) or mac
                )

            filtered_devices.append(device)

            _LOGGER.debug(
                "Processing connected device: %s (hostname: %s, interface: %s, wireless: %s, connected: %s, state: %s)",
                mac,
                device.hostname,
                device.interface,
                device.is_wireless or mac in forced_wireless,
                device.connected,
                device.neighbor_state,
            )

            # Apply forced wireless flag
            if mac in forced_wireless:
                device.is_wireless = True

            # dnsmasq reports "*" for clients that sent no hostname; treat it as
            # absent so it never ends up as a device label.
            hostname = "" if device.hostname == "*" else device.hostname

            if mac not in self._device_history:
                self._device_history[mac] = {
                    "initially_seen": current_time,
                    "last_seen": current_time,
                    "is_wireless": device.is_wireless,
                    "hostname": hostname,
                }
                history_updated = True
                _LOGGER.debug("New device added to history: %s", mac)
            else:
                hist = self._device_history[mac]
                hist["last_seen"] = current_time
                # Persistence: if it was EVER wireless, it stays wireless in history
                # to avoid fake-wired entries from DHCP leases when offline.
                if device.is_wireless and not hist.get("is_wireless"):
                    hist["is_wireless"] = True
                # Keep the last known hostname so offline devices stay readable
                # in the options flow and MQTT discovery. Never overwrite a good
                # hostname with a missing one.
                if hostname:
                    hist["hostname"] = hostname
                history_updated = True

            # Sync with shared wireless history for multi-AP coordination
            if self._device_history[mac].get("is_wireless"):
                domain_data = self.hass.data.setdefault(DOMAIN, {})
                wireless_history = domain_data.setdefault("wireless_history", {})
                wireless_history[mac] = True

        data.all_connected_devices = all_devices
        data.connected_devices = filtered_devices

        # Leases are the richest name source, so harvest them before the
        # whitelist filter below discards the untracked ones.
        for lease in data.dhcp_leases:
            if cleaned := _clean_hostname_entry(lease.mac, lease.hostname):
                if hostname_registry.get(cleaned[0]) != cleaned[1]:
                    hostname_registry[cleaned[0]] = cleaned[1]
                    registry_dirty = True

        # Filter DHCP leases by whitelist
        if whitelist:
            data.dhcp_leases = [
                lease
                for lease in data.dhcp_leases
                if lease.mac and lease.mac.lower() in whitelist
            ]

        # Filter DHCP leases to prevent entities for internal interfaces (veth, wlanX, etc.)
        filtered_leases = []
        for lease in data.dhcp_leases:
            mac = lease.mac.lower()
            if mac in own_macs:
                continue
            if lease.ip and lease.ip in own_ips:
                continue
            if lease.hostname:
                hostname = lease.hostname.lower()
                if re.match(
                    r"^(wlan|eth|lan|wan|br-|radio|phy|veth|lo|bond|team)[0-9]*([.-].*)?$",
                    hostname,
                ):
                    continue

            # Filter out randomized MACs if option is set
            if skip_random and is_random_mac(mac):
                continue

            _LOGGER.debug(
                "Processing DHCP lease: %s (hostname: %s, ip: %s)",
                mac,
                lease.hostname,
                lease.ip,
            )

            # "*" means the client sent no hostname — see the connected device loop
            lease_hostname = "" if lease.hostname == "*" else lease.hostname

            # Ensure lease devices are also in history so they are discovered as trackers
            if mac not in self._device_history:
                is_wireless = is_random_mac(mac)
                self._device_history[mac] = {
                    "initially_seen": current_time,
                    "last_seen": current_time,
                    "is_wireless": is_wireless,
                    "hostname": lease_hostname,
                }
                history_updated = True
                _LOGGER.debug(
                    "New lease-only device added to history: %s (guessed wireless: %s)",
                    mac,
                    is_wireless,
                )
            else:
                self._device_history[mac]["last_seen"] = current_time
                if lease_hostname:
                    self._device_history[mac]["hostname"] = lease_hostname
                history_updated = True

            filtered_leases.append(lease)
        data.dhcp_leases = filtered_leases

        if history_updated:
            await self._store.async_save(self._device_history)

        if registry_dirty:
            try:
                await self._hostname_store.async_save(hostname_registry)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Could not save shared hostname registry: %s", err)

        # Handle MQTT Discovery (Start or Cleanup)
        # Initial MQTT discovery if enabled, or cleanup if disabled
        if self.config_entry.options.get(CONF_MQTT_PRESENCE, False):
            self._mqtt_presence_configured = True
            self._mqtt_cleanup_done = False
            if not self._mqtt_discovery_started:
                self._mqtt_discovery_started = True
                self.hass.async_create_task(self._async_discovery_loop(clean=False))
        else:
            # If MQTT is disabled, only clean up if it was previously configured/enabled
            # or if a pending cleanup has not yet completed.
            if self._mqtt_presence_configured and not self._mqtt_cleanup_done:
                self.hass.async_create_task(self._async_discovery_loop(clean=True))

    @callback
    def _async_update_global_wireless_state(self, data: OpenWrtData) -> None:
        """Update global wireless state for device trackers."""
        domain_data = self.hass.data.setdefault(DOMAIN, {})
        wireless_states = domain_data.setdefault("tracker_wireless_state", {})
        all_trackers = domain_data.get("all_trackers", {})

        affected_macs: set[str] = set()

        for device in data.connected_devices:
            if not device.mac:
                continue
            mac = device.mac.lower()

            # Authority: Only wireless connected devices update the state
            if device.is_wireless and device.connected:
                ap_name = self.config_entry.title or self.config_entry.data.get(
                    CONF_HOST
                )
                wireless_states[mac] = {
                    "owner_entry_id": self.config_entry.entry_id,
                    "last_seen": datetime.now(),
                    "connected": True,
                    "connected_ap": ap_name,
                    "connected_ap_entry_id": self.config_entry.entry_id,
                    "interface": device.interface,
                    "signal_strength": device.signal,
                    "connection_type": device.connection_type,
                }
                affected_macs.add(mac)
            elif (
                wireless_states.get(mac, {}).get("owner_entry_id")
                == self.config_entry.entry_id
            ):
                # If we were the owner but no longer see it wireless/connected, mark as away
                if wireless_states[mac].get("connected"):
                    wireless_states[mac]["connected"] = False
                    affected_macs.add(mac)

        # 4. Global cleanup for stale entries (safety timeout)
        # If an AP goes offline or is removed, its owned devices might stay 'home' forever.
        # We clear any entry that hasn't been updated by ANYONE for more than 10 minutes.
        stale_threshold = datetime.now() - timedelta(minutes=10)
        for mac, state in list(wireless_states.items()):
            if state.get("last_seen", datetime.min) < stale_threshold:
                if state.get("connected"):
                    _LOGGER.debug(
                        "Clearing stale global wireless state for %s (no update for 10m)",
                        mac,
                    )
                    state["connected"] = False
                    affected_macs.add(mac)

        # Notify all trackers for the affected MACs
        for mac in affected_macs:
            trackers = all_trackers.get(mac, [])
            for tracker in trackers:
                if tracker.hass:
                    tracker.async_write_ha_state()
