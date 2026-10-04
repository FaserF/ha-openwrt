"""Device tracker entity and attributes helper for OpenWrt."""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta
from typing import Any


def _get_now():
    mod = sys.modules.get("custom_components.openwrt.device_tracker")
    dt = getattr(mod, "datetime", datetime)
    return dt.now()


from homeassistant.components.device_tracker import (
    ScannerEntity,
    SourceType,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers import (
    device_registry as dr,
)
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity


def _get_device_info(*args: Any, **kwargs: Any) -> DeviceInfo:
    mod = sys.modules.get("custom_components.openwrt.device_tracker")
    cls = getattr(mod, "DeviceInfo", DeviceInfo)
    return cls(*args, **kwargs)


from ..const import (
    ATTR_MANUFACTURER,
    CONF_CONSIDER_HOME,
    CONF_TRACK_WIRED,
    CONF_TRUST_STALE_ARP,
    DEFAULT_CONSIDER_HOME,
    DEFAULT_TRACK_WIRED,
    DOMAIN,
)
from ..coordinator import OpenWrtDataCoordinator
from ..helpers import get_via_device_id, is_random_mac, resolve_client_name
from ..helpers.mac_vendor import get_mac_vendor_info

_LOGGER = logging.getLogger(__name__)


def compute_device_tracker_attrs(
    coordinator: OpenWrtDataCoordinator,
    mac: str,
    device: Any | None,
) -> dict[str, Any]:
    """Compute extra state attributes for an OpenWrt device tracker entity."""
    domain_data = coordinator.hass.data.get(DOMAIN, {})
    wireless_states = domain_data.get("tracker_wireless_state", {})
    state_info = wireless_states.get(mac)

    attrs: dict[str, Any] = {
        "mac": mac,
    }

    if device:
        attrs.update(
            {
                "is_wireless": device.is_wireless,
                "connection_type": device.connection_type,
            }
        )
    elif state_info:
        attrs.update(
            {
                "is_wireless": True,
                "connection_type": state_info.get("connection_type"),
            }
        )
    elif mac in coordinator._device_history:
        is_wl = bool(coordinator._device_history[mac].get("is_wireless", False))
        attrs.update(
            {
                "is_wireless": is_wl,
                "connection_type": "wireless" if is_wl else "wired",
            }
        )

    # Add attribution attributes
    if state_info and state_info.get("connected"):
        attrs.update(
            {
                "connected_ap": state_info.get("connected_ap"),
                "connected_ap_entry_id": state_info.get("connected_ap_entry_id"),
                "interface": state_info.get("interface"),
                "signal_strength": state_info.get("signal_strength"),
                "connection_type": state_info.get("connection_type"),
            }
        )
    elif device:
        attrs.update(
            {
                "interface": device.interface,
                "signal_strength": device.signal,
                "connected_ap": None,
            }
        )
    else:
        attrs["connected_ap"] = None

    # Add historical seen data
    if mac in coordinator._device_history:
        history = coordinator._device_history[mac]
        attrs.update(
            {
                "initially_seen": datetime.fromtimestamp(
                    history["initially_seen"]
                ).isoformat(),
                "last_seen": datetime.fromtimestamp(history["last_seen"]).isoformat(),
            }
        )

    # Add optional metrics
    optional_metrics: dict[str, Any] = {}
    if device:
        optional_metrics.update(
            {
                "port": device.port,
                "fdb_age": device.fdb_age,
                "rx_bytes": device.rx_bytes,
                "tx_bytes": device.tx_bytes,
                "uptime": device.uptime,
                "neighbor_state": device.neighbor_state,
                "connection_info": device.connection_info,
            }
        )
        if device.is_wireless:
            optional_metrics.update(
                {
                    "signal_strength": device.signal,
                    "rx_rate": device.rx_rate,
                    "tx_rate": device.tx_rate,
                }
            )
    elif state_info and state_info.get("connected"):
        optional_metrics["signal_strength"] = state_info.get("signal_strength")

    attrs.update(
        {k: v for k, v in optional_metrics.items() if v is not None and v != ""}
    )

    # Add Mesh info
    if coordinator.data and mac in coordinator.data.batman_translation_table:
        originator_mac = coordinator.data.batman_translation_table[mac]
        attrs["mesh_node"] = originator_mac
        if originator_mac.lower() != coordinator.data.device_info.mac_address.lower():
            attrs["is_via_mesh"] = True

    return attrs


class OpenWrtDeviceTracker(CoordinatorEntity[OpenWrtDataCoordinator], ScannerEntity):
    """Representation of a tracked device on the OpenWrt router."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: OpenWrtDataCoordinator,
        entry: ConfigEntry,
        mac: str,
        hostname: str | None = None,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._mac = mac.lower()
        self._entry = entry
        self._attr_unique_id = f"openwrt_tracker_{self._mac}"

        if is_random_mac(self._mac):
            self._attr_entity_registry_enabled_default = False

        # Initial device name fallback
        self._initial_name = hostname or mac
        options = getattr(entry, "options", {})
        if not hasattr(options, "get"):
            options = {}
        data = getattr(entry, "data", {})
        if not hasattr(data, "get"):
            data = {}
        self._consider_home = timedelta(
            seconds=options.get(
                CONF_CONSIDER_HOME,
                data.get(CONF_CONSIDER_HOME, DEFAULT_CONSIDER_HOME),
            ),
        )
        self._last_seen: datetime | None = None

        # Track all created entity instances globally to enable cross-entry peer notification
        domain_data = coordinator.hass.data.setdefault(DOMAIN, {})
        all_trackers = domain_data.setdefault("all_trackers", {})
        trackers = all_trackers.setdefault(self._mac, [])
        if self not in trackers:
            trackers.append(self)

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info."""
        manufacturer = ATTR_MANUFACTURER
        model = "Tracked device"

        if vendor_info := get_mac_vendor_info(self._mac):
            manufacturer, model = vendor_info

        return _get_device_info(
            connections={(dr.CONNECTION_NETWORK_MAC, self._mac)},
            identifiers={(DOMAIN, self._mac)},
            name=self.name or self._initial_name,
            manufacturer=manufacturer,
            model=model,
            via_device_id=get_via_device_id(
                self.coordinator.hass, self.coordinator, self._entry, self._mac
            ),
        )

    @property
    def source_type(self) -> SourceType:
        """Return source type."""
        return SourceType.ROUTER

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        if getattr(self, "hass", None):
            self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        """Call when entity is being removed from hass."""
        await super().async_will_remove_from_hass()
        domain_data = self.coordinator.hass.data.get(DOMAIN, {})
        if "registered_macs" in domain_data:
            domain_data["registered_macs"].discard(self._mac)
        if "all_trackers" in domain_data and self._mac in domain_data["all_trackers"]:
            if self in domain_data["all_trackers"][self._mac]:
                domain_data["all_trackers"][self._mac].remove(self)

    @property
    def is_connected(self) -> bool:
        """Return connection status."""
        domain_data = self.coordinator.hass.data.get(DOMAIN, {})
        wireless_states = domain_data.get("tracker_wireless_state", {})
        state_info = wireless_states.get(self._mac)

        if state_info and "connected" in state_info:
            return self._check_consider_home(state_info["connected"])

        device = self._raw_get_device_data()
        if not device:
            return self._check_consider_home(False)

        wireless_history = domain_data.get("wireless_history", {})
        device_hist = getattr(self.coordinator, "_device_history", {})
        if not isinstance(device_hist, dict):
            device_hist = {}
        was_ever_wireless = wireless_history.get(self._mac) or device_hist.get(
            self._mac, {}
        ).get("is_wireless", False)

        if was_ever_wireless and not device.is_wireless:
            return self._check_consider_home(False)

        def _config_get(key: str, default: Any = None) -> Any:
            entry = self._entry
            options = getattr(entry, "options", None)
            if isinstance(options, dict) and key in options:
                return options[key]
            data = getattr(entry, "data", None)
            if isinstance(data, dict) and key in data:
                return data[key]
            if options is not None and hasattr(options, "get"):
                try:
                    val = options.get(key)
                    if val is not None and type(val).__name__ != "MagicMock":
                        return val
                except Exception:
                    pass
            if data is not None and hasattr(data, "get"):
                try:
                    val = data.get(key)
                    if val is not None and type(val).__name__ != "MagicMock":
                        return val
                except Exception:
                    pass
            return default

        trust_stale = _config_get(CONF_TRUST_STALE_ARP, True)
        valid_arp_states = ["REACHABLE", "DELAY", "PROBE", "PERMANENT"]
        if trust_stale:
            valid_arp_states.append("STALE")

        if device.neighbor_state:
            active_arp = device.neighbor_state.upper() in valid_arp_states
            if not active_arp:
                return self._check_consider_home(False)
        elif (device.is_wireless or was_ever_wireless) and not trust_stale:
            return self._check_consider_home(False)

        track_wired = _config_get(CONF_TRACK_WIRED, DEFAULT_TRACK_WIRED)
        if not track_wired and not device.is_wireless:
            return self._check_consider_home(False)

        return self._check_consider_home(device.connected)

    def _raw_get_device_data(self) -> Any | None:
        """Get raw device data from own coordinator."""
        if not self.coordinator.data:
            return None
        return next(
            (
                d
                for d in self.coordinator.data.connected_devices
                if d.mac and d.mac.lower() == self._mac
            ),
            None,
        )

    _get_device_data = _raw_get_device_data

    def _check_consider_home(self, connected: bool) -> bool:
        """Apply consider_home logic."""
        now = _get_now()
        if connected:
            self._last_seen = now
            return True

        return bool(self._last_seen and now - self._last_seen < self._consider_home)

    @property
    def mac_address(self) -> str:
        """Return MAC."""
        return self._mac

    @property
    def hostname(self) -> str | None:
        """Return hostname."""
        device = self._raw_get_device_data()
        if device and device.hostname:
            return device.hostname
        if self.coordinator.data:
            for lease in self.coordinator.data.dhcp_leases:
                if lease.mac and lease.mac.lower() == self._mac and lease.hostname:
                    return lease.hostname
        return None

    @property
    def ip_address(self) -> str | None:
        """Return IP."""
        device = self._raw_get_device_data()
        if device and device.ip:
            return device.ip
        if self.coordinator.data:
            for lease in self.coordinator.data.dhcp_leases:
                if lease.mac and lease.mac.lower() == self._mac and lease.ip:
                    return lease.ip
        return None

    @property
    def name(self) -> str:
        """Return name."""
        hostname = self.hostname
        if hostname and hostname != "*":
            router_hostname = ""
            if self.coordinator.data and self.coordinator.data.device_info:
                router_hostname = self.coordinator.data.device_info.hostname

            if hostname != router_hostname:
                return hostname

        return resolve_client_name(self.coordinator.hass, self._mac, self._initial_name)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return attributes."""
        device = self._raw_get_device_data()
        return compute_device_tracker_attrs(self.coordinator, self._mac, device)
