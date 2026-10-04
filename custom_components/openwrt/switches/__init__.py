"""Switches package for OpenWrt."""

from __future__ import annotations

from .security import (
    OpenWrtAccessControlSwitch,
    OpenWrtFirewallRuleSwitch,
    OpenWrtFirewallSwitch,
    OpenWrtLedSwitch,
    OpenWrtWireGuardSwitch,
    _add_access_control_switches,
    _add_firewall_switches,
    _add_led_switches,
    _add_vpn_switches,
)
from .services import (
    SERVICE_ICONS,
    OpenWrtAdBlockSwitch,
    OpenWrtBanIpSwitch,
    OpenWrtServiceSwitch,
    OpenWrtSimpleAdBlockSwitch,
    OpenWrtSqmSwitch,
    _add_package_switches,
    _add_service_switches,
    _add_sqm_switches,
)
from .wireless import (
    OpenWrtRadioSwitch,
    OpenWrtWirelessSwitch,
    OpenWrtWpsSwitch,
    _add_wireless_switches,
)

__all__ = [
    "OpenWrtAccessControlSwitch",
    "OpenWrtAdBlockSwitch",
    "OpenWrtBanIpSwitch",
    "OpenWrtFirewallRuleSwitch",
    "OpenWrtFirewallSwitch",
    "OpenWrtLedSwitch",
    "OpenWrtRadioSwitch",
    "OpenWrtServiceSwitch",
    "OpenWrtSimpleAdBlockSwitch",
    "OpenWrtSqmSwitch",
    "OpenWrtWireGuardSwitch",
    "OpenWrtWirelessSwitch",
    "OpenWrtWpsSwitch",
    "SERVICE_ICONS",
    "_add_access_control_switches",
    "_add_firewall_switches",
    "_add_led_switches",
    "_add_package_switches",
    "_add_service_switches",
    "_add_sqm_switches",
    "_add_vpn_switches",
    "_add_wireless_switches",
]
