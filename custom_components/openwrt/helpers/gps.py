"""GPS helper functions for OpenWrt integration."""

from __future__ import annotations

import logging
import shlex
from datetime import datetime
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)


# ... (skipping unchanged code for clarity, target matching handles replacement)


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
        return round(val, 6)
    except ValueError:
        return None


def parse_qgpsloc_response(response: str) -> tuple[float, float] | None:
    """Parse the +QGPSLOC response into (latitude, longitude)."""
    for line in response.splitlines():
        line = line.strip()
        if "+QGPSLOC:" in line:
            # Strip anything before "+QGPSLOC:" to handle echoes or prefixes
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
) -> tuple[float, float, datetime] | None:
    """Query GPS coordinates from modem, update HA location and return coordinates."""
    try:
        # Check if stty and timeout are available on the router
        stty_check = await client.execute_command("command -v stty")
        timeout_check = await client.execute_command("command -v timeout")
        if not stty_check or not timeout_check:
            _LOGGER.warning(
                "GPS tracking requires 'stty' and 'timeout' utilities on the router. "
                "Please install them (e.g. 'opkg update && opkg install busybox' or "
                "'apk add coreutils-stty coreutils-timeout')."
            )
            return None

        # Build single robust shell script to query the modem using file descriptor redirection
        cmd = (
            f"exec 3<>{shlex.quote(port)}; "
            f"stty 9600 cs8 -parenb -cstopb raw -echo min 0 time 20 <&3 2>/dev/null || "
            f"stty -F {shlex.quote(port)} 9600 cs8 -parenb -cstopb raw -echo min 0 time 20 2>/dev/null; "
            f"printf 'AT+QGPS?\\r' >&3; "
            f"GPS_STATUS=$(timeout 2 cat <&3); "
            f"if ! echo \"$GPS_STATUS\" | grep -q '+QGPS: 1'; then "
            f"printf 'AT+QGPS=1\\r' >&3; "
            f"sleep 5; "
            f"timeout 1 cat <&3 >/dev/null; "
            f"fi; "
            f"printf 'AT+QGPSLOC?\\r' >&3; "
            f"GPS_LOC=$(timeout 3 cat <&3); "
            f"exec 3>&-; "
            f'echo "===GPS_LOC==="; '
            f'echo "$GPS_LOC"'
        )

        res = await client.execute_command(cmd)

        coords = parse_qgpsloc_response(res)
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
            last_update = dt_util.now()
            return lat, lon, last_update

        _LOGGER.debug(
            "Failed to get valid GPS fix or coordinates from %s: %s", port, res
        )
    except Exception as err:
        _LOGGER.warning("Failed to update GPS location from Quectel modem: %s", err)
    return None
