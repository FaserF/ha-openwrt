# mypy: disable-error-code="attr-defined"
from __future__ import annotations

import asyncio
import contextlib
import json
import logging

from ..base import (
    LldpNeighbor,
    MwanStatus,
    NetworkInterface,
    UpnpMapping,
    WireGuardInterface,
    WireGuardPeer,
    WirelessInterface,
)
from .exceptions import *

_LOGGER = logging.getLogger(__name__)


class LuciRpcNetworkMixin:
    """Network methods for LuciRpcClient."""

    async def get_lldp_neighbors(self) -> list[LldpNeighbor]:
        """Get LLDP neighbor information via LuCI RPC."""
        neighbors: list[LldpNeighbor] = []
        try:
            # Try ubus first (same as ubus client)
            out = await self.execute_command("ubus call lldp show 2>/dev/null")
            if out and out.strip().startswith("{"):
                data = json.loads(out)
                for neighbor_data in data.get("lldp", []):
                    for details in neighbor_data.values():
                        if not isinstance(details, dict):
                            continue
                        # details is a list of neighbors for this interface?
                        # Actually 'lldp show' structure varies, but let's try a common one

            # Fallback to lldpcli -f json
            out = await self.execute_command(
                "lldpcli show neighbors -f json 2>/dev/null",
            )
            if out and out.strip().startswith("{"):
                data = json.loads(out)
                # Parse lldpcli json output (complex nested structure)
                # lldp -> neighbor -> [ { interface: { name: "...", neighbor: [...] } } ]
                lldp = data.get("lldp", {})
                for entry in lldp.get("interface", []):
                    local_iface = None
                    for iface_name, iface_data in entry.items():
                        local_iface = iface_name
                        for neighbor in iface_data.get("neighbor", []):
                            n = LldpNeighbor(local_interface=local_iface)
                            n.neighbor_name = neighbor.get("name", "")
                            n.neighbor_description = neighbor.get("descr", "")
                            n.neighbor_system_name = neighbor.get("sysname", "")

                            port = neighbor.get("port", [{}])[0]
                            n.neighbor_port = port.get("id", {}).get("value", "")

                            chassis = neighbor.get("chassis", [{}])[0]
                            n.neighbor_chassis = chassis.get("id", {}).get("value", "")

                            neighbors.append(n)
        except Exception:
            pass
        return neighbors

    async def get_external_ip(self) -> str | None:
        """Get public/external IP address."""
        try:
            status = await self.execute_command(
                "ubus call network.interface dump 2>/dev/null"
            )
            if status:
                data = json.loads(status)
                if data and isinstance(data, dict):
                    for iface_data in data.get("interface", []):
                        iface_name = iface_data.get("interface", "").lower()
                        if iface_name in ["wan", "wan6", "wwan", "modem"]:
                            ipv4_addrs = iface_data.get("ipv4-address", [])
                            if ipv4_addrs:
                                return ipv4_addrs[0].get("address")
        except (
            LuciRpcError,
            json.JSONDecodeError,
        ):
            pass
        return None

    async def get_wireless_interfaces(self) -> list[WirelessInterface]:
        """Get wireless interfaces via ubus iwinfo and UCI."""
        interfaces: list[WirelessInterface] = []
        iface_names: set[str] = set()

        # 1. Primary source: network.wireless status (UCI state)
        if self.packages.wireless is not False:
            try:
                wireless_data = await self.execute_command(
                    "ubus call network.wireless status 2>/dev/null"
                )
                if wireless_data and wireless_data.strip().startswith("{"):
                    data = json.loads(wireless_data)
                    for radio_name, radio_data in data.items():
                        if not isinstance(radio_data, dict):
                            continue
                        for iface in radio_data.get("interfaces", []):
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
                            wifi = WirelessInterface(
                                name=iface_name,
                                ssid=iface_config.get("ssid", ""),
                                mode=iface_config.get("mode", ""),
                                encryption=iface_config.get("encryption", ""),
                                enabled=not radio_data.get("disabled", False),
                                up=radio_data.get("up", False),
                                radio=radio_name,
                                hwmode=radio_data.get("config", {}).get("hwmode", ""),
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
            except Exception as err:
                _LOGGER.debug(
                    "network.wireless status failed via LuCI, trying UCI: %s", err
                )
                try:
                    uci_wireless_str = await self.execute_command("uci export wireless")
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
                                    sections[current_section][parts[1]] = parts[
                                        2
                                    ].strip("'\"")

                        for sect_name, sect_data in sections.items():
                            if sect_data.get(".type") != "wifi-iface":
                                continue

                            iface_name = sect_data.get("ifname") or sect_name
                            radio_name = sect_data.get("device", "")
                            radio_disabled = (
                                sections.get(radio_name, {}).get("disabled", "0") == "1"
                            )
                            iface_disabled = sect_data.get("disabled", "0") == "1"

                            wifi = WirelessInterface(
                                name=iface_name,
                                ssid=sect_data.get("ssid", ""),
                                mode=sect_data.get("mode", ""),
                                encryption=sect_data.get("encryption", ""),
                                enabled=not (radio_disabled or iface_disabled),
                                up=not (radio_disabled or iface_disabled),
                                radio=radio_name,
                                hwmode=sections.get(radio_name, {}).get("hwmode", ""),
                                section=sect_name,
                            )
                            interfaces.append(wifi)
                            iface_names.add(iface_name)
                except Exception as e:
                    _LOGGER.debug("UCI wireless fallback failed via LuCI: %s", e)

        # 2. Supplement/Fallback: iwinfo devices
        iw_devs = set()
        if self.packages.wireless is not False:
            try:
                iw_devs_str = await self.execute_command(
                    "ubus call iwinfo devices 2>/dev/null"
                )
                if iw_devs_str and iw_devs_str.strip().startswith("{"):
                    iw_devs = set(json.loads(iw_devs_str).get("devices", []))
                for name in iw_devs:
                    if name not in iface_names:
                        wifi = WirelessInterface(name=name, enabled=True, up=True)
                        interfaces.append(wifi)
                        iface_names.add(name)
            except Exception:
                pass

        # 3. Populate metrics via ubus iwinfo info in parallel
        async def _fetch_metrics(wifi: WirelessInterface) -> None:
            iface_name = wifi.name
            # Only call iwinfo if the device is known to iwinfo or looks like a wireless device
            if iface_name not in iw_devs and not iface_name.startswith(
                ("wlan", "ath", "ra", "wl", "phy", "ap", "radio")
            ):
                return

            try:
                # Get basic info
                iwinfo_str = await self.execute_command(
                    f'ubus call iwinfo info \'{{"device":"{iface_name}"}}\' 2>/dev/null'
                )
                if iwinfo_str and iwinfo_str.strip().startswith("{"):
                    info = json.loads(iwinfo_str)
                    if not wifi.ssid:
                        wifi.ssid = info.get("ssid", "")
                    wifi.mac_address = info.get("bssid", "").upper()
                    wifi.channel = info.get("channel", 0)
                    wifi.frequency = str(info.get("frequency", ""))
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
                    assoc_str = await self.execute_command(
                        f'ubus call iwinfo assoclist \'{{"device":"{iface_name}"}}\' 2>/dev/null'
                    )
                    if assoc_str and assoc_str.strip().startswith("{"):
                        assoc = json.loads(assoc_str).get("results", [])
                        wifi.clients_count = len(assoc)

                    if not wifi.clients_count:
                        with contextlib.suppress(Exception):
                            clients_str = await self.execute_command(
                                f"ubus call hostapd.{iface_name} get_clients 2>/dev/null"
                            )
                            if clients_str and clients_str.strip().startswith("{"):
                                hc = json.loads(clients_str).get("clients", {})
                                wifi.clients_count = len(hc)
            except Exception as err:
                _LOGGER.debug("Failed to get iwinfo for %s: %s", iface_name, err)
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
                            wifi.radio = wifi.radio or prev_wifi.radio
                            wifi.htmode = wifi.htmode or prev_wifi.htmode
                            wifi.txpower = wifi.txpower or prev_wifi.txpower
                            wifi.mesh_id = wifi.mesh_id or prev_wifi.mesh_id
                            wifi.mesh_fwding = wifi.mesh_fwding or prev_wifi.mesh_fwding
                            wifi.ifname = wifi.ifname or prev_wifi.ifname
                            wifi.section = wifi.section or prev_wifi.section
                            wifi.band = wifi.band or prev_wifi.band
                            wifi.width = wifi.width or prev_wifi.width
                            wifi.standard = wifi.standard or prev_wifi.standard
                            break

        if interfaces:
            await asyncio.gather(*[_fetch_metrics(w) for w in interfaces])

        # 4. Deduplicate and clean up
        # We group by Section ID (UCI), MAC address, or SSID+Frequency
        unique_ifaces: list[WirelessInterface] = []
        seen_keys: set[str] = set()

        for wifi in interfaces:
            # Skip interfaces that are clearly not operational or redundant placeholders
            # (No MAC and no SSID usually means a disabled/misconfigured UCI section)
            if (
                not wifi.mac_address
                and not wifi.ssid
                and wifi.mode.lower() in ("", "ap", "master")
            ):
                _LOGGER.debug(
                    "Skipping non-operational wireless interface: %s", wifi.name
                )
                continue

            # Skip unconfigured generic placeholders (ghosts)
            is_ghost_name = any(
                (wifi.name or "").startswith(p) or (wifi.section or "").startswith(p)
                for p in ["default_radio", "wifinet", "radio"]
            )
            if is_ghost_name and (
                not wifi.ssid
                or wifi.ssid == "OpenWrt"
                or not wifi.mac_address
                or wifi.mac_address == "00:00:00:00:00:00"
            ):
                _LOGGER.debug(
                    "Skipping ghost wireless interface: %s (SSID: %s)",
                    wifi.name,
                    wifi.ssid,
                )
                continue

            # Create a key for deduplication
            # Priority 1: MAC address (BSSID)
            # Priority 2: SSID + Radio (for merging UCI sections with physical interfaces)
            # Priority 3: Section ID (UCI)
            # Priority 4: SSID + Frequency
            if wifi.mac_address:
                key = f"mac_{wifi.mac_address.lower()}"
            elif wifi.ssid and wifi.radio:
                key = f"ssid_radio_{wifi.ssid}_{wifi.radio}"
            elif wifi.section:
                key = f"section_{wifi.section}"
            elif wifi.ssid and wifi.frequency:
                key = f"ssid_freq_{wifi.ssid}_{wifi.frequency}"
            else:
                key = f"name_{wifi.name}"

            if key not in seen_keys:
                unique_ifaces.append(wifi)
                seen_keys.add(key)
            else:
                # Merge data if this one has more info
                existing = next(
                    i
                    for i in unique_ifaces
                    if (
                        (wifi.mac_address and i.mac_address == wifi.mac_address)
                        or (wifi.section and i.section == wifi.section)
                        or (
                            wifi.ssid
                            and wifi.frequency
                            and i.ssid == wifi.ssid
                            and i.frequency == wifi.frequency
                        )
                    )
                )
                # Prefer system name over UCI section name for existing.name
                if len(wifi.name) > len(existing.name) and not existing.mac_address:
                    existing.name = wifi.name

                if not existing.frequency and wifi.frequency:
                    existing.frequency = wifi.frequency
                if not existing.mac_address and wifi.mac_address:
                    existing.mac_address = wifi.mac_address
                if not existing.ssid and wifi.ssid:
                    existing.ssid = wifi.ssid
                if wifi.clients_count > existing.clients_count:
                    existing.clients_count = wifi.clients_count
                if wifi.ifname and not existing.ifname:
                    existing.ifname = wifi.ifname

        return unique_ifaces

    async def get_upnp_mappings(self) -> list[UpnpMapping]:
        """Get active UPnP/NAT-PMP port mappings via LuCI RPC."""
        mappings: list[UpnpMapping] = []
        if self.packages.miniupnpd is False:
            return mappings

        try:
            stdout = await self.execute_command(
                "ubus call upnp get_mappings 2>/dev/null"
            )
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
            _LOGGER.debug("Failed to fetch UPnP mappings via LuCI RPC: %s", err)

        return mappings

    async def get_wireguard_interfaces(self) -> list[WireGuardInterface]:
        """Get WireGuard VPN interface and peer information via LuCI RPC."""
        interfaces: list[WireGuardInterface] = []
        # 1. Discover WG interfaces via ubus call
        status_str = await self.execute_command(
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
        stdout = await self.execute_command("wg show all dump 2>/dev/null")
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
                        allowed_ips=parts[4].split(",") if parts[4] != "(none)" else [],
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
        return interfaces

    async def get_network_interfaces(self) -> list[NetworkInterface]:
        """Get network interfaces."""
        interfaces: list[NetworkInterface] = []

        try:
            dump = await self.execute_command(
                "ubus call network.interface dump 2>/dev/null"
            )
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

            if interfaces:
                return interfaces
        except Exception:  # noqa: BLE001
            pass

        # Fallback to UCI config if ubus dump fails
        net_config = await self._rpc_call("uci", "get_all", ["network"])
        if isinstance(net_config, dict):
            for section, values in net_config.items():
                if isinstance(values, dict) and values.get(".type") == "interface":
                    iface = NetworkInterface(
                        name=section,
                        protocol=values.get("proto", ""),
                        device=str(values.get("device", values.get("ifname", ""))),
                    )
                    # Try to get MAC if possible
                    if iface.device:
                        try:
                            mac = await self.execute_command(
                                f"cat /sys/class/net/{iface.device}/address 2>/dev/null",
                            )
                            if mac and ":" in mac:
                                iface.mac_address = mac.strip().lower()
                        except Exception:
                            pass
                    interfaces.append(iface)

        # 3. Add physical devices that are NOT logical interfaces (e.g. eth1, eth2)
        try:
            dev_status_str = await self.execute_command(
                "ubus call network.device status 2>/dev/null"
            )
            if dev_status_str and dev_status_str.strip().startswith("{"):
                device_stats = json.loads(dev_status_str)
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
                        mac_address=dev_status.get("macaddr", ""),
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

                    interfaces.append(iface)
        except Exception:  # noqa: BLE001
            pass

        return interfaces

    async def _get_wireless_mapping(self) -> tuple[dict[str, str], dict[str, str]]:
        """Get mapping of UCI sections to system names and vice-versa."""
        uci_to_sys: dict[str, str] = {}
        try:
            # Discovery of wireless interfaces via ubus
            wireless_status = await self._rpc_call(
                "sys",
                "exec",
                ["ubus call network.wireless status 2>/dev/null"],
            )
            if wireless_status:
                try:
                    ws_data = json.loads(wireless_status)
                    for radio_data in ws_data.values():
                        if not isinstance(radio_data, dict):
                            continue
                        for iface in radio_data.get("interfaces", []):
                            if "section" in iface and "ifname" in iface:
                                uci_to_sys[iface["section"]] = iface["ifname"]
                except Exception:
                    pass

            # Fallback: Discovery of all hostapd objects via ubus
            if not uci_to_sys:
                try:
                    hostapd_list = await self._rpc_call(
                        "sys",
                        "exec",
                        ["ubus list 'hostapd.*' 2>/dev/null"],
                    )
                    if hostapd_list:
                        for obj in hostapd_list.splitlines():
                            if "." in obj:
                                iface = obj.split(".", 1)[1]
                                # Check if we can find this iface in wireless config via SSID
                                # We'll do this mapping in get_wireless_interfaces
                except Exception:
                    pass
        except LuciRpcError:
            pass

        sys_to_uci = {v: k for k, v in uci_to_sys.items()}
        self._uci_to_sys = uci_to_sys
        self._sys_to_uci = sys_to_uci
        return uci_to_sys, sys_to_uci

    async def set_wireless_enabled(self, interface: str, enabled: bool) -> bool:
        """Enable or disable a wireless radio via UCI."""
        try:
            action = "0" if enabled else "1"
            cmd = (
                f"uci set wireless.{interface}.disabled={action} && "
                "uci commit wireless && "
                "wifi reload"
            )
            await self.execute_command(cmd)
            self._last_full_poll = 0
            return True
        except Exception:
            return False

    async def manage_interface(self, name: str, action: str) -> bool:
        """Manage a network interface via LuCI RPC."""
        try:
            if action == "reconnect":
                await self.execute_command(f"ifdown {name} && ifup {name}")
            elif action == "up":
                await self.execute_command(f"ifup {name}")
            elif action == "down":
                await self.execute_command(f"ifdown {name}")
            return True
        except Exception:
            return False

    async def get_mwan_status(self) -> list[MwanStatus]:
        """Get multi-wan status via LuCI RPC."""
        try:
            # Try ubus first
            stdout = await self.execute_command("ubus call mwan3 status 2>/dev/null")
            if stdout and stdout.startswith("{"):
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
            return []
        except Exception as err:
            _LOGGER.debug("Failed to get mwan3 status via luci_rpc: %s", err)
            return []

    async def trigger_wps_push(self, interface: str) -> bool:
        """Trigger WPS push button via LuCI RPC."""
        try:
            # We use execute_command abstraction to call ubus
            await self.execute_command(f"ubus call hostapd.{interface} wps_push")
            return True
        except Exception as err:
            _LOGGER.debug(
                "Failed to trigger WPS push via luci_rpc for %s: %s", interface, err
            )
            return False
