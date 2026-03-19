"""Data update coordinator for OpenWrt integration.

Manages periodic data fetching from the OpenWrt device and firmware
update checking against the official OpenWrt release API.
"""

from __future__ import annotations

import logging
import re
import time
import traceback as _traceback
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
            CONF_PORT, DEFAULT_PORT_UBUS_SSL if use_ssl else DEFAULT_PORT_UBUS
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
        CONF_PORT, DEFAULT_PORT_UBUS_SSL if use_ssl else DEFAULT_PORT_UBUS
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
        self._store: storage.Store = storage.Store(
            hass, 1, f"{DOMAIN}_{config_entry.entry_id}_history"
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
        try:
            await self.client.connect()
        except Exception as err:
            raise UpdateFailed(f"Cannot connect to OpenWrt device: {err}") from err

    async def _async_update_data(self) -> OpenWrtData:
        """Fetch data from the OpenWrt device."""
        if not self.client.connected:
            try:
                await self.client.connect()
            except Exception as err:
                raise UpdateFailed(f"Cannot connect: {err}") from err

        try:
            _LOGGER.debug("Fetching all data from OpenWrt device")
            data = await self.client.get_all_data()
            _LOGGER.debug(
                "Successfully fetched data from OpenWrt: %d devices, %d interfaces",
                len(data.connected_devices),
                len(data.network_interfaces),
            )
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
                data = await self.client.get_all_data()
                _LOGGER.debug("Successfully fetched data on retry")
            except Exception as retry_err:
                _LOGGER.warning("Updating data failed for %s: %s", self.name, retry_err)
                if self.data:
                    _LOGGER.info("Using stale data for %s", self.name)
                    return self.data
                self.client._connected = False  # Force reconnection next time
                async_create_connection_lost_repair(self.hass, self.config_entry)
                raise UpdateFailed(f"Error fetching data: {retry_err}") from retry_err
        except Exception as err:
            _LOGGER.error(
                "Unexpected error updating OpenWrt data for %s: %s\n%s",
                self.name,
                err,
                _traceback.format_exc(),
            )
            raise UpdateFailed(f"Unexpected error: {err}") from err

        async_delete_connection_lost_repair(self.hass, self.config_entry)

        if (
            self.data
            and self.data.device_info.release_revision
            == data.device_info.release_revision
        ):
            data.firmware_current_version = self.data.firmware_current_version
            data.firmware_latest_version = self.data.firmware_latest_version
            data.firmware_upgradable = self.data.firmware_upgradable
            data.firmware_release_url = self.data.firmware_release_url
            data.firmware_checksum = self.data.firmware_checksum
            data.is_custom_build = self.data.is_custom_build
        else:
            data.firmware_current_version = (
                data.device_info.firmware_version or data.device_info.release_version
            )

        now = self.hass.loop.time()
        if now - self._last_firmware_check > FIRMWARE_CHECK_INTERVAL.total_seconds():
            self._last_firmware_check = now
            await self._check_firmware_update(data)

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
        self._last_update_time = now

        # Load history if not already loaded
        if not self._device_history:
            stored_data = await self._store.async_load()
            if stored_data:
                self._device_history = stored_data

        # Update last_seen and initially_seen
        current_time = int(time.time())
        history_updated = False

        # Collect all MAC addresses of the router itself to filter from connected devices
        own_macs = set()
        if data.device_info.mac_address:
            own_macs.add(data.device_info.mac_address.lower())
        for iface in data.network_interfaces:
            if iface.mac_address:
                own_macs.add(iface.mac_address.lower())
        for wifi_iface in data.wireless_interfaces:
            if wifi_iface.mac_address:
                own_macs.add(wifi_iface.mac_address.lower())

        # Filter connected devices and update history
        filtered_devices = []
        for device in data.connected_devices:
            mac = device.mac.lower()
            if mac in own_macs:
                continue

            # Filter out devices with hostnames that look like internal interfaces (e.g. wlan0, eth1)
            # This handles cases like AIoT chips that appear as clients to the main system
            if device.hostname:
                hostname = device.hostname.lower()
                if re.match(r"^(wlan|eth|lan|wan|br-|radio|phy)[0-9]+", hostname):
                    _LOGGER.debug(
                        "Filtering out device tracker with interface-like hostname: %s (%s)",
                        device.hostname,
                        device.mac,
                    )
                    continue

            filtered_devices.append(device)

            if mac not in self._device_history:
                self._device_history[mac] = {
                    "initially_seen": current_time,
                    "last_seen": current_time,
                }
                history_updated = True
            else:
                self._device_history[mac]["last_seen"] = current_time
                history_updated = True

        data.connected_devices = filtered_devices

        if history_updated:
            await self._store.async_save(self._device_history)

        # Update device registry if we have device info
        await self._async_update_device_registry(data)

        return data

    async def _async_update_device_registry(self, data: OpenWrtData) -> None:
        """Update the device registry with fresh device information."""
        if not data.device_info:
            return

        device_info = data.device_info
        device_registry = dr.async_get(self.hass)

        # Identify gateway device for topology mapping
        via_device = None
        if device_info.gateway_mac:
            gw_mac = device_info.gateway_mac.upper()
            for dev in device_registry.devices.values():
                if any(
                    conn[0] == dr.CONNECTION_NETWORK_MAC and conn[1].upper() == gw_mac
                    for conn in dev.connections
                ):
                    if dev.identifiers:
                        via_device = next(iter(dev.identifiers))
                    break

        _LOGGER.debug(
            "Updating device registry for %s: model=%s, mac=%s, via=%s",
            self.config_entry.unique_id or self.config_entry.data[CONF_HOST],
            device_info.model,
            device_info.mac_address,
            via_device,
        )

        device_registry.async_get_or_create(
            config_entry_id=self.config_entry.entry_id,
            identifiers={
                (
                    DOMAIN,
                    self.config_entry.unique_id or self.config_entry.data[CONF_HOST],
                )
            },
            connections={(dr.CONNECTION_NETWORK_MAC, device_info.mac_address.lower())}
            if device_info.mac_address
            else None,
            manufacturer=device_info.release_distribution or ATTR_MANUFACTURER,
            model=device_info.model or device_info.board_name,
            name=device_info.model or device_info.hostname or self.config_entry.title,
            sw_version=device_info.firmware_version,
            hw_version=device_info.board_name,
            via_device=via_device,
            configuration_url=f"http://{self.config_entry.data[CONF_HOST]}",
        )

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
        if not current_version:
            return


        session = async_get_clientsession(self.hass)

        try:
            if "SNAPSHOT" in current_version.upper() and data.device_info.target:
                target = data.device_info.target
                profile_url = f"https://downloads.openwrt.org/snapshots/targets/{target}/profiles.json"
                async with session.get(profile_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        profile_data = await resp.json()
                        version_code = profile_data.get("version_code", "")
                        if version_code:
                            latest_snapshot = f"SNAPSHOT ({version_code})"
                            if self._version_is_newer(current_version, latest_snapshot):
                                data.firmware_latest_version = latest_snapshot
                                data.firmware_upgradable = True

                                board = data.device_info.board_name.replace("_", "-").replace(",", "-")
                                data.firmware_release_url = f"https://downloads.openwrt.org/snapshots/targets/{target}/"

                                profiles = profile_data.get("profiles", {})
                                board_key = data.device_info.board_name.replace("-", "_").replace(",", "_")
                                board_profile = profiles.get(board_key)
                                if board_profile:
                                    images = board_profile.get("images", [])
                                    for img in images:
                                        if "sysupgrade" in img.get("name", ""):
                                            data.firmware_install_url = f"{data.firmware_release_url}{img.get('name')}"
                                            break
                return

            async with session.get(
                OPENWRT_RELEASE_API, timeout=aiohttp.ClientTimeout(total=15)
            ) as response:
                if response.status == 200:
                    versions_data = await response.json()
                    latest_stable = versions_data.get(
                        "stable_version", versions_data.get("latest", "")
                    )
                    if not latest_stable and isinstance(versions_data, dict):
                        for key in sorted(versions_data.keys(), reverse=True):
                            if not key.startswith(".") and not key.startswith("_"):
                                latest_stable = key
                                break

                    if latest_stable:
                        if self._version_is_newer(current_version, latest_stable):
                            data.firmware_latest_version = latest_stable
                            data.firmware_upgradable = True

                            # Try to build a direct sysupgrade URL if we have target/board
                            info = data.device_info
                            if info.target and info.board_name:
                                target = info.target
                                board = info.board_name.replace("_", "-").replace(",", "-")
                                dist = info.release_distribution or "openwrt"
                                # Standard OpenWrt sysupgrade URL pattern
                                data.firmware_install_url = (
                                    f"https://downloads.openwrt.org/releases/{latest_stable}/targets/{target}/"
                                    f"{dist}-{latest_stable}-{target.replace('/', '-')}-{board}-squashfs-sysupgrade.bin"
                                )

                            data.firmware_release_url = (
                                f"https://openwrt.org/releases/{latest_stable}"
                            )
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Failed to check official firmware updates")

    async def _check_asu_update(self, data: OpenWrtData) -> None:
        """Check for updates via the ASU (Attended Sysupgrade) API."""
        if not data.device_info.target or not data.device_info.board_name:
            return

        # Prepare request to ASU
        target = data.device_info.target
        model = data.device_info.board_name

        asu_url = self.config_entry.options.get(
            CONF_ASU_URL,
            self.config_entry.data.get(CONF_ASU_URL, "https://sysupgrade.openwrt.org"),
        )

        session = async_get_clientsession(self.hass)

        try:
            # Check for latest available version for this target on ASU
            # ASU API: GET https://sysupgrade.openwrt.org/api/v1/info?target={target}&model={model}
            url = f"{asu_url.rstrip('/')}/api/v1/info?target={target}&model={model}"
            is_snapshot = "SNAPSHOT" in data.device_info.release_version.upper()
            if is_snapshot:
                url += "&version=SNAPSHOT"

            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == 200:
                    asu_info = await resp.json()
                    version = asu_info.get("version", "")
                    revision = asu_info.get("revision", "")
                    if (revision and "SNAPSHOT" in version.upper()) or (
                        revision and not version
                    ):
                        latest_version = f"{version or 'SNAPSHOT'} ({revision})"
                    else:
                        latest_version = version or revision

                    if (
                        latest_version
                        and self._version_is_newer(
                            data.firmware_current_version or "", latest_version
                        )
                    ):
                        data.asu_update_available = True
                        data.asu_supported = True

                        try:
                            # Fetch installed packages for the update request
                            data.installed_packages = (
                                await self.client.get_installed_packages()
                            )
                        except Exception as e:
                            _LOGGER.debug(
                                "Failed to fetch installed packages for ASU: %s", e
                            )

                        # If ASU has a newer version than official version check, use it
                        if self._version_is_newer(
                            data.firmware_latest_version or "0.0.0", latest_version
                        ):
                            data.firmware_latest_version = latest_version
                            data.firmware_upgradable = True

                            # Try to get direct image URL from ASU for this version
                            images = asu_info.get("images", [])
                            for img in images:
                                if "sysupgrade" in img.get("name", ""):
                                    # ASU URLs are often relative or specific
                                    # We'll use the official releases URL as fallback
                                    # But ASU might provide a direct one if we requested a build
                                    pass

                            data.firmware_release_url = (
                                f"https://openwrt.org/releases/{latest_version}"
                            )
                            # ASU doesn't have a static download URL, it builds on demand
                            data.firmware_install_url = ""
                        _LOGGER.debug("ASU update found: %s", latest_version)
                    elif latest_version:
                        data.asu_supported = True
                elif resp.status == 404 and "," in model:
                    # Retry with underscore instead of comma (common ASU model ID variation)
                    alt_model = model.replace(",", "_")
                    _LOGGER.debug("ASU 404 for %s, retrying with %s", model, alt_model)
                    url = f"{asu_url.rstrip('/')}/api/v1/info?target={target}&model={alt_model}"
                    if "SNAPSHOT" in data.device_info.release_version.upper():
                        url += "&version=SNAPSHOT"
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp2:
                        if resp2.status == 200:
                            asu_info = await resp2.json()
                            version = asu_info.get("version", "")
                            revision = asu_info.get("revision", "")
                            if (revision and "SNAPSHOT" in version.upper()) or (revision and not version):
                                latest_version = f"{version or 'SNAPSHOT'} ({revision})"
                            else:
                                latest_version = version or revision

                            if latest_version and self._version_is_newer(
                                data.firmware_current_version or "", latest_version
                            ):
                                data.asu_update_available = True
                                data.asu_supported = True
                                data.firmware_latest_version = latest_version
                                data.firmware_upgradable = True
        except Exception as err:
            _LOGGER.debug("ASU check failed: %s", err)

    async def _check_custom_firmware_update(
        self, data: OpenWrtData, repo_input: str
    ) -> None:
        """Check for firmware updates from a custom GitHub repository."""
        data.is_custom_build = True
        owner, repo = self._parse_repo(repo_input)
        if not owner or not repo:
            return

        revision = data.device_info.release_revision
        router_hash = ""
        if revision and "-" in revision:
            router_hash = revision.split("-")[-1].strip()

        _LOGGER.debug(
            "Checking custom firmware for %s/%s (router hash: %s)",
            owner,
            repo,
            router_hash,
        )
        session = async_get_clientsession(self.hass)

        try:
            headers = {"Accept": "application/vnd.github+json"}

            url_releases = f"https://api.github.com/repos/{owner}/{repo}/releases"
            async with session.get(
                url_releases, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    return
                releases_data = await resp.json()

            if not releases_data:
                return

            latest_release = releases_data[0]
            latest_tag = latest_release.get("tag_name", "")
            latest_published = latest_release.get("published_at", "")

            if router_hash:
                url_tags = f"https://api.github.com/repos/{owner}/{repo}/tags"
                async with session.get(
                    url_tags, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        tags_data = await resp.json()
                        for tag in tags_data:
                            tag_commit = tag.get("commit", {}).get("sha", "")
                            if router_hash and tag_commit.startswith(router_hash):
                                data.firmware_current_version = tag.get("name")
                                break

            latest_version = latest_tag
            if "SNAPSHOT" in latest_tag.upper() and latest_published:
                date_part = latest_published.split("T")[0]
                latest_version = f"{latest_tag} ({date_part})"
                target_commit = latest_release.get("target_commitish", "")
                if target_commit and len(target_commit) >= 7:
                    latest_version = f"{latest_tag} ({target_commit[:7]})"
                elif date_part:
                    latest_version = f"{latest_tag} ({date_part})"

            data.firmware_latest_version = latest_version
            data.firmware_release_url = latest_release.get("html_url", "")

            is_upgradable = self._version_is_newer(
                data.firmware_current_version or "", latest_tag
            )
            if not is_upgradable and latest_version != latest_tag:
                 is_upgradable = self._version_is_newer(
                     data.firmware_current_version or "", latest_version
                 )
            data.firmware_upgradable = is_upgradable

            assets = latest_release.get("assets", [])
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
                asset_name = best_asset.get("name", "")
                if sha_url:
                    async with session.get(sha_url) as sha_resp:
                        if sha_resp.status == 200:
                            sha_content = await sha_resp.text()
                            for line in sha_content.splitlines():
                                if asset_name in line:
                                    data.firmware_checksum = line.split()[0]
                                    break
        except Exception as err:
            _LOGGER.debug("Failed to check custom firmware: %s", err)

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
                match = re.search(r'r(\d+)-', v)
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
        except (ValueError, AttributeError):
            return current != latest

    async def async_shutdown(self) -> None:
        """Shut down the coordinator and disconnect."""
        await super().async_shutdown()
        await self.client.disconnect()
