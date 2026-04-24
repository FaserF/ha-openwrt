"""Test the OpenWrt UPnP API."""

from unittest.mock import AsyncMock, patch

import pytest

from custom_components.openwrt.api.ubus import UbusClient


@pytest.fixture
def ubus_client() -> UbusClient:
    """Fixture for Ubus client."""
    return UbusClient(host="192.168.1.1", username="root", password="password")


@pytest.mark.asyncio
async def test_ubus_get_upnp_mappings(ubus_client: UbusClient):
    """Test fetching UPnP mappings via Ubus."""
    with patch.object(ubus_client, "_call", new_callable=AsyncMock) as mock_call:
        # 1. Mock upnp get_mappings
        mock_call.return_value = {
            "mappings": [
                {
                    "protocol": "TCP",
                    "ext_port": 1234,
                    "int_addr": "192.168.1.10",
                    "int_port": 1234,
                    "descr": "Test Game",
                    "enabled": 1,
                }
            ]
        }

        mappings = await ubus_client.get_upnp_mappings()

        assert len(mappings) == 1
        m1 = mappings[0]
        assert m1.protocol == "TCP"
        assert m1.external_port == 1234
        assert m1.internal_ip == "192.168.1.10"
        assert m1.internal_port == 1234
        assert m1.description == "Test Game"
        assert m1.enabled is True
