# mypy: disable-error-code="attr-defined"
from __future__ import annotations

import json
import logging
import shlex
from typing import Any

from ..base import (
    AccessControl,
    AdBlockStatus,
    FirewallRedirect,
    FirewallRule,
    NlbwmonTraffic,
    ServiceInfo,
    SimpleAdBlockStatus,
    SqmStatus,
)
from .exceptions import *

_LOGGER = logging.getLogger(__name__)


class SshFeaturesMixin:
    """Features methods for SshClient."""

    async def get_installed_packages(self) -> list[str]:
        """Get a list of installed packages via apk or opkg.

        On OpenWrt 25.x+ with APK: 'apk info' lists one package per line
        (no version suffix in the default output).  On older opkg-based
        firmware the first field (before the first space) is the package name.
        """
        try:
            cmd = (
                "if command -v apk >/dev/null 2>&1; then "
                "  apk info 2>/dev/null; "
                "else "
                "  opkg list-installed 2>/dev/null | cut -d' ' -f1; "
                "fi"
            )
            output = await self._exec(cmd)
            if not output:
                return []
            packages: list[str] = []
            for line in output.splitlines():
                name = line.strip()
                if not name:
                    continue

                # Strip version for apk (package-version-release)
                # On OpenWrt/APK, 'apk info -q' sometimes includes the version suffix.
                # Standard pkg names don't have digits immediately after a dash
                # unless it's the version part.
                if "-" in name and any(c.isdigit() for c in name):
                    parts = name.split("-")
                    # Package names like "sqm-scripts" are ok, but "sqm-scripts-1.0" should be "sqm-scripts"
                    # We look for the first part that starts with a digit or the last two parts
                    for i in range(1, len(parts)):
                        if parts[i] and parts[i][0].isdigit():
                            name = "-".join(parts[:i])
                            break

                packages.append(name)
            return list(set(packages))
        except Exception:
            return []

    async def get_services(self) -> list[ServiceInfo]:
        """Get init.d services."""
        services: list[ServiceInfo] = []

        ls_output = await self._exec("ls /etc/init.d/ 2>/dev/null")
        for svc_name in ls_output.strip().split("\n"):
            svc_name = svc_name.strip()
            if not svc_name:
                continue
            enabled = False
            running = False
            try:
                safe_svc = shlex.quote(f"/etc/init.d/{svc_name}")
                enabled_check = await self._exec(
                    f"{safe_svc} enabled && echo yes || echo no",
                )
                enabled = "yes" in enabled_check
                running_check = await self._exec(
                    f"{safe_svc} running && echo yes || echo no",
                )
                running = "yes" in running_check

                # Special handling for one-shot services that might be active but not "running"
                if (
                    not running
                    and svc_name in ("adblock", "simple-adblock", "sysctl")
                    and enabled
                ):
                    # For adblock, if ubus status says enabled, we consider it running
                    # but we don't want to duplicate too much logic here, so we just
                    # trust the 'enabled' state for these specific one-shot services
                    # if they are enabled at boot and it's a known one-shot service.
                    running = True
            except Exception:  # noqa: BLE001
                pass
            services.append(
                ServiceInfo(name=svc_name, enabled=enabled, running=running),
            )
        return services

    async def get_leds(self) -> list:
        """Get LEDs from /sys/class/leds."""
        from ..base import LedInfo

        leds: list[LedInfo] = []
        output = await self._exec(
            "for led in /sys/class/leds/*/; do "
            'name=$(basename "$led"); '
            'brightness=$(cat "$led/brightness" 2>/dev/null || echo 0); '
            'max=$(cat "$led/max_brightness" 2>/dev/null || echo 255); '
            'trigger=$(cat "$led/trigger" 2>/dev/null | tr " " "\\n" | grep "^\\[" | tr -d "[]" || echo none); '
            'echo "$name|$brightness|$max|$trigger"; '
            "done",
        )
        for line in output.strip().splitlines():
            parts = line.strip().split("|")
            if len(parts) >= 4:
                brightness = int(parts[1]) if parts[1].isdigit() else 0
                max_b = int(parts[2]) if parts[2].isdigit() else 255
                leds.append(
                    LedInfo(
                        name=parts[0],
                        brightness=brightness,
                        max_brightness=max_b,
                        trigger=parts[3],
                        active=brightness > 0,
                    ),
                )
        return leds

    async def set_led(self, name: str, brightness: int) -> bool:
        """Set LED via SSH."""
        try:
            # First ensure trigger is set to none to allow manual control
            await self._exec(f"echo none > /sys/class/leds/{name}/trigger 2>/dev/null")
            # Write brightness
            await self._exec(
                f"echo {int(brightness)} > /sys/class/leds/{name}/brightness"
            )
            self._last_full_poll = 0
            return True
        except Exception as err:
            _LOGGER.exception("Failed to set LED %s: %s", name, err)
            return False

    async def get_firewall_rules(self) -> list[FirewallRule]:
        """Get firewall rules via UCI over SSH."""
        from ..base import FirewallRule

        rules: list[FirewallRule] = []
        output = await self._exec("uci show firewall")
        sections: dict[str, dict[str, str]] = {}
        for line in output.splitlines():
            if "=" not in line:
                continue
            key, val = line.split("=", 1)
            parts = key.split(".")
            if len(parts) == 2:
                section = parts[1]
                if section not in sections:
                    sections[section] = {}
                sections[section][".type"] = val.strip("'")
            elif len(parts) >= 3:
                section = parts[1]
                if section not in sections:
                    sections[section] = {}
                sections[section][parts[2]] = val.strip("'")

        for section_id, data in sections.items():
            if data.get(".type") == "rule":
                rules.append(
                    FirewallRule(
                        name=data.get("name", section_id),
                        enabled=data.get("enabled", "1") == "1",
                        section_id=section_id,
                        target=data.get("target", ""),
                        src=data.get("src", ""),
                        dest=data.get("dest", ""),
                    ),
                )
        return rules

    async def set_firewall_rule_enabled(self, section_id: str, enabled: bool) -> bool:
        """Enable or disable a firewall rule via UCI over SSH."""
        try:
            val = "1" if enabled else "0"
            safe_val = shlex.quote(f"firewall.{section_id}.enabled={val}")
            await self._exec(f"uci set {safe_val}")
            await self._exec("uci commit firewall")
            await self._exec("/etc/init.d/firewall reload")
            self._last_full_poll = 0
            return True
        except Exception as err:
            _LOGGER.exception("Failed to set firewall rule via SSH: %s", err)
            return False

    async def get_firewall_redirects(self) -> list[FirewallRedirect]:
        """Get firewall port forwarding redirects via UCI over SSH."""
        redirects: list[FirewallRedirect] = []
        output = await self._exec("uci show firewall")
        sections: dict[str, dict[str, str]] = {}
        for line in output.splitlines():
            if "=" not in line:
                continue
            key, val = line.split("=", 1)
            parts = key.split(".")
            if len(parts) == 2:
                section = parts[1]
                if section not in sections:
                    sections[section] = {}
                sections[section][".type"] = val.strip("'")
            elif len(parts) >= 3:
                section = parts[1]
                if section not in sections:
                    sections[section] = {}
                sections[section][parts[2]] = val.strip("'")

        for section_id, data in sections.items():
            if data.get(".type") == "redirect":
                redirects.append(
                    FirewallRedirect(
                        name=data.get("name", section_id),
                        target_ip=data.get("dest_ip", ""),
                        target_port=data.get("dest_port", ""),
                        external_port=data.get("src_dport", ""),
                        protocol=data.get("proto", "tcp"),
                        enabled=data.get("enabled", "1") == "1",
                        section_id=section_id,
                    ),
                )
        return redirects

    async def set_firewall_redirect_enabled(
        self,
        section_id: str,
        enabled: bool,
    ) -> bool:
        """Enable or disable a firewall redirect via UCI over SSH."""
        try:
            val = "1" if enabled else "0"
            safe_val = shlex.quote(f"firewall.{section_id}.enabled={val}")
            await self._exec(f"uci set {safe_val}")
            await self._exec("uci commit firewall")
            await self._exec("/etc/init.d/firewall reload")
            self._last_full_poll = 0
            return True
        except Exception as err:
            _LOGGER.exception("Failed to set firewall redirect via SSH: %s", err)
            return False

    async def get_access_control(self) -> list[AccessControl]:
        """Get access control rules via UCI firewall rules over SSH."""
        rules: list[AccessControl] = []
        output = await self._exec("uci show firewall")
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
                    sections[section][parts[2]] = val.strip("'")

        for section_id, data in sections.items():
            if data.get(".type") != "rule":
                continue
            name = data.get("name", "")
            if not name.startswith("ha_acl_"):
                continue

            mac = data.get("src_mac", "").upper()
            if mac:
                rules.append(
                    AccessControl(
                        mac=mac,
                        name=name.replace("ha_acl_", ""),
                        blocked=data.get("enabled", "1") == "1"
                        and data.get("target") in ("REJECT", "DROP"),
                        section_id=section_id,
                    ),
                )
        return rules

    async def set_access_control_blocked(self, mac: str, blocked: bool) -> bool:
        """Block or unblock internet access for a MAC via SSH."""
        mac_upper = mac.upper()
        mac_safe = mac_upper.replace(":", "")
        rule_name = f"ha_acl_{mac_safe}"
        try:
            rules = await self.get_access_control()
            section_id = next((r.section_id for r in rules if r.mac == mac_upper), None)

            if blocked:
                if not section_id:
                    await self._exec("uci add firewall rule")
                    safe_rn = shlex.quote(f"firewall.{rule_name}=rule")
                    await self._exec(f"uci set {safe_rn}")
                    section_id = rule_name
                    safe_name = shlex.quote(f"firewall.{section_id}.name={rule_name}")
                    await self._exec(f"uci set {safe_name}")
                    safe_src = shlex.quote(f"firewall.{section_id}.src=lan")
                    await self._exec(f"uci set {safe_src}")
                    safe_dest = shlex.quote(f"firewall.{section_id}.dest=wan")
                    await self._exec(f"uci set {safe_dest}")
                    safe_mac = shlex.quote(f"firewall.{section_id}.src_mac={mac_upper}")
                    await self._exec(f"uci set {safe_mac}")
                    safe_target = shlex.quote(f"firewall.{section_id}.target=REJECT")
                    await self._exec(f"uci set {safe_target}")

                safe_en = shlex.quote(f"firewall.{section_id}.enabled=1")
                await self._exec(f"uci set {safe_en}")
            elif section_id:
                safe_dis = shlex.quote(f"firewall.{section_id}.enabled=0")
                await self._exec(f"uci set {safe_dis}")

            await self._exec("uci commit firewall")
            await self._exec("/etc/init.d/firewall reload")
            self._last_full_poll = 0
            return True
        except Exception as err:
            _LOGGER.exception("Failed to set access control via SSH: %s", err)
            return False

    async def install_firmware(self, url: str, keep_settings: bool = True) -> None:
        """Install firmware from the given URL via SSH."""
        # Use sysupgrade for installation
        # Download to /tmp and then run sysupgrade
        keep = "" if keep_settings else "-n"
        safe_url = shlex.quote(url)
        cmd = f"wget -O /tmp/firmware.bin {safe_url} && sysupgrade {keep} /tmp/firmware.bin"
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

                with open(local_path, "wb") as f:
                    f.write(base64.b64decode(b64_content))
                return True
        except Exception as err:
            _LOGGER.exception("Failed to download file via SSH: %s", err)
        return False

    async def get_adblock_status(self) -> AdBlockStatus:
        """Get adblock status via SSH."""
        from ..base import AdBlockStatus

        status = AdBlockStatus()
        # 1. Try ubus first (provides more details)
        try:
            out = await self._exec("ubus call adblock status 2>/dev/null")
            if out:
                try:
                    res = json.loads(out)
                    if res and isinstance(res, dict) and res.get("adblock_status"):
                        status.enabled = res.get("adblock_status") == "enabled"
                        status.status = res.get("adblock_status", "disabled")
                        status.version = res.get("adblock_version")
                        # Handle formatted numbers like "57,861" or "57.861"
                        blocked = (
                            str(res.get("blocked_domains", 0))
                            .replace(",", "")
                            .replace(".", "")
                        )
                        try:
                            status.blocked_domains = int(float(blocked))
                        except (ValueError, TypeError):
                            pass
                        status.last_update = res.get("last_run")
                        return status
                except json.JSONDecodeError:
                    pass
        except Exception as err:
            _LOGGER.debug("AdBlock ubus status failed (SSH): %s", err)

        # 2. Fallback to uci (basic status)
        try:
            enabled = await self._exec("uci -q get adblock.global.enabled")
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
        except Exception:
            pass
        return status

    async def set_adblock_fast_enabled(self, enabled: bool) -> bool:
        """Enable/disable adblock-fast service via SSH."""
        val = "1" if enabled else "0"
        try:
            safe_val = shlex.quote(f"adblock-fast.config.enabled={val}")
            await self._exec(f"uci set {safe_val} && uci commit adblock-fast")
            action = "start" if enabled else "stop"
            await self._exec(f"/etc/init.d/adblock-fast {action}")
            self._last_full_poll = 0
            return True
        except Exception:
            return False

    async def manage_service(self, name: str, action: str) -> bool:
        """Manage a system service (start/stop/restart/enable/disable) via SSH."""
        try:
            safe_svc = shlex.quote(f"/etc/init.d/{name}")
            safe_action = shlex.quote(action)
            await self._exec(f"{safe_svc} {safe_action}")
            self._last_full_poll = 0
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
            safe_val = shlex.quote(f"adblock.global.enabled={val}")
            await self._exec(f"uci set {safe_val} && uci commit adblock")
            action = "start" if enabled else "stop"
            await self._exec(f"/etc/init.d/adblock {action}")
            self._last_full_poll = 0
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
            self._last_full_poll = 0
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
            self._last_full_poll = 0
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
