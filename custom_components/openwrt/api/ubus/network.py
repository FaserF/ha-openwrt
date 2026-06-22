# mypy: disable-error-code="attr-defined"
from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from ..base import (
    MwanStatus,
    NetworkInterface,
    UpnpMapping,
    WireGuardInterface,
    WireGuardPeer,
    WirelessInterface,
)
from .exceptions import *

_LOGGER = logging.getLogger(__name__)
UBUS_JSONRPC_VERSION = "2.0"
UBUS_ID_AUTH = 1
UBUS_ID_CALL = 2


class UbusNetworkMixin:
    """Network methods for UbusClient."""

    async def get_external_ip(self) -> str | None:
        """Get the external IP address from the WAN interface."""
        status = await self._call("network.interface", "dump")
        for iface_data in status.get("interface", []):
            iface_name = iface_data.get("interface", "").lower()
            if iface_name in ["wan", "wan6", "wwan", "modem"]:
                ipv4_addrs = iface_data.get("ipv4-address", [])
                if ipv4_addrs:
                    return ipv4_addrs[0].get("address")
        return None

    async def get_wireless_interfaces(self) -> list[WirelessInterface]:
        """Get wireless interface information."""
        interfaces: list[WirelessInterface] = []
        iface_names: set[str] = set()

        # 1. Primary source: network.wireless status
        if self.packages.wireless is not False:
            try:
                wireless_data = await self._call("network.wireless", "status")
                if wireless_data and isinstance(wireless_data, dict):
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
                            wifi = WirelessInterface(
                                name=iface_name,
                                ssid=iface_config.get("ssid", ""),
                                mode=iface_config.get("mode", ""),
                                encryption=iface_config.get("encryption", ""),
                                enabled=not radio_data.get("disabled", False),
                                up=not radio_data.get("disabled", False),
                                radio=radio_name,
                                band=WirelessInterface._band_from_raw(
                                    radio_data.get("config", {}).get("band", "")
                                    or radio_data.get("config", {}).get("hwmode", "")
                                ),
                                htmode=radio_data.get("config", {}).get("htmode", ""),
                                hwmode=radio_data.get("config", {}).get("hwmode", ""),
                                txpower=radio_data.get("config", {}).get("txpower", 0),
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
                    for sect_name, sect_data in vals.items():
                        if sect_data.get(".type") != "wifi-iface":
                            continue

                        # In some firmwares (like Xiaomi), ifname is not in UCI
                        # But iwinfo might know the interface.
                        iface_name = sect_data.get("ifname") or sect_name
                        radio_name = sect_data.get("device", "")

                        # Get radio status to determine if enabled
                        radio_disabled = (
                            vals.get(radio_name, {}).get("disabled", "0") == "1"
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
                            band=WirelessInterface._band_from_raw(
                                vals.get(radio_name, {}).get("band", "")
                                or vals.get(radio_name, {}).get("hwmode", "")
                            ),
                            hwmode=vals.get(radio_name, {}).get("hwmode", ""),
                            section=sect_name,
                            ifname=sect_data.get("ifname"),
                        )
                        interfaces.append(wifi)
                        iface_names.add(iface_name)
                        if wifi.section and wifi.section != iface_name:
                            iface_names.add(wifi.section)
                        if wifi.ifname and wifi.ifname != iface_name:
                            iface_names.add(wifi.ifname)
            except Exception as e:
                _LOGGER.debug("UCI wireless fallback failed: %s", e)

        # 2. Supplement/Fallback: iwinfo devices
        # This is critical for devices where interfaces aren't in network.wireless or UCI names differ
        try:
            iw_devs = await self._call("iwinfo", "devices")
            candidates = []
            if isinstance(iw_devs, list):
                candidates = iw_devs
            elif isinstance(iw_devs, dict) and "devices" in iw_devs:
                candidates = iw_devs["devices"]

            for name in candidates:
                if name in iface_names:
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
                    # Found a new interface not in UCI status
                    wifi = WirelessInterface(name=name, enabled=True, up=True)
                    interfaces.append(wifi)
                    iface_names.add(name)
        except UbusError:
            _LOGGER.debug("iwinfo devices call failed")

        # 3. Populate metrics for all discovered interfaces in parallel
        async def _fetch_metrics(wifi: WirelessInterface) -> None:
            try:
                iwinfo = await self._call("iwinfo", "info", {"device": wifi.name})
                if iwinfo:
                    if not wifi.ssid:
                        wifi.ssid = iwinfo.get("ssid", "")
                    wifi.mac_address = iwinfo.get("bssid", "").upper()
                    wifi.channel = iwinfo.get("channel", 0)
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

                    if "hwmode" in iwinfo and not wifi.hwmode:
                        if isinstance(iwinfo["hwmode"], list):
                            wifi.hwmode = "/".join(iwinfo["hwmode"])
                        else:
                            wifi.hwmode = str(iwinfo["hwmode"])
                    if "htmode" in iwinfo and not wifi.htmode:
                        wifi.htmode = str(iwinfo["htmode"])

                # Association list
                assoc = await self._call("iwinfo", "assoclist", {"device": wifi.name})
                if assoc:
                    wifi.clients_count = len(assoc.get("results", []))

                if not wifi.clients_count:
                    with contextlib.suppress(Exception):
                        hostapd_clients = await self._call(
                            f"hostapd.{wifi.name}", "get_clients"
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

            except UbusError:
                _LOGGER.debug(
                    "Failed to fetch detailed info for wifi interface %s", wifi.name
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
                        if wifi.clients_count > 0:
                            existing.clients_count = wifi.clients_count
                        break

        return unique_ifaces

    async def get_upnp_mappings(self) -> list[UpnpMapping]:
        """Get active UPnP/NAT-PMP port mappings via ubus."""
        mappings: list[UpnpMapping] = []
        try:
            res = await self._call("upnp", "get_mappings")
            if not isinstance(res, dict) or "mappings" not in res:
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
        except UbusError:
            pass  # upnp object might not exist
        except Exception as err:
            _LOGGER.debug("Failed to fetch UPnP mappings: %s", err)

        return mappings

    async def get_wireguard_interfaces(self) -> list[WireGuardInterface]:
        """Get WireGuard VPN interface and peer information via ubus/CLI."""
        interfaces: list[WireGuardInterface] = []
        try:
            # 1. Discover WG interfaces via network.interface dump
            status = await self._call("network.interface", "dump")
            if not isinstance(status, dict):
                return interfaces

            wg_ifaces: dict[str, bool] = {}
            for iface_data in status.get("interface", []):
                if iface_data.get("proto") == "wireguard":
                    wg_ifaces[iface_data.get("interface")] = bool(iface_data.get("up"))

            if not wg_ifaces:
                return interfaces

            # 2. Fetch peer info via wg show dump
            # wg show all dump format:
            # interface public_key listen_port fwmark
            # peer_public_key preshared_key endpoint allowed_ips latest_handshake transfer_rx transfer_tx persistent_keepalive

            stdout = await self.execute_command("wg show all dump 2>/dev/null")
            if not stdout:
                return interfaces

            iface_map: dict[str, WireGuardInterface] = {}
            for line in stdout.splitlines():
                parts = line.split("\t")
                if len(parts) == 4:
                    # Interface line
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
                    # Peer line
                    # Format: interface peer_public_key preshared_key endpoint allowed_ips latest_handshake transfer_rx transfer_tx persistent_keepalive
                    # Wait, 'wg show all dump' includes the interface name as the first part for peers too?
                    # Let's check 'wg show dump' output:
                    # Interface: interface public_key listen_port fwmark
                    # Peer: peer_public_key preshared_key endpoint allowed_ips latest_handshake transfer_rx transfer_tx persistent_keepalive
                    # If using 'all', it's:
                    # interface peer_public_key preshared_key endpoint allowed_ips latest_handshake transfer_rx transfer_tx persistent_keepalive

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
            _LOGGER.debug("Failed to fetch WireGuard interfaces: %s", err)

        return interfaces

    async def get_network_interfaces(self) -> list[NetworkInterface]:
        """Get network interface information."""
        interfaces: list[NetworkInterface] = []

        try:
            status = await self._call("network.interface", "dump")
        except UbusError:
            return interfaces

        # 2. Fetch all device statistics and link status in one call (efficient)
        device_stats = {}
        try:
            device_stats = await self._call("network.device", "status")
        except UbusError:
            _LOGGER.debug("Failed to fetch all network device stats")

        for iface_data in status.get("interface", []):
            iface = NetworkInterface(
                name=iface_data.get("interface", ""),
                up=iface_data.get("up", False),
                protocol=iface_data.get("proto", ""),
                device=iface_data.get("l3_device", iface_data.get("device", "")),
                uptime=iface_data.get("uptime", 0),
            )

            ipv4_addrs = iface_data.get("ipv4-address", [])
            if ipv4_addrs:
                iface.ipv4_address = ipv4_addrs[0].get("address", "")

            ipv6_addrs = iface_data.get("ipv6-address", [])
            if ipv6_addrs:
                iface.ipv6_address = ipv6_addrs[0].get("address", "")

            iface.dns_servers = iface_data.get("dns-server", [])
            iface.ipv6_prefix = [
                p.get("address", "") for p in iface_data.get("ipv6-prefix", [])
            ]
            iface.ipv6_prefix_assignment = iface_data.get("ipv6-prefix-assignment", [])

            # Apply statistics and link status
            dev_name = iface.device
            if dev_name and dev_name in device_stats:
                dev_status = device_stats[dev_name]
                iface.is_link_up = dev_status.get("link", False)
                iface.link_speed = dev_status.get("speed", 0)
                iface.link_duplex = "full" if dev_status.get("full_duplex") else "half"

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
                iface.multicast = stats.get("multicast", 0)
                iface.mac_address = dev_status.get("macaddr", "")
                iface.speed = (
                    str(iface.link_speed)
                    if iface.link_speed
                    else dev_status.get("speed", "")
                )

            interfaces.append(iface)

        # 3. Add physical devices that are NOT logical interfaces (e.g. eth1, eth2)
        # to ensure they are visible as sensors even if they don't have a protocol/IP.
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
            iface.multicast = stats.get("multicast", 0)
            iface.speed = str(iface.link_speed) if iface.link_speed else ""

            interfaces.append(iface)

        return interfaces

    async def get_mwan_status(self) -> list[MwanStatus]:
        """Get MWAN3 multi-wan status."""
        statuses: list[MwanStatus] = []

        try:
            data = await self._call("mwan3", "status")
            interfaces = data.get("interfaces", {})
            for iface_name, iface_data in interfaces.items():
                statuses.append(
                    MwanStatus(
                        interface_name=iface_name,
                        status=iface_data.get("status", "unknown"),
                        online_ratio=float(iface_data.get("online", 0)),
                        uptime=iface_data.get("uptime", 0),
                        enabled=iface_data.get("enabled", False),
                    ),
                )
        except UbusError:
            _LOGGER.debug("MWAN3 not available (not installed or no permissions)")

        return statuses

    async def manage_interface(self, name: str, action: str) -> bool:
        """Manage a network interface (up/down/reconnect) via ubus."""
        try:
            if action in {"reconnect", "up"}:
                await self._call("network.interface", "up", {"interface": name})
            elif action == "down":
                await self._call("network.interface", "down", {"interface": name})
            return True
        except UbusError:
            return False

    async def get_gateway_mac(self) -> str | None:
        """Get the default gateway MAC address via ubus and triangulation."""
        try:
            # 1. Get default gateway IP from network.interface dump
            gw_ip = await self._get_gateway_ip_from_ubus()
            if not gw_ip:
                return None

            # 2. Get MAC for that IP via ip neighbor
            return await self._get_mac_from_ip(gw_ip)
        except Exception as err:
            _LOGGER.debug("Failed to get gateway MAC via ubus: %s", err)
        return None

    async def _get_gateway_ip_from_ubus(self) -> str | None:
        """Find the default gateway IP from 'network.interface dump'."""
        status = await self._call("network.interface", "dump")
        interfaces = status.get("interface", [])

        # Priority 1: Common WAN interfaces
        wan_names = ("wan", "wan6", "wwan", "modem")
        for iface in interfaces:
            if iface.get("interface", "").lower() in wan_names:
                gw = self._extract_gateway_from_iface(iface)
                if gw:
                    return gw

        # Priority 2: Any interface with a gateway
        for iface in interfaces:
            gw = self._extract_gateway_from_iface(iface)
            if gw:
                return gw

        return None

    def _extract_gateway_from_iface(self, iface_data: dict[str, Any]) -> str | None:
        """Extract gateway IP from a single interface entry."""
        for addr in iface_data.get("ipv4-address", []):
            if addr.get("gateway"):
                return addr.get("gateway")
        return None

    async def _get_mac_from_ip(self, ip: str) -> str | None:
        """Get the MAC address for a specific IP from the ARP/neighbor table."""
        neigh_out = await self.execute_command(f"ip neigh show {ip} 2>/dev/null")
        if "lladdr" in neigh_out:
            parts = neigh_out.split()
            try:
                idx = parts.index("lladdr")
                if len(parts) > idx + 1:
                    return parts[idx + 1].upper()
            except ValueError, IndexError:
                pass
        return None
