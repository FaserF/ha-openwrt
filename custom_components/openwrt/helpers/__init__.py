"""Helper functions for OpenWrt integration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from homeassistant.core import HomeAssistant

from ..const import CONF_HOST, DOMAIN

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


def normalize_band(band: str | None) -> str:
    """Normalize raw frequency or band strings to a standard format.

    Examples:
        "2412" -> "2.4 GHz"
        "5180" -> "5 GHz"
        "2.412" -> "2.4 GHz"
        "5 GHz" -> "5 GHz"
    """
    if not band:
        return "unknown"

    freq_str = str(band).lower().strip()

    # Handle numeric MHz/GHz strings
    try:
        # Remove units if present for numeric check
        clean_freq = freq_str.replace("ghz", "").replace("mhz", "").strip()
        if clean_freq.replace(".", "").isdigit():
            freq = float(clean_freq)
            # Frequencies in MHz
            if 2000 <= freq <= 3000:
                return "2.4 GHz"
            if 4900 <= freq <= 5900:
                return "5 GHz"
            if 5900 < freq <= 7200:
                return "6 GHz"
            # Frequencies in GHz
            if 2.0 <= freq <= 3.0:
                return "2.4 GHz"
            if 4.9 <= freq <= 5.9:
                return "5 GHz"
            if 5.9 < freq <= 7.2:
                return "6 GHz"
    except ValueError:
        pass

    # Fallback to keyword matching
    if "2.4" in freq_str:
        return "2.4 GHz"
    if "5" in freq_str:
        return "5 GHz"
    if "6" in freq_str:
        return "6 GHz"

    return freq_str if "ghz" in freq_str else f"{freq_str} GHz"


def format_ap_name(ssid: str, band: str = "") -> str:
    """Format the display name for an Access Point device.

    Examples:
        format_ap_name("SmartLife", "2.4 GHz") -> "AP SmartLife (2.4 GHz)"
        format_ap_name("SmartLife", "2412")     -> "AP SmartLife (2.4 GHz)"
    """
    label = ssid
    norm_band = normalize_band(band) if band else ""

    if norm_band and norm_band != "unknown":
        return f"AP {label} ({norm_band})"
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
        # Check the 'locally administered' bit (bit 1 of first byte)
        first_byte = int(clean_mac[:2], 16)
        return bool(first_byte & 0x02)
    except ValueError, IndexError:
        return False


def parse_uci_bool(value: Any, default: bool = False) -> bool:
    """Parse truthy/falsy UCI values robustly.

    Handles strings ('1', 'yes', 'on', 'true', 'enabled' as True),
    integers, booleans, and None.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        val = value.lower().strip()
        if val in ("1", "yes", "on", "true", "enabled"):
            return True
        if val in ("0", "no", "off", "false", "disabled"):
            return False
    return default


def get_via_device(
    hass: HomeAssistant,
    coordinator: Any,  # Avoid circular import
    entry: ConfigEntry,
    mac: str,
) -> tuple[str, str]:
    """Resolve the via_device for a connected device.

    Returns a tuple (DOMAIN, identifier). Falls back to the router if
    the AP device is not found in the registry or if the device is wired.
    """
    from homeassistant.helpers import device_registry as dr

    router_id = cast(str, entry.unique_id or entry.data[CONF_HOST])
    via_device = (DOMAIN, router_id)

    if coordinator.data:
        mac_lower = mac.lower()
        # 1. Check local wireless interfaces
        for device in coordinator.data.connected_devices:
            if (
                device.mac
                and device.mac.lower() == mac_lower
                and device.is_wireless
                and device.interface
            ):
                stable_id = coordinator.interface_to_stable_id.get(device.interface)
                if stable_id:
                    ap_id = format_ap_device_id(router_id, stable_id)
                    # Verify the AP device exists to avoid "non existing via_device" warnings
                    dev_reg = dr.async_get(hass)
                    if dev_reg.async_get_device(identifiers={(DOMAIN, ap_id)}):
                        via_device = (DOMAIN, ap_id)
                break

        # 2. If not local wireless, check Batman-adv mesh for remote nodes
        if (
            via_device == (DOMAIN, router_id)
            and mac_lower in coordinator.data.batman_translation_table
        ):
            originator_mac = coordinator.data.batman_translation_table[
                mac_lower
            ].lower()
            if (
                coordinator.data.device_info
                and coordinator.data.device_info.mac_address
                and originator_mac != coordinator.data.device_info.mac_address.lower()
            ):
                # It's behind another mesh node. Verify it exists in registry.
                dev_reg = dr.async_get(hass)
                if dev_reg.async_get_device(identifiers={(DOMAIN, originator_mac)}):
                    via_device = (DOMAIN, originator_mac)

    return via_device
