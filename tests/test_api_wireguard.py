"""Test the OpenWrt WireGuard API."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.openwrt.api.ubus import UbusClient


@pytest.fixture
def ubus_client() -> UbusClient:
    """Fixture for Ubus client."""
    return UbusClient(
        MagicMock(),
        MagicMock(),
        host="192.168.1.1",
        username="root",
        password="password",
    )


@pytest.mark.asyncio
async def test_ubus_get_wireguard_interfaces(ubus_client: UbusClient):
    """Test fetching WireGuard interfaces via Ubus."""
    with (
        patch.object(ubus_client, "_call", new_callable=AsyncMock) as mock_call,
        patch.object(
            ubus_client, "execute_command", new_callable=AsyncMock
        ) as mock_exec,
    ):
        # 1. Mock network.interface dump
        mock_call.side_effect = [
            {
                "interface": [
                    {"interface": "wg0", "proto": "wireguard", "up": True},
                    {"interface": "lan", "proto": "static"},
                ]
            }
        ]

        # 2. Mock `wg show all dump`. In `all` mode every line is prefixed with
        # the interface name, so:
        #   interface: iface private_key public_key listen_port fwmark   (5 cols)
        #   peer:      iface public_key preshared_key endpoint allowed_ips
        #              latest_handshake transfer_rx transfer_tx persistent_keepalive  (9 cols)
        mock_exec.return_value = (
            "wg0\tPRIVKEY_IFACE\tPUBKEY_IFACE\t51820\t0\n"
            "wg0\tPUBKEY_PEER\t(none)\t1.2.3.4:5678\t10.0.0.2/32\t1624531234\t1024\t2048\t25\n"
        )

        interfaces = await ubus_client.get_wireguard_interfaces()

        assert len(interfaces) == 1
        wg0 = interfaces[0]
        assert wg0.name == "wg0"
        # public key is the 3rd column (after the private key), not the 2nd
        assert wg0.public_key == "PUBKEY_IFACE"
        assert wg0.listen_port == 51820
        assert wg0.fwmark == 0
        assert wg0.enabled is True

        assert len(wg0.peers) == 1
        peer = wg0.peers[0]
        assert peer.public_key == "PUBKEY_PEER"
        assert peer.endpoint == "1.2.3.4:5678"
        assert peer.allowed_ips == ["10.0.0.2/32"]
        assert peer.latest_handshake == 1624531234
        assert peer.transfer_rx == 1024
        assert peer.transfer_tx == 2048
        assert peer.persistent_keepalive == 25


@pytest.mark.asyncio
async def test_ubus_wireguard_none_values_and_unknown_iface(ubus_client: UbusClient):
    """'(none)' endpoint/allowed-ips map to empty, a down interface is not
    enabled, and peer lines for an unknown interface are ignored."""
    with (
        patch.object(ubus_client, "_call", new_callable=AsyncMock) as mock_call,
        patch.object(
            ubus_client, "execute_command", new_callable=AsyncMock
        ) as mock_exec,
    ):
        mock_call.side_effect = [
            {"interface": [{"interface": "wg0", "proto": "wireguard", "up": False}]}
        ]
        mock_exec.return_value = (
            "wg0\tPRIVKEY\tPUBKEY0\t51820\t0\n"
            "wg0\tPEER_A\t(none)\t(none)\t(none)\t0\t0\t0\t0\n"
            # peer under an interface that is not a configured WG interface
            "other\tPEER_B\t(none)\t9.9.9.9:1\t10.0.0.9/32\t0\t0\t0\t0\n"
        )

        interfaces = await ubus_client.get_wireguard_interfaces()

        assert len(interfaces) == 1
        wg0 = interfaces[0]
        assert wg0.enabled is False
        assert len(wg0.peers) == 1
        peer = wg0.peers[0]
        assert peer.public_key == "PEER_A"
        assert peer.endpoint == ""
        assert peer.allowed_ips == []


@pytest.mark.asyncio
async def test_ubus_wireguard_4_columns(ubus_client: UbusClient):
    """Test fetching WireGuard interfaces via Ubus with 4-column interface line."""
    with (
        patch.object(ubus_client, "_call", new_callable=AsyncMock) as mock_call,
        patch.object(
            ubus_client, "execute_command", new_callable=AsyncMock
        ) as mock_exec,
    ):
        mock_call.side_effect = [
            {"interface": [{"interface": "wg0", "proto": "wireguard", "up": True}]}
        ]
        mock_exec.return_value = (
            "wg0\tPUBKEY0\t51820\t0\nwg0\tPEER_A\t(none)\t(none)\t(none)\t0\t0\t0\t0\n"
        )

        interfaces = await ubus_client.get_wireguard_interfaces()

        assert len(interfaces) == 1
        wg0 = interfaces[0]
        assert wg0.enabled is True
        assert wg0.public_key == "PUBKEY0"
        assert wg0.listen_port == 51820
        assert wg0.fwmark == 0
        assert len(wg0.peers) == 1
        peer = wg0.peers[0]
        assert peer.public_key == "PEER_A"
