"""Tests for Coordinator exponential backoff on poll failure."""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.openwrt.coordinator import OpenWrtDataCoordinator


@pytest.mark.asyncio
async def test_coordinator_backoff_on_failure(hass) -> None:
    """Test that coordinator doubles update interval on error and resets on success."""
    hass.loop.time = MagicMock(return_value=100000.0)
    mock_client = MagicMock()
    mock_client.connect = AsyncMock()
    mock_client.connected = True

    # 1. Raise Exception on first fetch
    mock_client.get_all_data = AsyncMock(side_effect=RuntimeError("Connection lost"))

    entry = MagicMock()
    entry.options = {"update_interval": 30}
    entry.data = {"host": "192.168.1.1"}
    entry.entry_id = "test_entry"

    coordinator = OpenWrtDataCoordinator(hass, entry, mock_client)

    # Trigger first failed update
    with pytest.raises(Exception, match="Connection lost"):
        await coordinator._async_update_data()

    # Verify update interval doubled to 60 seconds
    assert coordinator.update_interval == timedelta(seconds=60)
    assert coordinator._current_backoff_interval == 60

    # Trigger second failed update
    with pytest.raises(Exception, match="Connection lost"):
        await coordinator._async_update_data()

    # Verify update interval doubled again to 120 seconds
    assert coordinator.update_interval == timedelta(seconds=120)

    # 2. Succeed on third fetch
    mock_data = MagicMock()
    mock_data.system_resources.uptime = 100
    mock_client.get_all_data = AsyncMock(return_value=mock_data)
    await coordinator._async_update_data()

    # Verify update interval reset to default (30 seconds)
    assert coordinator.update_interval == timedelta(seconds=30)
    assert coordinator._current_backoff_interval == 30
