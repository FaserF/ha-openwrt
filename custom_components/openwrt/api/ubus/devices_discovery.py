# mypy: disable-error-code="attr-defined,has-type"
"""Ubus device discovery and association mixin."""

from __future__ import annotations

import logging
from typing import Any

from ..base import ConnectedDevice
from .exceptions import (
    UbusAuthError,
    UbusConnectionError,
    UbusError,
    UbusPermissionError,
    UbusSslError,
    UbusTimeoutError,
)

_LOGGER = logging.getLogger(__name__)


class UbusDevicesDiscoveryMixin:
    """Mixin for Ubus device discovery and wireless/bridge association."""

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

            # Track whether at least one FDB query succeeded and which wired MACs appear in any FDB entry.
            # Used to resolve STALE-ARP-only devices when trust_bridge_fdb is set.
            fdb_query_succeeded = False
            fdb_seen_macs: set[str] = set()

            # 2. For each device, fetch its FDB if it's a bridge or has members
            for dev_name, dev_info in device_status.items():
                if not dev_info.get("up"):
                    continue

                try:
                    fdb = await self._call("network.device", "fdb", {"name": dev_name})
                    fdb_query_succeeded = True
                    if fdb and isinstance(fdb, list):
                        for entry in fdb:
                            mac = entry.get("mac", "").lower()
                            if mac not in devices:
                                continue

                            dev = devices[mac]
                            # Only apply to wired devices or as supplemental info
                            port = entry.get("port", "")
                            if port:
                                fdb_seen_macs.add(mac)
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

            # 3. When trust_bridge_fdb is enabled, a wired device whose only evidence
            # of presence is a STALE ARP entry (not confirmed by any FDB) should be
            # treated as disconnected, provided the FDB could actually be queried.
            # If network.device fdb is unsupported (e.g. OpenWrt 25.x) or failed,
            # we must not mark devices away.
            if self.trust_stale_arp and fdb_query_succeeded:
                for mac, dev in devices.items():
                    if (
                        not dev.is_wireless
                        and dev.connected
                        and dev.neighbor_state
                        and dev.neighbor_state.upper() == "STALE"
                        and mac not in fdb_seen_macs
                    ):
                        _LOGGER.debug(
                            "Wired device %s has STALE ARP but is absent from bridge FDB;"
                            " marking disconnected",
                            mac,
                        )
                        dev.connected = False

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
                    if (
                        self.coordinator
                        and self.coordinator.data
                        and self.coordinator.data.all_connected_devices
                    ):
                        for prev_dev in self.coordinator.data.all_connected_devices:
                            if (
                                prev_dev.is_wireless
                                and prev_dev.connected
                                and prev_dev.interface == ifname
                            ):
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
                                    ),
                                )
                                dev.connected = True
                                dev.is_wireless = True
                                dev.interface = ifname
                                dev.connection_type = (
                                    prev_dev.connection_type or dev.connection_type
                                )
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
        self,
        devices: dict[str, ConnectedDevice],
        scanned_candidates: set[str] | None = None,
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
                            if scanned_candidates is not None:
                                scanned_candidates.add(ifname)
                    except (
                        UbusTimeoutError,
                        UbusConnectionError,
                        UbusSslError,
                        UbusPermissionError,
                        UbusAuthError,
                    ):
                        raise
                    except UbusError:
                        if (
                            (
                                scanned_candidates is None
                                or ifname not in scanned_candidates
                            )
                            and self.coordinator
                            and self.coordinator.data
                            and self.coordinator.data.all_connected_devices
                        ):
                            for prev_dev in self.coordinator.data.all_connected_devices:
                                if (
                                    prev_dev.is_wireless
                                    and prev_dev.connected
                                    and prev_dev.interface == ifname
                                ):
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
                                        ),
                                    )
                                    dev.connected = True
                                    dev.is_wireless = True
                                    dev.interface = ifname
                                    dev.connection_type = (
                                        prev_dev.connection_type or dev.connection_type
                                    )
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

            if not dev.rx_rate:
                dev.rx_rate = self._get_assoc_rate(client_data, "rx")
            if not dev.tx_rate:
                dev.tx_rate = self._get_assoc_rate(client_data, "tx")

    def _set_wireless_connection_type(self, dev: ConnectedDevice, ifname: str) -> None:
        """Determine specific wireless band from interface name."""
        if not dev.connection_type or dev.connection_type == "wired":
            dev.connection_type = "wireless"
            if "5g" in ifname.lower():
                dev.connection_type = "5GHz"
            elif "2g" in ifname.lower():
                dev.connection_type = "2.4GHz"
