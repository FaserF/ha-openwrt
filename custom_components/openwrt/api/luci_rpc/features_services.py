# mypy: disable-error-code="attr-defined,has-type"
"""LuCI RPC services, adblock, and QoS features mixin."""

from __future__ import annotations

import json
import logging
from typing import Any

from ..base import (
    AdBlockStatus,
    NlbwmonTraffic,
    ServiceInfo,
    SimpleAdBlockStatus,
    SqmStatus,
    WifiCredentials,
    classify_service,
)

_LOGGER = logging.getLogger(__name__)


def _extract_json(stdout: str | None) -> dict[str, Any]:
    """Parse the first JSON object embedded in command output."""
    if not stdout:
        return {}
    start = stdout.find("{")
    end = stdout.rfind("}")
    if start == -1 or end == -1:
        return {}
    try:
        data = json.loads(stdout[start : end + 1])
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


class LuciRpcFeaturesServicesMixin:
    """Mixin for LuCI RPC services, adblock, and QoS features."""

    async def get_adblock_status(self) -> AdBlockStatus:
        """Get adblock status via LuCI RPC."""
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
            out = await self._rpc_call(
                "sys",
                "exec",
                ["ubus call adblock status 2>/dev/null"],
            )
            if out:
                import json

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
            _LOGGER.debug("AdBlock ubus status failed (LuCI RPC): %s", err)

        # 3. Fallback to uci (basic status)
        try:
            enabled = await self._rpc_call(
                "sys",
                "exec",
                [
                    "uci -q get adblock.global.adb_enabled || uci -q get adblock.global.enabled"
                ],
            )
            status.enabled = (enabled or "").strip() == "1"
            status.status = "enabled" if status.enabled else "disabled"
        except Exception as err:
            _LOGGER.debug("AdBlock UCI status failed (LuCI RPC): %s", err)

        return status

    async def manage_service(self, name: str, action: str) -> bool:
        """Manage a system service (start/stop/restart/enable/disable) via LuCI RPC."""
        try:
            await self._rpc_call("sys", "exec", [f"/etc/init.d/{name} {action}"])
            self._invalidate_slow_poll()
            return True
        except Exception as err:
            _LOGGER.exception(
                "Failed to manage service %s (%s) via LuCI RPC: %s",
                name,
                action,
                err,
            )
            return False

    async def set_adblock_enabled(self, enabled: bool) -> bool:
        """Enable/disable adblock service via LuCI RPC."""
        val = "1" if enabled else "0"
        try:
            await self._rpc_call(
                "sys",
                "exec",
                [
                    f"uci set adblock.global.adb_enabled='{val}' && uci set adblock.global.enabled='{val}' && uci commit adblock"
                ],
            )
            action = "start" if enabled else "stop"
            await self._rpc_call("sys", "exec", [f"/etc/init.d/adblock {action}"])
            self._invalidate_slow_poll()
            return True
        except Exception:
            return False

    async def get_simple_adblock_status(self) -> SimpleAdBlockStatus:
        """Get simple-adblock status via LuCI RPC."""
        from ..base import SimpleAdBlockStatus

        status = SimpleAdBlockStatus()
        try:
            res = await self._rpc_call(
                "sys",
                "exec",
                ["uci -q get simple-adblock.config.enabled"],
            )
            status.enabled = res.strip() == "1"
            status.status = "enabled" if status.enabled else "disabled"
            count = await self._rpc_call(
                "sys",
                "exec",
                ["wc -l < /tmp/simple-adblock.blocked 2>/dev/null"],
            )
            if count and count.strip().isdigit():
                status.blocked_domains = int(count.strip())
        except Exception:
            pass
        return status

    async def set_simple_adblock_enabled(self, enabled: bool) -> bool:
        """Enable/disable simple-adblock service via LuCI RPC."""
        val = "1" if enabled else "0"
        try:
            await self._rpc_call(
                "sys",
                "exec",
                [
                    f"uci set simple-adblock.config.enabled='{val}' && uci commit simple-adblock",
                ],
            )
            action = "start" if enabled else "stop"
            await self._rpc_call(
                "sys",
                "exec",
                [f"/etc/init.d/simple-adblock {action}"],
            )
            self._invalidate_slow_poll()
            return True
        except Exception:
            return False

    # banIP is handled backend-agnostically in OpenWrtClient (base.py).

    async def get_sqm_status(self) -> list[SqmStatus]:
        """Get SQM status via LuCI RPC."""
        from ..base import SqmStatus

        sqm_instances: list[SqmStatus] = []
        resp = await self._rpc_call("uci", "get_all", ["sqm"])
        # Fallback to shell if permission denied or failed
        if not resp or (
            isinstance(resp, list) and len(resp) > 1 and resp[1] == "Permission denied"
        ):
            try:
                shell_out = await self.execute_command("uci show sqm 2>/dev/null")
                if shell_out:
                    # Parse uci output
                    sections: dict[str, dict[str, Any]] = {}
                    for line in shell_out.strip().split("\n"):
                        if "=" not in line:
                            continue
                        key, val = line.split("=", 1)
                        parts = key.split(".")
                        if len(parts) >= 2:
                            section = parts[1]
                            if section not in sections:
                                sections[section] = {}
                            if len(parts) == 2:
                                sections[section][".type"] = val.strip("'")
                            elif len(parts) == 3:
                                sections[section][parts[2]] = val.strip("'")
                    values_dict = sections
                else:
                    values_dict = {}
            except Exception:
                values_dict = {}
        else:
            values_dict = resp.get("values", resp) if isinstance(resp, dict) else {}

        if not isinstance(values_dict, dict):
            return sqm_instances

        for section_id, values in values_dict.items():
            if isinstance(values, dict) and values.get(".type") == "queue":
                sqm_instances.append(
                    SqmStatus(
                        section_id=section_id,
                        name=values.get("name", section_id),
                        enabled=values.get("enabled") == "1",
                        interface=values.get("interface", ""),
                        download=int(values.get("download", "0")),
                        upload=int(values.get("upload", "0")),
                        qdisc=values.get("qdisc", ""),
                        script=values.get("script", ""),
                    ),
                )
        return sqm_instances

    async def set_sqm_config(self, section_id: str, **kwargs: Any) -> bool:
        """Set SQM configuration via LuCI RPC."""
        try:
            for key, value in kwargs.items():
                val_str = (
                    "1" if value is True else "0" if value is False else str(value)
                )
                await self._rpc_call("uci", "set", ["sqm", section_id, key, val_str])
            await self._rpc_call("uci", "commit", ["sqm"])
            await self._rpc_call("sys", "exec", ["/etc/init.d/sqm reload"])
            self._invalidate_slow_poll()
            return True
        except Exception as err:
            _LOGGER.exception("Failed to set SQM config via LuCI RPC: %s", err)
            return False

    async def get_nlbwmon_data(self) -> dict[str, NlbwmonTraffic]:
        """Get bandwidth usage per MAC from nlbwmon via LuCI RPC."""
        # Try ubus first if available
        try:
            result = await self._rpc_call(
                "ubus", "call", ["nlbwmon", "get_data", {"group_by": "mac"}]
            )
            if result and "data" in result:
                traffic = {}
                for mac, data in result["data"].items():
                    mac_upper = mac.upper()
                    traffic[mac_upper] = NlbwmonTraffic(
                        mac=mac_upper,
                        rx_bytes=data.get("rx", 0),
                        tx_bytes=data.get("tx", 0),
                        rx_packets=data.get("rx_packets", 0),
                        tx_packets=data.get("tx_packets", 0),
                    )
                return traffic
        except Exception:
            pass

        # Fallback to sys.exec
        try:
            out = await self._rpc_call("sys", "exec", ["nlbw -c json -g mac"])
            if out:
                import json

                result = json.loads(out)
                if result and "data" in result:
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
            _LOGGER.debug("Failed to get nlbwmon data via LuCI RPC: %s", err)
        return {}

    async def get_wifi_credentials(self) -> list[WifiCredentials]:
        """Get wifi credentials via LuCI RPC."""
        try:
            # We use uci.get_all(["wireless"]) for LuCI RPC
            data = await self._rpc_call("uci", "get_all", ["wireless"])
            if not isinstance(data, dict):
                return []

            creds = []
            for name, val in data.items():
                if (
                    isinstance(val, dict)
                    and val.get(".type") == "wifi-iface"
                    and val.get("mode") == "ap"
                ):
                    creds.append(
                        WifiCredentials(
                            iface=name,
                            ssid=val.get("ssid", ""),
                            encryption=val.get("encryption", "none"),
                            key=val.get("key", ""),
                            hidden=bool(int(val.get("hidden", 0))),
                        )
                    )
            return creds
        except Exception as err:
            _LOGGER.debug("Failed to get wifi credentials via luci_rpc: %s", err)
            return []

    async def set_led(self, name: str, brightness: int) -> bool:
        """Set LED brightness via LuCI RPC.

        This method sets the trigger to 'none' before writing the brightness
        to ensure manual control is respected by the kernel.
        """
        try:
            # First ensure trigger is set to none to allow manual control
            await self.execute_command(
                f"echo none > /sys/class/leds/{name}/trigger 2>/dev/null"
            )
            # Write brightness
            await self.execute_command(
                f"echo {int(brightness)} > /sys/class/leds/{name}/brightness"
            )
            self._invalidate_slow_poll()
            return True
        except Exception as err:
            _LOGGER.debug("Failed to set LED %s via luci_rpc: %s", name, err)
            return False

    async def get_services(self) -> list[ServiceInfo]:
        """Get list of system services via ubus rc list."""
        services: list[ServiceInfo] = []

        # 'service list' tells daemons (which own procd instances) apart from
        # one-shot scripts, which procd always reports as not running.
        service_map = _extract_json(
            await self.execute_command("ubus call service list 2>/dev/null")
        )

        # 'rc list' is more reliable as it shows both enabled and running state
        rc_map = _extract_json(
            await self.execute_command("ubus call rc list 2>/dev/null")
        )
        for name, val in rc_map.items():
            if isinstance(val, dict):
                services.append(classify_service(name, val, service_map.get(name)))

        # Fallback to 'service list' alone if 'rc list' was empty
        if not services:
            for name, val in service_map.items():
                if isinstance(val, dict) and "instances" in val:
                    services.append(classify_service(name, {}, val))

        return services
