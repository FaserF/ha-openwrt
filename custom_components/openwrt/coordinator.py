"""Data update coordinator for OpenWrt integration.

Manages periodic data fetching from the OpenWrt device and firmware
update checking against the official OpenWrt release API.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import (
    device_registry as dr,
)
from homeassistant.helpers import (
    entity_registry as er,
)
from homeassistant.helpers import (
    storage,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api.base import OpenWrtClient, OpenWrtData
from .api.luci_rpc import (
    LuciRpcAuthError,
    LuciRpcClient,
    LuciRpcError,
    LuciRpcPackageMissingError,
)
from .api.ssh import SshAuthError, SshClient, SshError
from .api.ubus import (
    UbusAuthError,
    UbusClient,
    UbusConnectionError,
    UbusError,
    UbusPackageMissingError,
    UbusTimeoutError,
)
from .const import (
    ATTR_MANUFACTURER,
    CONF_ASU_URL,
    CONF_CONNECTION_TYPE,
    CONF_CUSTOM_FIRMWARE_REPO,
    CONF_DHCP_SOFTWARE,
    CONF_ENABLE_NLBWMON_SENSORS,
    CONF_FORCE_WIRELESS_MACS,
    CONF_MANUAL_TRACKED_DEVICES,
    CONF_MQTT_PRESENCE,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_SKIP_RANDOM_MAC,
    CONF_SSH_KEY,
    CONF_TARGET_OVERRIDE,
    CONF_TRACK_DEVICES,
    CONF_TRACKED_DEVICES,
    CONF_TRUST_BRIDGE_FDB,
    CONF_TRUST_STALE_ARP,
    CONF_UBUS_PATH,
    CONF_UPDATE_INTERVAL,
    CONF_USE_SSL,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
    CONNECTION_TYPE_LUCI_RPC,
    CONNECTION_TYPE_SSH,
    CONNECTION_TYPE_UBUS,
    DEFAULT_PORT_SSH,
    DEFAULT_PORT_UBUS,
    DEFAULT_PORT_UBUS_SSL,
    DEFAULT_SKIP_RANDOM_MAC,
    DEFAULT_TRACK_DEVICES,
    DEFAULT_UBUS_PATH,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    OPENWRT_RELEASE_API,
)
from .helpers import (
    format_ap_device_id,
    format_ap_name,
    is_random_mac,
)
from .helpers.mac_vendor import get_mac_vendor_info
from .repairs import (
    async_create_auth_repair,
    async_create_connection_lost_repair,
    async_create_missing_packages_repair,
    async_create_stale_permissions_repair,
    async_delete_connection_lost_repair,
    async_delete_stale_permissions_repair,
)

_LOGGER = logging.getLogger(__name__)

FIRMWARE_CHECK_INTERVAL = timedelta(hours=6)

# Map of legacy/deprecated snapshot targets to their modern equivalents.
# OpenWrt periodically consolidates targets (e.g. the AX generation moved to qualcommax).
SNAPSHOT_TARGET_MAP = {
    "ipq807x/generic": "qualcommax/ipq807x",
    "ipq60xx/generic": "qualcommax/ipq60xx",
    "ipq50xx/generic": "qualcommax/ipq50xx",
    "ipq806x/generic": "qualcommax/ipq806x",
    "mediatek/mt7981": "mediatek/filogic",
    "mediatek/mt7986": "mediatek/filogic",
    "mediatek/mt7622": "mediatek/filogic",
    "mediatek/mt7623": "mediatek/filogic",
    "rockchip/armv8": "rockchip/rk3328",
    "ipq807x": "qualcommax/ipq807x",
    "ipq60xx": "qualcommax/ipq60xx",
    "ipq50xx": "qualcommax/ipq50xx",
    "qualcommax/generic": "qualcommax/ipq807x",
}


def create_client(hass: HomeAssistant, config: Mapping[str, Any]) -> OpenWrtClient:
    """Create the appropriate API client based on configuration."""
    connection_type = config.get(CONF_CONNECTION_TYPE, CONNECTION_TYPE_UBUS)
    host = config[CONF_HOST]
    username = config[CONF_USERNAME]
    password = config.get(CONF_PASSWORD, "")
    use_ssl = config.get(CONF_USE_SSL, False)
    verify_ssl = config.get(CONF_VERIFY_SSL, False)
    dhcp_software = config.get(CONF_DHCP_SOFTWARE, "auto")

    trust_stale_arp = config.get(CONF_TRUST_STALE_ARP, True)
    trust_bridge_fdb = config.get(CONF_TRUST_BRIDGE_FDB, True)

    _LOGGER.debug("Creating client for router (type: %s)", connection_type)

    if connection_type == CONNECTION_TYPE_SSH:
        port = config.get(CONF_PORT, DEFAULT_PORT_SSH)
        return SshClient(
            hass=hass,
            session=None,
            host=host,
            username=username,
            password=password,
            port=port,
            ssh_key=config.get(CONF_SSH_KEY),
            dhcp_software=dhcp_software,
            trust_stale_arp=trust_stale_arp,
            trust_bridge_fdb=trust_bridge_fdb,
        )

    if connection_type == CONNECTION_TYPE_LUCI_RPC:
        port = config.get(
            CONF_PORT,
            DEFAULT_PORT_UBUS_SSL if use_ssl else DEFAULT_PORT_UBUS,
        )
        return LuciRpcClient(
            hass=hass,
            session=async_get_clientsession(hass),
            host=host,
            username=username,
            password=password,
            port=port,
            use_ssl=use_ssl,
            verify_ssl=verify_ssl,
            dhcp_software=dhcp_software,
            trust_stale_arp=trust_stale_arp,
            trust_bridge_fdb=trust_bridge_fdb,
        )

    port = config.get(
        CONF_PORT,
        DEFAULT_PORT_UBUS_SSL if use_ssl else DEFAULT_PORT_UBUS,
    )
    return UbusClient(
        hass=hass,
        session=async_get_clientsession(hass),
        host=host,
        username=username,
        password=password,
        port=port,
        use_ssl=use_ssl,
        verify_ssl=verify_ssl,
        ubus_path=config.get(CONF_UBUS_PATH, DEFAULT_UBUS_PATH),
        dhcp_software=dhcp_software,
        trust_stale_arp=trust_stale_arp,
        trust_bridge_fdb=trust_bridge_fdb,
    )


class OpenWrtDataCoordinator(DataUpdateCoordinator[OpenWrtData]):
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
        self._firmware_checked = False
        self._last_firmware_check: float = -86400.0  # Force check on startup
        self._last_update_time: float = 0.0
        self._device_history: dict[str, dict[str, Any]] = {}
        self._prev_network_stats: dict[str, dict[str, int]] = {}
        self._mqtt_discovered: set[str] = set()
        self._mqtt_discovery_started = False
        self._mqtt_cleanup_done = False
        # Interface name to stable identifier mapping (for AP devices)
        self.interface_to_stable_id: dict[str, str] = {}
        self.router_id = (
            self.config_entry.unique_id or self.config_entry.data[CONF_HOST]
        )
        self._last_version: str | None = None
        self._boot_time: datetime | None = None
        self._last_uptime: int | None = None
        self._store: storage.Store = storage.Store(
            hass,
            1,
            f"{DOMAIN}_{config_entry.entry_id}_history",
        )

        update_interval = config_entry.options.get(
            CONF_UPDATE_INTERVAL,
            config_entry.data.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL),
        )

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
                if isinstance(stored_data, dict) and "devices" in stored_data:
                    self._device_history.update(stored_data.get("devices", {}))
                    self._last_version = stored_data.get("last_version")
                else:
                    # Legacy structure (direct dict of devices)
                    self._device_history.update(stored_data)

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

        # Try to connect and perform first fetch
        for attempt in range(1, 4):
            try:
                _LOGGER.debug(
                    "Connecting to OpenWrt device (attempt %s/3)",
                    attempt,
                )
                if not self.client.connected:
                    await self.client.connect()

                # Also try an initial data fetch to populate the coordinator
                self.data = await self.client.get_all_data()
                if self.data:
                    if self.data.device_info:
                        self.data.firmware_current_version = (
                            self.data.device_info.firmware_version
                            or self.data.device_info.release_version
                        )
                    # Crucial: Populate interface mappings and register devices BEFORE platforms load
                    await self._async_update_device_registry(self.data)

                self.last_update_success = True
                _LOGGER.info("Successfully connected to OpenWrt device")
                break
            except Exception as err:
                if attempt < 3:
                    _LOGGER.warning(
                        "Initial connection/fetch failed, retrying in 5s: %s",
                        err,
                    )
                    await asyncio.sleep(5)
                else:
                    _LOGGER.warning(
                        "Initial connection/fetch failed after 3 attempts: %s. "
                        "Integration will retry in the background.",
                        err,
                    )
                    self.last_update_success = False

    async def _async_update_data(self) -> OpenWrtData:
        """Fetch data."""
        data = await self._async_fetch_all_data()

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
            if uptime >= 3600:
                # More than 1 hour: Round to start of hour to reduce state changes
                new_boot_time = boot_time_raw.replace(minute=0, second=0, microsecond=0)
            else:
                # Less than 1 hour: Round to start of minute
                new_boot_time = boot_time_raw.replace(second=0, microsecond=0)

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

        self._async_process_network_rates(data, now)
        self._last_update_time = now

        await self._async_update_device_registry(data)

        await self._async_filter_and_track_devices(data)

        try:
            await self._store.async_save(
                {
                    "devices": self._device_history,
                    "last_version": self._last_version,
                }
            )
        except Exception as err:
            _LOGGER.warning("Could not save persistent history: %s", err)

        self._async_check_stale_permissions(data)

        # Update global wireless state for device trackers
        self._async_update_global_wireless_state(data)

        if self.config_entry.options.get(CONF_MQTT_PRESENCE, False):
            try:
                await self._async_fetch_mqtt_presence_data(data)
            except Exception as err:
                _LOGGER.debug("MQTT presence data fetch failed: %s", err)

        if self.config_entry.options.get(
            CONF_ENABLE_NLBWMON_SENSORS,
            self.config_entry.data.get(CONF_ENABLE_NLBWMON_SENSORS, False),
        ):
            try:
                await self._async_fetch_nlbwmon_top_hosts_data(data)
            except Exception as err:
                _LOGGER.debug("nlbwmon top hosts fetch failed: %s", err)

        return data

    async def _async_fetch_mqtt_presence_data(self, data: OpenWrtData) -> None:
        """Fetch MQTT presence status and logs."""
        try:
            status_output = await self.client.execute_command(
                "/etc/init.d/presence_hostapd status 2>/dev/null"
            )
            data.mqtt_presence_status = (
                status_output.strip() if status_output else "stopped"
            )

            # Optimized log fetch: tail first, then grep
            logs_output = await self.client.execute_command(
                "logread | tail -n 100 | grep presence_event | tail -n 10"
            )
            data.mqtt_presence_logs = logs_output.splitlines() if logs_output else []
        except Exception as err:
            _LOGGER.debug("Failed to fetch MQTT presence data: %s", err)
            data.mqtt_presence_status = "error"
            data.mqtt_presence_logs = [str(err)]

    @staticmethod
    def _format_bytes(num_bytes: int) -> str:
        value = float(num_bytes)
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if value < 1024 or unit == "TB":
                return f"{int(value)} {unit}" if unit == "B" else f"{value:.2f} {unit}"
            value /= 1024
        return f"{num_bytes} B"

    async def _async_fetch_nlbwmon_top_hosts_data(self, data: OpenWrtData) -> None:
        """Fetch and parse nlbwmon top bandwidth hosts."""
        empty: dict[str, Any] = {
            "top_hosts": [],
            "host_count": 0,
            "total_rx_bytes": 0,
            "total_tx_bytes": 0,
        }
        result = await self.client.file_exec(
            "/usr/sbin/nlbw",
            ["-c", "json", "-g", "ip,mac", "-o", "-rx_bytes,-tx_bytes"],
        )
        _LOGGER.debug(
            "nlbwmon file_exec result keys: %s", list(result.keys()) if result else None
        )
        if not result:
            _LOGGER.info(
                "nlbwmon top hosts: file_exec returned empty — "
                "check that nlbwmon is installed and rpcd file ACL allows execution"
            )
            data.nlbwmon_top_hosts = empty
            return

        stdout = result.get("stdout", "")
        stderr = result.get("stderr", "")

        combined = (stdout + stderr).lower()
        if "permission denied" in combined or "access denied" in combined:
            _LOGGER.warning(
                "nlbwmon requires ubus file.exec permission for '/usr/sbin/nlbw' in rpcd ACL"
            )
            data.nlbwmon_top_hosts = empty
            return

        if not stdout:
            _LOGGER.info(
                "nlbwmon top hosts: empty stdout (code=%s, stderr=%r) — "
                "nlbw binary may not be installed or failed to run",
                result.get("code"),
                stderr[:200] if stderr else "",
            )
            data.nlbwmon_top_hosts = empty
            return

        _LOGGER.debug("nlbwmon raw stdout (first 500 chars): %.500s", stdout)

        try:
            raw = json.loads(stdout)
        except (json.JSONDecodeError, ValueError) as err:
            _LOGGER.error(
                "Failed to parse nlbwmon output: %s — stdout was: %.300s",
                err,
                stdout,
            )
            data.nlbwmon_top_hosts = empty
            return

        columns = raw.get("columns", [])
        rows = raw.get("data", [])
        _LOGGER.debug("nlbwmon columns=%s rows=%d", columns, len(rows))
        if not columns or not rows:
            _LOGGER.info(
                "nlbwmon returned no data rows (columns=%s, rows=%d) — "
                "nlbwmon may not have collected any traffic yet",
                columns,
                len(rows),
            )
            data.nlbwmon_top_hosts = empty
            return

        col = {name: idx for idx, name in enumerate(columns)}
        required = {"ip", "mac", "rx_bytes", "tx_bytes"}
        if not required.issubset(col.keys()):
            _LOGGER.error(
                "nlbwmon output missing required columns %s — got: %s",
                required - col.keys(),
                columns,
            )
            data.nlbwmon_top_hosts = empty
            return

        aggregated: dict[str, dict[str, Any]] = {}
        for row in rows:
            ip = row[col["ip"]] if "ip" in col else ""
            mac = (row[col["mac"]] if "mac" in col else "").upper()
            if mac == "00:00:00:00:00:00" and not ip:
                continue
            key = ip if mac == "00:00:00:00:00:00" else mac
            if key not in aggregated:
                aggregated[key] = {
                    "mac": mac,
                    "ip": ip,
                    "rx_bytes": 0,
                    "tx_bytes": 0,
                    "conns": 0,
                }
            aggregated[key]["rx_bytes"] += row[col["rx_bytes"]]
            aggregated[key]["tx_bytes"] += row[col["tx_bytes"]]
            if "conns" in col:
                aggregated[key]["conns"] += row[col["conns"]]
            current_ip = aggregated[key]["ip"]
            if ":" in current_ip and ":" not in ip and ip:
                aggregated[key]["ip"] = ip

        hostname_map: dict[str, str] = {}
        try:
            raw_leases = await self.client.get_dhcp_leases()
            for lease in raw_leases:
                if lease.mac and lease.hostname and lease.hostname != "*":
                    hostname_map[lease.mac.upper()] = lease.hostname
        except Exception:
            pass

        device_reg = dr.async_get(self.hass)
        for dev in device_reg.devices.values():
            name = dev.name_by_user or dev.name
            if not name:
                continue
            for conn_type, conn_mac in dev.connections:
                if conn_type == dr.CONNECTION_NETWORK_MAC:
                    mac_key = conn_mac.upper()
                    if mac_key and mac_key not in hostname_map:
                        hostname_map[mac_key] = name

        hosts = []
        for entry_data in aggregated.values():
            total = entry_data["rx_bytes"] + entry_data["tx_bytes"]
            if total == 0:
                continue
            mac = entry_data["mac"]
            hostname = hostname_map.get(mac) or entry_data["ip"] or mac or "Unknown"
            hosts.append({**entry_data, "total_bytes": total, "hostname": hostname})

        hosts.sort(key=lambda x: x["total_bytes"], reverse=True)

        top_hosts = [
            {
                "rank": i + 1,
                "hostname": h["hostname"],
                "ip": h["ip"],
                "mac": h["mac"],
                "connections": h["conns"],
                "rx_bytes": h["rx_bytes"],
                "tx_bytes": h["tx_bytes"],
                "total_bytes": h["total_bytes"],
                "download": self._format_bytes(h["rx_bytes"]),
                "upload": self._format_bytes(h["tx_bytes"]),
                "total": self._format_bytes(h["total_bytes"]),
            }
            for i, h in enumerate(hosts[:5])
        ]

        data.nlbwmon_top_hosts = {
            "top_hosts": top_hosts,
            "host_count": len(hosts),
            "total_rx_bytes": sum(h["rx_bytes"] for h in hosts),
            "total_tx_bytes": sum(h["tx_bytes"] for h in hosts),
        }

    def _async_check_stale_permissions(self, data: OpenWrtData) -> None:
        """Check for stale permissions."""
        username = self.config_entry.data.get(CONF_USERNAME, "homeassistant")
        if username == "root":
            return

        # Identify missing but expected permissions based on detected packages
        perms = data.permissions
        packages = data.packages

        stale = False
        reason = ""
        # We check for core features that indicate the RPC user needs more rights
        # than what were granted during its creation.
        if not perms.read_system:
            stale = True
            reason = "missing core system read permissions"
        elif packages.wireless and not perms.read_wireless:
            stale = True
            reason = "missing wireless read permissions (wireless package detected)"
        elif packages.mwan3 and not perms.read_mwan:
            stale = True
            reason = "missing mwan3 read permissions (mwan3 package detected)"
        elif packages.sqm_scripts and not perms.read_sqm:
            stale = True
            reason = "missing sqm read permissions (sqm package detected)"
        elif packages.adblock and not perms.read_services:
            stale = True
            reason = "missing service read permissions (adblock package detected)"
        elif packages.nlbwmon and not perms.read_network:
            stale = True
            reason = "missing network read permissions (nlbwmon package detected)"

        # Detect if an upgrade happened
        current_version = data.device_info.release_version
        is_upgrade = False
        if (
            self._last_version
            and current_version
            and self._last_version != current_version
        ):
            _LOGGER.info(
                "OpenWrt upgrade detected: %s -> %s",
                self._last_version,
                current_version,
            )
            is_upgrade = True

        # Update last version
        if current_version:
            self._last_version = current_version

        if stale:
            _LOGGER.warning(
                "Detected missing/stale RPC permissions for OpenWrt user '%s': %s (is_upgrade=%s). "
                "Some system sensors (such as CPU, memory, load, uptime, or temperature) will stop reporting. "
                "Please redeploy the Home Assistant user or restore the custom ACL rules on your router.",
                username,
                reason,
                is_upgrade,
            )
            async_create_stale_permissions_repair(
                self.hass, self.config_entry, is_upgrade=is_upgrade
            )
        else:
            async_delete_stale_permissions_repair(self.hass, self.config_entry)

    async def _async_fetch_all_data(self) -> OpenWrtData:
        """Fetch all data with retry logic."""
        if not self.client.connected:
            try:
                await self.client.connect()
            except Exception as err:
                if self.data:
                    _LOGGER.info(
                        "Reconnection failed, using stale data: %s",
                        err,
                    )
                    return self.data
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
            async_create_auth_repair(self.hass, self.config_entry)
            raise UpdateFailed(
                "Authentication failed. Check your credentials."
            ) from err
        except (UbusPackageMissingError, LuciRpcPackageMissingError) as err:
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
                if self.data:
                    _LOGGER.info("Using stale data")
                    return self.data
                self.client._connected = False
                async_create_connection_lost_repair(self.hass, self.config_entry)
                raise UpdateFailed(f"Error fetching data: {retry_err}") from retry_err
        except Exception as err:
            _LOGGER.exception("Unexpected error updating OpenWrt data: %s", err)
            raise UpdateFailed(f"Unexpected error: {err}") from err

    def _async_sync_firmware_state(self, data: OpenWrtData) -> None:
        """Sync firmware metadata."""
        if not data.device_info:
            return

        # Always initialize current version from device info
        data.firmware_current_version = (
            data.device_info.firmware_version or data.device_info.release_version
        )

        if (
            self.data
            and self.data.device_info.release_revision
            == data.device_info.release_revision
        ):
            # Preserve previously discovered current version if it was set
            if self.data.firmware_current_version:
                data.firmware_current_version = self.data.firmware_current_version

            data.firmware_latest_version = self.data.firmware_latest_version
            data.firmware_upgradable = self.data.firmware_upgradable
            data.firmware_release_url = self.data.firmware_release_url
            data.firmware_install_url = self.data.firmware_install_url
            data.firmware_checksum = self.data.firmware_checksum
            data.is_custom_build = self.data.is_custom_build
            data.asu_supported = self.data.asu_supported
            data.asu_update_available = self.data.asu_update_available
            data.asu_image_status = self.data.asu_image_status
            data.asu_image_url = self.data.asu_image_url
            data.installed_packages = self.data.installed_packages

    def _async_process_network_rates(self, data: OpenWrtData, now: float) -> None:
        """Calculate network rates."""
        elapsed = now - self._last_update_time
        if self._last_update_time > 0 and elapsed > 0:
            for iface in data.network_interfaces:
                prev = self._prev_network_stats.get(iface.name)
                if prev:
                    rx_diff = iface.rx_bytes - prev.get("rx_bytes", 0)
                    tx_diff = iface.tx_bytes - prev.get("tx_bytes", 0)
                    if rx_diff >= 0 and tx_diff >= 0:
                        iface.rx_rate = round(
                            (rx_diff * 8) / (1024 * 1024) / elapsed, 2
                        )
                        iface.tx_rate = round(
                            (tx_diff * 8) / (1024 * 1024) / elapsed, 2
                        )

        for iface in data.network_interfaces:
            self._prev_network_stats[iface.name] = {
                "rx_bytes": iface.rx_bytes,
                "tx_bytes": iface.tx_bytes,
            }

    async def _async_filter_and_track_devices(self, data: OpenWrtData) -> None:
        """Filter and track devices."""
        # Load history if needed
        if not self._device_history:
            stored_data = await self._store.async_load()
            if stored_data:
                self._device_history = stored_data

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

            # Handle MQTT Discovery if enabled
            if self.config_entry.options.get(CONF_MQTT_PRESENCE, False):
                await self._async_discovery_mqtt_device(mac, device.hostname or mac)

            # Filter by whitelist if configured
            if whitelist and mac not in whitelist:
                _LOGGER.debug(
                    "Skipping device %s: not in tracked_devices whitelist", mac
                )
                continue

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

            if mac not in self._device_history:
                self._device_history[mac] = {
                    "initially_seen": current_time,
                    "last_seen": current_time,
                    "is_wireless": device.is_wireless,
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
                history_updated = True

            # Sync with shared wireless history for multi-AP coordination
            if self._device_history[mac].get("is_wireless"):
                domain_data = self.hass.data.setdefault(DOMAIN, {})
                wireless_history = domain_data.setdefault("wireless_history", {})
                wireless_history[mac] = True

        data.all_connected_devices = all_devices
        data.connected_devices = filtered_devices
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

            # Ensure lease devices are also in history so they are discovered as trackers
            if mac not in self._device_history:
                is_wireless = is_random_mac(mac)
                self._device_history[mac] = {
                    "initially_seen": current_time,
                    "last_seen": current_time,
                    "is_wireless": is_wireless,
                }
                history_updated = True
                _LOGGER.debug(
                    "New lease-only device added to history: %s (guessed wireless: %s)",
                    mac,
                    is_wireless,
                )
            else:
                self._device_history[mac]["last_seen"] = current_time
                history_updated = True

            filtered_leases.append(lease)
        data.dhcp_leases = filtered_leases

        if history_updated:
            await self._store.async_save(self._device_history)

        # Handle MQTT Discovery (Start or Cleanup)
        # Initial MQTT discovery if enabled, or cleanup if disabled
        if self.config_entry.options.get(CONF_MQTT_PRESENCE, False):
            if not self._mqtt_discovery_started:
                self._mqtt_discovery_started = True
                self.hass.async_create_task(self._async_discovery_loop(clean=False))
        else:
            # If MQTT is disabled, ensure we clean up at least once per coordinator instance
            if not self._mqtt_cleanup_done:
                self._mqtt_cleanup_done = True
                self.hass.async_create_task(self._async_discovery_loop(clean=True))

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
            for mac, hist_data in list(self._device_history.items()):
                # Always cleanup legacy topics to be sure
                await self._async_discovery_mqtt_device_cleanup(mac)

                if not clean:
                    await self._async_discovery_mqtt_device(
                        mac, hist_data.get("hostname") or mac
                    )

                # Small delay between discovery calls to avoid flooding
                await asyncio.sleep(0.05)

        # Global registry cleanup (independent of device history)
        if clean and mqtt_ready:
            await self._async_global_registry_cleanup()

        _LOGGER.debug("MQTT discovery loop finished")

    @callback
    def _async_get_tracked_devices_whitelist(self) -> set[str] | None:
        """Get the merged whitelist of tracked devices."""
        tracked_devices = self.config_entry.options.get(CONF_TRACKED_DEVICES, [])
        manual_devices_raw = self.config_entry.options.get(
            CONF_MANUAL_TRACKED_DEVICES, ""
        )

        whitelist = set(tracked_devices)

        if manual_devices_raw:
            # Parse multi-line string of MAC addresses
            for line in manual_devices_raw.splitlines():
                mac = line.strip().lower()
                if mac:
                    # Basic MAC validation could be added here, but for now we just trust the user
                    whitelist.add(mac)

        return whitelist if whitelist else None

    async def _async_discovery_mqtt_device_cleanup(self, mac: str) -> None:
        """Remove legacy MQTT discovery messages for a device tracker."""
        mac_safe = mac.replace(":", "_")
        mac_colons = mac.lower()
        router_id_safe = self.router_id.replace(":", "_")

        mac_no_colons = mac.replace(":", "").lower()
        mac_6chars = mac_no_colons[-6:].upper()

        # Cleanup all legacy patterns we might have used
        # IMPORTANT: Discovery topics MUST NOT contain colons
        legacy_topics = [
            f"homeassistant/device_tracker/{self.router_id}_{mac_safe}/config",
            f"homeassistant/device_tracker/{router_id_safe}_{mac_safe}/config",
            f"homeassistant/device_tracker/openwrt_{mac_safe}/config",
            f"homeassistant/device_tracker/{mac_6chars}/config",
            f"homeassistant/device_tracker/openwrt_{mac_6chars}/config",
        ]

        for topic in legacy_topics:
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

        # Cleanup status topics too to clear retained messages
        status_topics = [
            f"presence/{mac_safe}",
            f"presence/{mac_colons}",
            f"openwrt/presence/{mac_safe}",
            f"openwrt/presence/{mac_colons}",
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

        # Get all known MAC formats from history
        known_macs = set()
        for mac in self._device_history:
            known_macs.add(mac.lower())
            known_macs.add(mac.replace(":", "").lower())
            known_macs.add(mac.replace(":", "_").lower())
            known_macs.add(mac.replace(":", "")[-6:].lower())  # Last 6 chars

        # Build a set of STRICT identifiers belonging to THIS router
        router_prefixes = {
            self.config_entry.entry_id.lower(),
            self.router_id.lower(),
            self.router_id.replace(":", "_").lower(),
            self.router_id.replace(":", "").lower(),
            "openwrt",  # Historical prefix
        }

        # Scan ALL entities for MQTT zombies belonging to THIS router
        for entry in list(ent_reg.entities.values()):
            if entry.platform == "mqtt" and entry.domain == "device_tracker":
                unique_id = (entry.unique_id or "").lower()
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
            "unique_id": f"openwrt_track_{mac_safe}",
            "payload_home": "home",
            "payload_not_home": "not_home",
            "source_type": "router",
            "device": {
                "connections": [["mac", mac]],
                "identifiers": [f"openwrt_{mac}"],
                "name": hostname,
                "via_device": self.router_id,
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
        via_device = None
        if device_info.gateway_mac:
            gw_mac = device_info.gateway_mac.lower()
            for dev in device_registry.devices.values():
                if any(
                    conn[0] == dr.CONNECTION_NETWORK_MAC and conn[1].lower() == gw_mac
                    for conn in dev.connections
                ):
                    if dev.identifiers:
                        via_device = next(iter(dev.identifiers))
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
        existing_device = device_registry.async_get_device(identifiers=identifiers)
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

        device_registry.async_get_or_create(
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
            via_device=via_device,
            configuration_url=f"http://{self.config_entry.data[CONF_HOST]}",
        )

        # 2. Register/Update AP devices for wireless interfaces
        # Ensure stable_id is based on SSID and Band to prevent duplicates
        # for mesh routers that spawn multiple virtual interfaces per radio.
        ap_info: dict[str, str] = {}

        for wifi in data.wireless_interfaces:
            # Skip interfaces without name or SSID
            if not wifi.name or not wifi.ssid:
                continue

            # Use the normalised band string ("2.4 GHz", "5 GHz", "6 GHz") rather
            # than the raw frequency in MHz. This groups all virtual interfaces on
            # the same radio+SSID combination under one stable AP device, even
            # when different channels are reported across updates.
            from .helpers import normalize_band

            band = normalize_band(wifi.band or wifi.frequency or wifi.radio)
            label = format_ap_name(wifi.ssid, band)

            # Use SSID and Band as stable identifier to group virtual interfaces
            stable_id = f"{wifi.ssid}_{band}"
            ap_info[stable_id] = label
            self.interface_to_stable_id[wifi.name] = stable_id

        for stable_id, label in ap_info.items():
            device_registry.async_get_or_create(
                config_entry_id=self.config_entry.entry_id,
                identifiers={(DOMAIN, format_ap_device_id(self.router_id, stable_id))},
                name=label,
                manufacturer=device_info.release_distribution or ATTR_MANUFACTURER,
                model="Access Point",
                via_device=(DOMAIN, self.router_id),
            )

        # 3. Retroactively update manufacturer/model for already-registered tracked devices.
        # HA only writes manufacturer/model at first creation; subsequent coordinator polls
        # are ignored unless we call async_update_device() explicitly. This loop fixes all
        # devices that were registered before the OUI mapping was added or that were created
        # with the generic "OpenWrt" / "Tracked device" defaults.
        mac_pattern = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$", re.IGNORECASE)
        for dev in device_registry.devices.values():
            # Only touch devices that belong to our specific config entry
            if self.config_entry.entry_id not in dev.config_entries:
                continue

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

        _LOGGER.debug(
            "Starting deep device registry cleanup for %s active identifiers",
            len(active_identifiers),
        )

        devices_to_remove = []
        # Iterate over all devices in the registry
        for dev in list(device_registry.devices.values()):
            # Check if any identifier belonging to our domain matches this router
            is_ours = False
            is_tracked_device = False
            tracked_mac = None

            for ident in dev.identifiers:
                if ident[0] == DOMAIN:
                    ident_str = str(ident[1])
                    if (
                        ident_str == self.router_id
                        or ident_str == self.config_entry.data.get(CONF_HOST)
                        or ident_str == self.config_entry.unique_id
                        or ident_str.startswith(f"{self.router_id}_")
                    ):
                        is_ours = True
                    elif re.match(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$", ident_str):
                        is_tracked_device = True
                        tracked_mac = ident_str

            if not is_ours and not is_tracked_device:
                continue

            # If it's one of our currently active APs or the router itself, keep it
            if is_ours and any(
                ident in active_identifiers for ident in dev.identifiers
            ):
                continue

            # Identify if this is an Access Point device (old or new style)
            # We also check the name as a fallback for old installations
            is_ap_related = any(
                "_ap_" in str(ident[1])
                for ident in dev.identifiers
                if ident[0] == DOMAIN
            )
            is_ghost_name = any(
                ghost in (dev.name or "")
                for ghost in ["default_radio", "wifinet", "radio"]
            )

            # Identify if this is a randomized MAC device and skip_random is enabled
            is_random_tracked = False
            if skip_random and is_tracked_device and tracked_mac:
                if is_random_mac(tracked_mac):
                    is_random_tracked = True

            if is_ap_related or is_ghost_name or is_random_tracked:
                _LOGGER.info(
                    "Removing orphaned/ghost/randomized device '%s' (id: %s, identifiers: %s)",
                    dev.name,
                    dev.id,
                    dev.identifiers,
                )

                # If it's a tracked device, we might only want to remove our config entry from it
                # if other integrations also track it. But async_remove_device is simpler and
                # usually what the user wants for randomized MACs to clear them out.
                devices_to_remove.append(dev.id)

        # Get the ID of our main router device to use as a fallback for orphans
        router_dev = device_registry.async_get_device(
            identifiers={(DOMAIN, self.router_id)}
        )
        router_dev_id = router_dev.id if router_dev else None

        # Build a mapping of via_device_id to find children efficiently without nested loops
        via_map: dict[str, list[dr.DeviceEntry]] = {}
        if router_dev_id:
            for other_dev in device_registry.devices.values():
                if other_dev.via_device_id:
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

    async def _check_firmware_update(self, data: OpenWrtData) -> None:
        """Check for firmware updates (official or custom)."""
        custom_repo = self.config_entry.options.get(
            CONF_CUSTOM_FIRMWARE_REPO,
            self.config_entry.data.get(CONF_CUSTOM_FIRMWARE_REPO, ""),
        )
        if custom_repo:
            await self._check_custom_firmware_update(data, custom_repo)
        else:
            await self._check_official_firmware_update(data)
            await self._check_asu_update(data)

    async def _check_official_firmware_update(self, data: OpenWrtData) -> None:
        """Check for firmware updates from the OpenWrt release API."""
        current_version = data.device_info.release_version
        session = async_get_clientsession(self.hass)

        if "SNAPSHOT" in current_version.upper():
            await self._check_snapshot_update(data, session)
        else:
            await self._check_stable_release_update(data, session)

    def _get_target(self, target: str) -> str:
        """Apply target migrations/mappings if needed."""
        override = self.config_entry.options.get(CONF_TARGET_OVERRIDE)
        if override:
            return override
        return SNAPSHOT_TARGET_MAP.get(target, target)

    async def _check_snapshot_update(
        self, data: OpenWrtData, session: aiohttp.ClientSession
    ) -> None:
        """Check for updates in SNAPSHOT builds."""
        target = self._get_target(data.device_info.target)
        _LOGGER.info(
            "Checking snapshot update for target: %s (original: %s)",
            target,
            data.device_info.target,
        )
        if not target:
            return

        url = f"https://downloads.openwrt.org/snapshots/targets/{target}/profiles.json"

        with contextlib.suppress(Exception):
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                _LOGGER.info(
                    "Snapshot profiles.json status for %s: %s", target, resp.status
                )
                if resp.status != 200:
                    return
                profile_data = await resp.json()
                version_code = profile_data.get("version_code", "")
                if not version_code:
                    return

                latest_snapshot = f"SNAPSHOT ({version_code})"
                _LOGGER.info(
                    "Comparing snapshot versions: current=%s, latest=%s",
                    data.firmware_current_version,
                    latest_snapshot,
                )
                data.firmware_latest_version = latest_snapshot
                if self._version_is_newer(
                    data.firmware_current_version, latest_snapshot
                ):
                    data.firmware_upgradable = True
                    _LOGGER.info(
                        "Newer snapshot found for %s: %s", target, latest_snapshot
                    )
                    data.firmware_release_url = (
                        f"https://downloads.openwrt.org/snapshots/targets/{target}/"
                    )
                else:
                    data.firmware_upgradable = False
                    _LOGGER.debug("Snapshot is up-to-date: %s", latest_snapshot)

                # Find sysupgrade image
                profiles = profile_data.get("profiles", {})
                board_name = data.device_info.board_name or ""
                board_key = board_name.replace("-", "_").replace(",", "_")
                board_profile = profiles.get(board_key)
                if board_profile:
                    for img in board_profile.get("images", []):
                        if "sysupgrade" in img.get("name", ""):
                            data.firmware_install_url = f"https://downloads.openwrt.org/snapshots/targets/{target}/{img.get('name')}"
                            break

    async def _check_stable_release_update(
        self, data: OpenWrtData, session: aiohttp.ClientSession
    ) -> None:
        """Check for updates in stable releases."""
        with contextlib.suppress(Exception):
            async with session.get(
                OPENWRT_RELEASE_API, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status != 200:
                    return
                versions_data = await resp.json()
                latest_stable = versions_data.get(
                    "stable_version", versions_data.get("latest", "")
                )

                if not latest_stable and isinstance(versions_data, dict):
                    for key in sorted(versions_data.keys(), reverse=True):
                        if not key.startswith(".") and not key.startswith("_"):
                            latest_stable = key
                            break

                if latest_stable:
                    data.firmware_latest_version = latest_stable
                    if self._version_is_newer(
                        data.device_info.release_version, latest_stable
                    ):
                        data.firmware_upgradable = True
                        self._set_stable_release_urls(data, latest_stable)
                    else:
                        data.firmware_upgradable = False

    def _set_stable_release_urls(self, data: OpenWrtData, latest_stable: str) -> None:
        """Determine release and install URLs for a stable release."""
        data.firmware_release_url = f"https://openwrt.org/releases/{latest_stable}"
        info = data.device_info
        target = self._get_target(info.target)
        if target and info.board_name:
            board = info.board_name.replace("_", "-").replace(",", "-")
            dist = info.release_distribution or "openwrt"
            data.firmware_install_url = (
                f"https://downloads.openwrt.org/releases/{latest_stable}/targets/{target}/"
                f"{dist}-{latest_stable}-{target.replace('/', '-')}-{board}-squashfs-sysupgrade.bin"
            )

    async def _check_asu_update(self, data: OpenWrtData) -> None:
        """Check for updates via the ASU (Attended Sysupgrade) API."""
        target = self._get_target(data.device_info.target)
        if not target or not data.device_info.board_name:
            return

        asu_url = self.config_entry.options.get(
            CONF_ASU_URL,
            self.config_entry.data.get(CONF_ASU_URL, "https://sysupgrade.openwrt.org"),
        )
        session = async_get_clientsession(self.hass)

        # 1. Fetch info from ASU
        asu_info = await self._fetch_asu_info(data, asu_url, session)
        if not asu_info:
            return

        # 2. Process findings
        data.asu_supported = True
        version = asu_info.get("version", "")
        revision = asu_info.get("revision", "")

        latest_version = version or revision
        if revision and ("SNAPSHOT" in version.upper() or not version):
            latest_version = f"{version or 'SNAPSHOT'} ({revision})"

        if not latest_version:
            return

        if self._version_is_newer(data.firmware_current_version or "", latest_version):
            data.asu_update_available = True
            await self._update_firmware_metadata_from_asu(data, latest_version)

    async def _fetch_asu_info(
        self, data: OpenWrtData, asu_url: str, session: aiohttp.ClientSession
    ) -> dict[str, Any] | None:
        """Fetch metadata from ASU API with model name variation fallback."""
        target = self._get_target(data.device_info.target)
        model = data.device_info.board_name
        is_snapshot = "SNAPSHOT" in data.device_info.release_version.upper()

        async def _do_fetch(m: str) -> dict[str, Any] | None:
            url = f"{asu_url.rstrip('/')}/api/v1/info?target={target}&model={m}"
            if is_snapshot:
                url += "&version=SNAPSHOT"
            with contextlib.suppress(Exception):
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    if resp.status == 404:
                        return {"status": 404}
            return None

        # Try primary model name
        res = await _do_fetch(model)
        if res and res.get("status") != 404:
            return res

        # Try fallback variation (comma to underscore) if first failed with 404
        if res and res.get("status") == 404 and "," in model:
            return await _do_fetch(model.replace(",", "_"))

        return None

    async def _update_firmware_metadata_from_asu(
        self, data: OpenWrtData, latest_version: str
    ) -> None:
        """Update coordinator data with findings from ASU."""
        # Ensure we have package list for future upgrade requests
        with contextlib.suppress(Exception):
            data.installed_packages = await self.client.get_installed_packages()

        if self._version_is_newer(
            data.firmware_latest_version or "0.0.0", latest_version
        ):
            data.firmware_latest_version = latest_version
            data.firmware_upgradable = True
            data.firmware_release_url = f"https://openwrt.org/releases/{latest_version}"
            data.firmware_install_url = ""  # Built on demand

    async def _check_custom_firmware_update(
        self,
        data: OpenWrtData,
        repo_input: str,
    ) -> None:
        """Check for firmware updates from a custom GitHub repository."""
        data.is_custom_build = True
        owner, repo = self._parse_repo(repo_input)
        if not owner or not repo:
            return

        router_hash = self._get_router_hash(data)
        _LOGGER.debug(
            "Checking custom firmware for %s/%s (router hash: %s)",
            owner,
            repo,
            router_hash,
        )

        session = async_get_clientsession(self.hass)
        headers = {"Accept": "application/vnd.github+json"}

        # 1. Get releases
        with contextlib.suppress(Exception):
            url = f"https://api.github.com/repos/{owner}/{repo}/releases"
            async with session.get(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    return
                releases = await resp.json()
                if not releases:
                    return
                latest_release = releases[0]

            # 2. Try to identify current version by commit hash if unknown
            if router_hash:
                await self._find_tag_by_hash(
                    data, owner, repo, router_hash, headers, session
                )

            # 3. Determine latest version and meta
            latest_tag = latest_release.get("tag_name", "")
            latest_version = self._get_latest_version_string(latest_release)

            data.firmware_latest_version = latest_version
            data.firmware_release_url = latest_release.get("html_url", "")

            # 4. Check if upgradable
            is_upgradable = self._version_is_newer(
                data.firmware_current_version or "", latest_tag
            )
            if not is_upgradable and latest_version != latest_tag:
                is_upgradable = self._version_is_newer(
                    data.firmware_current_version or "", latest_version
                )
            data.firmware_upgradable = is_upgradable

            # 5. Find sysupgrade image and checksum
            await self._process_custom_release_assets(data, latest_release, session)

    def _get_router_hash(self, data: OpenWrtData) -> str:
        """Extract commit hash from revision string."""
        revision = data.device_info.release_revision
        if revision and "-" in revision:
            return revision.split("-")[-1].strip()
        return ""

    async def _find_tag_by_hash(
        self,
        data: OpenWrtData,
        owner: str,
        repo: str,
        router_hash: str,
        headers: dict,
        session: aiohttp.ClientSession,
    ) -> None:
        """Find a GitHub tag that matches the router's commit hash."""
        with contextlib.suppress(Exception):
            url = f"https://api.github.com/repos/{owner}/{repo}/tags"
            async with session.get(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == 200:
                    tags = await resp.json()
                    for tag in tags:
                        sha = tag.get("commit", {}).get("sha", "")
                        if sha.startswith(router_hash):
                            data.firmware_current_version = tag.get("name")
                            break

    def _get_latest_version_string(self, release: dict[str, Any]) -> str:
        """Format the latest version string from release info."""
        tag = release.get("tag_name", "")
        if "SNAPSHOT" not in tag.upper():
            return tag

        published = release.get("published_at", "")
        commit = release.get("target_commitish", "")
        if commit and len(commit) >= 7:
            return f"{tag} ({commit[:7]})"
        if published:
            return f"{tag} ({published.split('T')[0]})"
        return tag

    async def _process_custom_release_assets(
        self, data: OpenWrtData, release: dict[str, Any], session: aiohttp.ClientSession
    ) -> None:
        """Find the best sysupgrade asset and its checksum from release."""
        assets = release.get("assets", [])
        pattern = self._build_sysupgrade_pattern(data)
        best_asset = None
        sha_url = None

        for asset in assets:
            name = asset.get("name", "")
            if "sha256sum" in name.lower() or name == "sha256sums":
                sha_url = asset.get("browser_download_url")
            if pattern and re.match(pattern, name, re.IGNORECASE):
                best_asset = asset

        if not best_asset:
            board_name = data.device_info.board_name or ""
            board = board_name.replace(",", "_").replace(" ", "_")
            for asset in assets:
                if board in asset.get("name", "") and "sysupgrade" in asset.get(
                    "name", ""
                ):
                    best_asset = asset
                    break

        if best_asset:
            data.firmware_install_url = best_asset.get("browser_download_url")
            if sha_url:
                await self._fetch_custom_checksum(
                    data, sha_url, best_asset.get("name", ""), session
                )

    async def _fetch_custom_checksum(
        self,
        data: OpenWrtData,
        sha_url: str,
        asset_name: str,
        session: aiohttp.ClientSession,
    ) -> None:
        """Fetch and parse checksum file from GitHub."""
        with contextlib.suppress(Exception):
            async with session.get(sha_url) as resp:
                if resp.status == 200:
                    content = await resp.text()
                    for line in content.splitlines():
                        if asset_name in line:
                            data.firmware_checksum = line.split()[0]
                            break

    @staticmethod
    def _parse_repo(repo_input: str) -> tuple[str, str]:
        """Parse 'owner/repo' from URL or direct input."""
        repo_input = repo_input.strip().strip("/")
        url_match = re.search(r"github\.com/([^/]+)/([^/]+)", repo_input)
        if url_match:
            return url_match.group(1), url_match.group(2)
        parts = repo_input.split("/")
        return (parts[0], parts[1]) if len(parts) == 2 else ("", repo_input)

    @staticmethod
    def _build_sysupgrade_pattern(data: OpenWrtData) -> str | None:
        """Build regex pattern for sysupgrade matching."""
        info = data.device_info
        if not info.target or not info.board_name:
            return None
        target = info.target.replace("/", "-")
        board = info.board_name.replace(",", "_").replace(" ", "_")
        return rf".*{re.escape(target)}.*{re.escape(board)}.*sysupgrade\.bin$"

    @staticmethod
    def _version_is_newer(current: str, latest: str) -> bool:
        """Compare firmware versions (e.g., '24.10.1' vs '25.12.0')."""
        import re

        if "SNAPSHOT" in current.upper() or "SNAPSHOT" in latest.upper():
            # For snapshots, we always prefer revision comparison if possible
            def get_rev_num(v: str) -> int:
                # Matches r12345 or SNAPSHOT (r12345)
                match = re.search(r"r(\d+)", v)
                if match:
                    return int(match.group(1))
                return -1

            rev_current = get_rev_num(current)
            rev_latest = get_rev_num(latest)

            _LOGGER.debug(
                "Comparing snapshots: current=%s (rev=%s), latest=%s (rev=%s)",
                current,
                rev_current,
                latest,
                rev_latest,
            )

            if rev_current >= 0 and rev_latest >= 0:
                if rev_latest != rev_current:
                    return rev_latest > rev_current

            # Fallback to string comparison if revisions aren't numeric/comparable
            # but strip "SNAPSHOT" and extra chars for a cleaner comparison
            clean_current = re.sub(
                r"[^a-zA-Z0-9-]", "", current.upper().replace("SNAPSHOT", "")
            )
            clean_latest = re.sub(
                r"[^a-zA-Z0-9-]", "", latest.upper().replace("SNAPSHOT", "")
            )
            result = clean_latest != clean_current
            _LOGGER.debug(
                "Snapshot fallback comparison: %s != %s -> %s",
                clean_latest,
                clean_current,
                result,
            )
            return result

        try:
            current_parts = [int(p) for p in current.split(".")]
            latest_parts = [int(p) for p in latest.split(".")]
            return latest_parts > current_parts
        except (
            ValueError,
            AttributeError,
        ):
            return current != latest

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

    async def async_shutdown(self) -> None:
        """Shut down the coordinator and disconnect."""
        await super().async_shutdown()
        await self.client.disconnect()
