# mypy: disable-error-code="attr-defined"
from __future__ import annotations

import logging

from ..base import (
    WifiCredentials,
    WpsStatus,
)
from .exceptions import UbusError

_LOGGER = logging.getLogger(__name__)
UBUS_JSONRPC_VERSION = "2.0"
UBUS_ID_AUTH = 1
UBUS_ID_CALL = 2


class UbusWirelessMixin:
    """Wireless methods for UbusClient."""

    async def get_wps_status(self) -> WpsStatus:
        """Get WPS status from the first wireless interface."""
        if self.packages.wireless is False:
            return WpsStatus()

        # 1. Try network.wireless status
        try:
            wireless_data = await self._call("network.wireless", "status")
            for radio_data in wireless_data.values():
                if not isinstance(radio_data, dict):
                    continue
                for iface in radio_data.get("interfaces", []):
                    iface_name = iface.get("ifname", "")
                    if iface_name:
                        try:
                            result = await self._call(
                                f"hostapd.{iface_name}",
                                "wps_status",
                            )
                            return WpsStatus(
                                enabled=result.get("pbc_status", "") == "Active",
                                status=result.get("pbc_status", "Disabled"),
                            )
                        except UbusError:
                            continue
        except UbusError:
            pass

        # 2. Fallback: discover directly from hostapd.* ubus objects
        try:
            objects = await self._list_objects()
            for obj in objects:
                if obj.startswith("hostapd."):
                    try:
                        result = await self._call(obj, "wps_status")
                        return WpsStatus(
                            enabled=result.get("pbc_status", "") == "Active",
                            status=result.get("pbc_status", "Disabled"),
                        )
                    except UbusError:
                        continue
        except Exception:
            pass

        return WpsStatus()

    async def set_wps(self, enabled: bool) -> bool:
        """Enable or disable WPS."""
        method = "wps_start" if enabled else "wps_cancel"
        success = False

        # 1. Try network.wireless status
        try:
            wireless_data = await self._call("network.wireless", "status")
            for radio_data in wireless_data.values():
                if not isinstance(radio_data, dict):
                    continue
                for iface in radio_data.get("interfaces", []):
                    iface_name = iface.get("ifname", "")
                    if iface_name:
                        try:
                            await self._call(f"hostapd.{iface_name}", method)
                            success = True
                        except UbusError:
                            continue
            if success:
                return True
        except UbusError as err:
            _LOGGER.debug("Failed to get network.wireless status for set_wps: %s", err)

        # 2. Fallback: discover directly from hostapd.* ubus objects
        try:
            objects = await self._list_objects()
            for obj in objects:
                if obj.startswith("hostapd."):
                    try:
                        await self._call(obj, method)
                        success = True
                    except UbusError:
                        continue
            if success:
                return True
        except Exception as err:
            _LOGGER.debug("Failed to list objects for set_wps: %s", err)

        return False

    async def set_wireless_enabled(self, interface: str, enabled: bool) -> bool:
        """Enable or disable a wireless radio via UCI."""
        committed = False
        try:
            action = "0" if enabled else "1"  # disabled=0 means enabled
            await self._call(
                "uci",
                "set",
                {
                    "config": "wireless",
                    "section": interface,
                    "values": {"disabled": action},
                },
            )
            await self._call("uci", "commit", {"config": "wireless"})
            committed = True
            await self._call("network.wireless", "notify")
            self._last_full_poll = 0
            return True
        except UbusError:
            if not committed:
                await self._revert_wireless_changes()
            return False

    async def _revert_wireless_changes(self) -> None:
        """Best-effort discard of an incomplete wireless UCI transaction."""
        try:
            await self._call("uci", "revert", {"config": "wireless"})
        except UbusError as err:
            _LOGGER.debug("Failed to revert incomplete wireless UCI changes: %s", err)

    async def set_wireless_network_enabled(
        self,
        interface: str,
        radio: str,
        enabled: bool,
        *,
        disable_radio: bool,
    ) -> bool:
        """Set an SSID and its radio in one UCI transaction."""
        committed = False
        try:
            if enabled:
                await self._call(
                    "uci",
                    "set",
                    {
                        "config": "wireless",
                        "section": radio,
                        "values": {"disabled": "0"},
                    },
                )
            await self._call(
                "uci",
                "set",
                {
                    "config": "wireless",
                    "section": interface,
                    "values": {"disabled": "0" if enabled else "1"},
                },
            )
            if disable_radio:
                await self._call(
                    "uci",
                    "set",
                    {
                        "config": "wireless",
                        "section": radio,
                        "values": {"disabled": "1"},
                    },
                )
            await self._call("uci", "commit", {"config": "wireless"})
            committed = True
            await self._call("network.wireless", "notify")
            self._last_full_poll = 0
            return True
        except UbusError:
            if not committed:
                await self._revert_wireless_changes()
            return False

    async def get_wifi_credentials(self) -> list[WifiCredentials]:
        """Get wifi credentials via UCI."""
        try:
            uci = await self._call("uci", "get", {"config": "wireless"})
            if not uci or "values" not in uci:
                return []

            creds = []
            for key, val in uci["values"].items():
                if isinstance(val, dict) and val.get(".type") == "wifi-iface":
                    if val.get("mode") == "ap":
                        ssid = val.get("ssid")
                        key_val = val.get("key")
                        if ssid:
                            creds.append(
                                WifiCredentials(
                                    iface=key,
                                    ssid=ssid,
                                    encryption=val.get("encryption", "none"),
                                    key=key_val or "",
                                    hidden=bool(int(val.get("hidden", 0))),
                                )
                            )
            return creds
        except Exception as err:
            _LOGGER.debug("Failed to get wifi credentials via ubus: %s", err)
            return []

    async def trigger_wps_push(self, interface: str) -> bool:
        """Trigger WPS push button on a specific wireless interface via ubus."""

        async def _call_wps(target: str) -> bool:
            try:
                await self._call(target, "wps_start")
                return True
            except UbusError:
                try:
                    await self._call(target, "wps_push")
                    return True
                except UbusError:
                    return False

        # 1. Try direct guess: hostapd.interface
        if await _call_wps(f"hostapd.{interface}"):
            return True

        # 2. List objects and find matching hostapd interface
        try:
            objects = await self._list_objects()
            for obj in objects:
                if obj.startswith("hostapd.") and interface in obj:
                    if await _call_wps(obj):
                        return True
        except Exception as err:
            _LOGGER.debug(
                "Failed to trigger WPS push via ubus for %s: %s", interface, err
            )

        return False
