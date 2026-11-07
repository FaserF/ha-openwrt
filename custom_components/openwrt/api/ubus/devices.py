# mypy: disable-error-code="attr-defined"
from __future__ import annotations

import contextlib
import logging
from typing import Any

from ..base import (
    ConnectedDevice,
    DhcpLease,
    IpNeighbor,
    LldpNeighbor,
)
from .exceptions import (
    UbusAuthError,
    UbusConnectionError,
    UbusError,
    UbusPermissionError,
    UbusSslError,
    UbusTimeoutError,
)

_LOGGER = logging.getLogger(__name__)
UBUS_JSONRPC_VERSION = "2.0"
UBUS_ID_AUTH = 1
UBUS_ID_CALL = 2


from .devices_discovery import UbusDevicesDiscoveryMixin


class UbusDevicesMixin(UbusDevicesDiscoveryMixin):
    """Devices methods for UbusClient."""

    async def get_connected_devices(self) -> list[ConnectedDevice]:
        """Get connected devices by combining DHCP leases, ARP, and wireless clients."""
        devices: dict[str, ConnectedDevice] = {}

        # 1. Start with initial device list from DHCP leases (active and static)
        await self._get_devices_from_dhcp(devices)
        await self._get_devices_from_static_leases(devices)

        # 2. Get active wireless interfaces (preferred source for client scanning)
        wireless_ifaces = []
        if self.packages.wireless is not False:
            try:
                wireless_ifaces = await self.get_wireless_interfaces()
            except Exception:
                pass

        # Also call network.wireless status as fallback/legacy
        wireless_data: dict[str, Any] = {}
        if self.packages.wireless is not False and not wireless_ifaces:
            try:
                wireless_data = await self._call("network.wireless", "status")
            except (
                UbusTimeoutError,
                UbusConnectionError,
                UbusSslError,
                UbusPermissionError,
                UbusAuthError,
            ):
                raise
            except UbusError:
                pass

        # 3. Process wireless associations (iwinfo)
        scanned_wireless_candidates: set[str] = set()
        if wireless_ifaces:
            for wifi_iface in wireless_ifaces:
                candidate_names = [wifi_iface.name]
                if wifi_iface.ifname and wifi_iface.ifname not in candidate_names:
                    candidate_names.append(wifi_iface.ifname)
                if wifi_iface.section and wifi_iface.section not in candidate_names:
                    candidate_names.append(wifi_iface.section)

                found_assoc = False
                for ifname in candidate_names:
                    if not ifname:
                        continue
                    try:
                        assoc = await self._call(
                            "iwinfo", "assoclist", {"device": ifname}
                        )
                        if assoc and isinstance(assoc, dict):
                            results = assoc.get("results", [])
                            for client in results:
                                mac = client.get("mac", "").lower()
                                dev = devices.setdefault(
                                    mac, ConnectedDevice(mac=mac, connected=True)
                                )
                                dev.connected = True
                                dev.is_wireless = True
                                dev.interface = wifi_iface.name or ifname
                                self._set_wireless_connection_type(dev, ifname)
                                dev.signal = client.get("signal", 0)
                                dev.noise = client.get("noise", 0)
                                dev.rx_rate = self._get_assoc_rate(client, "rx")
                                dev.tx_rate = self._get_assoc_rate(client, "tx")
                            found_assoc = True
                            if results:
                                break
                    except (
                        UbusTimeoutError,
                        UbusConnectionError,
                        UbusSslError,
                        UbusPermissionError,
                        UbusAuthError,
                    ):
                        raise
                    except UbusError:
                        pass

                if found_assoc:
                    scanned_wireless_candidates.update(c for c in candidate_names if c)
        elif wireless_data:
            await self._process_iwinfo_assoc(devices, wireless_data)
        else:
            # Fallback: scan all interfaces for iwinfo if network.wireless is missing
            await self._process_iwinfo_fallback(devices)

        # 4. Process IP neighbor (ARP/NDP) findings
        await self._merge_neighbor_data(devices)

        # 5. Process wireless client details (hostapd)
        if wireless_ifaces:
            for wifi_iface in wireless_ifaces:
                candidate_names = [wifi_iface.name]
                if wifi_iface.ifname and wifi_iface.ifname not in candidate_names:
                    candidate_names.append(wifi_iface.ifname)
                if wifi_iface.section and wifi_iface.section not in candidate_names:
                    candidate_names.append(wifi_iface.section)

                found_hostapd = False
                for ifname in candidate_names:
                    if not ifname:
                        continue
                    try:
                        hostapd_data = await self._call(
                            f"hostapd.{ifname}", "get_clients"
                        )
                        if hostapd_data and isinstance(hostapd_data, dict):
                            clients = hostapd_data.get("clients")
                            if isinstance(clients, dict):
                                self._merge_hostapd_clients(
                                    devices, clients, wifi_iface.name or ifname
                                )
                                found_hostapd = True
                                if clients:
                                    break
                    except (
                        UbusTimeoutError,
                        UbusConnectionError,
                        UbusSslError,
                        UbusPermissionError,
                        UbusAuthError,
                    ):
                        raise
                    except UbusError:
                        pass

                if found_hostapd:
                    scanned_wireless_candidates.update(c for c in candidate_names if c)

            # Discover and poll any supplemental hostapd objects directly before coordinator fallback
            await self._process_hostapd_fallback(devices, scanned_wireless_candidates)

            # Wireless coordinator fallback:
            # If neither iwinfo nor hostapd succeeded for an interface (e.g. transient RPC failure),
            # restore cached devices for that interface to avoid flapping.
            if (
                self.coordinator
                and self.coordinator.data
                and self.coordinator.data.all_connected_devices
            ):
                for wifi_iface in wireless_ifaces:
                    candidate_names = [wifi_iface.name]
                    if wifi_iface.ifname and wifi_iface.ifname not in candidate_names:
                        candidate_names.append(wifi_iface.ifname)
                    if wifi_iface.section and wifi_iface.section not in candidate_names:
                        candidate_names.append(wifi_iface.section)

                    stable_id = None
                    if self.coordinator and hasattr(
                        self.coordinator, "interface_to_stable_id"
                    ):
                        stable_id = (
                            self.coordinator.interface_to_stable_id.get(wifi_iface.name)
                            or (
                                wifi_iface.section
                                and self.coordinator.interface_to_stable_id.get(
                                    wifi_iface.section
                                )
                            )
                            or (
                                wifi_iface.ifname
                                and self.coordinator.interface_to_stable_id.get(
                                    wifi_iface.ifname
                                )
                            )
                        )

                    def _is_cand_scanned(
                        c: str,
                        s_id: str | None = stable_id,
                        r_name: str | None = wifi_iface.radio,
                    ) -> bool:
                        if c in scanned_wireless_candidates:
                            return True
                        if (
                            s_id
                            and self.coordinator
                            and hasattr(self.coordinator, "interface_to_stable_id")
                        ):
                            if any(
                                self.coordinator.interface_to_stable_id.get(s) == s_id
                                for s in scanned_wireless_candidates
                            ):
                                return True
                        for s in scanned_wireless_candidates:
                            if r_name and (
                                s.startswith(r_name)
                                or r_name.replace("radio", "phy") in s
                            ):
                                return True
                        return False

                    if any(_is_cand_scanned(c) for c in candidate_names if c):
                        continue

                    for prev_dev in self.coordinator.data.all_connected_devices:
                        if (
                            prev_dev.is_wireless
                            and prev_dev.connected
                            and prev_dev.interface in candidate_names
                            and not _is_cand_scanned(prev_dev.interface)
                        ):
                            dev = devices.setdefault(
                                prev_dev.mac,
                                ConnectedDevice(
                                    mac=prev_dev.mac,
                                    ip=prev_dev.ip,
                                    hostname=prev_dev.hostname,
                                    connected=True,
                                    is_wireless=True,
                                    interface=wifi_iface.name or prev_dev.interface,
                                    connection_type=prev_dev.connection_type,
                                    signal=prev_dev.signal,
                                    noise=prev_dev.noise,
                                    rx_rate=prev_dev.rx_rate,
                                    tx_rate=prev_dev.tx_rate,
                                ),
                            )
                            dev.connected = True
                            dev.is_wireless = True
                            dev.interface = wifi_iface.name or prev_dev.interface
                            dev.connection_type = (
                                prev_dev.connection_type or dev.connection_type
                            )
                            dev.signal = prev_dev.signal or dev.signal
                            dev.noise = prev_dev.noise or dev.noise
                            dev.rx_rate = prev_dev.rx_rate or dev.rx_rate
                            dev.tx_rate = prev_dev.tx_rate or dev.tx_rate
        elif wireless_data and self.packages.wireless is not False:
            await self._process_hostapd_clients(devices, wireless_data)

        # 6. Supplemental source: Bridge FDB (Forwarding Database)
        # This helps identifying which physical port a wired device is on
        if self.trust_bridge_fdb:
            await self._process_bridge_fdb(devices)

        # Final cleanup/standardization
        for dev in devices.values():
            if not dev.connection_type:
                dev.connection_type = "wireless" if dev.is_wireless else "wired"

        return list(devices.values())

    async def _get_devices_from_dhcp(self, devices: dict[str, ConnectedDevice]) -> None:
        """Populate initial device list from DHCP leases."""
        try:
            leases = await self.get_dhcp_leases()
            for lease in leases:
                mac = lease.mac.lower()
                devices[mac] = ConnectedDevice(
                    mac=mac,
                    ip=lease.ip,
                    hostname=lease.hostname,
                    is_wireless=False,
                    connected=False,
                )
        except (
            UbusTimeoutError,
            UbusConnectionError,
            UbusSslError,
            UbusPermissionError,
            UbusAuthError,
        ):
            raise
        except Exception:
            pass

    async def get_local_macs(self) -> set[str]:
        """Get all MAC addresses belonging to the router's physical and virtual interfaces."""
        macs = set()
        with contextlib.suppress(Exception):
            status = await self._call("network.device", "status")
            if status and isinstance(status, dict):
                for dev_info in status.values():
                    if isinstance(dev_info, dict) and (mac := dev_info.get("macaddr")):
                        macs.add(mac.lower())
        return macs

    async def get_local_ips(self) -> set[str]:
        """Get all IP addresses belonging to the router."""
        ips = set()
        with contextlib.suppress(Exception):
            dump = await self._call("network.interface", "dump")
            if dump and isinstance(dump, dict) and (ifaces := dump.get("interface")):
                for iface in ifaces:
                    if not isinstance(iface, dict):
                        continue
                    # IPv4
                    for addr in iface.get("ipv4-address", []):
                        if isinstance(addr, dict) and (address := addr.get("address")):
                            ips.add(address)
                    # IPv6
                    for addr in iface.get("ipv6-address", []):
                        if isinstance(addr, dict) and (address := addr.get("address")):
                            ips.add(address)
        return ips

    async def get_ip_neighbors(self) -> list[IpNeighbor]:
        """Get IP neighbor (ARP/NDP) table."""
        neighbors: list[IpNeighbor] = []

        # 1. Try ubus network.device status
        await self._get_neighbors_ubus(neighbors)

        # 2. Try file.exec ip neigh show (more complete on many systems)
        await self._get_neighbors_ip_neigh(neighbors)

        # 3. Fallback to /proc/net/arp via file.read (passive)
        if not neighbors:
            await self._get_neighbors_proc_arp(neighbors)

        return neighbors

    async def _get_neighbors_ubus(self, neighbors: list[IpNeighbor]) -> None:
        """Fetch neighbors using 'network.device status' ubus call."""
        with contextlib.suppress(Exception):
            status = await self._call("network.device", "status")
            if status and isinstance(status, dict):
                for dev_name, dev_info in status.items():
                    if not isinstance(dev_info, dict):
                        continue
                    for neigh in dev_info.get("neighbors", []):
                        mac = neigh.get("lladdr")
                        ip = neigh.get("address")
                        if mac and ip:
                            # Filter out IPv6 link-local addresses (fe80::/10)
                            import ipaddress

                            try:
                                ip_obj = ipaddress.ip_address(ip)
                                if ip_obj.version == 6 and ip_obj.is_link_local:
                                    continue
                            except ValueError:
                                pass

                            neighbors.append(
                                IpNeighbor(
                                    ip=ip,
                                    mac=mac.lower(),
                                    interface=dev_name,
                                    state=neigh.get("state", "REACHABLE"),
                                ),
                            )

    async def _get_neighbors_ip_neigh(self, neighbors: list[IpNeighbor]) -> None:
        """Fetch neighbors using 'ip neigh show' via file.exec."""
        existing_macs = {n.mac.lower() for n in neighbors}
        with contextlib.suppress(Exception):
            content = await self.execute_command("ip neigh show")
            if content:
                for line in content.strip().split("\n"):
                    neigh = self._parse_ip_neigh_line(line)
                    if neigh and neigh.mac.lower() not in existing_macs:
                        neighbors.append(neigh)
                        existing_macs.add(neigh.mac.lower())

    def _parse_ip_neigh_line(self, line: str) -> IpNeighbor | None:
        """Parse a single line from 'ip neigh show' output."""
        parts = line.split()
        if len(parts) < 4:
            return None

        ip = parts[0]

        # Filter out IPv6 link-local addresses (fe80::/10) as they can cause devices
        # to be falsely reported as home due to stale link-local neighbor entries.
        import ipaddress

        try:
            ip_obj = ipaddress.ip_address(ip)
            if ip_obj.version == 6 and ip_obj.is_link_local:
                return None
        except ValueError:
            pass

        mac = ""
        interface = ""
        state = parts[-1]

        if "lladdr" in parts:
            idx = parts.index("lladdr")
            if len(parts) > idx + 1:
                mac = parts[idx + 1].upper()
        if "dev" in parts:
            idx = parts.index("dev")
            if len(parts) > idx + 1:
                interface = parts[idx + 1]

        if mac:
            return IpNeighbor(ip=ip, mac=mac, interface=interface, state=state)
        return None

    async def _get_neighbors_proc_arp(self, neighbors: list[IpNeighbor]) -> None:
        """Fetch neighbors from /proc/net/arp via file.read."""
        try:
            result = await self._call("file", "read", {"path": "/proc/net/arp"})
            content = result.get("data", "") if isinstance(result, dict) else ""
            if content:
                for line in content.strip().split("\n")[1:]:  # Skip header
                    parts = line.split()
                    if len(parts) >= 6:
                        neighbors.append(
                            IpNeighbor(
                                ip=parts[0],
                                mac=parts[3].upper(),
                                interface=parts[5],
                                state="REACHABLE",
                            ),
                        )
        except Exception as err:
            _LOGGER.debug(
                "file.read /proc/net/arp failed or denied (ACL may require '/proc/*/net/arp'): %s",
                err,
            )

    async def get_dnsmasq_lease_configs(self) -> list[tuple[str, str | None]]:
        """Get dnsmasq lease files and domains from UCI config."""
        configs = []
        try:
            config = await self._call("uci", "get", {"config": "dhcp"})
            if config and isinstance(config, dict):
                for _section, values in config.items():
                    if isinstance(values, dict) and values.get(".type") == "dnsmasq":
                        leasefile = values.get("leasefile") or "/tmp/dhcp.leases"
                        domain = values.get("domain")
                        configs.append((leasefile, domain))
        except Exception:
            pass
        if not configs:
            configs.append(("/tmp/dhcp.leases", None))
        return configs

    async def get_dhcp_leases(self) -> list[DhcpLease]:
        """Get DHCP leases via ubus or file."""
        if self.dhcp_software == "none":
            return []

        leases: list[DhcpLease] = []

        # Try odhcpd via ubus
        if self.dhcp_software in ("auto", "odhcpd") and self.packages.dhcp is not False:
            try:
                # IPv4 leases
                result = await self._call("dhcp", "ipv4leases")
                for lease_data in result.get("device", {}).values():
                    # odhcpd can return list or dict per interface
                    lease_list = (
                        lease_data if isinstance(lease_data, list) else [lease_data]
                    )
                    for lease in lease_list:
                        if not isinstance(lease, dict):
                            continue
                        leases.append(
                            DhcpLease(
                                hostname=lease.get("hostname", ""),
                                mac=lease.get("mac", "").lower(),
                                ip=lease.get("ipaddr", ""),
                                expires=lease.get("expires", 0),
                                type="v4",
                            ),
                        )

                # IPv6 leases
                result_v6 = await self._call("dhcp", "ipv6leases")
                for lease_data in result_v6.get("device", {}).values():
                    lease_list = (
                        lease_data if isinstance(lease_data, list) else [lease_data]
                    )
                    for lease in lease_list:
                        if not isinstance(lease, dict):
                            continue
                        leases.append(
                            DhcpLease(
                                hostname=lease.get("hostname", ""),
                                mac=lease.get("mac", "").lower(),
                                ip=lease.get("ipaddr", ""),
                                expires=lease.get("expires", 0),
                                type="v6",
                                duid=lease.get("duid", ""),
                            ),
                        )

                if leases and self.dhcp_software == "odhcpd":
                    return leases
            except UbusError:
                if self.dhcp_software == "odhcpd":
                    _LOGGER.debug("Requested odhcpd but 'dhcp' ubus object not found")
                    return []

        # Parse dnsmasq leases from lease files
        if self.dhcp_software in ("auto", "dnsmasq"):
            lease_configs = await self.get_dnsmasq_lease_configs()
            seen_leases = set()
            for leasefile, domain in lease_configs:
                content = ""
                with contextlib.suppress(UbusError):
                    # Priority 1: file.read (more robust/standard)
                    result = await self._call("file", "read", {"path": leasefile})
                    content = result.get("data", "")

                if not content:
                    with contextlib.suppress(Exception):
                        # Priority 2: file.exec (original fallback)
                        content = await self.execute_command(
                            f"cat {leasefile} 2>/dev/null",
                        )

                if content:
                    for line in content.strip().split("\n"):
                        parts = line.split()
                        if len(parts) >= 4:
                            mac = parts[1].lower()
                            ip = parts[2]
                            if (mac, ip) in seen_leases:
                                continue
                            seen_leases.add((mac, ip))
                            hostname = parts[3] if parts[3] != "*" else ""
                            if hostname and domain and "." not in hostname:
                                hostname = f"{hostname}.{domain}"
                            leases.append(
                                DhcpLease(
                                    expires=int(parts[0]) if parts[0].isdigit() else 0,
                                    mac=mac,
                                    ip=ip,
                                    hostname=hostname,
                                ),
                            )
                elif self.dhcp_software == "dnsmasq":
                    _LOGGER.debug("Requested dnsmasq but could not read %s", leasefile)

        return leases

    async def get_lldp_neighbors(self) -> list[LldpNeighbor]:
        """Get LLDP neighbor information via ubus."""
        from ..base import LldpNeighbor

        neighbors: list[LldpNeighbor] = []
        if self.packages.lldp is False:
            return neighbors

        try:
            # ubus call lldp show
            data = await self._call("lldp", "show")
            # Parse ubus lldp output structure
            interfaces = data.get("lldp", {}).get("interface", [])
            if isinstance(interfaces, list):
                for iface in interfaces:
                    name = iface.get("name")
                    neighs = iface.get("neighbor", [])
                    if isinstance(neighs, list):
                        for neigh in neighs:
                            neighbors.append(
                                LldpNeighbor(
                                    local_interface=name or "",
                                    neighbor_name=neigh.get("name", ""),
                                    neighbor_port=(
                                        neigh.get("port", {}).get("id", "")
                                        if isinstance(neigh.get("port"), dict)
                                        else ""
                                    ),
                                    neighbor_chassis=(
                                        neigh.get("chassis", {}).get(
                                            "id",
                                            "",
                                        )
                                        if isinstance(neigh.get("chassis"), dict)
                                        else ""
                                    ),
                                    neighbor_description=neigh.get("description", ""),
                                    neighbor_system_name=neigh.get("sysname", ""),
                                ),
                            )
        except Exception as err:
            _LOGGER.debug("Failed to get LLDP neighbors via ubus: %s", err)
        return neighbors

    async def kick_device(self, mac_address: str, interface: str) -> bool:
        """Kick a wireless device from the network using hostapd via direct ubus call."""
        try:
            await self._call(
                f"hostapd.{interface}",
                "del_client",
                {"addr": mac_address, "reason": 5, "deauth": True, "ban_time": 60000},
            )
            return True
        except Exception as err:
            _LOGGER.debug(
                "Failed to kick device via hostapd ubus direct call: %s. Trying fallback.",
                err,
            )
            return await super().kick_device(mac_address, interface)
