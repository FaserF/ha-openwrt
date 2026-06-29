"""Tests for GPS location updates from Quectel modem."""

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
    assert parse_nmea_coordinate("5201.0090N") == pytest.approx(52.01681667)
    # Test valid South (negative)
    assert parse_nmea_coordinate("5201.0090S") == pytest.approx(-52.01681667)
    # Test valid East
    assert parse_nmea_coordinate("00043.0931E") == pytest.approx(0.71821833)
    # Test valid West (negative)
    assert parse_nmea_coordinate("00043.0931W") == pytest.approx(-0.71821833)
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
        "AT+QGPSLOC?\r\n"
        "+QGPSLOC: 105742.00,5201.0090N,00043.0931W,0.8,78.2,3,,0.0,0.0,290626,13\r\n"
        "OK\r\n"
    )
    coords = parse_qgpsloc_response(response)
    assert coords is not None
    lat, lon = coords
    assert lat == pytest.approx(52.01681667)
    assert lon == pytest.approx(-0.71821833)

    # Test invalid response without +QGPSLOC
    assert parse_qgpsloc_response("ERROR") is None

    # Test short response
    assert parse_qgpsloc_response("+QGPSLOC: 105742.00,5201.0090N") is None


async def test_async_update_gps_location(hass):
    """Test async_update_gps_location flow."""
    mock_client = MagicMock()
    mock_client.execute_command = AsyncMock()

    # Case 1: GPS is already enabled and returns valid coordinates
    mock_client.execute_command.side_effect = [
        "+QGPS: 1\r\nOK",  # Check if enabled
        "+QGPSLOC: 105742.00,5201.0090N,00043.0931W,0.8,78.2,3,,0.0,0.0,290626,13\r\nOK",  # Location
    ]

    with patch.object(
        hass.services, "async_call", new_callable=AsyncMock
    ) as mock_service_call:
        res = await async_update_gps_location(hass, mock_client, "/dev/ttyUSB3")
        assert res is True
        mock_service_call.assert_called_once_with(
            "homeassistant",
            "set_location",
            {
                "latitude": pytest.approx(52.01681667),
                "longitude": pytest.approx(-0.71821833),
            },
        )

    # Case 2: GPS is disabled, we enable it and then query location
    mock_client.execute_command.reset_mock()
    mock_client.execute_command.side_effect = [
        "+QGPS: 0\r\nOK",  # Check if enabled -> returns 0
        "OK",  # Enable command AT+QGPS=1 response
        "+QGPSLOC: 105742.00,5201.0090N,00043.0931W,0.8,78.2,3,,0.0,0.0,290626,13\r\nOK",  # Location
    ]

    with patch.object(
        hass.services, "async_call", new_callable=AsyncMock
    ) as mock_service_call:
        res = await async_update_gps_location(hass, mock_client, "/dev/ttyUSB3")
        assert res is True
        assert mock_client.execute_command.call_count == 3
        mock_service_call.assert_called_once()
