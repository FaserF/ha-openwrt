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
from .exceptions import *

_LOGGER = logging.getLogger(__name__)
UBUS_JSONRPC_VERSION = "2.0"
UBUS_ID_AUTH = 1
UBUS_ID_CALL = 2


class UbusDevicesMixin:
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
        if wireless_ifaces:
            for wifi_iface in wireless_ifaces:
                ifname = wifi_iface.name
                if not ifname:
                    continue
                try:
                    assoc = await self._call("iwinfo", "assoclist", {"device": ifname})
                    if assoc and isinstance(assoc, dict):
                        for client in assoc.get("results", []):
                            mac = client.get("mac", "").lower()
                            dev = devices.setdefault(
                                mac, ConnectedDevice(mac=mac, connected=True)
                            )
                            dev.connected = True
                            dev.is_wireless = True
                            dev.interface = ifname
                            self._set_wireless_connection_type(dev, ifname)
                            dev.signal = client.get("signal", 0)
                            dev.noise = client.get("noise", 0)
                            dev.rx_rate = self._get_assoc_rate(client, "rx")
                            dev.tx_rate = self._get_assoc_rate(client, "tx")
                except (
                    UbusTimeoutError,
                    UbusConnectionError,
                    UbusSslError,
                    UbusPermissionError,
                    UbusAuthError,
                ):
                    raise
                except UbusError:
                    if self.coordinator and self.coordinator.data and self.coordinator.data.all_connected_devices:
                        for prev_dev in self.coordinator.data.all_connected_devices:
                            if prev_dev.is_wireless and prev_dev.connected and prev_dev.interface == ifname:
                                dev = devices.setdefault(
                                    prev_dev.mac,
                                    ConnectedDevice(
                                        mac=prev_dev.mac,
                                        ip=prev_dev.ip,
                                        hostname=prev_dev.hostname,
                                        connected=True,
                                        is_wireless=True,
                                        interface=ifname,
                                        connection_type=prev_dev.connection_type,
                                        signal=prev_dev.signal,
                                        noise=prev_dev.noise,
                                        rx_rate=prev_dev.rx_rate,
                                        tx_rate=prev_dev.tx_rate,
                                    )
                                )
                                dev.connected = True
                                dev.is_wireless = True
                                dev.interface = ifname
                                dev.connection_type = prev_dev.connection_type or dev.connection_type
                                dev.signal = prev_dev.signal or dev.signal
                                dev.noise = prev_dev.noise or dev.noise
                                dev.rx_rate = prev_dev.rx_rate or dev.rx_rate
                                dev.tx_rate = prev_dev.tx_rate or dev.tx_rate
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
                ifname = wifi_iface.name
                if not ifname:
                    continue
                try:
                    hostapd_data = await self._call(f"hostapd.{ifname}", "get_clients")
                    if hostapd_data and isinstance(hostapd_data, dict):
                        clients = hostapd_data.get("clients")
                        if isinstance(clients, dict):
                            self._merge_hostapd_clients(devices, clients, ifname)
                except (
                    UbusTimeoutError,
                    UbusConnectionError,
                    UbusSslError,
                    UbusPermissionError,
                    UbusAuthError,
                ):
                    raise
                except UbusError:
                    if self.coordinator and self.coordinator.data and self.coordinator.data.all_connected_devices:
                        for prev_dev in self.coordinator.data.all_connected_devices:
                            if prev_dev.is_wireless and prev_dev.connected and prev_dev.interface == ifname:
                                dev = devices.setdefault(
                                    prev_dev.mac,
                                    ConnectedDevice(
                                        mac=prev_dev.mac,
                                        ip=prev_dev.ip,
                                        hostname=prev_dev.hostname,
                                        connected=True,
                                        is_wireless=True,
                                        interface=ifname,
                                        connection_type=prev_dev.connection_type,
                                        signal=prev_dev.signal,
                                        noise=prev_dev.noise,
                                        rx_rate=prev_dev.rx_rate,
                                        tx_rate=prev_dev.tx_rate,
                                    )
                                )
                                dev.connected = True
                                dev.is_wireless = True
                                dev.interface = ifname
                                dev.connection_type = prev_dev.connection_type or dev.connection_type
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

        # Always run fallback to ensure we catch any manually added or mesh interfaces
        if self.packages.wireless is not False:
            await self._process_hostapd_fallback(devices)

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

    async def _process_iwinfo_assoc(
        self, devices: dict[str, ConnectedDevice], wireless_data: dict[str, Any]
    ) -> None:
        """Fetch and merge iwinfo association lists."""
        for radio_data in wireless_data.values():
            if not isinstance(radio_data, dict):
                continue
            for iface in radio_data.get("interfaces", []):
                ifname = iface.get("ifname") or iface.get("device", "")
                if not ifname:
                    continue
                try:
                    assoc = await self._call("iwinfo", "assoclist", {"device": ifname})
                    if assoc and isinstance(assoc, dict):
                        for client in assoc.get("results", []):
                            mac = client.get("mac", "").lower()
                            dev = devices.setdefault(
                                mac, ConnectedDevice(mac=mac, connected=True)
                            )
                            dev.connected = True
                            dev.is_wireless = True
                            dev.interface = ifname
                            self._set_wireless_connection_type(dev, ifname)
                            dev.signal = client.get("signal", 0)
                            dev.noise = client.get("noise", 0)
                            dev.rx_rate = self._get_assoc_rate(client, "rx")
                            dev.tx_rate = self._get_assoc_rate(client, "tx")
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

    async def _merge_neighbor_data(self, devices: dict[str, ConnectedDevice]) -> None:
        """Update devices with ARP/neighbor information."""
        try:
            neighbors = await self.get_ip_neighbors()
            # STALE is intentionally included: Linux kernels age ARP entries to
            # STALE very quickly (30-60 s).  A STALE entry means the device WAS
            # reachable and likely still is – it will transition back to REACHABLE
            # on the next unicast exchange.  Excluding STALE would cause wired
            # clients to disappear from the count even while actively using the
            # network.
            active_states = ["REACHABLE", "DELAY", "PROBE", "PERMANENT"]
            if self.trust_stale_arp:
                active_states.append("STALE")
            for neigh in neighbors:
                mac = neigh.mac.lower()
                if not mac:
                    continue
                if mac in devices:
                    dev = devices[mac]
                    dev.neighbor_state = dev.neighbor_state or neigh.state
                    dev.interface = dev.interface or neigh.interface
                    # Mark wired devices as connected when the kernel's ARP table
                    # shows a recent (/active) entry.
                    if not dev.is_wireless and neigh.state.upper() in active_states:
                        dev.connected = True
                else:
                    is_active = neigh.state.upper() in active_states
                    devices[mac] = ConnectedDevice(
                        mac=mac,
                        ip=neigh.ip,
                        interface=neigh.interface,
                        connected=is_active,
                        connection_type="wired",
                        neighbor_state=neigh.state,
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

    async def _process_hostapd_clients(
        self, devices: dict[str, ConnectedDevice], wireless_data: dict[str, Any]
    ) -> None:
        """Fetch and merge hostapd client details (bytes/counters)."""
        for radio_data in wireless_data.values():
            if not isinstance(radio_data, dict):
                continue
            for iface in radio_data.get("interfaces", []):
                ifname = iface.get("ifname", "")
                if not ifname:
                    continue
                try:
                    hostapd_data = await self._call(f"hostapd.{ifname}", "get_clients")
                    if hostapd_data and isinstance(hostapd_data, dict):
                        clients = hostapd_data.get("clients")
                        if isinstance(clients, dict):
                            self._merge_hostapd_clients(devices, clients, ifname)
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

    async def _get_devices_from_static_leases(
        self, devices: dict[str, ConnectedDevice]
    ) -> None:
        """Populate device list from static DHCP leases in UCI."""
        try:
            config = await self._call("uci", "get", {"config": "dhcp"})
            if not config or not isinstance(config, dict):
                return

            for _section, values in config.items():
                if values.get(".type") == "host":
                    macs = values.get("mac")
                    if not macs:
                        continue

                    # mac can be a space-separated string or a list
                    if isinstance(macs, str):
                        mac_list = macs.split()
                    else:
                        mac_list = macs

                    for mac in mac_list:
                        mac_lower = mac.lower()
                        if mac_lower not in devices:
                            devices[mac_lower] = ConnectedDevice(
                                mac=mac_lower,
                                ip=values.get("ip", ""),
                                hostname=values.get("name", ""),
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

    async def _process_bridge_fdb(self, devices: dict[str, ConnectedDevice]) -> None:
        """Fetch and merge bridge FDB (forwarding database) information."""
        try:
            # 1. Fetch all network devices to find bridges and members
            device_status = await self._call("network.device", "status")
            if not device_status or not isinstance(device_status, dict):
                return

            # 2. For each device, fetch its FDB if it's a bridge or has members
            for dev_name, dev_info in device_status.items():
                if not dev_info.get("up"):
                    continue

                try:
                    fdb = await self._call("network.device", "fdb", {"name": dev_name})
                    if fdb and isinstance(fdb, list):
                        for entry in fdb:
                            mac = entry.get("mac", "").lower()
                            if mac not in devices:
                                continue

                            dev = devices[mac]
                            # Only apply to wired devices or as supplemental info
                            port = entry.get("port", "")
                            if port:
                                dev.port = port
                                dev.fdb_age = entry.get("age")
                                if dev.fdb_age is None or dev.fdb_age < 60:
                                    dev.connected = (
                                        True  # Seen on a physical port recently
                                    )
                                # If it's a wired device, we can improve its interface info
                                if not dev.is_wireless and not dev.interface:
                                    dev.interface = dev_name
                except (
                    UbusTimeoutError,
                    UbusConnectionError,
                    UbusSslError,
                    UbusPermissionError,
                    UbusAuthError,
                ):
                    raise
                except Exception:
                    continue
        except (
            UbusTimeoutError,
            UbusConnectionError,
            UbusSslError,
            UbusPermissionError,
            UbusAuthError,
        ):
            raise
        except Exception as err:
            _LOGGER.debug("Failed to fetch bridge FDB: %s", err)

    async def _process_iwinfo_fallback(
        self, devices: dict[str, ConnectedDevice]
    ) -> None:
        """Discover wireless interfaces from ubus object list and poll iwinfo."""
        try:
            objects = await self._list_objects()
            # On some devices, interfaces are named wlan0, wlan1, etc.
            # or have hostapd.wlan0 objects.
            candidates = set()
            for obj in objects:
                if obj.startswith("hostapd."):
                    candidates.add(obj.split(".", 1)[1])
                elif obj in ("iwinfo", "network.wireless"):
                    continue

            # Also try to discover candidates via iwinfo devices
            try:
                iw_devs = await self._call("iwinfo", "devices")
                if isinstance(iw_devs, list):
                    candidates.update(iw_devs)
                elif isinstance(iw_devs, dict) and "devices" in iw_devs:
                    candidates.update(iw_devs["devices"])
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

            # Additional common names if nothing found
            if not candidates:
                candidates = {
                    "wlan0",
                    "wlan1",
                    "wlan0-1",
                    "wlan1-1",
                    "ra0",
                    "ra1",
                    "rax0",
                    "rax1",
                }

            for ifname in candidates:
                try:
                    assoc = await self._call("iwinfo", "assoclist", {"device": ifname})
                    if assoc and isinstance(assoc, dict):
                        for client in assoc.get("results", []):
                            mac = client.get("mac", "").lower()
                            dev = devices.setdefault(
                                mac, ConnectedDevice(mac=mac, connected=True)
                            )
                            dev.connected = True
                            dev.is_wireless = True
                            dev.interface = ifname
                            self._set_wireless_connection_type(dev, ifname)
                            dev.signal = client.get("signal", 0)
                            dev.noise = client.get("noise", 0)
                except (
                    UbusTimeoutError,
                    UbusConnectionError,
                    UbusSslError,
                    UbusPermissionError,
                    UbusAuthError,
                ):
                    raise
                except UbusError:
                    if self.coordinator and self.coordinator.data and self.coordinator.data.all_connected_devices:
                        for prev_dev in self.coordinator.data.all_connected_devices:
                            if prev_dev.is_wireless and prev_dev.connected and prev_dev.interface == ifname:
                                dev = devices.setdefault(
                                    prev_dev.mac,
                                    ConnectedDevice(
                                        mac=prev_dev.mac,
                                        ip=prev_dev.ip,
                                        hostname=prev_dev.hostname,
                                        connected=True,
                                        is_wireless=True,
                                        interface=ifname,
                                        connection_type=prev_dev.connection_type,
                                        signal=prev_dev.signal,
                                        noise=prev_dev.noise,
                                        rx_rate=prev_dev.rx_rate,
                                        tx_rate=prev_dev.tx_rate,
                                    )
                                )
                                dev.connected = True
                                dev.is_wireless = True
                                dev.interface = ifname
                                dev.connection_type = prev_dev.connection_type or dev.connection_type
                                dev.signal = prev_dev.signal or dev.signal
                                dev.noise = prev_dev.noise or dev.noise
                                dev.rx_rate = prev_dev.rx_rate or dev.rx_rate
                                dev.tx_rate = prev_dev.tx_rate or dev.tx_rate
                    continue
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

    async def _process_hostapd_fallback(
        self, devices: dict[str, ConnectedDevice]
    ) -> None:
        """Fallback: Discover and poll hostapd objects directly."""
        try:
            ubus_objects = await self._list_objects()
            for obj_name in ubus_objects:
                if obj_name.startswith("hostapd."):
                    ifname = obj_name.split(".", 1)[1]
                    try:
                        hostapd_data = await self._call(obj_name, "get_clients")
                        if hostapd_data and isinstance(hostapd_data, dict):
                            clients = hostapd_data.get("clients")
                            if isinstance(clients, dict):
                                self._merge_hostapd_clients(devices, clients, ifname)
                    except (
                        UbusTimeoutError,
                        UbusConnectionError,
                        UbusSslError,
                        UbusPermissionError,
                        UbusAuthError,
                    ):
                        raise
                    except UbusError:
                        if self.coordinator and self.coordinator.data and self.coordinator.data.all_connected_devices:
                            for prev_dev in self.coordinator.data.all_connected_devices:
                                if prev_dev.is_wireless and prev_dev.connected and prev_dev.interface == ifname:
                                    dev = devices.setdefault(
                                        prev_dev.mac,
                                        ConnectedDevice(
                                            mac=prev_dev.mac,
                                            ip=prev_dev.ip,
                                            hostname=prev_dev.hostname,
                                            connected=True,
                                            is_wireless=True,
                                            interface=ifname,
                                            connection_type=prev_dev.connection_type,
                                            signal=prev_dev.signal,
                                            noise=prev_dev.noise,
                                            rx_rate=prev_dev.rx_rate,
                                            tx_rate=prev_dev.tx_rate,
                                        )
                                    )
                                    dev.connected = True
                                    dev.is_wireless = True
                                    dev.interface = ifname
                                    dev.connection_type = prev_dev.connection_type or dev.connection_type
                                    dev.signal = prev_dev.signal or dev.signal
                                    dev.noise = prev_dev.noise or dev.noise
                                    dev.rx_rate = prev_dev.rx_rate or dev.rx_rate
                                    dev.tx_rate = prev_dev.tx_rate or dev.tx_rate
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

    def _merge_hostapd_clients(
        self, devices: dict[str, ConnectedDevice], clients: dict[str, Any], ifname: str
    ) -> None:
        """Merge client data from hostapd into the devices dictionary."""
        for mac_addr, client_data in clients.items():
            mac = mac_addr.lower()
            dev = devices.setdefault(mac, ConnectedDevice(mac=mac, connected=True))
            dev.connected = True
            dev.is_wireless = True
            dev.interface = ifname
            self._set_wireless_connection_type(dev, ifname)

            bytes_data = client_data.get("bytes", {})
            if isinstance(bytes_data, dict):
                dev.rx_bytes = bytes_data.get("rx", 0)
                dev.tx_bytes = bytes_data.get("tx", 0)

            # Hostapd returns rate in 100kbps (tenths of Mbps).
            # Convert to Kbps by multiplying by 100.
            if "rx_rate" in client_data and not dev.rx_rate:
                dev.rx_rate = client_data.get("rx_rate", 0) * 100
            if "tx_rate" in client_data and not dev.tx_rate:
                dev.tx_rate = client_data.get("tx_rate", 0) * 100

    def _set_wireless_connection_type(self, dev: ConnectedDevice, ifname: str) -> None:
        """Determine specific wireless band from interface name."""
        if not dev.connection_type or dev.connection_type == "wired":
            dev.connection_type = "wireless"
            if "5g" in ifname.lower():
                dev.connection_type = "5GHz"
            elif "2g" in ifname.lower():
                dev.connection_type = "2.4GHz"

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
        with contextlib.suppress(Exception):
            result = await self._call("file", "read", {"path": "/proc/net/arp"})
            content = result.get("data", "")
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

        # Parse dnsmasq leases from /tmp/dhcp.leases
        if self.dhcp_software in ("auto", "dnsmasq"):
            content = ""
            with contextlib.suppress(UbusError):
                # Priority 1: file.read (more robust/standard)
                result = await self._call("file", "read", {"path": "/tmp/dhcp.leases"})
                content = result.get("data", "")

            if not content:
                with contextlib.suppress(Exception):
                    # Priority 2: file.exec (original fallback)
                    content = await self.execute_command(
                        "cat /tmp/dhcp.leases 2>/dev/null",
                    )

            if content:
                for line in content.strip().split("\n"):
                    parts = line.split()
                    if len(parts) >= 4:
                        leases.append(
                            DhcpLease(
                                expires=int(parts[0]) if parts[0].isdigit() else 0,
                                mac=parts[1].lower(),
                                ip=parts[2],
                                hostname=parts[3] if parts[3] != "*" else "",
                            ),
                        )
            elif self.dhcp_software == "dnsmasq":
                _LOGGER.debug("Requested dnsmasq but could not read /tmp/dhcp.leases")

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
