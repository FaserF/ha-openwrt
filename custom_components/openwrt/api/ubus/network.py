# mypy: disable-error-code="attr-defined"
from __future__ import annotations

import logging
from typing import Any

from ..base import (
    MwanPolicy,
    MwanPolicyInterface,
    MwanStatus,
    NetworkInterface,
    UpnpMapping,
    WireGuardInterface,
    WireGuardPeer,
    parse_mwan_track_metrics,
)
from .exceptions import UbusError

_LOGGER = logging.getLogger(__name__)
UBUS_JSONRPC_VERSION = "2.0"
UBUS_ID_AUTH = 1
UBUS_ID_CALL = 2


def _as_seconds(value: Any) -> float:
    """Coerce a ubus duration field to float seconds.

    Routers occasionally omit these fields or report them as null, in which
    case float() would raise and abort the whole status response.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


from .network_wireless import UbusNetworkWirelessMixin


class UbusNetworkMixin(UbusNetworkWirelessMixin):
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
                if len(parts) in (4, 5):
                    # Interface line
                    ifname = parts[0]
                    if ifname not in wg_ifaces:
                        continue
                    if len(parts) == 4:
                        public_key = parts[1]
                        listen_port = int(parts[2]) if parts[2].isdigit() else 0
                        fwmark = int(parts[3]) if parts[3].isdigit() else 0
                    else:
                        public_key = parts[2]
                        listen_port = int(parts[3]) if parts[3].isdigit() else 0
                        fwmark = int(parts[4]) if parts[4].isdigit() else 0
                    iface = WireGuardInterface(
                        name=ifname,
                        enabled=wg_ifaces[ifname],
                        public_key=public_key,
                        listen_port=listen_port,
                        fwmark=fwmark,
                    )
                    iface_map[ifname] = iface
                    interfaces.append(iface)
                elif len(parts) >= 9:
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

    async def get_mwan_status(self) -> tuple[list[MwanStatus], list[MwanPolicy]]:
        """Get MWAN3 multi-wan status and policies."""
        statuses: list[MwanStatus] = []
        policies: list[MwanPolicy] = []

        try:
            data = await self._call("mwan3", "status")
            interfaces = data.get("interfaces", {})
            for iface_name, iface_data in interfaces.items():
                # mwan3 reports "online" and "uptime" as durations in
                # seconds. online_ratio is expected to be the fraction of
                # uptime spent online; passing the raw seconds through made
                # the sensor display values in the millions of percent.
                online_secs = _as_seconds(iface_data.get("online"))
                uptime_secs = _as_seconds(iface_data.get("uptime"))
                latency, packet_loss = parse_mwan_track_metrics(iface_data)
                statuses.append(
                    MwanStatus(
                        interface_name=iface_name,
                        status=iface_data.get("status", "unknown"),
                        online=online_secs,
                        offline=_as_seconds(iface_data.get("offline")),
                        online_ratio=(
                            min(online_secs / uptime_secs, 1.0)
                            if uptime_secs > 0
                            else 0.0
                        ),
                        uptime=iface_data.get("uptime", 0),
                        enabled=iface_data.get("enabled", False),
                        latency=latency,
                        packet_loss=packet_loss,
                    ),
                )

            policies_data = data.get("policies", {})
            for family in ["ipv4", "ipv6"]:
                family_policies = policies_data.get(family, {})
                for policy_name, policy_interfaces in family_policies.items():
                    active_interfaces = []
                    if isinstance(policy_interfaces, list):
                        for iface in policy_interfaces:
                            active_interfaces.append(
                                MwanPolicyInterface(
                                    interface=iface.get("interface", ""),
                                    percent=iface.get("percent", 0),
                                )
                            )
                    policies.append(
                        MwanPolicy(
                            policy_name=policy_name,
                            family=family,
                            active_interfaces=active_interfaces,
                        )
                    )

        except UbusError:
            _LOGGER.debug("MWAN3 not available (not installed or no permissions)")
            # Let the caller keep the previous status: an empty list would
            # read as "no MWAN interfaces" and turn the sensors off.
            raise

        return statuses, policies

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
            except (ValueError, IndexError):
                pass
        return None
