# mypy: disable-error-code="attr-defined,has-type"
"""Ubus permissions and package inspection mixin."""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from ..base import OpenWrtPackages, OpenWrtPermissions
from .exceptions import UbusError, UbusPermissionError

_LOGGER = logging.getLogger(__name__)


class UbusServicesPackagesMixin:
    """Mixin for Ubus permission and package detection."""

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
            perms.read_mwan = has_perm("mwan3", "status")
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
        perms.read_mwan = await can_call("mwan3", "status") or is_root
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
            # 12: /etc/init.d/banip        -> ban_ip
            # 13: /etc/init.d/miniupnpd   -> miniupnpd
            # 14: /etc/init.d/nlbwmon     -> nlbwmon
            # 15: /etc/init.d/pbr         -> pbr
            # 16: /etc/init.d/adguardhome -> adguardhome
            # 17: /etc/init.d/unbound     -> unbound
            # 18: /usr/sbin/batctl        -> batctl
            # 19: /sys/module/batman_adv  -> batman_adv
            # 20: /etc/config/sqm         -> sqm_scripts (fallback)
            # 21: /usr/bin/stty           -> stty
            # 22: /bin/stty               -> stty
            # 23: /usr/bin/timeout        -> timeout
            # 24: /bin/timeout            -> timeout
            # 25: /usr/sbin/tailscale     -> tailscale
            # 26: /usr/bin/tailscale      -> tailscale
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
                    "/usr/bin/stty /bin/stty /usr/bin/timeout /bin/timeout "
                    "/usr/sbin/tailscale /usr/bin/tailscale "
                    "/usr/bin/openssl /bin/openssl /usr/bin/base64 /bin/base64; do "
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
                if packages.tailscale is not True:
                    packages.tailscale = detect_status(25) or detect_status(26)
                packages.openssl_util = detect_status(27) or detect_status(28)
                packages.coreutils_base64 = detect_status(29) or detect_status(30)

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
            ("tailscale", "tailscale"),
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
            ("/etc/init.d/snort", "snort"),
            ("/usr/sbin/tailscale", "tailscale"),
            ("/usr/bin/tailscale", "tailscale"),
            ("/usr/bin/openssl", "openssl_util"),
            ("/bin/openssl", "openssl_util"),
            ("/usr/bin/base64", "coreutils_base64"),
            ("/bin/base64", "coreutils_base64"),
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
            "snort": "snort",
            "tailscale": "tailscale",
            "openssl_util": "openssl-util",
            "coreutils_base64": "coreutils-base64",
        }
        for attr, pkg_name in mapping.items():
            if getattr(packages, attr) is not True:
                if pkg_name in ["wireguard", "openvpn"]:
                    setattr(packages, attr, any(pkg_name in p for p in installed))
                elif attr == "openssl_util":
                    setattr(
                        packages,
                        attr,
                        any(p in installed for p in ("openssl-util", "openssl")),
                    )
                elif attr == "coreutils_base64":
                    setattr(
                        packages,
                        attr,
                        any(p in installed for p in ("coreutils-base64", "base64")),
                    )
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
