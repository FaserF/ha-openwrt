"""Helper functions for OpenWrt integration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..const import CONF_HOST

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry


def _router_id(entry: ConfigEntry) -> str:
    """Extract the canonical router ID from a config entry."""
    return str(entry.unique_id or entry.data[CONF_HOST])


def format_ap_identifier(entry_or_router_id: ConfigEntry | str, iface_name: str) -> str:
    """Format the identifier string for an Access Point device."""
    if isinstance(entry_or_router_id, str):
        router_id = entry_or_router_id
    else:
        router_id = _router_id(entry_or_router_id)
    return f"{router_id}_ap_{iface_name}"


def format_ap_device_id(entry_or_router_id: ConfigEntry | str, iface_name: str) -> str:
    """Return the string identifier used in the device registry for an AP."""
    return format_ap_identifier(entry_or_router_id, iface_name)


def format_ap_name(ssid: str, band: str = "") -> str:
    """Format the display name for an Access Point device.

    Examples:
        format_ap_name("SmartLife", "2.4 GHz") -> "AP SmartLife (2.4 GHz)"
        format_ap_name("SmartLife", "2412")     -> "AP SmartLife (2.4 GHz)"
    """
    label = ssid
    # Normalise raw frequency strings like "2.412" or "2412" -> "2.4 GHz"
    if band:
        freq_str = str(band).lower()
        if "2.4" in freq_str or (
            freq_str.replace(".", "").isdigit() and 2000 <= float(freq_str) <= 3000
        ):
            band = "2.4 GHz"
        elif "5" in freq_str or (
            freq_str.replace(".", "").isdigit() and 4900 <= float(freq_str) <= 5900
        ):
            band = "5 GHz"
        elif "6" in freq_str or (
            freq_str.replace(".", "").isdigit() and 5900 < float(freq_str) <= 7200
        ):
            band = "6 GHz"
        elif "ghz" not in freq_str:
            band = f"{band} GHz"

    if band:
        return f"AP {label} ({band})"
    return f"AP {label}"


def is_random_mac(mac: str) -> bool:
    """Check if a MAC address is locally administered (randomized).

    A MAC address is randomized if the 'locally administered' bit is set
    in the first byte (the second-least significant bit).
    """
    if not mac:
        return False
    try:
        # Normalize: remove separators and take the first two chars (first byte)
        clean_mac = mac.replace(":", "").replace("-", "").replace(".", "")
        if len(clean_mac) < 2:
            return False
        first_byte = int(clean_mac[:2], 16)
        # Check the 'locally administered' bit (bit 1 of first byte)
        return bool(first_byte & 0x02)
    except ValueError, IndexError:
        return False
