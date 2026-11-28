"""Tests for extended log diagnostics and get_system_logs service."""

import shlex
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.openwrt import _register_services
from custom_components.openwrt.api.base import OpenWrtData
from custom_components.openwrt.api.ssh import SshClient
from custom_components.openwrt.api.ubus import UbusClient
from custom_components.openwrt.const import DATA_CLIENT, DATA_COORDINATOR, DOMAIN
from custom_components.openwrt.diagnostics import async_get_config_entry_diagnostics


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

    with patch.object(hass.services, "async_register") as mock_register:
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
        mock_client.get_system_logs.assert_called_once_with(count=10, log_filter=None)

        # Test kernel log type
        call_data.data = {
            "entry_id": "test_entry_id",
            "count": 20,
            "log_type": "kernel",
        }
        res = await log_handler(call_data)
        assert res == {"logs": ["dmesg line 1"]}
        mock_client.get_dmesg_logs.assert_called_once_with(count=20, log_filter=None)

        # Test filter is passed through
        mock_client.get_system_logs.reset_mock()
        call_data.data = {"entry_id": "test_entry_id", "filter": "hostapd"}
        await log_handler(call_data)
        mock_client.get_system_logs.assert_called_once_with(
            count=50, log_filter="hostapd"
        )


@pytest.mark.asyncio
async def test_diagnostics_redacts_logs(hass) -> None:
    """Test that diagnostics page includes and redacts logs properly."""
    data = OpenWrtData()
    data.system_logs = [
        "system log containing IP 192.168.1.50 and MAC aa:bb:cc:dd:ee:ff"
    ]
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
        patch(
            "custom_components.openwrt.diagnostics.async_redact_data",
            side_effect=lambda d, k: d,
        ),
        patch(
            "custom_components.openwrt.diagnostics._to_json_safe",
            side_effect=lambda x: x,
        ),
        patch("homeassistant.helpers.device_registry.async_get"),
        patch("homeassistant.helpers.entity_registry.async_get"),
        patch(
            "homeassistant.helpers.entity_registry.async_entries_for_config_entry",
            return_value=[],
        ),
    ):
        diag = await async_get_config_entry_diagnostics(hass, mock_entry)

        assert "system_logs" in diag
        assert "dmesg_logs" in diag
        assert diag["system_logs"] == [
            "system log containing IP [REDACTED_IP] and MAC [REDACTED_MAC]"
        ]
        assert diag["dmesg_logs"] == [
            "kernel log containing IP [REDACTED_IP] and MAC [REDACTED_MAC]"
        ]

        # A live fetch with more lines is preferred over the cached ones
        mock_client = MagicMock()
        mock_client.get_system_logs = AsyncMock(return_value=["fresh 192.0.2.2"])
        hass.data[DOMAIN]["test_entry_id"][DATA_CLIENT] = mock_client
        diag = await async_get_config_entry_diagnostics(hass, mock_entry)
        mock_client.get_system_logs.assert_awaited_once_with(count=100)
        assert diag["system_logs"] == ["fresh [REDACTED_IP]"]

        # An empty live fetch falls back to the cached lines
        mock_client.get_system_logs = AsyncMock(return_value=[])
        diag = await async_get_config_entry_diagnostics(hass, mock_entry)
        assert diag["system_logs"] == [
            "system log containing IP [REDACTED_IP] and MAC [REDACTED_MAC]"
        ]


@pytest.mark.asyncio
async def test_log_filter_is_shell_quoted() -> None:
    """Test the log filter reaches logread/grep as one argument, never as shell."""
    pattern = "'; reboot #"
    client = UbusClient(
        MagicMock(),
        MagicMock(),
        host="192.0.2.1",
        username="root",
        password="password",
    )

    with patch.object(
        client, "execute_command", new_callable=AsyncMock, return_value="line"
    ) as mock_exec:
        assert await client.get_system_logs(count=20, log_filter=pattern) == ["line"]
        cmd = mock_exec.call_args[0][0]
        assert shlex.split(cmd) == [
            "/sbin/logread",
            "-e",
            pattern,
            "|",
            "tail",
            "-n",
            "20",
        ]

        await client.get_dmesg_logs(count=5, log_filter=pattern)
        cmd = mock_exec.call_args[0][0]
        assert shlex.split(cmd) == [
            "dmesg",
            "|",
            "grep",
            "-e",
            pattern,
            "|",
            "tail",
            "-n",
            "5",
        ]


@pytest.mark.asyncio
async def test_ssh_log_filter_uses_logread() -> None:
    """Test SSH skips the unfiltered ubus log read when a filter is given."""
    client = SshClient(
        MagicMock(),
        MagicMock(),
        host="192.0.2.1",
        username="root",
        password="password",
    )

    with patch.object(
        client, "_exec", new_callable=AsyncMock, return_value="hostapd: up"
    ) as mock_exec:
        logs = await client.get_system_logs(count=5, log_filter="hostapd")

    assert logs == ["hostapd: up"]
    mock_exec.assert_called_once_with("/sbin/logread -e hostapd | tail -n 5")
