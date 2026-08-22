"""Tests for GPS location updates from Quectel modem."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.openwrt.helpers.gps import (
    async_update_gps_location,
    parse_nmea_coordinate,
    parse_qgpsloc_response,
)


@pytest.fixture(autouse=True)
def enable_socket(socket_enabled):
    """Enable socket calls for Windows asyncio loop initialization."""
    pass


def test_parse_nmea_coordinate():
    """Test parsing NMEA coordinate string into decimal degrees."""
    # Test valid North
    assert parse_nmea_coordinate("5201.0090N") == pytest.approx(52.016817)
    # Test valid South (negative)
    assert parse_nmea_coordinate("5201.0090S") == pytest.approx(-52.016817)
    # Test valid East
    assert parse_nmea_coordinate("00043.0931E") == pytest.approx(0.718218)
    # Test valid West (negative)
    assert parse_nmea_coordinate("00043.0931W") == pytest.approx(-0.718218)
    # Test empty or short
    assert parse_nmea_coordinate("") is None
    assert parse_nmea_coordinate("12") is None
    # Test invalid directions
    assert parse_nmea_coordinate("5201.0090X") is None
    # Test invalid formats
    assert parse_nmea_coordinate("52010090N") is None
    assert parse_nmea_coordinate("abcN") is None


def test_parse_qgpsloc_response():
    """Test parsing standard +QGPSLOC response."""
    response = (
        "===GPS_LOC===\r\n"
        "+QGPSLOC: 105742.00,5201.0090N,00043.0931W,0.8,78.2,3,,0.0,0.0,290626,13\r\n"
        "OK\r\n"
    )
    coords = parse_qgpsloc_response(response)
    assert coords is not None
    lat, lon = coords
    assert lat == pytest.approx(52.016817)
    assert lon == pytest.approx(-0.718218)

    # Test invalid response without +QGPSLOC
    assert parse_qgpsloc_response("ERROR") is None

    # Test short response
    assert parse_qgpsloc_response("+QGPSLOC: 105742.00,5201.0090N") is None


async def test_async_update_gps_location(hass):
    """Test async_update_gps_location flow."""
    mock_client = MagicMock()
    mock_client.execute_command = AsyncMock()

    # Mock stty and timeout checks to pass
    mock_client.execute_command.side_effect = [
        "/usr/bin/stty",  # command -v stty
        "/usr/bin/timeout",  # command -v timeout
        "===GPS_LOC===\n+QGPSLOC: 105742.00,5201.0090N,00043.0931W,0.8,78.2,3,,0.0,0.0,290626,13\nOK",  # location output
    ]

    with (
        patch.object(
            hass.services, "async_call", new_callable=AsyncMock
        ) as mock_service_call,
        patch(
            "custom_components.openwrt.helpers.gps.dt_util.now",
            return_value=datetime(2026, 1, 1, tzinfo=UTC),
        ),
    ):
        res = await async_update_gps_location(hass, mock_client, "/dev/ttyUSB3")
        assert res is not None
        assert res[0] == pytest.approx(52.016817)
        assert res[1] == pytest.approx(-0.718218)
        assert isinstance(res[2], datetime)
        mock_service_call.assert_called_once_with(
            "homeassistant",
            "set_location",
            {
                "latitude": pytest.approx(52.016817),
                "longitude": pytest.approx(-0.718218),
            },
        )
        assert mock_client.execute_command.call_count == 3
