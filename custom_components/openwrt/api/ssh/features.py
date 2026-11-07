# mypy: disable-error-code="attr-defined"
from __future__ import annotations

import contextlib
import json
import logging
import shlex
from typing import Any

from ..base import (
    AccessControl,
    FirewallRedirect,
    FirewallRule,
    ServiceInfo,
    classify_service,
)

_LOGGER = logging.getLogger(__name__)


from .features_services import SshFeaturesServicesMixin


class SshFeaturesMixin(SshFeaturesServicesMixin):
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

        # 'service list' distinguishes daemons (which own procd instances) from
        # one-shot scripts, whose 'running' check never succeeds.
        service_map: dict[str, Any] = {}
        with contextlib.suppress(Exception):
            parsed = json.loads(
                await self._exec("ubus call service list 2>/dev/null") or "{}"
            )
            if isinstance(parsed, dict):
                service_map = parsed

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
            except Exception:  # noqa: BLE001
                pass
            services.append(
                classify_service(
                    svc_name,
                    {"enabled": enabled, "running": running},
                    service_map.get(svc_name),
                ),
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
            self._invalidate_slow_poll()
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
            self._invalidate_slow_poll()
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
            self._invalidate_slow_poll()
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
            reload_result = await self._exec("/etc/init.d/firewall reload; echo RC=$?")
            if "RC=0" not in (reload_result or ""):
                _LOGGER.warning(
                    "[openwrt-access-control] Firewall reload failed after "
                    "setting access control for %s (blocked=%s): %s",
                    mac_upper,
                    blocked,
                    reload_result,
                )
                return False

            self._invalidate_slow_poll()

            if blocked:
                return await self._flush_conntrack_for_mac(mac_upper)

            return True
        except Exception as err:
            _LOGGER.exception("Failed to set access control via SSH: %s", err)
            return False
