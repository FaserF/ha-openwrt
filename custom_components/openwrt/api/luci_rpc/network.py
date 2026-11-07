# mypy: disable-error-code="attr-defined"
from __future__ import annotations

import json
import logging

from ..base import (
    LldpNeighbor,
    MwanPolicy,
    MwanPolicyInterface,
    MwanStatus,
    NetworkInterface,
    UpnpMapping,
    WireGuardInterface,
    WireGuardPeer,
    parse_mwan_track_metrics,
)
from .exceptions import LuciRpcError

_LOGGER = logging.getLogger(__name__)


from .network_wireless import LuciRpcNetworkWirelessMixin


class LuciRpcNetworkMixin(LuciRpcNetworkWirelessMixin):
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
            if len(parts) in (4, 5):
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

        except Exception:  # noqa: BLE001
            pass

        # Fallback to UCI config if ubus dump produces no interfaces
        if not interfaces:
            try:
                net_config = await self._rpc_call("uci", "get_all", ["network"])
                if isinstance(net_config, dict):
                    for section, values in net_config.items():
                        if (
                            isinstance(values, dict)
                            and values.get(".type") == "interface"
                        ):
                            iface = NetworkInterface(
                                name=section,
                                protocol=values.get("proto", ""),
                                device=str(
                                    values.get("device", values.get("ifname", ""))
                                ),
                            )
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
            except Exception:  # noqa: BLE001
                pass

        # 3. Fetch device statistics and enrich interfaces (logical & physical)
        try:
            dev_status_str = await self.execute_command(
                "ubus call network.device status 2>/dev/null"
            )
            if dev_status_str and dev_status_str.strip().startswith("{"):
                device_stats = json.loads(dev_status_str)

                # Enrich existing logical interfaces with statistics
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

                # Add physical devices that are NOT logical interfaces (e.g. eth1, eth2)
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

    async def get_mwan_status(self) -> tuple[list[MwanStatus], list[MwanPolicy]]:
        """Get multi-wan status and policies via LuCI RPC."""
        try:
            # Try ubus first
            stdout = await self.execute_command("ubus call mwan3 status 2>/dev/null")
            if stdout and stdout.startswith("{"):
                result = json.loads(stdout)
                status_list = []
                for name, data in result.get("interfaces", {}).items():
                    latency, packet_loss = parse_mwan_track_metrics(data)
                    status_list.append(
                        MwanStatus(
                            interface_name=name,
                            status=data.get("status", "unknown"),
                            online=float(data.get("online") or 0),
                            offline=float(data.get("offline") or 0),
                            online_ratio=float(data.get("online_ratio", 0.0)),
                            uptime=int(data.get("uptime", 0)),
                            enabled=bool(data.get("enabled", False)),
                            latency=latency,
                            packet_loss=packet_loss,
                        )
                    )

                policies_list = []
                policies_data = result.get("policies", {})
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
                        policies_list.append(
                            MwanPolicy(
                                policy_name=policy_name,
                                family=family,
                                active_interfaces=active_interfaces,
                            )
                        )
                return status_list, policies_list
            return [], []
        except Exception as err:
            _LOGGER.debug("Failed to get mwan3 status via luci_rpc: %s", err)
            return [], []
