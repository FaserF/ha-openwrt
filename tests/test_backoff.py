"""Tests for Coordinator exponential backoff on poll failure."""

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed

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
    coordinator._store = MagicMock()
    coordinator._store.async_load = AsyncMock(return_value={})
    coordinator._store.async_save = AsyncMock()

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


def _coordinator(hass, mock_client) -> OpenWrtDataCoordinator:
    """Build a coordinator with a 30 s update interval."""
    mock_client.connect = AsyncMock()
    mock_client.connected = True
    mock_client.reboot = AsyncMock(return_value=True)
    entry = MagicMock()
    entry.options = {"update_interval": 30}
    entry.data = {"host": "192.168.1.1"}
    entry.entry_id = "test_entry"
    coordinator = OpenWrtDataCoordinator(hass, entry, mock_client)
    coordinator._store = MagicMock()
    coordinator._store.async_load = AsyncMock(return_value={})
    coordinator._store.async_save = AsyncMock()
    coordinator.async_update_listeners = MagicMock()
    coordinator._schedule_refresh = MagicMock()
    return coordinator


@pytest.mark.asyncio
async def test_reboot_polls_quickly_until_router_is_back(hass) -> None:
    """A reboot shows the router unavailable and polls every 10 s until it booted."""
    now = 100000.0
    hass.loop.time = MagicMock(side_effect=lambda: now)
    mock_client = MagicMock()
    coordinator = _coordinator(hass, mock_client)

    await coordinator.async_reboot()

    assert coordinator.in_reboot_installation
    assert coordinator.last_update_success is False
    coordinator.async_update_listeners.assert_called_once()
    coordinator._schedule_refresh.assert_called_once()
    assert coordinator.update_interval == timedelta(seconds=10)

    # Still up with its old uptime: it has not rebooted yet
    old = MagicMock()
    old.system_resources.uptime = 86400
    mock_client.get_all_data = AsyncMock(return_value=old)
    now += 5
    with pytest.raises(UpdateFailed, match="not rebooted yet"):
        await coordinator._async_update_data()

    # Down: no backoff and no "connection lost" repair
    mock_client.get_all_data = AsyncMock(side_effect=TimeoutError("down"))
    now += 10
    with (
        patch(
            "custom_components.openwrt.coordinator.async_create_connection_lost_repair"
        ) as repair,
        pytest.raises(UpdateFailed),
    ):
        await coordinator._async_update_data()
    repair.assert_not_called()
    assert coordinator.in_reboot_installation
    assert coordinator.update_interval == timedelta(seconds=10)

    # Back with an uptime younger than the reboot
    new = MagicMock()
    new.system_resources.uptime = 20
    mock_client.get_all_data = AsyncMock(return_value=new)
    now += 30
    await coordinator._async_update_data()
    assert not coordinator.in_reboot_installation
    assert coordinator.update_interval == timedelta(seconds=30)


@pytest.mark.asyncio
async def test_reboot_state_ends_after_timeout(hass) -> None:
    """A router that never comes back falls back to backoff and the repair."""
    now = 100000.0
    hass.loop.time = MagicMock(side_effect=lambda: now)
    mock_client = MagicMock()
    coordinator = _coordinator(hass, mock_client)
    mock_client.get_all_data = AsyncMock(side_effect=TimeoutError("down"))

    await coordinator.async_reboot()
    now += 601
    with (
        patch(
            "custom_components.openwrt.coordinator.async_create_connection_lost_repair"
        ) as repair,
        pytest.raises(UpdateFailed),
    ):
        await coordinator._async_update_data()
    repair.assert_called_once()
    assert not coordinator.in_reboot_installation
    assert coordinator.update_interval == timedelta(seconds=20)


@pytest.mark.asyncio
async def test_reboot_abandons_hung_polls(hass) -> None:
    """A poll that hangs while the router goes down cannot block the next one."""
    hass.loop.time = MagicMock(return_value=100000.0)
    mock_client = MagicMock()
    coordinator = _coordinator(hass, mock_client)
    started = asyncio.Event()

    async def hang() -> None:
        started.set()
        await asyncio.Event().wait()

    mock_client.get_all_data = hang
    # A zero poll timeout expires at the first await: no real waiting
    with patch(
        "custom_components.openwrt.coordinator.REBOOT_POLL_TIMEOUT", timedelta(0)
    ):
        # A poll already in flight, without a timeout, when Reboot is pressed
        in_flight = asyncio.create_task(coordinator._async_update_data())
        await started.wait()
        await coordinator.async_reboot()
        # The guard only fails the test, instead of hanging, if not bounded
        async with asyncio.timeout(1):
            with pytest.raises(UpdateFailed):
                await in_flight

        # A poll started in the reboot state
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()

    assert coordinator.in_reboot_installation
    assert coordinator.update_interval == timedelta(seconds=10)
    assert coordinator._current_backoff_interval == 10


@pytest.mark.asyncio
async def test_firmware_install_reboot_ends_on_first_success(hass) -> None:
    """A firmware install sets the flag directly and keeps its upstream behaviour."""
    hass.loop.time = MagicMock(return_value=100000.0)
    mock_client = MagicMock()
    coordinator = _coordinator(hass, mock_client)
    coordinator.in_reboot_installation = True
    data = MagicMock()
    # Longer than the loop time: the reboot-button uptime check would reject it
    data.system_resources.uptime = 200000
    mock_client.get_all_data = AsyncMock(return_value=data)

    with patch("custom_components.openwrt.coordinator._LOGGER") as logger:
        await coordinator._async_update_data()

    assert not coordinator.in_reboot_installation
    assert coordinator._reboot_started == 0.0
    logger.warning.assert_not_called()


@pytest.mark.asyncio
async def test_failed_reboot_keeps_normal_polling(hass) -> None:
    """A reboot the router did not accept does not enter the rebooting state."""
    hass.loop.time = MagicMock(return_value=100000.0)
    mock_client = MagicMock()
    coordinator = _coordinator(hass, mock_client)
    mock_client.reboot = AsyncMock(return_value=False)

    await coordinator.async_reboot()

    assert not coordinator.in_reboot_installation
    coordinator.async_update_listeners.assert_not_called()
    coordinator._schedule_refresh.assert_not_called()
