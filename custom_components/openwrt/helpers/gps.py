"""GPS helper functions for OpenWrt integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


def parse_nmea_coordinate(coord_str: str) -> float | None:
    """Parse NMEA coordinate string (ddmm.mmmmN/S or dddmm.mmmmE/W) into decimal degrees."""
    if not coord_str or len(coord_str) < 3:
        return None
    direction = coord_str[-1].upper()
    if direction not in ("N", "S", "E", "W"):
        return None
    num_str = coord_str[:-1]
    try:
        dot_idx = num_str.find(".")
        if dot_idx == -1:
            return None
        degrees_str = num_str[: dot_idx - 2]
        minutes_str = num_str[dot_idx - 2 :]
        degrees = float(degrees_str) if degrees_str else 0.0
        minutes = float(minutes_str)
        val = degrees + (minutes / 60.0)
        if direction in ("S", "W"):
            val = -val
        return val
    except ValueError:
        return None


def parse_qgpsloc_response(response: str) -> tuple[float, float] | None:
    """Parse the +QGPSLOC response into (latitude, longitude)."""
    for line in response.splitlines():
        line = line.strip()
        if "+QGPSLOC:" in line:
            # Strip anything before "+QGPSLOC: " to handle echoes or prefixes
            start = line.find("+QGPSLOC:")
            content = line[start + len("+QGPSLOC:") :].strip()
            parts = content.split(",")
            if len(parts) >= 3:
                lat_part = parts[1].strip()
                lon_part = parts[2].strip()
                lat = parse_nmea_coordinate(lat_part)
                lon = parse_nmea_coordinate(lon_part)
                if lat is not None and lon is not None:
                    return lat, lon
    return None


async def async_update_gps_location(
    hass: HomeAssistant,
    client: Any,
    port: str,
) -> bool:
    """Query GPS coordinates from modem and update HA location."""
    try:
        # Check if GPS is enabled
        check_cmd = f'echo -e "AT+QGPS?\\r" | microcom -t 1000 {port}'
        check_res = await client.execute_command(check_cmd)

        # If QGPS is disabled or query returned error, try enabling it
        if "+QGPS: 0" in check_res or "ERROR" in check_res:
            _LOGGER.debug("GPS is disabled, enabling via AT+QGPS=1")
            enable_cmd = f'echo -e "AT+QGPS=1\\r" | microcom -t 1000 {port}'
            await client.execute_command(enable_cmd)

        # Poll the GPS location
        loc_cmd = f'echo -e "AT+QGPSLOC?\\r" | microcom -t 2000 {port}'
        loc_res = await client.execute_command(loc_cmd)

        coords = parse_qgpsloc_response(loc_res)
        if coords:
            lat, lon = coords
            _LOGGER.info("Updating Home Assistant location to: %s, %s", lat, lon)
            await hass.services.async_call(
                "homeassistant",
                "set_location",
                {
                    "latitude": lat,
                    "longitude": lon,
                },
            )
            return True
        _LOGGER.debug(
            "Failed to get valid GPS fix or coordinates from %s: %s", port, loc_res
        )
    except Exception as err:
        _LOGGER.warning("Failed to update GPS location from Quectel modem: %s", err)
    return False
