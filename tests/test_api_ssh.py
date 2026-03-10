"""Test the OpenWrt SSH API client."""

from unittest.mock import AsyncMock, patch

import pytest

from custom_components.openwrt.api.ssh import SshAuthError, SshClient


@pytest.fixture
def ssh_client() -> SshClient:
    """Fixture for SSH client."""
    return SshClient(host="192.168.1.1", username="root", password="password")


@pytest.mark.asyncio
async def test_ssh_connect_success(ssh_client: SshClient):
    """Test successful SSH connection."""
    with patch("paramiko.SSHClient") as mock_ssh:
        await ssh_client.connect()

        assert ssh_client.connected is True
        mock_ssh.return_value.connect.assert_called_once()


@pytest.mark.asyncio
async def test_ssh_connect_auth_error(ssh_client: SshClient):
    """Test SSH auth error."""
    import paramiko

    with patch("paramiko.SSHClient") as mock_ssh:
        mock_ssh.return_value.connect.side_effect = paramiko.AuthenticationException(
            "Auth Failed"
        )

        with pytest.raises(SshAuthError):
            await ssh_client.connect()


@pytest.mark.asyncio
async def test_ssh_get_device_info(ssh_client: SshClient):
    """Test fetching device info via SSH."""
    ssh_client._connected = True
    with patch.object(ssh_client, "_exec", new_callable=AsyncMock) as mock_exec:
        # Mock responses for the multiple cat commands in get_device_info
        def exec_side_effect(command: str) -> str:
            if "board.json" in command:
                return '{"model": "SSH Router", "release": {"target": "x86"}}'
            if "hostname" in command:
                return "OpenWrt"
            if "openwrt_release" in command:
                return "DISTRIB_RELEASE='25.12'\nDISTRIB_REVISION='r2'"
            return ""

        mock_exec.side_effect = exec_side_effect

        info = await ssh_client.get_device_info()
        assert info.model == "SSH Router"
        assert info.release_version == "25.12"
        assert info.release_revision == "r2"
        assert info.hostname == "OpenWrt"
