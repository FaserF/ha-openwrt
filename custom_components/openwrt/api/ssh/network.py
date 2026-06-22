# mypy: disable-error-code="attr-defined"
from __future__ import annotations

import contextlib
import json
import logging
import shlex
from typing import Any

from ..base import (
    LldpNeighbor,
    MwanStatus,
    NetworkInterface,
    UpnpMapping,
    WifiCredentials,
    WireGuardInterface,
    WireGuardPeer,
    WirelessInterface,
)
from .exceptions import *

_LOGGER = logging.getLogger(__name__)


class SshNetworkMixin:
    """Network methods for SshClient."""

    async def get_external_ip(self) -> str | None:
        """Get public/external IP address."""
        try:
            # 1. Try to find the default gateway interface
            route_info = await self.execute_command("ip route show default 2>/dev/null")
            wan_iface = None
            if route_info and "dev " in route_info:
                parts = route_info.split()
                try:
                    dev_idx = parts.index("dev")
                    wan_iface = parts[dev_idx + 1]
                except ValueError, IndexError:
                    pass

            # 2. Get interface dump
            status = await self._exec("ubus call network.interface dump 2>/dev/null")
            if status and status.startswith("{"):
                data = json.loads(status)
                for iface_data in data.get("interface", []):
                    iface_id = iface_data.get("interface", "").lower()
                    l3_dev = iface_data.get("l3_device", "").lower()

                    # Match by name or by the device found in routes
                    if iface_id in ["wan", "wan6", "wwan", "modem"] or (
                        wan_iface and (iface_id == wan_iface or l3_dev == wan_iface)
                    ):
                        ipv4_addrs = iface_data.get("ipv4-address", [])
                        if ipv4_addrs:
                            ip = ipv4_addrs[0].get("address")
                            # If it's a private IP, we might want to try an external check
                            if ip and not ip.startswith(
                                (
                                    "192.168.",
                                    "10.",
                                    "172.16.",
                                    "172.17.",
                                    "172.18.",
                                    "172.19.",
                                    "172.20.",
                                    "172.21.",
                                    "172.22.",
                                    "172.23.",
                                    "172.24.",
                                    "172.25.",
                                    "172.26.",
                                    "172.27.",
                                    "172.28.",
                                    "172.29.",
                                    "172.30.",
                                    "172.31.",
                                )
                            ):
                                return ip

                            # If we found a private IP but no public one yet, keep it as fallback
                            fallback_ip = ip

            # 3. Fallback: Try to get real public IP via external service if we only have a private one or none
            external_check = await self.execute_command(
                "curl -s http://icanhazip.com || wget -qO- http://icanhazip.com || curl -s https://api.ipify.org || wget -qO- https://api.ipify.org 2>/dev/null"
            )
            if external_check and "." in external_check:
                return external_check.strip().splitlines()[0]

            return fallback_ip if "fallback_ip" in locals() else None
        except Exception:  # noqa: BLE001
            return None

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

                        wifi = WirelessInterface(
                            name=iface_name,
                            ssid=config.get("ssid", ""),
                            mode=config.get("mode", ""),
                            encryption=config.get("encryption", ""),
                            enabled=not radio_data.get("disabled", False),
                            up=radio_data.get("up", False),
                            radio=radio_name,
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

        # 2. UCI fallback if no interfaces found via ubus
        if not interfaces:
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

                    for sect_name, sect_data in sections.items():
                        if sect_data.get(".type") != "wifi-iface":
                            continue

                        iface_name = sect_data.get("ifname") or sect_name
                        radio_name = sect_data.get("device", "")
                        radio_disabled = (
                            sections.get(radio_name, {}).get("disabled", "0") == "1"
                        )
                        iface_disabled = sect_data.get("disabled", "0") == "1"

                        ifname_val = sect_data.get("ifname")
                        is_disabled = radio_disabled or iface_disabled

                        wifi = WirelessInterface(
                            name=iface_name,
                            ssid=sect_data.get("ssid", ""),
                            mode=sect_data.get("mode", ""),
                            encryption=sect_data.get("encryption", ""),
                            enabled=not is_disabled,
                            up=not is_disabled,
                            radio=radio_name,
                            hwmode=sections.get(radio_name, {}).get("hwmode", ""),
                            section=sect_name,
                            ifname=ifname_val or "",
                        )
                        # Only add if not explicitly disabled or if we have no other choice
                        if not is_disabled:
                            interfaces.append(wifi)
                            iface_names.add(iface_name)
                            if sect_name and sect_name != iface_name:
                                iface_names.add(sect_name)
                            if ifname_val and ifname_val != iface_name:
                                iface_names.add(ifname_val)
            except Exception as e:
                _LOGGER.debug("UCI wireless fallback failed via SSH: %s", e)

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
                if self.coordinator and self.coordinator.data and self.coordinator.data.wireless_interfaces:
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
                            wifi.mesh_id = prev_wifi.mesh_id
                            wifi.mesh_fwding = prev_wifi.mesh_fwding
                            wifi.ifname = prev_wifi.ifname
                            wifi.section = prev_wifi.section
                            wifi.band = prev_wifi.band
                            wifi.width = prev_wifi.width
                            wifi.standard = prev_wifi.standard
                            break

        return interfaces

    async def get_upnp_mappings(self) -> list[UpnpMapping]:
        """Get active UPnP/NAT-PMP port mappings via SSH."""
        mappings: list[UpnpMapping] = []
        try:
            stdout = await self._exec("ubus call upnp get_mappings 2>/dev/null")
            if not stdout or not stdout.strip().startswith("{"):
                return mappings

            res = json.loads(stdout)
            if "mappings" not in res:
                return mappings

            for m in res["mappings"]:
                mappings.append(
                    UpnpMapping(
                        protocol=m.get("protocol", "TCP").upper(),
                        external_port=int(m.get("ext_port", 0)),
                        internal_ip=m.get("int_addr", ""),
                        internal_port=int(m.get("int_port", 0)),
                        description=m.get("descr", ""),
                        enabled=bool(m.get("enabled", True)),
                    )
                )
        except Exception as err:
            _LOGGER.debug("Failed to fetch UPnP mappings via SSH: %s", err)

        return mappings

    async def get_wireguard_interfaces(self) -> list[WireGuardInterface]:
        """Get WireGuard VPN interface and peer information via SSH."""
        interfaces: list[WireGuardInterface] = []
        try:
            # 1. Discover WG interfaces via ubus call
            status_str = await self._exec(
                "ubus call network.interface dump 2>/dev/null"
            )
            if not status_str or not status_str.strip().startswith("{"):
                return interfaces

            status = json.loads(status_str)
            wg_ifaces: dict[str, bool] = {}
            for iface_data in status.get("interface", []):
                if iface_data.get("proto") == "wireguard":
                    wg_ifaces[iface_data.get("interface")] = bool(iface_data.get("up"))

            if not wg_ifaces:
                return interfaces

            # 2. Fetch peer info via wg show all dump
            stdout = await self._exec("wg show all dump 2>/dev/null")
            if not stdout:
                return interfaces

            iface_map: dict[str, WireGuardInterface] = {}
            for line in stdout.splitlines():
                parts = line.split("\t")
                if len(parts) == 4:
                    ifname = parts[0]
                    if ifname not in wg_ifaces:
                        continue
                    iface = WireGuardInterface(
                        name=ifname,
                        enabled=wg_ifaces[ifname],
                        public_key=parts[1],
                        listen_port=int(parts[2]) if parts[2].isdigit() else 0,
                        fwmark=int(parts[3]) if parts[3].isdigit() else 0,
                    )
                    iface_map[ifname] = iface
                    interfaces.append(iface)
                elif len(parts) >= 8:
                    ifname = parts[0]
                    if ifname in iface_map:
                        peer = WireGuardPeer(
                            public_key=parts[1],
                            endpoint=parts[3] if parts[3] != "(none)" else "",
                            allowed_ips=(
                                parts[4].split(",") if parts[4] != "(none)" else []
                            ),
                            latest_handshake=int(parts[5]) if parts[5].isdigit() else 0,
                            transfer_rx=int(parts[6]) if parts[6].isdigit() else 0,
                            transfer_tx=int(parts[7]) if parts[7].isdigit() else 0,
                            persistent_keepalive=(
                                int(parts[8])
                                if len(parts) > 8 and parts[8].isdigit()
                                else 0
                            ),
                        )
                        iface_map[ifname].peers.append(peer)
        except Exception as err:
            _LOGGER.debug("Failed to fetch WireGuard interfaces via SSH: %s", err)

        return interfaces

    async def get_network_interfaces(self) -> list[NetworkInterface]:
        """Get network interfaces."""
        interfaces: list[NetworkInterface] = []

        try:
            dump = await self._exec("ubus call network.interface dump 2>/dev/null")
            if dump and dump.strip().startswith("{"):
                data = json.loads(dump)
                for iface_data in data.get("interface", []):
                    iface = NetworkInterface(
                        name=iface_data.get("interface", ""),
                        up=iface_data.get("up", False),
                        protocol=iface_data.get("proto", ""),
                        device=iface_data.get(
                            "l3_device",
                            iface_data.get("device", ""),
                        ),
                        uptime=iface_data.get("uptime", 0),
                    )
                    ipv4 = iface_data.get("ipv4-address", [])
                    if ipv4:
                        iface.ipv4_address = ipv4[0].get("address", "")
                    ipv6 = iface_data.get("ipv6-address", [])
                    if ipv6:
                        iface.ipv6_address = ipv6[0].get("address", "")
                    iface.dns_servers = iface_data.get("dns-server", [])
                    interfaces.append(iface)

            # 2. Fetch all device statistics and link status
            dev_status_str = await self._exec(
                "ubus call network.device status 2>/dev/null"
            )
            if dev_status_str and dev_status_str.strip().startswith("{"):
                device_stats = json.loads(dev_status_str)
                for iface in interfaces:
                    dev_name = iface.device
                    if dev_name and dev_name in device_stats:
                        dev_status = device_stats[dev_name]
                        iface.is_link_up = dev_status.get("link", False)
                        iface.link_speed = dev_status.get("speed", 0)
                        iface.link_duplex = (
                            "full" if dev_status.get("full_duplex") else "half"
                        )

                        stats = dev_status.get("statistics", {})
                        iface.rx_bytes = stats.get("rx_bytes", 0)
                        iface.tx_bytes = stats.get("tx_bytes", 0)
                        iface.rx_packets = stats.get("rx_packets", 0)
                        iface.tx_packets = stats.get("tx_packets", 0)
                        iface.rx_errors = stats.get("rx_errors", 0)
                        iface.tx_errors = stats.get("tx_errors", 0)
                        iface.rx_dropped = stats.get("rx_dropped", 0)
                        iface.tx_dropped = stats.get("tx_dropped", 0)
                        iface.collisions = stats.get("collisions", 0)
                        iface.mac_address = dev_status.get("macaddr", "")
                        iface.speed = (
                            str(iface.link_speed)
                            if iface.link_speed
                            else str(dev_status.get("speed", ""))
                        )

                # 3. Add physical devices that are NOT logical interfaces (e.g. eth1, eth2)
                seen_phys = {i.device for i in interfaces if i.device}
                seen_phys.update({i.name for i in interfaces})

                for dev_name, dev_status in device_stats.items():
                    if dev_name in seen_phys:
                        continue
                    # Skip virtual/internal interfaces to avoid clutter
                    if dev_name.startswith(("lo", "teql", "sit", "gre", "erspan")):
                        continue

                    iface = NetworkInterface(
                        name=dev_name,
                        device=dev_name,
                        up=dev_status.get("up", False),
                        is_link_up=dev_status.get("link", False),
                        link_speed=dev_status.get("speed", 0),
                        link_duplex="full" if dev_status.get("full_duplex") else "half",
                        mac_address=dev_status.get("macaddr", ""),
                        speed=str(dev_status.get("speed", "")),
                    )
                    stats = dev_status.get("statistics", {})
                    iface.rx_bytes = stats.get("rx_bytes", 0)
                    iface.tx_bytes = stats.get("tx_bytes", 0)
                    interfaces.append(iface)

        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Failed to get network interfaces via SSH: %s", err)

        return interfaces

    async def set_wireless_enabled(self, interface: str, enabled: bool) -> bool:
        """Enable/disable a wireless interface."""
        try:
            action = "0" if enabled else "1"
            safe_val = shlex.quote(f"wireless.{interface}.disabled={action}")
            await self._exec(f"uci set {safe_val}")
            await self._exec("uci commit wireless")
            await self._exec("wifi reload")
            self._last_full_poll = 0
            return True
        except Exception as err:
            _LOGGER.exception("Failed to set wireless %s: %s", interface, err)
            return False

    async def manage_interface(self, name: str, action: str) -> bool:
        """Manage a network interface (up/down/reconnect) via SSH."""
        try:
            safe_name = shlex.quote(name)
            if action == "reconnect":
                await self._exec(f"ifdown {safe_name} && ifup {safe_name}")
            elif action == "up":
                await self._exec(f"ifup {safe_name}")
            elif action == "down":
                await self._exec(f"ifdown {safe_name}")
            return True
        except Exception as err:
            _LOGGER.exception("Failed to manage interface %s: %s", name, err)
            return False

    async def get_lldp_neighbors(self) -> list[LldpNeighbor]:
        """Get LLDP neighbor information via SSH."""
        neighbors: list[LldpNeighbor] = []

        try:
            # Method 1: ubus (preferred)
            await self._get_lldp_from_ubus(neighbors)
            if neighbors:
                return neighbors

            # Method 2: lldpcli
            await self._get_lldp_from_lldpcli(neighbors)

        except Exception as err:
            _LOGGER.debug("Failed to get LLDP neighbors via SSH: %s", err)
        return neighbors

    async def _get_lldp_from_ubus(self, neighbors: list[LldpNeighbor]) -> None:
        """Fetch LLDP neighbors from 'lldp show' ubus call via SSH."""
        if self.packages.lldp is False:
            return
        with contextlib.suppress(Exception):
            stdout = await self._exec("ubus call lldp show 2>/dev/null")
            if stdout and stdout.strip().startswith("{"):
                data = json.loads(stdout)
                interfaces = data.get("lldp", {}).get("interface", [])
                if isinstance(interfaces, list):
                    for iface in interfaces:
                        name = iface.get("name")
                        for neigh in iface.get("neighbor", []):
                            neighbors.append(
                                self._parse_ubus_lldp_neigh(name or "", neigh)
                            )

    def _parse_ubus_lldp_neigh(
        self, local_iface: str, neigh: dict[str, Any]
    ) -> LldpNeighbor:
        """Parse a single LLDP neighbor entry from ubus output."""
        from ..base import LldpNeighbor

        return LldpNeighbor(
            local_interface=local_iface,
            neighbor_name=neigh.get("name", ""),
            neighbor_port=(
                neigh.get("port", {}).get("id", "")
                if isinstance(neigh.get("port"), dict)
                else ""
            ),
            neighbor_chassis=(
                neigh.get("chassis", {}).get("id", "")
                if isinstance(neigh.get("chassis"), dict)
                else ""
            ),
            neighbor_description=neigh.get("description", ""),
            neighbor_system_name=neigh.get("sysname", ""),
        )

    async def _get_lldp_from_lldpcli(self, neighbors: list[LldpNeighbor]) -> None:
        """Fetch LLDP neighbors using 'lldpcli show neighbors' via SSH."""
        with contextlib.suppress(Exception):
            stdout = await self._exec("lldpcli show neighbors -f json 2>/dev/null")
            if stdout and stdout.strip().startswith("{"):
                data = json.loads(stdout)
                interfaces = data.get("lldp", {}).get("interface", {})
                if isinstance(interfaces, dict):
                    for iface_name, iface_data in interfaces.items():
                        neighs = iface_data.get("neighbor", [])
                        if isinstance(neighs, dict):
                            neighs = [neighs]
                        for neigh in neighs if isinstance(neighs, list) else []:
                            neighbors.append(
                                self._parse_lldpcli_neigh(iface_name, neigh)
                            )

    def _parse_lldpcli_neigh(
        self, local_iface: str, neigh: dict[str, Any]
    ) -> LldpNeighbor:
        """Parse a single LLDP neighbor entry from lldpcli JSON output."""
        from ..base import LldpNeighbor

        return LldpNeighbor(
            local_interface=local_iface,
            neighbor_name=neigh.get("name", ""),
            neighbor_port=(
                neigh.get("port", {}).get("id", {}).get("value", "")
                if isinstance(neigh.get("port"), dict)
                else ""
            ),
            neighbor_chassis=(
                neigh.get("chassis", {}).get("id", {}).get("value", "")
                if isinstance(neigh.get("chassis"), dict)
                else ""
            ),
            neighbor_description=neigh.get("description", ""),
            neighbor_system_name=neigh.get("sysname", ""),
        )

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

    async def get_mwan_status(self) -> list[MwanStatus]:
        """Get multi-wan status via SSH."""
        try:
            stdout = await self._exec("ubus call mwan3 status 2>/dev/null")
            if not stdout or not stdout.startswith("{"):
                return []

            result = json.loads(stdout)
            status_list = []
            for name, data in result.get("interfaces", {}).items():
                status_list.append(
                    MwanStatus(
                        interface_name=name,
                        status=data.get("status", "unknown"),
                        online_ratio=float(data.get("online_ratio", 0.0)),
                        uptime=int(data.get("uptime", 0)),
                        enabled=bool(data.get("enabled", False)),
                        latency=data.get("latency"),
                        packet_loss=data.get("packet_loss"),
                    )
                )
            return status_list
        except Exception as err:
            _LOGGER.debug("Failed to get mwan3 status via ssh: %s", err)
            return []

    async def trigger_wps_push(self, interface: str) -> bool:
        """Trigger WPS push button via SSH."""
        try:
            # hostapd_cli -i wlan0 wps_push
            safe_iface = shlex.quote(interface)
            await self.execute_command(f"hostapd_cli -i {safe_iface} wps_push")
            return True
        except Exception as err:
            _LOGGER.debug(
                "Failed to trigger WPS push via ssh for %s: %s", interface, err
            )
            return False
