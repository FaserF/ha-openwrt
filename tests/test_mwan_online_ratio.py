"""Tests for the MWAN3 online ratio since the router last booted."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.openwrt.api.base import (
    MwanStatus,
    OpenWrtData,
    OpenWrtPackages,
)
from custom_components.openwrt.api.luci_rpc import LuciRpcClient
from custom_components.openwrt.api.ssh import SshClient
from custom_components.openwrt.api.ubus import UbusClient, UbusTimeoutError
from custom_components.openwrt.coordinator import OpenWrtDataCoordinator
from custom_components.openwrt.sensor import _create_mwan_sensors

BOOT = datetime(2026, 10, 3, 3, 2, 0, tzinfo=UTC)

# The reply of "ubus call mwan3 status": every backend reads this structure.
MWAN_REPLY = {
    "interfaces": {
        "wan": {"status": "online", "online": 55090, "offline": 0, "uptime": 61245},
        "backup": {
            "status": "offline",
            "online": 0,
            "offline": 42,
            "uptime": 61226,
        },
    }
}


def _make_coordinator(
    stored: dict[str, Any] | None = None,
    load_error: Exception | None = None,
) -> tuple[OpenWrtDataCoordinator, MagicMock]:
    """Create a coordinator whose MWAN3 store returns the given payload."""
    hass = MagicMock()
    hass.loop = MagicMock()
    hass.loop.time = MagicMock(return_value=123456789.0)
    config_entry = MagicMock()
    config_entry.options = {}
    config_entry.data = {"host": "192.168.1.1", "username": "root"}
    config_entry.entry_id = "test_entry"

    with patch("custom_components.openwrt.coordinator.storage.Store") as mock_store:
        store = mock_store.return_value
        store.async_load = AsyncMock(return_value=stored, side_effect=load_error)
        store.async_save = AsyncMock()
        coordinator = OpenWrtDataCoordinator(hass, config_entry, AsyncMock())
    coordinator._boot_time = BOOT
    return coordinator, store


async def _poll(
    coordinator: OpenWrtDataCoordinator,
    system_uptime: int,
    rebooted: bool = False,
    **interfaces: tuple[int, int, int],
) -> dict[str, MwanStatus]:
    """Run one update cycle; each interface is (mwan uptime, online, offline)."""
    data = OpenWrtData()
    data.mwan_status = [
        MwanStatus(interface_name=name, uptime=uptime, online=online, offline=offline)
        for name, (uptime, online, offline) in interfaces.items()
    ]
    await coordinator._async_update_mwan_boot_totals(data, system_uptime, rebooted)
    return {m.interface_name: m for m in data.mwan_status}


async def test_no_ratio_before_the_first_online_moment() -> None:
    """Booting, DHCP and mwan3's own checks are not an outage of the line."""
    coordinator, _ = _make_coordinator()

    result = await _poll(coordinator, 30, wan=(25, 0, 25))

    assert result["wan"].boot_online_ratio is None
    assert result["wan"].coverage_start is None


async def test_window_opens_at_the_first_online_moment() -> None:
    """The first online stamp opens the window; a clean run reports 100 %."""
    coordinator, store = _make_coordinator()
    now = datetime(2026, 10, 3, 3, 20, 0, tzinfo=UTC)

    with patch(
        "custom_components.openwrt.coordinator.dt_util.utcnow", return_value=now
    ):
        result = await _poll(coordinator, 1000, wan=(990, 100, 0))

    assert result["wan"].boot_online_ratio == 1.0
    # Online since uptime second 900, i.e. 100 s before this sample.
    assert result["wan"].coverage_start == now - timedelta(seconds=100)
    store.async_save.assert_awaited()


async def test_observed_outage_is_counted_to_the_second() -> None:
    """Both ends of a seen outage come from the router's own stamps."""
    coordinator, _ = _make_coordinator()
    await _poll(coordinator, 1000, wan=(990, 100, 0))  # online since 900

    # Offline since uptime 1450: the open outage counts up to this sample.
    result = await _poll(coordinator, 1500, wan=(1490, 0, 50))
    assert result["wan"].boot_online_ratio == pytest.approx(550 / 600)

    # Online again since 1970: the outage lasted exactly 1970 - 1450 = 520 s.
    result = await _poll(coordinator, 2000, wan=(1990, 30, 0))
    assert result["wan"].boot_online_ratio == pytest.approx((1100 - 520) / 1100)


async def test_outage_between_two_polls_is_counted_conservatively() -> None:
    """An outage no poll saw is dated from the last poll, so the ratio errs low."""
    coordinator, _ = _make_coordinator()
    await _poll(coordinator, 1000, wan=(990, 100, 0))  # online since 900

    # The online stamp moved to 1040 without an offline sample in between.
    result = await _poll(coordinator, 1060, wan=(1050, 20, 0))

    # Counted from the last poll (1000) up to the new stamp (1040): 40 s.
    assert result["wan"].boot_online_ratio == pytest.approx((160 - 40) / 160)


async def test_handed_back_reply_does_not_invent_an_outage() -> None:
    """A repeated (uptime, online, offline) triple carries no new information.

    After failed calls the client hands back its previous mwan3 reply. Paired
    with a fresh system uptime it would move the reconstructed start of the
    online spell forward and book the difference as an outage.
    """
    coordinator, _ = _make_coordinator()
    await _poll(coordinator, 100, wan=(90, 10, 0))  # online since 90

    result = await _poll(coordinator, 400, wan=(90, 10, 0))

    assert result["wan"].boot_online_ratio == 1.0


async def test_interfaces_are_tracked_independently() -> None:
    """An outage on one uplink does not leak into the other."""
    coordinator, _ = _make_coordinator()
    await _poll(coordinator, 1000, wan=(990, 100, 0), backup=(995, 990, 0))

    result = await _poll(coordinator, 1500, wan=(1490, 0, 50), backup=(1495, 1490, 0))

    assert result["wan"].boot_online_ratio == pytest.approx(550 / 600)
    assert result["backup"].boot_online_ratio == 1.0


@pytest.mark.parametrize("rebooted_flag", [True, False])
async def test_router_reboot_starts_a_new_window(rebooted_flag: bool) -> None:
    """A new boot time resets the totals, even if the reboot flag was missed.

    The first sample of the new boot arrives late enough that its uptime is
    already past the old first-online second, so only the boot time can tell
    the two boots apart.
    """
    coordinator, _ = _make_coordinator()
    await _poll(coordinator, 1000, wan=(990, 100, 0))
    await _poll(coordinator, 1500, wan=(1490, 0, 50))  # outage still open

    coordinator._boot_time = BOOT + timedelta(days=1)
    result = await _poll(coordinator, 5000, rebooted=rebooted_flag, wan=(4990, 1000, 0))

    assert result["wan"].boot_online_ratio == 1.0


async def test_uptime_going_backwards_reanchors_the_window() -> None:
    """A first-online second later than the current uptime belongs to no boot."""
    coordinator, _ = _make_coordinator()
    await _poll(coordinator, 1000, wan=(990, 100, 0))
    await _poll(coordinator, 1500, wan=(1490, 0, 50))  # outage still open

    result = await _poll(coordinator, 200, wan=(190, 100, 0))

    assert result["wan"].boot_online_ratio == 1.0


async def test_mwan_sample_from_before_the_reboot_is_dropped() -> None:
    """A tracking session cannot be older than the router itself.

    Such a sample was retained from before the reboot, e.g. because the mwan3
    query failed. Its state and ratio describe the previous boot.
    """
    coordinator, _ = _make_coordinator()
    data = OpenWrtData()
    data.mwan_status = [
        MwanStatus(
            interface_name="wan",
            status="online",
            uptime=5000,
            online=4000,
            boot_online_ratio=0.97,
            coverage_start=BOOT,
        )
    ]

    await coordinator._async_update_mwan_boot_totals(data, 100, True)

    assert data.mwan_status == []


async def test_reply_handed_back_after_a_reboot_is_dropped() -> None:
    """A failed query right after a reboot hands back the last reply before it.

    Its session can be shorter than the new uptime - here the line came up
    50 s before the reboot - so only its identity with that reply gives it
    away.
    """
    coordinator, _ = _make_coordinator()
    data = OpenWrtData()
    data.mwan_status = [
        MwanStatus(interface_name="wan", status="online", uptime=50, online=50)
    ]
    await coordinator._async_update_mwan_boot_totals(data, 7200, False)

    await coordinator._async_update_mwan_boot_totals(data, 100, True)

    assert data.mwan_status == []


async def test_fresh_reply_after_a_reboot_is_kept() -> None:
    """Only a handed-back reply is dropped; a real one always differs."""
    coordinator, _ = _make_coordinator()
    data = OpenWrtData()
    data.mwan_status = [
        MwanStatus(interface_name="wan", status="online", uptime=50, online=50)
    ]
    await coordinator._async_update_mwan_boot_totals(data, 7200, False)

    result = await _poll(coordinator, 100, rebooted=True, wan=(40, 0, 40))

    assert list(result) == ["wan"]
    assert result["wan"].boot_online_ratio is None


async def test_short_window_publishes_no_earlier_ratio() -> None:
    """Below a minute nothing is published, even on a reply handed back."""
    coordinator, _ = _make_coordinator()
    data = OpenWrtData()
    data.mwan_status = [
        MwanStatus(
            interface_name="wan",
            uptime=40,
            online=40,
            boot_online_ratio=0.97,
            coverage_start=BOOT,
        )
    ]

    await coordinator._async_update_mwan_boot_totals(data, 1000, False)

    assert data.mwan_status[0].boot_online_ratio is None


async def test_totals_survive_a_home_assistant_restart() -> None:
    """Totals of the current router boot are restored from storage."""
    stored = {
        "boot_epoch": BOOT.timestamp(),
        "first_online": {"wan": 900.0},
        "offline_total": {"wan": 520.0},
        "offline_since": {},
        "last_uptime": {"wan": 2000.0},
        "online_start": {"wan": 1970.0},
    }
    coordinator, _ = _make_coordinator(stored=stored)

    # Still the same online spell (since 1970), now polled after the restart.
    result = await _poll(coordinator, 3000, wan=(2990, 1030, 0))

    assert result["wan"].boot_online_ratio == pytest.approx((2100 - 520) / 2100)


async def test_stored_totals_of_an_earlier_boot_are_discarded() -> None:
    """Totals stored for another router boot must not leak into this one."""
    stored = {
        "boot_epoch": (BOOT - timedelta(days=1)).timestamp(),
        "first_online": {"wan": 900.0},
        "offline_total": {"wan": 520.0},
        "offline_since": {},
        "last_uptime": {"wan": 2000.0},
        "online_start": {"wan": 1970.0},
    }
    coordinator, _ = _make_coordinator(stored=stored)

    result = await _poll(coordinator, 1000, wan=(990, 100, 0))

    assert result["wan"].boot_online_ratio == 1.0


async def test_unreadable_store_is_not_fatal() -> None:
    """Persisting is best effort; a broken store only loses the history."""
    coordinator, _ = _make_coordinator(load_error=OSError("store unreadable"))

    result = await _poll(coordinator, 1000, wan=(990, 100, 0))

    assert result["wan"].boot_online_ratio == 1.0


async def test_ubus_mwan_status_passes_the_state_counters_through() -> None:
    """Both stamps reach the coordinator: exactly one of them is non-zero."""
    client = UbusClient(
        MagicMock(),
        MagicMock(),
        host="192.168.1.1",
        username="ha-user",
        password="password",
    )
    client._session_id = "test_token"

    with patch.object(client, "_call", new_callable=AsyncMock, return_value=MWAN_REPLY):
        statuses = {m.interface_name: m for m in await client.get_mwan_status()}

    assert (statuses["wan"].online, statuses["wan"].offline) == (55090, 0)
    assert (statuses["backup"].online, statuses["backup"].offline) == (0, 42)


@pytest.mark.parametrize(
    ("client_class", "runner"),
    [(LuciRpcClient, "execute_command"), (SshClient, "_exec")],
)
async def test_shell_backends_pass_the_state_counters_through(
    client_class: type[LuciRpcClient] | type[SshClient], runner: str
) -> None:
    """LuCI RPC and SSH read the same reply, so the ratio works there too."""
    client = client_class(
        MagicMock(),
        MagicMock(),
        host="192.168.1.1",
        username="root",
        password="password",
    )

    with patch.object(
        client, runner, new_callable=AsyncMock, return_value=json.dumps(MWAN_REPLY)
    ):
        statuses = {m.interface_name: m for m in await client.get_mwan_status()}

    assert (statuses["wan"].online, statuses["wan"].offline) == (55090, 0)
    assert (statuses["backup"].online, statuses["backup"].offline) == (0, 42)


def test_ratio_sensor_reports_the_boot_ratio_and_its_window() -> None:
    """The sensor shows the ratio in percent and the window it covers."""
    coverage = datetime(2026, 10, 3, 3, 17, 0, tzinfo=UTC)
    data = OpenWrtData()
    data.mwan_status = [
        MwanStatus(
            interface_name="wan", boot_online_ratio=0.98579, coverage_start=coverage
        ),
        MwanStatus(interface_name="backup"),
    ]
    coordinator = MagicMock()
    coordinator.data = data
    entry = MagicMock()
    entry.entry_id = "test"

    (wan,) = _create_mwan_sensors(coordinator, entry, "wan")
    (backup,) = _create_mwan_sensors(coordinator, entry, "backup")

    assert wan.native_value == pytest.approx(98.579)
    assert wan.extra_state_attributes == {"coverage_start": coverage.isoformat()}
    # No window yet: unknown rather than a misleading 0 %.
    assert backup.native_value is None
    assert backup.extra_state_attributes == {"coverage_start": None}


def _mock_fetchers(client: UbusClient) -> None:
    """Stub every fetch get_all_data makes, so only the MWAN path matters."""
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
        "get_vpn_status",
        "get_wifi_credentials",
        "get_dhcp_leases",
        "get_lldp_neighbors",
        "get_upnp_mappings",
    ):
        setattr(client, name, AsyncMock(return_value=[]))
    client.get_local_macs = AsyncMock(return_value=set())
    client.get_local_ips = AsyncMock(return_value=set())
    client.check_packages = AsyncMock(return_value=OpenWrtPackages(mwan3=True))
    client.check_permissions = AsyncMock()
    client.is_reboot_required = AsyncMock(return_value=False)
    client._call = AsyncMock(return_value={})
    client.execute_command = AsyncMock(return_value="")
    client.read_file = AsyncMock(return_value=None)


async def test_fresh_mwan_status_is_not_replaced_by_the_medium_cache() -> None:
    """MWAN status is fetched on every update; a cached copy must not win.

    The medium tier caches its results for 180 s and restores them on the
    updates in between. MWAN status used to be part of that cache, so every
    fresh fetch was overwritten again until the next medium poll.
    """
    client = UbusClient(
        MagicMock(),
        MagicMock(),
        host="192.168.1.1",
        username="ha-user",
        password="password",
    )
    _mock_fetchers(client)
    first = [MwanStatus(interface_name="wan", status="online", uptime=100, online=100)]
    second = [MwanStatus(interface_name="wan", status="offline", uptime=160, offline=5)]
    client.get_mwan_status = AsyncMock(side_effect=[first, second])
    client.coordinator = MagicMock()
    client.coordinator.data = OpenWrtData()

    await client.get_all_data()  # first update: every tier runs, medium data cached
    data = await client.get_all_data()  # next update: fast tier only

    assert data.mwan_status is second
    assert data.mwan_status[0].status == "offline"


async def test_failed_mwan_query_keeps_the_previous_status() -> None:
    """A failed ubus query must not read as "no MWAN interfaces".

    An empty list would turn the MWAN online binary sensor off and the ratio
    unknown until the next update, as if the uplink had failed.
    """
    client = UbusClient(
        MagicMock(),
        MagicMock(),
        host="192.168.1.1",
        username="ha-user",
        password="password",
    )
    _mock_fetchers(client)
    replies: list[Any] = [
        {"interfaces": {"wan": {"status": "online", "online": 100, "uptime": 100}}},
        UbusTimeoutError("mwan3 status timed out"),
    ]

    async def _call(obj: str, method: str, *args: Any, **kwargs: Any) -> Any:
        if (obj, method) != ("mwan3", "status"):
            return {}
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    client._call = AsyncMock(side_effect=_call)
    client.coordinator = MagicMock()
    client.coordinator.data = OpenWrtData()

    client.coordinator.data = await client.get_all_data()
    previous = client.coordinator.data.mwan_status
    data = await client.get_all_data()

    assert not replies
    assert data.mwan_status is previous
    assert data.mwan_status[0].status == "online"
