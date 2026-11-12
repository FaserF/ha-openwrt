"""Tests for firmware update backup failure and URL resolution."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.openwrt.api import firmware_install
from custom_components.openwrt.api.firmware_install import (
    FIRMWARE_FLASHING,
    FIRMWARE_JOB_BUSY,
    FIRMWARE_JOB_CANCELLED,
    FIRMWARE_JOB_EXIT,
    FIRMWARE_JOB_MISSING,
    FIRMWARE_JOB_RUNNING,
    FIRMWARE_JOB_STARTED,
    FIRMWARE_JOB_TOO_LATE,
)
from custom_components.openwrt.coordinator import OpenWrtData, OpenWrtDataCoordinator
from custom_components.openwrt.update import OpenWrtUpdateEntity


class _FakeJobRouter:
    """Answer the detached install's commands the way the router's shell would.

    polls are returned one per poll (an Exception is raised instead), the last
    one repeating; after a cancel, the polls in after_cancel take over. A poll answer that does not end the job gets the running
    marker appended, as the poll command prints it for a live job.
    """

    def __init__(
        self,
        polls: list,
        launch: object = FIRMWARE_JOB_STARTED,
        cancel: object = FIRMWARE_JOB_CANCELLED,
        after_cancel: list | None = None,
    ) -> None:
        self.polls = list(polls)
        self.after_cancel = after_cancel
        self.launch = launch
        self.cancel = cancel
        self.launches: list[str] = []
        self.cancels = 0
        self.poll_count = 0

    async def __call__(self, command: str) -> str:
        if "sysupgrade" in command:
            self.launches.append(command)
            answer = self.launch
        elif FIRMWARE_JOB_TOO_LATE in command:
            self.cancels += 1
            answer = self.cancel
        else:
            self.poll_count += 1
            if self.cancels and self.after_cancel is not None:
                self.polls, self.after_cancel = self.after_cancel, None
            answer = self.polls.pop(0) if len(self.polls) > 1 else self.polls[0]
            if isinstance(answer, str) and FIRMWARE_JOB_EXIT not in answer:
                answer = f"{answer}\n{FIRMWARE_JOB_RUNNING}"
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.mark.asyncio
async def test_firmware_update_backup_failure_aborts(hass: HomeAssistant) -> None:
    """Test that a backup failure during firmware update raises HomeAssistantError and aborts."""
    entry = MagicMock()
    entry.entry_id = "test_entry_id"
    entry.options = {"auto_backup": True, "update_interval": 30}
    entry.data = {"host": "192.168.1.1"}

    mock_client = AsyncMock()
    # Mock create_backup returning None (failure)
    mock_client.create_backup.return_value = None

    coordinator = MagicMock(spec=OpenWrtDataCoordinator)
    coordinator.hass = hass
    coordinator.config_entry = entry
    coordinator.client = mock_client
    coordinator.router_id = "11:22:33:44:55:66"
    coordinator.data = OpenWrtData()
    coordinator.data.firmware_install_url = "http://example.com/firmware.bin"
    coordinator.data.asu_status = "unsupported"
    coordinator.async_get_non_default_packages = AsyncMock(return_value=[])

    update_entity = OpenWrtUpdateEntity(coordinator, entry)
    update_entity.hass = hass

    with pytest.raises(HomeAssistantError, match="Failed to create backup on router"):
        await update_entity.async_install(
            version="25.12.5", backup=True, keep_settings=True
        )


@pytest.mark.asyncio
async def test_async_set_stable_release_urls(hass: HomeAssistant) -> None:
    """Test that _async_set_stable_release_urls dynamically fetches profiles.json."""
    entry = MagicMock()
    entry.entry_id = "test_entry_id"
    entry.options = {"update_interval": 30}
    entry.data = {"host": "192.168.1.1"}

    mock_client = AsyncMock()
    coordinator = OpenWrtDataCoordinator(hass, entry, mock_client)

    data = OpenWrtData()
    data.device_info.target = "mediatek/filogic"
    data.device_info.board_name = "xiaomi_mi-router-ax3000t-ubootmod"
    data.device_info.release_distribution = "openwrt"

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(
        return_value={
            "profiles": {
                "xiaomi-mi-router-ax3000t-ubootmod": {
                    "images": [
                        {
                            "name": "openwrt-25.12.5-mediatek-filogic-xiaomi_mi-router-ax3000t-ubootmod-squashfs-sysupgrade.itb",
                            "type": "sysupgrade",
                        }
                    ]
                }
            }
        }
    )

    mock_session = MagicMock(spec=aiohttp.ClientSession)
    mock_session.get.return_value.__aenter__.return_value = mock_resp

    await coordinator._async_set_stable_release_urls(data, "25.12.5", mock_session)

    # Assert dynamic URL resolved from profiles.json
    assert (
        data.firmware_install_url
        == "https://downloads.openwrt.org/releases/25.12.5/targets/mediatek/filogic/openwrt-25.12.5-mediatek-filogic-xiaomi_mi-router-ax3000t-ubootmod-squashfs-sysupgrade.itb"
    )


@pytest.mark.asyncio
async def test_check_snapshot_update_release_branch(hass: HomeAssistant) -> None:
    """Test that _check_snapshot_update correctly targets release branch snapshots."""
    entry = MagicMock()
    entry.entry_id = "test_entry_id"
    entry.options = {"update_interval": 30}
    entry.data = {"host": "192.168.1.1"}

    mock_client = AsyncMock()
    coordinator = OpenWrtDataCoordinator(hass, entry, mock_client)

    data = OpenWrtData()
    data.device_info.target = "x86/64"
    data.device_info.board_name = "qemu-standard-pc-q35-ich9-2009"
    data.device_info.release_version = "25.12-SNAPSHOT"
    data.firmware_current_version = "25.12-SNAPSHOT (r33058-949e61ec65)"

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(
        return_value={
            "version_code": "r35287-b519bc3b76",
            "profiles": {
                "qemu_standard_pc_q35_ich9_2009": {
                    "images": [
                        {
                            "name": "openwrt-25.12-SNAPSHOT-x86-64-generic-ext4-combined.img.gz",
                            "type": "combined",
                        },
                        {
                            "name": "openwrt-25.12-SNAPSHOT-x86-64-generic-squashfs-sysupgrade.img.gz",
                            "type": "sysupgrade",
                        },
                    ]
                }
            },
        }
    )

    mock_session = MagicMock(spec=aiohttp.ClientSession)
    mock_session.get.return_value.__aenter__.return_value = mock_resp

    await coordinator._check_snapshot_update(data, mock_session)

    assert data.firmware_latest_version == "25.12-SNAPSHOT (r35287-b519bc3b76)"
    assert data.firmware_upgradable is True
    assert (
        data.firmware_release_url
        == "https://downloads.openwrt.org/releases/25.12-SNAPSHOT/targets/x86/64/"
    )
    assert (
        data.firmware_install_url
        == "https://downloads.openwrt.org/releases/25.12-SNAPSHOT/targets/x86/64/openwrt-25.12-SNAPSHOT-x86-64-generic-squashfs-sysupgrade.img.gz"
    )


def test_version_is_newer_ignores_revision_suffix() -> None:
    """Test that version_is_newer correctly handles and ignores revision suffixes."""
    from custom_components.openwrt.coordinator.firmware_helpers import version_is_newer

    # 1. current has revision suffix, latest does not, but base version matches -> False
    assert version_is_newer("25.12.6 (r33311-981c978eff)", "25.12.6") is False

    # 2. current has revision suffix, latest is newer -> True
    assert version_is_newer("25.12.5 (r33311-981c978eff)", "25.12.6") is True

    # 3. current has revision suffix, latest is older -> False
    assert version_is_newer("25.12.6 (r33311-981c978eff)", "25.12.5") is False

    # 4. generic comparison without suffix where latest is older -> False
    assert version_is_newer("25.12.6", "25.12.5") is False


@pytest.mark.asyncio
async def test_sysupgrade_command_has_no_trailing_rm() -> None:
    """Test that install_firmware does not append a trailing 'rm -f /tmp/firmware.bin'."""
    from custom_components.openwrt.api.luci_rpc import LuciRpcClient
    from custom_components.openwrt.api.ssh import SshClient
    from custom_components.openwrt.api.ubus import UbusClient

    ubus_client = UbusClient(
        MagicMock(), MagicMock(), host="192.168.1.1", username="root", password="pwd"
    )
    luci_client = LuciRpcClient(
        MagicMock(), MagicMock(), host="192.168.1.1", username="root", password="pwd"
    )
    ssh_client = SshClient(
        MagicMock(), MagicMock(), host="192.168.1.1", username="root", password="pwd"
    )

    for client in (ubus_client, luci_client, ssh_client):
        router = _FakeJobRouter([f"{FIRMWARE_FLASHING}\n{FIRMWARE_JOB_EXIT}0"])
        with (
            patch.object(client, "_firmware_exec", router),
            patch.object(firmware_install, "FIRMWARE_POLL_INTERVAL", 0),
        ):
            await client.install_firmware(
                "http://example.com/firmware.bin", keep_settings=True, force=False
            )
        (cmd,) = router.launches
        assert "sysupgrade" in cmd
        assert "rm -f /tmp/firmware.bin )" not in cmd


@pytest.mark.asyncio
async def test_firmware_update_disabled(hass: HomeAssistant) -> None:
    """Test that check_firmware_update is not called and entity is not created when disabled."""
    from custom_components.openwrt.const import CONF_CHECK_FIRMWARE_UPDATES, DOMAIN
    from custom_components.openwrt.update import async_setup_entry

    entry = MagicMock()
    entry.entry_id = "test_entry_id_fw_disabled"
    entry.options = {CONF_CHECK_FIRMWARE_UPDATES: False, "update_interval": 30}
    entry.data = {"host": "192.168.1.1"}

    # We will test update.py entity creation
    coordinator = MagicMock(spec=OpenWrtDataCoordinator)
    coordinator.data = OpenWrtData()
    coordinator.data.permissions = MagicMock()
    coordinator.async_add_listener = MagicMock()
    coordinator.router_id = "test_router"
    coordinator.data.permissions.read_system = True

    hass.data[DOMAIN] = {entry.entry_id: {"coordinator": coordinator}}

    async_add_entities = MagicMock()

    await async_setup_entry(hass, entry, async_add_entities)

    added_entities = (
        async_add_entities.call_args[0][0] if async_add_entities.call_args else []
    )
    assert not any(isinstance(e, OpenWrtUpdateEntity) for e in added_entities)

    # Now verify it's added if True
    entry.options = {CONF_CHECK_FIRMWARE_UPDATES: True, "update_interval": 30}

    await async_setup_entry(hass, entry, async_add_entities)

    added_entities = (
        async_add_entities.call_args[0][0] if async_add_entities.call_args else []
    )
    assert any(isinstance(e, OpenWrtUpdateEntity) for e in added_entities)


# --- Firmware state after an upgrade ---


def _upgrade_coordinator(hass_time: float = 1000.0):
    from custom_components.openwrt.api.base import DeviceInfo

    hass = MagicMock()
    hass.loop.time.return_value = hass_time
    entry = MagicMock()
    entry.entry_id = "test_entry_id"
    entry.options = {"update_interval": 30}
    entry.data = {"host": "192.0.2.1"}
    coordinator = OpenWrtDataCoordinator(hass, entry, AsyncMock())

    data = OpenWrtData()
    data.device_info = DeviceInfo(
        release_version="25.12.5",
        release_revision="r33051-f5dae5ece4",
        firmware_version="25.12.5 (r33051-f5dae5ece4)",
        target="qualcommax/ipq807x",
        board_name="vendor,board",
    )
    data.system_resources.uptime = 50000
    coordinator.data = data
    return coordinator, entry, data


@pytest.mark.asyncio
async def test_revision_change_after_upgrade_clears_stale_offer() -> None:
    """After a flash, installed_version follows and the old offer disappears."""
    from custom_components.openwrt.api.base import DeviceInfo
    from custom_components.openwrt.coordinator import FIRMWARE_CHECK_INTERVAL

    coordinator, entry, data = _upgrade_coordinator()
    entity = OpenWrtUpdateEntity(coordinator, entry)

    # Before the upgrade: the last check found 25.12.6
    coordinator._async_sync_firmware_state(data)
    data.firmware_latest_version = "25.12.6"
    data.firmware_upgradable = True
    data.firmware_install_url = "https://downloads.example.com/fw-sysupgrade.bin"
    coordinator._last_uptime = data.system_resources.uptime
    assert entity.installed_version == "25.12.5 (r33051-f5dae5ece4)"
    assert entity.latest_version == "25.12.6"

    # The router comes back on the new firmware (uptime went backwards).
    # get_all_data reuses the same data object with its cached device info.
    data.system_resources.uptime = 60
    new_info = DeviceInfo(
        release_version="25.12.6",
        release_revision="r33311-981c978eff",
        firmware_version="25.12.6 (r33311-981c978eff)",
        target="qualcommax/ipq807x",
        board_name="vendor,board",
    )
    coordinator.client.async_refresh_device_info = AsyncMock(return_value=new_info)

    await coordinator._async_refresh_device_info_after_reboot(data)
    coordinator._async_sync_firmware_state(data)

    coordinator.client.async_refresh_device_info.assert_awaited_once()
    assert entity.installed_version == "25.12.6 (r33311-981c978eff)"
    # No stale offer: latest equals installed until the re-check runs
    assert entity.latest_version == entity.installed_version
    assert data.firmware_upgradable is False
    assert data.firmware_install_url == ""
    assert data.firmware_latest_version == ""

    # The re-check is due about two minutes later, not after six hours
    interval = FIRMWARE_CHECK_INTERVAL.total_seconds()
    assert 1000.0 + 60 - coordinator._last_firmware_check <= interval
    assert 1000.0 + 121 - coordinator._last_firmware_check > interval


@pytest.mark.asyncio
async def test_device_info_not_refreshed_without_reboot() -> None:
    """Ordinary polls keep the cached device info (no extra router calls)."""
    coordinator, _entry, data = _upgrade_coordinator()
    coordinator._last_uptime = 50000
    data.system_resources.uptime = 50030
    coordinator.client.async_refresh_device_info = AsyncMock()

    await coordinator._async_refresh_device_info_after_reboot(data)
    coordinator.client.async_refresh_device_info.assert_not_called()

    # Same revision on the next sync: the found update is kept
    coordinator._async_sync_firmware_state(data)
    data.firmware_latest_version = "25.12.6"
    data.firmware_upgradable = True
    coordinator._async_sync_firmware_state(data)
    assert data.firmware_upgradable is True
    assert data.firmware_latest_version == "25.12.6"


@pytest.mark.asyncio
async def test_device_info_refreshed_after_install_reconnect() -> None:
    """The first successful poll after an install refreshes device info once."""
    coordinator, _entry, data = _upgrade_coordinator()
    coordinator._device_info_refresh_pending = True
    coordinator.client.async_refresh_device_info = AsyncMock(
        return_value=data.device_info
    )

    await coordinator._async_refresh_device_info_after_reboot(data)
    await coordinator._async_refresh_device_info_after_reboot(data)
    coordinator.client.async_refresh_device_info.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_device_info_refresh_is_retried() -> None:
    """A refresh that fails while the router comes back is retried next poll."""
    coordinator, _entry, data = _upgrade_coordinator()
    coordinator._device_info_refresh_pending = True
    coordinator.client.async_refresh_device_info = AsyncMock(
        side_effect=[TimeoutError("ubus not ready"), data.device_info]
    )

    await coordinator._async_refresh_device_info_after_reboot(data)
    assert coordinator._device_info_refresh_pending is True
    await coordinator._async_refresh_device_info_after_reboot(data)
    assert coordinator._device_info_refresh_pending is False
    await coordinator._async_refresh_device_info_after_reboot(data)
    assert coordinator.client.async_refresh_device_info.await_count == 2


@pytest.mark.asyncio
async def test_client_refresh_device_info_updates_cache_and_forces_slow_poll() -> None:
    """The client re-reads device info and makes the next poll a slow poll."""
    from custom_components.openwrt.api.base import DeviceInfo
    from custom_components.openwrt.api.ssh import SshClient

    client = SshClient(
        MagicMock(), MagicMock(), host="192.0.2.1", username="root", password="pwd"
    )
    client._last_slow_poll_time = 12345.0
    info = DeviceInfo(release_revision="r2")
    with patch.object(client, "get_device_info", AsyncMock(return_value=info)):
        assert await client.async_refresh_device_info() is info
    assert client._cached_device_info is info
    assert client._last_slow_poll_time == 0.0
