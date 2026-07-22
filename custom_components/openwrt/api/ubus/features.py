# mypy: disable-error-code="attr-defined"
from __future__ import annotations

import logging
from typing import Any

from ..base import (
    AccessControl,
    AdBlockStatus,
    FirewallRedirect,
    FirewallRule,
    NlbwmonTraffic,
    SimpleAdBlockStatus,
    SqmStatus,
)
from .exceptions import *

_LOGGER = logging.getLogger(__name__)
UBUS_JSONRPC_VERSION = "2.0"
UBUS_ID_AUTH = 1
UBUS_ID_CALL = 2


class UbusFeaturesMixin:
    """Features methods for UbusClient."""

    async def set_firewall_rule_enabled(self, section_id: str, enabled: bool) -> bool:
        """Enable or disable a firewall rule via UCI."""
        action = "1" if enabled else "0"
        try:
            await self._call(
                "uci",
                "set",
                {
                    "config": "firewall",
                    "section": section_id,
                    "values": {"enabled": action},
                },
            )
            await self._call("uci", "commit", {"config": "firewall"})
        except UbusError as err:
            _LOGGER.error(
                "uci set/commit failed for firewall rule %s (enabled=%s): %s",
                section_id,
                enabled,
                err,
            )
            raise

        reload_result = await self.execute_command(
            "/etc/init.d/firewall reload; echo RC=$?"
        )
        if "RC=0" not in (reload_result or ""):
            _LOGGER.error(
                "firewall reload failed after setting rule %s (enabled=%s): %s",
                section_id,
                enabled,
                reload_result,
            )
            raise UbusError(f"firewall reload failed after updating rule {section_id}")

        self._last_full_poll = 0
        return True

    async def get_firewall_rules(self) -> list[FirewallRule]:
        """Get general firewall rules via UCI."""
        rules: list[FirewallRule] = []
        config = await self._call("uci", "get", {"config": "firewall"})
        values = config.get("values", {})

        for section_id, section_data in values.items():
            if section_data.get(".type") != "rule":
                continue

            display_id = section_id
            if section_id.startswith("cfg"):
                rule_sects = [k for k, v in values.items() if v.get(".type") == "rule"]
                try:
                    idx = rule_sects.index(section_id)
                    display_id = f"@rule[{idx}]"
                except ValueError:
                    pass

            rules.append(
                FirewallRule(
                    name=section_data.get("name", display_id),
                    enabled=str(section_data.get("enabled", "1")) == "1",
                    section_id=display_id,
                    target=section_data.get("target", ""),
                    src=section_data.get("src", ""),
                    dest=section_data.get("dest", ""),
                ),
            )
        return rules

    async def get_firewall_redirects(self) -> list[FirewallRedirect]:
        """Get firewall port forwarding redirects via UCI."""
        redirects: list[FirewallRedirect] = []
        config = await self._call("uci", "get", {"config": "firewall"})
        vals = config.get("values", {})

        for section_id, redirect in vals.items():
            if redirect.get(".type") != "redirect":
                continue

            # Standardize section ID: Prefer named sections, fallback to anonymous index
            # if it looks like cfgXXXXXX
            display_id = section_id
            if section_id.startswith("cfg"):
                # Find the index of this redirect among all redirects
                redirect_sects = [
                    k for k, v in vals.items() if v.get(".type") == "redirect"
                ]
                try:
                    idx = redirect_sects.index(section_id)
                    display_id = f"@redirect[{idx}]"
                except ValueError:
                    pass

            redirects.append(
                FirewallRedirect(
                    name=redirect.get("name", "Unnamed Redirect"),
                    target_ip=redirect.get("dest_ip", ""),
                    target_port=redirect.get("dest_port", ""),
                    external_port=redirect.get("src_dport", ""),
                    protocol=redirect.get("proto", "tcp"),
                    enabled=redirect.get("enabled", "1") == "1",
                    section_id=display_id,
                )
            )
        return redirects

    async def set_firewall_redirect_enabled(
        self,
        section_id: str,
        enabled: bool,
    ) -> bool:
        """Enable or disable a firewall redirect via UCI."""
        try:
            value = "1" if enabled else "0"
            await self._call(
                "uci",
                "set",
                {
                    "config": "firewall",
                    "section": section_id,
                    "values": {"enabled": value},
                },
            )
            await self._call("uci", "commit", {"config": "firewall"})
            await self._call("service", "reloading", {"service": "firewall"})
            self._last_full_poll = 0
            return True
        except UbusError:
            return False

    async def get_access_control(self) -> list[AccessControl]:
        """Get list of access control rules via UCI firewall rules."""
        rules: list[AccessControl] = []
        config = await self._call("uci", "get", {"config": "firewall"})
        values = config.get("values", {})

        for section_id, section_data in values.items():
            if section_data.get(".type") != "rule":
                continue

            name = section_data.get("name", "")
            if not name.startswith("ha_acl_"):
                continue

            mac = section_data.get("src_mac", "").upper()
            if mac:
                rules.append(
                    AccessControl(
                        mac=mac,
                        name=name.replace("ha_acl_", ""),
                        blocked=str(section_data.get("enabled", "1")) == "1"
                        and section_data.get("target") in ("REJECT", "DROP"),
                        section_id=section_id,
                    ),
                )
        return rules

    async def set_access_control_blocked(self, mac: str, blocked: bool) -> bool:
        """Block or unblock a device's internet access via UCI firewall rule."""
        mac_upper = mac.upper()
        mac_safe = mac_upper.replace(":", "")
        rule_name = f"ha_acl_{mac_safe}"

        try:
            rules = await self.get_access_control()
            section_id = next((r.section_id for r in rules if r.mac == mac_upper), None)

            if blocked:
                if not section_id:
                    res = await self._call(
                        "uci",
                        "add",
                        {"config": "firewall", "type": "rule"},
                    )
                    section_id = res.get("section")
                    if not section_id:
                        return False

                    await self._call(
                        "uci",
                        "set",
                        {
                            "config": "firewall",
                            "section": section_id,
                            "values": {
                                "name": rule_name,
                                "src": "lan",
                                "dest": "wan",
                                "src_mac": mac_upper,
                                "target": "REJECT",
                                "enabled": "1",
                            },
                        },
                    )
                else:
                    await self._call(
                        "uci",
                        "set",
                        {
                            "config": "firewall",
                            "section": section_id,
                            "values": {"enabled": "1", "target": "REJECT"},
                        },
                    )
            elif section_id:
                await self._call(
                    "uci",
                    "set",
                    {
                        "config": "firewall",
                        "section": section_id,
                        "values": {"enabled": "0"},
                    },
                )

            await self._call("uci", "commit", {"config": "firewall"})
            await self._call("service", "reloading", {"service": "firewall"})
            self._last_full_poll = 0
            return True
        except UbusError:
            return False

    async def get_adblock_status(self) -> AdBlockStatus:
        """Get adblock status via ubus/uci."""
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
                        blocked_str = str(blocked).replace(",", "").replace(".", "")
                        try:
                            status.blocked_domains = int(float(blocked_str))
                        except (ValueError, TypeError):
                            pass

                        last_run = res.get("last_run") or res.get("last_update")
                        if isinstance(last_run, dict):
                            status.last_update = last_run.get("timestamp")
                        else:
                            status.last_update = last_run
                        return status
            except Exception as err:
                _LOGGER.debug("AdBlock JSON file read failed (%s): %s", path, err)

        # 2. Try ubus call if file read failed/was empty
        try:
            res = await self._call("adblock", "status")
            if res and isinstance(res, dict) and res.get("adblock_status"):
                status.enabled = res.get("adblock_status") == "enabled"
                status.status = res.get("adblock_status", "disabled")
                status.version = res.get("adblock_version")
                # Handle formatted numbers like "57,861" or "57.861"
                blocked = (
                    str(res.get("blocked_domains", 0)).replace(",", "").replace(".", "")
                )
                try:
                    status.blocked_domains = int(float(blocked))
                except (ValueError, TypeError):
                    pass
                status.last_update = res.get("last_run")
                return status
        except Exception as err:
            _LOGGER.debug("AdBlock ubus status failed: %s", err)

        # 3. Fallback to uci (basic status)
        try:
            enabled = await self.execute_command(
                "uci -q get adblock.global.adb_enabled || uci -q get adblock.global.enabled"
            )
            status.enabled = (enabled or "").strip() == "1"
            status.status = "enabled" if status.enabled else "disabled"
        except Exception as err:
            _LOGGER.debug("AdBlock UCI status failed: %s", err)

        return status

    async def set_adblock_enabled(self, enabled: bool) -> bool:
        """Enable/disable adblock service."""
        val = "1" if enabled else "0"
        try:
            await self.execute_command(
                f"uci set adblock.global.adb_enabled='{val}' && uci set adblock.global.enabled='{val}' && uci commit adblock",
            )
            action = "start" if enabled else "stop"
            await self.execute_command(f"/etc/init.d/adblock {action}")
            self._last_full_poll = 0
            return True
        except Exception:
            return False

    async def get_simple_adblock_status(self) -> SimpleAdBlockStatus:
        """Get simple-adblock status via uci."""
        from ..base import SimpleAdBlockStatus

        status = SimpleAdBlockStatus()
        try:
            res = await self.execute_command("uci -q get simple-adblock.config.enabled")
            status.enabled = res.strip() == "1"
            status.status = "enabled" if status.enabled else "disabled"
            # Optional: try to count blocked domains if file exists
            count = await self.execute_command(
                "wc -l < /tmp/simple-adblock.blocked 2>/dev/null",
            )
            if count and count.strip().isdigit():
                status.blocked_domains = int(count.strip())
        except Exception:
            pass
        return status

    async def set_simple_adblock_enabled(self, enabled: bool) -> bool:
        """Enable/disable simple-adblock service."""
        val = "1" if enabled else "0"
        try:
            await self.execute_command(
                f"uci set simple-adblock.config.enabled='{val}' && uci commit simple-adblock",
            )
            action = "start" if enabled else "stop"
            await self.execute_command(f"/etc/init.d/simple-adblock {action}")
            self._last_full_poll = 0
            return True
        except Exception:
            return False

    # banIP status/report + enable/disable are implemented backend-agnostically
    # in OpenWrtClient.get_banip_status / set_banip_enabled (base.py).

    async def get_sqm_status(self) -> list[SqmStatus]:
        """Get SQM status via uci ubus."""
        from ..base import SqmStatus

        sqm_instances: list[SqmStatus] = []
        try:
            resp = await self._call("uci", "get", {"config": "sqm"})
            if not resp or not isinstance(resp, dict):
                return sqm_instances

            # Support both {"values": {...}} and direct {...}
            values = resp.get("values", resp)
            if not isinstance(values, dict):
                return sqm_instances

            for section_id, section_data in values.items():
                if (
                    isinstance(section_data, dict)
                    and section_data.get(".type") == "queue"
                ):
                    sqm = SqmStatus(
                        section_id=section_id,
                        name=str(section_data.get("name", section_id)),
                        enabled=section_data.get("enabled") == "1",
                        interface=section_data.get("interface", ""),
                        download=int(section_data.get("download", 0)),
                        upload=int(section_data.get("upload", 0)),
                        qdisc=section_data.get("qdisc", ""),
                        script=section_data.get("script", ""),
                    )
                    sqm_instances.append(sqm)
        except Exception as err:
            _LOGGER.debug("SQM status check failed: %s", err)
        return sqm_instances

    async def set_sqm_config(self, section_id: str, **kwargs: Any) -> bool:
        """Set SQM configuration via uci ubus."""
        try:
            for key, value in kwargs.items():
                val_str = (
                    "1" if value is True else "0" if value is False else str(value)
                )
                await self._call(
                    "uci",
                    "set",
                    {"config": "sqm", "section": section_id, "values": {key: val_str}},
                )
            await self._call("uci", "commit", {"config": "sqm"})
            await self._call(
                "file",
                "exec",
                {"command": "/etc/init.d/sqm", "params": ["reload"]},
            )
            self._last_full_poll = 0
            return True
        except UbusPermissionError as err:
            _LOGGER.debug("SQM config via ubus denied (permissions): %s", err)
            return False
        except Exception as err:
            _LOGGER.exception("Failed to set SQM config: %s", err)
            return False

    async def get_nlbwmon_data(self) -> dict[str, NlbwmonTraffic]:
        """Get bandwidth usage per MAC from nlbwmon."""
        try:
            result = await self._call("nlbwmon", "get_data", {"group_by": "mac"})
            if not result or "data" not in result:
                return {}

            traffic = {}
            for mac, data in result["data"].items():
                mac_upper = mac.upper()
                traffic[mac_upper] = NlbwmonTraffic(
                    mac=mac_upper,
                    rx_bytes=data.get("rx", 0),
                    tx_bytes=data.get("tx", 0),
                    rx_packets=data.get("rx_packets", 0),
                )
            return traffic
        except Exception:
            return {}
