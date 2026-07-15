"""Test the OpenWrt Ubus API client."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.openwrt.api.ubus import UbusClient


@pytest.fixture
def ubus_client() -> UbusClient:
    """Fixture for Ubus client."""
    return UbusClient(
        MagicMock(),
        MagicMock(),
        host="192.168.1.1",
        username="ha-user",
        password="password",
    )


class MockResponse:
    def __init__(self, status, json_data, headers=None):
        self.status = status
        self._json_data = json_data
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    def raise_for_status(self):
        pass

    async def json(self):
        return self._json_data


@pytest.mark.asyncio
async def test_ubus_connect_success(ubus_client: UbusClient):
    """Test successful connection and login."""
    # No forced-HTTPS redirect: endpoint probe sees a plain 200, stays http.
    ubus_client.session.get = MagicMock(return_value=MockResponse(200, {}))
    mock_post = MagicMock()
    ubus_client.session.post = mock_post
    mock_post.return_value = MockResponse(
        200,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": [0, {"ubus_rpc_session": "test_token"}],
        },
    )

    await ubus_client.connect()

    assert ubus_client.connected is True
    assert ubus_client._session_id == "test_token"


@pytest.mark.asyncio
async def test_ubus_connect_auth_error(ubus_client: UbusClient):
    """Test auth error handling."""
    ubus_client.session.get = MagicMock(return_value=MockResponse(200, {}))
    mock_post = MagicMock()
    ubus_client.session.post = mock_post
    mock_post.return_value = MockResponse(
        200,
        {"jsonrpc": "2.0", "id": 1, "result": [5, {"message": "Access denied"}]},
    )

    from custom_components.openwrt.api.ubus import UbusAuthError

    with pytest.raises(UbusAuthError):
        await ubus_client.connect()


@pytest.mark.asyncio
async def test_ubus_connect_upgrades_on_https_redirect(ubus_client: UbusClient):
    """A forced-HTTPS redirect upgrades scheme/port before login (no cleartext)."""
    ubus_client.session.get = MagicMock(
        return_value=MockResponse(
            307, {}, {"Location": "https://192.168.1.1:8443/ubus"}
        )
    )
    mock_post = MagicMock(
        return_value=MockResponse(
            200,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": [0, {"ubus_rpc_session": "test_token"}],
            },
        )
    )
    ubus_client.session.post = mock_post

    await ubus_client.connect()

    assert ubus_client.use_ssl is True
    assert ubus_client.port == 8443
    assert ubus_client._base_url == "https://192.168.1.1:8443/ubus"
    # login POST went to the upgraded https URL, not the original http one
    assert mock_post.call_args.args[0] == "https://192.168.1.1:8443/ubus"


@pytest.mark.asyncio
async def test_ubus_upgrade_preserves_ipv6_brackets(ubus_client: UbusClient):
    """An IPv6 redirect target keeps its brackets so _base_url stays valid."""
    ubus_client.session.get = MagicMock(
        return_value=MockResponse(307, {}, {"Location": "https://[fd00::1]:8443/ubus"})
    )
    ubus_client.session.post = MagicMock(
        return_value=MockResponse(
            200,
            {"jsonrpc": "2.0", "id": 1, "result": [0, {"ubus_rpc_session": "t"}]},
        )
    )

    await ubus_client.connect()

    assert ubus_client.host == "[fd00::1]"
    assert ubus_client._base_url == "https://[fd00::1]:8443/ubus"


@pytest.mark.asyncio
async def test_ubus_probe_skipped_when_ssl_configured(ubus_client: UbusClient):
    """When the entry is already SSL, no redirect probe GET is issued."""
    ubus_client.use_ssl = True
    ubus_client.session.get = MagicMock()
    ubus_client.session.post = MagicMock(
        return_value=MockResponse(
            200,
            {"jsonrpc": "2.0", "id": 1, "result": [0, {"ubus_rpc_session": "t"}]},
        )
    )

    await ubus_client.connect()

    ubus_client.session.get.assert_not_called()


@pytest.mark.asyncio
async def test_ubus_get_device_info(ubus_client: UbusClient):
    """Test fetching device info."""
    ubus_client._session_id = "test_token"
    with patch.object(ubus_client, "_call", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = {
            "model": "Test Router",
            "release": {
                "distribution": "OpenWrt",
                "version": "25.12",
                "revision": "r1",
                "target": "test/target",
            },
        }

        info = await ubus_client.get_device_info()
        assert info.model == "Test Router"
        assert info.release_version == "25.12"
        assert info.architecture == ""


@pytest.mark.asyncio
async def test_ubus_get_sqm_status(ubus_client: UbusClient):
    """Test fetching SQM status via ubus."""
    ubus_client._session_id = "test_token"
    with patch.object(ubus_client, "_call", new_callable=AsyncMock) as mock_call:
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

        status = await ubus_client.get_sqm_status()
        assert len(status) == 1
        assert status[0].section_id == "eth0"
        assert status[0].enabled is True
        assert status[0].download == 100000


@pytest.mark.asyncio
async def test_ubus_set_sqm_config(ubus_client: UbusClient):
    """Test setting SQM config via ubus."""
    ubus_client._session_id = "test_token"
    with patch.object(ubus_client, "_call", new_callable=AsyncMock) as mock_call:
        await ubus_client.set_sqm_config("eth0", enabled=False, download=200000)

        # Should call uci set (at least twice) and commit
        assert mock_call.call_count >= 3

        # Check if enabled was set
        mock_call.assert_any_call(
            "uci",
            "set",
            {"config": "sqm", "section": "eth0", "values": {"enabled": "0"}},
        )
        # Check if download was set
        mock_call.assert_any_call(
            "uci",
            "set",
            {"config": "sqm", "section": "eth0", "values": {"download": "200000"}},
        )
        # Check commit
        mock_call.assert_any_call("uci", "commit", {"config": "sqm"})


@pytest.mark.asyncio
async def test_ubus_check_permissions(ubus_client: UbusClient):
    """Test checking permissions via ubus."""
    ubus_client._session_id = "test_token"
    from custom_components.openwrt.api.ubus import UbusPermissionError

    # Mock ubus 'session' list and 'uci' calls
    with (
        patch.object(ubus_client, "_call", new_callable=AsyncMock) as mock_call,
        patch.object(
            ubus_client,
            "execute_command",
            new_callable=AsyncMock,
        ) as mock_exec,
    ):

        def side_effect(obj, method, params=None):
            if obj == "session" and method == "list":
                # Return restricted permissions via session list
                return {"values": {"access": {"system": {"read": True, "write": True}}}}
            if obj == "uci" and method == "get":
                msg = "Access denied"
                raise UbusPermissionError(msg)
            return {}

        mock_call.side_effect = side_effect
        mock_exec.return_value = ""

        perms = await ubus_client.check_permissions()
        assert perms.read_system is True
        assert perms.write_system is True
        assert perms.read_network is False


@pytest.mark.asyncio
async def test_ubus_check_permissions_root(ubus_client: UbusClient):
    """Test checking permissions for root user."""
    ubus_client.username = "root"
    ubus_client._session_id = "test_token"

    with (
        patch.object(ubus_client, "_call", new_callable=AsyncMock) as mock_call,
        patch.object(
            ubus_client,
            "execute_command",
            new_callable=AsyncMock,
        ) as mock_exec,
    ):
        mock_call.return_value = {"values": {"access": {"*": {"*": True}}}}
        mock_exec.return_value = "exists"

        perms = await ubus_client.check_permissions()
        # Check a few key permissions that should be True for root
        assert perms.read_system is True
        assert perms.write_system is True
        assert perms.read_network is True
        assert perms.read_wireless is True
        assert perms.write_firewall is True


@pytest.mark.asyncio
async def test_ubus_provision_user(ubus_client: UbusClient):
    """Test user provisioning via ubus."""
    ubus_client._session_id = "test_token"
    with patch.object(
        ubus_client,
        "execute_command",
        new_callable=AsyncMock,
    ) as mock_exec:
        mock_exec.return_value = "LOG: Provisioning SUCCESS"

        result = await ubus_client.provision_user("homeassistant", "new-password")

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
async def test_ubus_get_firewall_rules_anonymous(ubus_client: UbusClient):
    """Test fetching firewall rules with anonymous sections."""
    ubus_client._session_id = "test_token"
    with patch.object(ubus_client, "_call", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = {
            "values": {
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
        }

        rules = await ubus_client.get_firewall_rules()
        assert len(rules) == 2

        assert rules[0].section_id == "@rule[0]"
        assert rules[0].name == "Allow-DHCP-Renew"
        assert rules[0].enabled is True

        assert rules[1].section_id == "@rule[1]"
        assert rules[1].name == "@rule[1]"
        assert rules[1].enabled is False


@pytest.mark.asyncio
async def test_ubus_get_connected_devices_wireless(ubus_client: UbusClient):
    """Test get_connected_devices parses iwinfo associations with interface and type in Ubus."""
    ubus_client._session_id = "test_token"
    ubus_client._connected = True
    ubus_client.packages.wireless = True
    ubus_client.trust_bridge_fdb = False
    ubus_client._list_objects = AsyncMock(return_value=["hostapd.wlan0"])

    with patch.object(ubus_client, "_call", new_callable=AsyncMock) as mock_call:

        def call_side_effect(object_name, method, *args, **kwargs):
            if object_name == "uci" and method == "get":
                return {"values": {}}
            if object_name == "network.wireless" and method == "status":
                return {
                    "radio0": {"interfaces": [{"ifname": "wlan0", "device": "wlan0"}]}
                }
            if object_name == "iwinfo" and method == "assoclist":
                return {
                    "results": [
                        {"mac": "00:11:22:33:44:55", "signal": -60, "noise": -90}
                    ]
                }
            if object_name.startswith("hostapd.") and method == "get_clients":
                return None
            return {}

        mock_call.side_effect = call_side_effect

        with (
            patch.object(
                ubus_client, "get_dhcp_leases", new_callable=AsyncMock
            ) as mock_dhcp,
            patch.object(
                ubus_client, "get_ip_neighbors", new_callable=AsyncMock
            ) as mock_neigh,
        ):
            mock_dhcp.return_value = []
            mock_neigh.return_value = []

            devices = await ubus_client.get_connected_devices()
            assert len(devices) == 1

            dev = devices[0]
            assert dev.mac == "00:11:22:33:44:55"
            assert dev.is_wireless is True
            assert dev.connected is True
            assert dev.interface == "wlan0"
            assert dev.connection_type == "wireless"
            assert dev.signal == -60
            assert dev.noise == -90


@pytest.mark.asyncio
async def test_ubus_get_wireless_interfaces_matching(ubus_client: UbusClient):
    """Test get_wireless_interfaces matches physical interfaces to UCI sections by SSID and band."""
    ubus_client._session_id = "test_token"
    ubus_client._connected = True
    ubus_client.packages.wireless = True

    with patch.object(ubus_client, "_call", new_callable=AsyncMock) as mock_call:

        def call_side_effect(object_name, method, params=None, *args, **kwargs):
            if object_name == "network.wireless" and method == "status":
                return {
                    "radio0": {
                        "config": {"band": "2g", "hwmode": "11g"},
                        "interfaces": [
                            {
                                "section": "default_radio0",
                                "ifname": "",
                                "config": {"ssid": "AP NYCR"},
                            }
                        ],
                    },
                    "radio1": {
                        "config": {"band": "5g", "hwmode": "11a"},
                        "interfaces": [
                            {
                                "section": "default_radio1",
                                "ifname": "",
                                "config": {"ssid": "AP NYCR"},
                            }
                        ],
                    },
                }
            if object_name == "iwinfo" and method == "devices":
                return ["wlan1", "wlan0"]
            if object_name == "iwinfo" and method == "info":
                device = params.get("device") if params else None
                if device == "wlan1":
                    return {
                        "ssid": "AP NYCR",
                        "frequency": 5180,
                        "bssid": "00:11:22:33:44:55",
                        "channel": 36,
                    }
                if device == "wlan0":
                    return {
                        "ssid": "AP NYCR",
                        "frequency": 2412,
                        "bssid": "00:11:22:33:44:66",
                        "channel": 1,
                    }
            return {}

        mock_call.side_effect = call_side_effect

        interfaces = await ubus_client.get_wireless_interfaces()
        assert len(interfaces) == 2

        wifi2g = next(w for w in interfaces if w.section == "default_radio0")
        assert wifi2g.name == "wlan0"
        assert wifi2g.ifname == "wlan0"
        assert wifi2g.band == "2.4 GHz"
        assert wifi2g.channel == 1

        wifi5g = next(w for w in interfaces if w.section == "default_radio1")
        assert wifi5g.name == "wlan1"
        assert wifi5g.ifname == "wlan1"
        assert wifi5g.band == "5 GHz"
        assert wifi5g.channel == 36


@pytest.mark.asyncio
async def test_ubus_get_connected_devices_from_wireless_interfaces(
    ubus_client: UbusClient,
):
    """Test that get_connected_devices uses get_wireless_interfaces to discover and poll interfaces."""
    ubus_client._session_id = "test_token"
    ubus_client._connected = True
    ubus_client.packages.wireless = True
    ubus_client.trust_bridge_fdb = False
    ubus_client._list_objects = AsyncMock(return_value=["hostapd.wlan0"])

    from custom_components.openwrt.api.base import WirelessInterface

    mock_ifaces = [
        WirelessInterface(name="wlan0", ssid="TestSSID", band="2.4 GHz"),
    ]

    with (
        patch.object(
            ubus_client,
            "get_wireless_interfaces",
            new_callable=AsyncMock,
            return_value=mock_ifaces,
        ),
        patch.object(ubus_client, "_call", new_callable=AsyncMock) as mock_call,
        patch.object(
            ubus_client, "get_dhcp_leases", new_callable=AsyncMock, return_value=[]
        ),
        patch.object(
            ubus_client, "get_ip_neighbors", new_callable=AsyncMock, return_value=[]
        ),
    ):

        def call_side_effect(object_name, method, params=None, *args, **kwargs):
            if object_name == "iwinfo" and method == "assoclist":
                device = params.get("device") if params else None
                if device == "wlan0":
                    return {
                        "results": [
                            {"mac": "AA:BB:CC:DD:EE:FF", "signal": -50, "noise": -95}
                        ]
                    }
            if object_name == "hostapd.wlan0" and method == "get_clients":
                return {
                    "clients": {
                        "AA:BB:CC:DD:EE:FF": {
                            "bytes": {"rx": 100, "tx": 200},
                            "rx_rate": 12010,
                            "tx_rate": 8660,
                        }
                    }
                }
            return {}

        mock_call.side_effect = call_side_effect

        devices = await ubus_client.get_connected_devices()
        assert len(devices) == 1
        dev = devices[0]
        assert dev.mac == "aa:bb:cc:dd:ee:ff"
        assert dev.is_wireless is True
        assert dev.interface == "wlan0"
        assert dev.rx_bytes == 100
        assert dev.tx_bytes == 200
        assert dev.rx_rate == 1201000
        assert dev.tx_rate == 866000


@pytest.mark.asyncio
async def test_ubus_kick_device(ubus_client: UbusClient):
    """Test that kick_device calls del_client on hostapd.<interface> via direct ubus call."""
    ubus_client._session_id = "test_token"
    ubus_client._connected = True

    with patch.object(
        ubus_client, "_call", new_callable=AsyncMock, return_value={}
    ) as mock_call:
        success = await ubus_client.kick_device("00:11:22:33:44:55", "wlan0")
        assert success is True
        mock_call.assert_called_once_with(
            "hostapd.wlan0",
            "del_client",
            {
                "addr": "00:11:22:33:44:55",
                "reason": 5,
                "deauth": True,
                "ban_time": 60000,
            },
        )


@pytest.mark.asyncio
async def test_ubus_get_ip_neighbors_filters_ipv6_link_local(ubus_client: UbusClient):
    """Test that get_ip_neighbors in Ubus filters out IPv6 link-local addresses."""
    ubus_client._session_id = "test_token"
    ubus_client._connected = True

    ubus_status_mock = {
        "br-lan": {
            "neighbors": [
                {
                    "address": "192.168.1.5",
                    "lladdr": "00:11:22:33:44:55",
                    "state": "REACHABLE",
                },
                {"address": "fe80::1", "lladdr": "aa:bb:cc:dd:ee:ff", "state": "STALE"},
                {
                    "address": "2001:db8::1",
                    "lladdr": "00:11:22:33:44:56",
                    "state": "REACHABLE",
                },
            ]
        }
    }

    ip_neigh_mock_output = (
        "192.168.1.5 dev br-lan lladdr 00:11:22:33:44:55 REACHABLE\n"
        "2001:db8::1 dev br-lan lladdr 00:11:22:33:44:56 REACHABLE\n"
        "fe80::1 dev br-lan lladdr aa:bb:cc:dd:ee:ff STALE\n"
    )

    with (
        patch.object(
            ubus_client, "_call", new_callable=AsyncMock, return_value=ubus_status_mock
        ),
        patch.object(
            ubus_client,
            "execute_command",
            new_callable=AsyncMock,
            return_value=ip_neigh_mock_output,
        ),
    ):
        neighbors = await ubus_client.get_ip_neighbors()

        assert len(neighbors) == 2
        ips = {n.ip for n in neighbors}
        assert "192.168.1.5" in ips
        assert "2001:db8::1" in ips
        assert "fe80::1" not in ips
