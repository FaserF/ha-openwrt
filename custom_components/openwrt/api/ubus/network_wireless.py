# mypy: disable-error-code="attr-defined,has-type"
"""Ubus wireless networking mixin for OpenWrt."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from ..base import WirelessInterface
from .exceptions import (
    UbusError,
)

_LOGGER = logging.getLogger(__name__)


def _valid_txpower(value: Any) -> int:
    """Return a positive dBm value, or zero when OpenWrt has no reading."""
    try:
        power = int(float(value))
    except (TypeError, ValueError):
        return 0
    return power if power > 0 else 0


class UbusNetworkWirelessMixin:
    """Mixin for Ubus wireless networking operations."""

    async def get_wireless_interfaces(self) -> list[WirelessInterface]:
        """Get wireless interface information."""
        interfaces: list[WirelessInterface] = []
        iface_names: set[str] = set()
        radio_names: set[str] = set()
        skip_iwinfo_info = self._ubus_path.rstrip("/") == "/cgi-bin/luci/admin/ubus"

        # 1. Primary source: network.wireless status
        if self.packages.wireless is not False:
            try:
                wireless_data = await self._call("network.wireless", "status")
                if wireless_data and isinstance(wireless_data, dict):
                    radio_names.update(wireless_data)
                    for radio_name, radio_data in wireless_data.items():
                        if not isinstance(radio_data, dict):
                            continue

                        radio_interfaces = radio_data.get("interfaces", [])
                        for iface in radio_interfaces:
                            # Prefer the actual kernel interface name (ifname/device)
                            # over the UCI section name. On devices like the Velop WHW03
                            # that use phy*-ap* naming, the section field (e.g.
                            # "default_radio0") differs from the actual device name
                            # (e.g. "phy0-ap0"). Using section as the primary name
                            # prevents the iwinfo step from recognising the real device
                            # name as "already seen", causing duplicate entries.
                            section = iface.get("section", "")
                            ifname = iface.get("ifname") or iface.get("device", "")
                            # Use the actual kernel name if available; fall back to
                            # the UCI section name only when no kernel name exists.
                            iface_name = ifname or section
                            if not iface_name:
                                continue

                            iface_config = iface.get("config", {})
                            iface_disabled = WirelessInterface._uci_disabled(
                                iface_config.get("disabled", False)
                            )
                            wifi = WirelessInterface(
                                name=iface_name,
                                ssid=iface_config.get("ssid", ""),
                                mode=iface_config.get("mode", ""),
                                encryption=iface_config.get("encryption", ""),
                                enabled=not radio_data.get("disabled", False),
                                interface_enabled=not iface_disabled,
                                up=not radio_data.get("disabled", False),
                                radio=radio_name,
                                radio_enabled=not radio_data.get("disabled", False),
                                band=WirelessInterface._band_from_raw(
                                    radio_data.get("config", {}).get("band", "")
                                    or radio_data.get("config", {}).get("hwmode", "")
                                ),
                                htmode=radio_data.get("config", {}).get("htmode", ""),
                                hwmode=radio_data.get("config", {}).get("hwmode", ""),
                                txpower=_valid_txpower(
                                    radio_data.get("config", {}).get("txpower")
                                ),
                                mesh_id=iface_config.get("mesh_id", ""),
                                mesh_fwding=iface_config.get("mesh_fwding", False),
                                section=section,
                                ifname=ifname,
                            )
                            interfaces.append(wifi)
                            # Track both the kernel name and the UCI section name so
                            # the iwinfo step does not create a second entry for the
                            # same physical interface under a different name.
                            iface_names.add(iface_name)
                            if section and section != iface_name:
                                iface_names.add(section)
                            if ifname and ifname != iface_name:
                                iface_names.add(ifname)
            except UbusError:
                _LOGGER.debug(
                    "network.wireless status call failed, trying UCI fallback"
                )
            try:
                uci_wireless = await self._call("uci", "get", {"config": "wireless"})
                if (
                    uci_wireless
                    and isinstance(uci_wireless, dict)
                    and "values" in uci_wireless
                ):
                    vals = uci_wireless["values"]
                    by_section = {
                        wifi.section: wifi for wifi in interfaces if wifi.section
                    }
                    by_name = {wifi.name: wifi for wifi in interfaces if wifi.name}
                    wifi_iface_count = 0
                    for sect_name, sect_data in vals.items():
                        if (
                            not isinstance(sect_data, dict)
                            or sect_data.get(".type") != "wifi-iface"
                        ):
                            continue

                        iface_name = sect_data.get("ifname") or sect_name
                        radio_name = sect_data.get("device", "")
                        radio_config = (
                            vals.get(radio_name, {})
                            if isinstance(vals.get(radio_name), dict)
                            else {}
                        )

                        # Get radio status to determine if enabled
                        radio_disabled = WirelessInterface._uci_disabled(
                            radio_config.get("disabled", "0")
                        )
                        iface_disabled = WirelessInterface._uci_disabled(
                            sect_data.get("disabled", "0")
                        )
                        configured_enabled = not (radio_disabled or iface_disabled)

                        anon_name = f"wifinet{wifi_iface_count}"
                        existing = (
                            by_section.get(sect_name)
                            or by_section.get(anon_name)
                            or by_name.get(iface_name)
                            or by_name.get(sect_name)
                            or by_name.get(anon_name)
                        )
                        wifi_iface_count += 1

                        if existing is not None:
                            existing.section = sect_name
                            existing.radio = existing.radio or radio_name
                            existing.radio_enabled = not radio_disabled
                            existing.interface_enabled = not iface_disabled
                            existing.enabled = configured_enabled
                            existing.up = existing.up and configured_enabled
                            existing.ssid = existing.ssid or sect_data.get("ssid", "")
                            existing.mode = existing.mode or sect_data.get("mode", "")
                            existing.encryption = existing.encryption or sect_data.get(
                                "encryption", ""
                            )
                            existing.hwmode = existing.hwmode or radio_config.get(
                                "hwmode", ""
                            )
                            existing.band = (
                                existing.band
                                or WirelessInterface._band_from_raw(
                                    radio_config.get("band", "")
                                    or radio_config.get("hwmode", "")
                                )
                            )
                            iface_names.add(iface_name)
                            iface_names.add(sect_name)
                            continue

                        wifi = WirelessInterface(
                            name=iface_name,
                            ssid=sect_data.get("ssid", ""),
                            mode=sect_data.get("mode", ""),
                            encryption=sect_data.get("encryption", ""),
                            enabled=configured_enabled,
                            interface_enabled=not iface_disabled,
                            up=configured_enabled,
                            radio=radio_name,
                            radio_enabled=not radio_disabled,
                            band=WirelessInterface._band_from_raw(
                                radio_config.get("band", "")
                                or radio_config.get("hwmode", "")
                            ),
                            hwmode=radio_config.get("hwmode", ""),
                            txpower=_valid_txpower(radio_config.get("txpower")),
                            section=sect_name,
                            ifname=sect_data.get("ifname", ""),
                        )
                        interfaces.append(wifi)
                        iface_names.add(iface_name)
                        iface_names.add(sect_name)
            except Exception as e:
                _LOGGER.debug("UCI wireless fallback failed: %s", e)

        # 2. Supplement/Fallback: iwinfo devices and hostapd.* objects
        # This is critical for devices where interfaces aren't in network.wireless or UCI names differ
        try:
            candidates = []
            try:
                iw_devs = await self._call("iwinfo", "devices")
                if isinstance(iw_devs, list):
                    candidates.extend(iw_devs)
                elif isinstance(iw_devs, dict) and "devices" in iw_devs:
                    candidates.extend(iw_devs["devices"])
            except Exception:
                _LOGGER.debug("iwinfo devices call failed")

            if self._connected:
                try:
                    ubus_objs = await self._list_objects()
                    for obj in ubus_objs:
                        if obj.startswith("hostapd."):
                            h_name = obj.split(".", 1)[1]
                            if h_name and h_name not in candidates:
                                candidates.append(h_name)
                except Exception:
                    pass

            for name in candidates:
                if skip_iwinfo_info or name in iface_names or name in radio_names:
                    continue

                # Check if any existing interface from UCI matches this physical device
                found_match = False
                try:
                    info = await self._call("iwinfo", "info", {"device": name})
                    if info and info.get("ssid"):
                        # Try to match with a UCI section by SSID and band
                        physical_band = WirelessInterface._band_from_raw(
                            info.get("frequency", "") or info.get("hwmode", "")
                        )
                        for wifi in interfaces:
                            if (
                                not wifi.ifname or wifi.ifname == wifi.section
                            ) and wifi.ssid == info.get("ssid"):
                                if (
                                    not wifi.band
                                    or not physical_band
                                    or wifi.band == physical_band
                                ):
                                    wifi.name = name
                                    wifi.ifname = name
                                    iface_names.add(name)
                                    found_match = True
                                    break
                except Exception:
                    pass

                if not found_match:
                    try:
                        h_status = await self._call(f"hostapd.{name}", "get_status")
                        if (
                            h_status
                            and isinstance(h_status, dict)
                            and h_status.get("ssid")
                        ):
                            h_ssid = h_status.get("ssid")
                            for wifi in interfaces:
                                if (
                                    not wifi.ifname or wifi.ifname == wifi.section
                                ) and (wifi.ssid == h_ssid or not wifi.ssid):
                                    wifi.name = name
                                    wifi.ifname = name
                                    if not wifi.ssid and h_ssid:
                                        wifi.ssid = str(h_ssid)
                                    iface_names.add(name)
                                    found_match = True
                                    break
                    except Exception:
                        pass

                if not found_match:
                    # Found a new interface not in UCI status
                    wifi = WirelessInterface(name=name, enabled=True, up=True)
                    interfaces.append(wifi)
                    iface_names.add(name)
        except Exception as err:
            _LOGGER.debug("Supplemental wireless interface discovery failed: %s", err)

        # 3. Populate metrics for all discovered interfaces in parallel
        async def _fetch_metrics(wifi: WirelessInterface) -> None:
            try:
                iwinfo = {}
                if not skip_iwinfo_info:
                    try:
                        async with asyncio.timeout(5):
                            iwinfo = await self._call(
                                "iwinfo", "info", {"device": wifi.name}
                            )
                    except TimeoutError:
                        _LOGGER.debug(
                            "iwinfo info timed out for %s, skipping", wifi.name
                        )
                if iwinfo:
                    if not wifi.ssid:
                        wifi.ssid = iwinfo.get("ssid", "")
                    wifi.mac_address = iwinfo.get("bssid", "").upper()
                    wifi.channel = iwinfo.get("channel", 0)
                    reported_txpower = _valid_txpower(iwinfo.get("txpower"))
                    if reported_txpower:
                        wifi.txpower = reported_txpower
                    wifi.frequency = str(iwinfo.get("frequency", ""))
                    # Re-resolve band from frequency if not already set
                    if not wifi.band and wifi.frequency:
                        wifi.band = WirelessInterface._band_from_raw(wifi.frequency)

                    # Fallback: Infer from channel if frequency is missing or empty
                    if (
                        not wifi.frequency or wifi.frequency == "None"
                    ) and wifi.channel > 0:
                        if 1 <= wifi.channel <= 14:
                            wifi.frequency = "2.4 GHz"
                        elif 32 <= wifi.channel <= 177:
                            wifi.frequency = "5 GHz"
                        elif 182 <= wifi.channel <= 196:
                            wifi.frequency = "6 GHz"

                    wifi.signal = iwinfo.get("signal", 0)
                    wifi.noise = iwinfo.get("noise", 0)
                    wifi.bitrate = (
                        iwinfo.get("bitrate", 0) / 1000.0
                        if iwinfo.get("bitrate")
                        else 0.0
                    )
                    q_val = iwinfo.get("quality")
                    q_max = iwinfo.get("quality_max", 100)
                    if q_val is not None and q_max:
                        wifi.quality = round((q_val / q_max) * 100, 1)

                    if iwinfo.get("txpower_offset") is not None:
                        wifi.txpower_offset = iwinfo["txpower_offset"]

                    if "hwmode" in iwinfo and not wifi.hwmode:
                        if isinstance(iwinfo["hwmode"], list):
                            wifi.hwmode = "/".join(iwinfo["hwmode"])
                        else:
                            wifi.hwmode = str(iwinfo["hwmode"])
                    if "htmode" in iwinfo and not wifi.htmode:
                        wifi.htmode = str(iwinfo["htmode"])

                # Association list
                candidate_names = [wifi.name]
                if wifi.ifname and wifi.ifname not in candidate_names:
                    candidate_names.append(wifi.ifname)
                if wifi.section and wifi.section not in candidate_names:
                    candidate_names.append(wifi.section)

                for cand in candidate_names:
                    with contextlib.suppress(Exception):
                        assoc = await self._call(
                            "iwinfo", "assoclist", {"device": cand}
                        )
                        if assoc and isinstance(assoc, dict):
                            results = assoc.get("results", [])
                            if results:
                                wifi.clients_count = len(results)
                                break

                if not wifi.clients_count:
                    for cand in candidate_names:
                        with contextlib.suppress(Exception):
                            hostapd_clients = await self._call(
                                f"hostapd.{cand}", "get_clients"
                            )
                            if hostapd_clients and isinstance(hostapd_clients, dict):
                                clients = hostapd_clients.get("clients", {})
                                count = sum(
                                    1
                                    for c in clients.values()
                                    if isinstance(c, dict) and c.get("authorized", True)
                                )
                                if count > 0:
                                    wifi.clients_count = count
                                    break

            except (TimeoutError, UbusError):
                _LOGGER.debug(
                    "Failed to fetch detailed info for wifi interface %s", wifi.name
                )
                if (
                    self.coordinator
                    and self.coordinator.data
                    and self.coordinator.data.wireless_interfaces
                ):
                    for prev_wifi in self.coordinator.data.wireless_interfaces:
                        if prev_wifi.name == wifi.name:
                            wifi.ssid = prev_wifi.ssid
                            wifi.mac_address = prev_wifi.mac_address
                            wifi.channel = prev_wifi.channel
                            wifi.frequency = prev_wifi.frequency
                            wifi.signal = prev_wifi.signal
                            wifi.noise = prev_wifi.noise
                            wifi.bitrate = prev_wifi.bitrate
                            wifi.quality = prev_wifi.quality
                            wifi.hwmode = prev_wifi.hwmode
                            wifi.encryption = prev_wifi.encryption
                            wifi.clients_count = prev_wifi.clients_count
                            wifi.enabled = prev_wifi.enabled
                            wifi.up = prev_wifi.up
                            wifi.radio = prev_wifi.radio
                            wifi.htmode = prev_wifi.htmode
                            wifi.txpower = prev_wifi.txpower
                            wifi.txpower_offset = prev_wifi.txpower_offset
                            wifi.mesh_id = prev_wifi.mesh_id
                            wifi.mesh_fwding = prev_wifi.mesh_fwding
                            wifi.ifname = prev_wifi.ifname
                            wifi.section = prev_wifi.section
                            wifi.band = prev_wifi.band
                            wifi.width = prev_wifi.width
                            wifi.standard = prev_wifi.standard
                            break

        if interfaces:
            await asyncio.gather(*[_fetch_metrics(w) for w in interfaces])

        # 4. Deduplicate and clean up
        unique_ifaces: list[WirelessInterface] = []
        seen_keys: set[str] = set()

        for wifi in interfaces:
            # Skip interfaces that are clearly not operational or redundant placeholders
            if not wifi.mac_address and not wifi.ssid:
                _LOGGER.debug(
                    "Skipping non-operational wireless interface: %s", wifi.name
                )
                continue

            # Create a key for deduplication
            if wifi.mac_address:
                key = f"mac_{wifi.mac_address}"
            elif wifi.ssid and wifi.radio:
                key = f"ssid_radio_{wifi.ssid}_{wifi.radio}"
            elif wifi.section:
                key = f"section_{wifi.section}"
            else:
                key = f"name_{wifi.name}"

            if key not in seen_keys:
                unique_ifaces.append(wifi)
                seen_keys.add(key)
            else:
                # Merge data if this one has more info
                for existing in unique_ifaces:
                    if (
                        wifi.mac_address and existing.mac_address == wifi.mac_address
                    ) or (
                        wifi.ssid
                        and wifi.radio
                        and existing.ssid == wifi.ssid
                        and existing.radio == wifi.radio
                    ):
                        if not existing.ssid:
                            existing.ssid = wifi.ssid
                        if not existing.mac_address:
                            existing.mac_address = wifi.mac_address
                        existing.clients_count = wifi.clients_count
                        break

        return unique_ifaces
