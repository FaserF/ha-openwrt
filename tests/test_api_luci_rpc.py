"""Test the OpenWrt LuCI RPC API client."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.openwrt.api.luci_rpc import (
    LuciRpcAuthError,
    LuciRpcClient,
)


@pytest.fixture
def luci_client() -> LuciRpcClient:
    """Fixture for LuCI RPC client."""
    return LuciRpcClient(
        MagicMock(),
        MagicMock(),
        host="192.168.1.1",
        username="root",
        password="password",
    )


class MockResponse:
    def __init__(self, status, json_data, headers=None):
        self.status = status
        self._json_data = json_data
        self.headers = headers or {"Content-Type": "application/json"}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    def raise_for_status(self):
        pass

    async def json(self):
        return self._json_data


@pytest.mark.asyncio
async def test_luci_connect_success(luci_client: LuciRpcClient):
    """Test successful connection and login."""
    mock_post = MagicMock()
    luci_client.session.post = mock_post
    # LuCI returns the token as result directly
    mock_post.return_value = MockResponse(
        200,
        {"id": 1, "result": "luci_test_token"},
    )

    await luci_client.connect()

    assert luci_client.connected is True
    assert luci_client._auth_token == "luci_test_token"


@pytest.mark.asyncio
async def test_luci_connect_auth_error(luci_client: LuciRpcClient):
    """Test auth error handling."""
    mock_post = MagicMock()
    luci_client.session.post = mock_post
    mock_post.return_value = MockResponse(
        200,
        {"id": 1, "error": {"message": "Invalid credentials"}},
    )

    with pytest.raises(LuciRpcAuthError):
        await luci_client.connect()


@pytest.mark.asyncio
async def test_luci_get_device_info(luci_client: LuciRpcClient):
    """Test fetching device info."""
    luci_client._auth_token = "luci_test_token"
    with patch.object(luci_client, "_rpc_call", new_callable=AsyncMock) as mock_call:

        def call_side_effect(*args, **kwargs):
            method = args[1]
            if method == "hostname":
                return "LuCI-Router"
            if method == "exec":
                cmd = args[2][0]
                if "openwrt_release" in cmd:
                    return "DISTRIB_RELEASE='25.12'\nDISTRIB_REVISION='luci-r3'\nDISTRIB_ARCH='arm/v8'\nDISTRIB_TARGET='arm/v8'"
            return ""

        mock_call.side_effect = call_side_effect

        info = await luci_client.get_device_info()
        assert info.hostname == "LuCI-Router"
        assert info.release_version == "25.12"
        assert info.architecture == "arm/v8"


@pytest.mark.asyncio
async def test_luci_get_sqm_status(luci_client: LuciRpcClient):
    """Test fetching SQM status via LuCI RPC."""
    luci_client._auth_token = "luci_test_token"
    with patch.object(luci_client, "_rpc_call", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = {
            "eth0": {
                ".type": "queue",
                ".name": "eth0",
                "enabled": "1",
                "interface": "wan",
                "download": "100000",
                "upload": "50000",
                "qdisc": "fq_codel",
                "script": "simple.qos",
            },
        }

        status = await luci_client.get_sqm_status()
        assert len(status) == 1
        assert status[0].section_id == "eth0"
        assert status[0].enabled is True
        assert status[0].download == 100000


@pytest.mark.asyncio
async def test_luci_set_sqm_config(luci_client: LuciRpcClient):
    """Test setting SQM config via LuCI RPC."""
    luci_client._auth_token = "luci_test_token"
    with patch.object(luci_client, "_rpc_call", new_callable=AsyncMock) as mock_call:
        await luci_client.set_sqm_config("eth0", enabled=False, download=200000)

        # Check if calls were made
        assert mock_call.call_count >= 3

        # Check if enabled was set
        mock_call.assert_any_call("uci", "set", ["sqm", "eth0", "enabled", "0"])
        # Check if download was set
        mock_call.assert_any_call("uci", "set", ["sqm", "eth0", "download", "200000"])
        # Check commit
        mock_call.assert_any_call("uci", "commit", ["sqm"])


@pytest.mark.asyncio
async def test_luci_provision_user(luci_client: LuciRpcClient):
    """Test user provisioning via LuCI RPC."""
    luci_client._auth_token = "luci_test_token"
    with patch.object(
        luci_client,
        "execute_command",
        new_callable=AsyncMock,
    ) as mock_exec:
        mock_exec.return_value = "LOG: Provisioning SUCCESS"

        result = await luci_client.provision_user("homeassistant", "new-password")

        # provision_user returns (success: bool, error: str | None)
        success, error = result
        assert success is True
        assert error is None
        script = mock_exec.call_args[0][0]
        assert "USER='homeassistant'" in script
        assert "PASS='new-password'" in script
        assert '$UCI set rpcd."$SECTION"=login' in script
        assert '$UCI set rpcd."$SECTION".password="\\$p\\$$USER"' in script
        assert "/etc/init.d/rpcd restart" in script


@pytest.mark.asyncio
async def test_luci_check_permissions(luci_client: LuciRpcClient):
    """Test checking permissions via LuCI RPC."""
    luci_client._auth_token = "luci_test_token"
    with (
        patch.object(luci_client, "_rpc_call", new_callable=AsyncMock) as mock_call,
        patch.object(
            luci_client,
            "execute_command",
            new_callable=AsyncMock,
        ) as mock_exec,
    ):
        # Mock responses for UCI permission probes
        mock_call.return_value = {"values": {"something": "here"}}
        mock_exec.return_value = "ls\noutput"

        perms = await luci_client.check_permissions()
        assert perms.read_system is True
        assert perms.read_network is True
        assert perms.read_firewall is True
        assert perms.write_services is True


@pytest.mark.asyncio
async def test_luci_get_firewall_rules_anonymous(luci_client: LuciRpcClient):
    """Test fetching firewall rules with anonymous sections via LuCI RPC."""
    luci_client._auth_token = "luci_test_token"
    with patch.object(luci_client, "_rpc_call", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = {
            "cfg012345": {
                ".type": "rule",
                "name": "Allow-DHCP-Renew",
                "enabled": "1",
                "src": "wan",
                "dest": "lan",
                "target": "ACCEPT",
            },
            "cfg067890": {
                ".type": "rule",
                "enabled": "0",
                "src": "lan",
                "dest": "wan",
                "target": "REJECT",
            },
        }

        rules = await luci_client.get_firewall_rules()
        assert len(rules) == 2

        assert rules[0].section_id == "@rule[0]"
        assert rules[0].name == "Allow-DHCP-Renew"
        assert rules[0].enabled is True

        assert rules[1].section_id == "@rule[1]"
        assert rules[1].name == "@rule[1]"
        assert rules[1].enabled is False


@pytest.mark.asyncio
async def test_luci_get_connected_devices_fdb(luci_client: LuciRpcClient):
    """Test get_connected_devices parses FDB age properly in LuCI RPC."""
    luci_client._auth_token = "luci_test_token"
    luci_client._get_wireless_mapping = AsyncMock()
    luci_client._rpc_call = AsyncMock(return_value=None)
    luci_client.trust_bridge_fdb = True
    luci_client.trust_stale_arp = False

    with patch.object(
        luci_client, "execute_command", new_callable=AsyncMock
    ) as mock_exec:

        def exec_side_effect(command: str) -> str:
            if "cat /tmp/dhcp.leases" in command:
                return (
                    "1611234567 00:11:22:33:44:55 192.168.1.5 host1 *\n"
                    "1611234567 aa:bb:cc:dd:ee:ff 192.168.1.6 host2 *\n"
                )
            if "cat /proc/net/arp" in command:
                return ""
            if "ip neigh show" in command:
                return (
                    "192.168.1.5 dev br-lan lladdr 00:11:22:33:44:55 REACHABLE\n"
                    "192.168.1.6 dev br-lan lladdr aa:bb:cc:dd:ee:ff STALE\n"
                )
            if "ubus call network.device status" in command:
                return '{"br-lan": {"up": true}}'
            if "ubus call network.device fdb" in command:
                return (
                    "["
                    '  {"mac": "00:11:22:33:44:55", "port": "lan1", "age": 10},'
                    '  {"mac": "aa:bb:cc:dd:ee:ff", "port": "lan2", "age": 120}'
                    "]"
                )
            return ""

        mock_exec.side_effect = exec_side_effect

        devices = await luci_client.get_connected_devices()
        assert len(devices) == 2

        dev1 = next(d for d in devices if d.mac == "00:11:22:33:44:55")
        assert dev1.connected is True
        assert dev1.fdb_age == 10
        assert dev1.port == "lan1"

        dev2 = next(d for d in devices if d.mac == "aa:bb:cc:dd:ee:ff")
        assert dev2.connected is False
        assert dev2.fdb_age == 120
        assert dev2.port == "lan2"


@pytest.mark.asyncio
async def test_luci_kick_device(luci_client: LuciRpcClient):
    """Test that kick_device calls del_client via direct ubus call over LuCI RPC."""
    luci_client._auth_token = "luci_test_token"

    with patch.object(
        luci_client, "_rpc_call", new_callable=AsyncMock, return_value={}
    ) as mock_call:
        success = await luci_client.kick_device("00:11:22:33:44:55", "wlan0")
        assert success is True
        mock_call.assert_called_once_with(
            "ubus",
            "call",
            [
                "hostapd.wlan0",
                "del_client",
                {
                    "addr": "00:11:22:33:44:55",
                    "reason": 5,
                    "deauth": True,
                    "ban_time": 60000,
                },
            ],
        )


@pytest.mark.asyncio
async def test_luci_get_connected_devices_iwinfo_rates(
    luci_client: LuciRpcClient,
):
    """Test LuCI RPC client parses rates and noise correctly from JSON iwinfo and hostapd fallback."""
    luci_client._auth_token = "luci_test_token"
    luci_client._get_wireless_mapping = AsyncMock()
    luci_client._rpc_call = AsyncMock(return_value=None)
    luci_client.packages.wireless = True

    with patch.object(
        luci_client, "execute_command", new_callable=AsyncMock
    ) as mock_exec:

        def exec_side_effect(command: str) -> str:
            if "cat /proc/net/arp" in command:
                return ""
            if "iwinfo" in command:
                if "assoclist" in command:
                    return '{"results": [{"mac": "aa:bb:cc:dd:ee:ff", "signal": -50, "noise": -95, "rx": {"rate": 120100}, "tx": {"rate": 86600}}]}'
                return "wlan0"
            if "ubus list 'hostapd.*'" in command:
                return 'hostapd.wlan0 {"clients": {"aa:bb:cc:dd:ee:ff": {"signal": -50, "bytes": {"rx": 123, "tx": 456}, "rx_rate": 24020, "tx_rate": 18010}}}'
            return ""

        mock_exec.side_effect = exec_side_effect

        devices = await luci_client.get_connected_devices()
        assert len(devices) == 1
        dev = devices[0]
        assert dev.mac == "aa:bb:cc:dd:ee:ff"
        assert dev.is_wireless is True
        assert dev.signal == -50
        assert dev.noise == -95
        assert dev.rx_rate == 120100  # From iwinfo (precedence over hostapd fallback)
        assert dev.tx_rate == 86600
        assert dev.rx_bytes == 123  # From hostapd fallback
        assert dev.tx_bytes == 456


@pytest.mark.asyncio
async def test_luci_get_connected_devices_iwinfo_fallback_rates(
    luci_client: LuciRpcClient,
):
    """Test that fallback interface names from hostapd are queried via iwinfo assoclist if iwinfo CLI does not report them."""
    luci_client._auth_token = "luci_test_token"
    luci_client._get_wireless_mapping = AsyncMock()
    luci_client._rpc_call = AsyncMock(return_value=None)
    luci_client.packages.wireless = True

    with patch.object(
        luci_client, "execute_command", new_callable=AsyncMock
    ) as mock_exec:

        def exec_side_effect(command: str) -> str:
            if "cat /proc/net/arp" in command:
                return ""
            if "iwinfo 2>/dev/null" in command:
                return ""  # iwinfo CLI returns nothing
            if "ubus call iwinfo assoclist" in command and "phy0-ap0" in command:
                return '{"results": [{"mac": "11:22:33:44:55:66", "signal": -45, "noise": -90, "rx": {"rate": 240200}, "tx": {"rate": 180100}}]}'
            if "ubus list 'hostapd.*'" in command:
                return 'hostapd.phy0-ap0 {"clients": {"11:22:33:44:55:66": {"signal": -45, "bytes": {"rx": 1000, "tx": 2000}}}}'
            return ""

        mock_exec.side_effect = exec_side_effect

        devices = await luci_client.get_connected_devices()
        assert len(devices) == 1
        dev = devices[0]
        assert dev.mac == "11:22:33:44:55:66"
        assert dev.is_wireless is True
        assert dev.interface == "phy0-ap0"
        assert dev.rx_rate == 240200
        assert dev.tx_rate == 180100
        assert dev.rx_bytes == 1000
        assert dev.tx_bytes == 2000
