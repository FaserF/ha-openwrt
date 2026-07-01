# mypy: disable-error-code="attr-defined"
from __future__ import annotations

import asyncio
import contextlib
import logging
import shlex
from typing import Any

import paramiko  # type: ignore[import-untyped]
from homeassistant.helpers import storage

from ..base import (
    PROVISION_SCRIPT_TEMPLATE,
    OpenWrtClient,
    OpenWrtPackages,
    OpenWrtPermissions,
)
from .devices import SshDevicesMixin
from .exceptions import *
from .features import SshFeaturesMixin
from .network import SshNetworkMixin
from .system import SshSystemMixin

_LOGGER = logging.getLogger(__name__)


class SshClient(
    SshSystemMixin,
    SshNetworkMixin,
    SshDevicesMixin,
    SshFeaturesMixin,
    OpenWrtClient,
):
    """OpenWrt SSH client."""

    def __init__(
        self,
        hass: Any,
        session: Any,
        host: str,
        username: str,
        password: str,
        port: int = 22,
        use_ssl: bool = False,
        verify_ssl: bool = False,
        ssh_key: str | None = None,
        dhcp_software: str = "auto",
        trust_stale_arp: bool = True,
        trust_bridge_fdb: bool = True,
    ) -> None:
        """Initialize the SSH client."""
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
        self._ssh_key = ssh_key
        self._client: Any = None
        self._semaphore = asyncio.Semaphore(2)

    async def _exec(self, command: str, retry: bool = True) -> str:
        """Execute a command via SSH and return stdout."""
        loop = asyncio.get_event_loop()

        def _run() -> str:
            if self._client is None:
                msg = "Not connected"
                raise SshError(msg)
            _stdin, stdout, stderr = self._client.exec_command(command, timeout=15)
            # Read streams to prevent blocking
            out_bytes = stdout.read()
            err_bytes = stderr.read()
            # Wait for exit status
            exit_code = stdout.channel.recv_exit_status()
            output = out_bytes.decode("utf-8", errors="replace")
            error = err_bytes.decode("utf-8", errors="replace")
            if exit_code != 0 or error:
                _LOGGER.debug(
                    "SSH command '%s' returned %d. Stdout: '%s', Stderr: '%s'",
                    command[:100] + "..." if len(command) > 100 else command,
                    exit_code,
                    output.strip(),
                    error.strip(),
                )
            return output or error

        try:
            async with self._semaphore:
                return await loop.run_in_executor(None, _run)
        except Exception as err:
            _LOGGER.debug("SSH command failed, marking as disconnected: %s", err)
            self._connected = False
            if self._client:
                with contextlib.suppress(Exception):
                    self._client.close()
                self._client = None

            if retry:
                _LOGGER.debug("Attempting to reconnect and retry SSH command...")
                try:
                    if await self.connect():
                        return await self._exec(command, retry=False)
                except Exception as reconnect_err:
                    _LOGGER.debug(
                        "SSH reconnection failed during retry: %s",
                        reconnect_err,
                    )
                    if isinstance(reconnect_err, SshError):
                        raise
                    raise SshConnectionError(
                        f"SSH connection failed: {reconnect_err}"
                    ) from reconnect_err

            if isinstance(err, SshError):
                raise
            raise SshConnectionError(f"SSH command failed: {err}") from err

    async def execute_command(self, command: str) -> str:
        """Execute a command via SSH."""
        return await self._exec(command)

    async def file_exec(
        self, command: str, params: list[str] | None = None
    ) -> dict[str, Any]:
        """Execute a binary directly via SSH, returning a file.exec-compatible dict."""

        parts = [command] + (params or [])
        cmd = " ".join(shlex.quote(p) for p in parts)
        output = await self._exec(f"{cmd} 2>&1; echo __HA_RC__$?")
        if not output:
            return {}
        # Use partition() rather than splitlines() so the sentinel is found even when
        # the command output does not end with a newline.
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
        if rc != 0:
            return {"code": rc, "stdout": "", "stderr": stdout}
        return {"code": rc, "stdout": stdout, "stderr": ""}

    async def provision_user(
        self,
        username: str,
        password: str,
    ) -> tuple[bool, str | None]:
        """Create a dedicated system user and configure RPC permissions via SSH."""
        # Use the harmonized provisioning script from base
        script = PROVISION_SCRIPT_TEMPLATE.format(username=username, password=password)
        try:
            output = await self._exec(script)

            if output is None:
                output = ""

            if output:
                _LOGGER.debug(
                    "Provisioning output for %s via SSH: %s",
                    username,
                    output,
                )

            if "Provisioning SUCCESS" in output:
                return True, None

            if "LOG: FAIL:" in output:
                fail_msg = output.split("LOG: FAIL:")[1].splitlines()[0].strip()
                _LOGGER.error("Provisioning failed via SSH: %s", fail_msg)
                return False, fail_msg

            # Empty output usually means permission denied
            if not output:
                _LOGGER.warning(
                    "Provisioning for %s returned empty output. "
                    "Ensure '%s' has appropriate execution rights.",
                    username,
                    self.username,
                )
                return (
                    False,
                    (
                        f"Provisioning failed: empty response from SSH. "
                        f"Ensure '{self.username}' has execution permission."
                    ),
                )

            return (
                False,
                "Provisioning script returned failure without specific error via SSH. Check router logs (logread).",
            )
        except Exception as err:
            _LOGGER.exception("Failed to provision user %s via SSH: %s", username, err)
            return False, str(err)

    async def connect(self) -> bool:
        """Connect via SSH."""
        try:
            return await self._connect()
        except Exception as err:
            self._last_connect_error = err
            raise

    async def _connect(self) -> bool:
        """Connect via SSH."""
        loop = asyncio.get_event_loop()

        pinned_data: dict[str, Any] = {}
        store: storage.Store | None = None
        if self.hass:
            store = storage.Store(self.hass, version=1, key="openwrt_ssh_host_keys")
            try:
                loaded = await store.async_load()
                if isinstance(loaded, dict):
                    pinned_data = loaded
            except Exception:
                pass

        new_keys: dict[str, str] = {}
        host_id = f"{self.host}:{self.port}"
        expected_keys = pinned_data.get(host_id, {})

        def _connect() -> None:
            import io

            client = paramiko.SSHClient()
            client.load_system_host_keys()

            class PinningHostKeyPolicy(paramiko.MissingHostKeyPolicy):
                def missing_host_key(
                    self, client_: Any, hostname: str, key: Any
                ) -> None:
                    key_type = key.get_name()
                    key_b64 = key.get_base64()

                    if not expected_keys:
                        new_keys[key_type] = key_b64
                        client_.get_host_keys().add(hostname, key_type, key)
                        return

                    if key_type in expected_keys:
                        if expected_keys[key_type] == key_b64:
                            client_.get_host_keys().add(hostname, key_type, key)
                            return
                        raise paramiko.SSHException(
                            f"SSH host key mismatch for {hostname}! Expected pinned key for {key_type}, but received a different key. Possible security intercept."
                        )

                    raise paramiko.SSHException(
                        f"SSH host key mismatch for {hostname}! Host key algorithm {key_type} not in pinned keys."
                    )

            client.set_missing_host_key_policy(PinningHostKeyPolicy())

            connect_kwargs: dict[str, Any] = {
                "hostname": self.host,
                "port": self.port,
                "username": self.username,
                "timeout": 10,
                "allow_agent": False,
                "look_for_keys": False,
            }

            if self._ssh_key:
                key_file = io.StringIO(self._ssh_key)
                try:
                    pkey = paramiko.RSAKey.from_private_key(key_file)
                except Exception:
                    key_file.seek(0)
                    try:
                        pkey = paramiko.Ed25519Key.from_private_key(key_file)
                    except Exception:
                        key_file.seek(0)
                        pkey = paramiko.ECDSAKey.from_private_key(key_file)
                connect_kwargs["pkey"] = pkey
            else:
                connect_kwargs["password"] = self.password

            try:
                client.connect(**connect_kwargs)
            except paramiko.AuthenticationException as err:
                msg = f"SSH auth failed for {self.username}@{self.host}. Check credentials/key."
                raise SshAuthError(
                    msg,
                ) from err
            except TimeoutError as err:
                msg = f"SSH connection timed out for {self.host}"
                raise SshTimeoutError(
                    msg,
                ) from err
            except (OSError, paramiko.SSHException) as err:
                err_str = str(err).lower()
                if "connection refused" in err_str:
                    msg = f"SSH connection refused on {self.host}:{self.port}. Is SSH enabled?"
                    raise SshConnectionError(
                        msg,
                    ) from err
                if "no route to host" in err_str:
                    msg = f"Host {self.host} is unreachable."
                    raise SshConnectionError(
                        msg,
                    ) from err
                msg = f"SSH connection failed: {err}"
                raise SshError(msg) from err
            except Exception as err:
                msg = f"SSH connection failed: {err}"
                raise SshError(msg) from err

            transport = client.get_transport()
            if transport:
                transport.set_keepalive(30)

            self._client = client

        try:
            await loop.run_in_executor(None, _connect)
            self._connected = True
            _LOGGER.debug("SSH connected to %s", self.host)
            if new_keys and store:
                pinned_data[host_id] = new_keys
                try:
                    await store.async_save(pinned_data)
                except Exception as err:
                    _LOGGER.debug("Failed to save pinned SSH host key: %s", err)
            return True
        except (
            SshError,
            SshAuthError,
        ):
            raise
        except Exception as err:
            msg = f"SSH connection error: {err}"
            raise SshError(msg) from err

    async def disconnect(self) -> None:
        """Disconnect SSH."""
        if self._client:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._client.close)
            self._client = None
        self._connected = False

    async def check_permissions(self) -> OpenWrtPermissions:
        """Check user permissions via SSH.

        SSH access generally provides full root access, but we try to
        verify if common commands work to be safe.
        """
        from ..base import OpenWrtPermissions

        perms = OpenWrtPermissions()
        is_root = self.username == "root"

        try:
            # Root always has full permissions
            if is_root:
                perms.read_system = True
                perms.read_network = True
                perms.read_firewall = True
                perms.read_wireless = True
                perms.read_sqm = True
                perms.read_led = True
                perms.read_vpn = True
                perms.read_mwan = True
                perms.read_devices = True
                perms.read_services = True
                perms.write_system = True
                perms.write_network = True
                perms.write_firewall = True
                perms.write_wireless = True
                perms.write_sqm = True
                perms.write_led = True
                perms.write_vpn = True
                perms.write_access_control = True
                perms.write_devices = True
                perms.write_services = True
                perms.read_batman = True
                perms.write_mqtt = True
                return perms

            # 1. Check UCI read access (very common baseline for non-root)
            uci_check = await self._exec("uci show system 2>/dev/null | head -n 1")
            if uci_check.strip():
                perms.read_system = True
                perms.read_network = True
                perms.read_firewall = True
                perms.read_wireless = True
                perms.read_sqm = True
                perms.read_led = True
                perms.read_vpn = True
                perms.read_mwan = True
                perms.read_devices = True
                perms.read_services = True

            # 2. Check UBUS access for critical features
            ubus_list = await self._exec("ubus list 2>/dev/null")
            if "network.wireless" in ubus_list or "iwinfo" in ubus_list:
                perms.read_wireless = True
            elif not perms.read_wireless:
                # If UCI and UBUS both fail for wireless, we might still have iwinfo CLI
                iwinfo_check = await self._exec("iwinfo 2>/dev/null")
                if "ESSID" in iwinfo_check:
                    perms.read_wireless = True

            # 3. Check Batman access
            if "batman-adv" in ubus_list:
                perms.read_batman = True
            elif not perms.read_batman:
                bat_check = await self._exec("[ -d /sys/module/batman_adv ] && echo 1")
                if bat_check.strip() == "1":
                    perms.read_batman = True

            # 4. Write permissions
            # Test write access with a dummy UCI change (without commit)
            try:
                write_check = await self._exec(
                    "uci set system.@system[0].ha_test='1' 2>/dev/null && echo 1"
                )
                if write_check.strip() == "1":
                    perms.write_system = True
                    # Assume others follow if system is writable
                    perms.write_network = True
                    perms.write_firewall = True
                    perms.write_mqtt = True
                    perms.write_wireless = True
                    perms.write_sqm = True
                    perms.write_led = True
                    perms.write_devices = True
                    perms.write_services = True
                    perms.write_access_control = True
                    perms.write_vpn = True

                    # 5. Check for MQTT write access specifically
                    # If we have UCI write access, we probably have enough,
                    # but let's be sure we can write to /etc/presence if it exists
                    mqtt_check = await self._exec(
                        "[ -w /etc/presence ] || [ -w /tmp ] && echo 1"
                    )
                    if mqtt_check.strip() == "1":
                        perms.write_mqtt = True
            except Exception:
                pass
        except Exception:
            if is_root:
                # Fallback for root if probes fail
                perms.read_system = True
                perms.read_network = True
                perms.write_system = True
                perms.write_network = True
        return perms

    async def check_packages(self) -> OpenWrtPackages:
        """Check installed packages via SSH probes."""
        packages = OpenWrtPackages()
        try:
            # Step 1: Check existence of binaries or init scripts
            await self._check_packages_from_files(packages)

            # Step 2: Fallback to full list check
            await self._check_packages_from_opkg(packages)

        except Exception as err:
            _LOGGER.debug("Package check failed via SSH: %s", err)

        self._ensure_all_packages_initialized(packages)
        return packages

    async def _check_packages_from_files(self, packages: OpenWrtPackages) -> None:
        """Identify packages by probing filesystem for binaries or scripts via SSH."""
        cmd = (
            "for f in /etc/init.d/sqm /etc/init.d/mwan3 /usr/bin/iwinfo "
            "/usr/bin/etherwake /usr/bin/wg /usr/sbin/openvpn "
            "/usr/lib/lua/luci/controller/rpc.lua "
            "/usr/share/luci/menu.d/luci-mod-rpc.json "
            "/usr/lib/lua/luci/controller/attendedsysupgrade.lua "
            "/usr/share/luci/menu.d/luci-app-attendedsysupgrade.json "
            "/etc/init.d/adblock /etc/init.d/simple-adblock /etc/init.d/ban-ip /etc/init.d/miniupnpd /etc/init.d/nlbwmon /etc/init.d/pbr /etc/init.d/adguardhome /etc/init.d/unbound /usr/lib/rpcd/led.so /etc/config/sqm /etc/init.d/odhcpd /etc/init.d/lldpd /usr/sbin/batctl /sys/module/batman_adv /usr/bin/stty /bin/stty /usr/bin/timeout /bin/timeout; do "
            "if [ -f $f ] || [ -x $f ] || [ -d $f ]; then echo 1; else echo 0; fi; done"
        )
        out = await self._exec(cmd)
        results = out.strip().splitlines()

        def detect(idx: int) -> bool:
            return len(results) > idx and results[idx].strip() == "1"

        packages.sqm_scripts = detect(0) or detect(19)
        packages.mwan3 = detect(1)
        packages.iwinfo = detect(2)
        packages.etherwake = detect(3)
        packages.wireguard = detect(4)
        packages.openvpn = detect(5)
        packages.luci_mod_rpc = detect(6) or detect(7)
        packages.asu = detect(8) or detect(9)
        packages.adblock = detect(10)
        packages.simple_adblock = detect(11)
        packages.ban_ip = detect(12)
        packages.miniupnpd = detect(13)
        packages.nlbwmon = detect(14)
        packages.pbr = detect(15)
        packages.adguardhome = detect(16)
        packages.unbound = detect(17)

        packages.dhcp = detect(20)
        if packages.dhcp:
            # Specifically check for ipv4leases method
            dhcp_check = await self._exec("ubus list dhcp")
            if "ipv4leases" not in dhcp_check:
                packages.dhcp = False
        packages.lldp = detect(21)
        packages.batctl = detect(22)
        packages.batman_adv = detect(23)
        packages.stty = detect(24) or detect(25)
        packages.timeout = detect(26) or detect(27)

        # Detect wireless via presence of iwinfo or ubus network.wireless
        if packages.iwinfo:
            packages.wireless = True
        else:
            # Last ditch check for wireless
            wifi_check = await self._exec("ubus list network.wireless")
            if wifi_check and "network.wireless" in wifi_check:
                packages.wireless = True

    async def _check_packages_from_opkg(self, packages: OpenWrtPackages) -> None:
        """Identify packages by checking the full installed package list."""
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
            "luci_mod_rpc": "luci-rpc",
            "asu": "luci-app-attendedsysupgrade",
            "adblock": "adblock",
            "simple_adblock": "simple-adblock",
            "ban_ip": "ban-ip",
            "dhcp": "odhcpd",
            "lldp": "lldpd",
            "wireless": "iwinfo",
            "batman_adv": "kmod-batman-adv",
            "batctl": "batctl",
        }
        for attr, pkg_name in mapping.items():
            if getattr(packages, attr) is not True:
                if pkg_name in ("wireguard", "openvpn", "batctl"):
                    setattr(packages, attr, any(pkg_name in p for p in installed))
                elif attr == "luci_mod_rpc":
                    setattr(
                        packages,
                        attr,
                        any(p in installed for p in ("luci-rpc", "luci-mod-rpc")),
                    )
                else:
                    setattr(packages, attr, pkg_name in installed)

    def _ensure_all_packages_initialized(self, packages: OpenWrtPackages) -> None:
        """Ensure no package attributes remain as None (default to False)."""
        import dataclasses

        for field in dataclasses.fields(packages):
            if getattr(packages, field.name) is None:
                setattr(packages, field.name, False)
