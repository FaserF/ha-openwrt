# mypy: disable-error-code="attr-defined,has-type"
"""Aggregator mixin for collecting all OpenWrt device data."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from ..models import OpenWrtData

_LOGGER = logging.getLogger(__name__)


class OpenWrtAggregatorMixin:
    """Mixin for aggregating data across all OpenWrt subsystems."""

    async def get_all_data(self, is_full_poll: bool = False) -> OpenWrtData:
        """Fetch all data from OpenWrt in parallel blocks with robust fallbacks."""

        now = time.time()
        slow_generation = self._slow_poll_generation

        # Multi-Interval-Polling:
        # - Fast-poll: CPU/RAM/Uptime/Load, Active Interfaces, Network traffic rates, Connected clients/wireless, DHCP Leases. (Every cycle)
        # - Medium-poll: IP neighbors, MWAN, Latency, External IP, VPN, UPnP, AdBlock, banIP, etc. (Every 3 cycles or ~3 minutes)
        # - Slow-poll: Device info, system logs, packages lists, firewall, sqm, wireguard, services, etc. (Every 30 cycles or ~15-30 minutes)

        is_slow_poll = (
            is_full_poll
            or (self._cached_device_info is None)
            or (now - self._last_slow_poll_time >= 900.0)
        )
        is_medium_poll = is_slow_poll or (now - self._last_medium_poll_time >= 180.0)

        self._poll_count += 1
        data = (self.coordinator.data if self.coordinator else None) or OpenWrtData()

        def get_val(res: Any, default: Any, name: str = "") -> Any:
            """Safely get value from gather result, keeping previous data on failure."""
            if isinstance(res, Exception):
                if name:
                    _LOGGER.debug("Fetch of %s failed: %s", name, res)
                return default
            if res is None:
                return default
            # Stickiness for critical lists: if we previously had data and now it's empty,
            # retain the old data for a few cycles to avoid entities going unavailable
            # during temporary router boot states or command glitches.
            if (
                name in ("network_interfaces", "SQM")
                and isinstance(res, list)
                and default
            ):
                if name == "network_interfaces":
                    new_names = {i.name for i in res}
                    for old_iface in default:
                        if old_iface.name not in new_names:
                            old_iface.up = False
                            res.append(old_iface)
                elif name == "SQM":
                    new_ids = {s.section_id for s in res}
                    for old_sqm in default:
                        if old_sqm.section_id not in new_ids:
                            res.append(old_sqm)

                if not res:
                    _LOGGER.debug(
                        "Fetch of %s returned empty result, retaining previous data",
                        name,
                    )
                    return default
            return res

        # 1. Fast-changing Core data - Essential for basic functionality, trackers, and rates
        core_tasks = [
            self.get_system_resources(),
            self.get_network_interfaces(),
            self.get_connected_devices(),
            self.get_local_macs(),
            self.get_local_ips(),
        ]

        # Add fast dynamic tasks (wireless, dhcp) to core_tasks
        core_dynamic_tasks: dict[str, Any] = {}
        if data.packages.wireless is not False:
            core_dynamic_tasks["wireless"] = self.get_wireless_interfaces()
        if data.packages.dhcp is not False:
            core_dynamic_tasks["dhcp"] = self.get_dhcp_leases()
        if data.packages.mwan3 is not False:
            # Fast tier on purpose: the MWAN3 counters bound both the
            # accuracy of the online ratio and how quickly the mwan_online
            # binary sensor reacts to an outage. One extra ubus call per
            # cycle is negligible against a local router.
            core_dynamic_tasks["mwan"] = self.get_mwan_status()

        core_dyn_keys = list(core_dynamic_tasks.keys())

        core_results = await asyncio.gather(
            *core_tasks,
            *[core_dynamic_tasks[k] for k in core_dyn_keys],
            return_exceptions=True,
        )

        data.system_resources = get_val(
            core_results[0], data.system_resources, "system_resources"
        )
        await self._fetch_conntrack(data.system_resources)
        data.network_interfaces = get_val(
            core_results[1], data.network_interfaces, "network_interfaces"
        )
        data.connected_devices = get_val(
            core_results[2], data.connected_devices, "connected_devices"
        )
        data.local_macs = get_val(core_results[3], data.local_macs, "local_macs")
        data.local_ips = get_val(core_results[4], data.local_ips, "local_ips")

        core_dyn_offset = len(core_tasks)
        core_dyn_results = dict(
            zip(core_dyn_keys, core_results[core_dyn_offset:], strict=False)
        )

        if "wireless" in core_dyn_results:
            data.wireless_interfaces = get_val(
                core_dyn_results["wireless"], data.wireless_interfaces, "wireless"
            )
        if "dhcp" in core_dyn_results:
            data.dhcp_leases = get_val(
                core_dyn_results["dhcp"], data.dhcp_leases, "DHCP"
            )
        if "mwan" in core_dyn_results:
            mwan_result = core_dyn_results["mwan"]
            if isinstance(mwan_result, tuple) and len(mwan_result) == 2:
                data.mwan_status, data.mwan_policies = mwan_result
            elif isinstance(mwan_result, Exception):
                _LOGGER.debug("Failed to get mwan status: %s", mwan_result)
            else:
                _LOGGER.debug(
                    "Unexpected return type from get_mwan_status: %s", type(mwan_result)
                )

        # 2. Slow-changing optional data (Slow Poll) - Reduces router load
        if is_slow_poll:
            slow_optional_tasks = {
                "device_info": self.get_device_info(),
                "services": self.get_services(),
                "leds": self.get_leds(),
                "firewall_redirects": self.get_firewall_redirects(),
                "firewall_rules": self.get_firewall_rules(),
                "access_control": self.get_access_control(),
                "sqm": self.get_sqm_status(),
                "packages": self.check_packages(),
                "permissions": self.check_permissions(),
                "reboot_required": self.is_reboot_required(),
                "system_logs": self.get_system_logs(count=10),
                "dmesg_logs": self.get_dmesg_logs(count=50),
                "upgradeable_packages": self.get_upgradeable_packages(),
            }

            keys = list(slow_optional_tasks.keys())
            slow_results = await asyncio.gather(
                *[slow_optional_tasks[k] for k in keys], return_exceptions=True
            )
            slow_map = dict(zip(keys, slow_results, strict=False))

            data.device_info = get_val(
                slow_map["device_info"], data.device_info, "device_info"
            )
            data.services = get_val(slow_map["services"], data.services, "services")
            data.leds = get_val(slow_map["leds"], data.leds, "LEDs")
            data.firewall_redirects = get_val(
                slow_map["firewall_redirects"],
                data.firewall_redirects,
                "firewall redirects",
            )
            data.firewall_rules = get_val(
                slow_map["firewall_rules"], data.firewall_rules, "firewall rules"
            )
            data.access_control = get_val(
                slow_map["access_control"], data.access_control, "access control"
            )
            data.sqm = get_val(slow_map["sqm"], data.sqm, "SQM")
            data.packages = get_val(slow_map["packages"], data.packages, "packages")
            data.permissions = get_val(
                slow_map["permissions"], data.permissions, "permissions"
            )
            data.reboot_required = get_val(
                slow_map["reboot_required"], data.reboot_required, "reboot required"
            )
            data.system_logs = get_val(
                slow_map["system_logs"], data.system_logs, "system logs"
            )
            data.dmesg_logs = get_val(
                slow_map["dmesg_logs"], data.dmesg_logs, "dmesg logs"
            )
            data.upgradeable_packages = get_val(
                slow_map["upgradeable_packages"],
                data.upgradeable_packages,
                "upgradeable packages",
            )

            self._cached_device_info = data.device_info
            self._cached_slow_data = {
                k: data.__dict__.get(k)
                for k in [
                    "services",
                    "leds",
                    "firewall_redirects",
                    "firewall_rules",
                    "access_control",
                    "sqm",
                    "packages",
                    "permissions",
                    "reboot_required",
                    "system_logs",
                    "dmesg_logs",
                    "upgradeable_packages",
                ]
            }
            # An action during this poll may have changed what was just read.
            if self._slow_poll_generation == slow_generation:
                self._last_slow_poll_time = now
        else:
            # Reuse cached data on fast/medium polls
            if self._cached_device_info:
                data.device_info = self._cached_device_info

            cached = getattr(self, "_cached_slow_data", {})
            for k, v in cached.items():
                if hasattr(data, k) and v is not None:
                    setattr(data, k, v)

        # 3. Medium-changing dynamic data
        if is_medium_poll:
            medium_tasks = {
                "ip_neighbors": self.get_ip_neighbors(),
                "qmodem": self.get_qmodem_info(),
                "vpn": self.get_vpn_status(),
                "latency": self.get_latency(),
                "external_ip": self.get_external_ip(),
                "gateway_mac": self.get_gateway_mac(),
                "wifi_credentials": self.get_wifi_credentials(),
                "wireguard": self.get_wireguard_interfaces(),
            }

            if data.packages.wireless is not False:
                medium_tasks["wps"] = self.get_wps_status()
            if data.packages.lldp is not False:
                medium_tasks["lldp"] = self.get_lldp_neighbors()
            if data.packages.miniupnpd is not False:
                medium_tasks["upnp"] = self.get_upnp_mappings()
            if data.packages.adblock:
                medium_tasks["adblock"] = self.get_adblock_status()
            if data.packages.simple_adblock:
                medium_tasks["simple_adblock"] = self.get_simple_adblock_status()
            if data.packages.ban_ip:
                medium_tasks["ban_ip"] = self.get_banip_status()
            if data.packages.tailscale:
                medium_tasks["tailscale"] = self.get_tailscale_status()
            if (data.packages.batman_adv or data.packages.batctl) and (
                not self.coordinator or data.permissions.read_batman
            ):
                medium_tasks["batman"] = self.get_batman_data()

            med_keys = list(medium_tasks.keys())
            med_results = await asyncio.gather(
                *[medium_tasks[k] for k in med_keys], return_exceptions=True
            )
            med_map = dict(zip(med_keys, med_results, strict=False))

            data.ip_neighbors = get_val(
                med_map.get("ip_neighbors"), data.ip_neighbors, "IP neighbors"
            )
            data.qmodem_info = get_val(med_map.get("qmodem"), data.qmodem_info, "modem")
            data.vpn_interfaces = get_val(
                med_map.get("vpn"), data.vpn_interfaces, "VPN"
            )
            data.wireguard_interfaces = get_val(
                med_map.get("wireguard"), data.wireguard_interfaces, "wireguard"
            )
            data.latency = get_val(med_map.get("latency"), data.latency, "latency")
            data.external_ip = get_val(
                med_map.get("external_ip"), data.external_ip, "external IP"
            )
            if data.device_info:
                data.device_info.gateway_mac = get_val(
                    med_map.get("gateway_mac"),
                    data.device_info.gateway_mac,
                    "gateway MAC",
                )
            data.wifi_credentials = get_val(
                med_map.get("wifi_credentials"),
                data.wifi_credentials,
                "WiFi credentials",
            )

            if "wps" in med_map:
                data.wps_status = get_val(med_map["wps"], data.wps_status, "WPS")
            if "lldp" in med_map:
                data.lldp_neighbors = get_val(
                    med_map["lldp"], data.lldp_neighbors, "LLDP"
                )
            if "upnp" in med_map:
                data.upnp_mappings = get_val(
                    med_map["upnp"], data.upnp_mappings, "UPnP"
                )
            if "adblock" in med_map:
                data.adblock = get_val(med_map["adblock"], data.adblock, "adblock")
            if "simple_adblock" in med_map:
                data.simple_adblock = get_val(
                    med_map["simple_adblock"], data.simple_adblock, "simple adblock"
                )
            if "ban_ip" in med_map:
                data.ban_ip = get_val(med_map["ban_ip"], data.ban_ip, "ban-ip")
            if "tailscale" in med_map:
                data.tailscale = get_val(
                    med_map["tailscale"], data.tailscale, "tailscale"
                )
            elif data.packages.tailscale is False:
                # Uninstalled: drop old status instead of keeping it forever.
                data.tailscale = None
            if "batman" in med_map:
                batman = get_val(med_map["batman"], None, "batman")
                if batman:
                    data.batman_originators = batman.get("originators", [])
                    data.batman_neighbors = batman.get("neighbors", [])
                    data.batman_gateways = batman.get("gateways", [])
                    data.batman_translation_table = batman.get("translation_table", {})
                    data.batman_mesh_active = batman.get("mesh_active", False)
                    data.batman_tables_supported = batman.get("tables_supported")

            self._cached_medium_data = {
                k: data.__dict__.get(k)
                for k in [
                    "ip_neighbors",
                    # No mwan_status: it is fetched on every update, and
                    # restoring a cached copy would hide the fresh state.
                    "qmodem_info",
                    "vpn_interfaces",
                    "wireguard_interfaces",
                    "latency",
                    "external_ip",
                    "wifi_credentials",
                    "wps_status",
                    "lldp_neighbors",
                    "upnp_mappings",
                    "adblock",
                    "simple_adblock",
                    "ban_ip",
                    "tailscale",
                    "batman_originators",
                    "batman_neighbors",
                    "batman_gateways",
                    "batman_translation_table",
                    "batman_mesh_active",
                    "batman_tables_supported",
                ]
            }
            if data.device_info:
                self._cached_medium_data["gateway_mac"] = data.device_info.gateway_mac
            self._last_medium_poll_time = now
        else:
            # Reuse cached medium data
            med_cached = getattr(self, "_cached_medium_data", {})
            for k, v in med_cached.items():
                if hasattr(data, k) and v is not None:
                    setattr(data, k, v)
            if (
                "gateway_mac" in med_cached
                and data.device_info
                and med_cached["gateway_mac"]
            ):
                data.device_info.gateway_mac = med_cached["gateway_mac"]

        # Populate MAC address for device info if missing
        if data.device_info and not data.device_info.mac_address:
            # Try br-lan first, then eth0, then anything with a valid MAC
            mac_map = {
                iface.name: iface.mac_address
                for iface in data.network_interfaces
                if iface.mac_address and iface.mac_address != "00:00:00:00:00:00"
            }
            if "br-lan" in mac_map:
                data.device_info.mac_address = mac_map["br-lan"]
            elif "eth0" in mac_map:
                data.device_info.mac_address = mac_map["eth0"]
            elif mac_map:
                # Pick the first non-zero MAC
                data.device_info.mac_address = next(iter(mac_map.values()))

        return data
