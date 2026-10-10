"""Tests for Tailscale entities, cleanup, diagnostics and config flow defaults."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.openwrt.api.base import (
    OpenWrtData,
    OpenWrtPackages,
    OpenWrtPermissions,
    TailscalePeer,
    TailscaleStatus,
)
from custom_components.openwrt.const import (
    CONF_ENABLE_SERVICES,
    CONF_ENABLE_VPN,
    CONF_TRACK_DEVICES,
    DATA_COORDINATOR,
    DOMAIN,
)

ENTRY_ID = "test_entry"
KEY_EXPIRY = datetime(2027, 2, 19, 15, 49, 27, tzinfo=UTC)


def make_status(**overrides) -> TailscaleStatus:
    """Return an anonymized running status with two peers."""
    status = TailscaleStatus(
        daemon_running=True,
        backend_state="running",
        version="1.98.3-1 (OpenWrt)",
        self_node=TailscalePeer(
            node_id="nSelf",
            hostname="router",
            dns_name="router.example.ts.net",
            ip_addresses=["100.64.0.1", "fd7a:115c:a1e0::1"],
            online=True,
            relay="waw",
            key_expiry=KEY_EXPIRY,
        ),
        advertised_routes=["198.51.100.0/24"],
        magic_dns_suffix="example.ts.net",
        peers=[
            TailscalePeer(
                node_id="nPeerA",
                hostname="peer-a",
                os="windows",
                ip_addresses=["100.64.0.2"],
                online=True,
                connection="direct",
            ),
            TailscalePeer(
                node_id="nPeerB",
                hostname="peer-b",
                os="linux",
                ip_addresses=["100.64.0.3"],
                online=False,
                last_seen=datetime(2026, 9, 30, 16, 54, 24, tzinfo=UTC),
            ),
        ],
        rx_bytes=2 * 1024 * 1024,
        tx_bytes=1024 * 1024,
    )
    for key, value in overrides.items():
        setattr(status, key, value)
    return status


def make_data(status: TailscaleStatus | None = None, **kwargs) -> OpenWrtData:
    return OpenWrtData(
        permissions=kwargs.get("permissions", OpenWrtPermissions(read_vpn=True)),
        packages=kwargs.get("packages", OpenWrtPackages(tailscale=True)),
        tailscale=status if status is not None else make_status(),
    )


def make_entry(options: dict | None = None) -> MagicMock:
    entry = MagicMock()
    entry.entry_id = ENTRY_ID
    entry.unique_id = "router-unique-id"
    entry.data = {"host": "192.0.2.1"}
    entry.options = {CONF_ENABLE_VPN: True} if options is None else options
    return entry


def make_coordinator(data: OpenWrtData) -> MagicMock:
    coordinator = MagicMock()
    coordinator.data = data
    coordinator.last_update_success = True
    return coordinator


def setup_sensors(data: OpenWrtData, entry: MagicMock) -> dict:
    from custom_components.openwrt.sensors.tailscale import (
        _async_setup_tailscale_sensors,
    )

    entities: list = []
    _async_setup_tailscale_sensors(make_coordinator(data), entry, entities, set())
    return {e.entity_description.key: e for e in entities}


def setup_binary_sensors(data: OpenWrtData, entry: MagicMock) -> dict:
    from custom_components.openwrt.binary_sensor import (
        _async_setup_tailscale_binary_sensors,
    )

    entities: list = []
    _async_setup_tailscale_binary_sensors(
        make_coordinator(data), entry, entities, set()
    )
    return {e.entity_description.key: e for e in entities}


def test_sensors_created_with_tsvpn_unique_ids() -> None:
    """All sensors use the _tsvpn_ token; traffic counters are disabled."""
    sensors = setup_sensors(make_data(), make_entry())

    assert set(sensors) == {
        "tsvpn_backend_state",
        "tsvpn_ip",
        "tsvpn_home_derp",
        "tsvpn_peers_online",
        "tsvpn_key_expiry",
        "tsvpn_rx",
        "tsvpn_tx",
    }
    for entity in sensors.values():
        assert entity.unique_id.startswith(f"{ENTRY_ID}_tsvpn_")
    assert sensors["tsvpn_rx"].entity_registry_enabled_default is False
    assert sensors["tsvpn_tx"].entity_registry_enabled_default is False
    assert sensors["tsvpn_home_derp"].entity_registry_enabled_default is False
    assert sensors["tsvpn_backend_state"].entity_registry_enabled_default is True


def test_sensor_values_and_attributes() -> None:
    """Sensor values come from the parsed status."""
    sensors = setup_sensors(make_data(), make_entry())

    state = sensors["tsvpn_backend_state"]
    assert state.native_value == "running"
    assert "running" in state.entity_description.options
    assert state.extra_state_attributes == {
        "version": "1.98.3-1 (OpenWrt)",
        "needs_login": False,
        "exit_node_in_use": False,
    }
    ip = sensors["tsvpn_ip"]
    assert ip.native_value == "100.64.0.1"
    assert ip.extra_state_attributes["ipv6"] == "fd7a:115c:a1e0::1"
    assert ip.extra_state_attributes["advertised_routes"] == ["198.51.100.0/24"]
    assert sensors["tsvpn_peers_online"].native_value == 1
    assert sensors["tsvpn_peers_online"].extra_state_attributes == {
        "total": 2,
        "offline": 1,
    }
    assert sensors["tsvpn_key_expiry"].native_value == KEY_EXPIRY
    assert sensors["tsvpn_home_derp"].native_value == "waw"
    assert sensors["tsvpn_rx"].native_value == 2.0
    assert sensors["tsvpn_tx"].native_value == 1.0


def test_optional_sensors_skipped() -> None:
    """No key-expiry sensor without expiry, no counters without tailscale0."""
    status = make_status(rx_bytes=None, tx_bytes=None)
    status.self_node.key_expiry = None
    sensors = setup_sensors(make_data(status), make_entry())

    assert "tsvpn_key_expiry" not in sensors
    assert "tsvpn_rx" not in sensors
    assert "tsvpn_tx" not in sensors


@pytest.mark.parametrize(
    ("options", "permissions", "packages"),
    [
        ({}, OpenWrtPermissions(read_vpn=True), OpenWrtPackages(tailscale=True)),
        (
            {CONF_ENABLE_VPN: False},
            OpenWrtPermissions(read_vpn=True),
            OpenWrtPackages(tailscale=True),
        ),
        (
            {CONF_ENABLE_VPN: True},
            OpenWrtPermissions(read_vpn=False),
            OpenWrtPackages(tailscale=True),
        ),
        (
            {CONF_ENABLE_VPN: True},
            OpenWrtPermissions(read_vpn=True),
            OpenWrtPackages(tailscale=False),
        ),
    ],
)
def test_entities_gated(options, permissions, packages) -> None:
    """Nothing is created unless VPN is enabled, permitted and installed."""
    data = make_data(permissions=permissions, packages=packages)
    entry = make_entry(options)

    assert setup_sensors(data, entry) == {}
    assert setup_binary_sensors(data, entry) == {}


def test_entities_wait_for_first_status() -> None:
    """No entities before the first successful fetch."""
    data = make_data()
    data.tailscale = None

    assert setup_sensors(data, make_entry()) == {}
    assert setup_binary_sensors(data, make_entry()) == {}


def test_binary_sensors_and_peers() -> None:
    """Connected/problem sensors plus disabled-by-default peer sensors."""
    data = make_data()
    binary = setup_binary_sensors(data, make_entry())

    assert set(binary) == {
        "tsvpn_connected",
        "tsvpn_health",
        "tsvpn_peer_nPeerA_online",
        "tsvpn_peer_nPeerB_online",
    }
    assert binary["tsvpn_connected"].is_on is True
    assert binary["tsvpn_health"].is_on is False
    assert binary["tsvpn_health"].extra_state_attributes == {
        "messages": [],
        "needs_login": False,
    }

    peer_a = binary["tsvpn_peer_nPeerA_online"]
    assert peer_a.unique_id == f"{ENTRY_ID}_tsvpn_peer_nPeerA_online"
    # Real HA reads this from the description (the test Entity mock does not).
    assert peer_a.entity_description.entity_registry_enabled_default is False
    assert peer_a.entity_description.translation_placeholders == {"peer": "peer-a"}
    assert peer_a.is_on is True
    assert peer_a.extra_state_attributes["connection"] == "direct"
    assert peer_a.extra_state_attributes["os"] == "windows"
    peer_b = binary["tsvpn_peer_nPeerB_online"]
    assert peer_b.is_on is False
    assert peer_b.extra_state_attributes["last_seen"] == "2026-09-30T16:54:24+00:00"


def test_problem_sensor_on_health_or_login() -> None:
    """Health warnings or a required login turn the problem sensor on."""
    binary = setup_binary_sensors(
        make_data(make_status(health=["warning"])), make_entry()
    )
    assert binary["tsvpn_health"].is_on is True
    assert binary["tsvpn_health"].extra_state_attributes["messages"] == ["warning"]

    binary = setup_binary_sensors(
        make_data(make_status(needs_login=True, backend_state="needs_login")),
        make_entry(),
    )
    assert binary["tsvpn_health"].is_on is True
    assert binary["tsvpn_connected"].is_on is False


def test_daemon_unavailable_state() -> None:
    """A stopped daemon is a state, not an unavailable entity."""
    data = make_data()
    sensors = setup_sensors(data, make_entry())
    binary = setup_binary_sensors(data, make_entry())

    data.tailscale = TailscaleStatus(
        daemon_running=False, backend_state="daemon_unavailable"
    )

    assert sensors["tsvpn_backend_state"].available is True
    assert sensors["tsvpn_backend_state"].native_value == "daemon_unavailable"
    assert binary["tsvpn_connected"].is_on is False
    # Peers are unknown while the daemon is down.
    assert binary["tsvpn_peer_nPeerA_online"].available is False


def test_removed_peer_becomes_unavailable() -> None:
    """A peer missing from new data is unavailable, not deleted."""
    data = make_data()
    binary = setup_binary_sensors(data, make_entry())
    peer_b = binary["tsvpn_peer_nPeerB_online"]
    assert peer_b.available is True

    data.tailscale = make_status(peers=[data.tailscale.peers[0]])

    assert peer_b.available is False
    assert peer_b.is_on is False
    assert peer_b.extra_state_attributes == {}


def test_new_peer_added_later() -> None:
    """Discovery adds peers that appear after setup, without duplicates."""
    from custom_components.openwrt.binary_sensor import (
        _async_setup_tailscale_binary_sensors,
    )

    data = make_data()
    coordinator = make_coordinator(data)
    entry = make_entry()
    tracked: set[str] = set()
    first: list = []
    _async_setup_tailscale_binary_sensors(coordinator, entry, first, tracked)

    data.tailscale.peers.append(TailscalePeer(node_id="nPeerC", hostname="peer-c"))
    second: list = []
    _async_setup_tailscale_binary_sensors(coordinator, entry, second, tracked)

    assert len(first) == 4
    assert [e.entity_description.key for e in second] == ["tsvpn_peer_nPeerC_online"]


async def _run_binary_cleanup(data: OpenWrtData, registry_entries: list) -> MagicMock:
    from custom_components.openwrt.binary_sensor import async_setup_entry

    entry = make_entry()
    coordinator = make_coordinator(data)
    coordinator.async_add_listener = MagicMock(return_value=MagicMock())
    hass = MagicMock()
    hass.data = {DOMAIN: {ENTRY_ID: {DATA_COORDINATOR: coordinator}}}
    jobs: list = []
    hass.add_job = jobs.append
    ent_reg = MagicMock()

    with (
        patch(
            "custom_components.openwrt.binary_sensor.er.async_get",
            return_value=ent_reg,
        ),
        patch(
            "custom_components.openwrt.binary_sensor.er.async_entries_for_config_entry",
            return_value=registry_entries,
        ),
    ):
        await async_setup_entry(hass, entry, MagicMock())
        for job in jobs:
            await job()
    return ent_reg


def _peer_registry_entries() -> list:
    return [
        MagicMock(
            domain="binary_sensor",
            entity_id="binary_sensor.router_tailscale_peer_peer_a",
            unique_id=f"{ENTRY_ID}_tsvpn_peer_nPeerA_online",
        ),
        MagicMock(
            domain="binary_sensor",
            entity_id="binary_sensor.router_tailscale_peer_gone",
            unique_id=f"{ENTRY_ID}_tsvpn_peer_nGone_online",
        ),
    ]


async def test_setup_cleanup_prunes_stale_peers_when_running() -> None:
    """Registry entries of vanished peers are removed on authoritative data."""
    ent_reg = await _run_binary_cleanup(make_data(), _peer_registry_entries())

    ent_reg.async_remove.assert_called_once_with(
        "binary_sensor.router_tailscale_peer_gone"
    )


@pytest.mark.parametrize(
    "status",
    [
        TailscaleStatus(daemon_running=False, backend_state="daemon_unavailable"),
        TailscaleStatus(daemon_running=True, backend_state="stopped"),
        TailscaleStatus(daemon_running=True, backend_state="needs_login"),
    ],
)
async def test_setup_cleanup_keeps_peers_during_outage(status) -> None:
    """Outages and logged-out states never prune peer entities."""
    ent_reg = await _run_binary_cleanup(make_data(status), _peer_registry_entries())

    ent_reg.async_remove.assert_not_called()


async def test_setup_cleanup_keeps_peers_without_status() -> None:
    """Unknown status (None) never prunes peer entities."""
    data = make_data()
    data.tailscale = None
    ent_reg = await _run_binary_cleanup(data, _peer_registry_entries())

    ent_reg.async_remove.assert_not_called()


async def test_disable_vpn_removes_only_tsvpn_entities(hass) -> None:
    """Disabling VPN removes _tsvpn_ entities but keeps generic tailscale ones."""
    from custom_components.openwrt import _async_cleanup_disabled_features

    entry = make_entry(
        {
            CONF_ENABLE_VPN: False,
            CONF_ENABLE_SERVICES: True,
            CONF_TRACK_DEVICES: True,
        }
    )
    unique_ids = {
        "sensor.router_tailscale_vpn_state": f"{ENTRY_ID}_tsvpn_backend_state",
        "binary_sensor.router_tailscale_peer_a": f"{ENTRY_ID}_tsvpn_peer_nA_online",
        "sensor.router_tailscale_rx": f"{ENTRY_ID}_net_tailscale_rx",
        "binary_sensor.router_service_tailscale": (
            f"{ENTRY_ID}_service_tailscale_running"
        ),
        "switch.router_tailscale": f"{ENTRY_ID}_service_tailscale",
    }
    entries = [
        MagicMock(entity_id=entity_id, domain=entity_id.split(".")[0], unique_id=uid)
        for entity_id, uid in unique_ids.items()
    ]
    ent_reg = MagicMock()

    with (
        patch("custom_components.openwrt.er.async_get", return_value=ent_reg),
        patch("custom_components.openwrt.dr.async_get", return_value=MagicMock()),
        patch(
            "custom_components.openwrt.er.async_entries_for_config_entry",
            return_value=entries,
        ),
        patch(
            "custom_components.openwrt.dr.async_entries_for_config_entry",
            return_value=[],
        ),
    ):
        await _async_cleanup_disabled_features(hass, entry)

    removed = {call.args[0] for call in ent_reg.async_remove.call_args_list}
    assert removed == {
        "sensor.router_tailscale_vpn_state",
        "binary_sensor.router_tailscale_peer_a",
    }


async def test_diagnostics_tailscale_block_is_private() -> None:
    """Diagnostics expose counts/states only."""
    from custom_components.openwrt.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    data = make_data()
    entry = MagicMock()
    entry.data = {}
    entry.options = {}
    entry.entry_id = ENTRY_ID
    entry.unique_id = "router-unique-id"
    hass = MagicMock()
    hass.data = {DOMAIN: {ENTRY_ID: {DATA_COORDINATOR: make_coordinator(data)}}}

    with (
        patch(
            "custom_components.openwrt.diagnostics.async_redact_data",
            side_effect=lambda value, keys: value,
        ),
        patch("homeassistant.helpers.device_registry.async_get"),
        patch("homeassistant.helpers.entity_registry.async_get"),
        patch(
            "homeassistant.helpers.entity_registry.async_entries_for_config_entry",
            return_value=[],
        ),
        patch(
            "homeassistant.helpers.device_registry.async_entries_for_config_entry",
            return_value=[],
        ),
    ):
        diag = await async_get_config_entry_diagnostics(hass, entry)

    assert diag["packages"]["tailscale"] is True
    block = diag["tailscale"]
    assert block == {
        "backend_state": "running",
        "version": "1.98.3-1 (OpenWrt)",
        "daemon_running": True,
        "needs_login": False,
        "peers_total": 2,
        "peers_online": 1,
        "health_count": 0,
        "exit_node_in_use": False,
    }
    text = repr(block) + repr(diag["packages"])
    for private in ("100.64.", "peer-a", "router.example", "example.ts.net"):
        assert private not in text


async def test_config_flow_vpn_default_with_only_tailscale() -> None:
    """The VPN option defaults to on when only Tailscale is installed."""
    from custom_components.openwrt.config_flow import OpenWrtConfigFlow

    flow = OpenWrtConfigFlow()
    flow.hass = MagicMock()
    flow._data = {}
    flow._packages = OpenWrtPackages(
        tailscale=True, wireguard=False, openvpn=False, sqm_scripts=False
    )

    with patch(
        "custom_components.openwrt.config_flow.translation.async_get_translations",
        new_callable=AsyncMock,
        return_value={},
    ):
        result = await flow.async_step_packages(None)

    schema = result["data_schema"].schema
    vpn_key = next(key for key in schema if str(key) == CONF_ENABLE_VPN)
    assert vpn_key.default() is True
    assert "**tailscale** | ✅" in str(result["description_placeholders"])


async def test_options_flow_keeps_vpn_enabled_with_only_tailscale() -> None:
    """Re-opening options must not silently turn VPN off on Tailscale-only routers."""
    from custom_components.openwrt.config_flow import OpenWrtOptionsFlow

    flow = OpenWrtOptionsFlow(make_entry({CONF_ENABLE_VPN: True}))
    flow.hass = MagicMock()
    flow._packages = OpenWrtPackages(tailscale=True, wireguard=False, openvpn=False)

    with patch(
        "custom_components.openwrt.config_flow.translation.async_get_translations",
        new_callable=AsyncMock,
        return_value={},
    ):
        result = await flow.async_step_options_packages(None)

    assert result["step_id"] == "options_packages"
    schema = result["data_schema"].schema
    vpn_key = next(key for key in schema if str(key) == CONF_ENABLE_VPN)
    assert vpn_key.default() is True
