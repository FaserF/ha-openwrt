# mypy: disable-error-code="attr-defined"
from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

from ..base import (
    PROVISION_SCRIPT_TEMPLATE,
    OpenWrtClient,
    OpenWrtPackages,
    OpenWrtPermissions,
)
from .devices import LuciRpcDevicesMixin
from .exceptions import *
from .features import LuciRpcFeaturesMixin
from .network import LuciRpcNetworkMixin
from .system import LuciRpcSystemMixin

_LOGGER = logging.getLogger(__name__)


class LuciRpcClient(
    LuciRpcSystemMixin,
    LuciRpcNetworkMixin,
    LuciRpcDevicesMixin,
    LuciRpcFeaturesMixin,
    OpenWrtClient,
):
    """Client for OpenWrt LuCI RPC API."""

    def __init__(
        self,
        hass: Any,
        session: Any,
        host: str,
        username: str,
        password: str,
        port: int = 80,
        use_ssl: bool = False,
        verify_ssl: bool = False,
        dhcp_software: str = "auto",
        trust_stale_arp: bool = True,
        trust_bridge_fdb: bool = True,
    ) -> None:
        """Initialize the LuCI RPC client."""
        super().__init__(
            hass,
            session,
            host,
            username,
            password,
            port,
            use_ssl,
            verify_ssl,
            dhcp_software,
            trust_stale_arp,
            trust_bridge_fdb,
        )
        self._auth_token: str = ""

        self._rpc_id: int = 0
        self._semaphore = asyncio.Semaphore(5)

    @property
    def _base_url(self) -> str:
        """Return base URL for LuCI."""
        scheme = "https" if self.use_ssl else "http"
        return f"{scheme}://{self.host}:{self.port}"

    async def _rpc_call(
        self,
        endpoint: str,
        method: str,
        params: list[Any] | None = None,
        reauthenticated: bool = False,
    ) -> Any:
        """Make a LuCI JSON-RPC call."""
        if self.session is None:
            raise LuciRpcError("Session not initialized")
        session = self.session
        self._rpc_id += 1

        url = f"{self._base_url}/cgi-bin/luci/rpc/{endpoint}"
        if self._auth_token:
            url += f"?auth={self._auth_token}"

        payload = {
            "id": self._rpc_id,
            "method": method,
            "params": params or [],
        }

        reauth_needed = False
        data: dict[str, Any] = {}
        try:
            async with self._semaphore:
                async with session.post(
                    url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                    ssl=self.verify_ssl if self.use_ssl else False,
                ) as response:
                    if response.status == 403:
                        if self._auth_token and not reauthenticated:
                            reauth_needed = True
                        else:
                            msg = f"Access denied to LuCI RPC on {self.host}"
                            raise LuciRpcError(msg)
                    else:
                        response.raise_for_status()

                        # Check content type to ensure it's JSON
                        content_type = response.headers.get("Content-Type", "").lower()
                        if "application/json" not in content_type:
                            text = await response.text()
                            if "<html" in text.lower():
                                _LOGGER.debug(
                                    "Received HTML instead of JSON from LuCI RPC: %s",
                                    text[:200],
                                )
                                msg = "LuCI RPC returned HTML instead of JSON. Is 'luci-mod-rpc' installed?"
                                raise LuciRpcPackageMissingError(msg)
                            msg = (
                                f"Unexpected content type from LuCI RPC: {content_type}"
                            )
                            raise LuciRpcError(msg)

                        data = await response.json()

            if reauth_needed:
                self._auth_token = ""
                await self.connect()
                return await self._rpc_call(
                    endpoint,
                    method,
                    params,
                    reauthenticated=True,
                )

        except TimeoutError as err:
            msg = f"Timeout communicating with LuCI on {self.host}"
            raise LuciRpcTimeoutError(msg) from err
        except aiohttp.ClientConnectorError as err:
            msg = f"Cannot connect to LuCI on {self.host}: {err}"
            raise LuciRpcConnectionError(msg) from err
        except aiohttp.ClientSSLError as err:
            msg = f"SSL error connecting to LuCI on {self.host}: {err}"
            raise LuciRpcSslError(msg) from err
        except aiohttp.ClientError as err:
            if not reauthenticated:
                _LOGGER.debug(
                    "LuCI RPC connection error (%s), retrying after session reset",
                    err,
                )
                await self.disconnect()
                return await self._rpc_call(
                    endpoint,
                    method,
                    params,
                    reauthenticated=True,
                )
            self._connected = False
            msg = f"Communication error: {err}"
            raise LuciRpcError(msg) from err

        return data.get("result")

    async def execute_command(self, command: str) -> str:
        """Execute a command via LuCI RPC sys.exec with fallback to ubus file.exec."""
        # 1. Try sys.exec
        try:
            escaped_cmd = command.replace("'", "'\\''")
            out = await self._rpc_call(
                "sys", "exec", [f"/bin/sh -c '{escaped_cmd}' 2>&1"]
            )
            if out and out.strip():
                return out
        except (
            LuciRpcTimeoutError,
            LuciRpcConnectionError,
            LuciRpcSslError,
            LuciRpcAuthError,
        ):
            raise
        except Exception as err:
            _LOGGER.debug(
                "Command failed via LuCI RPC sys.exec: %s. Trying fallback.", err
            )

        # 2. Fallback to LuCI RPC ubus call -> file:exec
        try:
            res = await self._rpc_call(
                "ubus",
                "call",
                [
                    "file",
                    "exec",
                    {"command": "/bin/sh", "params": ["-c", command.strip()]},
                ],
            )
            if isinstance(res, dict):
                stdout = str(res.get("stdout") or "").strip()
                stderr = str(res.get("stderr") or "").strip()
                if stderr and not stdout:
                    _LOGGER.debug("LuCI RPC file.exec stderr: %s", stderr)
                return stdout or stderr
        except (
            LuciRpcTimeoutError,
            LuciRpcConnectionError,
            LuciRpcSslError,
            LuciRpcAuthError,
        ):
            raise
        except Exception as err:
            _LOGGER.debug(
                "Command failed via LuCI RPC ubus file.exec fallback: %s", err
            )

        return ""

    async def file_exec(
        self, command: str, params: list[str] | None = None
    ) -> dict[str, Any]:
        """Execute a binary via the existing sys.exec/shell path on LuCI RPC.

        Calling file.exec directly with an arbitrary binary fails unless that binary
        is explicitly listed in the rpcd file ACL. Routing through execute_command()
        uses /bin/sh (which IS in the ACL) and avoids that restriction.
        """
        import shlex

        parts = [command] + (params or [])
        cmd = " ".join(shlex.quote(p) for p in parts)
        output = await self.execute_command(f"{cmd}; echo __HA_RC__$?")
        if not output:
            return {}
        # Use partition() rather than splitlines() so the sentinel is found even when
        # the command output does not end with a newline (e.g. nlbw outputs compact
        # JSON without a trailing \n, causing the sentinel to land on the same line).
        rc = 0
        if "__HA_RC__" in output:
            body, _, rc_part = output.partition("__HA_RC__")
            try:
                rc = int(rc_part.strip())
            except ValueError:
                rc = 1
            stdout = body.rstrip("\n")
        else:
            stdout = output.strip()
        # execute_command merges stdout+stderr; classify permission errors as stderr
        # so callers can distinguish them from normal (possibly empty-stdout) output.
        lower = stdout.lower()
        if "permission denied" in lower or "access denied" in lower:
            return {"code": rc or 1, "stdout": "", "stderr": stdout}
        return {"code": rc, "stdout": stdout, "stderr": ""}

    async def read_file(self, path: str) -> str | None:
        """Read a file via LuCI RPC (cat through sys.exec)."""
        import shlex

        out = await self.execute_command(f"cat {shlex.quote(path)} 2>/dev/null")
        return out if out else None

    async def user_exists(self, username: str) -> bool:
        """Check if a system user exists on the device."""
        # 1. Try via LuCI RPC (often more restricted than ubus, but let's try reading passwd)
        try:
            res = await self.execute_command(
                f"grep -q '^{username}:' /etc/passwd && echo 'exists'",
            )
            if res and isinstance(res, str) and "exists" in res:
                return True
        except Exception:
            pass

        # 2. Fallback to base method
        return await super().user_exists(username)

    async def provision_user(
        self,
        username: str,
        password: str,
    ) -> tuple[bool, str | None]:
        """Create a dedicated system user and configure RPC permissions via LuCI RPC."""
        # Use the harmonized provisioning script from base
        script = PROVISION_SCRIPT_TEMPLATE.format(username=username, password=password)
        try:
            output = await self.execute_command(script)

            if output is None:
                output = ""

            if output:
                _LOGGER.debug(
                    "Provisioning output for %s via LuCI RPC: %s",
                    username,
                    output,
                )

            if "Provisioning SUCCESS" in output:
                return True, None

            if "LOG: FAIL:" in output:
                fail_msg = output.split("LOG: FAIL:")[1].splitlines()[0].strip()
                _LOGGER.error("Provisioning failed via LuCI RPC: %s", fail_msg)
                return False, fail_msg

            # Empty output usually means permission denied (sys.exec)
            if not output:
                _LOGGER.warning(
                    "Provisioning for %s returned empty output. "
                    "This typically means the current user ('%s') lacks "
                    "'sys.exec' RPC permission. "
                    "Provisioning must be run as 'root'.",
                    username,
                    self.username,
                )
                return (
                    False,
                    (
                        f"Provisioning failed: empty response from LuCI sys.exec. "
                        f"Ensure '{self.username}' has sys.exec permission, "
                        "or run provisioning as 'root'."
                    ),
                )

            return (
                False,
                "Provisioning script returned failure without specific error via LuCI RPC. Check router logs (logread).",
            )
        except LuciRpcError as err:
            msg = (
                f"Provisioning failed: '{self.username}' lacks 'sys.exec' RPC permission. "
                "Switch to 'root' or grant exec rights to this user."
            )
            _LOGGER.error("%s (%s)", msg, err)
            return False, msg
        except Exception as err:
            _LOGGER.exception(
                "Failed to provision user %s via LuCI RPC: %s", username, err
            )
            return False, str(err)

    async def connect(self) -> bool:
        """Authenticate with LuCI."""
        try:
            return await self._connect()
        except Exception as err:
            self._last_connect_error = err
            raise

    async def _connect(self) -> bool:
        """Authenticate with LuCI."""
        if self.session is None:
            raise LuciRpcError("Session not initialized")
        session = self.session
        self._rpc_id += 1

        url = f"{self._base_url}/cgi-bin/luci/rpc/auth"
        payload = {
            "id": self._rpc_id,
            "method": "login",
            "params": [self.username, self.password],
        }

        try:
            async with session.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json"},
                ssl=self.verify_ssl if self.use_ssl else False,
            ) as response:
                if response.status == 404:
                    msg = (
                        "LuCI RPC auth endpoint not found. Is 'luci-mod-rpc' installed?"
                    )
                    raise LuciRpcPackageMissingError(
                        msg,
                    )
                response.raise_for_status()

                # Check content type to ensure it's JSON
                content_type = response.headers.get("Content-Type", "").lower()
                if "application/json" not in content_type:
                    text = await response.text()
                    if "<html" in text.lower():
                        _LOGGER.debug(
                            "Received HTML instead of JSON from LuCI Auth: %s",
                            text[:200],
                        )
                        msg = "LuCI Auth returned HTML instead of JSON. Is 'luci-mod-rpc' installed?"
                        raise LuciRpcPackageMissingError(
                            msg,
                        )
                    msg = f"Unexpected content type from LuCI Auth: {content_type}"
                    raise LuciRpcError(
                        msg,
                    )

                data = await response.json()
        except TimeoutError as err:
            msg = f"Login timeout for LuCI on {self.host}"
            raise LuciRpcTimeoutError(msg) from err
        except aiohttp.ClientConnectorError as err:
            msg = f"Cannot connect to LuCI: {err}"
            raise LuciRpcConnectionError(msg) from err
        except aiohttp.ClientSSLError as err:
            msg = f"SSL error connecting to LuCI: {err}"
            raise LuciRpcSslError(msg) from err
        except aiohttp.ClientError as err:
            msg = f"Cannot connect: {err}"
            raise LuciRpcError(msg) from err

        result = data.get("result")
        if (
            result is None
            or result == "null"
            or (isinstance(result, str) and not result)
        ):
            _LOGGER.error("LuCI RPC auth returned no token: %s", data)
            msg = f"Authentication failed for {self.username}@{self.host}. Check credentials."
            raise LuciRpcAuthError(
                msg,
            )

        self._auth_token = result
        self._connected = True
        _LOGGER.debug("Authenticated with LuCI on %s", self.host)
        return True

    async def disconnect(self) -> None:
        """Disconnect and cleanup."""
        # Shared session managed by HA, no need to close
        self._connected = False

    async def check_permissions(self) -> OpenWrtPermissions:
        """Check what permissions the current user has."""
        from ..base import OpenWrtPermissions

        perms = OpenWrtPermissions()

        async def can_read_uci(config: str) -> bool:
            try:
                await self._rpc_call("uci", "get_all", [config])
                return True
            except LuciRpcError as err:
                return "Access denied" not in str(err)

        async def can_write_uci(config: str) -> bool:
            try:
                # Calling set with missing args to test permission before validation
                await self._rpc_call("uci", "set", [config])
                return True
            except LuciRpcError as err:
                return "Access denied" not in str(err)

        perms.read_system = await can_read_uci("system")
        perms.write_system = await can_write_uci("system")
        perms.read_network = await can_read_uci("network")
        perms.write_network = await can_write_uci("network")
        perms.read_firewall = await can_read_uci("firewall")
        perms.write_firewall = await can_write_uci("firewall")
        perms.read_wireless = await can_read_uci("wireless")
        perms.write_wireless = await can_write_uci("wireless")
        perms.read_sqm = await can_read_uci("sqm")
        perms.write_sqm = await can_write_uci("sqm")
        perms.read_vpn = perms.read_network
        perms.write_vpn = perms.write_network
        perms.read_mwan = await can_read_uci("mwan3")
        perms.read_led = perms.read_system
        perms.write_led = perms.write_system
        perms.read_devices = await can_read_uci("dhcp") or perms.read_network

        try:
            await self._rpc_call("sys", "exec", ["ls"])
            perms.read_services = True
            perms.write_services = True
            perms.write_devices = True
            perms.write_mqtt = True
        except LuciRpcError as err:
            denied = "Access denied" in str(err)
            perms.read_services = not denied
            perms.write_services = not denied
            perms.write_devices = not denied
            perms.write_mqtt = not denied

        perms.write_access_control = perms.write_firewall
        perms.read_batman = perms.read_services
        return perms

    async def check_packages(self) -> OpenWrtPackages:
        """Check installed packages with multiple fallbacks."""
        packages = OpenWrtPackages()
        # Step 1: Check via ubus call (if ubus is available via sys.exec)
        try:
            ubus_list = await self.execute_command("ubus list")
            objects = ubus_list.splitlines() if ubus_list else []

            if "sqm" in objects:
                packages.sqm_scripts = True
            if "mwan3" in objects:
                packages.mwan3 = True
            if "luci" in objects or "luci-rpc" in objects:
                packages.luci_mod_rpc = True
            if "upnp" in objects:
                packages.miniupnpd = True
            if "nlbwmon" in objects:
                packages.nlbwmon = True
            if "pbr" in objects:
                packages.pbr = True
            if "dhcp" in objects:
                # Specifically check for ipv4leases method
                try:
                    dhcp_info = await self.execute_command("ubus list dhcp")
                    if "ipv4leases" in dhcp_info:
                        packages.dhcp = True
                    else:
                        packages.dhcp = False
                except Exception:
                    packages.dhcp = True
            if "network.wireless" in objects or "iwinfo" in objects:
                packages.wireless = True
            # Check for hostapd objects as proof of wireless capability
            if any(obj.startswith("hostapd.") for obj in objects):
                packages.wireless = True
            if "lldp" in objects:
                packages.lldp = True
            if "batman-adv" in objects:
                packages.batman_adv = True
            if "batctl" in objects:
                packages.batctl = True
        except Exception:
            pass

        # Step 2: Check via file existence (sys.exec)
        # Index map (0-based):
        #  0: /etc/init.d/sqm             -> sqm_scripts
        #  1: /etc/init.d/mwan3           -> mwan3
        #  2: /usr/bin/iwinfo             -> iwinfo
        #  3: /usr/bin/etherwake          -> etherwake
        #  4: /usr/bin/wg                 -> wireguard
        #  5: /usr/sbin/openvpn           -> openvpn
        #  6: luci-mod-rpc (lua)          -> luci_mod_rpc
        #  7: luci-mod-rpc (menu.d)       -> luci_mod_rpc
        #  8: asu (lua)                   -> asu
        #  9: asu (menu.d)                -> asu
        # 10: /etc/init.d/adblock         -> adblock
        # 11: /etc/init.d/simple-adblock  -> simple_adblock
        # 12: /etc/init.d/ban-ip          -> ban_ip
        # 13: /etc/init.d/miniupnpd      -> miniupnpd
        # 14: /etc/init.d/nlbwmon        -> nlbwmon
        # 15: /etc/init.d/pbr            -> pbr
        # 16: /etc/init.d/adguardhome   -> adguardhome
        # 17: /etc/init.d/unbound       -> unbound
        # 18: /etc/init.d/odhcpd        -> dhcp (fallback)
        # 19: /etc/init.d/lldpd         -> lldp
        cmd = (
            "for f in /etc/init.d/sqm /etc/init.d/mwan3 /usr/bin/iwinfo "
            "/usr/bin/etherwake /usr/bin/wg /usr/sbin/openvpn "
            "/usr/lib/lua/luci/controller/rpc.lua "
            "/usr/share/luci/menu.d/luci-mod-rpc.json "
            "/usr/lib/lua/luci/controller/attendedsysupgrade.lua "
            "/usr/share/luci/menu.d/luci-app-attendedsysupgrade.json "
            "/etc/init.d/adblock "
            "/etc/init.d/simple-adblock "
            "/etc/init.d/ban-ip "
            "/etc/init.d/miniupnpd "
            "/etc/init.d/nlbwmon "
            "/etc/init.d/pbr "
            "/etc/init.d/adguardhome "
            "/etc/init.d/unbound "
            "/etc/init.d/odhcpd "
            "/etc/init.d/lldpd "
            "/usr/sbin/batctl "
            "/sys/module/batman_adv "
            "/usr/bin/stty /bin/stty /usr/bin/timeout /bin/timeout; do "
            "if [ -e $f ]; then echo 1; else echo 0; fi; done"
        )
        out = await self._rpc_call("sys", "exec", [cmd])
        if out:
            results = out.strip().splitlines()

            def detect_status(idx: int) -> bool:
                return len(results) > idx and results[idx].strip() == "1"

            if packages.sqm_scripts is not True:
                packages.sqm_scripts = detect_status(0)
            if packages.mwan3 is not True:
                packages.mwan3 = detect_status(1)
            if packages.iwinfo is not True:
                packages.iwinfo = detect_status(2)
            if packages.etherwake is not True:
                packages.etherwake = detect_status(3)
            if packages.wireguard is not True:
                packages.wireguard = detect_status(4)
            if packages.openvpn is not True:
                packages.openvpn = detect_status(5)
            if packages.luci_mod_rpc is not True:
                packages.luci_mod_rpc = (
                    detect_status(6) or detect_status(7) or (len(objects) > 0)
                )
            if packages.asu is not True:
                packages.asu = detect_status(8) or detect_status(9)
            if packages.adblock is not True:
                packages.adblock = detect_status(10)
            if packages.simple_adblock is not True:
                packages.simple_adblock = detect_status(11)
            if packages.ban_ip is not True:
                packages.ban_ip = detect_status(12)
            if packages.miniupnpd is not True:
                packages.miniupnpd = detect_status(13)
            if packages.nlbwmon is not True:
                packages.nlbwmon = detect_status(14)
            if packages.pbr is not True:
                packages.pbr = detect_status(15)
            if packages.adguardhome is not True:
                packages.adguardhome = detect_status(16)
            if packages.unbound is not True:
                packages.unbound = detect_status(17)
            if packages.dhcp is not True:
                packages.dhcp = detect_status(18)
            if packages.lldp is not True:
                packages.lldp = detect_status(19)
            if packages.batctl is not True:
                packages.batctl = detect_status(20)
            if packages.batman_adv is not True:
                packages.batman_adv = detect_status(21)
            packages.stty = detect_status(22) or detect_status(23)
            packages.timeout = detect_status(24) or detect_status(25)

        # Step 3: Check UCI configs for remaining packages (very robust fallback)
        if packages.sqm_scripts is not True:
            try:
                res = await self._rpc_call("uci", "get_all", ["sqm"])
                if res and isinstance(res, dict):
                    packages.sqm_scripts = True
            except Exception:
                pass

        if packages.mwan3 is not True:
            try:
                res = await self._rpc_call("uci", "get_all", ["mwan3"])
                if res and isinstance(res, dict):
                    packages.mwan3 = True
            except Exception:
                pass

        if packages.openvpn is not True:
            try:
                res = await self._rpc_call("uci", "get_all", ["openvpn"])
                if res and isinstance(res, dict):
                    packages.openvpn = True
            except Exception:
                pass

        if packages.wireguard is not True:
            try:
                res = await self._rpc_call("uci", "get_all", ["network"])
                if (
                    res
                    and isinstance(res, dict)
                    and any(
                        v.get("proto") == "wireguard"
                        for v in res.values()
                        if isinstance(v, dict)
                    )
                ):
                    packages.wireguard = True
            except Exception:
                pass

        # Step 4: Fallback to get_installed_packages (full list check)
        installed = await self.get_installed_packages()
        if installed:
            mapping = {
                "sqm_scripts": "sqm-scripts",
                "mwan3": "mwan3",
                "iwinfo": "iwinfo",
                "etherwake": "etherwake",
                "wireguard": "wireguard",
                "openvpn": "openvpn",
                "luci_mod_rpc": "luci-mod-rpc",
                "asu": "luci-app-attendedsysupgrade",
                "adblock": "adblock",
                "simple_adblock": "simple-adblock",
                "ban_ip": "ban-ip",
            }
            for attr, pkg in mapping.items():
                if getattr(packages, attr) is not True:
                    if pkg in ["wireguard", "openvpn"]:
                        setattr(
                            packages,
                            attr,
                            any(pkg in p for p in installed),
                        )
                    elif attr == "asu":
                        setattr(
                            packages,
                            attr,
                            any(
                                p in installed
                                for p in [
                                    "luci-app-attendedsysupgrade",
                                    "attendedsysupgrade-common",
                                ]
                            ),
                        )
                    else:
                        setattr(packages, attr, pkg in installed)

        # Final pass: Initialize remaining to False (to avoid staying at None)
        import dataclasses

        # Infer wireless support if iwinfo is present (crucial fallback for restricted rpc)
        if packages.wireless is None and packages.iwinfo:
            packages.wireless = True

        for field in dataclasses.fields(packages):
            if getattr(packages, field.name) is None:
                setattr(packages, field.name, False)

        return packages
