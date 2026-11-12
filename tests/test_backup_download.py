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


# --- Binary-safe download over LuCI RPC and SSH ---

GZIP_BYTES = b"\x1f\x8b\x08\x00" + bytes(range(256)) + b"\x00\xff" * 64


def _encoded_output(data: bytes, size: int | None = None, hex_mode: bool = False):
    import base64

    size = len(data) if size is None else size
    if hex_mode:
        body = "__HA_DL_HEX__\n" + data.hex()
    else:
        encoded = base64.b64encode(data).decode()
        # Wrapped like `base64` output, to prove whitespace is tolerated
        body = "\n".join(encoded[i : i + 76] for i in range(0, len(encoded), 76))
    return f"__HA_DL_SIZE__   {size}\n__HA_DL_BEGIN__\n{body}\n__HA_DL_END__\n"


def _luci_client():
    from custom_components.openwrt.api.luci_rpc import LuciRpcClient

    return LuciRpcClient(
        MagicMock(), MagicMock(), host="192.0.2.1", username="root", password="pwd"
    )


@pytest.mark.asyncio
async def test_luci_download_never_saves_raw_output() -> None:
    """Raw (unencoded) output is never saved, and no RPC read endpoint is called."""
    client = _luci_client()
    raw = GZIP_BYTES.decode("latin-1")

    with (
        patch.object(client, "_rpc_call", new_callable=AsyncMock) as mock_rpc,
        patch.object(client, "execute_command", new_callable=AsyncMock) as mock_exec,
        patch.object(client, "_write_bytes") as mock_write,
    ):
        mock_rpc.return_value = raw
        mock_exec.return_value = raw
        assert await client.download_file("/tmp/b.tar.gz", "/tmp/l.tar.gz") is False
        mock_write.assert_not_called()
        mock_rpc.assert_not_called()


@pytest.mark.asyncio
async def test_luci_download_round_trips_binary_base64_and_hex() -> None:
    """Router-side encoding (base64 or hex) round-trips arbitrary bytes."""
    client = _luci_client()
    for hex_mode in (False, True):
        with (
            patch.object(
                client,
                "execute_command",
                new_callable=AsyncMock,
                return_value=_encoded_output(GZIP_BYTES, hex_mode=hex_mode),
            ) as mock_exec,
            patch.object(client, "_write_bytes") as mock_write,
        ):
            assert await client.download_file("/tmp/b.tar.gz", "/tmp/l.tar.gz")
            mock_write.assert_called_once_with("/tmp/l.tar.gz", GZIP_BYTES)
            cmd = mock_exec.call_args[0][0]
            assert "b64enc(fs.readfile(" in cmd
            assert "wc -c" in cmd


@pytest.mark.asyncio
async def test_download_size_mismatch_is_not_saved() -> None:
    """A decoded size that differs from the router's byte count fails."""
    client = _luci_client()
    with (
        patch.object(
            client,
            "execute_command",
            new_callable=AsyncMock,
            return_value=_encoded_output(GZIP_BYTES[:-10], size=len(GZIP_BYTES)),
        ),
        patch.object(client, "_write_bytes") as mock_write,
    ):
        assert await client.download_file("/tmp/b.tar.gz", "/tmp/l.tar.gz") is False
        mock_write.assert_not_called()


@pytest.mark.asyncio
async def test_download_fails_clearly_without_encoder_or_file() -> None:
    """No encoder on the router, or no file, fails without writing anything."""
    client = _luci_client()
    for output in (
        "__HA_DL_SIZE__10\n__HA_DL_BEGIN__\n__HA_DL_HEX__\n__HA_DL_NOENC__\n\n__HA_DL_END__\n",
        "__HA_DL_NOFILE__\n",
        "",
    ):
        with (
            patch.object(
                client, "execute_command", new_callable=AsyncMock, return_value=output
            ),
            patch.object(client, "_write_bytes") as mock_write,
        ):
            assert await client.download_file("/tmp/b", "/tmp/l") is False
            mock_write.assert_not_called()


@pytest.mark.asyncio
async def test_ssh_download_uses_router_side_encoding() -> None:
    """SSH no longer relies on a base64 binary; it uses the verified helper."""
    from custom_components.openwrt.api.ssh import SshClient

    client = SshClient(
        MagicMock(), MagicMock(), host="192.0.2.1", username="root", password="pwd"
    )
    with (
        patch.object(
            client,
            "_exec",
            new_callable=AsyncMock,
            return_value=_encoded_output(GZIP_BYTES),
        ) as mock_exec,
        patch.object(client, "_write_bytes") as mock_write,
    ):
        assert await client.download_file("/tmp/b.tar.gz", "/tmp/l.tar.gz")
        mock_write.assert_called_once_with("/tmp/l.tar.gz", GZIP_BYTES)
        assert mock_exec.await_count == 1


@pytest.mark.asyncio
async def test_ubus_download_rejects_non_base64() -> None:
    """ubus file.read data that is not valid base64 is not saved."""
    from custom_components.openwrt.api.ubus import UbusClient

    client = UbusClient(
        MagicMock(), MagicMock(), host="192.0.2.1", username="root", password="pwd"
    )
    with (
        patch.object(
            client,
            "_call",
            new_callable=AsyncMock,
            return_value={"data": GZIP_BYTES.decode("latin-1")},
        ),
        patch.object(client, "_write_bytes") as mock_write,
    ):
        assert await client.download_file("/tmp/b", "/tmp/l") is False
        mock_write.assert_not_called()
