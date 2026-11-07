# mypy: disable-error-code="attr-defined,has-type"
"""SSH services, adblock, sqm, and firmware operations mixin."""

from __future__ import annotations

import asyncio
import logging
import shlex
from typing import Any

from ..base import (
    AdBlockStatus,
    NlbwmonTraffic,
    SimpleAdBlockStatus,
    SqmStatus,
)
from .exceptions import SshError

_LOGGER = logging.getLogger(__name__)


class SshFeaturesServicesMixin:
    """Mixin for SSH services, adblock, and firmware features."""

    async def install_firmware(
        self, url: str, keep_settings: bool = True, force: bool = False
    ) -> None:
        """Install firmware from the given URL via SSH."""
        # Use sysupgrade for installation
        # Download to /tmp and then run sysupgrade
        keep = "" if keep_settings else "-n"
        force_flag = "-F " if force else ""
        safe_url = shlex.quote(url)
        cmd = (
            f"if command -v uclient-fetch >/dev/null 2>&1; then "
            f"  uclient-fetch --no-check-certificate -O /tmp/firmware.bin {safe_url}; "
            f"elif command -v curl >/dev/null 2>&1; then "
            f"  curl -k -L -o /tmp/firmware.bin {safe_url}; "
            f"else "
            f"  wget --no-check-certificate -O /tmp/firmware.bin {safe_url}; "
            f"fi && sysupgrade {force_flag}{keep} /tmp/firmware.bin"
        )
        try:
            _LOGGER.info("Initiating firmware installation via SSH from: %s", url)
            # We expect this to eventually fail or disconnect as the router reboots
            await self._exec(cmd)
        except Exception as err:
            # If it's a connection error, it's likely the router rebooting
            err_msg = str(err).lower()
            if any(
                msg in err_msg
                for msg in [
                    "connection reset",
                    "broken pipe",
                    "closed",
                    "eof",
                    "timeout",
                    "cannot connect",
                    "could not connect",
                    "connection lost",
                    "connection failed",
                    "connection error",
                    "unreachable",
                    "host",
                ]
            ):
                _LOGGER.info(
                    "SSH connection lost during sysupgrade - device is likely rebooting",
                )
                return
            _LOGGER.exception("Failed to execute sysupgrade via SSH: %s", err)
            msg = f"sysupgrade execution failed: {err}"
            raise SshError(msg) from err

    async def download_file(self, remote_path: str, local_path: str) -> bool:
        """Download a file from the router via SSH using cat (fallback for SCP)."""
        try:
            # Using cat and reading back the result. For larger files this might be slow,
            # but for backups (~some KB) it should be fine.
            safe_path = shlex.quote(remote_path)
            content = await self._exec(f"cat {safe_path}")
            if content:
                # We need to be careful with binary data over SSH exec
                # If the file is a .tar.gz, it's binary.
                # Let's try base64 to be safe if it's binary.
                b64_content = await self._exec(f"base64 {safe_path}")
                import base64

                data = base64.b64decode(b64_content)
                await asyncio.to_thread(self._write_bytes, local_path, data)
                return True
        except Exception as err:
            _LOGGER.exception("Failed to download file via SSH: %s", err)
        return False

    async def get_adblock_status(self) -> AdBlockStatus:
        """Get adblock status via SSH."""
        from ..base import AdBlockStatus

        status = AdBlockStatus()
        # 1. Try reading runtime JSON file first
        for path in ("/var/run/adblock/adblock.runtime.json", "/tmp/adb_runtime.json"):
            try:
                content = await self.read_file(path)
                if content:
                    import json

                    res = json.loads(content)
                    if (
                        res
                        and isinstance(res, dict)
                        and (res.get("adblock_status") or res.get("status"))
                    ):
                        status.enabled = (
                            res.get("adblock_status") == "enabled"
                            or res.get("status") == "enabled"
                        )
                        status.status = res.get("adblock_status") or res.get(
                            "status", "disabled"
                        )
                        status.version = res.get("adblock_version") or res.get(
                            "version"
                        )

                        blocked = res.get("blocked_domains") or res.get("blocked") or 0
                        from ...helpers import parse_blocked_domains

                        status.blocked_domains = parse_blocked_domains(blocked)

                        last_run = res.get("last_run") or res.get("last_update")
                        if isinstance(last_run, dict):
                            status.last_update = last_run.get("timestamp")
                        else:
                            status.last_update = last_run
                        return status
            except Exception as err:
                _LOGGER.debug("AdBlock JSON file read failed (%s): %s", path, err)

        # 2. Try ubus first (provides more details)
        try:
            out = await self._exec("ubus call adblock status 2>/dev/null")
            if out:
                try:
                    res = json.loads(out)
                    if res and isinstance(res, dict) and res.get("adblock_status"):
                        status.enabled = res.get("adblock_status") == "enabled"
                        status.status = res.get("adblock_status", "disabled")
                        status.version = res.get("adblock_version")
                        from ...helpers import parse_blocked_domains

                        status.blocked_domains = parse_blocked_domains(
                            res.get("blocked_domains", 0)
                        )
                        status.last_update = res.get("last_run")
                        return status
                except json.JSONDecodeError:
                    pass
        except Exception as err:
            _LOGGER.debug("AdBlock ubus status failed (SSH): %s", err)

        # 3. Fallback to uci (basic status)
        try:
            enabled = await self._exec(
                "uci -q get adblock.global.adb_enabled || uci -q get adblock.global.enabled"
            )
            status.enabled = (enabled or "").strip() == "1"
            status.status = "enabled" if status.enabled else "disabled"
        except Exception as err:
            _LOGGER.debug("AdBlock UCI status failed (SSH): %s", err)

        return status

    async def get_adblock_fast_status(self) -> SimpleAdBlockStatus:
        """Get adblock-fast status via SSH."""
        from ..base import SimpleAdBlockStatus

        status = SimpleAdBlockStatus()
        try:
            # Fallback to uci
            res = await self._exec("uci -q get adblock-fast.config.enabled")
            status.enabled = res.strip() == "1"
            status.status = "enabled" if status.enabled else "disabled"
            count = await self._exec("wc -l < /tmp/adblock-fast.blocked 2>/dev/null")
            if count and count.strip().isdigit():
                status.blocked_domains = int(count.strip())
            else:
                status.blocked_domains = 0
        except Exception as err:
            _LOGGER.debug("Adblock-fast status query failed via SSH: %s", err)
        return status

    async def set_adblock_fast_enabled(self, enabled: bool) -> bool:
        """Enable/disable adblock-fast service via SSH."""
        val = "1" if enabled else "0"
        try:
            safe_val = shlex.quote(f"adblock-fast.config.enabled={val}")
            await self._exec(f"uci set {safe_val} && uci commit adblock-fast")
            action = "start" if enabled else "stop"
            await self._exec(f"/etc/init.d/adblock-fast {action}")
            self._invalidate_slow_poll()
            return True
        except Exception:
            return False

    async def manage_service(self, name: str, action: str) -> bool:
        """Manage a system service (start/stop/restart/enable/disable) via SSH."""
        try:
            safe_svc = shlex.quote(f"/etc/init.d/{name}")
            safe_action = shlex.quote(action)
            await self._exec(f"{safe_svc} {safe_action}")
            self._invalidate_slow_poll()
            return True
        except Exception as err:
            _LOGGER.exception(
                "Failed to manage service %s (%s) via SSH: %s",
                name,
                action,
                err,
            )
            return False

    async def set_adblock_enabled(self, enabled: bool) -> bool:
        """Enable/disable adblock service via SSH."""
        val = "1" if enabled else "0"
        try:
            await self._exec(
                f"uci set adblock.global.adb_enabled='{val}' && uci set adblock.global.enabled='{val}' && uci commit adblock"
            )
            action = "start" if enabled else "stop"
            await self._exec(f"/etc/init.d/adblock {action}")
            self._invalidate_slow_poll()
            return True
        except Exception:
            return False

    async def get_simple_adblock_status(self) -> SimpleAdBlockStatus:
        """Get simple-adblock status via SSH."""
        from ..base import SimpleAdBlockStatus

        status = SimpleAdBlockStatus()
        try:
            res = await self._exec("uci -q get simple-adblock.config.enabled")
            status.enabled = res.strip() == "1"
            status.status = "enabled" if status.enabled else "disabled"
            count = await self._exec("wc -l < /tmp/simple-adblock.blocked 2>/dev/null")
            if count and count.strip().isdigit():
                status.blocked_domains = int(count.strip())
        except Exception:
            pass
        return status

    async def set_simple_adblock_enabled(self, enabled: bool) -> bool:
        """Enable/disable simple-adblock service via SSH."""
        val = "1" if enabled else "0"
        try:
            safe_val = shlex.quote(f"simple-adblock.config.enabled={val}")
            await self._exec(f"uci set {safe_val} && uci commit simple-adblock")
            action = "start" if enabled else "stop"
            await self._exec(f"/etc/init.d/simple-adblock {action}")
            self._invalidate_slow_poll()
            return True
        except Exception:
            return False

    # banIP is handled backend-agnostically in OpenWrtClient (base.py).

    async def get_sqm_status(self) -> list[SqmStatus]:
        """Get SQM status via SSH."""
        from ..base import SqmStatus

        sqm_instances: list[SqmStatus] = []
        try:
            output = await self._exec("uci show sqm")
            sections: dict[str, dict[str, str]] = {}
            for line in output.splitlines():
                if "=" not in line:
                    continue
                key, val = line.split("=", 1)
                parts = key.split(".")
                if len(parts) >= 2:
                    section = parts[1]
                    if section not in sections:
                        sections[section] = {}
                    if len(parts) >= 3:
                        sections[section][parts[2]] = val.strip("'").strip('"')
                    else:
                        sections[section][".type"] = val.strip("'").strip('"')

            for section_id, data in sections.items():
                if data.get(".type") == "queue":
                    sqm_instances.append(
                        SqmStatus(
                            section_id=section_id,
                            name=data.get("name", section_id),
                            enabled=data.get("enabled") == "1",
                            interface=data.get("interface", ""),
                            download=int(data.get("download", "0")),
                            upload=int(data.get("upload", "0")),
                            qdisc=data.get("qdisc", ""),
                            script=data.get("script", ""),
                        ),
                    )
        except Exception as err:
            _LOGGER.debug("Failed to get SQM status via SSH: %s", err)
        return sqm_instances

    async def set_sqm_config(self, section_id: str, **kwargs: Any) -> bool:
        """Set SQM configuration via SSH."""
        try:
            for key, value in kwargs.items():
                val_str = (
                    "1" if value is True else "0" if value is False else str(value)
                )
                safe_val = shlex.quote(f"sqm.{section_id}.{key}={val_str}")
                await self._exec(f"uci set {safe_val}")
            await self._exec("uci commit sqm")
            await self._exec("/etc/init.d/sqm reload")
            self._invalidate_slow_poll()
            return True
        except Exception as err:
            _LOGGER.exception("Failed to set SQM config via SSH: %s", err)
            return False

    async def get_nlbwmon_data(self) -> dict[str, NlbwmonTraffic]:
        """Get bandwidth usage per MAC from nlbwmon via SSH."""
        try:
            out = await self._exec("nlbw -c json -g mac")
            if not out:
                return {}

            import json

            result = json.loads(out)
            if not result or "data" not in result:
                return {}

            traffic = {}
            for entry in result["data"]:
                mac = entry.get("mac", "").upper()
                if not mac:
                    continue
                traffic[mac] = NlbwmonTraffic(
                    mac=mac,
                    rx_bytes=entry.get("rx", 0),
                    tx_bytes=entry.get("tx", 0),
                    rx_packets=entry.get("rx_packets", 0),
                    tx_packets=entry.get("tx_packets", 0),
                )
            return traffic
        except Exception as err:
            _LOGGER.debug("Failed to get nlbwmon data via SSH: %s", err)
            return {}

    async def is_reboot_required(self) -> bool:
        """Check if reboot is required via SSH."""
        try:
            output = await self.execute_command(
                "[ -f /tmp/.reboot-needed ] || [ -f /var/run/reboot-required ] && echo 1"
            )
            return output.strip() == "1"
        except Exception:
            return False
