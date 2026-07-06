"""Tests for banIP status + packet-block counter parsing."""

from unittest.mock import MagicMock, patch

import pytest

from custom_components.openwrt.api.ubus import UbusClient

_REPORT = (
    '[{"sets":{},"timestamp":"t",'
    '"sum_synflood":"0","sum_udpflood":"0","sum_icmpflood":"2","sum_ctinvalid":"47",'
    '"sum_tcpinvalid":"0","sum_bcp38":"0","sum_setinbound":"5","sum_setoutbound":"3",'
    '"sum_cntelements":"19111","autoadd_block":"1"}]'
)


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
async def test_get_banip_status_parses_report(ubus_client: UbusClient):
    async def fake_exec(command, params=None):
        if params == ["enabled"]:
            return {"code": 0}
        if params == ["report", "json"]:
            return {"code": 0, "stdout": _REPORT}
        return {}

    with patch.object(ubus_client, "file_exec", side_effect=fake_exec):
        st = await ubus_client.get_banip_status()

    assert st.enabled is True
    assert st.status == "enabled"
    assert st.banned_ips == 19111
    assert st.blocked_inbound == 5
    assert st.blocked_outbound == 3
    assert st.block_stats["ct_invalid"] == 47
    assert st.block_stats["icmp_flood"] == 2
    assert st.block_stats["autoadd_block"] == 1
    # total blocked packets excludes the autoadd bookkeeping counter
    assert st.blocked_packets == 5 + 3 + 0 + 0 + 2 + 47 + 0 + 0


@pytest.mark.asyncio
async def test_get_banip_status_disabled_empty_report(ubus_client: UbusClient):
    async def fake_exec(command, params=None):
        if params == ["enabled"]:
            return {"code": 1}
        return {"code": 0, "stdout": "[]"}

    with patch.object(ubus_client, "file_exec", side_effect=fake_exec):
        st = await ubus_client.get_banip_status()

    assert st.enabled is False
    assert st.status == "disabled"
    assert st.banned_ips == 0
    assert st.blocked_packets == 0
