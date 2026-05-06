"""Tests for Batman-adv mesh support."""

from unittest.mock import patch

import pytest

from custom_components.openwrt.api.luci_rpc import LuciRpcClient
from custom_components.openwrt.api.ssh import SshClient
from custom_components.openwrt.api.ubus import UbusClient

MOCK_BATMAN_O = """
 * 00:11:22:33:44:56    0.010s   (123) 00:11:22:33:44:56 [br-lan]
   00:11:22:33:44:57    0.050s   (45) 00:11:22:33:44:56 [br-lan]
"""

MOCK_BATMAN_N = """
[br-lan] 00:11:22:33:44:56    0.010s
"""

MOCK_BATMAN_GWL = """
* 00:11:22:33:44:56 (200) 00:11:22:33:44:56 [br-lan] 100/100
"""

MOCK_BATMAN_TG = """
 * 00:11:22:33:44:58   -1 [....]    0.000s 00:11:22:33:44:56 (0x1234)
   00:11:22:33:44:59   -1 [.P..]    0.010s 00:11:22:33:44:57 (0x8765)
"""


@pytest.mark.asyncio
async def test_ubus_get_batman_data():
    """Test get_batman_data via Ubus."""
    client = UbusClient("192.168.1.1", "user", "pass")
    client.packages.batctl = True

    async def mock_execute(command):
        if "batctl o" in command:
            return MOCK_BATMAN_O
        if "batctl n" in command:
            return MOCK_BATMAN_N
        if "batctl gwl" in command:
            return MOCK_BATMAN_GWL
        if "batctl tg" in command:
            return MOCK_BATMAN_TG
        return ""

    with patch.object(client, "execute_command", side_effect=mock_execute):
        data = await client.get_batman_data()

        assert len(data["originators"]) == 2
        assert data["originators"][0].mac == "00:11:22:33:44:56"
        assert data["originators"][0].tq == 123
        assert data["originators"][1].mac == "00:11:22:33:44:57"
        assert data["originators"][1].tq == 45

        assert len(data["neighbors"]) == 1
        assert data["neighbors"][0].mac == "00:11:22:33:44:56"

        assert len(data["gateways"]) == 1
        assert data["gateways"][0].mac == "00:11:22:33:44:56"
        assert data["gateways"][0].is_selected is True

        assert len(data["translation_table"]) == 2
        assert data["translation_table"]["00:11:22:33:44:58"] == "00:11:22:33:44:56"
        assert data["translation_table"]["00:11:22:33:44:59"] == "00:11:22:33:44:57"


@pytest.mark.asyncio
async def test_ssh_get_batman_data():
    """Test get_batman_data via SSH."""
    client = SshClient("192.168.1.1", "user", "pass")
    client.packages.batctl = True

    async def mock_execute(command):
        if "batctl o" in command:
            return MOCK_BATMAN_O
        if "batctl n" in command:
            return MOCK_BATMAN_N
        if "batctl gwl" in command:
            return MOCK_BATMAN_GWL
        if "batctl tg" in command:
            return MOCK_BATMAN_TG
        return ""

    with patch.object(client, "execute_command", side_effect=mock_execute):
        data = await client.get_batman_data()

        assert len(data["originators"]) == 2
        assert data["originators"][0].mac == "00:11:22:33:44:56"
        assert data["originators"][0].tq == 123
        assert data["originators"][1].tq == 45
        assert data["translation_table"]["00:11:22:33:44:58"] == "00:11:22:33:44:56"


@pytest.mark.asyncio
async def test_luci_get_batman_data():
    """Test get_batman_data via LuCI-RPC."""
    client = LuciRpcClient("192.168.1.1", "user", "pass")
    client.packages.batctl = True

    async def mock_execute(command):
        if "batctl o" in command:
            return MOCK_BATMAN_O
        if "batctl n" in command:
            return MOCK_BATMAN_N
        if "batctl gwl" in command:
            return MOCK_BATMAN_GWL
        if "batctl tg" in command:
            return MOCK_BATMAN_TG
        return ""

    with patch.object(client, "execute_command", side_effect=mock_execute):
        data = await client.get_batman_data()

        assert len(data["originators"]) == 2
        assert data["originators"][0].mac == "00:11:22:33:44:56"
        assert data["originators"][0].tq == 123
        assert data["originators"][1].tq == 45
        assert data["translation_table"]["00:11:22:33:44:58"] == "00:11:22:33:44:56"
