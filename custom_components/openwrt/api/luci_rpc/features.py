# mypy: disable-error-code="attr-defined"
from __future__ import annotations

import json
import logging
from typing import Any

from ..base import (
    AccessControl,
    AdBlockStatus,
    FirewallRedirect,
    FirewallRule,
    LedInfo,
    NlbwmonTraffic,
    ServiceInfo,
    SimpleAdBlockStatus,
    SqmStatus,
    WifiCredentials,
    WpsStatus,
)
from .exceptions import *

_LOGGER = logging.getLogger(__name__)


class LuciRpcFeaturesMixin:
    """Features methods for LuciRpcClient."""

    async def get_leds(self) -> list:
        """Get LEDs from /sys/class/leds via sys.exec."""
        from ..base import LedInfo

        leds: list[LedInfo] = []
        cmd = (
            "for led in /sys/class/leds/*/; do "
            'name=$(basename "$led"); '
            'brightness=$(cat "$led/brightness" 2>/dev/null || echo 0); '
            'max=$(cat "$led/max_brightness" 2>/dev/null || echo 255); '
            'trigger=$(cat "$led/trigger" 2>/dev/null | tr " " "\\n" | grep "^\\[" | tr -d "[]" || echo none); '
            'echo "$name|$brightness|$max|$trigger"; '
            "done"
        )
        output = await self._rpc_call("sys", "exec", [cmd])
        if output:
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

    async def install_firmware(self, url: str, keep_settings: bool = True) -> None:
        """Install firmware from the given URL via LuCI RPC."""
        keep = "" if keep_settings else "-n"
        cmd = f"wget --no-check-certificate -O /tmp/firmware.bin '{url}' && sysupgrade {keep} /tmp/firmware.bin"
        try:
            _LOGGER.info("Initiating firmware installation via LuCI RPC from: %s", url)
            await self.execute_command(cmd)
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
                    "LuCI RPC connection lost during sysupgrade - device is rebooting",
                )
            else:
                _LOGGER.exception("Failed to execute sysupgrade via LuCI RPC: %s", err)
                msg = f"sysupgrade execution failed: {err}"
                raise LuciRpcError(msg) from err

    async def download_file(self, remote_path: str, local_path: str) -> bool:
        """Download a file from the router via LuCI RPC file.read."""
        try:
            import base64

            # Try LuCI file.read first
            try:
                res = await self._rpc_call("file", "read", [remote_path])
                if res and isinstance(res, str):
                    with open(local_path, "wb") as f:
                        f.write(base64.b64decode(res))
                    return True
            except (
                LuciRpcTimeoutError,
                LuciRpcConnectionError,
                LuciRpcSslError,
                LuciRpcAuthError,
            ):
                raise
            except LuciRpcError:
                # Fallback to sys.exec if file read is not available
                # Fabian's AX3600 has openssl but no standalone base64 command
                cmd = f"openssl base64 -in {remote_path} || base64 {remote_path} || cat {remote_path} | base64"
                output = await self.execute_command(cmd)
                if output:
                    with open(local_path, "wb") as f:
                        f.write(
                            base64.b64decode(output.replace("\n", "").replace("\r", ""))
                        )
                    return True
        except (
            LuciRpcTimeoutError,
            LuciRpcConnectionError,
            LuciRpcSslError,
            LuciRpcAuthError,
        ):
            raise
        except Exception as err:
            _LOGGER.exception("Failed to download file via LuCI RPC: %s", err)
        return False

    async def get_firewall_rules(self) -> list[FirewallRule]:
        """Get firewall rules via LuCI RPC UCI."""
        rules: list[FirewallRule] = []
        data = await self._rpc_call("uci", "get_all", ["firewall"])
        if not isinstance(data, dict):
            return []

        for section_id, val in data.items():
            if isinstance(val, dict) and val.get(".type") == "rule":
                display_id = section_id
                if section_id.startswith("cfg"):
                    rule_sects = [
                        k
                        for k, v in data.items()
                        if isinstance(v, dict) and v.get(".type") == "rule"
                    ]
                    try:
                        idx = rule_sects.index(section_id)
                        display_id = f"@rule[{idx}]"
                    except ValueError:
                        pass

                try:
                    enabled = bool(int(val.get("enabled", "1")))
                except (ValueError, TypeError):
                    enabled = True
                rules.append(
                    FirewallRule(
                        section_id=display_id,
                        name=str(val.get("name") or display_id),
                        enabled=enabled,
                        src=val.get("src", ""),
                        dest=val.get("dest", ""),
                        target=val.get("target", "REJECT"),
                    )
                )
        return rules
        return rules

    async def set_firewall_rule_enabled(self, section_id: str, enabled: bool) -> bool:
        """Enable or disable a firewall rule via UCI over LuCI RPC."""
        try:
            val = "1" if enabled else "0"
            cmd = f"uci set firewall.{section_id}.enabled='{val}' && uci commit firewall && /etc/init.d/firewall reload"
            await self.execute_command(cmd)
            self._last_full_poll = 0
            return True
        except Exception as err:
            _LOGGER.exception("Failed to set firewall rule via LuCI RPC: %s", err)
            return False

    async def get_firewall_redirects(self) -> list[FirewallRedirect]:
        """Get firewall port forwarding redirects via LuCI RPC UCI."""
        redirects: list[FirewallRedirect] = []
        data = await self._rpc_call("uci", "get_all", ["firewall"])
        if not isinstance(data, dict):
            return []

        for section_id, val in data.items():
            if isinstance(val, dict) and val.get(".type") == "redirect":
                display_id = section_id
                if section_id.startswith("cfg"):
                    redirect_sects = [
                        k
                        for k, v in data.items()
                        if isinstance(v, dict) and v.get(".type") == "redirect"
                    ]
                    try:
                        idx = redirect_sects.index(section_id)
                        display_id = f"@redirect[{idx}]"
                    except ValueError:
                        pass

                redirects.append(
                    FirewallRedirect(
                        section_id=display_id,
                        name=str(val.get("name") or display_id),
                        enabled=str(val.get("enabled", "1")) == "1",
                        external_port=val.get("src_dport", ""),
                        target_ip=val.get("dest_ip", ""),
                        target_port=val.get("dest_port", ""),
                        protocol=val.get("proto", "tcp"),
                    )
                )
        return redirects
        return redirects

    async def get_firewall_rules_uci_not_used(self) -> list[FirewallRule]:
        """Deprecated."""
        return []

    async def get_access_control(self) -> list[AccessControl]:
        """Get access control rules via LuCI RPC UCI."""
        rules: list[AccessControl] = []
        # OpenWrt Parental Control usually uses firewall rules with ha_acl_ prefix
        data = await self._rpc_call("uci", "get_all", ["firewall"])
        if not isinstance(data, dict):
            return []

        for _section_id, val in data.items():
            if (
                isinstance(val, dict)
                and val.get(".type") == "rule"
                and val.get("name", "").startswith("ha_acl_")
            ):
                rules.append(
                    AccessControl(
                        mac=val.get("src_mac", "").upper(),
                        name=val.get("name", "").replace("ha_acl_", ""),
                        blocked=(val.get("target") == "REJECT"),
                        section_id=_section_id,
                    )
                )
        return rules
        return rules

    async def get_wps_status(self) -> WpsStatus:
        """Get WPS status via LuCI RPC."""
        if self.packages.wireless is False:
            return WpsStatus()
        try:
            # We check if hostapd is running and has wps enabled
            # This is hard via RPC, but we can check wireless config
            data = await self._rpc_call("uci", "get_all", ["wireless"])
            if not isinstance(data, dict):
                return WpsStatus()

            wps_enabled = False
            for val in data.values():
                if (
                    isinstance(val, dict)
                    and val.get(".type") == "wifi-iface"
                    and int(val.get("wps_pushbutton", "0")) == 1
                ):
                    wps_enabled = True
                    break

            return WpsStatus(enabled=wps_enabled)
        except Exception as err:
            _LOGGER.debug("Failed to get WPS status via luci_rpc: %s", err)
            return WpsStatus()

    async def set_firewall_redirect_enabled(
        self,
        section_id: str,
        enabled: bool,
    ) -> bool:
        """Enable or disable a firewall redirect via UCI over LuCI RPC."""
        try:
            val = "1" if enabled else "0"
            cmd = f"uci set firewall.{section_id}.enabled='{val}' && uci commit firewall && /etc/init.d/firewall reload"
            await self.execute_command(cmd)
            self._last_full_poll = 0
            return True
        except Exception as err:
            _LOGGER.exception("Failed to set firewall redirect via LuCI RPC: %s", err)
            return False

    async def get_adblock_status(self) -> AdBlockStatus:
        """Get adblock status via LuCI RPC."""
        from ..base import AdBlockStatus

        status = AdBlockStatus()
        # 1. Try ubus first (provides more details)
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
            _LOGGER.debug("AdBlock ubus status failed (LuCI RPC): %s", err)

        # 2. Fallback to uci (basic status)
        try:
            enabled = await self._rpc_call(
                "sys",
                "exec",
                ["uci -q get adblock.global.enabled"],
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
            self._last_full_poll = 0
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
                [f"uci set adblock.global.enabled='{val}' && uci commit adblock"],
            )
            action = "start" if enabled else "stop"
            await self._rpc_call("sys", "exec", [f"/etc/init.d/adblock {action}"])
            self._last_full_poll = 0
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
            self._last_full_poll = 0
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
            self._last_full_poll = 0
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
            self._last_full_poll = 0
            return True
        except Exception as err:
            _LOGGER.debug("Failed to set LED %s via luci_rpc: %s", name, err)
            return False

    async def get_services(self) -> list[ServiceInfo]:
        """Get list of system services via ubus rc list."""
        services: list[ServiceInfo] = []
        # 'rc list' is more reliable as it shows both enabled and running state
        stdout = await self.execute_command("ubus call rc list 2>/dev/null")
        if stdout:
            # Find the first { and last } to extract JSON
            start = stdout.find("{")
            end = stdout.rfind("}")
            if start != -1 and end != -1:
                try:
                    data = json.loads(stdout[start : end + 1])
                    for name, val in data.items():
                        if isinstance(val, dict):
                            services.append(
                                ServiceInfo(
                                    name=name,
                                    enabled=val.get("enabled", False),
                                    running=val.get("running", False)
                                    or (
                                        val.get("running") is False
                                        and val.get("exit_code") == 0
                                        and name
                                        in ("adblock", "simple-adblock", "sysctl")
                                    ),
                                )
                            )
                except json.JSONDecodeError:
                    pass

        # Fallback to 'service list' if 'rc list' was empty
        if not services:
            stdout = await self.execute_command("ubus call service list 2>/dev/null")
            if stdout:
                start = stdout.find("{")
                end = stdout.rfind("}")
                if start != -1 and end != -1:
                    try:
                        data = json.loads(stdout[start : end + 1])
                        for name, val in data.items():
                            if isinstance(val, dict) and "instances" in val:
                                running = any(
                                    inst.get("running", False)
                                    or (
                                        inst.get("running") is False
                                        and inst.get("exit_code") == 0
                                        and name
                                        in ("adblock", "simple-adblock", "sysctl")
                                    )
                                    for inst in val.get("instances", {}).values()
                                )
                                services.append(ServiceInfo(name=name, running=running))
                    except json.JSONDecodeError:
                        pass

        return services
