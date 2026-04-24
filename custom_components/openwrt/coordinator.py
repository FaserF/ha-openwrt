"""Data update coordinator for OpenWrt integration.

Manages periodic data fetching from the OpenWrt device and firmware
update checking against the official OpenWrt release API.
"""

from __future__ import annotations

import contextlib
import logging
import re
import time
from datetime import timedelta
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_MANUFACTURER, CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    device_registry as dr,
)
from homeassistant.helpers import (
    storage,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

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
    CONF_ASU_URL,
    CONF_CONNECTION_TYPE,
    CONF_CUSTOM_FIRMWARE_REPO,
    CONF_DHCP_SOFTWARE,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_SSH_KEY,
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
    DEFAULT_UBUS_PATH,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    OPENWRT_RELEASE_API,
)
from .helpers import format_ap_device_id, format_ap_name
from .repairs import (
    async_create_auth_repair,
    async_create_connection_lost_repair,
    async_create_missing_packages_repair,
    async_delete_connection_lost_repair,
)

_LOGGER = logging.getLogger(__name__)

FIRMWARE_CHECK_INTERVAL = timedelta(hours=6)


def create_client(config: dict[str, Any]) -> OpenWrtClient:
    """Create the appropriate API client based on configuration."""
    connection_type = config.get(CONF_CONNECTION_TYPE, CONNECTION_TYPE_UBUS)
    host = config[CONF_HOST]
    username = config[CONF_USERNAME]
    password = config.get(CONF_PASSWORD, "")
    use_ssl = config.get(CONF_USE_SSL, False)
    verify_ssl = config.get(CONF_VERIFY_SSL, False)
    dhcp_software = config.get(CONF_DHCP_SOFTWARE, "auto")

    _LOGGER.debug("Creating client for %s (type: %s)", host, connection_type)
    _LOGGER.debug(
        "Config data: %s",
        {k: (v if k != CONF_PASSWORD else "********") for k, v in config.items()},
    )

    if connection_type == CONNECTION_TYPE_SSH:
        port = config.get(CONF_PORT, DEFAULT_PORT_SSH)
        return SshClient(
            host=host,
            username=username,
            password=password,
            port=port,
            ssh_key=config.get(CONF_SSH_KEY),
            dhcp_software=dhcp_software,
        )

    if connection_type == CONNECTION_TYPE_LUCI_RPC:
        port = config.get(
            CONF_PORT,
            DEFAULT_PORT_UBUS_SSL if use_ssl else DEFAULT_PORT_UBUS,
        )
        return LuciRpcClient(
            host=host,
            username=username,
            password=password,
            port=port,
            use_ssl=use_ssl,
            verify_ssl=verify_ssl,
            dhcp_software=dhcp_software,
        )

    port = config.get(
        CONF_PORT,
        DEFAULT_PORT_UBUS_SSL if use_ssl else DEFAULT_PORT_UBUS,
    )
    return UbusClient(
        host=host,
        username=username,
        password=password,
        port=port,
        use_ssl=use_ssl,
        verify_ssl=verify_ssl,
        ubus_path=config.get(CONF_UBUS_PATH, DEFAULT_UBUS_PATH),
        dhcp_software=dhcp_software,
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
        """Initialize the coordinator."""
        self.client = client
        self.hass = hass
        self.config_entry = config_entry
        self._firmware_checked = False
        self._last_firmware_check: float = 0.0
        self._last_update_time: float = 0.0
        self._prev_network_stats: dict[str, dict[str, int]] = {}
        self._device_history: dict[str, dict[str, Any]] = {}
        self.interface_to_stable_id: dict[str, str] = {}
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
        """Set up the coordinator (connect to device)."""
        # Load history from storage
        try:
            stored_history = await self._store.async_load()
            if stored_history:
                self._device_history.update(stored_history)
                _LOGGER.debug(
                    "Loaded %s devices from persistent history",
                    len(self._device_history),
                )
        except Exception as err:
            _LOGGER.warning("Could not load persistent history: %s", err)

        try:
            await self.client.connect()
        except Exception as err:
            msg = f"Cannot connect to OpenWrt device: {err}"
            raise UpdateFailed(msg) from err

    async def _async_update_data(self) -> OpenWrtData:
        """Fetch data from the OpenWrt device."""
        # 1. Fetch data from device
        data = await self._async_fetch_all_data()

        async_delete_connection_lost_repair(self.hass, self.config_entry)

        # 2. Transfer firmware state if revision hasn't changed
        self._async_sync_firmware_state(data)

        # 3. Periodic firmware checks
        now = self.hass.loop.time()
        if now - self._last_firmware_check > FIRMWARE_CHECK_INTERVAL.total_seconds():
            self._last_firmware_check = now
            await self._check_firmware_update(data)

        # 4. Calculate network rates
        self._async_process_network_rates(data, now)
        self._last_update_time = now

        # 5. Update device registry
        await self._async_update_device_registry(data)

        # 6. Device tracking and filtering
        await self._async_filter_and_track_devices(data)

        # 7. Persist history if it changed
        try:
            await self._store.async_save(self._device_history)
        except Exception as err:
            _LOGGER.warning("Could not save persistent history: %s", err)

        return data

    async def _async_fetch_all_data(self) -> OpenWrtData:
        """Fetch all data from the client with retry logic."""
        if not self.client.connected:
            try:
                await self.client.connect()
            except Exception as err:
                raise UpdateFailed(f"Cannot connect: {err}") from err

        try:
            _LOGGER.debug("Fetching all data from OpenWrt device")
            return await self.client.get_all_data()
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
                _LOGGER.warning("Updating data failed for %s: %s", self.name, retry_err)
                if self.data:
                    _LOGGER.info("Using stale data for %s", self.name)
                    return self.data
                self.client._connected = False
                async_create_connection_lost_repair(self.hass, self.config_entry)
                raise UpdateFailed(f"Error fetching data: {retry_err}") from retry_err
        except Exception as err:
            _LOGGER.exception("Unexpected error updating OpenWrt data: %s", err)
            raise UpdateFailed(f"Unexpected error: {err}") from err

    def _async_sync_firmware_state(self, data: OpenWrtData) -> None:
        """Sync firmware metadata from previous data if revision is unchanged."""
        if (
            self.data
            and self.data.device_info.release_revision
            == data.device_info.release_revision
        ):
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
        else:
            data.firmware_current_version = (
                data.device_info.firmware_version or data.device_info.release_version
            )

    def _async_process_network_rates(self, data: OpenWrtData, now: float) -> None:
        """Calculate network rates based on bytes diff since last update."""
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
        """Filter out internal devices and update tracking history."""
        # Load history if needed
        if not self._device_history:
            stored_data = await self._store.async_load()
            if stored_data:
                self._device_history = stored_data

        own_macs = self._get_own_macs(data)
        own_ips = data.local_ips
        current_time = int(time.time())
        history_updated = False

        filtered_devices = []
        for device in data.connected_devices:
            mac = device.mac.lower()
            # 1. Filter out router's own interfaces (always)
            if mac in own_macs:
                continue

            # 2. Filter out router's own IP addresses
            if device.ip and device.ip in own_ips:
                continue

            # 3. Filter out internal interface names masquerading as hostnames
            if device.hostname:
                hostname = device.hostname.lower()
                # Enhanced regex to catch more interface-like names (wlan0, eth0.1, br-lan, etc.)
                if re.match(
                    r"^(wlan|eth|lan|wan|br-|radio|phy|veth|lo|bond|team)[0-9]*([.-].*)?$",
                    hostname,
                ):
                    continue

            # 4. Filter if hostname is identical to the interface name (likely self-reported neighbor)
            if (
                device.interface
                and device.hostname
                and device.interface.lower() == device.hostname.lower()
            ):
                continue

            filtered_devices.append(device)

            if mac not in self._device_history:
                self._device_history[mac] = {
                    "initially_seen": current_time,
                    "last_seen": current_time,
                    "is_wireless": device.is_wireless,
                }
                history_updated = True
            else:
                hist = self._device_history[mac]
                hist["last_seen"] = current_time
                # Persistence: if it was EVER wireless, it stays wireless in history
                # to avoid fake-wired entries from DHCP leases when offline.
                if device.is_wireless and not hist.get("is_wireless"):
                    hist["is_wireless"] = True
                history_updated = True

        data.connected_devices = filtered_devices

        # 5. Filter DHCP leases to prevent entities for internal interfaces (veth, wlanX, etc.)
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
            filtered_leases.append(lease)
        data.dhcp_leases = filtered_leases

        if history_updated:
            await self._store.async_save(self._device_history)

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

        router_id = self.config_entry.unique_id or self.config_entry.data[CONF_HOST]
        _LOGGER.debug(
            "Updating device registry for %s: model=%s",
            router_id,
            device_info.model,
        )

        # 1. Register/Update the main router device
        device_registry.async_get_or_create(
            config_entry_id=self.config_entry.entry_id,
            identifiers={(DOMAIN, router_id)},
            connections=(
                {(dr.CONNECTION_NETWORK_MAC, device_info.mac_address.lower())}
                if device_info.mac_address
                else None
            ),
            manufacturer=device_info.release_distribution or ATTR_MANUFACTURER,
            model=device_info.model or device_info.board_name,
            name=device_info.model or device_info.hostname or self.config_entry.title,
            sw_version=device_info.firmware_version,
            hw_version=device_info.board_name,
            via_device=via_device,
            configuration_url=f"http://{self.config_entry.data[CONF_HOST]}",
        )

        # 2. Register/Update AP devices for wireless interfaces
        # Ensure stable_id is always the physical interface name (e.g. phy1-ap0)
        # to avoid ghost devices from UCI section name changes.
        ap_info: dict[str, tuple[str, str]] = {}

        for wifi in data.wireless_interfaces:
            # Skip interfaces without name or SSID
            if not wifi.name or not wifi.ssid:
                continue

            label = format_ap_name(wifi.ssid, wifi.frequency)

            # Use physical interface name as stable identifier to prevent duplicates
            stable_id = wifi.name
            ap_info[wifi.name] = (label, stable_id)
            self.interface_to_stable_id[wifi.name] = stable_id

        # Also check connected devices for any interfaces we might have missed
        for device in data.connected_devices:
            if (
                device.is_wireless
                and device.interface
                and device.interface not in ap_info
            ):
                ap_info[device.interface] = (device.interface, device.interface)

        # Collect all valid stable_ids for this update cycle
        valid_stable_ids = {info[1] for info in ap_info.values()}

        for _iface_name, (label, stable_id) in ap_info.items():
            device_registry.async_get_or_create(
                config_entry_id=self.config_entry.entry_id,
                identifiers={(DOMAIN, format_ap_device_id(router_id, stable_id))},
                name=label,
                manufacturer=device_info.release_distribution or ATTR_MANUFACTURER,
                model="Access Point",
                via_device=(DOMAIN, router_id),
            )

        # 3. Cleanup orphaned AP devices
        devices_to_remove = []
        for dev in dr.async_entries_for_config_entry(
            device_registry, self.config_entry.entry_id
        ):
            ap_stable_id = None
            for identifier in dev.identifiers:
                if identifier[0] == DOMAIN and "_ap_" in identifier[1]:
                    # Extract the part after the last '_ap_'
                    ap_stable_id = identifier[1].split("_ap_", 1)[-1]
                    break

            if ap_stable_id and ap_stable_id not in valid_stable_ids:
                _LOGGER.info(
                    "Removing orphaned AP device '%s' (id: %s)", ap_stable_id, dev.id
                )
                devices_to_remove.append(dev.id)

        for dev_id in devices_to_remove:
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

    async def _check_snapshot_update(
        self, data: OpenWrtData, session: aiohttp.ClientSession
    ) -> None:
        """Check for updates in SNAPSHOT builds."""
        if not data.device_info.target:
            return

        target = data.device_info.target
        url = f"https://downloads.openwrt.org/snapshots/targets/{target}/profiles.json"

        with contextlib.suppress(Exception):
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    return
                profile_data = await resp.json()
                version_code = profile_data.get("version_code", "")
                if not version_code:
                    return

                latest_snapshot = f"SNAPSHOT ({version_code})"
                if self._version_is_newer(
                    data.device_info.release_version, latest_snapshot
                ):
                    data.firmware_latest_version = latest_snapshot
                    data.firmware_upgradable = True
                    data.firmware_release_url = (
                        f"https://downloads.openwrt.org/snapshots/targets/{target}/"
                    )

                    # Find sysupgrade image
                    profiles = profile_data.get("profiles", {})
                    board_key = data.device_info.board_name.replace("-", "_").replace(
                        ",", "_"
                    )
                    board_profile = profiles.get(board_key)
                    if board_profile:
                        for img in board_profile.get("images", []):
                            if "sysupgrade" in img.get("name", ""):
                                data.firmware_install_url = (
                                    f"{data.firmware_release_url}{img.get('name')}"
                                )
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

                if latest_stable and self._version_is_newer(
                    data.device_info.release_version, latest_stable
                ):
                    data.firmware_latest_version = latest_stable
                    data.firmware_upgradable = True
                    self._set_stable_release_urls(data, latest_stable)

    def _set_stable_release_urls(self, data: OpenWrtData, latest_stable: str) -> None:
        """Determine release and install URLs for a stable release."""
        data.firmware_release_url = f"https://openwrt.org/releases/{latest_stable}"
        info = data.device_info
        if info.target and info.board_name:
            target = info.target
            board = info.board_name.replace("_", "-").replace(",", "-")
            dist = info.release_distribution or "openwrt"
            data.firmware_install_url = (
                f"https://downloads.openwrt.org/releases/{latest_stable}/targets/{target}/"
                f"{dist}-{latest_stable}-{target.replace('/', '-')}-{board}-squashfs-sysupgrade.bin"
            )

    async def _check_asu_update(self, data: OpenWrtData) -> None:
        """Check for updates via the ASU (Attended Sysupgrade) API."""
        if not data.device_info.target or not data.device_info.board_name:
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
        target = data.device_info.target
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
            board = data.device_info.board_name.replace(",", "_").replace(" ", "_")
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

        if "SNAPSHOT" in current.upper() and "SNAPSHOT" not in latest.upper():
            return False

        if "SNAPSHOT" in current.upper() and "SNAPSHOT" in latest.upper():

            def get_rev_num(v: str) -> int:
                match = re.search(r"r(\d+)-", v)
                if match:
                    return int(match.group(1))
                return -1

            rev_current = get_rev_num(current)
            rev_latest = get_rev_num(latest)
            if rev_current >= 0 and rev_latest >= 0 and rev_latest != rev_current:
                return rev_latest > rev_current
            return current != latest

        try:
            current_parts = [int(p) for p in current.split(".")]
            latest_parts = [int(p) for p in latest.split(".")]
            return latest_parts > current_parts
        except (
            ValueError,
            AttributeError,
        ):
            return current != latest

    async def async_shutdown(self) -> None:
        """Shut down the coordinator and disconnect."""
        await super().async_shutdown()
        await self.client.disconnect()
