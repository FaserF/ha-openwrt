# mypy: disable-error-code="attr-defined,has-type"
"""VPN, modem, and service status mixin for OpenWrt."""

from __future__ import annotations

import json
import logging
import re
import shlex
from typing import Any

from ..models import (
    BanIpStatus,
    LatencyResult,
    QModemInfo,
    TailscaleStatus,
    VpnInterface,
)

_LOGGER = logging.getLogger(__name__)


class OpenWrtStatusMixin:
    """Mixin for VPN, modem, and service status operations."""

    async def _flush_conntrack_for_mac(self, mac: str) -> bool:
        """Drop already-established connections for a MAC's current IP(s).

        A new firewall REJECT/DROP rule only stops *new* connection attempts.
        Any sockets that were already open before the rule was applied are
        already tracked by the kernel's connection tracker and will keep
        flowing until they time out on their own. Explicitly flushing those
        conntrack entries (equivalent to ``conntrack -D -s <ip>``) forces the
        block to take effect immediately instead of waiting for existing
        streams to finish.

        All outcomes are logged at INFO/WARNING (not DEBUG) with the
        "[openwrt-conntrack-flush]" tag so this is easy to find and confirm
        in the Home Assistant log, since a silently-swallowed failure here is
        indistinguishable from "nothing to flush" otherwise.

        Returns True if all flushed successfully or no IPs found, False on error.
        """
        mac_lower = mac.lower()
        try:
            neighbors = await self.get_ip_neighbors()
        except Exception as err:
            _LOGGER.warning(
                "[openwrt-conntrack-flush] Could not read the ARP/neighbor "
                "table to resolve an IP for %s: %s",
                mac,
                err,
            )
            return False

        ips = {n.ip for n in neighbors if n.mac.lower() == mac_lower and n.ip}
        if not ips:
            _LOGGER.warning(
                "[openwrt-conntrack-flush] No IP address found for %s in the "
                "ARP/neighbor table; skipping conntrack flush (device may be "
                "idle, or the table entry is stale)",
                mac,
            )
            return True

        overall_success = True
        for ip in ips:
            safe_ip = shlex.quote(ip)
            try:
                output = await self.execute_command(
                    f"conntrack -D -s {safe_ip}; echo RC=$?"
                )
            except Exception as err:
                _LOGGER.warning(
                    "[openwrt-conntrack-flush] Failed to run conntrack for %s (%s): %s",
                    mac,
                    ip,
                    err,
                )
                overall_success = False
                continue

            if "RC=0" not in (output or ""):
                _LOGGER.warning(
                    "[openwrt-conntrack-flush] Failed to flush conntrack for %s (%s): %s",
                    mac,
                    ip,
                    output,
                )
                overall_success = False
                continue

            summary = [
                line
                for line in (output or "").strip().splitlines()
                if not line.startswith("RC=")
            ]
            last_line = (
                summary[-1] if summary else "no output (is conntrack-tools installed?)"
            )
            _LOGGER.info(
                "[openwrt-conntrack-flush] %s (%s): %s",
                mac,
                ip,
                last_line,
            )

        return overall_success

    async def get_vpn_status(self) -> list[VpnInterface]:
        """Get VPN tunnel status (WireGuard/OpenVPN)."""
        vpn_interfaces: list[VpnInterface] = []
        try:
            # Try WireGuard first
            output = await self.execute_command("wg show all dump 2>/dev/null")
            if output and "not found" not in output.lower():
                current_iface = ""
                for line in output.strip().splitlines():
                    parts = line.split("\t")
                    if len(parts) >= 4:
                        iface_name = parts[0]
                        if iface_name != current_iface:
                            current_iface = iface_name
                            # First line per interface is the interface itself
                            vpn = VpnInterface(
                                name=iface_name,
                                type="wireguard",
                            )
                            # Check if interface is up
                            ip_out = await self.execute_command(
                                f"ip link show {iface_name} 2>/dev/null",
                            )
                            vpn.up = bool(ip_out and "UP" in ip_out)

                            # Get RX/TX bytes
                            rx_out = await self.execute_command(
                                f"cat /sys/class/net/{iface_name}/statistics/rx_bytes 2>/dev/null",
                            )
                            tx_out = await self.execute_command(
                                f"cat /sys/class/net/{iface_name}/statistics/tx_bytes 2>/dev/null",
                            )
                            try:
                                vpn.rx_bytes = (
                                    int(rx_out.strip())
                                    if rx_out and rx_out.strip().isdigit()
                                    else 0
                                )
                                vpn.tx_bytes = (
                                    int(tx_out.strip())
                                    if tx_out and tx_out.strip().isdigit()
                                    else 0
                                )
                            except (
                                ValueError,
                                AttributeError,
                            ):
                                pass

                            vpn_interfaces.append(vpn)
                        else:
                            # Subsequent lines are peers
                            for vpn in vpn_interfaces:
                                if vpn.name == current_iface:
                                    vpn.peers += 1
                                    # parts[5] = latest-handshake
                                    if len(parts) > 5 and parts[5].isdigit():
                                        handshake = int(parts[5])
                                        vpn.latest_handshake = max(
                                            vpn.latest_handshake, handshake
                                        )
                                    break
        except Exception as err:
            _LOGGER.debug("WireGuard status check failed: %s", err)

        try:
            # Try OpenVPN
            output = await self.execute_command("pgrep -a openvpn 2>/dev/null")
            if output and "not found" not in output.lower() and output.strip():
                # OpenVPN is running – check interfaces
                tun_output = await self.execute_command(
                    "ip -br link show type tun 2>/dev/null",
                )
                if tun_output:
                    for line in tun_output.strip().splitlines():
                        parts = line.split()
                        if len(parts) >= 2:
                            iface_name = parts[0]
                            if iface_name.startswith("tailscale"):
                                # Tailscale's TUN device is not an OpenVPN tunnel
                                continue
                            state = parts[1]
                            vpn = VpnInterface(
                                name=iface_name,
                                type="openvpn",
                                up=state == "UP",
                            )
                            # Get RX/TX bytes
                            rx_out = await self.execute_command(
                                f"cat /sys/class/net/{iface_name}/statistics/rx_bytes 2>/dev/null",
                            )
                            tx_out = await self.execute_command(
                                f"cat /sys/class/net/{iface_name}/statistics/tx_bytes 2>/dev/null",
                            )
                            try:
                                vpn.rx_bytes = (
                                    int(rx_out.strip())
                                    if rx_out and rx_out.strip().isdigit()
                                    else 0
                                )
                                vpn.tx_bytes = (
                                    int(tx_out.strip())
                                    if tx_out and tx_out.strip().isdigit()
                                    else 0
                                )
                            except (
                                ValueError,
                                AttributeError,
                            ):
                                pass
                            vpn_interfaces.append(vpn)
        except Exception as err:
            _LOGGER.debug("OpenVPN status check failed: %s", err)

        return vpn_interfaces

    async def get_tailscale_status(self) -> TailscaleStatus | None:
        """Get Tailscale status; None means unknown (keep previous data)."""
        from ..tailscale import TAILSCALE_STATUS_COMMAND, parse_tailscale_output

        raw = await self.execute_command(TAILSCALE_STATUS_COMMAND)
        return parse_tailscale_output(raw)

    async def get_banip_status(self) -> BanIpStatus:
        """Get banIP status and runtime block counters."""
        status = BanIpStatus()

        try:
            res = await self.file_exec("/etc/init.d/banip", ["enabled"])
            if isinstance(res, dict) and "code" in res:
                status.enabled = res.get("code") == 0
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("banip enabled probe failed: %s", err)
        status.status = "enabled" if status.enabled else "disabled"

        # Element count + packet-block counters from banIP's JSON report.
        try:
            res = await self.file_exec("/etc/init.d/banip", ["report", "json"])
            out = res.get("stdout", "") if isinstance(res, dict) else ""
            if out:
                payload = json.loads(out)
                summary = (
                    payload[0] if isinstance(payload, list) and payload else payload
                )
                if isinstance(summary, dict):

                    def _n(key: str) -> int:
                        try:
                            return int(str(summary.get(key, "0")).strip() or "0")
                        except (ValueError, TypeError):
                            return 0

                    status.banned_ips = _n("sum_cntelements")
                    status.blocked_inbound = _n("sum_setinbound")
                    status.blocked_outbound = _n("sum_setoutbound")
                    status.block_stats = {
                        "inbound": status.blocked_inbound,
                        "outbound": status.blocked_outbound,
                        "syn_flood": _n("sum_synflood"),
                        "udp_flood": _n("sum_udpflood"),
                        "icmp_flood": _n("sum_icmpflood"),
                        "ct_invalid": _n("sum_ctinvalid"),
                        "tcp_invalid": _n("sum_tcpinvalid"),
                        "bcp38": _n("sum_bcp38"),
                        "autoadd_block": _n("autoadd_block"),
                    }
                    status.blocked_packets = sum(
                        v for k, v in status.block_stats.items() if k != "autoadd_block"
                    )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("banip report failed: %s", err)
        return status

    async def get_latency(self, target: str = "8.8.8.8") -> LatencyResult | None:
        """Measure network latency via ping."""
        try:
            output = await self.execute_command(f"ping -c 3 -W 2 {target}")
            if not output:
                _LOGGER.debug("Ping command returned no output for %s", target)
                return None

            result = LatencyResult(target=target)
            # We got some output, so the command itself is available
            result.available = True
            _LOGGER.debug("Ping output for %s: %s", target, output)

            # Parse avg from "min/avg/max/mdev = x/y/z/w ms" or similar
            # We use a regex that looks for the slash-separated numbers
            stats_match = re.search(
                r"(\d+\.\d+)/(\d+\.\d+)/(\d+\.\d+)/(\d+\.\d+)", output
            )
            if stats_match:
                # stats_match.group(2) is avg
                result.latency_ms = round(float(stats_match.group(2)), 1)
            else:
                # Fallback for simpler ping versions: "round-trip min/avg/max = 1.2/3.4/5.6 ms"
                # or even "1 packets transmitted, 1 packets received, 0% packet loss"
                stats_match = re.search(r"=\s*([0-9.]+)/([0-9.]+)/([0-9.]+)", output)
                if stats_match:
                    result.latency_ms = round(float(stats_match.group(2)), 1)

            # Parse packet loss: "0% packet loss"
            loss_match = re.search(r"(\d+)%\s*packet\s*loss", output, re.IGNORECASE)
            if loss_match:
                result.packet_loss = float(loss_match.group(1))

            if result.latency_ms is None:
                return None

            return result
        except Exception as err:
            _LOGGER.debug("Latency check failed for %s: %s", target, err)
            return None

    async def get_qmodem_info(self) -> QModemInfo:
        """Get cellular modem status from QModem's modem_ctrl ubus subsystem (if available)."""
        info = QModemInfo()
        try:
            output = await self.execute_command("ubus call modem_ctrl info")
            if not output or "Not found" in output or "Method not found" in output:
                return info

            try:
                data = json.loads(output)
            except json.JSONDecodeError:
                return info

            info_list = data.get("info", [])
            if not info_list:
                return info

            info.enabled = True

            for info_item in info_list:
                modem_info_list = info_item.get("modem_info", [])

                current_context = None
                lte_signals = {}
                nr5g_signals = {}

                for item in modem_info_list:
                    class_origin = item.get("class_origin", "")
                    item_key = item.get("key", "")
                    value = item.get("value", "")

                    if item_key == "LTE":
                        current_context = "LTE"
                    elif item_key.startswith("NR"):
                        current_context = "NR5G"

                    if class_origin == "Base Information":
                        if item_key == "manufacturer":
                            info.manufacturer = str(value) if value else ""
                        elif item_key == "revision":
                            info.revision = str(value) if value else ""
                        elif item_key == "temperature":
                            match = re.search(r"(\d+)", str(value))
                            info.temperature = int(match.group(1)) if match else None
                        elif item_key == "voltage":
                            match = re.search(r"(\d+)", str(value))
                            info.voltage = int(match.group(1)) if match else None
                        elif item_key == "connect_status":
                            info.connect_status = str(value) if value else ""
                    elif class_origin == "SIM Information":
                        if item_key == "SIM Status":
                            info.sim_status = (
                                str(value).replace("\n", " ").strip() if value else ""
                            )
                        elif item_key == "ISP":
                            info.isp = (
                                str(value).replace("\n", " ").strip() if value else ""
                            )
                        elif item_key == "SIM Slot":
                            info.sim_slot = (
                                str(value).replace("\n", " ").strip() if value else ""
                            )

                    elif class_origin == "Cell Information":
                        if current_context == "LTE":
                            lte_signals[item_key] = value
                        elif current_context == "NR5G":
                            nr5g_signals[item_key] = value

                def extract_int(val: Any, pattern: str = r"(-?\d+)") -> int | None:
                    match = re.search(pattern, str(val))
                    return int(match.group(1)) if match else None

                if "RSRP" in lte_signals:
                    info.lte_rsrp = extract_int(lte_signals["RSRP"])
                if "RSRQ" in lte_signals:
                    info.lte_rsrq = extract_int(lte_signals["RSRQ"])
                if "RSSI" in lte_signals:
                    info.lte_rssi = extract_int(lte_signals["RSSI"])
                if "SINR" in lte_signals:
                    info.lte_sinr = extract_int(lte_signals["SINR"], r"(\d+)")

                if "RSRP" in nr5g_signals:
                    info.nr5g_rsrp = extract_int(nr5g_signals["RSRP"])
                if "RSRQ" in nr5g_signals:
                    info.nr5g_rsrq = extract_int(nr5g_signals["RSRQ"])
                if "SINR" in nr5g_signals:
                    info.nr5g_sinr = extract_int(nr5g_signals["SINR"], r"(\d+)")

        except Exception as err:
            _LOGGER.debug("Error retrieving QModem info: %s", err)

        return info
