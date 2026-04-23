"""Helper functions for OpenWrt integration."""

from __future__ import annotations

from ..const import DOMAIN


def format_ap_identifier(router_id: str, iface_name: str) -> str:
    """Format the identifier for an Access Point device."""
    return f"{router_id}_ap_{iface_name}"


def format_ap_device_id(router_id: str, iface_name: str) -> tuple[str, str]:
    """Format the device registry identifier tuple for an AP."""
    return (DOMAIN, format_ap_identifier(router_id, iface_name))
