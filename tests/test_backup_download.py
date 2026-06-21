"""Tests for automated backup download and cleanup."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from custom_components.openwrt import _register_services
from custom_components.openwrt.const import DOMAIN, DATA_CLIENT


@pytest.mark.asyncio
async def test_backup_download_service(hass) -> None:
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

    hass.config.path = MagicMock(side_effect=lambda *args: os.path.join("/fake/config", *args))

    with (
        patch("homeassistant.core.ServiceRegistry.async_register") as mock_register,
        patch("os.makedirs") as mock_makedirs
    ):
        _register_services(hass)
        
        backup_handler = None
        for call in mock_register.call_args_list:
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
        mock_makedirs.assert_called_once_with("/fake/config/my_backups", exist_ok=True)
        mock_client.download_file.assert_called_once_with(
            "/tmp/backup-ha-12345.tar.gz",
            os.path.normpath("/fake/config/my_backups/backup-ha-12345.tar.gz")
        )
        
        # Check remote file deletion
        mock_client.execute_command.assert_called_once_with("rm -f /tmp/backup-ha-12345.tar.gz")
        
        assert res == {
            "backup_path": "/tmp/backup-ha-12345.tar.gz",
            "local_path": os.path.normpath("/fake/config/my_backups/backup-ha-12345.tar.gz"),
            "filename": "backup-ha-12345.tar.gz",
        }
