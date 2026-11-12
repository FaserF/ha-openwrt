# mypy: disable-error-code="attr-defined,has-type"
"""Helper mixin for OpenWrt client parsing and command building."""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import shlex
from typing import TYPE_CHECKING, Any

from ..models import IpNeighbor

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

_LOGGER = logging.getLogger(__name__)


class OpenWrtClientHelpersMixin:
    """Mixin providing parsing and command building helper methods."""

    _logread_flag: str | None = None
    _last_cpu_stats: tuple[int, int] | None = None

    def _get_assoc_rate(self, client: dict[str, Any], direction: str) -> int:
        """Helper to safely extract wireless rate from assoclist/hostapd data."""
        # 1. Check "rate" dict (hostapd format: "rate": {"rx": 866700} in Kbps)
        rate_obj = client.get("rate")
        if isinstance(rate_obj, dict):
            val = rate_obj.get(direction)
            if isinstance(val, (int, float)):
                return int(val)

        # 2. Check "rx"/"tx" dict (iwinfo format: "rx": {"rate": 120100} in Kbps)
        val = client.get(direction)
        if isinstance(val, dict):
            rate_val = val.get("rate")
            if isinstance(rate_val, (int, float)):
                return int(rate_val)
        elif isinstance(val, (int, float)):
            return int(val)

        # 3. Check "rx_rate"/"tx_rate" (hostapd legacy/other format: "rx_rate": 8660 or {"rate": 8660} in tenths of Mbps)
        dir_rate = client.get(f"{direction}_rate")
        if isinstance(dir_rate, dict):
            rate_val = dir_rate.get("rate")
            if isinstance(rate_val, (int, float)):
                return int(rate_val * 100)
        elif isinstance(dir_rate, (int, float)):
            return int(dir_rate * 100)

        return 0

    async def _get_logread_command(self, count: int) -> str:
        """Resolve the correct logread command (detecting -n vs -l)."""
        if self._logread_flag is None:
            # Default to -n as it's the legacy/standard OpenWrt behavior
            self._logread_flag = "-n"
            try:
                # Test which flag is supported by running help
                help_out = await self.execute_command("/sbin/logread --help 2>&1")
                if help_out:
                    # Look for -l (modern BusyBox/OpenWrt 25+)
                    # Use a flexible regex to handle tabs, spaces, and different placeholders
                    if "-l" in help_out:
                        self._logread_flag = "-l"
                        _LOGGER.debug(
                            "Detected logread -l support (modern OpenWrt/BusyBox)"
                        )
                    elif "-n" in help_out:
                        self._logread_flag = "-n"
                        _LOGGER.debug("Confirmed logread -n support (standard OpenWrt)")
                    else:
                        _LOGGER.debug(
                            "Unknown logread help format, falling back to %s",
                            self._logread_flag,
                        )
                else:
                    _LOGGER.debug("logread --help returned no output, using default -n")
            except Exception as err:
                _LOGGER.debug(
                    "Could not verify logread flag support, defaulting to -n: %s", err
                )

        # Use absolute path and ensure count is an integer
        return f"/sbin/logread {self._logread_flag} {int(count or 10)}"

    def _calculate_cpu_usage(self, proc_stat: str) -> float:
        """Calculate CPU usage percentage from /proc/stat.

        Formula:
        Idle = idle + iowait
        NonIdle = user + nice + system + irq + softirq + steal
        Total = Idle + NonIdle

        Percentage = (Total - Idle) / Total
        """
        if not proc_stat or not isinstance(proc_stat, str):
            return 0.0

        # Basic validation: must start with 'cpu ' and have enough parts
        if not proc_stat.startswith("cpu "):
            _LOGGER.debug(
                "Invalid /proc/stat data received for CPU calculation: %s...",
                proc_stat[:50],
            )
            return 0.0

        try:
            # cpu  user nice system idle iowait irq softirq steal guest guest_nice
            line = proc_stat.splitlines()[0]
            parts = line.split()
            if len(parts) < 5:
                return 0.0

            # parts[1] is user, parts[2] is nice, etc.
            user = int(parts[1])
            nice = int(parts[2])
            system = int(parts[3])
            idle = int(parts[4])
            iowait = int(parts[5]) if len(parts) > 5 else 0
            irq = int(parts[6]) if len(parts) > 6 else 0
            softirq = int(parts[7]) if len(parts) > 7 else 0
            steal = int(parts[8]) if len(parts) > 8 else 0

            idle_time = idle + iowait
            non_idle_time = user + nice + system + irq + softirq + steal
            total_time = idle_time + non_idle_time

            if self._last_cpu_stats is None:
                self._last_cpu_stats = (total_time, idle_time)
                return 0.0

            prev_total, prev_idle = self._last_cpu_stats
            self._last_cpu_stats = (total_time, idle_time)

            total_diff = total_time - prev_total
            idle_diff = idle_time - prev_idle

            if total_diff <= 0:
                return 0.0

            cpu_usage = (total_diff - idle_diff) / total_diff
            return round(max(0.0, min(100.0, cpu_usage * 100.0)), 1)

        except (ValueError, IndexError) as err:
            _LOGGER.debug("Error parsing /proc/stat for CPU usage: %s", err)
            return 0.0

    async def _download_file_via_command(
        self,
        remote_path: str,
        local_path: str,
        run: Callable[[str], Awaitable[str]],
    ) -> bool:
        """Download a (binary) file by encoding it on the router in one command.

        The router prints the file's byte count and then the file encoded as
        base64 (ucode's b64enc with its fs module, or a base64/openssl binary
        when one exists) or as hex (busybox hexdump). Nothing is written
        locally unless the decoded size matches the router's count.
        """
        path = shlex.quote(remote_path)
        cmd = (
            f"P={path}; "
            'if [ ! -f "$P" ]; then echo __HA_DL_NOFILE__; '
            "else "
            'echo "__HA_DL_SIZE__$(wc -c < "$P")"; '
            "echo __HA_DL_BEGIN__; "
            '{ P="$P" ucode -l fs -e '
            "'print(b64enc(fs.readfile(getenv(\"P\"))))' 2>/dev/null "
            '|| base64 "$P" 2>/dev/null '
            '|| openssl base64 -in "$P" 2>/dev/null '
            "|| { echo __HA_DL_HEX__; "
            'hexdump -ve \'1/1 "%.2x"\' "$P" 2>/dev/null; } '
            "|| echo __HA_DL_NOENC__; }; "
            "echo; echo __HA_DL_END__; "
            "fi"
        )
        output = await run(cmd)
        data = self._decode_downloaded_file(remote_path, output)
        if data is None:
            return False
        await asyncio.to_thread(self._write_bytes, local_path, data)
        return True

    @staticmethod
    def _decode_downloaded_file(remote_path: str, output: Any) -> bytes | None:
        """Decode _download_file_via_command output; None (logged) on any doubt."""
        if not isinstance(output, str):
            _LOGGER.error("Download of %s returned no output", remote_path)
            return None
        if "__HA_DL_NOFILE__" in output:
            _LOGGER.error(
                "Download failed: %s does not exist on the router", remote_path
            )
            return None
        size_match = re.search(r"__HA_DL_SIZE__\s*(\d+)", output)
        body_match = re.search(r"__HA_DL_BEGIN__(.*?)__HA_DL_END__", output, re.S)
        if not size_match or not body_match:
            _LOGGER.error("Download of %s returned unexpected output", remote_path)
            return None
        body = body_match.group(1)
        if "__HA_DL_NOENC__" in body:
            _LOGGER.error(
                "Download of %s failed: the router has no ucode, base64, openssl "
                "or hexdump to encode the file",
                remote_path,
            )
            return None
        try:
            if "__HA_DL_HEX__" in body:
                hex_text = "".join(body.split("__HA_DL_HEX__", 1)[1].split())
                data = bytes.fromhex(hex_text)
            else:
                data = base64.b64decode("".join(body.split()), validate=True)
        except ValueError as err:
            _LOGGER.error(
                "Download of %s returned undecodable data: %s", remote_path, err
            )
            return None
        expected = int(size_match.group(1))
        if len(data) != expected:
            _LOGGER.error(
                "Download of %s is %d bytes but the file on the router is %d bytes; "
                "not saving it",
                remote_path,
                len(data),
                expected,
            )
            return None
        return data

    async def get_gateway_mac(self) -> str | None:
        """Get the default gateway MAC address."""
        try:
            # 1. Get default gateway IP
            route_out = await self.execute_command("ip route show default 2>/dev/null")
            if not route_out:
                return None

            # Example: default via 192.168.178.1 dev eth0 proto static
            parts = route_out.split()
            if "via" not in parts:
                return None

            gw_ip = parts[parts.index("via") + 1]

            # 2. Get MAC for that IP
            neigh_out = await self.execute_command(f"ip neigh show {gw_ip} 2>/dev/null")
            if "lladdr" in neigh_out:
                neigh_parts = neigh_out.split()
                return neigh_parts[neigh_parts.index("lladdr") + 1].upper()
        except Exception as err:
            _LOGGER.debug("Failed to get gateway MAC: %s", err)
        return None

    def _parse_ip_neigh_line(self, line: str) -> IpNeighbor | None:
        """Parse a single line from 'ip neigh show'."""
        parts = line.split()
        if len(parts) < 4:
            return None

        ip, mac, interface, state = parts[0], "", "", parts[-1]

        # Filter out IPv6 link-local addresses (fe80::/10) as they can cause devices
        # to be falsely reported as home due to stale link-local neighbor entries.
        import ipaddress

        try:
            ip_obj = ipaddress.ip_address(ip)
            if ip_obj.version == 6 and ip_obj.is_link_local:
                return None
        except ValueError:
            pass

        # Filter out invalid or inactive states
        # LuCI typically only considers REACHABLE, DELAY, PROBE, and PERMANENT as active.
        # STALE means the device was seen recently but hasn't responded to the last probe.
        if state in ("FAILED", "INCOMPLETE"):
            return None

        if "lladdr" in parts:
            idx = parts.index("lladdr")
            if len(parts) > idx + 1:
                mac = parts[idx + 1].lower()
        if "dev" in parts:
            idx = parts.index("dev")
            if len(parts) > idx + 1:
                interface = parts[idx + 1]

        if mac:
            return IpNeighbor(ip=ip, mac=mac, interface=interface, state=state)
        return None

    async def _get_neighbors_proc_arp(self, neighbors: list[IpNeighbor]) -> None:
        """Fetch neighbors from /proc/net/arp."""
        try:
            content = ""
            # Prefer reading the file directly so that restricted ACLs can allow reading
            # ARP cache without requiring full root file.exec. (Only if _call is available, e.g. UbusClient)
            if hasattr(self, "_call"):
                try:
                    result = await self._call("file", "read", {"path": "/proc/net/arp"})
                    content = result.get("data", "")
                except Exception:
                    pass
            if not content:
                content = await self.execute_command("cat /proc/net/arp 2>/dev/null")
            if content:
                lines = content.strip().split("\n")
                if len(lines) > 1:
                    for line in lines[1:]:
                        parts = line.split()
                        if len(parts) >= 4:
                            neighbors.append(
                                IpNeighbor(
                                    ip=parts[0],
                                    mac=parts[3].lower(),
                                    interface=parts[5] if len(parts) > 5 else "",
                                    state="REACHABLE",
                                ),
                            )
        except Exception:
            pass
