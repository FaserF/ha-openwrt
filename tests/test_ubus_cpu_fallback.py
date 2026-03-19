"""Test the Ubus CPU fallback logic."""

import asyncio
from unittest.mock import AsyncMock, patch
import pytest

from custom_components.openwrt.api.ubus import UbusClient
from custom_components.openwrt.api.base import SystemResources

@pytest.fixture
def ubus_client() -> UbusClient:
    """Fixture for Ubus client."""
    client = UbusClient(host="192.168.1.1", username="root", password="password")
    client._session_id = "test_token"
    return client

@pytest.mark.asyncio
async def test_ubus_cpu_priority_1_system_info(ubus_client: UbusClient):
    """Test Priority 1: CPU field in system info."""
    with patch.object(ubus_client, "_call", new_callable=AsyncMock) as mock_call, \
         patch.object(ubus_client, "execute_command", new_callable=AsyncMock) as mock_exec:
        
        # Mock system info with cpu field
        def side_effect(obj, method, params=None):
            if obj == "system" and method == "info":
                return {
                    "cpu": {
                        "user": 100, "nice": 0, "system": 50, "idle": 1000,
                        "iowait": 0, "irq": 0, "softirq": 0, "steal": 0
                    }
                }
            if obj == "file" and method == "read":
                return {"data": "restricted"}
            return {}

        mock_call.side_effect = side_effect
        mock_exec.return_value = "restricted"

        # First call to establish baseline (returns 0.0)
        resources1 = await ubus_client.get_system_resources()
        assert resources1.cpu_usage == 0.0
        
        # Second call with increased values
        def side_effect2(obj, method, params=None):
            if obj == "system" and method == "info":
                return {
                    "cpu": {
                        "user": 150, "nice": 0, "system": 100, "idle": 1100,
                        "iowait": 0, "irq": 0, "softirq": 0, "steal": 0
                    }
                }
            return {}
        mock_call.side_effect = side_effect2
        
        resources2 = await ubus_client.get_system_resources()
        # DiffTotal: 200, DiffIdle: 100 -> 50%
        assert resources2.cpu_usage == 50.0

@pytest.mark.asyncio
async def test_ubus_cpu_priority_2_file_read(ubus_client: UbusClient):
    """Test Priority 2: file.read /proc/stat."""
    with patch.object(ubus_client, "_call", new_callable=AsyncMock) as mock_call, \
         patch.object(ubus_client, "execute_command", new_callable=AsyncMock) as mock_exec:
        
        # Mock system info WITHOUT cpu field, but file.read WITH data
        def side_effect(obj, method, params=None):
            if obj == "system" and method == "info":
                return {"memory": {"total": 1000}}
            if obj == "file" and method == "read" and params.get("path") == "/proc/stat":
                return {"data": "cpu  100 0 50 1000 0 0 0 0 0 0\n"}
            return {}

        mock_call.side_effect = side_effect
        mock_exec.return_value = "" # Restricted

        resources1 = await ubus_client.get_system_resources()
        assert resources1.cpu_usage == 0.0
        
        # Second call
        def side_effect2(obj, method, params=None):
            if obj == "file" and method == "read":
                return {"data": "cpu  150 0 100 1100 0 0 0 0 0 0\n"}
            return {"memory": {"total": 1000}}
        mock_call.side_effect = side_effect2
        
        resources2 = await ubus_client.get_system_resources()
        assert resources2.cpu_usage == 50.0

@pytest.mark.asyncio
async def test_ubus_cpu_priority_3_file_exec(ubus_client: UbusClient):
    """Test Priority 3: file.exec cat /proc/stat."""
    with patch.object(ubus_client, "_call", new_callable=AsyncMock) as mock_call, \
         patch.object(ubus_client, "execute_command", new_callable=AsyncMock) as mock_exec:
        
        # Mock everything restricted except file.exec (via execute_command)
        mock_call.return_value = {} # No cpu in system info, no data in file.read
        
        mock_exec.return_value = "cpu  100 0 50 1000 0 0 0 0 0 0\n"

        resources1 = await ubus_client.get_system_resources()
        assert resources1.cpu_usage == 0.0
        
        # Second call
        mock_exec.return_value = "cpu  150 0 100 1100 0 0 0 0 0 0\n"
        
        resources2 = await ubus_client.get_system_resources()
        assert resources2.cpu_usage == 50.0
