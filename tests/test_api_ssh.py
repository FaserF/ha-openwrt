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


@pytest.mark.asyncio
async def test_ssh_get_connected_devices_iwinfo_fallback(ssh_client: SshClient):
    """Test SSH client fallback to ubus hostapd for wifi clients when iwinfo fails."""
    ssh_client._connected = True
    with patch.object(ssh_client, "_exec", new_callable=AsyncMock) as mock_exec:
        def exec_side_effect(command: str) -> str:
            if "cat /proc/net/arp" in command:
                return "IP address       HW type     Flags       HW address            Mask     Device\n192.168.1.5      0x1         0x2         00:11:22:33:44:55     *        br-lan"
            if "iwinfo" in command:
                if "assoclist" in command:
                    return "No information"
                return "wlan0"
            if "ubus list 'hostapd.*'" in command:
                return 'hostapd.wlan0 {"clients": {"aa:bb:cc:dd:ee:ff": {"signal": -50}}}'
            return ""

        mock_exec.side_effect = exec_side_effect

        devices = await ssh_client.get_connected_devices()
        assert len(devices) == 2

        # ARP device
        dev1 = next(d for d in devices if d.mac == "00:11:22:33:44:55")
        assert dev1.ip == "192.168.1.5"

        # Ubus fallback device
        dev2 = next(d for d in devices if d.mac == "aa:bb:cc:dd:ee:ff")
        assert dev2.is_wireless is True
        assert dev2.signal == -50


@pytest.mark.asyncio
async def test_ssh_get_temperature_fallback(ssh_client: SshClient):
    """Test SSH client fallback for temperature within system resources."""
    ssh_client._connected = True
    with patch.object(ssh_client, "_exec", new_callable=AsyncMock) as mock_exec:
        def exec_side_effect(command: str) -> str:
            if "thermal_zone0" in command:
                return "45000" if "temp" in command else "cpu-thermal"
            if "loadavg" in command:
                return "0.0 0.0 0.0 1/100 1234"
            if "uptime" in command:
                return "100.0"
            return ""

        mock_exec.side_effect = exec_side_effect

        resources = await ssh_client.get_system_resources()
        assert resources.temperature == 45.0
