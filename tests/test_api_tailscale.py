"""Tests for Tailscale status parsing, polling and package detection."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.openwrt.api.base import (
    OpenWrtData,
    OpenWrtPackages,
    OpenWrtPermissions,
    TailscaleStatus,
)
from custom_components.openwrt.api.luci_rpc import LuciRpcClient
from custom_components.openwrt.api.ssh import SshClient
from custom_components.openwrt.api.tailscale import (
    RC_MARKER,
    STATE_OPTIONS,
    TAILSCALE_STATUS_COMMAND,
    normalize_backend_state,
    parse_tailscale_output,
)
from custom_components.openwrt.api.ubus import UbusClient

HOST = "192.0.2.1"
AUTH_URL = "https://login.example.invalid/a/0123456789abcdef"
LOGIN_NAME = "someone@example.invalid"


def make_status(**overrides: Any) -> dict[str, Any]:
    """Return an anonymized `tailscale status --json` document."""
    doc: dict[str, Any] = {
        "Version": "1.98.3-1 (OpenWrt)",
        "TUN": True,
        "BackendState": "Running",
        "HaveNodeKey": True,
        "AuthURL": "",
        "TailscaleIPs": ["100.64.0.1", "fd7a:115c:a1e0::1"],
        "Self": {
            "ID": "nSelf0001CNTRL",
            "PublicKey": "nodekey:00000000000000000000000000000000",
            "HostName": "router",
            "DNSName": "router.example.ts.net.",
            "OS": "linux",
            "TailscaleIPs": ["100.64.0.1", "fd7a:115c:a1e0::1"],
            "PrimaryRoutes": ["198.51.100.0/24"],
            "Addrs": ["203.0.113.10:41641"],
            "CurAddr": "",
            "Relay": "waw",
            "RxBytes": 0,
            "TxBytes": 0,
            "Online": True,
            "Active": False,
            "LastSeen": "0001-01-01T00:00:00Z",
            "KeyExpiry": "2027-02-19T15:49:27.123456789Z",
        },
        "Health": [],
        "MagicDNSSuffix": "example.ts.net",
        "CurrentTailnet": {
            "Name": LOGIN_NAME,
            "MagicDNSSuffix": "example.ts.net",
            "MagicDNSEnabled": True,
        },
        "Peer": {
            "nodekey:aaaa": {
                "ID": "nPeerA0001CNTRL",
                "HostName": "peer-a",
                "DNSName": "peer-a.example.ts.net.",
                "OS": "windows",
                "TailscaleIPs": ["100.64.0.2"],
                "CurAddr": "203.0.113.20:41641",
                "Relay": "fra",
                "RxBytes": 1234,
                "TxBytes": 5678,
                "Online": True,
                "Active": True,
                "LastSeen": "0001-01-01T00:00:00Z",
            },
            "nodekey:bbbb": {
                "ID": "nPeerB0001CNTRL",
                "HostName": "peer-b",
                "OS": "linux",
                "TailscaleIPs": ["100.64.0.3"],
                "CurAddr": "",
                "Relay": "fra",
                "Online": True,
                "Active": True,
                "ExitNodeOption": True,
                "LastSeen": "0001-01-01T00:00:00Z",
            },
            "nodekey:cccc": {
                "ID": "nPeerC0001CNTRL",
                "HostName": "peer-c",
                "OS": "android",
                "TailscaleIPs": ["100.64.0.4"],
                "CurAddr": "",
                "Online": False,
                "Active": False,
                "LastSeen": "2026-09-30T16:54:24.1Z",
                "KeyExpiry": "2027-03-18T10:45:43Z",
            },
        },
        "User": {"1": {"LoginName": LOGIN_NAME, "ProfilePicURL": "https://x"}},
    }
    doc.update(overrides)
    return doc


def wrap(body: str, rc: int = 0, counters: str = "12345\n678\n") -> str:
    """Wrap output the way TAILSCALE_STATUS_COMMAND produces it."""
    return f"{body}\n{RC_MARKER}{rc}\n{counters}"


def test_command_contract() -> None:
    """The command echoes the exit code and reads tailscale0 counters."""
    assert "tailscale status --json" in TAILSCALE_STATUS_COMMAND
    assert f"{RC_MARKER}$?" in TAILSCALE_STATUS_COMMAND
    assert "/sys/class/net/tailscale0/statistics/rx_bytes" in TAILSCALE_STATUS_COMMAND
    # LuCI-RPC wraps the command in single quotes.
    assert "'" not in TAILSCALE_STATUS_COMMAND


def test_parse_running_status() -> None:
    """A running daemon with peers is parsed into the dataclasses."""
    status = parse_tailscale_output(wrap(json.dumps(make_status())))

    assert status is not None
    assert status.daemon_running is True
    assert status.backend_state == "running"
    assert status.version == "1.98.3-1 (OpenWrt)"
    assert status.needs_login is False
    assert status.self_node.node_id == "nSelf0001CNTRL"
    assert status.self_node.ip_addresses == ["100.64.0.1", "fd7a:115c:a1e0::1"]
    assert status.self_node.dns_name == "router.example.ts.net"
    assert status.self_node.relay == "waw"
    assert status.self_node.last_seen is None
    assert status.self_node.key_expiry is not None
    assert status.self_node.key_expiry.tzinfo is not None
    assert status.self_node.key_expiry.year == 2027
    assert status.advertised_routes == ["198.51.100.0/24"]
    assert status.magic_dns_suffix == "example.ts.net"
    assert status.exit_node_in_use is False
    assert status.rx_bytes == 12345
    assert status.tx_bytes == 678

    peers = {p.hostname: p for p in status.peers}
    assert [p.hostname for p in status.peers] == ["peer-a", "peer-b", "peer-c"]
    assert peers["peer-a"].connection == "direct"
    assert peers["peer-b"].connection == "relay"
    assert peers["peer-b"].exit_node_option is True
    assert peers["peer-c"].connection == "idle"
    assert peers["peer-c"].online is False
    assert peers["peer-c"].last_seen is not None
    assert peers["peer-a"].last_seen is None


def test_parse_drops_personal_data() -> None:
    """User, tailnet name, auth URL, keys and public endpoints are not kept."""
    doc = make_status(BackendState="NeedsLogin", AuthURL=AUTH_URL)
    status = parse_tailscale_output(wrap(json.dumps(doc)))

    assert status is not None
    assert status.backend_state == "needs_login"
    assert status.needs_login is True
    text = repr(status)
    for secret in (AUTH_URL, LOGIN_NAME, "nodekey:", "203.0.113."):
        assert secret not in text


def test_parse_null_fields() -> None:
    """Null Peer/TailscaleIPs/Health/Self do not raise."""
    doc = make_status(Peer=None, TailscaleIPs=None, Health=None, Self=None)
    status = parse_tailscale_output(wrap(json.dumps(doc)))

    assert status is not None
    assert status.peers == []
    assert status.health == []
    assert status.self_node.ip_addresses == []
    assert status.advertised_routes == []


def test_parse_missing_key_expiry_and_counters() -> None:
    """Disabled key expiry and missing tailscale0 counters map to None."""
    doc = make_status()
    del doc["Self"]["KeyExpiry"]
    status = parse_tailscale_output(wrap(json.dumps(doc), counters=""))

    assert status is not None
    assert status.self_node.key_expiry is None
    assert status.rx_bytes is None
    assert status.tx_bytes is None


def test_parse_tolerates_leading_warning() -> None:
    """A stderr warning printed before the JSON does not break parsing."""
    body = "Warning: client version != tailscaled server version\n" + json.dumps(
        make_status()
    )
    status = parse_tailscale_output(wrap(body))

    assert status is not None
    assert status.backend_state == "running"


def test_parse_health_and_exit_node() -> None:
    """Health messages and an active exit node are reported."""
    doc = make_status(
        Health=["some health warning"], ExitNodeStatus={"ID": "nPeerB0001CNTRL"}
    )
    status = parse_tailscale_output(wrap(json.dumps(doc)))

    assert status is not None
    assert status.health == ["some health warning"]
    assert status.exit_node_in_use is True


def test_parse_daemon_unavailable() -> None:
    """The daemon-down error is reported explicitly, not as unknown."""
    body = (
        "failed to connect to local tailscaled; it doesn't appear to be running "
        "(sudo systemctl start tailscaled ?)"
    )
    status = parse_tailscale_output(wrap(body, rc=1, counters=""))

    assert status is not None
    assert status.daemon_running is False
    assert status.backend_state == "daemon_unavailable"
    assert status.peers == []


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        json.dumps(make_status()),  # no exit-code marker
        wrap("sh: tailscale: not found", rc=127, counters=""),
        wrap('{"BackendState": "Runn', counters=""),  # truncated output
        wrap("no json here", counters=""),
        wrap("[1, 2, 3]", counters=""),
        f"{RC_MARKER}garbage",
    ],
)
def test_parse_unknown_results(raw: str | None) -> None:
    """Transport failures and malformed output return None (keep old data)."""
    assert parse_tailscale_output(raw) is None


def test_normalize_backend_state() -> None:
    """Known states are normalized, everything else is unknown."""
    assert normalize_backend_state("Running") == "running"
    assert normalize_backend_state("NeedsMachineAuth") == "needs_machine_auth"
    assert normalize_backend_state("SomethingNew") == "unknown"
    assert normalize_backend_state("") == "unknown"
    assert set(STATE_OPTIONS) >= {"running", "daemon_unavailable", "unknown"}


@pytest.mark.parametrize("client_class", [UbusClient, SshClient, LuciRpcClient])
async def test_get_tailscale_status_all_transports(client_class) -> None:
    """Every transport runs the same command through execute_command."""
    client = client_class(MagicMock(), MagicMock(), HOST, "user", "pass")
    raw = wrap(json.dumps(make_status()))
    with patch.object(
        client, "execute_command", new_callable=AsyncMock, return_value=raw
    ) as mock_exec:
        status = await client.get_tailscale_status()

    mock_exec.assert_awaited_once_with(TAILSCALE_STATUS_COMMAND)
    assert status is not None
    assert status.backend_state == "running"


def _mock_all_data_client(packages: OpenWrtPackages) -> UbusClient:
    """Return a UbusClient whose data getters are all mocked."""
    client = UbusClient(MagicMock(), MagicMock(), HOST, "user", "pass")
    for name in (
        "get_system_resources",
        "get_device_info",
        "get_qmodem_info",
        "get_latency",
        "get_external_ip",
        "get_gateway_mac",
        "get_wps_status",
    ):
        setattr(client, name, AsyncMock())
    for name in (
        "get_network_interfaces",
        "get_connected_devices",
        "get_services",
        "get_leds",
        "get_firewall_redirects",
        "get_firewall_rules",
        "get_access_control",
        "get_sqm_status",
        "get_wireguard_interfaces",
        "get_system_logs",
        "get_ip_neighbors",
        "get_mwan_status",
        "get_vpn_status",
        "get_wifi_credentials",
        "get_dhcp_leases",
        "get_lldp_neighbors",
        "get_upnp_mappings",
    ):
        setattr(client, name, AsyncMock(return_value=[]))
    client.get_local_macs = AsyncMock(return_value=set())
    client.get_local_ips = AsyncMock(return_value=set())
    client.check_packages = AsyncMock(return_value=packages)
    client.check_permissions = AsyncMock(return_value=OpenWrtPermissions(read_vpn=True))
    client.is_reboot_required = AsyncMock(return_value=False)
    client._call = AsyncMock(return_value={})
    client.execute_command = AsyncMock(return_value="")
    client.read_file = AsyncMock(return_value=None)
    return client


async def test_get_all_data_polls_tailscale_in_medium_tier() -> None:
    """Status is fetched when installed and cached for fast polls."""
    client = _mock_all_data_client(OpenWrtPackages(tailscale=True))
    status = TailscaleStatus(daemon_running=True, backend_state="running")
    client.get_tailscale_status = AsyncMock(return_value=status)
    client.coordinator = MagicMock()
    client.coordinator.data = OpenWrtData()

    data = await client.get_all_data()

    client.get_tailscale_status.assert_awaited_once()
    assert data.tailscale is status
    assert client._cached_medium_data["tailscale"] is status


async def test_get_all_data_keeps_previous_status_on_unknown() -> None:
    """An unknown (None) result keeps the previous status."""
    client = _mock_all_data_client(OpenWrtPackages(tailscale=True))
    client.get_tailscale_status = AsyncMock(return_value=None)
    previous = TailscaleStatus(daemon_running=True, backend_state="running")
    client.coordinator = MagicMock()
    client.coordinator.data = OpenWrtData(tailscale=previous)

    data = await client.get_all_data()

    assert data.tailscale is previous


async def test_get_all_data_drops_status_when_uninstalled() -> None:
    """No polling and no stale status once the package is gone."""
    client = _mock_all_data_client(OpenWrtPackages(tailscale=False))
    client.get_tailscale_status = AsyncMock()
    client.coordinator = MagicMock()
    client.coordinator.data = OpenWrtData(
        tailscale=TailscaleStatus(daemon_running=True, backend_state="running")
    )

    data = await client.get_all_data()

    client.get_tailscale_status.assert_not_awaited()
    assert data.tailscale is None


async def test_openvpn_tun_scan_skips_tailscale() -> None:
    """tailscale0 is never reported as an OpenVPN tunnel."""
    client = UbusClient(MagicMock(), MagicMock(), HOST, "user", "pass")

    async def fake_exec(command: str) -> str:
        if command.startswith("pgrep -a openvpn"):
            return "1234 /usr/sbin/openvpn --config /etc/openvpn/client.conf"
        if command.startswith("ip -br link show type tun"):
            return "tun0             UNKNOWN\ntailscale0       UNKNOWN"
        if command.startswith("cat /sys/class/net/"):
            return "42"
        return ""

    with patch.object(client, "execute_command", side_effect=fake_exec):
        interfaces = await client.get_vpn_status()

    assert [i.name for i in interfaces] == ["tun0"]


def _probe(length: int, ones: set[int]) -> str:
    return "\n".join("1" if i in ones else "0" for i in range(length)) + "\n"


@pytest.mark.parametrize("index", [25, 26])
async def test_ubus_detects_tailscale(index: int) -> None:
    """ubus batch probe: trailing indices 25/26 are /usr/sbin and /usr/bin."""
    client = UbusClient(MagicMock(), MagicMock(), HOST, "user", "pass")

    def call_side_effect(obj, method, params=None):
        if obj == "file" and method == "exec":
            return {"stdout": _probe(27, {index})}
        return {}

    with (
        patch.object(client, "_list_objects", new_callable=AsyncMock, return_value=[]),
        patch.object(client, "_call", new_callable=AsyncMock) as mock_call,
        patch.object(
            client, "get_installed_packages", new_callable=AsyncMock, return_value=[]
        ),
    ):
        mock_call.side_effect = call_side_effect
        packages = await client.check_packages()

    assert packages.tailscale is True
    # Neighbouring positional indices are untouched.
    assert packages.timeout is False
    assert packages.stty is False


async def test_ubus_without_tailscale() -> None:
    """Old 25-line probe output (no tailscale) leaves the package False."""
    client = UbusClient(MagicMock(), MagicMock(), HOST, "user", "pass")

    def call_side_effect(obj, method, params=None):
        if obj == "file" and method == "exec":
            return {"stdout": _probe(25, {23})}
        return {}

    with (
        patch.object(client, "_list_objects", new_callable=AsyncMock, return_value=[]),
        patch.object(client, "_call", new_callable=AsyncMock) as mock_call,
        patch.object(
            client, "get_installed_packages", new_callable=AsyncMock, return_value=[]
        ),
    ):
        mock_call.side_effect = call_side_effect
        packages = await client.check_packages()

    assert packages.tailscale is False
    assert packages.timeout is True


@pytest.mark.parametrize("index", [29, 30])
async def test_ssh_detects_tailscale(index: int) -> None:
    """SSH probe: trailing indices 29/30."""
    client = SshClient(MagicMock(), MagicMock(), host=HOST, username="u", password="p")
    with (
        patch.object(client, "_exec", new_callable=AsyncMock) as mock_exec,
        patch.object(
            client, "get_installed_packages", new_callable=AsyncMock, return_value=[]
        ),
    ):
        mock_exec.return_value = _probe(31, {index})
        packages = await client.check_packages()

    assert packages.tailscale is True
    assert packages.snort is False


async def test_ssh_detects_tailscale_from_package_list() -> None:
    """SSH falls back to the installed package list."""
    client = SshClient(MagicMock(), MagicMock(), host=HOST, username="u", password="p")
    with (
        patch.object(client, "_exec", new_callable=AsyncMock) as mock_exec,
        patch.object(
            client,
            "get_installed_packages",
            new_callable=AsyncMock,
            return_value=["tailscale"],
        ),
    ):
        mock_exec.return_value = _probe(29, set())
        packages = await client.check_packages()

    assert packages.tailscale is True


@pytest.mark.parametrize("index", [27, 28])
async def test_luci_detects_tailscale(index: int) -> None:
    """LuCI-RPC probe: trailing indices 27/28."""
    client = LuciRpcClient(MagicMock(), MagicMock(), HOST, "user", "pass")
    with (
        patch.object(client, "_rpc_call", new_callable=AsyncMock) as mock_rpc,
        patch.object(
            client, "execute_command", new_callable=AsyncMock, return_value=""
        ),
        patch.object(
            client, "get_installed_packages", new_callable=AsyncMock, return_value=[]
        ),
    ):
        mock_rpc.return_value = _probe(29, {index})
        packages = await client.check_packages()

    assert packages.tailscale is True
    assert packages.snort is False
