"""Test the OpenWrt Ubus API client."""

from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from custom_components.openwrt.api.ubus import UbusClient, UbusError


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
@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ([{"system": {}, "uci": {}}], ["system", "uci"]),
        ({"system": {}, "uci": {}}, ["system", "uci"]),
    ],
)
async def test_ubus_list_objects_response_formats(
    ubus_client: UbusClient, result, expected
):
    """Test standard and direct object-map list responses."""
    ubus_client._connected = True
    ubus_client.session.post = MagicMock(
        return_value=MockResponse(200, {"result": result})
    )

    assert await ubus_client._list_objects() == expected


@pytest.mark.asyncio
async def test_ubus_list_objects_literal_wildcard(ubus_client: UbusClient):
    """Test probing core objects when a proxy treats the wildcard literally."""
    ubus_client._connected = True
    ubus_client.session.post = MagicMock(
        return_value=MockResponse(200, {"result": {"*": {}}})
    )
    ubus_client._get_object_methods = AsyncMock(
        side_effect=lambda name: {"methods": {}} if name in {"system", "uci"} else {}
    )

    assert await ubus_client._list_objects() == ["system", "uci"]


@pytest.mark.asyncio
async def test_ubus_get_object_methods_direct_response(ubus_client: UbusClient):
    """Test method discovery through a direct object-map response."""
    ubus_client.session.post = MagicMock(
        return_value=MockResponse(
            200, {"result": {"uci": {"get": {"config": "String"}}}}
        )
    )

    assert await ubus_client._get_object_methods("uci") == {"get": {"config": "String"}}


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
                        "disabled": True,
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
                        "txpower": 23,
                    }
                if device == "wlan0":
                    return {
                        "ssid": "AP NYCR",
                        "frequency": 2412,
                        "bssid": "00:11:22:33:44:66",
                        "channel": 1,
                        "txpower": 20,
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
        assert wifi2g.txpower == 20
        assert wifi2g.radio_enabled is True

        wifi5g = next(w for w in interfaces if w.section == "default_radio1")
        assert wifi5g.name == "wlan1"
        assert wifi5g.ifname == "wlan1"
        assert wifi5g.band == "5 GHz"
        assert wifi5g.channel == 36
        assert wifi5g.txpower == 23
        assert wifi5g.radio_enabled is False


@pytest.mark.asyncio
async def test_ubus_rejects_invalid_reported_txpower(ubus_client: UbusClient) -> None:
    """Keep configured TX power when iwinfo reports an invalid zero value."""
    ubus_client.packages.wireless = True

    def call_side_effect(object_name, method, params=None, *args, **kwargs):
        if object_name == "network.wireless" and method == "status":
            return {
                "radio0": {
                    "config": {"band": "2g", "txpower": 17},
                    "interfaces": [
                        {
                            "section": "main",
                            "ifname": "wlan0",
                            "config": {"ssid": "Main"},
                        }
                    ],
                }
            }
        if object_name == "uci" and method == "get":
            return {}
        if object_name == "iwinfo" and method == "devices":
            return ["wlan0"]
        if object_name == "iwinfo" and method == "info":
            return {"ssid": "Main", "txpower": 0}
        return {}

    with patch.object(
        ubus_client,
        "_call",
        new_callable=AsyncMock,
        side_effect=call_side_effect,
    ):
        interfaces = await ubus_client.get_wireless_interfaces()

    assert interfaces[0].txpower == 17


@pytest.mark.asyncio
async def test_ubus_wireless_skips_radio_info(ubus_client: UbusClient):
    """Test that physical radio names are not queried as iwinfo interfaces."""
    ubus_client.packages.wireless = True

    with patch.object(ubus_client, "_call", new_callable=AsyncMock) as mock_call:

        def call_side_effect(object_name, method, params=None, *args, **kwargs):
            if object_name == "network.wireless" and method == "status":
                return {
                    "radio0": {
                        "config": {"band": "2g"},
                        "interfaces": [
                            {
                                "section": "default_radio0",
                                "ifname": "wlan0",
                                "config": {"ssid": "Test"},
                            }
                        ],
                    }
                }
            if object_name == "iwinfo" and method == "devices":
                return ["radio0"]
            if object_name == "iwinfo" and method == "info":
                assert params == {"device": "wlan0"}
                return {"channel": 1}
            return {}

        mock_call.side_effect = call_side_effect

        interfaces = await ubus_client.get_wireless_interfaces()

    assert len(interfaces) == 1
    assert interfaces[0].channel == 1


@pytest.mark.asyncio
async def test_ubus_wireless_skips_info_for_luci_admin_proxy(
    ubus_client: UbusClient,
):
    """Test avoiding iwinfo info calls that hang GL.iNet's LuCI proxy."""
    ubus_client.packages.wireless = True
    ubus_client._ubus_path = "/cgi-bin/luci/admin/ubus"

    with patch.object(ubus_client, "_call", new_callable=AsyncMock) as mock_call:

        def call_side_effect(object_name, method, *args, **kwargs):
            if object_name == "network.wireless" and method == "status":
                return {
                    "wifi0": {
                        "config": {"band": "2g", "hwmode": "11beg"},
                        "interfaces": [
                            {
                                "section": "iot2g",
                                "ifname": "wlan06",
                                "config": {"ssid": "Test"},
                            }
                        ],
                    }
                }
            if object_name == "iwinfo" and method == "devices":
                return ["wifi0", "wlan06"]
            if object_name == "iwinfo" and method == "info":
                pytest.fail("iwinfo info must not be called through this proxy")
            if object_name == "iwinfo" and method == "assoclist":
                return {"results": [{"mac": "00:11:22:33:44:55"}]}
            return {}

        mock_call.side_effect = call_side_effect

        interfaces = await ubus_client.get_wireless_interfaces()

    assert len(interfaces) == 1
    assert interfaces[0].name == "wlan06"
    assert interfaces[0].clients_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("enabled", "disable_radio", "radio_state"),
    [(True, False, "0"), (False, True, "1")],
)
async def test_ubus_coordinates_ssid_and_radio_in_one_transaction(
    ubus_client: UbusClient,
    enabled: bool,
    disable_radio: bool,
    radio_state: str,
) -> None:
    """Commit the SSID and required radio state together."""
    with patch.object(ubus_client, "_call", new_callable=AsyncMock) as mock_call:
        result = await ubus_client.set_wireless_network_enabled(
            "main",
            "radio0",
            enabled,
            disable_radio=disable_radio,
        )

    assert result is True
    assert mock_call.await_args_list[-2:] == [
        call("uci", "commit", {"config": "wireless"}),
        call("network.wireless", "notify"),
    ]
    assert (
        call(
            "uci",
            "set",
            {
                "config": "wireless",
                "section": "main",
                "values": {"disabled": "0" if enabled else "1"},
            },
        )
        in mock_call.await_args_list
    )
    assert (
        call(
            "uci",
            "set",
            {
                "config": "wireless",
                "section": "radio0",
                "values": {"disabled": radio_state},
            },
        )
        in mock_call.await_args_list
    )


@pytest.mark.asyncio
async def test_ubus_reverts_partial_wireless_transaction(
    ubus_client: UbusClient,
) -> None:
    """Discard staged UCI changes when a pre-commit operation fails."""
    calls = []

    async def call_side_effect(object_name, method, params=None, *args, **kwargs):
        calls.append(call(object_name, method, params))
        if object_name == "uci" and method == "set" and params["section"] == "main":
            raise UbusError("set failed")
        return {}

    with patch.object(
        ubus_client,
        "_call",
        new_callable=AsyncMock,
        side_effect=call_side_effect,
    ):
        result = await ubus_client.set_wireless_network_enabled(
            "main",
            "radio0",
            True,
            disable_radio=False,
        )

    assert result is False
    assert call("uci", "revert", {"config": "wireless"}) in calls
    assert call("uci", "commit", {"config": "wireless"}) not in calls


@pytest.mark.asyncio
async def test_ubus_does_not_revert_after_successful_commit(
    ubus_client: UbusClient,
) -> None:
    """Do not undo committed state when only the wireless notification fails."""
    calls = []

    async def call_side_effect(object_name, method, params=None, *args, **kwargs):
        calls.append(call(object_name, method, params))
        if object_name == "network.wireless" and method == "notify":
            raise UbusError("notify failed")
        return {}

    with patch.object(
        ubus_client,
        "_call",
        new_callable=AsyncMock,
        side_effect=call_side_effect,
    ):
        result = await ubus_client.set_wireless_network_enabled(
            "main",
            "radio0",
            False,
            disable_radio=True,
        )

    assert result is False
    assert call("uci", "commit", {"config": "wireless"}) in calls
    assert call("uci", "revert", {"config": "wireless"}) not in calls


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


@pytest.mark.asyncio
async def test_ubus_call_session_expiry_jsonrpc_32002(ubus_client: UbusClient):
    """Test session expiry handling when ubus returns a JSON-RPC -32002 error."""
    from custom_components.openwrt.api.ubus import UbusPermissionError

    ubus_client._session_id = "expired_token"
    ubus_client.session = MagicMock()

    expired_resp = MockResponse(
        200,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "error": {"code": -32002, "message": "Access denied"},
        },
    )
    success_resp = MockResponse(
        200,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "result": [0, {"status": "ok"}],
        },
    )

    ubus_client.session.post = MagicMock(side_effect=[expired_resp, success_resp])

    with patch.object(ubus_client, "connect", new_callable=AsyncMock) as mock_connect:

        def connect_side_effect():
            ubus_client._session_id = "new_token"

        mock_connect.side_effect = connect_side_effect

        result = await ubus_client._call("system", "board")
        assert result == {"status": "ok"}
        assert mock_connect.call_count == 1

    # Now test when second call after connect also fails with -32002
    ubus_client._session_id = "expired_token"
    ubus_client.session.post = MagicMock(side_effect=[expired_resp, expired_resp])

    with (
        patch.object(ubus_client, "connect", new_callable=AsyncMock) as mock_connect,
        pytest.raises(UbusPermissionError) as exc_info,
    ):
        await ubus_client._call("system", "board")

    assert "Access denied to ubus" in str(exc_info.value)


@pytest.mark.asyncio
async def test_set_firewall_rule_enabled_propagates_ubus_error(ubus_client: UbusClient):
    """Test set_firewall_rule_enabled propagates UbusError when _call fails."""
    from custom_components.openwrt.api.ubus import UbusError

    with patch.object(
        ubus_client, "_call", new_callable=AsyncMock, side_effect=UbusError("UCI error")
    ):
        with pytest.raises(UbusError):
            await ubus_client.set_firewall_rule_enabled("rule_1", True)


@pytest.mark.asyncio
async def test_set_firewall_rule_enabled_reload_failure(ubus_client: UbusClient):
    """Test set_firewall_rule_enabled raises UbusError when firewall reload fails."""
    from custom_components.openwrt.api.ubus import UbusError

    with (
        patch.object(ubus_client, "_call", new_callable=AsyncMock, return_value={}),
        patch.object(
            ubus_client,
            "execute_command",
            new_callable=AsyncMock,
            return_value="RC=1",
        ),
        pytest.raises(UbusError) as exc_info,
    ):
        await ubus_client.set_firewall_rule_enabled("rule_1", True)

    assert "firewall reload failed" in str(exc_info.value)


@pytest.mark.asyncio
async def test_ubus_wps_status_and_control(ubus_client: UbusClient):
    """Test get_wps_status, set_wps, and trigger_wps_push via ubus."""
    ubus_client.packages.wireless = True

    # 1. Test get_wps_status via network.wireless status
    with patch.object(ubus_client, "_call", new_callable=AsyncMock) as mock_call:
        mock_call.side_effect = lambda obj, method, *args, **kwargs: {
            ("network.wireless", "status"): {
                "radio0": {"interfaces": [{"ifname": "wlan0"}]}
            },
            ("hostapd.wlan0", "wps_status"): {"pbc_status": "Active"},
        }.get((obj, method), {})

        status = await ubus_client.get_wps_status()
        assert status.enabled is True
        assert status.status == "Active"

    # 2. Test fallback to _list_objects when network.wireless fails
    from custom_components.openwrt.api.ubus import UbusError

    with (
        patch.object(ubus_client, "_call", new_callable=AsyncMock) as mock_call,
        patch.object(
            ubus_client,
            "_list_objects",
            new_callable=AsyncMock,
            return_value=["hostapd.wlan0"],
        ),
    ):

        def fallback_call(obj, method, *args, **kwargs):
            if obj == "network.wireless":
                raise UbusError("Not accepted")
            if obj == "hostapd.wlan0" and method == "wps_status":
                return {"pbc_status": "Active"}
            if obj == "hostapd.wlan0" and method == "wps_start":
                return {}
            return {}

        mock_call.side_effect = fallback_call

        status = await ubus_client.get_wps_status()
        assert status.enabled is True
        assert status.status == "Active"

        res = await ubus_client.set_wps(True)
        assert res is True

    # 3. Test trigger_wps_push tries wps_start first, then wps_push
    with patch.object(ubus_client, "_call", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = {}
        res = await ubus_client.trigger_wps_push("wlan0")
        assert res is True
        mock_call.assert_called_with("hostapd.wlan0", "wps_start")


@pytest.mark.asyncio
async def test_ubus_set_wireless_network_enabled_resolves_wifinet_section(
    ubus_client: UbusClient,
) -> None:
    """Test that set_wireless_network_enabled resolves wifinet3 to the correct UCI section."""
    uci_data = {
        "values": {
            "radio0": {".type": "wifi-device"},
            "radio1": {".type": "wifi-device"},
            "default_radio0": {
                ".type": "wifi-iface",
                "device": "radio0",
                "mode": "ap",
                "ssid": "SSID0",
            },
            "cfg02e1b1": {
                ".type": "wifi-iface",
                "device": "radio0",
                "mode": "ap",
                "ssid": "SSID1",
            },
            "cfg03e1b2": {
                ".type": "wifi-iface",
                "device": "radio1",
                "mode": "ap",
                "ssid": "SSID2",
            },
            "cfg04e1b3": {
                ".type": "wifi-iface",
                "device": "radio1",
                "mode": "ap",
                "ssid": "SSID3",
            },
        }
    }

    recorded_calls = []

    async def mock_call(obj, method, params=None):
        recorded_calls.append((obj, method, params))
        if obj == "uci" and method == "get":
            return uci_data
        return {}

    with patch.object(ubus_client, "_call", side_effect=mock_call):
        # wifinet3 is the 4th wifi-iface section (index 3) -> cfg04e1b3
        success = await ubus_client.set_wireless_network_enabled(
            "wifinet3", "radio1", True, disable_radio=False
        )
        assert success is True

        # Verify uci set was called for cfg04e1b3, not wifinet3
        set_calls = [
            params
            for obj, method, params in recorded_calls
            if obj == "uci" and method == "set"
        ]
        assert any(
            c.get("section") == "cfg04e1b3" and c.get("values") == {"disabled": "0"}
            for c in set_calls
        )
        assert not any(c.get("section") == "wifinet3" for c in set_calls)
