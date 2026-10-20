# mypy: disable-error-code="attr-defined"
from __future__ import annotations

import asyncio
import logging

from ..base import (
    AccessControl,
    FirewallRedirect,
    FirewallRule,
    LedInfo,
    WpsStatus,
)
from ..firmware_install import (
    run_detached_firmware_install,
)
from .exceptions import (
    LuciRpcAuthError,
    LuciRpcConnectionError,
    LuciRpcError,
    LuciRpcSslError,
    LuciRpcTimeoutError,
)

_LOGGER = logging.getLogger(__name__)


from .features_services import LuciRpcFeaturesServicesMixin


class LuciRpcFeaturesMixin(LuciRpcFeaturesServicesMixin):
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

    async def _firmware_exec(self, command: str) -> str:
        """Run a firmware job command via sys.exec, raising on any failure.

        Unlike execute_command, there is no file.exec fallback (which would
        run the command a second time) and failures are not turned into "".
        _rpc_call may re-send the request after a connection error; the launch
        command is safe to deliver twice (see build_detached_sysupgrade_command).
        """
        escaped_cmd = command.replace("'", "'\\''")
        out = await self._rpc_call("sys", "exec", [f"/bin/sh -c '{escaped_cmd}' 2>&1"])
        if not isinstance(out, str):
            msg = f"LuCI RPC sys.exec returned no output: {out!r}"
            raise LuciRpcError(msg)
        return out

    async def install_firmware(
        self,
        url: str,
        keep_settings: bool = True,
        force: bool = False,
        sha256: str = "",
    ) -> None:
        """Install firmware from the given URL via LuCI RPC.

        sys.exec has no timeout of its own, but uhttpd kills the LuCI request
        after its script_timeout (60 s by default) and answers 502, which
        _rpc_call retries and execute_command then hands to its fallback. So
        the install runs detached on the router and is polled; a timeout or
        lost contact before sysupgrade starts is an error, never a reboot.
        """
        _LOGGER.info("Initiating firmware installation via LuCI RPC from: %s", url)
        await run_detached_firmware_install(
            self._firmware_exec, url, keep_settings, force, sha256
        )

    async def download_file(self, remote_path: str, local_path: str) -> bool:
        """Download a file from the router via LuCI RPC."""
        try:
            import base64
            import shlex

            # 1. Try ubus file.read via LuCI RPC
            try:
                res = await self._rpc_call(
                    "ubus",
                    "call",
                    ["file", "read", {"path": remote_path, "base64": True}],
                )
                if res and isinstance(res, dict) and "data" in res:
                    data = base64.b64decode(res["data"])
                    await asyncio.to_thread(self._write_bytes, local_path, data)
                    return True
            except (
                LuciRpcTimeoutError,
                LuciRpcConnectionError,
                LuciRpcSslError,
                LuciRpcAuthError,
            ):
                raise
            except Exception as err:
                _LOGGER.debug("LuCI RPC ubus file.read failed: %s", err)

            # 2. Try legacy LuCI file.read (custom/older LuCI modules)
            try:
                res = await self._rpc_call("file", "read", [remote_path])
                if res and isinstance(res, str):
                    await asyncio.to_thread(
                        self._write_bytes, local_path, base64.b64decode(res)
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
                _LOGGER.debug("LuCI RPC file.read failed: %s", err)

            # 3. Fallback to sys.exec command with stderr redirection (#212)
            safe_path = shlex.quote(remote_path)
            cmd = (
                f"openssl base64 -in {safe_path} 2>/dev/null || "
                f"base64 {safe_path} 2>/dev/null || "
                f"cat {safe_path} 2>/dev/null | base64 2>/dev/null"
            )
            output = await self.execute_command(cmd)
            if output:
                lines = [
                    line.strip()
                    for line in output.splitlines()
                    if line.strip() and not line.strip().startswith("sh: ")
                ]
                clean_b64 = "".join(lines)
                if clean_b64:
                    data = base64.b64decode(clean_b64)
                    await asyncio.to_thread(self._write_bytes, local_path, data)
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
            self._invalidate_slow_poll()
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

    async def set_wps(self, enabled: bool) -> bool:
        """Enable or disable WPS via LuCI RPC."""
        method = "wps_start" if enabled else "wps_cancel"
        try:
            cmd = (
                f"for obj in $(ubus list 'hostapd.*' 2>/dev/null); do "
                f'ubus call "$obj" {method} 2>/dev/null; '
                f"done"
            )
            await self.execute_command(cmd)
            return True
        except Exception as err:
            _LOGGER.debug("Failed to set WPS via luci_rpc: %s", err)
            return False

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
            self._invalidate_slow_poll()
            return True
        except Exception as err:
            _LOGGER.exception("Failed to set firewall redirect via LuCI RPC: %s", err)
            return False
