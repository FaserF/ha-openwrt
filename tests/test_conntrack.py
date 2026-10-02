"""Tests for connection-tracking (conntrack) metric collection."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.openwrt.api.base import SystemResources
from custom_components.openwrt.api.ubus import UbusClient


@pytest.fixture
def ubus_client() -> UbusClient:
    return UbusClient(
        MagicMock(),
        MagicMock(),
        host="192.168.1.1",
        username="root",
        password="password",
    )


@pytest.mark.asyncio
async def test_fetch_conntrack_populates(ubus_client: UbusClient):
    """count/max are parsed from the two /proc entries via read_file."""
    res = SystemResources()

    async def fake_read(path: str):
        if path.endswith("nf_conntrack_count"):
            return "256\n"
        if path.endswith("nf_conntrack_max"):
            return "262144\n"
        return None

    with patch.object(ubus_client, "read_file", side_effect=fake_read):
        await ubus_client._fetch_conntrack(res)

    assert res.conntrack_count == 256
    assert res.conntrack_max == 262144


@pytest.mark.asyncio
async def test_fetch_conntrack_missing_stays_zero(ubus_client: UbusClient):
    """If the reads fail/return nothing, the counters stay at 0."""
    res = SystemResources()
    with patch.object(ubus_client, "read_file", new_callable=AsyncMock) as mock_read:
        mock_read.return_value = None
        await ubus_client._fetch_conntrack(res)

    assert res.conntrack_count == 0
    assert res.conntrack_max == 0


@pytest.mark.asyncio
async def test_flush_conntrack_for_mac_success(ubus_client: UbusClient):
    """Test _flush_conntrack_for_mac emits RC marker and returns True on RC=0."""
    from custom_components.openwrt.api.base import IpNeighbor

    neighbors = [
        IpNeighbor(
            ip="192.168.1.50",
            mac="AA:BB:CC:DD:EE:FF",
            interface="br-lan",
            state="REACHABLE",
        )
    ]
    with (
        patch.object(
            ubus_client,
            "get_ip_neighbors",
            new_callable=AsyncMock,
            return_value=neighbors,
        ),
        patch.object(
            ubus_client,
            "execute_command",
            new_callable=AsyncMock,
            return_value="deleted 1 flow\nRC=0",
        ) as mock_exec,
    ):
        result = await ubus_client._flush_conntrack_for_mac("aa:bb:cc:dd:ee:ff")

    assert result is True
    mock_exec.assert_called_once_with("conntrack -D -s 192.168.1.50; echo RC=$?")


@pytest.mark.asyncio
async def test_flush_conntrack_for_mac_failure_rc(ubus_client: UbusClient):
    """Test _flush_conntrack_for_mac returns False when RC marker is non-zero."""
    from custom_components.openwrt.api.base import IpNeighbor

    neighbors = [
        IpNeighbor(
            ip="192.168.1.50",
            mac="AA:BB:CC:DD:EE:FF",
            interface="br-lan",
            state="REACHABLE",
        )
    ]
    with (
        patch.object(
            ubus_client,
            "get_ip_neighbors",
            new_callable=AsyncMock,
            return_value=neighbors,
        ),
        patch.object(
            ubus_client,
            "execute_command",
            new_callable=AsyncMock,
            return_value="conntrack v1.4.6: 0 flow entries have been deleted.\nRC=1",
        ) as mock_exec,
    ):
        result = await ubus_client._flush_conntrack_for_mac("aa:bb:cc:dd:ee:ff")

    assert result is False
    mock_exec.assert_called_once_with("conntrack -D -s 192.168.1.50; echo RC=$?")


@pytest.mark.asyncio
async def test_flush_conntrack_for_mac_no_ips(ubus_client: UbusClient):
    """Test _flush_conntrack_for_mac returns True when device has no IP in neighbor table."""
    with patch.object(
        ubus_client, "get_ip_neighbors", new_callable=AsyncMock, return_value=[]
    ):
        result = await ubus_client._flush_conntrack_for_mac("aa:bb:cc:dd:ee:ff")

    assert result is True


@pytest.mark.asyncio
async def test_flush_conntrack_for_mac_neighbor_lookup_error(ubus_client: UbusClient):
    """Test _flush_conntrack_for_mac returns False when get_ip_neighbors raises."""
    with patch.object(
        ubus_client,
        "get_ip_neighbors",
        new_callable=AsyncMock,
        side_effect=RuntimeError("arp error"),
    ):
        result = await ubus_client._flush_conntrack_for_mac("aa:bb:cc:dd:ee:ff")

    assert result is False
