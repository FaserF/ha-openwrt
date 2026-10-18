"""Tests for firmware update backup failure and URL resolution."""

from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.openwrt.coordinator import OpenWrtData, OpenWrtDataCoordinator
from custom_components.openwrt.update import OpenWrtUpdateEntity


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

    with patch.object(
        ubus_client, "execute_command", new_callable=AsyncMock
    ) as mock_exec:
        await ubus_client.install_firmware(
            "http://example.com/firmware.bin", keep_settings=True, force=False
        )
        cmd = mock_exec.call_args[0][0]
        assert "sysupgrade" in cmd
        assert not cmd.endswith("rm -f /tmp/firmware.bin")

    with patch.object(
        luci_client, "execute_command", new_callable=AsyncMock
    ) as mock_exec:
        await luci_client.install_firmware(
            "http://example.com/firmware.bin", keep_settings=True, force=False
        )
        cmd = mock_exec.call_args[0][0]
        assert "sysupgrade" in cmd
        assert not cmd.endswith("rm -f /tmp/firmware.bin")

    with patch.object(ssh_client, "_exec", new_callable=AsyncMock) as mock_exec:
        await ssh_client.install_firmware(
            "http://example.com/firmware.bin", keep_settings=True, force=False
        )
        cmd = mock_exec.call_args[0][0]
        assert "sysupgrade" in cmd
        assert not cmd.endswith("rm -f /tmp/firmware.bin")


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
    coordinator.client.get_installed_packages = AsyncMock(return_value=[])

    with (
        patch("custom_components.openwrt.helpers.asu.AsuClient") as mock_asu,
        pytest.raises(HomeAssistantError, match="installed packages"),
    ):
        await entity.async_install(version=None, backup=False)
    mock_asu.assert_not_called()
    coordinator.client.install_firmware.assert_not_called()


@pytest.mark.asyncio
async def test_asu_install_builds_with_router_packages(hass: HomeAssistant) -> None:
    """The ASU flow requests the resolved version/profile with the router's packages."""
    data = _release_router_data()
    data.asu_status = "available"
    data.asu_supported = True
    data.asu_update_available = True
    data.asu_target_version = "24.10.8"
    data.asu_profile = "tplink_archer-c7-v2"
    entity, coordinator = _entity(hass, data)
    coordinator._get_target = MagicMock(side_effect=lambda t: t)
    coordinator.client.get_installed_packages = AsyncMock(
        return_value=["base-files", "tailscale"]
    )

    asu_instance = MagicMock()
    asu_instance.request_build = AsyncMock(return_value="hash")
    asu_instance.poll_build_status = AsyncMock(return_value=f"{ASU}/store/x/img.bin")
    with patch(
        "custom_components.openwrt.helpers.asu.AsuClient", return_value=asu_instance
    ):
        await entity.async_install(version=None, backup=False)

    kwargs = asu_instance.request_build.call_args.kwargs
    assert kwargs["version"] == "24.10.8"
    assert kwargs["board_name"] == "tplink_archer-c7-v2"
    assert kwargs["packages"] == ["base-files", "tailscale"]
    coordinator.client.install_firmware.assert_awaited_once_with(
        f"{ASU}/store/x/img.bin", keep_settings=True
    )


@pytest.mark.asyncio
async def test_non_default_packages_against_stock_profile(hass: HomeAssistant) -> None:
    """Extra packages are the explicitly installed ones the stock image lacks."""
    coordinator = _asu_coordinator(hass)
    coordinator.client.get_user_installed_packages = AsyncMock(
        return_value=["base-files", "dnsmasq", "luci", "kmod-usb2", "tailscale", "pbr"]
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

    with patch.object(client, "execute_command", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = f"{marker}\nbase-files\ntailscale>=1.80\npbr@custom\n"
        assert await client.get_user_installed_packages() == [
            "base-files",
            "tailscale",
            "pbr",
        ]

        mock_exec.return_value = f"{marker}\n"
        assert await client.get_user_installed_packages() == []

        mock_exec.return_value = f"{marker}\n__NO_PACKAGE_DB__\n"
        assert await client.get_user_installed_packages() is None

        mock_exec.return_value = ""
        assert await client.get_user_installed_packages() is None

        mock_exec.side_effect = RuntimeError("permission denied")
        assert await client.get_user_installed_packages() is None
