# mypy: disable-error-code="attr-defined,has-type,assignment"
from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigFlowResult,
)
from homeassistant.const import CONF_HOST
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo
from homeassistant.helpers.service_info.ssdp import SsdpServiceInfo
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from ..const import (
    CONF_CONNECTION_TYPE,
    CONNECTION_TYPE_LUCI_RPC,
    CONNECTION_TYPE_SSH,
    CONNECTION_TYPE_UBUS,
    DOCS_URL,
    DOMAIN,
)
from .helpers import selector

_LOGGER = logging.getLogger(__name__)

CONNECTION_TYPE_MAP = {
    CONNECTION_TYPE_UBUS: "ubus (HTTP/HTTPS)",
    CONNECTION_TYPE_LUCI_RPC: "LuCI RPC",
    CONNECTION_TYPE_SSH: "SSH (not recommended)",
}


class DiscoveryFlowMixin:
    """Discovery and initial setup steps for OpenWrtConfigFlow."""

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        if user_input is not None:
            if user_input.get("flow_type") == "manual":
                return await self.async_step_manual_entry()
            return await self.async_step_discovery()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "flow_type",
                        default="manual",
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=["discovery", "manual"],
                            translation_key="flow_type",
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        ),
                    ),
                },
            ),
            description_placeholders={"docs_url": DOCS_URL},
        )

    async def async_step_discovery(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Scan for routers in the background."""
        if user_input is not None or self._discovered_routers:
            if not self._discovered_routers:
                return self.async_show_form(
                    step_id="discovery",
                    errors={"base": "no_devices_found"},
                    description_placeholders={"discovery_info": ""},
                )

            if len(self._discovered_routers) == 1:
                return await self.async_step_confirm_discovery()

            return await self.async_step_select_device()

        # Perform scanning
        potential_hosts = await self._async_get_potential_hosts()
        tasks = [self._async_probe_router(host) for host in potential_hosts]
        results = await asyncio.gather(*tasks)

        existing_hosts = {
            entry.data.get(CONF_HOST)
            for entry in self.hass.config_entries.async_entries(DOMAIN)
        }

        for router_info in results:
            if router_info:
                host = router_info["host"]
                # Skip already configured routers or duplicates
                if host not in existing_hosts and not any(
                    r["host"] == host for r in self._discovered_routers
                ):
                    self._discovered_routers.append(router_info)

        if not self._discovered_routers:
            return await self.async_step_manual_entry()

        return await self.async_step_discovery(user_input={})

    async def _async_get_potential_hosts(self) -> set[str]:
        """Gather potential router IPs from defaults and HA network adapters."""
        potential_hosts: set[str] = {"192.168.1.1", "192.168.0.1", "10.0.0.1"}

        with contextlib.suppress(Exception):
            from homeassistant.components import network

            adapters = await network.async_get_adapters(self.hass)
            for adapter in adapters:
                for ipv4 in adapter.get("ipv4", []):
                    local_ip = ipv4.get("address")
                    if local_ip and (parts := local_ip.split(".")) and len(parts) == 4:
                        potential_hosts.add(".".join([*parts[:-1], "1"]))
                        potential_hosts.add(".".join([*parts[:-1], "254"]))

        return potential_hosts

    async def async_step_manual_entry(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Handle manual entry if discovery fails or if chosen."""
        if user_input is not None:
            self._data.update(user_input)
            if user_input[CONF_CONNECTION_TYPE] == CONNECTION_TYPE_SSH:
                return await self.async_step_ssh()
            return await self.async_step_credentials()

        return self.async_show_form(
            step_id="manual_entry",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_HOST,
                        default=self._data.get(CONF_HOST, "192.168.1.1"),
                    ): str,
                    vol.Required(
                        CONF_CONNECTION_TYPE,
                        default=self._data.get(
                            CONF_CONNECTION_TYPE, CONNECTION_TYPE_LUCI_RPC
                        ),
                    ): vol.In(CONNECTION_TYPE_MAP),
                },
            ),
        )

    async def async_step_select_device(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Handle selecting a router when multiple are found."""
        if user_input is not None:
            host = user_input["device"]
            if host == "manual":
                return await self.async_step_manual_entry()

            router = next(r for r in self._discovered_routers if r["host"] == host)
            self._discovered_routers = [router]
            self._discovered_name = router.get("hostname")
            return await self.async_step_confirm_discovery()

        device_options = {
            r[
                "host"
            ]: f"{r.get('hostname', 'OpenWrt')} ({r['host']}) - {r['method'].upper()} [Available: {', '.join(r['capabilities'])}]"
            for r in self._discovered_routers
        }
        # Add manual entry option
        device_options["manual"] = "Enter details manually..."

        return self.async_show_form(
            step_id="select_device",
            data_schema=vol.Schema(
                {
                    vol.Required("device"): vol.In(device_options),
                },
            ),
            description_placeholders={"public_info": ""},
        )

    def _is_excluded(
        self,
        host: str,
        hostname: str | None = None,
        properties: Mapping[str, Any] | None = None,
    ) -> bool:
        """Centralized check for non-router OpenWrt devices like vacuums."""
        exclusions = [
            "valetudo",
            "vacuum",
            "dreame",
            "roborock",
            "cleaner",
            "mop",
            "robot",
            "airpurifier",
            "washer",
            "dryer",
            "fridge",
            "oven",
            "camera",
            "tuya",
            "broadlink",
            "shelly",
            "opnsense",
            "pfsense",
            "fortigate",
            "mikrotik",
            "ubnt",
            "unifi",
            "tplink",
            "asuswrt",
            "ddwrt",
            "padavan",
            "proxmox",
            "esxi",
            "truenas",
            "freenas",
        ]

        # Check hostname/name
        search_target = ""
        if hostname:
            search_target += hostname.lower()
        if properties:
            # Check all property values for exclusions
            for val in properties.values():
                if isinstance(val, str):
                    search_target += " " + val.lower()

        if any(exc in search_target for exc in exclusions):
            _LOGGER.info(
                "Definitively excluded %s (%s) as a non-router device",
                host,
                hostname,
            )
            return True

        return False

    async def _async_probe_router(
        self,
        host: str,
        hostname: str | None = None,
    ) -> dict[str, Any] | None:
        """Probe a host and return metadata if it's OpenWrt."""
        _LOGGER.debug("Probing router logic for %s (hint: %s)", host, hostname)

        # Definitive exclusions
        if self._is_excluded(host, hostname):
            return None

        effective_hostname = hostname
        if not effective_hostname or effective_hostname == host:
            try:
                loop = asyncio.get_running_loop()
                result = await loop.run_in_executor(None, socket.gethostbyaddr, host)
                effective_hostname = result[0]
            except Exception:
                effective_hostname = effective_hostname or host

        # Re-check exclusion after reverse DNS
        if self._is_excluded(host, effective_hostname):
            return None

        # Port accessibility check
        if not await self._async_check_reachable(
            host, CONNECTION_TYPE_UBUS
        ) and not await self._async_check_reachable(host, CONNECTION_TYPE_SSH):
            return None

        # Deep probe for LuCI/metadata and capabilities
        capabilities = []
        best_method = None

        # Check ubus/luci with the already resolved hostname
        probed_methods = await self._async_probe_openwrt(host, effective_hostname)
        if probed_methods:
            capabilities.extend(probed_methods)
            if CONNECTION_TYPE_UBUS in probed_methods:
                best_method = CONNECTION_TYPE_UBUS
            else:
                best_method = probed_methods[0]

        # Check SSH
        if await self._async_check_reachable(host, CONNECTION_TYPE_SSH):
            # Robust SSH banner check is already inside check_reachable (or should be)
            if "ssh" not in capabilities:
                capabilities.append("ssh")
            if not best_method:
                best_method = "ssh"

        if best_method:
            return {
                "host": host,
                "hostname": effective_hostname,
                "capabilities": capabilities,
                "method": best_method,
            }

        return None

    async def _async_check_reachable(self, host: str, connection_type: str) -> bool:
        """Check if the host is reachable on the expected ports."""
        ports = [22] if connection_type == CONNECTION_TYPE_SSH else [80, 443]
        if ":" in host:
            try:
                host_part, port_str = host.split(":")
                ports = [int(port_str)]
                host = host_part
            except ValueError:
                pass

        for port in ports:
            try:
                async with asyncio.timeout(1.5):
                    reader, writer = await asyncio.open_connection(host, port)
                    if port == 22:
                        # Try to read SSH banner to reduce false positives
                        try:
                            banner = await reader.read(100)
                            banner_str = banner.decode("utf-8", errors="ignore").lower()
                            if (
                                "dropbear" not in banner_str
                                and "openwrt" not in banner_str
                            ):
                                _LOGGER.debug(
                                    "Excluded %s: SSH banner '%s' does not look like OpenWrt",
                                    host,
                                    banner_str.strip(),
                                )
                                writer.close()
                                await writer.wait_closed()
                                continue
                        except Exception:
                            # If we can't read the banner, we still allow it but it's less certain
                            pass
                    writer.close()
                    await writer.wait_closed()
                    return True
            except (
                TimeoutError,
                socket.gaierror,
                ConnectionRefusedError,
                OSError,
            ):
                continue
        return False

    async def async_step_ssdp(
        self,
        discovery_info: SsdpServiceInfo,
    ) -> ConfigFlowResult:
        """Handle SSDP auto-discovery."""
        host = (
            urlparse(discovery_info.ssdp_location or "").hostname
            or discovery_info.ssdp_location
        )
        if not host:
            return self.async_abort(reason="no_host")
        self._async_abort_entries_match({CONF_HOST: host})

        # SSDP often includes a serial number which is often the MAC
        serial = discovery_info.upnp.get("serialNumber")
        if serial:
            # Serial is often the MAC or contains it
            unique_id = (
                dr.format_mac(serial) if ":" in serial or len(serial) == 12 else serial
            )
            await self.async_set_unique_id(unique_id)
            self._abort_if_unique_id_configured(updates={CONF_HOST: host})
        else:
            await self.async_set_unique_id(host)
            self._abort_if_unique_id_configured()

        hostname = discovery_info.upnp.get("friendlyName") or discovery_info.upnp.get(
            "modelName",
        )
        if self._is_excluded(host, hostname, discovery_info.upnp):
            return self.async_abort(reason="not_openwrt")

        probe_result = await self._async_probe_router(host, hostname)
        if not probe_result:
            return self.async_abort(reason="not_openwrt")

        self._discovered_routers = [probe_result]
        self._discovered_name = probe_result.get("hostname") or f"OpenWrt ({host})"

        self.context["title_placeholders"] = {
            "name": self._discovered_name,
            "host": host,
        }

        return await self.async_step_confirm_discovery()

    async def async_step_dhcp(
        self,
        discovery_info: DhcpServiceInfo,
    ) -> ConfigFlowResult:
        """Handle DHCP auto-discovery."""
        host = discovery_info.ip
        mac = discovery_info.macaddress
        self._async_abort_entries_match({CONF_HOST: host})
        await self.async_set_unique_id(dr.format_mac(mac))
        self._abort_if_unique_id_configured(updates={CONF_HOST: host})

        probe_result = await self._async_probe_router(host, discovery_info.hostname)
        if not probe_result:
            return self.async_abort(reason="not_openwrt")

        self._discovered_routers = [probe_result]
        self._discovered_name = probe_result.get("hostname") or f"OpenWrt ({host})"

        self.context.update(
            {
                "title_placeholders": {
                    "name": self._discovered_name,
                    "host": host,
                },
            },
        )

        return await self.async_step_confirm_discovery()

    async def async_step_zeroconf(
        self,
        discovery_info: ZeroconfServiceInfo,
    ) -> ConfigFlowResult:
        """Handle Zeroconf auto-discovery."""
        host = discovery_info.host
        # Zeroconf properties might have MAC
        mac = discovery_info.properties.get("mac")
        if mac:
            await self.async_set_unique_id(dr.format_mac(mac))
            self._abort_if_unique_id_configured(updates={CONF_HOST: host})
        else:
            await self.async_set_unique_id(host)
            self._abort_if_unique_id_configured()

        if self._is_excluded(host, discovery_info.name, discovery_info.properties):
            return self.async_abort(reason="not_openwrt")

        probe_result = await self._async_probe_router(host, discovery_info.name)
        if not probe_result:
            return self.async_abort(reason="not_openwrt")

        self._discovered_routers = [probe_result]
        self._discovered_name = probe_result.get("hostname") or f"OpenWrt ({host})"

        self.context.update(
            {
                "title_placeholders": {
                    "name": self._discovered_name,
                    "host": host,
                },
            },
        )

        return await self.async_step_confirm_discovery()

    async def async_step_confirm_discovery(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Confirm the discovered device."""
        if user_input is not None:
            if user_input.get(CONF_CONNECTION_TYPE) == "manual":
                return await self.async_step_manual_entry()

            router = self._discovered_routers[-1]
            self._data[CONF_HOST] = router["host"]
            self._data[CONF_CONNECTION_TYPE] = user_input.get(
                CONF_CONNECTION_TYPE,
                router["method"],
            )
            return await self.async_step_credentials()

        router = self._discovered_routers[-1]
        capabilities = ", ".join(
            [CONNECTION_TYPE_MAP.get(c, c) for c in router.get("capabilities", [])],
        )

        schema = vol.Schema({})
        options: list[str] = list(router.get("capabilities", []))
        if "manual" not in options:
            options.append("manual")

        if len(options) > 0:
            schema = vol.Schema(
                {
                    vol.Required(
                        CONF_CONNECTION_TYPE,
                        default=router["method"],
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=options,
                            translation_key="connection_type",
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        ),
                    ),
                },
            )

        return self.async_show_form(
            step_id="confirm_discovery",
            data_schema=schema,
            description_placeholders={
                "name": self._discovered_name or "OpenWrt Router",
                "host": router["host"],
                "method": CONNECTION_TYPE_MAP.get(router["method"], router["method"]),
                "capabilities": capabilities,
            },
        )
