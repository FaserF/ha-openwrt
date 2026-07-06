"""Test the OpenWrt SSH API client."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.openwrt.api.ssh import SshAuthError, SshClient


@pytest.fixture
def ssh_client() -> SshClient:
    """Fixture for SSH client."""
    return SshClient(
        MagicMock(),
        MagicMock(),
        host="192.168.1.1",
        username="root",
        password="password",
    )


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
            "Auth Failed",
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
                return 'hostapd.wlan0 {"clients": {"aa:bb:cc:dd:ee:ff": {"signal": -50, "bytes": {"rx": 123, "tx": 456}, "rx_rate": 24020, "tx_rate": 18010}}}'
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
        assert dev2.rx_bytes == 123
        assert dev2.tx_bytes == 456
        assert dev2.rx_rate == 2402000
        assert dev2.tx_rate == 1801000


@pytest.mark.asyncio
async def test_ssh_get_temperature_fallback(ssh_client: SshClient):
    """Test SSH client fallback for temperature within system resources."""
    ssh_client._connected = True
    with patch.object(ssh_client, "_exec", new_callable=AsyncMock) as mock_exec:

        def exec_side_effect(command: str) -> str:
            if "ls -d /sys/class/thermal/thermal_zone*" in command:
                return "/sys/class/thermal/thermal_zone0"
            if "thermal_zone0/temp" in command:
                return "45000"
            if "loadavg" in command:
                return "0.0 0.0 0.0 1/100 1234"
            if "uptime" in command:
                return "100.0"
            return ""

        mock_exec.side_effect = exec_side_effect

        resources = await ssh_client.get_system_resources()
        assert resources.temperature == 45.0


@pytest.mark.asyncio
async def test_ssh_get_sqm_status(ssh_client: SshClient):
    """Test fetching SQM status via SSH."""
    ssh_client._connected = True
    with patch.object(ssh_client, "_exec", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = (
            "sqm.eth0=queue\n"
            "sqm.eth0.enabled='1'\n"
            "sqm.eth0.interface='wan'\n"
            "sqm.eth0.download='100000'\n"
            "sqm.eth0.upload='50000'\n"
            "sqm.eth0.qdisc='fq_codel'\n"
            "sqm.eth0.script='simple.qos'\n"
        )

        status = await ssh_client.get_sqm_status()
        assert len(status) == 1
        assert status[0].section_id == "eth0"
        assert status[0].enabled is True
        assert status[0].download == 100000


@pytest.mark.asyncio
async def test_ssh_set_sqm_config(ssh_client: SshClient):
    """Test setting SQM config via SSH."""
    ssh_client._connected = True
    with patch.object(ssh_client, "_exec", new_callable=AsyncMock) as mock_exec:
        await ssh_client.set_sqm_config("eth0", enabled=False, download=200000)

        # Check if commands were executed
        calls = [c.args[0] for c in mock_exec.call_args_list]
        assert "uci set sqm.eth0.enabled=0" in calls
        assert "uci set sqm.eth0.download=200000" in calls
        assert "uci commit sqm" in calls
        assert "/etc/init.d/sqm reload" in calls


@pytest.mark.asyncio
async def test_ssh_provision_user(ssh_client: SshClient):
    """Test user provisioning via SSH."""
    ssh_client._connected = True
    with patch.object(ssh_client, "_exec", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = "LOG: Provisioning SUCCESS"

        result = await ssh_client.provision_user("homeassistant", "new-password")

        # provision_user returns (success: bool, error: str | None)
        success, error = result
        assert success is True
        assert error is None
        script = mock_exec.call_args[0][0]
        assert "USER='homeassistant'" in script
        assert "PASS='new-password'" in script
        assert '$UCI set rpcd."$SECTION"=login' in script
        assert '$UCI set rpcd."$SECTION".password="\\$p\\$$USER"' in script
        assert '$UCI add_list rpcd."$SECTION".read="homeassistant"' in script
        assert "chpasswd" in script
        assert "passwd" in script
        assert "/etc/init.d/rpcd restart" in script


@pytest.mark.asyncio
async def test_ssh_get_connected_devices_iwinfo_rates(ssh_client: SshClient):
    """Test SSH client parses rates and noise correctly from iwinfo assoclist JSON."""
    ssh_client._connected = True
    ssh_client.packages.wireless = True
    with patch.object(ssh_client, "_exec", new_callable=AsyncMock) as mock_exec:

        def exec_side_effect(command: str) -> str:
            if "cat /proc/net/arp" in command:
                return ""
            if "network.wireless status" in command:
                return '{"radio0": {"interfaces": [{"ifname": "wlan0"}]}}'
            if "iwinfo" in command:
                if "assoclist" in command:
                    return '{"results": [{"mac": "aa:bb:cc:dd:ee:ff", "signal": -50, "noise": -95, "rx": {"rate": 120100}, "tx": {"rate": 86600}}]}'
                return "wlan0"
            if "ubus list 'hostapd.*'" in command:
                return ""
            return ""

        mock_exec.side_effect = exec_side_effect

        devices = await ssh_client.get_connected_devices()
        assert len(devices) == 1
        dev = devices[0]
        assert dev.mac == "aa:bb:cc:dd:ee:ff"
        assert dev.is_wireless is True
        assert dev.signal == -50
        assert dev.noise == -95
        assert dev.rx_rate == 120100
        assert dev.tx_rate == 86600


@pytest.mark.asyncio
async def test_ssh_get_ip_neighbors_filters_ipv6_link_local(ssh_client: SshClient):
    """Test that get_ip_neighbors parses IPv4 and IPv6 global addresses but filters IPv6 link-local."""
    ssh_client._connected = True
    with patch.object(ssh_client, "_exec", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = (
            "192.168.1.5 dev br-lan lladdr 00:11:22:33:44:55 REACHABLE\n"
            "2001:db8::1 dev br-lan lladdr 00:11:22:33:44:56 REACHABLE\n"
            "fe80::1 dev br-lan lladdr aa:bb:cc:dd:ee:ff STALE\n"
        )
        neighbors = await ssh_client.get_ip_neighbors()

        # Should only contain the IPv4 and IPv6 global address, not the link-local fe80::1
        assert len(neighbors) == 2

        ips = [n.ip for n in neighbors]
        assert "192.168.1.5" in ips
        assert "2001:db8::1" in ips
        assert "fe80::1" not in ips


@pytest.mark.asyncio
async def test_ssh_get_connected_devices_iwinfo_fallback_rates(ssh_client: SshClient):
    """Test that fallback interface names from hostapd are queried via iwinfo assoclist if ubus call iwinfo devices is empty."""
    ssh_client._connected = True
    ssh_client.packages.wireless = True
    with patch.object(ssh_client, "_exec", new_callable=AsyncMock) as mock_exec:

        def exec_side_effect(command: str) -> str:
            if "cat /proc/net/arp" in command:
                return ""
            if "ubus call iwinfo devices" in command:
                return '{"devices": []}'  # empty devices list
            if "ubus list 'hostapd.*'" in command:
                return "hostapd.phy0-ap0"
            if "ubus call iwinfo assoclist" in command and "phy0-ap0" in command:
                return '{"results": [{"mac": "11:22:33:44:55:66", "signal": -45, "noise": -90, "rx": {"rate": 240200}, "tx": {"rate": 180100}}]}'
            if "hostapd.phy0-ap0" in command and "get_clients" in command:
                return '{"clients": {"11:22:33:44:55:66": {"signal": -45, "bytes": {"rx": 1000, "tx": 2000}}}}'
            return ""

        mock_exec.side_effect = exec_side_effect

        devices = await ssh_client.get_connected_devices()
        assert len(devices) == 1
        dev = devices[0]
        assert dev.mac == "11:22:33:44:55:66"
        assert dev.is_wireless is True
        assert dev.interface == "phy0-ap0"
        assert dev.rx_rate == 240200
        assert dev.tx_rate == 180100
