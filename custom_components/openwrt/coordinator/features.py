"""Feature data mixin for OpenWrt coordinator."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import TYPE_CHECKING, Any

from homeassistant.helpers import device_registry as dr

from ..api.base import OpenWrtData
from ..const import (
    CONF_REVERSE_DNS,
    CONF_USERNAME,
    DEFAULT_REVERSE_DNS,
)
from ..repairs import (
    async_create_stale_permissions_repair,
    async_delete_stale_permissions_repair,
)

if TYPE_CHECKING:
    from .base import CoordinatorBase

    _Base = CoordinatorBase
else:
    _Base = object

_LOGGER = logging.getLogger(__name__)


class FeaturesMixin(_Base):
    """Mixin for coordinator features (MQTT presence, nlbwmon, snort, reverse DNS, stale permissions)."""

    async def _async_fetch_mqtt_presence_data(self, data: OpenWrtData) -> None:
        """Fetch MQTT presence status and logs."""
        try:
            status_output = await self.client.execute_command(
                "/etc/init.d/presence_hostapd status 2>/dev/null"
            )
            data.mqtt_presence_status = (
                status_output.strip() if status_output else "stopped"
            )

            # Optimized log fetch: tail first, then grep
            logs_output = await self.client.execute_command(
                "logread | tail -n 100 | grep presence_event | tail -n 10"
            )
            data.mqtt_presence_logs = logs_output.splitlines() if logs_output else []
        except Exception as err:
            _LOGGER.debug("Failed to fetch MQTT presence data: %s", err)
            data.mqtt_presence_status = "error"
            data.mqtt_presence_logs = [str(err)]

    @staticmethod
    def _format_bytes(num_bytes: int) -> str:
        value = float(num_bytes)
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if value < 1024 or unit == "TB":
                return f"{int(value)} {unit}" if unit == "B" else f"{value:.2f} {unit}"
            value /= 1024
        return f"{num_bytes} B"

    async def _async_fetch_nlbwmon_top_hosts_data(self, data: OpenWrtData) -> None:
        """Fetch and parse nlbwmon top bandwidth hosts."""
        empty: dict[str, Any] = {
            "top_hosts": [],
            "host_count": 0,
            "total_rx_bytes": 0,
            "total_tx_bytes": 0,
        }
        result = await self.client.file_exec(
            "/usr/sbin/nlbw",
            ["-c", "json", "-g", "ip,mac", "-o", "-rx_bytes,-tx_bytes"],
        )
        _LOGGER.debug(
            "nlbwmon file_exec result keys: %s", list(result.keys()) if result else None
        )
        if not result:
            _LOGGER.info(
                "nlbwmon top hosts: file_exec returned empty — "
                "check that nlbwmon is installed and rpcd file ACL allows execution"
            )
            data.nlbwmon_top_hosts = empty
            return

        stdout = result.get("stdout", "")
        stderr = result.get("stderr", "")

        combined = (stdout + stderr).lower()
        if "permission denied" in combined or "access denied" in combined:
            _LOGGER.warning(
                "nlbwmon requires ubus file.exec permission for '/usr/sbin/nlbw' in rpcd ACL"
            )
            data.nlbwmon_top_hosts = empty
            return

        if not stdout:
            _LOGGER.info(
                "nlbwmon top hosts: empty stdout (code=%s, stderr=%r) — "
                "nlbw binary may not be installed or failed to run",
                result.get("code"),
                stderr[:200] if stderr else "",
            )
            data.nlbwmon_top_hosts = empty
            return

        _LOGGER.debug("nlbwmon raw stdout (first 500 chars): %.500s", stdout)

        try:
            raw = json.loads(stdout)
        except (json.JSONDecodeError, ValueError) as err:
            _LOGGER.error(
                "Failed to parse nlbwmon output: %s — stdout was: %.300s",
                err,
                stdout,
            )
            data.nlbwmon_top_hosts = empty
            return

        columns = raw.get("columns", [])
        rows = raw.get("data", [])
        _LOGGER.debug("nlbwmon columns=%s rows=%d", columns, len(rows))
        if not columns or not rows:
            _LOGGER.info(
                "nlbwmon returned no data rows (columns=%s, rows=%d) — "
                "nlbwmon may not have collected any traffic yet",
                columns,
                len(rows),
            )
            data.nlbwmon_top_hosts = empty
            return

        col = {name: idx for idx, name in enumerate(columns)}
        required = {"ip", "mac", "rx_bytes", "tx_bytes"}
        if not required.issubset(col.keys()):
            _LOGGER.error(
                "nlbwmon output missing required columns %s — got: %s",
                required - col.keys(),
                columns,
            )
            data.nlbwmon_top_hosts = empty
            return

        aggregated: dict[str, dict[str, Any]] = {}
        for row in rows:
            ip = row[col["ip"]] if "ip" in col else ""
            mac = (row[col["mac"]] if "mac" in col else "").upper()
            if mac == "00:00:00:00:00:00" and not ip:
                continue
            key = ip if mac == "00:00:00:00:00:00" else mac
            if key not in aggregated:
                aggregated[key] = {
                    "mac": mac,
                    "ip": ip,
                    "rx_bytes": 0,
                    "tx_bytes": 0,
                    "conns": 0,
                }
            aggregated[key]["rx_bytes"] += row[col["rx_bytes"]]
            aggregated[key]["tx_bytes"] += row[col["tx_bytes"]]
            if "conns" in col:
                aggregated[key]["conns"] += row[col["conns"]]
            current_ip = aggregated[key]["ip"]
            if ":" in current_ip and ":" not in ip and ip:
                aggregated[key]["ip"] = ip

        hostname_map: dict[str, str] = {}
        try:
            raw_leases = await self.client.get_dhcp_leases()
            for lease in raw_leases:
                if lease.mac and lease.hostname and lease.hostname != "*":
                    hostname_map[lease.mac.upper()] = lease.hostname
        except Exception:
            pass

        device_reg = dr.async_get(self.hass)
        devices_iterable: Any
        if hasattr(device_reg.devices, "values"):
            # Avoid deprecated mapping access on modern DeviceRegistry while supporting older mock dicts
            dev_devices = device_reg.devices
            devices_iterable = (
                dev_devices
                if not isinstance(dev_devices, dict)
                else dev_devices.values()
            )
        else:
            devices_iterable = device_reg.devices
        for dev_or_id in devices_iterable:
            if isinstance(dev_or_id, str):
                dev = device_reg.async_get(dev_or_id)
            else:
                dev = dev_or_id
            if not dev:
                continue
            name = dev.name_by_user or dev.name
            if not name:
                continue
            for conn_type, conn_mac in dev.connections:
                if conn_type == dr.CONNECTION_NETWORK_MAC:
                    mac_key = conn_mac.upper()
                    if mac_key and mac_key not in hostname_map:
                        hostname_map[mac_key] = name

        hosts = []
        for entry_data in aggregated.values():
            total = entry_data["rx_bytes"] + entry_data["tx_bytes"]
            if total == 0:
                continue
            mac = entry_data["mac"]
            hostname = hostname_map.get(mac) or entry_data["ip"] or mac or "Unknown"
            hosts.append({**entry_data, "total_bytes": total, "hostname": hostname})

        hosts.sort(key=lambda x: x["total_bytes"], reverse=True)

        top_hosts = [
            {
                "rank": i + 1,
                "hostname": h["hostname"],
                "ip": h["ip"],
                "mac": h["mac"],
                "connections": h["conns"],
                "rx_bytes": h["rx_bytes"],
                "tx_bytes": h["tx_bytes"],
                "total_bytes": h["total_bytes"],
                "download": self._format_bytes(h["rx_bytes"]),
                "upload": self._format_bytes(h["tx_bytes"]),
                "total": self._format_bytes(h["total_bytes"]),
            }
            for i, h in enumerate(hosts[:5])
        ]

        data.nlbwmon_top_hosts = {
            "top_hosts": top_hosts,
            "host_count": len(hosts),
            "total_rx_bytes": sum(h["rx_bytes"] for h in hosts),
            "total_tx_bytes": sum(h["tx_bytes"] for h in hosts),
        }

    async def _async_fetch_snort_data(self, data: OpenWrtData) -> None:
        """Fetch Snort IDS status and latest alert via rpcd file.exec.

        Reads snort's alert_json log(s) for the alert count and most recent
        alert, plus the service running state. Multi-threaded snort writes one
        file per packet thread (/var/log/<tid>_alert_json.txt), so the counts
        are aggregated across all of them. Note the log lives on tmpfs, so the
        count resets on reboot (alerts since boot).
        """
        empty: dict[str, Any] = {
            "installed": False,
            "running": False,
            "alert_count": 0,
            "last_alert": None,
            "recent_alerts": [],
        }

        # 1. Service state via a direct exec of the init script (no /bin/sh).
        installed = False
        running = False
        try:
            res = await self.client.file_exec("/etc/init.d/snort", ["running"])
            if isinstance(res, dict) and "code" in res:
                installed = True
                running = res.get("code") == 0
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("snort: init 'running' probe failed: %s", err)

        if not installed:
            data.snort_status = empty
            return

        # Snort 3 writes one alert file per packet thread when running
        # multi-threaded (e.g. multi-queue NFQ): /var/log/<tid>_alert_json.txt.
        # Single-thread setups write the unprefixed /var/log/alert_json.txt.
        # Aggregate across whichever exist so the sensor works on any config.
        # rpcd file.exec runs the binary directly (no shell), so we pass an
        # explicit candidate list rather than a glob. tail/wc rather than
        # file.read: rpcd file.read truncates past ~256KB, which would blind
        # the sensor on a large IDS log.
        log_dir = "/var/log"
        candidates = [
            f"{log_dir}/alert_json.txt",
            *(f"{log_dir}/{tid}_alert_json.txt" for tid in range(16)),
        ]
        present: list[str] = []
        alerts: list[dict[str, Any]] = []
        count = 0
        try:
            res = await self.client.file_exec("/usr/bin/wc", ["-l", *candidates])
            out = res.get("stdout", "") if isinstance(res, dict) else ""
            # Lines look like "  <count> /var/log/<name>"; missing files only
            # error on stderr, and the trailing "total" line (emitted only for
            # >1 file) has no path so it fails this match and is skipped.
            for ln in out.splitlines():
                # Matches "<count> /path/alert_json.txt" and the per-thread
                # "<count> /path/<tid>_alert_json.txt"; the "total" summary line
                # has no such path and is skipped.
                m = re.match(r"\s*(\d+)\s+(\S*alert_json\.txt)\s*$", ln)
                if not m:
                    continue
                count += int(m.group(1))
                present.append(m.group(2))
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("snort: wc failed: %s", err)

        if present:
            try:
                # -q suppresses the "==> file <==" headers busybox prints for
                # multiple files, keeping the stream pure JSONL.
                res = await self.client.file_exec(
                    "/usr/bin/tail", ["-q", "-n", "20", *present]
                )
                body = res.get("stdout", "") if isinstance(res, dict) else ""
                for ln in body.splitlines():
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        obj = json.loads(ln)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if isinstance(obj, dict):
                        alerts.append(obj)
                # Per-thread files aren't globally ordered once merged; sort by
                # snort's epoch "seconds" field (best effort) so last_alert and
                # recent_alerts reflect true recency, then keep the newest 20.
                alerts.sort(
                    key=lambda a: (
                        a["seconds"]
                        if isinstance(a.get("seconds"), (int, float))
                        else 0
                    )
                )
                alerts = alerts[-20:]
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("snort: tail failed: %s", err)

        def _clean(value: Any) -> str | None:
            """Coerce to a single-line, length-capped string (attribute hygiene)."""
            if value is None:
                return None
            return str(value).replace("\n", " ").replace("\r", " ").strip()[:256]

        def _hostport(addr: Any, port: Any) -> str | None:
            if not addr:
                return None
            addr = str(addr)
            # Bracket IPv6 so "addr:port" stays unambiguous.
            return f"[{addr}]:{port}" if ":" in addr else f"{addr}:{port}"

        def _fmt_alert(a: dict[str, Any]) -> dict[str, Any]:
            return {
                "message": _clean(a.get("msg")),
                "timestamp": _clean(a.get("timestamp")),
                "proto": _clean(a.get("proto")),
                "src": _hostport(a.get("src_addr"), a.get("src_port")),
                "dst": _hostport(a.get("dst_addr"), a.get("dst_port")),
                "sid": a.get("sid"),
                "action": _clean(a.get("action")),
            }

        data.snort_status = {
            "installed": True,
            "running": running,
            "alert_count": count,
            "last_alert": _fmt_alert(alerts[-1]) if alerts else None,
            # newest first for display
            "recent_alerts": [_fmt_alert(a) for a in reversed(alerts)],
        }

    def _async_check_stale_permissions(self, data: OpenWrtData) -> None:
        """Check for stale permissions."""
        username = self.config_entry.data.get(CONF_USERNAME, "homeassistant")
        if username == "root":
            async_delete_stale_permissions_repair(self.hass, self.config_entry)
            return

        # Core system read permission is required for the integration to function
        perms = data.permissions

        stale = False
        reason = ""
        if not perms.read_system:
            stale = True
            reason = "missing core system read permissions"

        # Detect if an upgrade happened
        current_version = data.device_info.release_version
        is_upgrade = False
        if (
            self._last_version
            and current_version
            and self._last_version != current_version
        ):
            _LOGGER.info(
                "OpenWrt upgrade detected: %s -> %s",
                self._last_version,
                current_version,
            )
            is_upgrade = True

        # Update last version
        if current_version:
            self._last_version = current_version

        if stale:
            _LOGGER.warning(
                "Detected missing/stale RPC permissions for OpenWrt user '%s': %s (is_upgrade=%s). "
                "Some system sensors (such as CPU, memory, load, uptime, or temperature) will stop reporting. "
                "Please redeploy the Home Assistant user or restore the custom ACL rules on your router.",
                username,
                reason,
                is_upgrade,
            )
            async_create_stale_permissions_repair(
                self.hass, self.config_entry, is_upgrade=is_upgrade
            )
        else:
            async_delete_stale_permissions_repair(self.hass, self.config_entry)

    async def _async_resolve_reverse_dns(self, data: OpenWrtData) -> None:
        """Resolve reverse DNS for connected devices and DHCP leases if they have no hostname."""
        if not self.config_entry.options.get(CONF_REVERSE_DNS, DEFAULT_REVERSE_DNS):
            return

        import socket

        tasks = []

        async def resolve_device(device) -> None:
            try:
                resolved = await self.hass.async_add_executor_job(
                    socket.gethostbyaddr, device.ip
                )
                if resolved and resolved[0]:
                    device.hostname = resolved[0]
            except Exception as err:
                _LOGGER.debug(
                    "Reverse DNS failed for device %s (%s): %s",
                    device.mac,
                    device.ip,
                    err,
                )

        async def resolve_lease(lease) -> None:
            try:
                resolved = await self.hass.async_add_executor_job(
                    socket.gethostbyaddr, lease.ip
                )
                if resolved and resolved[0]:
                    lease.hostname = resolved[0]
            except Exception as err:
                _LOGGER.debug(
                    "Reverse DNS failed for lease %s (%s): %s", lease.mac, lease.ip, err
                )

        for device in data.connected_devices:
            if device.ip and (not device.hostname or device.hostname == "*"):
                tasks.append(resolve_device(device))
        for lease in data.dhcp_leases:
            if lease.ip and (not lease.hostname or lease.hostname == "*"):
                tasks.append(resolve_lease(lease))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
