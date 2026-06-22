# mypy: disable-error-code="attr-defined"
from __future__ import annotations

import json
import logging

from ..base import (
    ConnectedDevice,
    DhcpLease,
)
from .exceptions import *

_LOGGER = logging.getLogger(__name__)


class LuciRpcDevicesMixin:
    """Devices methods for LuciRpcClient."""

    async def get_connected_devices(self) -> list[ConnectedDevice]:
        """Get connected devices by combining DHCP, ARP and wireless station info via sys.exec."""
        # Ensure mapping is available
        await self._get_wireless_mapping()
        devices: dict[str, ConnectedDevice] = {}

        # 1. DHCP Leases
        try:
            leases_str = await self.execute_command("cat /tmp/dhcp.leases 2>/dev/null")
            if leases_str:
                for line in leases_str.strip().split("\n"):
                    parts = line.split()
                    if len(parts) >= 4:
                        mac = parts[1].lower()
                        devices[mac] = ConnectedDevice(
                            mac=mac,
                            ip=parts[2],
                            hostname=parts[3] if parts[3] != "*" else "",
                            connected=False,  # DHCP alone is not proof of connectivity
                            is_wireless=False,
                            connection_type="wired",
                        )
        except (
            LuciRpcTimeoutError,
            LuciRpcConnectionError,
            LuciRpcSslError,
            LuciRpcAuthError,
        ):
            raise
        except LuciRpcError:
            pass

        # 2. ARP Neighbors
        try:
            arp = await self.execute_command("cat /proc/net/arp 2>/dev/null")
            if arp:
                lines = arp.strip().split("\n")
                if len(lines) > 1:
                    for line in lines[1:]:
                        parts = line.split()
                        if len(parts) >= 4:
                            mac = parts[3].lower()
                            if not mac or mac == "00:00:00:00:00:00":
                                continue
                            if mac not in devices:
                                devices[mac] = ConnectedDevice(
                                    mac=mac,
                                    ip=parts[0],
                                    connected=False,  # Neighbors alone might be stale
                                    is_wireless=False,
                                    connection_type="wired",
                                )
        except (
            LuciRpcTimeoutError,
            LuciRpcConnectionError,
            LuciRpcSslError,
            LuciRpcAuthError,
        ):
            raise
        except LuciRpcError:
            pass

        # 3. Wireless Clients (iwinfo station dump)
        try:
            # Get wireless interfaces first
            iw_out = await self.execute_command(
                "iwinfo 2>/dev/null | grep -E '^[a-z0-9_-]+' | awk '{print $1}'"
            )
            if iw_out:
                ifaces = iw_out.strip().split()
                for iface in ifaces:
                    assoc_str = await self.execute_command(
                        f'ubus call iwinfo assoclist \'{{"device":"{iface}"}}\' 2>/dev/null'
                    )
                    if assoc_str and assoc_str.strip().startswith("{"):
                        try:
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
                                dev.interface = iface
                                dev.signal = client.get("signal", 0)
                                dev.noise = client.get("noise", 0)
                                dev.rx_rate = self._get_assoc_rate(client, "rx")
                                dev.tx_rate = self._get_assoc_rate(client, "tx")

                                # Set connection type based on interface name
                                if "5g" in iface.lower():
                                    dev.connection_type = "5GHz"
                                elif "2g" in iface.lower():
                                    dev.connection_type = "2.4GHz"
                                else:
                                    dev.connection_type = "wireless"
                        except Exception:
                            pass
        except (
            LuciRpcTimeoutError,
            LuciRpcConnectionError,
            LuciRpcSslError,
            LuciRpcAuthError,
        ):
            raise
        except LuciRpcError:
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

        # 4. Fallback: Discovery of all hostapd objects
        if self.packages.wireless is not False:
            try:
                cmd = "for obj in $(ubus list 'hostapd.*'); do echo \"$obj $(ubus call $obj get_clients 2>/dev/null)\"; done"
                stdout = await self.execute_command(cmd)
                if stdout:
                    for line in stdout.splitlines():
                        if not line.strip():
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
                                        mac = mac.lower()
                                        if mac in devices:
                                            dev = devices[mac]
                                        else:
                                            dev = ConnectedDevice(mac=mac, connected=False)
                                            devices[mac] = dev

                                            dev.connected = True  # Wireless association

                                        dev.is_wireless = True
                                        # Map system interface name to UCI section if possible
                                        dev.interface = getattr(self, "_sys_to_uci", {}).get(
                                            iface_name,
                                            iface_name,
                                        )
                                        if not dev.signal:
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

                                        if "5g" in iface_name.lower():
                                            dev.connection_type = "5GHz"
                                        elif "2g" in iface_name.lower():
                                            dev.connection_type = "2.4GHz"
                                        elif not dev.connection_type:
                                            dev.connection_type = "wireless"
                        except (
                            json.JSONDecodeError,
                            KeyError,
                        ):
                            continue
            except LuciRpcError:
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

        # 5. Final refinement from IP neighbors (for states)
        try:
            active_states = ["REACHABLE", "DELAY", "PROBE", "PERMANENT"]
            if self.trust_stale_arp:
                active_states.append("STALE")
            neighbors = await self.get_ip_neighbors()
            for neigh in neighbors:
                mac = neigh.mac.lower()
                if mac in devices:
                    dev = devices[mac]

                    # Neighbors alone might be stale.
                    # For wireless devices, we only trust wireless association (Step 3/4).
                    # For wired devices (or unknown), we trust the neighbor state if enabled.
                    if not dev.is_wireless and neigh.state.upper() in active_states:
                        dev.connected = True

                    if not dev.neighbor_state:
                        dev.neighbor_state = neigh.state
                    if not dev.interface:
                        dev.interface = neigh.interface
                else:
                    is_active = neigh.state.upper() in active_states
                    devices[mac] = ConnectedDevice(
                        mac=mac,
                        ip=neigh.ip,
                        interface=neigh.interface,
                        is_wireless=False,
                        connected=is_active,
                        connection_type="wired",
                        neighbor_state=neigh.state,
                    )
        except (
            LuciRpcTimeoutError,
            LuciRpcConnectionError,
            LuciRpcSslError,
            LuciRpcAuthError,
        ):
            raise
        except Exception:
            pass

        # 5. Supplemental source: Bridge FDB (Forwarding Database)
        if self.trust_bridge_fdb:
            await self._process_bridge_fdb(devices)

        return list(devices.values())

    async def _process_bridge_fdb(self, devices: dict[str, ConnectedDevice]) -> None:
        """Fetch and merge bridge FDB (forwarding database) information via LuCI RPC."""
        try:
            # 1. Fetch all network devices
            dev_status_str = await self.execute_command(
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
                    fdb_str = await self.execute_command(
                        f'ubus call network.device fdb \'{{"name":"{dev_name}"}}\' 2>/dev/null'
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
                except (
                    LuciRpcTimeoutError,
                    LuciRpcConnectionError,
                    LuciRpcSslError,
                    LuciRpcAuthError,
                ):
                    raise
                except Exception:
                    continue
        except (
            LuciRpcTimeoutError,
            LuciRpcConnectionError,
            LuciRpcSslError,
            LuciRpcAuthError,
        ):
            raise
        except Exception as err:
            _LOGGER.debug("Failed to fetch bridge FDB via LuCI RPC: %s", err)

    async def kick_device(self, mac_address: str, interface: str) -> bool:
        """Kick a device, mapping UCI section back to system name if needed."""
        sys_iface = getattr(self, "_uci_to_sys", {}).get(interface, interface)
        try:
            await self._rpc_call(
                "ubus",
                "call",
                [
                    f"hostapd.{sys_iface}",
                    "del_client",
                    {
                        "addr": mac_address,
                        "reason": 5,
                        "deauth": True,
                        "ban_time": 60000,
                    },
                ],
            )
            return True
        except Exception as err:
            _LOGGER.debug(
                "Failed to kick device via LuCI RPC ubus call: %s. Trying fallback.",
                err,
            )
            return await super().kick_device(mac_address, sys_iface)

    async def get_dhcp_leases(self) -> list[DhcpLease]:
        """Get DHCP leases via LuCI RPC."""
        if self.dhcp_software == "none":
            return []

        leases: list[DhcpLease] = []

        # Try odhcpd via ubus call over sys.exec if enabled
        if self.dhcp_software in ("auto", "odhcpd") and self.packages.dhcp is not False:
            try:
                stdout = await self._rpc_call(
                    "sys",
                    "exec",
                    ["ubus call dhcp ipv4leases 2>/dev/null"],
                )
                if stdout and stdout.strip().startswith("{"):
                    data = json.loads(stdout)
                    if data and isinstance(data, dict):
                        for lease_data in data.get("dhcp_leases", []):
                            leases.append(
                                DhcpLease(
                                    hostname=lease_data.get("hostname", ""),
                                    mac=lease_data.get("mac", "").lower(),
                                    ip=lease_data.get("ipaddr", ""),
                                    expires=lease_data.get("expires", 0),
                                ),
                            )
                    if leases and self.dhcp_software == "odhcpd":
                        return leases
            except Exception:  # noqa: BLE001
                if self.dhcp_software == "odhcpd":
                    _LOGGER.debug(
                        "Requested odhcpd but 'ubus call dhcp' failed via LuCI RPC",
                    )
                    return []

        # Parse dnsmasq leases from /tmp/dhcp.leases
        if (
            self.dhcp_software in ("auto", "dnsmasq")
            and self.packages.dhcp is not False
        ):
            try:
                leases_str = await self._rpc_call(
                    "sys",
                    "exec",
                    ["cat /tmp/dhcp.leases 2>/dev/null"],
                )
                if leases_str:
                    for line in leases_str.strip().split("\n"):
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
            except LuciRpcError:
                if self.dhcp_software == "dnsmasq":
                    _LOGGER.debug(
                        "Requested dnsmasq but cat /tmp/dhcp.leases failed via LuCI RPC",
                    )

        return leases

    async def get_local_macs(self) -> set[str]:
        """Get all MAC addresses belonging to the router's physical and virtual interfaces."""
        macs = set()
        try:
            status_str = await self._rpc_call(
                "sys",
                "exec",
                ["ubus call network.device status 2>/dev/null"],
            )
            if status_str and status_str.strip().startswith("{"):
                status = json.loads(status_str)
                if status and isinstance(status, dict):
                    for dev_info in status.values():
                        if isinstance(dev_info, dict) and (
                            mac := dev_info.get("macaddr")
                        ):
                            macs.add(mac.lower())
        except Exception:  # noqa: BLE001
            pass
        return macs

    async def get_local_ips(self) -> set[str]:
        """Get all IP addresses belonging to the router."""
        ips = set()
        try:
            dump_str = await self._rpc_call(
                "sys",
                "exec",
                ["ubus call network.interface dump 2>/dev/null"],
            )
            if dump_str and dump_str.strip().startswith("{"):
                dump = json.loads(dump_str)
                if (
                    dump
                    and isinstance(dump, dict)
                    and (ifaces := dump.get("interface"))
                ):
                    for iface in ifaces:
                        if not isinstance(iface, dict):
                            continue
                        # IPv4
                        for addr in iface.get("ipv4-address", []):
                            if (
                                isinstance(addr, dict)
                                and (address := addr.get("address"))
                                and address not in ips
                            ):
                                ips.add(address)
                        # IPv6
                        for addr in iface.get("ipv6-address", []):
                            if (
                                isinstance(addr, dict)
                                and (address := addr.get("address"))
                                and address not in ips
                            ):
                                ips.add(address)
        except Exception:  # noqa: BLE001
            pass
        return ips
