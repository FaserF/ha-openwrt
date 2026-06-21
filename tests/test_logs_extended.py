"""Tests for extended log diagnostics and get_system_logs service."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from custom_components.openwrt import _register_services
from custom_components.openwrt.const import DOMAIN, DATA_CLIENT, DATA_COORDINATOR
from custom_components.openwrt.diagnostics import async_get_config_entry_diagnostics
from custom_components.openwrt.api.base import OpenWrtData


@pytest.mark.asyncio
async def test_get_system_logs_service(hass) -> None:
    """Test get_system_logs service handler with system and kernel types."""
    mock_client = MagicMock()
    mock_client.get_system_logs = AsyncMock(return_value=["syslog line 1"])
    mock_client.get_dmesg_logs = AsyncMock(return_value=["dmesg line 1"])

    hass.data = {
        DOMAIN: {
            "test_entry_id": {
                DATA_CLIENT: mock_client,
            }
        }
    }

    with patch("homeassistant.core.ServiceRegistry.async_register") as mock_register:
        _register_services(hass)
        
        log_handler = None
        for call in mock_register.call_args_list:
            if call[0][1] == "get_system_logs":
                log_handler = call[0][2]
                break

        assert log_handler is not None

        # Test system log type
        call_data = MagicMock()
        call_data.data = {
            "entry_id": "test_entry_id",
            "count": 10,
            "log_type": "system",
        }
        res = await log_handler(call_data)
        assert res == {"logs": ["syslog line 1"]}
        mock_client.get_system_logs.assert_called_once_with(count=10)

        # Test kernel log type
        call_data.data = {
            "entry_id": "test_entry_id",
            "count": 20,
            "log_type": "kernel",
        }
        res = await log_handler(call_data)
        assert res == {"logs": ["dmesg line 1"]}
        mock_client.get_dmesg_logs.assert_called_once_with(count=20)


@pytest.mark.asyncio
async def test_diagnostics_redacts_logs(hass) -> None:
    """Test that diagnostics page includes and redacts logs properly."""
    data = OpenWrtData()
    data.system_logs = ["system log containing IP 192.168.1.50 and MAC aa:bb:cc:dd:ee:ff"]
    data.dmesg_logs = ["kernel log containing IP 10.0.0.1 and MAC 11-22-33-44-55-66"]

    mock_coordinator = MagicMock()
    mock_coordinator.data = data
    mock_coordinator.last_update_success = True

    hass.data = {
        DOMAIN: {
            "test_entry_id": {
                DATA_COORDINATOR: mock_coordinator,
            }
        }
    }

    mock_entry = MagicMock()
    mock_entry.data = {}
    mock_entry.options = {}
    mock_entry.entry_id = "test_entry_id"

    with (
        patch("custom_components.openwrt.diagnostics.async_redact_data", side_effect=lambda d, k: d),
        patch("homeassistant.helpers.device_registry.async_get"),
        patch("homeassistant.helpers.entity_registry.async_get"),
        patch("homeassistant.helpers.entity_registry.async_entries_for_config_entry", return_value=[]),
    ):
        diag = await async_get_config_entry_diagnostics(hass, mock_entry)

        assert "system_logs" in diag
        assert "dmesg_logs" in diag
        assert diag["system_logs"] == ["system log containing IP [REDACTED_IP] and MAC [REDACTED_MAC]"]
        assert diag["dmesg_logs"] == ["kernel log containing IP [REDACTED_IP] and MAC [REDACTED_MAC]"]
