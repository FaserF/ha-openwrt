"""Test the OpenWrt WireGuard API."""

from unittest.mock import AsyncMock, patch

import pytest

from custom_components.openwrt.api.ubus import UbusClient


@pytest.fixture
def ubus_client() -> UbusClient:
    """Fixture for Ubus client."""
    return UbusClient(host="192.168.1.1", username="root", password="password")

@pytest.mark.asyncio
async def test_ubus_get_wireguard_interfaces(ubus_client: UbusClient):
    """Test fetching WireGuard interfaces via Ubus."""
    with patch.object(ubus_client, "_call", new_callable=AsyncMock) as mock_call, \
         patch.object(ubus_client, "execute_command", new_callable=AsyncMock) as mock_exec:

        # 1. Mock network.interface dump
        mock_call.side_effect = [
            {
                "interface": [
                    {"interface": "wg0", "proto": "wireguard"},
                    {"interface": "lan", "proto": "static"}
                ]
            }
        ]

        # 2. Mock wg show all dump
        # Format: interface public_key listen_port fwmark
        # Format: interface peer_public_key preshared_key endpoint allowed_ips latest_handshake transfer_rx transfer_tx persistent_keepalive
        mock_exec.return_value = (
            "wg0\tPUBKEY_IFACE\t51820\t0\n"
            "wg0\tPUBKEY_PEER\t(none)\t1.2.3.4:5678\t10.0.0.2/32\t1624531234\t1024\t2048\t25\n"
        )

        interfaces = await ubus_client.get_wireguard_interfaces()

        assert len(interfaces) == 1
        wg0 = interfaces[0]
        assert wg0.name == "wg0"
        assert wg0.public_key == "PUBKEY_IFACE"
        assert wg0.listen_port == 51820

        assert len(wg0.peers) == 1
        peer = wg0.peers[0]
        assert peer.public_key == "PUBKEY_PEER"
        assert peer.endpoint == "1.2.3.4:5678"
        assert peer.allowed_ips == ["10.0.0.2/32"]
        assert peer.latest_handshake == 1624531234
        assert peer.transfer_rx == 1024
        assert peer.transfer_tx == 2048
        assert peer.persistent_keepalive == 25
