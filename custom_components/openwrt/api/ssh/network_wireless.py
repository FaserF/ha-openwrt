# mypy: disable-error-code="attr-defined,has-type"
"""SSH wireless networking mixin for OpenWrt."""

from __future__ import annotations

import contextlib
import json
import logging
import re
import shlex

from ..base import WifiCredentials, WirelessInterface, WpsStatus

_LOGGER = logging.getLogger(__name__)


class SshNetworkWirelessMixin:
    """Mixin for SSH wireless networking operations."""

    async def get_wireless_interfaces(self) -> list[WirelessInterface]:
        """Get wireless interfaces via ubus iwinfo."""
        interfaces: list[WirelessInterface] = []
        if self.packages.wireless is False:
            return interfaces
        iface_names: set[str] = set()

        # 1. Primary source: network.wireless status
        try:
            wifi_json = await self._exec(
                "ubus call network.wireless status 2>/dev/null"
            )
            if wifi_json and wifi_json.strip().startswith("{"):
                data = json.loads(wifi_json)
                for radio_name, radio_data in data.items():
                    if not isinstance(radio_data, dict):
                        continue
                    for iface in radio_data.get("interfaces", []):
                        config = iface.get("config", {})
                        iface_name = (
                            iface.get("ifname")
                            or iface.get("section")
                            or iface.get("device", "")
                        )
                        if not iface_name or iface_name in iface_names:
                            continue

                        iface_disabled = WirelessInterface._uci_disabled(
                            config.get("disabled", False)
                        )
                        radio_disabled = WirelessInterface._uci_disabled(
                            radio_data.get("disabled", False)
                        )
                        wifi = WirelessInterface(
                            name=iface_name,
                            ssid=config.get("ssid", ""),
                            mode=config.get("mode", ""),
                            encryption=config.get("encryption", ""),
                            enabled=not (radio_disabled or iface_disabled),
                            interface_enabled=not iface_disabled,
                            up=radio_data.get("up", False),
                            radio=radio_name,
                            radio_enabled=not radio_disabled,
                            band=WirelessInterface._band_from_raw(
                                radio_data.get("config", {}).get("band", "")
                                or radio_data.get("config", {}).get("hwmode", "")
                            ),
                            hwmode=radio_data.get("config", {}).get("hwmode", ""),
                            section=iface.get("section"),
                            ifname=iface.get("ifname"),
                        )
                        interfaces.append(wifi)
                        iface_names.add(iface_name)
                        if wifi.section and wifi.section != iface_name:
                            iface_names.add(wifi.section)
                        if wifi.ifname and wifi.ifname != iface_name:
                            iface_names.add(wifi.ifname)
        except Exception as err:
            _LOGGER.debug("Failed to get network.wireless status via SSH: %s", err)

        # 2. Supplement: iwinfo devices
        try:
            iw_devs_str = await self._exec("ubus call iwinfo devices 2>/dev/null")
            if iw_devs_str and iw_devs_str.strip().startswith("{"):
                iw_devs = json.loads(iw_devs_str).get("devices", [])
                for name in iw_devs:
                    if name not in iface_names:
                        wifi = WirelessInterface(name=name, enabled=True, up=True)
                        interfaces.append(wifi)
                        iface_names.add(name)
        except Exception as err:
            _LOGGER.debug("network.wireless status failed via SSH: %s", err)

        # 2. Always supplement runtime data with UCI. OpenWrt omits disabled
        # interfaces (and all interfaces of a disabled radio) from runtime status.
        if self.packages.wireless is not False:
            try:
                uci_wireless_str = await self._exec("uci export wireless 2>/dev/null")
                if uci_wireless_str:
                    sections: dict[str, dict[str, str]] = {}
                    current_section = ""
                    for line in uci_wireless_str.splitlines():
                        line = line.strip()
                        if line.startswith("config"):
                            parts = line.split()
                            if len(parts) >= 3:
                                current_section = parts[2].strip("'\"")
                                sections[current_section] = {".type": parts[1]}
                        elif line.startswith("option") and current_section:
                            parts = line.split(None, 2)
                            if len(parts) >= 3:
                                sections[current_section][parts[1]] = parts[2].strip(
                                    "'\""
                                )

                    by_section = {
                        wifi.section: wifi for wifi in interfaces if wifi.section
                    }
                    for sect_name, sect_data in sections.items():
                        if sect_data.get(".type") != "wifi-iface":
                            continue

                        iface_name = sect_data.get("ifname") or sect_name
                        radio_name = sect_data.get("device", "")
                        radio_config = sections.get(radio_name, {})
                        radio_disabled = WirelessInterface._uci_disabled(
                            radio_config.get("disabled", "0")
                        )
                        iface_disabled = WirelessInterface._uci_disabled(
                            sect_data.get("disabled", "0")
                        )
                        configured_enabled = not (radio_disabled or iface_disabled)

                        existing = by_section.get(sect_name)
                        if existing is not None:
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
                            section=sect_name,
                            ifname=sect_data.get("ifname", ""),
                        )
                        interfaces.append(wifi)
                        by_section[sect_name] = wifi
                        iface_names.add(iface_name)
                        if sect_name and sect_name != iface_name:
                            iface_names.add(sect_name)
                        if wifi.ifname and wifi.ifname != iface_name:
                            iface_names.add(wifi.ifname)
            except Exception as e:
                _LOGGER.debug("UCI wireless supplement failed via SSH: %s", e)

        # 3. Populate metrics via ubus iwinfo
        for wifi in interfaces:
            iface_name = wifi.name
            try:
                # Get basic info
                safe_arg = shlex.quote(json.dumps({"device": iface_name}))
                info_str = await self._exec(
                    f"ubus call iwinfo info {safe_arg} 2>/dev/null"
                )
                if info_str and info_str.strip().startswith("{"):
                    info = json.loads(info_str)
                    if not wifi.ssid:
                        wifi.ssid = info.get("ssid", "")
                    wifi.mac_address = info.get("bssid", "").upper()
                    wifi.channel = info.get("channel", 0)
                    wifi.frequency = str(info.get("frequency", ""))
                    # Re-resolve band from frequency if not already set
                    if not wifi.band and wifi.frequency:
                        wifi.band = WirelessInterface._band_from_raw(wifi.frequency)
                    wifi.signal = info.get("signal", 0)
                    wifi.noise = info.get("noise", 0)
                    wifi.bitrate = (
                        (info.get("bitrate", 0) / 1000.0)
                        if info.get("bitrate")
                        else 0.0
                    )

                    # Quality
                    q_val = info.get("quality")
                    q_max = info.get("quality_max", 100)
                    if q_val is not None and q_max:
                        wifi.quality = round((q_val / q_max) * 100, 1)

                    if info.get("txpower") is not None:
                        wifi.txpower = info["txpower"]
                    if info.get("txpower_offset") is not None:
                        wifi.txpower_offset = info["txpower_offset"]

                    # Association list for client count
                    assoc_str = await self._exec(
                        f"ubus call iwinfo assoclist {safe_arg} 2>/dev/null"
                    )
                    if assoc_str and assoc_str.strip().startswith("{"):
                        assoc = json.loads(assoc_str).get("results", [])
                        wifi.clients_count = len(assoc)

                    if not wifi.clients_count:
                        with contextlib.suppress(Exception):
                            safe_obj = shlex.quote(f"hostapd.{iface_name}")
                            hostapd_clients = await self._exec(
                                f"ubus call {safe_obj} get_clients 2>/dev/null"
                            )
                            if hostapd_clients and hostapd_clients.strip().startswith(
                                "{"
                            ):
                                hc = json.loads(hostapd_clients).get("clients", {})
                                wifi.clients_count = len(hc)
            except Exception as err:
                _LOGGER.debug(
                    "Failed to get iwinfo for %s via SSH: %s", iface_name, err
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

        return interfaces

    async def _exec_wireless_mutation(self, commands: list[str]) -> bool:
        """Execute wireless commands and preserve their remote exit status."""
        command = " && ".join(commands)
        output = await self._exec(f'{command}; printf "\\n__HA_RC__:%s" $?')
        _prefix, marker, status = output.rpartition("__HA_RC__:")
        return marker == "__HA_RC__:" and status.strip() == "0"

    async def set_wireless_enabled(self, interface: str, enabled: bool) -> bool:
        """Enable/disable a wireless interface."""
        try:
            match = re.match(r"^wifinet(\d+)$", interface, re.IGNORECASE)
            target = f"@wifi-iface[{match.group(1)}]" if match else interface
            action = "0" if enabled else "1"
            safe_val = shlex.quote(f"wireless.{target}.disabled={action}")
            if not await self._exec_wireless_mutation(
                [f"uci set {safe_val}", "uci commit wireless", "wifi reload"]
            ):
                return False
            self._invalidate_slow_poll()
            return True
        except Exception as err:
            _LOGGER.exception("Failed to set wireless %s: %s", interface, err)
            return False

    async def set_wireless_network_enabled(
        self,
        interface: str,
        radio: str,
        enabled: bool,
        *,
        disable_radio: bool,
        ssid: str = "",
    ) -> bool:
        """Set an SSID and its radio in one UCI transaction."""
        try:
            match = re.match(r"^wifinet(\d+)$", interface, re.IGNORECASE)
            target = f"@wifi-iface[{match.group(1)}]" if match else interface
            assignments = []
            if enabled:
                assignments.append(f"wireless.{radio}.disabled=0")
            assignments.append(f"wireless.{target}.disabled={0 if enabled else 1}")
            if disable_radio:
                assignments.append(f"wireless.{radio}.disabled=1")
            commands = [f"uci set {shlex.quote(value)}" for value in assignments]
            commands.extend(("uci commit wireless", "wifi reload"))
            if not await self._exec_wireless_mutation(commands):
                return False
            self._invalidate_slow_poll()
            return True
        except Exception as err:
            _LOGGER.exception("Failed to coordinate wireless network: %s", err)
            return False

    async def get_wifi_credentials(self) -> list[WifiCredentials]:
        """Get wifi credentials via SSH."""
        try:
            # Try ubus first
            stdout = await self._exec(
                'ubus call uci get \'{"config":"wireless"}\' 2>/dev/null'
            )
            if stdout and stdout.startswith("{"):
                data = json.loads(stdout)
                creds = []
                for name, val in data.get("values", {}).items():
                    if val.get(".type") == "wifi-iface" and val.get("mode") == "ap":
                        creds.append(
                            WifiCredentials(
                                iface=name,
                                ssid=val.get("ssid", ""),
                                encryption=val.get("encryption", "none"),
                                key=val.get("key", ""),
                                hidden=bool(int(val.get("hidden", 0))),
                            )
                        )
                return creds

            # Fallback to uci export
            stdout = await self._exec("uci export wireless 2>/dev/null")
            if not stdout:
                return []

            creds = []
            current_iface = None
            ssid = None
            key = None
            enc = None
            hidden = False

            for line in stdout.splitlines():
                line = line.strip()
                if line.startswith("config wifi-iface"):
                    if ssid:
                        creds.append(
                            WifiCredentials(
                                iface=current_iface or "",
                                ssid=ssid,
                                encryption=enc or "none",
                                key=key or "",
                                hidden=hidden,
                            )
                        )
                    parts = line.split()
                    current_iface = (
                        parts[-1].strip("'") if len(parts) > 2 else "unknown"
                    )
                    ssid = None
                    key = None
                    enc = None
                    hidden = False
                elif line.startswith("option ssid"):
                    parts = line.split("'")
                    if len(parts) > 1:
                        ssid = parts[1]
                elif line.startswith("option key"):
                    parts = line.split("'")
                    if len(parts) > 1:
                        key = parts[1]
                elif line.startswith("option encryption"):
                    parts = line.split("'")
                    if len(parts) > 1:
                        enc = parts[1]
                elif line.startswith("option hidden"):
                    parts = line.split("'")
                    if len(parts) > 1:
                        hidden = parts[1] == "1"

            if ssid:
                creds.append(
                    WifiCredentials(
                        iface=current_iface or "",
                        ssid=ssid,
                        encryption=enc or "none",
                        key=key or "",
                        hidden=hidden,
                    )
                )

            return creds
        except Exception as err:
            _LOGGER.debug("Failed to get wifi credentials via ssh: %s", err)
            return []

    async def get_wps_status(self) -> WpsStatus:
        """Get WPS status via SSH."""
        if self.packages.wireless is False:
            return WpsStatus()
        try:
            # Check active PBC status on hostapd interfaces
            out = await self._exec("ubus list 'hostapd.*' 2>/dev/null")
            if out:
                for line in out.strip().splitlines():
                    obj = line.strip()
                    if obj:
                        status_out = await self._exec(
                            f"ubus call {obj} wps_status 2>/dev/null"
                        )
                        if status_out and "Active" in status_out:
                            return WpsStatus(enabled=True, status="Active")
            return WpsStatus(enabled=False, status="Disabled")
        except Exception as err:
            _LOGGER.debug("Failed to get WPS status via ssh: %s", err)
            return WpsStatus()

    async def set_wps(self, enabled: bool) -> bool:
        """Enable or disable WPS via SSH."""
        method = "wps_start" if enabled else "wps_cancel"
        try:
            out = await self._exec("ubus list 'hostapd.*' 2>/dev/null")
            if out:
                success = False
                for line in out.strip().splitlines():
                    obj = line.strip()
                    if obj:
                        res = await self._exec(f"ubus call {obj} {method} 2>/dev/null")
                        if res is not None:
                            success = True
                return success
        except Exception as err:
            _LOGGER.debug("Failed to set WPS via ssh: %s", err)
        return False

    async def trigger_wps_push(self, interface: str) -> bool:
        """Trigger WPS push button via SSH."""
        try:
            # Try ubus call first with wps_start, then hostapd_cli
            safe_iface = shlex.quote(interface)
            try:
                await self.execute_command(
                    f"ubus call hostapd.{safe_iface} wps_start 2>/dev/null || hostapd_cli -i {safe_iface} wps_push"
                )
                return True
            except Exception:
                await self.execute_command(f"hostapd_cli -i {safe_iface} wps_push")
                return True
        except Exception as err:
            _LOGGER.debug(
                "Failed to trigger WPS push via ssh for %s: %s", interface, err
            )
            return False
