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


# --- Verified firmware download ---

SHA = "ab" * 32


def test_sysupgrade_command_verifies_checksum_and_tls() -> None:
    """The command checks TLS and the checksum, and never forces sysupgrade."""
    from custom_components.openwrt.api.firmware_install import (
        FIRMWARE_CHECKSUM_MISMATCH,
        build_sysupgrade_command,
    )

    cmd = build_sysupgrade_command(
        "https://example.com/fw.bin", keep_settings=True, sha256=SHA.upper()
    )
    assert "--no-check-certificate" not in cmd
    assert "curl -k" not in cmd
    assert "sysupgrade -F" not in cmd
    assert f'= "{SHA}" ]' in cmd
    assert cmd.index("sha256sum") < cmd.index("sysupgrade /tmp/firmware.bin")
    assert FIRMWARE_CHECKSUM_MISMATCH in cmd
    assert cmd.endswith("sysupgrade /tmp/firmware.bin")

    no_sum = build_sysupgrade_command("https://example.com/fw.bin")
    assert "sha256sum" not in no_sum
    assert build_sysupgrade_command("u", keep_settings=False).endswith(
        "sysupgrade -n /tmp/firmware.bin"
    )


def test_sysupgrade_command_rejects_malformed_checksum() -> None:
    """A checksum that is not 64 hex digits never reaches the shell."""
    from custom_components.openwrt.api.firmware_install import (
        FirmwareInstallError,
        build_sysupgrade_command,
    )

    with pytest.raises(FirmwareInstallError):
        build_sysupgrade_command("u", sha256='"; reboot; echo "')


@pytest.mark.asyncio
async def test_install_firmware_raises_on_checksum_mismatch() -> None:
    """Each client turns the router's mismatch marker into an error."""
    from custom_components.openwrt.api.firmware_install import (
        FIRMWARE_CHECKSUM_MISMATCH,
        FirmwareInstallError,
    )
    from custom_components.openwrt.api.luci_rpc import LuciRpcClient
    from custom_components.openwrt.api.ssh import SshClient
    from custom_components.openwrt.api.ubus import UbusClient

    kwargs = {"host": "192.0.2.1", "username": "root", "password": "pwd"}
    for client in (
        UbusClient(MagicMock(), MagicMock(), **kwargs),
        LuciRpcClient(MagicMock(), MagicMock(), **kwargs),
        SshClient(MagicMock(), MagicMock(), **kwargs),
    ):
        router = _FakeJobRouter([f"{FIRMWARE_CHECKSUM_MISMATCH}\n{FIRMWARE_JOB_EXIT}1"])
        with (
            patch.object(client, "_firmware_exec", router),
            patch.object(firmware_install, "FIRMWARE_POLL_INTERVAL", 0),
            pytest.raises(FirmwareInstallError, match="checksum mismatch"),
        ):
            await client.install_firmware("https://example.com/fw.bin", sha256=SHA)
        assert "sha256sum" in router.launches[0]


@pytest.mark.asyncio
async def test_custom_build_install_passes_checksum_without_force(
    hass: HomeAssistant,
) -> None:
    """Custom repository images are verified and not flashed with -F."""
    entry = MagicMock()
    entry.entry_id = "test_entry_id"
    entry.options = {"auto_backup": False}
    entry.data = {"host": "192.0.2.1"}

    coordinator = MagicMock(spec=OpenWrtDataCoordinator)
    coordinator.hass = hass
    coordinator.config_entry = entry
    coordinator.client = AsyncMock()
    coordinator.router_id = "00:00:5e:00:53:01"
    coordinator.data = OpenWrtData()
    coordinator.data.is_custom_build = True
    coordinator.data.firmware_install_url = "https://example.com/fw-sysupgrade.bin"
    coordinator.data.firmware_checksum = SHA

    entity = OpenWrtUpdateEntity(coordinator, entry)
    entity.hass = hass
    await entity.async_install(version=None, backup=False)

    coordinator.client.install_firmware.assert_awaited_once_with(
        "https://example.com/fw-sysupgrade.bin",
        keep_settings=True,
        force=False,
        sha256=SHA,
    )


@pytest.mark.asyncio
async def test_custom_checksum_matches_exact_file_name(hass: HomeAssistant) -> None:
    """The checksum is taken from the line for exactly this file name."""
    entry = MagicMock()
    entry.entry_id = "test_entry_id"
    entry.options = {"update_interval": 30}
    entry.data = {"host": "192.0.2.1"}
    coordinator = OpenWrtDataCoordinator(hass, entry, AsyncMock())

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.text = AsyncMock(
        return_value=(
            f"{'11' * 32}  fw-sysupgrade.bin.sig\n"
            f"{'22' * 32}  other-fw-sysupgrade.bin\n"
            f"{'33' * 32} *fw-sysupgrade.bin\n"
        )
    )
    mock_session = MagicMock(spec=aiohttp.ClientSession)
    mock_session.get.return_value.__aenter__.return_value = mock_resp

    data = OpenWrtData()
    await coordinator._fetch_custom_checksum(
        data, "https://example.com/sha256sums", "fw-sysupgrade.bin", mock_session
    )
    assert data.firmware_checksum == "33" * 32


# --- ASU image selection and checksum ---


def _asu_done(images: list[dict]) -> dict:
    """An ASU GET /api/v1/build/{hash} response for a finished build."""
    return {"status": 200, "detail": "done", "bin_dir": "f00d", "images": images}


@pytest.mark.asyncio
async def test_asu_poll_returns_sysupgrade_image_and_sha256(
    hass: HomeAssistant,
) -> None:
    """The sysupgrade image is picked by type, never the first image."""
    from custom_components.openwrt.helpers.asu import AsuClient

    client = AsuClient(hass, "https://asu.example.com/")
    images = [
        {"name": "fw-initramfs-kernel.bin", "type": "kernel", "sha256": "11" * 32},
        {"name": "fw-squashfs-factory.bin", "type": "factory", "sha256": "22" * 32},
        {
            "name": "fw-squashfs-sysupgrade.bin",
            "type": "sysupgrade",
            "filesystem": "squashfs",
            "sha256": SHA,
        },
    ]
    with patch.object(client, "_request", AsyncMock(return_value=_asu_done(images))):
        image = await client.poll_build_status("f00d")

    assert image.url == "https://asu.example.com/store/f00d/fw-squashfs-sysupgrade.bin"
    assert image.sha256 == SHA


@pytest.mark.asyncio
async def test_asu_poll_prefers_squashfs_sysupgrade(hass: HomeAssistant) -> None:
    """With several sysupgrade filesystems, squashfs wins."""
    from custom_components.openwrt.helpers.asu import AsuClient

    client = AsuClient(hass)
    images = [
        {
            "name": "fw-ext4-sysupgrade.img.gz",
            "type": "sysupgrade",
            "filesystem": "ext4",
            "sha256": "33" * 32,
        },
        {
            "name": "fw-squashfs-sysupgrade.img.gz",
            "type": "sysupgrade",
            "filesystem": "squashfs",
            "sha256": SHA,
        },
    ]
    with patch.object(client, "_request", AsyncMock(return_value=_asu_done(images))):
        image = await client.poll_build_status("f00d")
    assert image.url.endswith("fw-squashfs-sysupgrade.img.gz")
    assert image.sha256 == SHA


@pytest.mark.parametrize(
    "images",
    [
        # Only a factory image (previously flashed as the fallback).
        [{"name": "fw-squashfs-factory.bin", "type": "factory", "sha256": SHA}],
        # Combined (x86) images are not sysupgrade images.
        [{"name": "fw-combined-efi.img.gz", "type": "combined-efi", "sha256": SHA}],
        # A sysupgrade image without a usable checksum.
        [{"name": "fw-sysupgrade.bin", "type": "sysupgrade"}],
        [{"name": "fw-sysupgrade.bin", "type": "sysupgrade", "sha256": "abc"}],
        [],
    ],
)
@pytest.mark.asyncio
async def test_asu_poll_refuses_without_verified_sysupgrade_image(
    hass: HomeAssistant, images: list[dict]
) -> None:
    """No sysupgrade image, or no sha256 for it, is an error, not a guess."""
    from custom_components.openwrt.helpers.asu import AsuClient, AsuClientError

    client = AsuClient(hass)
    with (
        patch.object(client, "_request", AsyncMock(return_value=_asu_done(images))),
        pytest.raises(AsuClientError),
    ):
        await client.poll_build_status("f00d")


def _install_entity(hass: HomeAssistant, data: OpenWrtData) -> OpenWrtUpdateEntity:
    entry = MagicMock()
    entry.entry_id = "test_entry_id"
    entry.options = {"auto_backup": False}
    entry.data = {"host": "192.0.2.1", "connection_type": "ssh"}

    coordinator = MagicMock(spec=OpenWrtDataCoordinator)
    coordinator.hass = hass
    coordinator.config_entry = entry
    coordinator.client = AsyncMock()
    coordinator.router_id = "00:00:5e:00:53:01"
    coordinator.name = "router-a"
    coordinator.data = data

    entity = OpenWrtUpdateEntity(coordinator, entry)
    entity.hass = hass
    return entity


@pytest.mark.asyncio
async def test_asu_install_passes_image_sha256(hass: HomeAssistant) -> None:
    """The ASU image is flashed with the checksum the ASU server published."""
    from custom_components.openwrt.helpers.asu import AsuImage

    data = OpenWrtData()
    data.asu_status = "available"
    data.asu_supported = True
    data.asu_update_available = True
    data.firmware_latest_version = "25.12.6"
    entity = _install_entity(hass, data)
    entity.coordinator.client.get_user_installed_packages = AsyncMock(
        return_value=["base-files"]
    )

    url = "https://asu.example.com/store/f00d/fw-squashfs-sysupgrade.bin"
    with patch("custom_components.openwrt.update.AsuClient") as asu_cls:
        asu_cls.return_value.request_build = AsyncMock(return_value="f00d")
        asu_cls.return_value.poll_build_status = AsyncMock(
            return_value=AsuImage(url=url, sha256=SHA)
        )
        await entity.async_install(version=None, backup=False)

    entity.coordinator.client.install_firmware.assert_awaited_once_with(
        url, keep_settings=True, force=False, sha256=SHA
    )


# --- Official (stock) image checksum ---


def _profiles_coordinator(hass: HomeAssistant, profiles_json: dict):
    entry = MagicMock()
    entry.entry_id = "test_entry_id"
    entry.options = {"update_interval": 30}
    entry.data = {"host": "192.0.2.1"}
    coordinator = OpenWrtDataCoordinator(hass, entry, AsyncMock())

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value=profiles_json)
    mock_session = MagicMock(spec=aiohttp.ClientSession)
    mock_session.get.return_value.__aenter__.return_value = mock_resp
    return coordinator, mock_session


@pytest.mark.asyncio
async def test_stable_release_carries_profiles_sha256(hass: HomeAssistant) -> None:
    """The stable image's sha256 comes from profiles.json with its URL."""
    coordinator, session = _profiles_coordinator(
        hass,
        {
            "profiles": {
                "vendor_router-a": {
                    "images": [
                        {
                            "name": "fw-factory.bin",
                            "type": "factory",
                            "sha256": "11" * 32,
                        },
                        {
                            "name": "fw-sysupgrade.bin",
                            "type": "sysupgrade",
                            "sha256": SHA,
                        },
                    ]
                }
            }
        },
    )
    data = OpenWrtData()
    data.device_info.target = "ramips/mt7621"
    data.device_info.board_name = "vendor,router-a"

    await coordinator._async_set_stable_release_urls(data, "25.12.6", session)

    assert data.firmware_install_url.endswith("/fw-sysupgrade.bin")
    assert data.firmware_checksum == SHA


@pytest.mark.asyncio
async def test_stable_release_guessed_url_has_no_checksum(hass: HomeAssistant) -> None:
    """The guessed fallback URL never inherits a checksum from elsewhere."""
    coordinator, session = _profiles_coordinator(hass, {"profiles": {}})
    data = OpenWrtData()
    data.device_info.target = "ramips/mt7621"
    data.device_info.board_name = "vendor,router-a"
    data.firmware_checksum = "44" * 32

    await coordinator._async_set_stable_release_urls(data, "25.12.6", session)

    assert data.firmware_install_url.endswith("-squashfs-sysupgrade.bin")
    assert data.firmware_checksum == ""


@pytest.mark.asyncio
async def test_snapshot_carries_profiles_sha256(hass: HomeAssistant) -> None:
    """The snapshot image's sha256 comes from profiles.json with its URL."""
    coordinator, session = _profiles_coordinator(
        hass,
        {
            "version_code": "r2-bbbb",
            "profiles": {
                "vendor_router_a": {
                    "images": [
                        {
                            "name": "fw-sysupgrade.bin",
                            "type": "sysupgrade",
                            "sha256": SHA,
                        }
                    ]
                }
            },
        },
    )
    data = OpenWrtData()
    data.device_info.target = "ramips/mt7621"
    data.device_info.board_name = "vendor,router-a"
    data.device_info.release_version = "SNAPSHOT"
    data.firmware_current_version = "SNAPSHOT (r1-aaaa)"

    await coordinator._check_snapshot_update(data, session)

    assert data.firmware_install_url.endswith("/fw-sysupgrade.bin")
    assert data.firmware_checksum == SHA


@pytest.mark.asyncio
async def test_stock_install_passes_checksum(hass: HomeAssistant) -> None:
    """An official image is flashed with its profiles.json checksum."""
    data = OpenWrtData()
    data.firmware_install_url = "https://downloads.example.com/fw-sysupgrade.bin"
    data.firmware_checksum = SHA
    data.asu_status = "unsupported"
    entity = _install_entity(hass, data)
    entity.coordinator.async_get_non_default_packages = AsyncMock(return_value=[])

    await entity.async_install(version=None, backup=False)

    entity.coordinator.client.install_firmware.assert_awaited_once_with(
        "https://downloads.example.com/fw-sysupgrade.bin",
        keep_settings=True,
        force=False,
        sha256=SHA,
    )


# --- Detached install (ubus, LuCI RPC, SSH) ---


def _rpc_clients() -> list:
    """A ubus, a LuCI RPC and an SSH client, each with its own connection error."""
    from custom_components.openwrt.api.luci_rpc import LuciRpcClient
    from custom_components.openwrt.api.luci_rpc.exceptions import (
        LuciRpcConnectionError,
    )
    from custom_components.openwrt.api.ssh import SshClient, SshConnectionError
    from custom_components.openwrt.api.ubus import UbusClient
    from custom_components.openwrt.api.ubus.exceptions import UbusConnectionError

    kwargs = {"host": "192.0.2.1", "username": "root", "password": "pwd"}
    return [
        (UbusClient(MagicMock(), MagicMock(), **kwargs), UbusConnectionError),
        (LuciRpcClient(MagicMock(), MagicMock(), **kwargs), LuciRpcConnectionError),
        (SshClient(MagicMock(), MagicMock(), **kwargs), SshConnectionError),
    ]


@pytest.fixture
def fast_polls():
    """Poll without waiting."""
    with patch.object(firmware_install, "FIRMWARE_POLL_INTERVAL", 0):
        yield


@pytest.mark.asyncio
@pytest.mark.parametrize("index", [0, 1, 2], ids=["ubus", "luci_rpc", "ssh"])
async def test_rpc_install_survives_long_download(index: int, fast_polls) -> None:
    """A download that outlasts any server timeout is polled, not cut off or re-run."""
    client, conn_error = _rpc_clients()[index]
    router = _FakeJobRouter(
        ["Downloading"] * 50
        + [f"Downloading\nDownload completed\n{FIRMWARE_FLASHING}", conn_error("gone")]
    )
    with patch.object(client, "_firmware_exec", router):
        await client.install_firmware("https://example.com/fw.bin", sha256=SHA)

    assert len(router.launches) == 1
    assert router.poll_count == 52
    assert router.cancels == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("index", [0, 1, 2], ids=["ubus", "luci_rpc", "ssh"])
async def test_rpc_install_timeout_cancels_and_raises(index: int, fast_polls) -> None:
    """A download still running at the deadline is cancelled and reported."""
    client, _ = _rpc_clients()[index]
    router = _FakeJobRouter(["Downloading"])
    with (
        patch.object(client, "_firmware_exec", router),
        patch.object(firmware_install, "FIRMWARE_JOB_TIMEOUT", 0.05),
        pytest.raises(
            firmware_install.FirmwareInstallError, match="sysupgrade will not run"
        ),
    ):
        await client.install_firmware("https://example.com/fw.bin", sha256=SHA)

    assert len(router.launches) == 1
    assert router.cancels == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("index", [0, 1, 2], ids=["ubus", "luci_rpc", "ssh"])
async def test_rpc_install_lost_contact_before_flash_is_unknown(
    index: int, fast_polls
) -> None:
    """Losing the router before sysupgrade starts is an error, never a reboot."""
    client, conn_error = _rpc_clients()[index]
    router = _FakeJobRouter(
        ["Downloading", conn_error("Cannot connect")],
        cancel=conn_error("Cannot connect"),
    )
    with (
        patch.object(client, "_firmware_exec", router),
        patch.object(firmware_install, "FIRMWARE_JOB_TIMEOUT", 0.05),
        pytest.raises(
            firmware_install.FirmwareInstallError, match="unknown whether sysupgrade"
        ),
    ):
        await client.install_firmware("https://example.com/fw.bin", sha256=SHA)
    assert len(router.launches) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("index", [0, 1, 2], ids=["ubus", "luci_rpc", "ssh"])
async def test_rpc_install_router_restart_before_flash_is_an_error(
    index: int, fast_polls
) -> None:
    """A job that vanished before sysupgrade started is not reported as success."""
    client, conn_error = _rpc_clients()[index]
    router = _FakeJobRouter(["Downloading", conn_error("reset"), FIRMWARE_JOB_MISSING])
    with (
        patch.object(client, "_firmware_exec", router),
        pytest.raises(firmware_install.FirmwareInstallError, match="restarted"),
    ):
        await client.install_firmware("https://example.com/fw.bin", sha256=SHA)


@pytest.mark.asyncio
@pytest.mark.parametrize("index", [0, 1, 2], ids=["ubus", "luci_rpc", "ssh"])
async def test_rpc_install_reports_sysupgrade_refusal(index: int, fast_polls) -> None:
    """sysupgrade rejecting the image surfaces its message and exit code."""
    client, _ = _rpc_clients()[index]
    router = _FakeJobRouter(
        [f"{FIRMWARE_FLASHING}\nImage check failed.\n{FIRMWARE_JOB_EXIT}1"]
    )
    with (
        patch.object(client, "_firmware_exec", router),
        pytest.raises(
            firmware_install.FirmwareInstallError, match="exit code 1.*Image check"
        ),
    ):
        await client.install_firmware("https://example.com/fw.bin", sha256=SHA)


@pytest.mark.asyncio
@pytest.mark.parametrize("index", [0, 1, 2], ids=["ubus", "luci_rpc", "ssh"])
async def test_rpc_install_refuses_while_another_runs(index: int, fast_polls) -> None:
    """A second install while one is running on the router is refused."""
    client, _ = _rpc_clients()[index]
    router = _FakeJobRouter([], launch=FIRMWARE_JOB_BUSY)
    with (
        patch.object(client, "_firmware_exec", router),
        pytest.raises(
            firmware_install.FirmwareInstallError, match="already|still running"
        ),
    ):
        await client.install_firmware("https://example.com/fw.bin", sha256=SHA)
    assert router.poll_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("index", [0, 1, 2], ids=["ubus", "luci_rpc", "ssh"])
async def test_rpc_install_too_late_to_cancel_follows_the_flash(
    index: int, fast_polls
) -> None:
    """If sysupgrade passed the gate first, the install watches it to the reboot."""
    client, conn_error = _rpc_clients()[index]
    # Polls show the download until the deadline; then the router goes down.
    router = _FakeJobRouter(
        ["Downloading"],
        cancel=FIRMWARE_JOB_TOO_LATE,
        after_cancel=[f"{FIRMWARE_FLASHING}", conn_error("gone")],
    )
    with (
        patch.object(client, "_firmware_exec", router),
        patch.object(firmware_install, "FIRMWARE_JOB_TIMEOUT", 0.05),
    ):
        await client.install_firmware("https://example.com/fw.bin", sha256=SHA)
    assert router.cancels == 1
    assert len(router.launches) == 1


@pytest.mark.asyncio
async def test_ssh_install_releases_the_connection_lock_between_polls(
    fast_polls,
) -> None:
    """Other SSH commands run while the download is in progress."""
    from custom_components.openwrt.api.ssh import SshClient, SshConnectionError

    client = SshClient(
        MagicMock(), MagicMock(), host="192.0.2.1", username="root", password="pwd"
    )
    downloaded = asyncio.Event()
    router = _FakeJobRouter(["Downloading"])

    async def exec_locked(command: str, retry: bool = True) -> str:
        assert client._connection_lock.locked()
        if command == "uptime":
            return "up"
        if downloaded.is_set() and "sysupgrade" not in command:
            if router.poll_count:
                raise SshConnectionError("gone")
            router.poll_count += 1
            return f"{FIRMWARE_FLASHING}\n{FIRMWARE_JOB_RUNNING}"
        return await router(command)

    with patch.object(client, "_exec_locked", side_effect=exec_locked):
        install = asyncio.create_task(
            client.install_firmware("https://example.com/fw.bin", sha256=SHA)
        )
        while router.poll_count < 3:
            await asyncio.sleep(0)
        # A regular poll gets the connection while the install is still running.
        assert await asyncio.wait_for(client._exec("uptime"), 1) == "up"
        assert not install.done()
        downloaded.set()
        router.poll_count = 0
        await install

    assert len(router.launches) == 1
    assert router.cancels == 0


def test_detached_command_is_idempotent_per_job() -> None:
    """The launch creates its job directory atomically before anything runs."""
    cmd = firmware_install.build_detached_sysupgrade_command(
        "0123456789abcdef", "https://example.com/fw.bin", sha256=SHA
    )
    job = f"{firmware_install.FIRMWARE_JOB_DIR_PREFIX}0123456789abcdef"
    assert cmd.startswith(f"if ! mkdir {job} 2>/dev/null; then")
    # The job takes the cancel gate and announces itself before sysupgrade.
    assert (
        cmd.index(f"mkdir {job}/gate")
        < cmd.index(FIRMWARE_FLASHING)
        < cmd.index("sysupgrade /tmp/firmware.bin")
    )
    assert "</dev/null &" in cmd
    with pytest.raises(firmware_install.FirmwareInstallError):
        firmware_install.build_detached_sysupgrade_command("x; reboot", "u")


@pytest.mark.asyncio
async def test_ubus_rpcd_timeout_is_not_success() -> None:
    """rpcd's exec timeout (status 7) used to read as an empty, successful run."""
    client, _ = _rpc_clients()[0]
    with (
        patch.object(
            client,
            "_call",
            AsyncMock(side_effect=base_ubus_error("Ubus call failed with code 7")),
        ) as call,
        pytest.raises(firmware_install.FirmwareInstallError, match="Could not start"),
    ):
        await client.install_firmware("https://example.com/fw.bin", sha256=SHA)
    call.assert_awaited_once()
    # execute_command still swallows it, which is why the install no longer uses it.
    with patch.object(
        client,
        "_call",
        AsyncMock(side_effect=base_ubus_error("Ubus call failed with code 7")),
    ):
        assert await client.execute_command("true") == ""


def base_ubus_error(message: str) -> Exception:
    """A UbusError as _call raises it."""
    from custom_components.openwrt.api.ubus.exceptions import UbusError

    return UbusError(message)


@pytest.mark.asyncio
async def test_luci_bad_gateway_is_not_success_and_not_rerun() -> None:
    """uhttpd's 502 after script_timeout used to fall through to success."""
    from custom_components.openwrt.api.luci_rpc.exceptions import LuciRpcError

    client, _ = _rpc_clients()[1]
    rpc = AsyncMock(
        side_effect=LuciRpcError("Communication error: 502, message='Bad Gateway'")
    )
    with (
        patch.object(client, "_rpc_call", rpc),
        pytest.raises(firmware_install.FirmwareInstallError, match="Could not start"),
    ):
        await client.install_firmware("https://example.com/fw.bin", sha256=SHA)
    # One sys.exec, no file.exec fallback that would run the command again.
    rpc.assert_awaited_once()
    endpoint, method, (wrapped,) = rpc.call_args[0]
    assert (endpoint, method) == ("sys", "exec")
    assert wrapped.startswith("/bin/sh -c 'if ! mkdir ")
    assert wrapped.endswith("' 2>&1")


# --- Attended Sysupgrade (ASU) detection and the stock-image fallback ---

ASU = "https://sysupgrade.openwrt.org"


def _asu_coordinator(hass: HomeAssistant) -> OpenWrtDataCoordinator:
    entry = MagicMock()
    entry.entry_id = "test_entry_id"
    entry.options = {"update_interval": 30}
    entry.data = {"host": "192.0.2.1"}
    return OpenWrtDataCoordinator(hass, entry, AsyncMock())


def _release_router_data() -> OpenWrtData:
    data = OpenWrtData()
    data.device_info.target = "ath79/generic"
    data.device_info.board_name = "tplink,archer-c7-v2"
    data.device_info.release_distribution = "OpenWrt"
    data.device_info.release_version = "24.10.2"
    data.firmware_current_version = "24.10.2"
    data.firmware_latest_version = "24.10.8"
    data.firmware_upgradable = True
    return data


def _overview(targets_2410: dict | None = None, targets_2512: dict | None = None):
    return {
        "latest": ["25.12.6", "24.10.8"],
        "branches": {
            "25.12": {
                "path": "releases/{version}",
                "enabled": True,
                "versions": ["25.12.6", "25.12.5"],
                "name": "25.12",
                "targets": targets_2512 or {},
            },
            "24.10": {
                "path": "releases/{version}",
                "enabled": True,
                "versions": ["24.10.8", "24.10.2"],
                "name": "24.10",
                "targets": (
                    targets_2410
                    if targets_2410 is not None
                    else {"ath79/generic": "mips_24kc"}
                ),
            },
        },
    }


def _fake_fetch(responses: dict[str, tuple[int, object]]):
    calls: list[str] = []

    async def _fetch(_session, url):
        calls.append(url)
        return responses.get(url, (404, None))

    return _fetch, calls


@pytest.mark.asyncio
async def test_fetch_asu_json_status_reports_404(hass: HomeAssistant) -> None:
    """A 404 is reported as such, never turned into a truthy payload."""
    coordinator = _asu_coordinator(hass)
    mock_resp = MagicMock()
    mock_resp.status = 404
    mock_resp.json = AsyncMock(return_value={"detail": "Not Found"})
    mock_session = MagicMock(spec=aiohttp.ClientSession)
    mock_session.get.return_value.__aenter__.return_value = mock_resp

    assert await coordinator._fetch_asu_json_status(mock_session, "u") == (404, None)
    assert await coordinator._fetch_asu_json(mock_session, "u") is None


@pytest.mark.asyncio
async def test_asu_overview_404_is_not_asu_support(hass: HomeAssistant) -> None:
    """A server answering 404 (e.g. the old /api/v1/info) must not mean 'supported'."""
    coordinator = _asu_coordinator(hass)
    data = _release_router_data()
    fetch, _ = _fake_fetch({})

    with patch.object(coordinator, "_fetch_asu_json_status", side_effect=fetch):
        await coordinator._check_asu_update(data)

    assert data.asu_status == "error"
    assert data.asu_supported is False
    assert data.asu_update_available is False


@pytest.mark.asyncio
async def test_asu_profile_404_is_unsupported(hass: HomeAssistant) -> None:
    """Target listed, but the board's profile JSON is 404: ASU does not support it."""
    coordinator = _asu_coordinator(hass)
    data = _release_router_data()
    fetch, calls = _fake_fetch({f"{ASU}/json/v1/overview.json": (200, _overview())})

    with patch.object(coordinator, "_fetch_asu_json_status", side_effect=fetch):
        await coordinator._check_asu_update(data)

    assert data.asu_status == "unsupported"
    assert data.asu_supported is False
    assert data.asu_update_available is False
    assert (
        f"{ASU}/json/v1/releases/24.10.8/targets/ath79/generic/tplink_archer-c7-v2.json"
        in calls
    )
    assert not any("/api/v1/info" in url for url in calls)


@pytest.mark.asyncio
async def test_asu_available_sets_update(hass: HomeAssistant) -> None:
    """Overview lists the target and the profile JSON exists: ASU is available."""
    coordinator = _asu_coordinator(hass)
    coordinator.client.get_installed_packages = AsyncMock(return_value=["tailscale"])
    data = _release_router_data()
    profile_url = (
        f"{ASU}/json/v1/releases/24.10.8/targets/ath79/generic/tplink_archer-c7-v2.json"
    )
    fetch, _ = _fake_fetch(
        {
            f"{ASU}/json/v1/overview.json": (200, _overview()),
            profile_url: (200, {"version_number": "24.10.8", "version_code": "r1"}),
        }
    )

    with patch.object(coordinator, "_fetch_asu_json_status", side_effect=fetch):
        await coordinator._check_asu_update(data)

    assert data.asu_status == "available"
    assert data.asu_supported is True
    assert data.asu_update_available is True
    assert data.asu_target_version == "24.10.8"
    assert data.asu_profile == "tplink_archer-c7-v2"


@pytest.mark.asyncio
async def test_asu_branch_with_zero_targets_is_temporarily_unavailable(
    hass: HomeAssistant,
) -> None:
    """Right after a release the branch is listed with no targets: wait, don't fall back."""
    coordinator = _asu_coordinator(hass)
    data = _release_router_data()
    data.firmware_latest_version = "25.12.6"
    fetch, calls = _fake_fetch({f"{ASU}/json/v1/overview.json": (200, _overview())})

    with patch.object(coordinator, "_fetch_asu_json_status", side_effect=fetch):
        await coordinator._check_asu_update(data)

    assert data.asu_status == "temporarily_unavailable"
    assert data.asu_supported is True
    assert data.asu_update_available is False
    assert "25.12" in data.asu_status_reason
    assert calls == [f"{ASU}/json/v1/overview.json"]


@pytest.mark.asyncio
async def test_asu_unlisted_newer_version_is_temporarily_unavailable(
    hass: HomeAssistant,
) -> None:
    """A release newer than anything ASU lists is 'not yet', not 'unsupported'."""
    coordinator = _asu_coordinator(hass)
    data = _release_router_data()
    data.firmware_latest_version = "25.12.7"
    fetch, _ = _fake_fetch({f"{ASU}/json/v1/overview.json": (200, _overview())})

    with patch.object(coordinator, "_fetch_asu_json_status", side_effect=fetch):
        await coordinator._check_asu_update(data)

    assert data.asu_status == "temporarily_unavailable"


def _entity(hass: HomeAssistant, data: OpenWrtData, conn: str = "ssh"):
    entry = MagicMock()
    entry.entry_id = "test_entry_id"
    entry.options = {"auto_backup": False}
    entry.data = {"host": "192.0.2.1", "connection_type": conn}

    coordinator = MagicMock(spec=OpenWrtDataCoordinator)
    coordinator.hass = hass
    coordinator.config_entry = entry
    coordinator.client = AsyncMock()
    coordinator.router_id = "00:00:5e:00:53:01"
    coordinator.name = "router"
    coordinator.data = data
    coordinator.async_get_non_default_packages = AsyncMock(return_value=[])

    entity = OpenWrtUpdateEntity(coordinator, entry)
    entity.hass = hass
    return entity, coordinator


def _stock_url_data() -> OpenWrtData:
    data = _release_router_data()
    data.firmware_install_url = (
        "https://downloads.openwrt.org/releases/24.10.8/targets/ath79/generic/"
        "openwrt-24.10.8-ath79-generic-tplink_archer-c7-v2-squashfs-sysupgrade.bin"
    )
    return data


@pytest.mark.asyncio
async def test_install_refused_while_asu_temporarily_unavailable(
    hass: HomeAssistant,
) -> None:
    """The stock image is never flashed as a fallback when ASU is only 'not yet'."""
    data = _stock_url_data()
    data.asu_status = "temporarily_unavailable"
    data.asu_status_reason = "The ASU server has not indexed any targets for 25.12 yet"
    data.asu_supported = True
    entity, coordinator = _entity(hass, data)

    can_install, reason = entity._check_installability(data)
    assert can_install is False
    assert "temporarily unavailable" in reason
    with pytest.raises(HomeAssistantError, match="temporarily unavailable"):
        await entity.async_install(version=None, backup=False)
    coordinator.client.install_firmware.assert_not_called()
    coordinator.client.create_backup.assert_not_called()


@pytest.mark.asyncio
async def test_install_refused_when_asu_supported_without_newer_build(
    hass: HomeAssistant,
) -> None:
    """ASU supports the board but has no newer build: refuse, don't flash stock."""
    data = _stock_url_data()
    data.asu_status = "available"
    data.asu_supported = True
    data.asu_update_available = False
    entity, coordinator = _entity(hass, data)

    with pytest.raises(HomeAssistantError, match="would remove every package"):
        await entity.async_install(version=None, backup=False)
    coordinator.client.install_firmware.assert_not_called()


@pytest.mark.asyncio
async def test_stock_install_refused_when_router_has_extra_packages(
    hass: HomeAssistant,
) -> None:
    """ASU unsupported: the stock image is refused if it would drop packages."""
    data = _stock_url_data()
    data.asu_status = "unsupported"
    entity, coordinator = _entity(hass, data)
    coordinator.async_get_non_default_packages = AsyncMock(
        return_value=["mwan3", "pbr", "tailscale"]
    )

    with pytest.raises(HomeAssistantError, match="mwan3, pbr, tailscale"):
        await entity.async_install(version=None, backup=False)
    coordinator.client.install_firmware.assert_not_called()


@pytest.mark.asyncio
async def test_stock_install_refused_when_packages_unknown(
    hass: HomeAssistant,
) -> None:
    """If the package comparison cannot be made, the stock image is refused."""
    data = _stock_url_data()
    data.asu_status = "error"
    entity, coordinator = _entity(hass, data)
    coordinator.async_get_non_default_packages = AsyncMock(return_value=None)

    with pytest.raises(HomeAssistantError, match="could not confirm"):
        await entity.async_install(version=None, backup=False)
    coordinator.client.install_firmware.assert_not_called()


@pytest.mark.asyncio
async def test_stock_install_allowed_for_stock_router_without_asu(
    hass: HomeAssistant,
) -> None:
    """ASU unsupported and nothing beyond the stock image: flashing stock is fine."""
    data = _stock_url_data()
    data.asu_status = "unsupported"
    entity, coordinator = _entity(hass, data)

    await entity.async_install(version=None, backup=False)
    coordinator.client.install_firmware.assert_awaited_once()
    call = coordinator.client.install_firmware.call_args
    assert call.args[0] == data.firmware_install_url
    assert call.kwargs["keep_settings"] is True
    assert call.kwargs["force"] is False


@pytest.mark.asyncio
async def test_asu_install_refused_without_package_list(hass: HomeAssistant) -> None:
    """An ASU build with an empty package list would be a stock image: refuse."""
    data = _release_router_data()
    data.asu_status = "available"
    data.asu_supported = True
    data.asu_update_available = True
    entity, coordinator = _entity(hass, data)
    coordinator.client.get_user_installed_packages = AsyncMock(return_value=None)
    coordinator.client.get_installed_packages = AsyncMock(return_value=[])

    with (
        patch("custom_components.openwrt.update.AsuClient") as mock_asu,
        pytest.raises(HomeAssistantError, match="installed packages"),
    ):
        await entity.async_install(version=None, backup=False)
    mock_asu.assert_not_called()
    coordinator.client.install_firmware.assert_not_called()


@pytest.mark.asyncio
async def test_asu_install_builds_with_router_packages(hass: HomeAssistant) -> None:
    """The ASU flow requests the resolved version/profile with the router's packages."""
    from custom_components.openwrt.helpers.asu import AsuImage

    data = _release_router_data()
    data.asu_status = "available"
    data.asu_supported = True
    data.asu_update_available = True
    data.asu_target_version = "24.10.8"
    data.asu_profile = "tplink_archer-c7-v2"
    entity, coordinator = _entity(hass, data)
    coordinator._get_target = MagicMock(side_effect=lambda t: t)
    coordinator.client.get_user_installed_packages = AsyncMock(
        return_value=["base-files", "tailscale"]
    )

    asu_instance = MagicMock()
    asu_instance.request_build = AsyncMock(return_value="hash")
    asu_instance.poll_build_status = AsyncMock(
        return_value=AsuImage(url=f"{ASU}/store/x/img.bin", sha256=SHA)
    )
    with patch("custom_components.openwrt.update.AsuClient", return_value=asu_instance):
        await entity.async_install(version=None, backup=False)

    kwargs = asu_instance.request_build.call_args.kwargs
    assert kwargs["version"] == "24.10.8"
    assert kwargs["board_name"] == "tplink_archer-c7-v2"
    assert kwargs["packages"] == ["base-files", "tailscale"]
    coordinator.client.install_firmware.assert_awaited_once_with(
        f"{ASU}/store/x/img.bin", keep_settings=True, force=False, sha256=SHA
    )


@pytest.mark.asyncio
async def test_non_default_packages_against_stock_profile(hass: HomeAssistant) -> None:
    """Extra packages are the explicitly installed ones the stock image lacks."""
    coordinator = _asu_coordinator(hass)
    coordinator.client.get_user_installed_packages = AsyncMock(
        return_value=[
            "base-files",
            "dnsmasq",
            "luci",
            "kernel",
            "kmod-usb2",
            "tailscale",
            "pbr",
        ]
    )
    data = _release_router_data()
    profiles_url = (
        "https://downloads.openwrt.org/releases/24.10.2/targets/ath79/generic/"
        "profiles.json"
    )
    fetch, _ = _fake_fetch(
        {
            profiles_url: (
                200,
                {
                    "default_packages": ["base-files", "dnsmasq", "ppp"],
                    "profiles": {
                        "tplink_archer-c7-v2": {
                            "device_packages": ["kmod-usb2", "-ppp"],
                        }
                    },
                },
            )
        }
    )

    with patch.object(coordinator, "_fetch_asu_json_status", side_effect=fetch):
        extra = await coordinator.async_get_non_default_packages(data)

    assert extra == ["pbr", "tailscale"]


@pytest.mark.asyncio
async def test_non_default_packages_unknown_without_package_db(
    hass: HomeAssistant,
) -> None:
    """No readable package list means 'unknown', not 'no extras'."""
    coordinator = _asu_coordinator(hass)
    coordinator.client.get_user_installed_packages = AsyncMock(return_value=None)

    assert (
        await coordinator.async_get_non_default_packages(_release_router_data()) is None
    )


@pytest.mark.asyncio
async def test_get_user_installed_packages_parsing() -> None:
    """apk world constraints are stripped; a missing package DB returns None."""
    from custom_components.openwrt.api.ssh import SshClient

    client = SshClient(
        MagicMock(), MagicMock(), host="192.0.2.1", username="root", password="pwd"
    )
    marker = "__OPENWRT_HA_PKG_LIST__"
    ok = "__OPENWRT_HA_PKG_READ_OK__"

    with patch.object(client, "execute_command", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = (
            f"{marker}\nbase-files\ntailscale>=1.80\npbr@custom\n{ok}\n"
        )
        assert await client.get_user_installed_packages() == [
            "base-files",
            "tailscale",
            "pbr",
        ]

        # A successful read of an empty list (opkg with no user packages).
        mock_exec.return_value = f"{marker}\n{ok}\n"
        assert await client.get_user_installed_packages() == []

        # The read failed after the start marker: no success marker.
        mock_exec.return_value = f"{marker}\n"
        assert await client.get_user_installed_packages() is None
        mock_exec.return_value = f"{marker}\ncat: /etc/apk/world: I/O error\n"
        assert await client.get_user_installed_packages() is None

        mock_exec.return_value = f"{marker}\n__NO_PACKAGE_DB__\n{ok}\n"
        assert await client.get_user_installed_packages() is None

        mock_exec.return_value = ""
        assert await client.get_user_installed_packages() is None

        mock_exec.side_effect = RuntimeError("permission denied")
        assert await client.get_user_installed_packages() is None


# --- Package names sent to ASU ---

# Real apk names with a dash followed by a digit, as `apk info` prints them
# (no version suffix without -v). None of these may be shortened.
APK_NAMES = [
    "base-files",
    "kmod-ath11k",
    "ath11k-firmware-ipq8074",
    "libnghttp2-14",
    "libustream-mbedtls20201210",
    "tailscale",
]


@pytest.mark.asyncio
async def test_installed_package_names_are_not_stripped() -> None:
    """apk names like libnghttp2-14 reach callers unchanged on every backend."""
    from custom_components.openwrt.api.luci_rpc import LuciRpcClient
    from custom_components.openwrt.api.ssh import SshClient
    from custom_components.openwrt.api.ubus import UbusClient

    kwargs = {"host": "192.0.2.1", "username": "root", "password": "pwd"}
    output = "\n".join(APK_NAMES) + "\n"
    for client, runner in [
        (LuciRpcClient(MagicMock(), MagicMock(), **kwargs), "execute_command"),
        (UbusClient(MagicMock(), MagicMock(), **kwargs), "execute_command"),
        (SshClient(MagicMock(), MagicMock(), **kwargs), "_exec"),
    ]:
        with patch.object(client, runner, new_callable=AsyncMock, return_value=output):
            assert await client.get_installed_packages() == APK_NAMES


@pytest.mark.asyncio
async def test_asu_build_sends_apk_world_names_unchanged(hass: HomeAssistant) -> None:
    """The ASU request carries the explicitly installed set, names untouched."""
    data = _release_router_data()
    data.asu_status = "available"
    data.asu_supported = True
    data.asu_update_available = True
    data.asu_target_version = "25.12.6"
    entity, coordinator = _entity(hass, data)
    coordinator._get_target = MagicMock(side_effect=lambda t: t)
    coordinator.client.get_user_installed_packages = AsyncMock(return_value=APK_NAMES)
    coordinator.client.get_installed_packages = AsyncMock(return_value=["unused"])

    asu_instance = MagicMock()
    asu_instance.request_build = AsyncMock(side_effect=RuntimeError("stop here"))
    with (
        patch(
            "custom_components.openwrt.update.AsuClient",
            return_value=asu_instance,
        ),
        pytest.raises(HomeAssistantError),
    ):
        await entity.async_install(version=None, backup=False)

    assert asu_instance.request_build.call_args.kwargs["packages"] == APK_NAMES
    coordinator.client.get_installed_packages.assert_not_called()


@pytest.mark.asyncio
async def test_asu_build_falls_back_to_full_list_without_world(
    hass: HomeAssistant,
) -> None:
    """If the explicit set cannot be read, the full installed list is sent."""
    data = _release_router_data()
    data.asu_status = "available"
    data.asu_supported = True
    data.asu_update_available = True
    entity, coordinator = _entity(hass, data)
    coordinator._get_target = MagicMock(side_effect=lambda t: t)
    coordinator.client.get_user_installed_packages = AsyncMock(return_value=None)
    coordinator.client.get_installed_packages = AsyncMock(return_value=APK_NAMES)

    asu_instance = MagicMock()
    asu_instance.request_build = AsyncMock(side_effect=RuntimeError("stop here"))
    with (
        patch(
            "custom_components.openwrt.update.AsuClient",
            return_value=asu_instance,
        ),
        pytest.raises(HomeAssistantError),
    ):
        await entity.async_install(version=None, backup=False)

    assert asu_instance.request_build.call_args.kwargs["packages"] == APK_NAMES


# --- ASU client identification ---


def test_asu_client_name_is_name_slash_version() -> None:
    """ASU expects '<name>/<version>' (it answers 500 otherwise)."""
    import json
    import re
    from pathlib import Path

    from custom_components.openwrt.helpers.asu import ASU_CLIENT_NAME

    manifest = Path(__file__).parent.parent / "custom_components/openwrt/manifest.json"
    version = json.loads(manifest.read_text())["version"]
    assert re.fullmatch(r"ha-openwrt/\d+(\.\d+)*", ASU_CLIENT_NAME)
    assert version.startswith(ASU_CLIENT_NAME.split("/", 1)[1])


@pytest.mark.asyncio
async def test_asu_build_payload_has_no_router_or_entry_name(
    hass: HomeAssistant,
) -> None:
    """The public ASU server gets the client id only, never names from the lab."""
    import json

    from custom_components.openwrt.helpers.asu import ASU_CLIENT_NAME, AsuClient

    data = _release_router_data()
    data.asu_status = "available"
    data.asu_supported = True
    data.asu_update_available = True
    data.asu_target_version = "25.12.6"
    entity, coordinator = _entity(hass, data)
    coordinator.name = "living-room-router"
    coordinator.config_entry.title = "living-room-router"
    coordinator._get_target = MagicMock(side_effect=lambda t: t)
    coordinator.client.get_user_installed_packages = AsyncMock(
        return_value=["base-files"]
    )

    payloads: list[dict] = []

    async def fake_request(method, path, payload=None, timeout=60.0):
        payloads.append(payload)
        raise RuntimeError("stop after the build request")

    with (
        patch.object(AsuClient, "_request", side_effect=fake_request),
        pytest.raises(HomeAssistantError),
    ):
        await entity.async_install(version=None, backup=False)

    assert payloads[0]["client"] == ASU_CLIENT_NAME
    assert "living-room-router" not in json.dumps(payloads[0])


# --- apk world entries that are not package names ---

WORLD_WITH_CONSTRAINTS = (
    "__OPENWRT_HA_PKG_LIST__\n"
    "base-files\n"
    "!wpad-basic-mbedtls\n"
    "wpad-mbedtls\n"
    "kernel=6.12.112~f0a7348f9cb959e1bf4d0447669b031e-r1\n"
    "tailscale>=1.80\n"
    "pbr@custom\n"
    "libnghttp2-14\n"
    "__OPENWRT_HA_PKG_READ_OK__\n"
)
CLEAN_WORLD = [
    "base-files",
    "wpad-mbedtls",
    "kernel",
    "tailscale",
    "pbr",
    "libnghttp2-14",
]


@pytest.mark.asyncio
async def test_world_exclusions_and_constraints_are_not_package_names() -> None:
    """'!pkg' entries are dropped and constraint suffixes are stripped."""
    from custom_components.openwrt.api.ssh import SshClient

    client = SshClient(
        MagicMock(), MagicMock(), host="192.0.2.1", username="root", password="pwd"
    )
    with patch.object(
        client,
        "execute_command",
        new_callable=AsyncMock,
        return_value=WORLD_WITH_CONSTRAINTS,
    ):
        assert await client.get_user_installed_packages() == CLEAN_WORLD


@pytest.mark.asyncio
async def test_asu_payload_gets_only_clean_world_names(hass: HomeAssistant) -> None:
    """End to end: a world with '!pkg' and constraints yields a valid payload."""
    from custom_components.openwrt.api.ssh import SshClient
    from custom_components.openwrt.helpers.asu import AsuClient

    data = _release_router_data()
    data.asu_status = "available"
    data.asu_supported = True
    data.asu_update_available = True
    data.asu_target_version = "25.12.6"
    entity, coordinator = _entity(hass, data)
    coordinator._get_target = MagicMock(side_effect=lambda t: t)
    ssh = SshClient(
        MagicMock(), MagicMock(), host="192.0.2.1", username="root", password="pwd"
    )
    coordinator.client = ssh

    payloads: list[dict] = []

    async def fake_request(method, path, payload=None, timeout=60.0):
        payloads.append(payload)
        raise RuntimeError("stop after the build request")

    with (
        patch.object(
            ssh,
            "execute_command",
            new_callable=AsyncMock,
            return_value=WORLD_WITH_CONSTRAINTS,
        ),
        patch.object(AsuClient, "_request", side_effect=fake_request),
        pytest.raises(HomeAssistantError),
    ):
        await entity.async_install(version=None, backup=False)

    assert payloads[0]["packages"] == CLEAN_WORLD
    assert not any(p.startswith("!") for p in payloads[0]["packages"])


@pytest.mark.asyncio
async def test_asu_refuses_invalid_package_names_before_requesting(
    hass: HomeAssistant,
) -> None:
    """A name ASU would reject is named in the error and nothing is sent."""
    from custom_components.openwrt.helpers.asu import AsuClient

    data = _release_router_data()
    data.asu_status = "available"
    data.asu_supported = True
    data.asu_update_available = True
    entity, coordinator = _entity(hass, data)
    coordinator.client.get_user_installed_packages = AsyncMock(
        return_value=["base-files", "odd$name"]
    )

    with (
        patch.object(AsuClient, "_request", new_callable=AsyncMock) as mock_request,
        pytest.raises(HomeAssistantError, match=r"odd\$name"),
    ):
        await entity.async_install(version=None, backup=False)
    mock_request.assert_not_called()
    coordinator.client.install_firmware.assert_not_called()
