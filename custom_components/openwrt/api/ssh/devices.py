# mypy: disable-error-code="attr-defined"
from __future__ import annotations

import contextlib
import json
import logging
import re
import shlex

from ..base import (
    ConnectedDevice,
    DhcpLease,
)
from .exceptions import *

_LOGGER = logging.getLogger(__name__)


class SshDevicesMixin:
    """Devices methods for SshClient."""

    async def get_connected_devices(self) -> list[ConnectedDevice]:
        """Get connected devices by combining DHCP, ARP and wireless station info."""
        devices: dict[str, ConnectedDevice] = {}

        # 1. DHCP Leases
        await self._add_dhcp_devices_ssh(devices)

        # 2. IP Neighbors
        await self._add_neighbor_devices_ssh(devices)

        # 3. Wireless Clients (iwinfo station dump)
        await self._add_wireless_devices_iwinfo_ssh(devices)

        # 4. Fallback to wireless clients via ubus (hostapd)
        if not any(d.is_wireless for d in devices.values()):
            await self._add_wireless_devices_ubus_ssh(devices)

        # 4. Supplemental source: Bridge FDB (Forwarding Database)
        if self.trust_bridge_fdb:
            await self._process_bridge_fdb(devices)

        return list(devices.values())

    async def _process_bridge_fdb(self, devices: dict[str, ConnectedDevice]) -> None:
        """Fetch and merge bridge FDB (forwarding database) information via SSH."""
        try:
            # 1. Fetch all network devices
            dev_status_str = await self._exec(
                "ubus call network.device status 2>/dev/null"
            )
            if not dev_status_str or not dev_status_str.strip().startswith("{"):
                return

            device_status = json.loads(dev_status_str)

            # 2. For each active device, fetch its FDB
            for dev_name, dev_info in device_status.items():
                if not dev_info.get("up"):
                    continue

                try:
                    safe_arg = shlex.quote(json.dumps({"name": dev_name}))
                    fdb_str = await self._exec(
                        f"ubus call network.device fdb {safe_arg} 2>/dev/null"
                    )
                    if fdb_str and fdb_str.strip().startswith("["):
                        fdb = json.loads(fdb_str)
                        for entry in fdb:
                            mac = entry.get("mac", "").lower()
                            if mac not in devices:
                                continue

                            dev = devices[mac]
                            port = entry.get("port", "")
                            if port:
                                dev.port = port
                                dev.fdb_age = entry.get("age")
                                if dev.fdb_age is None or dev.fdb_age < 60:
                                    dev.connected = (
                                        True  # Seen on a physical port recently
                                    )
                                if not dev.is_wireless and not dev.interface:
                                    dev.interface = dev_name
                except SshError:
                    raise
                except Exception:
                    continue
        except SshError:
            raise
        except Exception as err:
            _LOGGER.debug("Failed to fetch bridge FDB via SSH: %s", err)

    async def _add_dhcp_devices_ssh(self, devices: dict[str, ConnectedDevice]) -> None:
        """Add devices discovered via DHCP leases."""
        try:
            leases = await self.get_dhcp_leases()
            for lease in leases:
                mac = lease.mac.lower()
                devices[mac] = ConnectedDevice(
                    mac=mac,
                    ip=lease.ip,
                    hostname=lease.hostname,
                    connected=False,  # DHCP alone is not proof of connectivity
                    is_wireless=False,
                    connection_type="wired",
                )
        except SshError:
            raise
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("DHCP device discovery failed (SSH): %s", err)

    async def _add_neighbor_devices_ssh(
        self, devices: dict[str, ConnectedDevice]
    ) -> None:
        """Add or update devices discovered via IP neighbors (ARP)."""
        try:
            neighbors = await self.get_ip_neighbors()
            active_states = ["REACHABLE", "DELAY", "PROBE", "PERMANENT"]
            if self.trust_stale_arp:
                active_states.append("STALE")
            for neigh in neighbors:
                mac = neigh.mac.lower()
                if not mac:
                    continue

                is_active = neigh.state.upper() in active_states

                if mac in devices:
                    dev = devices[mac]
                    if not dev.neighbor_state:
                        dev.neighbor_state = neigh.state
                    if not dev.interface:
                        dev.interface = neigh.interface
                    # Only mark as connected via ARP if not already confirmed as wireless
                    # to prevent stale ARP entries from keeping wireless devices 'home'.
                    if not dev.is_wireless and is_active:
                        dev.connected = True
                    continue

                devices[mac] = ConnectedDevice(
                    mac=mac,
                    ip=neigh.ip,
                    interface=neigh.interface,
                    connected=is_active,
                    is_wireless=False,
                    connection_type="wired",
                    neighbor_state=neigh.state,
                )
        except SshError:
            raise
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Neighbor device discovery failed (SSH): %s", err)

    async def _add_wireless_devices_iwinfo_ssh(
        self, devices: dict[str, ConnectedDevice]
    ) -> None:
        """Add or update wireless devices via iwinfo ubus calls."""
        if self.packages.wireless is False:
            return
        try:
            # Use get_wireless_interfaces to find active interfaces
            wireless_ifaces = await self.get_wireless_interfaces()
            for wifi_iface in wireless_ifaces:
                iface_name = wifi_iface.name
                # Use ubus call for JSON output over SSH
                safe_arg = shlex.quote(json.dumps({"device": iface_name}))
                assoc_str = await self._exec(
                    f"ubus call iwinfo assoclist {safe_arg} 2>/dev/null"
                )
                if assoc_str and assoc_str.strip().startswith("{"):
                    assoc = json.loads(assoc_str).get("results", [])
                    for client in assoc:
                        mac = client.get("mac", "").lower()
                        if not mac:
                            continue

                        dev = devices.setdefault(
                            mac, ConnectedDevice(mac=mac, connected=True)
                        )
                        dev.connected = True
                        dev.is_wireless = True
                        dev.interface = iface_name
                        dev.signal = client.get("signal", 0)
                        dev.noise = client.get("noise", 0)
                        dev.rx_rate = self._get_assoc_rate(client, "rx")
                        dev.tx_rate = self._get_assoc_rate(client, "tx")

                        # Set connection type based on interface frequency/name
                        if "5g" in iface_name.lower() or (
                            wifi_iface.frequency and "5" in wifi_iface.frequency
                        ):
                            dev.connection_type = "5GHz"
                        elif "6g" in iface_name.lower() or (
                            wifi_iface.frequency and "6" in wifi_iface.frequency
                        ):
                            dev.connection_type = "6GHz"
                        elif "2g" in iface_name.lower() or (
                            wifi_iface.frequency and "2" in wifi_iface.frequency
                        ):
                            dev.connection_type = "2.4GHz"
                        else:
                            dev.connection_type = "wireless"
        except SshError:
            raise
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("iwinfo wireless discovery failed (SSH): %s", err)
            if self.coordinator and self.coordinator.data and self.coordinator.data.all_connected_devices:
                for prev_dev in self.coordinator.data.all_connected_devices:
                    if prev_dev.is_wireless and prev_dev.connected:
                        dev = devices.setdefault(
                            prev_dev.mac,
                            ConnectedDevice(
                                mac=prev_dev.mac,
                                ip=prev_dev.ip,
                                hostname=prev_dev.hostname,
                                connected=True,
                                is_wireless=True,
                                interface=prev_dev.interface,
                                connection_type=prev_dev.connection_type,
                                signal=prev_dev.signal,
                                noise=prev_dev.noise,
                                rx_rate=prev_dev.rx_rate,
                                tx_rate=prev_dev.tx_rate,
                            )
                        )
                        dev.connected = True
                        dev.is_wireless = True
                        dev.interface = prev_dev.interface or dev.interface
                        dev.connection_type = prev_dev.connection_type or dev.connection_type
                        dev.signal = prev_dev.signal or dev.signal
                        dev.noise = prev_dev.noise or dev.noise
                        dev.rx_rate = prev_dev.rx_rate or dev.rx_rate
                        dev.tx_rate = prev_dev.tx_rate or dev.tx_rate

    async def _add_wireless_devices_ubus_ssh(
        self, devices: dict[str, ConnectedDevice]
    ) -> None:
        """Add or update wireless devices via ubus hostapd."""
        if self.packages.wireless is False:
            return
        try:
            cmd = "for obj in $(ubus list 'hostapd.*'); do echo \"$obj $(ubus call $obj get_clients)\"; done"
            stdout = await self._exec(cmd)
            for line in stdout.splitlines():
                if not (line := line.strip()):
                    continue
                parts = line.split(" ", 1)
                if len(parts) < 2:
                    continue
                obj_name, data_str = parts
                iface_name = obj_name.split(".", 1)[1] if "." in obj_name else obj_name
                try:
                    data = json.loads(data_str)
                    if data and isinstance(data, dict):
                        clients = data.get("clients")
                        if isinstance(clients, dict):
                            for mac, info in clients.items():
                                mac_lower = mac.lower()
                                dev = devices.setdefault(
                                    mac_lower,
                                    ConnectedDevice(mac=mac_lower, connected=True),
                                )
                                dev.is_wireless = True
                                dev.interface = iface_name
                                dev.signal = info.get("signal", 0)

                                bytes_data = info.get("bytes", {})
                                if isinstance(bytes_data, dict):
                                    dev.rx_bytes = bytes_data.get("rx", 0)
                                    dev.tx_bytes = bytes_data.get("tx", 0)

                                # Hostapd returns rate in 100kbps (tenths of Mbps).
                                # Convert to Kbps by multiplying by 100.
                                if "rx_rate" in info and not dev.rx_rate:
                                    dev.rx_rate = info.get("rx_rate", 0) * 100
                                if "tx_rate" in info and not dev.tx_rate:
                                    dev.tx_rate = info.get("tx_rate", 0) * 100
                                dev.connection_type = (
                                    "5GHz"
                                    if "5g" in iface_name.lower()
                                    else (
                                        "2.4GHz"
                                        if "2g" in iface_name.lower()
                                        else "wireless"
                                    )
                                )
                except (json.JSONDecodeError, KeyError):
                    continue
        except SshError:
            raise
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("hostapd wireless discovery failed (SSH): %s", err)
            if self.coordinator and self.coordinator.data and self.coordinator.data.all_connected_devices:
                for prev_dev in self.coordinator.data.all_connected_devices:
                    if prev_dev.is_wireless and prev_dev.connected:
                        dev = devices.setdefault(
                            prev_dev.mac,
                            ConnectedDevice(
                                mac=prev_dev.mac,
                                ip=prev_dev.ip,
                                hostname=prev_dev.hostname,
                                connected=True,
                                is_wireless=True,
                                interface=prev_dev.interface,
                                connection_type=prev_dev.connection_type,
                                signal=prev_dev.signal,
                                noise=prev_dev.noise,
                                rx_rate=prev_dev.rx_rate,
                                tx_rate=prev_dev.tx_rate,
                            )
                        )
                        dev.connected = True
                        dev.is_wireless = True
                        dev.interface = prev_dev.interface or dev.interface
                        dev.connection_type = prev_dev.connection_type or dev.connection_type
                        dev.signal = prev_dev.signal or dev.signal
                        dev.noise = prev_dev.noise or dev.noise
                        dev.rx_rate = prev_dev.rx_rate or dev.rx_rate
                        dev.tx_rate = prev_dev.tx_rate or dev.tx_rate

    async def get_dhcp_leases(self) -> list[DhcpLease]:
        """Get DHCP leases via SSH."""
        if self.dhcp_software == "none":
            return []

        leases: list[DhcpLease] = []

        # 1. Try odhcpd via ubus
        if self.dhcp_software in ("auto", "odhcpd") and self.packages.dhcp is not False:
            await self._get_leases_odhcpd(leases)
            if leases and self.dhcp_software == "odhcpd":
                return leases

        # 2. Try dnsmasq via file
        if (
            self.dhcp_software in ("auto", "dnsmasq")
            and self.packages.dhcp is not False
        ):
            await self._get_leases_dnsmasq(leases)

        return leases

    async def _get_leases_odhcpd(self, leases: list[DhcpLease]) -> None:
        """Fetch DHCP leases from odhcpd via ubus over SSH."""
        with contextlib.suppress(Exception):
            # IPv4
            stdout = await self._exec("ubus call dhcp ipv4leases 2>/dev/null")
            if stdout and stdout.strip().startswith("{"):
                data = json.loads(stdout)
                for lease_data in data.get("device", {}).values():
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

            # IPv6
            stdout_v6 = await self._exec("ubus call dhcp ipv6leases 2>/dev/null")
            if stdout_v6 and stdout_v6.strip().startswith("{"):
                data_v6 = json.loads(stdout_v6)
                for lease_data in data_v6.get("device", {}).values():
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

    async def _get_leases_dnsmasq(self, leases: list[DhcpLease]) -> None:
        """Fetch DHCP leases from dnsmasq lease file via SSH."""
        with contextlib.suppress(Exception):
            content = await self._exec("cat /tmp/dhcp.leases 2>/dev/null")
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

    async def get_local_macs(self) -> set[str]:
        """Get all MAC addresses belonging to the router's physical and virtual interfaces."""
        macs = set()
        try:
            # Use 'ip link show' which is very reliable
            stdout = await self._exec("ip link show 2>/dev/null")
            if stdout:
                # Find lines like 'link/ether 00:11:22:33:44:55 ...'
                matches = re.finditer(r"link/ether\s+([0-9a-fA-F:]{17})", stdout)
                for match in matches:
                    macs.add(match.group(1).lower())
        except Exception:  # noqa: BLE001
            pass
        return macs

    async def get_local_ips(self) -> set[str]:
        """Get all IP addresses belonging to the router."""
        ips = set()
        try:
            # Use 'ip addr show'
            stdout = await self._exec("ip addr show 2>/dev/null")
            if stdout:
                # Find IPv4 and IPv6 addresses
                ipv4_matches = re.finditer(r"inet\s+([0-9.]+)/", stdout)
                for match in ipv4_matches:
                    ips.add(match.group(1))
                ipv6_matches = re.finditer(r"inet6\s+([0-9a-fA-F:]+)/", stdout)
                for match in ipv6_matches:
                    ips.add(match.group(1))
        except Exception:  # noqa: BLE001
            pass
        return ips
