"""Tests for automated backup download and cleanup."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.openwrt import _register_services
from custom_components.openwrt.const import DATA_CLIENT, DOMAIN


@pytest.mark.asyncio
async def test_backup_download_service(hass: MagicMock) -> None:
    """Test the create_backup service downloads the backup and removes remote file."""
    mock_client = MagicMock()
    mock_client.create_backup = AsyncMock(return_value="/tmp/backup-ha-12345.tar.gz")
    mock_client.download_file = AsyncMock(return_value=True)
    mock_client.execute_command = AsyncMock()

    hass.data = {
        DOMAIN: {
            "test_entry_id": {
                DATA_CLIENT: mock_client,
            }
        }
    }

    hass.config.path = MagicMock(
        side_effect=lambda *args: os.path.join("/fake/config", *args)
    )

    hass.services.async_register = MagicMock()

    with patch("os.makedirs") as mock_makedirs:
        _register_services(hass)

        backup_handler = None
        for call in hass.services.async_register.call_args_list:
            if call[0][1] == "create_backup":
                backup_handler = call[0][2]
                break

        assert backup_handler is not None

        call_data = MagicMock()
        call_data.data = {
            "entry_id": "test_entry_id",
            "download_path": "my_backups",
        }

        res = await backup_handler(call_data)

        # Check download directory creation and download_file call
        assert mock_makedirs.call_count == 1
        assert os.path.normpath(mock_makedirs.call_args[0][0]) == os.path.normpath(
            "/fake/config/my_backups"
        )
        assert mock_client.download_file.call_count == 1
        assert (
            mock_client.download_file.call_args[0][0] == "/tmp/backup-ha-12345.tar.gz"
        )
        assert os.path.normpath(
            mock_client.download_file.call_args[0][1]
        ) == os.path.normpath("/fake/config/my_backups/backup-ha-12345.tar.gz")

        # Check remote file deletion
        mock_client.execute_command.assert_called_once_with(
            "rm -f /tmp/backup-ha-12345.tar.gz"
        )

        assert res["backup_path"] == "/tmp/backup-ha-12345.tar.gz"
        assert res["filename"] == "backup-ha-12345.tar.gz"
        assert os.path.normpath(res["local_path"]) == os.path.normpath(
            "/fake/config/my_backups/backup-ha-12345.tar.gz"
        )


@pytest.mark.asyncio
async def test_backup_retention_policy(hass: MagicMock) -> None:
    """Test the create_backup service respects backup retention policy."""
    mock_client = MagicMock()
    mock_client.create_backup = AsyncMock(return_value="/tmp/backup-ha-new.tar.gz")
    mock_client.download_file = AsyncMock(return_value=True)
    mock_client.execute_command = AsyncMock()

    mock_entry = MagicMock()
    mock_entry.options = {"backup_retention_days": 30}

    hass.data = {
        DOMAIN: {
            "test_entry_id": {
                DATA_CLIENT: mock_client,
            }
        }
    }

    hass.config.path = MagicMock(
        side_effect=lambda *args: os.path.join("/fake/config", *args)
    )

    hass.services.async_register = MagicMock()

    with (
        patch("os.makedirs"),
        patch("os.listdir") as mock_listdir,
        patch("os.path.getmtime") as mock_getmtime,
        patch("os.remove") as mock_remove,
        patch("time.time", return_value=10000000),
        patch.object(hass.config_entries, "async_get_entry", return_value=mock_entry),
    ):
        _register_services(hass)

        backup_handler = None
        for call in hass.services.async_register.call_args_list:
            if call[0][1] == "create_backup":
                backup_handler = call[0][2]
                break

        assert backup_handler is not None

        # Return three files:
        # 1. Old backup file (> 30 days, should be removed) -> 10000000 - 31*86400
        # 2. New backup file (< 30 days, should be kept) -> 10000000 - 10*86400
        # 3. Not a backup file (should be ignored)
        mock_listdir.return_value = [
            "backup-ha-old.tar.gz",
            "backup-ha-new.tar.gz",
            "other-file.txt",
        ]

        def getmtime_side_effect(path: str) -> float:
            if "old" in path:
                return float(10000000 - 31 * 86400)
            return float(10000000 - 10 * 86400)

        mock_getmtime.side_effect = getmtime_side_effect

        call_data = MagicMock()
        call_data.data = {
            "entry_id": "test_entry_id",
            "download_path": "my_backups",
        }

        await backup_handler(call_data)

        # Assert old backup was removed
        assert mock_remove.call_count == 1
        assert os.path.normpath(mock_remove.call_args[0][0]) == os.path.normpath(
            "/fake/config/my_backups/backup-ha-old.tar.gz"
        )


@pytest.mark.asyncio
async def test_ubus_download_file_base64() -> None:
    """Test that UbusClient.download_file passes base64: True to file.read."""
    import base64
    from custom_components.openwrt.api.ubus import UbusClient

    ubus_client = UbusClient(
        MagicMock(), MagicMock(), host="192.168.1.1", username="root", password="pwd"
    )
    raw_content = b"fake binary tarball content \x8b\x00"
    b64_content = base64.b64encode(raw_content).decode("ascii")

    with (
        patch.object(ubus_client, "_call", new_callable=AsyncMock) as mock_call,
        patch("builtins.open", MagicMock()) as mock_file,
    ):
        mock_call.return_value = {"data": b64_content}
        mock_file_handle = MagicMock()
        mock_file.return_value.__enter__.return_value = mock_file_handle

        success = await ubus_client.download_file(
            "/tmp/backup.tar.gz", "/tmp/local_backup.tar.gz"
        )
        assert success is True
        mock_call.assert_called_once_with(
            "file", "read", {"path": "/tmp/backup.tar.gz", "base64": True}
        )
        mock_file_handle.write.assert_called_once_with(raw_content)

