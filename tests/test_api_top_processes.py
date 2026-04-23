"""Test the OpenWrt System Resources API for top processes."""

from unittest.mock import AsyncMock, patch

import pytest

from custom_components.openwrt.api.ubus import UbusClient


@pytest.fixture
def ubus_client() -> UbusClient:
    """Fixture for Ubus client."""
    return UbusClient(host="192.168.1.1", username="root", password="password")

@pytest.mark.asyncio
async def test_ubus_get_top_processes(ubus_client: UbusClient):
    """Test fetching top processes via Ubus."""
    with patch.object(ubus_client, "_call", new_callable=AsyncMock) as mock_call, \
         patch.object(ubus_client, "execute_command", new_callable=AsyncMock) as mock_exec:

        # 1. Mock system.info
        mock_call.return_value = {
            "uptime": 1000,
            "load": [0, 0, 0],
            "memory": {"total": 1024, "free": 512}
        }

        # 2. Mock top -n 1 -b
        mock_exec.return_value = (
            "Mem: 123K used, 456K free, 0K shrd, 0K buff, 0K cached\n"
            "CPU:  0% usr  0% sys  0% nic 100% idle  0% io  0% irq  0% sirq\n"
            "Load average: 0.00 0.00 0.00 1/100 1234\n"
            "  PID  PPID USER     STAT   VSZ %VSZ %CPU COMMAND\n"
            " 1234     1 root     S     1234   1%   5.5 /usr/sbin/rpcd\n"
            " 5678     1 root     S      567   0%   2.0 /usr/sbin/uhttpd\n"
        )

        resources = await ubus_client.get_system_resources()

        assert len(resources.top_processes) == 2
        p1 = resources.top_processes[0]
        assert p1.pid == 1234
        assert p1.user == "root"
        assert p1.cpu_usage == 5.5
        assert p1.command == "/usr/sbin/rpcd"

        p2 = resources.top_processes[1]
        assert p2.pid == 5678
        assert p2.cpu_usage == 2.0
        assert p2.command == "/usr/sbin/uhttpd"
