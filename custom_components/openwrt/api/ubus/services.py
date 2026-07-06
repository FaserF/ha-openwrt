# mypy: disable-error-code="attr-defined"
from __future__ import annotations

import contextlib
import logging
from typing import Any

from ..base import (
    DiagnosticResult,
    OpenWrtPackages,
    OpenWrtPermissions,
    ServiceInfo,
)
from .exceptions import *

_LOGGER = logging.getLogger(__name__)
UBUS_JSONRPC_VERSION = "2.0"
UBUS_ID_AUTH = 1
UBUS_ID_CALL = 2


class UbusServicesMixin:
    """Services methods for UbusClient."""

    async def check_permissions(self) -> OpenWrtPermissions:
        """Check user permissions via ubus session list and uci tests."""
        if self.session is None:
            raise UbusError("Session not initialized")

        from ..base import OpenWrtPermissions

        perms = OpenWrtPermissions()
        try:
            # 1. Try to get permissions from session list (definitive)
            if await self._check_perms_from_session(perms):
                return perms

            # 2. Fallback to manual probes (trial and error)
            await self._check_perms_from_probes(perms)

        except Exception as err:
            _LOGGER.debug("Error checking permissions via ubus: %s", err)
            if self.connected:
                # Default safety fallbacks
                perms.read_system = True
                perms.read_network = True

        return perms

    async def _check_perms_from_session(self, perms: OpenWrtPermissions) -> bool:
        """Fetch and parse ACLs from the current ubus session."""
        with contextlib.suppress(Exception):
            session_info = await self._call("session", "list")
            if not isinstance(session_info, dict):
                return False

            # Check for modern 'acls' or legacy 'values.access'
            acls_all = session_info.get("acls", {})
            acls_ubus = acls_all.get("ubus", {})
            acls_uci = acls_all.get("uci", {})

            # Legacy fallback
            access = session_info.get("values", {}).get("access", {})

            if not acls_ubus and not acls_uci and not access:
                return False

            def has_perm(obj: str, method: str) -> bool:
                # Check ubus structure
                for pattern in (obj, "*"):
                    obj_acls = acls_ubus.get(pattern, [])
                    if method in obj_acls or "*" in obj_acls:
                        return True

                # Also check if it's in a wildcard group like 'hostapd.*'
                for pattern, methods in acls_ubus.items():
                    if pattern.endswith(".*") and obj.startswith(pattern[:-1]):
                        if method in methods or "*" in methods:
                            return True

                # Check legacy/values structure with pattern matching
                for pattern, methods in access.items():
                    if (
                        pattern in ("*", obj)
                        or (pattern.endswith("*") and obj.startswith(pattern[:-1]))
                    ) and ("*" in methods or method in methods):
                        return True
                return False

            def has_uci(config: str, method: str) -> bool:
                # Check uci structure (modern)
                for pattern in (config, "*"):
                    config_acls = acls_uci.get(pattern, [])
                    if method in config_acls or "*" in config_acls:
                        return True

                # Check legacy structure (fallback)
                for pattern in ("uci", config, "*"):
                    obj_access = access.get(pattern, {})
                    if isinstance(obj_access, dict):
                        if method in obj_access or "*" in obj_access:
                            return True
                return False

            perms.read_system = has_perm("system", "info") or has_uci("system", "read")
            perms.write_system = has_perm("system", "reboot") or has_uci(
                "system", "write"
            )
            perms.read_network = has_perm("network", "status") or has_uci(
                "network", "read"
            )
            perms.write_network = has_perm("network.interface", "up") or has_uci(
                "network", "write"
            )
            perms.read_firewall = has_perm("firewall", "status") or has_uci(
                "firewall", "read"
            )
            perms.write_firewall = has_uci("firewall", "write")
            perms.read_wireless = has_perm("iwinfo", "info") or has_uci(
                "wireless", "read"
            )
            perms.write_wireless = has_uci("wireless", "write")
            perms.read_sqm = has_uci("sqm", "read")
            perms.write_sqm = has_uci("sqm", "write")
            perms.read_vpn = perms.read_network
            perms.write_vpn = perms.write_network
            perms.read_mwan = has_uci("mwan3", "read")
            perms.read_led = has_perm("led", "list") or has_uci("system", "read")
            perms.write_led = has_perm("led", "set") or has_uci("system", "write")
            perms.read_devices = has_perm("network", "status") or has_uci(
                "dhcp", "read"
            )
            perms.write_devices = has_perm("sys", "exec")
            perms.read_services = has_perm("service", "list")
            perms.write_services = has_perm("service", "list")
            perms.write_access_control = perms.write_firewall
            perms.read_batman = has_perm("batman", "*") or has_perm("file", "exec")
            perms.write_mqtt = has_perm("file", "exec")

            return True
        return False

    async def _check_perms_from_probes(self, perms: OpenWrtPermissions) -> None:
        """Identify permissions by attempting to call various methods."""

        async def can_call(
            obj: str, method: str, params: dict[str, Any] | None = None
        ) -> bool:
            try:
                # We use a very light call to test permission
                if obj == "uci" and method == "get":
                    await self._call(obj, method, {"config": "system"})
                elif obj == "file" and method == "list":
                    await self._call(obj, method, {"path": "/etc/config"})
                else:
                    await self._call(obj, method, params or {})
                return True
            except UbusPermissionError:
                return False
            except Exception:
                # If it's another error (e.g. 'Not found'), we might still have permission
                # but the object or method is missing. For permission checking, we
                # only care about explicit 'Permission denied' (Access denied).
                return True

        # Root user override: If we are root, and the session list failed (handled in caller),
        # we assume read permissions for core objects as a starting point.
        is_root = self.username == "root"

        perms.read_system = await can_call("system", "board") or is_root
        perms.write_system = (
            await can_call("uci", "set", {"config": "system"}) or is_root
        )
        perms.read_network = await can_call("network.interface", "dump") or is_root
        perms.write_network = (
            await can_call("network.interface", "up", {"interface": "loopback"})
            or is_root
        )
        perms.read_firewall = (
            await can_call("uci", "get", {"config": "firewall"}) or is_root
        )
        perms.write_firewall = (
            await can_call("uci", "set", {"config": "firewall"}) or is_root
        )
        perms.read_wireless = await can_call("network.wireless", "status") or is_root
        perms.write_wireless = (
            await can_call("uci", "set", {"config": "wireless"}) or is_root
        )
        perms.read_sqm = await can_call("uci", "get", {"config": "sqm"}) or is_root
        perms.write_sqm = await can_call("uci", "set", {"config": "sqm"}) or is_root

        # LEDs often use the 'file' object to read /sys/class/leds
        has_file_read = await can_call("file", "list", {"path": "/sys/class/leds"})
        perms.read_led = (
            await can_call("uci", "get", {"config": "system"}) or has_file_read
        ) or is_root
        perms.write_led = (
            await can_call("uci", "set", {"config": "system"}) or has_file_read
        ) or is_root

        perms.read_vpn = perms.read_network
        perms.read_mwan = await can_call("uci", "get", {"config": "mwan3"}) or is_root
        perms.read_devices = await can_call("dhcp", "ipv4leases") or perms.read_network
        perms.write_devices = (
            await can_call("file", "exec", {"command": "/usr/bin/id"})
            or await can_call("file", "exec", {"command": "/bin/sh"})
            or is_root
        )
        perms.write_access_control = perms.write_firewall
        perms.read_batman = (
            await can_call("file", "exec", {"command": "/usr/sbin/batctl"}) or is_root
        )
        perms.read_services = await can_call("service", "list") or is_root
        perms.write_services = await can_call("service", "list") or is_root
        perms.write_mqtt = perms.write_devices

    async def check_packages(self) -> OpenWrtPackages:
        """Check installed packages."""
        packages = OpenWrtPackages()
        try:
            # Step 1: Check available ubus objects (very robust)
            objects = await self._list_objects()
            packages.iwinfo = "iwinfo" in objects
            packages.luci_mod_rpc = "luci-rpc" in objects
            if "mwan3" in objects:
                packages.mwan3 = True
            if "sqm" in objects:
                packages.sqm_scripts = True
            if "adblock" in objects:
                packages.adblock = True
            if "upnp" in objects:
                packages.miniupnpd = True
            if "nlbwmon" in objects:
                packages.nlbwmon = True
            if "pbr" in objects:
                packages.pbr = True
            if "dhcp" in objects:
                # Specifically check for ipv4leases method to avoid "Not found" on some setups
                try:
                    dhcp_methods = await self._get_object_methods("dhcp")
                    if "ipv4leases" in dhcp_methods:
                        packages.dhcp = True
                    else:
                        packages.dhcp = False
                except Exception:
                    packages.dhcp = (
                        True  # Fallback to True if check fails but object exists
                    )
            if "network.wireless" in objects or "iwinfo" in objects:
                packages.wireless = True
            # Check for hostapd objects as proof of wireless capability
            if any(obj.startswith("hostapd.") for obj in objects):
                packages.wireless = True
            if "lldp" in objects:
                packages.lldp = True

            # Step 2: Try executing a small script for remaining/all (fastest for root)
            # Index map (0-based):
            #  0: /etc/init.d/sqm          -> sqm_scripts
            #  1: /etc/init.d/mwan3        -> mwan3
            #  2: /usr/bin/iwinfo          -> iwinfo
            #  3: /usr/bin/etherwake       -> etherwake
            #  4: /usr/bin/wg              -> wireguard
            #  5: /usr/sbin/openvpn        -> openvpn
            #  6: luci-mod-rpc (lua)       -> luci_mod_rpc
            #  7: luci-mod-rpc (menu.d)    -> luci_mod_rpc
            #  8: asu (lua)                -> asu
            #  9: asu (menu.d)             -> asu
            # 10: /etc/init.d/adblock      -> adblock
            # 11: /etc/init.d/simple-adblock -> simple_adblock
            # 12: /etc/init.d/ban-ip       -> ban_ip
            # 13: /etc/init.d/miniupnpd   -> miniupnpd
            # 14: /etc/init.d/nlbwmon     -> nlbwmon
            # 15: /etc/init.d/pbr         -> pbr
            # 16: /etc/init.d/adguardhome -> adguardhome
            # 17: /etc/init.d/unbound     -> unbound
            # 18: /etc/config/sqm         -> sqm_scripts (fallback)
            try:
                cmd = (
                    "for f in /etc/init.d/sqm /etc/init.d/mwan3 /usr/bin/iwinfo "
                    "/usr/bin/etherwake /usr/bin/wg /usr/sbin/openvpn "
                    "/usr/lib/lua/luci/controller/rpc.lua "
                    "/usr/share/luci/menu.d/luci-mod-rpc.json "
                    "/usr/lib/lua/luci/controller/attendedsysupgrade.lua "
                    "/usr/share/luci/menu.d/luci-app-attendedsysupgrade.json "
                    "/etc/init.d/adblock "
                    "/etc/init.d/simple-adblock "
                    "/etc/init.d/banip "
                    "/etc/init.d/miniupnpd "
                    "/etc/init.d/nlbwmon "
                    "/etc/init.d/pbr "
                    "/etc/init.d/adguardhome "
                    "/etc/init.d/unbound "
                    "/usr/sbin/batctl "
                    "/sys/module/batman_adv "
                    "/etc/config/sqm "
                    "/usr/bin/stty /bin/stty /usr/bin/timeout /bin/timeout; do "
                    "if [ -f $f ] || [ -x $f ]; then echo 1; else echo 0; fi; done"
                )
                result = await self._call(
                    "file",
                    "exec",
                    {"command": "/bin/sh", "params": ["-c", cmd]},
                )
                out = result.get("stdout", "")
                results = out.strip().splitlines()

                def detect_status(idx: int) -> bool:
                    return len(results) > idx and results[idx].strip() == "1"

                if packages.sqm_scripts is not True:
                    packages.sqm_scripts = detect_status(0) or detect_status(20)
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
                        "luci-rpc" in objects or detect_status(6) or detect_status(7)
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
                packages.batctl = detect_status(18)
                packages.batman_adv = detect_status(19)
                packages.stty = detect_status(21) or detect_status(22)
                packages.timeout = detect_status(23) or detect_status(24)

            except Exception as err:
                _LOGGER.debug("Package detection via RPC failed, falling back: %s", err)
                # Step 1: Re-use _list_objects() which uses the correct JSON-RPC
                # 'list' method (not a ubus object call).
                objects = await self._list_objects()
                self._check_packages_from_ubus(packages, objects)

                # Step 2: Check by UCI config exists
                await self._check_packages_from_uci(packages, objects)

            # Step 3: Check by file paths (last resort)
            await self._check_packages_from_files(packages)

            # Step 4: Final verification via full package list
            await self._check_packages_from_full_list(packages)

        except Exception as err:
            _LOGGER.debug("Package check failed: %s", err)

        self._ensure_all_packages_initialized(packages)
        return packages

    def _check_packages_from_ubus(
        self, packages: OpenWrtPackages, objects: list[str]
    ) -> None:
        """Identify packages based on existence of specific ubus objects."""
        packages.luci_mod_rpc = "luci" in objects
        packages.iwinfo = "iwinfo" in objects
        packages.mwan3 = "mwan3" in objects or "mwan3.status" in objects
        packages.sqm_scripts = "sqm" in objects
        packages.wireguard = "wg" in objects
        packages.asu = "attendedsysupgrade" in objects

    async def _check_packages_from_uci(
        self, packages: OpenWrtPackages, objects: list[str]
    ) -> None:
        """Identify packages based on existence of specific UCI configs."""
        configs = [
            ("sqm", "sqm_scripts"),
            ("mwan3", "mwan3"),
            ("openvpn", "openvpn"),
            ("attendedsysupgrade", "asu"),
        ]
        for config, attr in configs:
            if getattr(packages, attr) is not True:
                with contextlib.suppress(Exception):
                    await self._call("uci", "get", {"config": config})
                    setattr(packages, attr, True)

        if packages.wireguard is not True:
            with contextlib.suppress(Exception):
                res = await self._call("uci", "get", {"config": "network"})
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

    async def _check_packages_from_files(self, packages: OpenWrtPackages) -> None:
        """Identify packages by probing specific filesystem paths."""
        # For each attribute we try multiple candidate paths (first match wins).
        # luci-app-attendedsysupgrade changed file locations across LuCI versions:
        #   - Legacy: controller lua file
        #   - Modern LuCI (menu.d): JSON menu descriptor
        #   - Always present: UCI config file created at install time
        check_list: list[tuple[str, str]] = [
            ("/usr/bin/etherwake", "etherwake"),
            ("/usr/bin/wg", "wireguard"),
            # ASU: try all known install paths
            ("/etc/config/attendedsysupgrade", "asu"),
            ("/usr/share/luci/menu.d/luci-app-attendedsysupgrade.json", "asu"),
            ("/usr/lib/lua/luci/controller/attendedsysupgrade.lua", "asu"),
            ("/etc/init.d/adblock", "adblock"),
            ("/etc/init.d/simple-adblock", "simple_adblock"),
            ("/etc/init.d/banip", "ban_ip"),
        ]
        for path, attr in check_list:
            if getattr(packages, attr) is not True:
                with contextlib.suppress(Exception):
                    stat = await self._call("file", "stat", {"path": path})
                    if stat and isinstance(stat, dict) and "type" in stat:
                        setattr(packages, attr, True)

    async def _check_packages_from_full_list(self, packages: OpenWrtPackages) -> None:
        """Last resort: check against the full list of installed packages."""
        installed = await self.get_installed_packages()
        if not installed:
            return

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
            "ban_ip": "banip",
        }
        for attr, pkg_name in mapping.items():
            if getattr(packages, attr) is not True:
                if pkg_name in ["wireguard", "openvpn"]:
                    setattr(packages, attr, any(pkg_name in p for p in installed))
                else:
                    setattr(packages, attr, pkg_name in installed)

    def _ensure_all_packages_initialized(self, packages: OpenWrtPackages) -> None:
        """Ensure no package attributes remain as None (default to False)."""
        import dataclasses

        # Infer wireless support if iwinfo is present (crucial fallback for restricted ubus)
        if packages.wireless is None and packages.iwinfo:
            packages.wireless = True

        for field in dataclasses.fields(packages):
            if getattr(packages, field.name) is None:
                setattr(packages, field.name, False)

    async def get_services(self) -> list[ServiceInfo]:
        """Get init.d services via the rc ubus interface."""
        services: list[ServiceInfo] = []
        result = await self._call("rc", "list")
        for name, data in result.items():
            services.append(
                ServiceInfo(
                    name=name,
                    enabled=data.get("enabled", False),
                    running=data.get("running", False)
                    or (
                        data.get("running") is False
                        and data.get("exit_code") == 0
                        and name in ("adblock", "simple-adblock", "sysctl")
                    ),
                ),
            )
        return services

    async def manage_service(self, name: str, action: str) -> bool:
        """Manage a system service (start/stop/restart/enable/disable)."""
        try:
            # 1. Try standard ubus rc.init (best practice)
            await self._call("rc", "init", {"name": name, "action": action})
            self._last_full_poll = 0
            return True
        except (
            UbusPermissionError,
            UbusError,
        ):
            try:
                # 2. Try ubus file.exec (direct init script call)
                await self._call(
                    "file",
                    "exec",
                    {"command": f"/etc/init.d/{name}", "params": [action]},
                )
                self._last_full_poll = 0
                return True
            except Exception:
                # 3. Final fallback to shell execute_command
                try:
                    await self.execute_command(f"/etc/init.d/{name} {action}")
                    self._last_full_poll = 0
                    return True
                except Exception as err:
                    _LOGGER.debug(
                        "Failed to manage service %s (%s) via any method: %s",
                        name,
                        action,
                        err,
                    )
                    return False

    async def get_installed_packages(self) -> list[str]:
        """Get a list of installed packages via apk or opkg.

        On OpenWrt 25.x+ with APK: 'apk info' lists one package per line
        (no version suffix in the default output).  On older opkg-based
        firmware the first field (before the first space) is the package name.
        """
        try:
            # Try apk first (OpenWrt 25+); fall back to opkg.
            # apk info -q suppresses progress/warnings. We strip any trailing
            # version suffix that some apk builds append (e.g. "pkg-1.0-r0").
            # Try apk (OpenWrt 25+) or opkg. Use absolute paths as fallback.
            cmd = (
                "if command -v apk >/dev/null 2>&1; then APK=apk; "
                "elif [ -x /sbin/apk ]; then APK=/sbin/apk; fi; "
                'if [ -n "$APK" ]; then $APK info 2>/dev/null; '
                "else "
                "  if command -v opkg >/dev/null 2>&1; then OPKG=opkg; "
                "  elif [ -x /bin/opkg ]; then OPKG=/bin/opkg; fi; "
                "  if [ -n \"$OPKG\" ]; then $OPKG list-installed 2>/dev/null | cut -d' ' -f1; fi; "
                "fi"
            )
            output = await self.execute_command(cmd)
            if not output:
                return []
            packages: list[str] = []
            for line in output.splitlines():
                name = line.strip()
                if name:
                    packages.append(name)
            return packages
        except UbusError:
            _LOGGER.debug("Failed to list installed packages via ubus file.exec")
            return []
        except Exception as err:
            _LOGGER.debug("Unexpected error listing installed packages: %s", err)
            return []

    async def get_leds(self) -> list:
        """Get LEDs from /sys/class/leds via file.exec."""
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
        stdout = await self.execute_command(cmd)
        for line in stdout.strip().splitlines():
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

    async def perform_diagnostics(self) -> list[DiagnosticResult]:
        """Perform ubus-specific diagnostic checks."""
        results: list[DiagnosticResult] = []

        # Check connection error
        if self._last_connect_error:
            err_str = str(self._last_connect_error)
            if (
                "ConnectionRefusedError" in err_str
                or "connection refused" in err_str.lower()
                or "connect call failed" in err_str.lower()
                or "1225" in err_str
            ):
                results.append(
                    DiagnosticResult(
                        name="Router Web Server",
                        status="FAIL",
                        message="Connection refused by the router's web server.",
                        details=(
                            "The router's web server (uhttpd) is not running or is blocking the connection on port 80/443. "
                            "Please check that uhttpd/nginx is enabled and running on the router (e.g. run "
                            "'/etc/init.d/uhttpd start' over SSH)."
                        ),
                    )
                )
            elif "404" in err_str or "not found" in err_str.lower():
                results.append(
                    DiagnosticResult(
                        name="RPC Endpoint Availability",
                        status="FAIL",
                        message="API endpoint not found (HTTP 404).",
                        details=(
                            "The router returned HTTP 404 (Not Found) for the API endpoint. "
                            "This indicates that the required RPC package is not installed on the router. "
                            "Please ensure that the 'uhttpd-mod-ubus' package is installed."
                        ),
                    )
                )

        # 1. Check Session ACLs
        try:
            session_data = await self._call("session", "list")
            acls = session_data.get("acls", {})
            if acls:
                results.append(
                    DiagnosticResult(
                        name="Session ACLs",
                        status="PASS",
                        message=f"Session for '{self.username}' is active with {len(acls)} ACL groups.",
                        details=str(acls),
                    )
                )
            else:
                results.append(
                    DiagnosticResult(
                        name="Session ACLs",
                        status="WARN",
                        message="Session is active but no ACLs were returned. This may be a restricted session.",
                    )
                )
        except Exception as err:
            results.append(
                DiagnosticResult(
                    name="Session ACLs",
                    status="FAIL",
                    message="Failed to retrieve session ACLs.",
                    details=str(err),
                )
            )

        # 2. Check for key objects
        required_objects = ["system", "network.interface", "uci"]

        try:
            objects = await self._list_objects()
            found_req = [obj for obj in required_objects if obj in objects]

            if len(found_req) == len(required_objects):
                results.append(
                    DiagnosticResult(
                        name="Core Ubus Objects",
                        status="PASS",
                        message=f"All core objects found: {', '.join(found_req)}",
                    )
                )
            else:
                missing = set(required_objects) - set(found_req)
                results.append(
                    DiagnosticResult(
                        name="Core Ubus Objects",
                        status="FAIL",
                        message=f"Missing core objects: {', '.join(missing)}",
                        remedy="Ensure 'rpcd' is running and your user has access to these objects in /etc/config/rpcd.",
                    )
                )
        except Exception as err:
            results.append(
                DiagnosticResult(
                    name="Ubus Objects",
                    status="FAIL",
                    message="Failed to list ubus objects.",
                    details=str(err),
                )
            )

        # 3. Check for logread flag support (Fixes #17 analysis)
        try:
            cmd = await self._get_logread_command(1)
            results.append(
                DiagnosticResult(
                    name="Logread Compatibility",
                    status="PASS",
                    message=f"Using command: {cmd}",
                    details=f"Detected flag: {self._logread_flag}",
                )
            )
        except Exception as err:
            results.append(
                DiagnosticResult(
                    name="Logread Compatibility",
                    status="FAIL",
                    message="Failed to detect logread capabilities.",
                    details=str(err),
                )
            )

        # 4. Check for file.exec (Common blocker)
        try:
            await self.execute_command("id")
            results.append(
                DiagnosticResult(
                    name="Command Execution (file.exec)",
                    status="PASS",
                    message="Successfully executed 'id' command.",
                )
            )
        except UbusPermissionError:
            results.append(
                DiagnosticResult(
                    name="Command Execution (file.exec)",
                    status="FAIL",
                    message="Permission denied for file.exec.",
                    remedy="Grant 'file': ['exec'] permission to your user or connect as 'root'. Note: Some OEM firmwares block this entirely.",
                )
            )
        except Exception as err:
            results.append(
                DiagnosticResult(
                    name="Command Execution (file.exec)",
                    status="FAIL",
                    message="Failed to execute command.",
                    details=str(err),
                )
            )

        # 5. Get recent logs
        try:
            logs = await self.get_system_logs(20)
            if logs:
                results.append(
                    DiagnosticResult(
                        name="System Logs (Recent)",
                        status="INFO",
                        message=f"Retrieved {len(logs)} log entries.",
                        details="\n".join(logs),
                    )
                )
        except Exception:
            pass

        return results

    async def set_led(self, name: str, brightness: int) -> bool:
        """Enable or disable an LED via ubus."""
        try:
            val = str(int(brightness))
            # First ensure trigger is set to none
            await self.execute_command(
                f"echo none > /sys/class/leds/{name}/trigger 2>/dev/null"
            )
            # Try file.write
            await self._call(
                "file",
                "write",
                {"path": f"/sys/class/leds/{name}/brightness", "data": val},
            )
            self._last_full_poll = 0
            return True
        except Exception:
            try:
                # Fallback to shell
                await self.execute_command(
                    f"echo {val} > /sys/class/leds/{name}/brightness"
                )
                self._last_full_poll = 0
                return True
            except Exception as err:
                _LOGGER.debug("Failed to set LED %s via ubus: %s", name, err)
                return False

    async def is_reboot_required(self) -> bool:
        """Check if reboot is required via common OpenWrt flags."""
        try:
            # Check for common flags
            paths = ["/tmp/.reboot-needed", "/var/run/reboot-required"]
            for path in paths:
                res = await self._call("file", "stat", {"path": path})
                if res and isinstance(res, dict) and "type" in res:
                    return True
            return False
        except Exception:
            # Fallback to shell
            try:
                output = await self.execute_command(
                    "[ -f /tmp/.reboot-needed ] || [ -f /var/run/reboot-required ] && echo 1"
                )
                return output.strip() == "1"
            except Exception:
                return False
