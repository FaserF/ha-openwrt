# mypy: disable-error-code="attr-defined,has-type"
"""LuCI RPC packages and permissions checking mixin."""

from __future__ import annotations

import logging

from ..base import OpenWrtPackages, OpenWrtPermissions
from .exceptions import LuciRpcError

_LOGGER = logging.getLogger(__name__)


class LuciRpcPackagesMixin:
    """Mixin for LuCI RPC permissions and package checks."""

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
        # 20: /usr/sbin/batctl          -> batctl
        # 21: /sys/module/batman_adv    -> batman_adv
        # 22: /usr/bin/stty             -> stty
        # 23: /bin/stty                 -> stty
        # 24: /usr/bin/timeout          -> timeout
        # 25: /bin/timeout              -> timeout
        # 26: /etc/init.d/snort         -> snort
        # 27: /usr/sbin/tailscale       -> tailscale
        # 28: /usr/bin/tailscale        -> tailscale
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
            "/usr/bin/stty /bin/stty /usr/bin/timeout /bin/timeout /etc/init.d/snort "
            "/usr/sbin/tailscale /usr/bin/tailscale "
            "/usr/bin/openssl /bin/openssl /usr/bin/base64 /bin/base64; do "
            "if [ -e $f ]; then echo 1; else echo 0; fi; done"
        )
        try:
            out = await self._rpc_call("sys", "exec", [cmd])
        except Exception:
            out = ""
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
            packages.snort = detect_status(26)
            if packages.tailscale is not True:
                packages.tailscale = detect_status(27) or detect_status(28)
            packages.openssl_util = detect_status(29) or detect_status(30)
            packages.coreutils_base64 = detect_status(31) or detect_status(32)

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
                "tailscale": "tailscale",
                "openssl_util": "openssl-util",
                "coreutils_base64": "coreutils-base64",
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
