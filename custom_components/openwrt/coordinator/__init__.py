"""Data update coordinator for OpenWrt integration.

Manages periodic data fetching from the OpenWrt device and firmware
update checking against the official OpenWrt release API.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    device_registry as dr,
)
from homeassistant.helpers import (
    entity_registry as er,
)
from homeassistant.helpers import (
    storage,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from ..api.base import OpenWrtClient, OpenWrtData
from ..api.luci_rpc import (
    LuciRpcAuthError,
    LuciRpcError,
    LuciRpcPackageMissingError,
)
from ..api.ssh import SshAuthError, SshError
from ..api.ubus import (
    UbusAuthError,
    UbusConnectionError,
    UbusError,
    UbusPackageMissingError,
    UbusTimeoutError,
)
from ..const import (
    CONF_CONNECTION_TYPE,
    CONF_CONSIDER_HOME,
    CONF_ENABLE_NLBWMON_SENSORS,
    CONF_ENABLE_SNORT_SENSORS,
    CONF_GPS_MODEM_ENABLED,
    CONF_GPS_MODEM_PORT,
    CONF_GPS_POLL_INTERVAL,
    CONF_MANUAL_TRACKED_DEVICES,
    CONF_MQTT_PRESENCE,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_REVERSE_DNS,
    CONF_SKIP_RANDOM_MAC,
    CONF_SSH_KEY,
    CONF_TRACK_DEVICES,
    CONF_TRACKED_DEVICES,
    CONF_UBUS_PATH,
    CONF_UPDATE_INTERVAL,
    CONF_USE_SSL,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
    DATA_COORDINATOR,
    DEFAULT_CONSIDER_HOME,
    DEFAULT_GPS_MODEM_PORT,
    DEFAULT_GPS_POLL_INTERVAL,
    DEFAULT_REVERSE_DNS,
    DEFAULT_SKIP_RANDOM_MAC,
    DEFAULT_TRACK_DEVICES,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
)
from ..helpers import format_ap_device_id, get_via_device
from ..repairs import (
    async_create_auth_repair,
    async_create_connection_lost_repair,
    async_create_missing_packages_repair,
    async_delete_connection_lost_repair,
)
from .client import create_client
from .device_registry import DeviceRegistryMixin
from .discovery import DiscoveryMixin
from .features import FeaturesMixin
from .firmware import FIRMWARE_CHECK_INTERVAL, SNAPSHOT_TARGET_MAP, FirmwareMixin
from .network import NetworkMixin
from .tracking import TrackingMixin, _clean_hostname_entry

__all__ = [
    "CONF_CONNECTION_TYPE",
    "CONF_CONSIDER_HOME",
    "CONF_ENABLE_NLBWMON_SENSORS",
    "CONF_ENABLE_SNORT_SENSORS",
    "CONF_GPS_MODEM_ENABLED",
    "CONF_GPS_MODEM_PORT",
    "CONF_GPS_POLL_INTERVAL",
    "CONF_MANUAL_TRACKED_DEVICES",
    "CONF_MQTT_PRESENCE",
    "CONF_PASSWORD",
    "CONF_PORT",
    "CONF_REVERSE_DNS",
    "CONF_SKIP_RANDOM_MAC",
    "CONF_SSH_KEY",
    "CONF_TRACK_DEVICES",
    "CONF_TRACKED_DEVICES",
    "CONF_UBUS_PATH",
    "CONF_UPDATE_INTERVAL",
    "CONF_USE_SSL",
    "CONF_USERNAME",
    "CONF_VERIFY_SSL",
    "DATA_COORDINATOR",
    "DEFAULT_CONSIDER_HOME",
    "DEFAULT_GPS_MODEM_PORT",
    "DEFAULT_GPS_POLL_INTERVAL",
    "DEFAULT_REVERSE_DNS",
    "DEFAULT_SKIP_RANDOM_MAC",
    "DEFAULT_TRACK_DEVICES",
    "DEFAULT_UPDATE_INTERVAL",
    "DOMAIN",
    "DeviceRegistryMixin",
    "DiscoveryMixin",
    "FIRMWARE_CHECK_INTERVAL",
    "FeaturesMixin",
    "FirmwareMixin",
    "NetworkMixin",
    "OpenWrtDataCoordinator",
    "SNAPSHOT_TARGET_MAP",
    "TrackingMixin",
    "_clean_hostname_entry",
    "create_client",
    "dr",
    "er",
    "format_ap_device_id",
    "get_via_device",
]

_LOGGER = logging.getLogger(__name__)


class OpenWrtDataCoordinator(
    FeaturesMixin,
    NetworkMixin,
    TrackingMixin,
    DiscoveryMixin,
    DeviceRegistryMixin,
    FirmwareMixin,
    DataUpdateCoordinator[OpenWrtData],
):
    """Coordinator for fetching data from an OpenWrt device."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        client: OpenWrtClient,
    ) -> None:
        """Initialize."""
        self.client = client
        self.client.coordinator = self
        self.hass = hass
        self.config_entry = config_entry
        self.in_reboot_installation = False
        self._firmware_checked = False
        # Initialize firmware check timer to avoid blocking initial startup with external GitHub/ASU API calls
        self._last_firmware_check: float = (
            self.hass.loop.time() if self.hass and self.hass.loop else 0.0
        )
        self._last_gps_check: float = -86400.0  # Force check on startup
        self._last_update_time: float = 0.0
        self._device_history: dict[str, dict[str, Any]] = {}
        self._wireless_last_seen: dict[str, float] = {}
        self._prev_network_stats: dict[str, dict[str, int]] = {}
        self._mqtt_discovered: set[str] = set()
        self._mqtt_discovery_started = False
        self._mqtt_cleanup_done = False
        self._mqtt_presence_configured = False
        # Interface name to stable identifier mapping (for AP devices)
        self.interface_to_stable_id: dict[str, str] = {}
        self.active_device_identifiers: set[tuple[str, str]] = set()
        unique_id = self.config_entry.unique_id
        if unique_id and len(unique_id.replace(":", "")) == 12:
            try:
                self.router_id = dr.format_mac(unique_id)
            except Exception:
                self.router_id = unique_id
        else:
            self.router_id = unique_id or self.config_entry.data[CONF_HOST]

        self._last_version: str | None = None
        self._boot_time: datetime | None = None
        self._last_uptime: int | None = None
        # MWAN3 availability since the router last booted, kept on the
        # router's own monotonic uptime rather than on a wall clock.
        self._mwan_first_online: dict[str, float] = {}
        self._mwan_offline_total: dict[str, float] = {}
        self._mwan_offline_since: dict[str, float] = {}
        self._mwan_last_uptime: dict[str, float] = {}
        self._mwan_online_start: dict[str, float] = {}
        self._mwan_sample: dict[str, tuple[float, float, float]] = {}
        self._mwan_boot_epoch: float | None = None
        self._mwan_totals_loaded = False
        self._mwan_store: storage.Store = storage.Store(
            hass,
            1,
            f"{DOMAIN}_{config_entry.entry_id}_mwan_totals",
        )
        self._store: storage.Store = storage.Store(
            hass,
            1,
            f"{DOMAIN}_{config_entry.entry_id}_history",
        )
        # Shared by every entry, not per-entry: an access point has no hostnames
        # of its own and must be able to read names the DHCP router resolved
        # *before* its entities are created, otherwise it registers its devices
        # under bare MAC addresses and the names never get rewritten.
        # One Store instance for the whole domain, not one per entry: separate
        # Store objects on the same key can interleave their writes and land an
        # older snapshot last.
        self._hostname_store: storage.Store = hass.data.setdefault(
            DOMAIN, {}
        ).setdefault("hostname_store", storage.Store(hass, 1, f"{DOMAIN}_hostnames"))

        update_interval = self.config_entry.options.get(
            CONF_UPDATE_INTERVAL,
            self.config_entry.data.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL),
        )
        self._configured_update_interval = update_interval
        self._current_backoff_interval = update_interval

        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=config_entry.data.get(CONF_HOST, "unknown"),
            update_interval=timedelta(seconds=update_interval),
        )

    async def _async_setup(self) -> None:
        """Set up (connect to device)."""
        # Load history and version from storage
        try:
            stored_data = await self._store.async_load()
            if stored_data:
                loaded_devices = {}
                if isinstance(stored_data, dict) and "devices" in stored_data:
                    loaded_devices = stored_data.get("devices", {})
                    self._last_version = stored_data.get("last_version")
                    self._mqtt_cleanup_done = stored_data.get(
                        "mqtt_cleanup_done", False
                    )
                    self._mqtt_presence_configured = stored_data.get(
                        "mqtt_presence_configured", False
                    )
                elif isinstance(stored_data, dict):
                    # Legacy structure (direct dict of devices)
                    loaded_devices = stored_data

                if isinstance(loaded_devices, dict):
                    for mac, hist in loaded_devices.items():
                        if isinstance(hist, dict):
                            self._device_history[mac] = hist

                _LOGGER.debug(
                    "Loaded %s devices from persistent history (last_version: %s)",
                    len(self._device_history),
                    self._last_version,
                )

                # Populate shared wireless history
                domain_data = self.hass.data.setdefault(DOMAIN, {})
                wireless_history = domain_data.setdefault("wireless_history", {})
                for mac, hist in self._device_history.items():
                    if hist.get("is_wireless"):
                        wireless_history[mac.lower()] = True
        except Exception as err:
            _LOGGER.warning("Could not load persistent history: %s", err)

        # Try to connect
        for attempt in range(1, 4):
            try:
                _LOGGER.debug(
                    "Connecting to OpenWrt device (attempt %s/3)",
                    attempt,
                )
                if not self.client.connected:
                    await self.client.connect()

                self.last_update_success = True
                _LOGGER.info("Successfully connected to OpenWrt device")
                break
            except Exception as err:
                if attempt < 3:
                    _LOGGER.warning(
                        "Initial connection failed, retrying in 5s: %s",
                        err,
                    )
                    await asyncio.sleep(5)
                else:
                    _LOGGER.warning(
                        "Initial connection failed after 3 attempts: %s. "
                        "Integration will retry in the background.",
                        err,
                    )
                    self.last_update_success = False

    async def _async_update_data(self) -> OpenWrtData:
        """Fetch data."""
        try:
            data = await self._async_fetch_all_data()
            await self._async_resolve_reverse_dns(data)
            if self.in_reboot_installation:
                _LOGGER.info(
                    "Device rebooted successfully after firmware update, connection restored."
                )
                self.in_reboot_installation = False
            # Reset backoff on success
            if self._current_backoff_interval != self._configured_update_interval:
                self._current_backoff_interval = self._configured_update_interval
                self.update_interval = timedelta(
                    seconds=self._configured_update_interval
                )
                _LOGGER.info(
                    "Connection re-established, resetting update interval to default (%s s)",
                    self._configured_update_interval,
                )
        except Exception as err:
            # Double backoff up to 10 minutes (600 seconds)
            self._current_backoff_interval = min(
                self._current_backoff_interval * 2, 600
            )
            self.update_interval = timedelta(seconds=self._current_backoff_interval)
            if self.in_reboot_installation:
                _LOGGER.info(
                    "Device is rebooting due to firmware update (%s). Polling again in %s seconds.",
                    err,
                    self._current_backoff_interval,
                )
            else:
                _LOGGER.warning(
                    "Update failed: %s. Backing off next poll to %s seconds",
                    err,
                    self._current_backoff_interval,
                )
            raise UpdateFailed(f"Error fetching data: {err}") from err

        async_delete_connection_lost_repair(self.hass, self.config_entry)

        self._async_sync_firmware_state(data)

        # Periodic firmware checks (wrapped in try-except to prevent crashing the whole coordinator)
        now = self.hass.loop.time()
        if now - self._last_firmware_check > FIRMWARE_CHECK_INTERVAL.total_seconds():
            self._last_firmware_check = now
            try:
                await self._check_firmware_update(data)
            except Exception as err:
                _LOGGER.debug("Firmware update check failed: %s", err)

        # Calculate stabilized boot time
        uptime = data.system_resources.uptime
        if uptime > 0:
            utc_now = dt_util.utcnow()
            # Calculate what the boot time would be based on current uptime
            boot_time_raw = utc_now - timedelta(seconds=uptime)
            # Round to the nearest minute. Rounding to the full hour once
            # uptime exceeded 3600 s discarded the minute component, so a
            # router booted at 05:02 was reported as 05:00, and one booted
            # at 05:47 also as 05:00 - an error of up to 59 minutes.
            # Adding 30 s before truncating rounds to the nearest minute
            # rather than always flooring, which would report a boot time
            # up to 59 s early. Poll jitter is already absorbed by the
            # >60 s stabilization check below, which is what keeps the
            # sensor from flickering.
            new_boot_time = (boot_time_raw + timedelta(seconds=30)).replace(
                second=0, microsecond=0
            )

            # Stabilization logic:
            # If we don't have a boot time yet, set it.
            # If uptime decreased significantly (>10s), the router rebooted.
            # If the difference is significant (> 60s), update it (covers clock syncs/drift).
            # Otherwise, keep the old value to prevent sensor flickering from poll jitter.

            rebooted = self._last_uptime is not None and uptime < (
                self._last_uptime - 10
            )

            if self._boot_time is None or rebooted:
                if rebooted:
                    _LOGGER.info(
                        "Reboot detected on %s (uptime decreased from %s to %s)",
                        self.client.host,
                        self._last_uptime,
                        uptime,
                    )
                self._boot_time = new_boot_time
            else:
                diff = abs((new_boot_time - self._boot_time).total_seconds())
                if diff > 60:
                    _LOGGER.debug(
                        "Boot time drifted significantly (>60s), updating: %s",
                        self._boot_time,
                    )
                    self._boot_time = new_boot_time

            data.boot_time = self._boot_time
            self._last_uptime = uptime

            await self._async_update_mwan_boot_totals(data, uptime, rebooted)

        self._async_process_network_rates(data, now)
        self._last_update_time = now

        await self._async_update_device_registry(data)

        await self._async_filter_and_track_devices(data)

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
            _LOGGER.warning("Could not save persistent history: %s", err)

        self._async_check_stale_permissions(data)

        # Update global wireless state for device trackers
        self._async_update_global_wireless_state(data)

        # Fetch optional sub-features concurrently
        sub_tasks = []
        if self.config_entry.options.get(CONF_MQTT_PRESENCE, False):
            sub_tasks.append(self._async_fetch_mqtt_presence_data(data))

        if self.config_entry.options.get(
            CONF_ENABLE_NLBWMON_SENSORS,
            self.config_entry.data.get(CONF_ENABLE_NLBWMON_SENSORS, False),
        ):
            sub_tasks.append(self._async_fetch_nlbwmon_top_hosts_data(data))

        if self.config_entry.options.get(
            CONF_ENABLE_SNORT_SENSORS,
            self.config_entry.data.get(CONF_ENABLE_SNORT_SENSORS, False),
        ):
            sub_tasks.append(self._async_fetch_snort_data(data))

        if sub_tasks:
            await asyncio.gather(*sub_tasks, return_exceptions=True)

        if self.config_entry.options.get(CONF_GPS_MODEM_ENABLED, False):
            data.qmodem_info.enabled = True
            gps_port = self.config_entry.options.get(
                CONF_GPS_MODEM_PORT, DEFAULT_GPS_MODEM_PORT
            )
            gps_interval = self.config_entry.options.get(
                CONF_GPS_POLL_INTERVAL, DEFAULT_GPS_POLL_INTERVAL
            )
            now_time = self.hass.loop.time()
            if now_time - self._last_gps_check >= gps_interval:
                self._last_gps_check = now_time
                from .helpers.gps import async_update_gps_location

                data.qmodem_info.gps_last_update_attempted = dt_util.now()

                try:
                    res = await async_update_gps_location(
                        self.hass, self.client, gps_port
                    )
                    if res:
                        lat, lon, last_update = res
                        data.qmodem_info.gps_latitude = lat
                        data.qmodem_info.gps_longitude = lon
                        data.qmodem_info.gps_last_update = last_update
                        data.qmodem_info.gps_last_update_successful = last_update
                        data.qmodem_info.gps_last_update_ok = True
                    else:
                        data.qmodem_info.gps_last_update_ok = False
                except Exception as gps_err:
                    _LOGGER.debug("GPS location update failed: %s", gps_err)
                    data.qmodem_info.gps_last_update_ok = False

            # Preserve previous GPS data if we are not currently polling it
            if self.data and self.data.qmodem_info:
                if data.qmodem_info.gps_latitude is None:
                    data.qmodem_info.gps_latitude = self.data.qmodem_info.gps_latitude
                if data.qmodem_info.gps_longitude is None:
                    data.qmodem_info.gps_longitude = self.data.qmodem_info.gps_longitude
                if data.qmodem_info.gps_last_update is None:
                    val = self.data.qmodem_info.gps_last_update
                    if isinstance(val, str):
                        val = dt_util.parse_datetime(val)
                    data.qmodem_info.gps_last_update = val
                if data.qmodem_info.gps_last_update_successful is None:
                    val = self.data.qmodem_info.gps_last_update_successful
                    if isinstance(val, str):
                        val = dt_util.parse_datetime(val)
                    data.qmodem_info.gps_last_update_successful = val
                if data.qmodem_info.gps_last_update_attempted is None:
                    val = self.data.qmodem_info.gps_last_update_attempted
                    if isinstance(val, str):
                        val = dt_util.parse_datetime(val)
                    data.qmodem_info.gps_last_update_attempted = val
                if data.qmodem_info.gps_last_update_ok is None:
                    data.qmodem_info.gps_last_update_ok = (
                        self.data.qmodem_info.gps_last_update_ok
                    )

        return data

    async def _async_fetch_all_data(self) -> OpenWrtData:
        """Fetch all data with retry logic."""
        if not self.client.connected:
            try:
                await self.client.connect()
            except Exception as err:
                self.client._connected = False
                raise UpdateFailed(f"Cannot connect: {err}") from err

        try:
            _LOGGER.debug("Fetching all data from OpenWrt device")
            data = await self.client.get_all_data()

            # Robustness: If core components (interfaces) are missing but we either expect them
            # (initial fetch) or previously had them, retry once after a small delay.
            # This handles cases where the router is still starting services like rpcd/network.
            if not data.network_interfaces and (
                self.data is None or self.data.network_interfaces
            ):
                _LOGGER.debug(
                    "Fetched data is missing core network interfaces, retrying in 2s..."
                )
                await asyncio.sleep(2)
                data = await self.client.get_all_data()

            return data
        except (UbusAuthError, LuciRpcAuthError, SshAuthError) as err:
            self.client._connected = False
            async_create_auth_repair(self.hass, self.config_entry)
            raise UpdateFailed(
                "Authentication failed. Check your credentials."
            ) from err
        except (UbusPackageMissingError, LuciRpcPackageMissingError) as err:
            self.client._connected = False
            packages = (
                ["uhttpd-mod-ubus"] if "ubus" in str(err).lower() else ["luci-mod-rpc"]
            )
            async_create_missing_packages_repair(self.hass, self.config_entry, packages)
            raise UpdateFailed(f"Missing required OpenWrt package: {err}") from err
        except (
            TimeoutError,
            UbusTimeoutError,
            UbusConnectionError,
            UbusError,
            LuciRpcError,
            SshError,
            aiohttp.ClientError,
        ) as err:
            _LOGGER.debug("Data fetch failed, attempting reconnect and retry: %s", err)
            try:
                await self.client.connect()
                return await self.client.get_all_data()
            except Exception as retry_err:
                _LOGGER.warning("Updating data failed: %s", retry_err)
                self.client._connected = False
                async_create_connection_lost_repair(self.hass, self.config_entry)
                raise UpdateFailed(f"Error fetching data: {retry_err}") from retry_err
        except Exception as err:
            _LOGGER.exception("Unexpected error updating OpenWrt data: %s", err)
            self.client._connected = False
            raise UpdateFailed(f"Unexpected error: {err}") from err

    async def async_shutdown(self) -> None:
        """Shut down the coordinator and disconnect."""
        await super().async_shutdown()
        await self.client.disconnect()
