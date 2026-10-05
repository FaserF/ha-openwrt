"""Base coordinator protocol for mixin typing."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import storage
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from ..api.base import OpenWrtClient, OpenWrtData


class CoordinatorBase(DataUpdateCoordinator[OpenWrtData]):
    """Base coordinator interface providing type hints for mixins."""

    config_entry: ConfigEntry
    client: OpenWrtClient
    hass: HomeAssistant
    in_reboot_installation: bool
    _firmware_checked: bool
    _last_firmware_check: float
    _last_gps_check: float
    _last_update_time: float
    _device_history: dict[str, dict[str, Any]]
    _wireless_last_seen: dict[str, float]
    _prev_network_stats: dict[str, dict[str, int]]
    _mqtt_discovered: set[str]
    _mqtt_discovery_started: bool
    _mqtt_cleanup_done: bool
    _mqtt_presence_configured: bool
    interface_to_stable_id: dict[str, str]
    active_device_identifiers: set[tuple[str, str]]
    router_id: str
    _last_version: str | None
    _boot_time: datetime | None
    _last_uptime: int | None
    _mwan_first_online: dict[str, float]
    _mwan_offline_total: dict[str, float]
    _mwan_offline_since: dict[str, float]
    _mwan_last_uptime: dict[str, float]
    _mwan_online_start: dict[str, float]
    _mwan_sample: dict[str, tuple[float, float, float]]
    _mwan_boot_epoch: float | None
    _mwan_totals_loaded: bool
    _mwan_store: storage.Store
    _store: storage.Store
    _hostname_store: storage.Store
    _configured_update_interval: int
    _current_backoff_interval: int
    update_interval: timedelta | None

    def _get_own_macs(self, data: OpenWrtData) -> set[str]:
        """Collect all MAC addresses belonging to the router itself."""
        raise NotImplementedError

    def _async_get_tracked_devices_whitelist(self) -> set[str] | None:
        """Get the set of allowed MAC addresses for tracking."""
        raise NotImplementedError

    async def _async_discovery_mqtt_device(self, mac: str, hostname: str) -> None:
        """Send discovery message for a single device tracker."""
        raise NotImplementedError

    async def _async_discovery_mqtt_device_cleanup(self, mac: str) -> None:
        """Remove discovery and clean up state for a specific device."""
        raise NotImplementedError

    async def _async_discovery_loop(self, clean: bool = False) -> None:
        """Loop through history and discover or cleanup devices for MQTT."""
        raise NotImplementedError
