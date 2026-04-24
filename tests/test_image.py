from unittest.mock import MagicMock, patch

import pytest

from custom_components.openwrt.api.base import WifiCredentials
from custom_components.openwrt.image import OpenWrtWifiQrImage


def test_image_entity_init():
    """Test image entity initialization."""
    coordinator = MagicMock()
    coordinator.interface_to_stable_id = {"wlan0": "wifinet1"}
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.unique_id = "test_mac"

    cred = WifiCredentials(
        iface="wlan0", ssid="TestSSID", key="TestPass", encryption="psk2", hidden=False
    )

    with patch("custom_components.openwrt.image.DeviceInfo") as mock_device_info:
        with patch("homeassistant.components.image.ImageEntity.__init__"):
            entity = OpenWrtWifiQrImage(None, coordinator, entry, cred)

        assert entity.unique_id == "test_entry_wifi_qr_TestSSID"
        assert entity.name == "Wi-Fi QR Code (TestSSID)"
        # Check that DeviceInfo was called with correct identifiers
        mock_device_info.assert_called_once()
        args, kwargs = mock_device_info.call_args
        assert kwargs["identifiers"] == {("openwrt", "test_mac")}


@pytest.mark.asyncio
async def test_image_generation():
    """Test QR code generation logic."""
    coordinator = MagicMock()
    coordinator.interface_to_stable_id = {"wlan0": "wifinet1"}
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.unique_id = "test_mac"

    cred = WifiCredentials(
        iface="wlan0", ssid="TestSSID", key="TestPass", encryption="psk2", hidden=False
    )
    coordinator.data.wifi_credentials = [cred]

    with patch("homeassistant.components.image.ImageEntity.__init__"):
        entity = OpenWrtWifiQrImage(None, coordinator, entry, cred)

    # Mock segno in sys.modules if not present
    import sys

    mock_segno = MagicMock()
    with patch.dict(sys.modules, {"segno": mock_segno}):
        mock_qr = MagicMock()
        mock_segno.make.return_value = mock_qr

        await entity.async_image()

        mock_segno.make.assert_called_once()
        qr_str = mock_segno.make.call_args[0][0]
        assert "WIFI:S:TestSSID" in qr_str
        assert "P:TestPass" in qr_str
