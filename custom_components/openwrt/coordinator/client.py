"""Client creation helper for OpenWrt coordinator."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from ..api.base import OpenWrtClient
from ..api.luci_rpc import LuciRpcClient
from ..api.ssh import SshClient
from ..api.ubus import UbusClient
from ..const import (
    CONF_CONNECTION_TYPE,
    CONF_DHCP_SOFTWARE,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_SSH_KEY,
    CONF_TRUST_BRIDGE_FDB,
    CONF_TRUST_STALE_ARP,
    CONF_UBUS_PATH,
    CONF_USE_SSL,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
    CONNECTION_TYPE_LUCI_RPC,
    CONNECTION_TYPE_SSH,
    CONNECTION_TYPE_UBUS,
    DEFAULT_PORT_SSH,
    DEFAULT_PORT_UBUS,
    DEFAULT_PORT_UBUS_SSL,
    DEFAULT_UBUS_PATH,
)

_LOGGER = logging.getLogger(__name__)


def create_client(hass: HomeAssistant, config: Mapping[str, Any]) -> OpenWrtClient:
    """Create the appropriate API client based on configuration."""
    connection_type = config.get(CONF_CONNECTION_TYPE, CONNECTION_TYPE_UBUS)
    host = config[CONF_HOST]
    username = config[CONF_USERNAME]
    password = config.get(CONF_PASSWORD, "")
    use_ssl = config.get(CONF_USE_SSL, False)
    verify_ssl = config.get(CONF_VERIFY_SSL, False)
    dhcp_software = config.get(CONF_DHCP_SOFTWARE, "auto")

    trust_stale_arp = config.get(CONF_TRUST_STALE_ARP, True)
    trust_bridge_fdb = config.get(CONF_TRUST_BRIDGE_FDB, True)

    _LOGGER.debug("Creating client for router (type: %s)", connection_type)

    if connection_type == CONNECTION_TYPE_SSH:
        port = config.get(CONF_PORT, DEFAULT_PORT_SSH)
        return SshClient(
            hass=hass,
            session=None,
            host=host,
            username=username,
            password=password,
            port=port,
            ssh_key=config.get(CONF_SSH_KEY),
            dhcp_software=dhcp_software,
            trust_stale_arp=trust_stale_arp,
            trust_bridge_fdb=trust_bridge_fdb,
        )

    if connection_type == CONNECTION_TYPE_LUCI_RPC:
        port = config.get(
            CONF_PORT,
            DEFAULT_PORT_UBUS_SSL if use_ssl else DEFAULT_PORT_UBUS,
        )
        return LuciRpcClient(
            hass=hass,
            session=async_get_clientsession(hass),
            host=host,
            username=username,
            password=password,
            port=port,
            use_ssl=use_ssl,
            verify_ssl=verify_ssl,
            dhcp_software=dhcp_software,
            trust_stale_arp=trust_stale_arp,
            trust_bridge_fdb=trust_bridge_fdb,
        )

    port = config.get(
        CONF_PORT,
        DEFAULT_PORT_UBUS_SSL if use_ssl else DEFAULT_PORT_UBUS,
    )
    return UbusClient(
        hass=hass,
        session=async_get_clientsession(hass),
        host=host,
        username=username,
        password=password,
        port=port,
        use_ssl=use_ssl,
        verify_ssl=verify_ssl,
        ubus_path=config.get(CONF_UBUS_PATH, DEFAULT_UBUS_PATH),
        dhcp_software=dhcp_software,
        trust_stale_arp=trust_stale_arp,
        trust_bridge_fdb=trust_bridge_fdb,
    )
