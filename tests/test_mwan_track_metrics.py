"""Tests for the latency and packet loss of MWAN3 interfaces."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.sensor import SensorStateClass

from custom_components.openwrt.api.base import (
    MwanStatus,
    OpenWrtData,
    parse_mwan_track_metrics,
)
from custom_components.openwrt.api.luci_rpc import LuciRpcClient
from custom_components.openwrt.api.ssh import SshClient
from custom_components.openwrt.api.ubus import UbusClient
from custom_components.openwrt.sensors.mwan import OpenWrtMwanMetricSensor

NO_REPLY = 999999


def _target(status: str, latency: Any = 0, packetloss: Any = 0) -> dict[str, Any]:
    """Return one entry of mwan3's track_ip list."""
    return {
        "ip": "192.0.2.1",
        "status": status,
        "latency": latency,
        "packetloss": packetloss,
    }


# Shape of `ubus call mwan3 status` on mwan3 3.6.11: "wan" as reported without
# check_quality, "backup" as it reads with check_quality enabled.
MWAN_REPLY = {
    "interfaces": {
        "wan": {
            "online": 2872,
            "offline": 0,
            "uptime": 24514,
            "status": "online",
            "enabled": True,
            "tracking": "active",
            "check_quality": False,
            "track_ip": [
                _target("up"),
                _target("skipped"),
                _target("up"),
            ],
        },
        "backup": {
            "online": 136615,
            "offline": 0,
            "uptime": 136653,
            "status": "online",
            "enabled": True,
            "tracking": "active",
            "check_quality": True,
            "track_ip": [
                _target("up", 40, 0),
                _target("skipped", 300, 100),
                _target("up", 50, 0),
            ],
        },
    },
    "policies": {"ipv4": {}, "ipv6": {}},
}


@pytest.mark.parametrize(
    ("iface_data", "expected"),
    [
        pytest.param(
            {"check_quality": False, "track_ip": [_target("up"), _target("skipped")]},
            (None, None),
            id="without check_quality the zeros are no measurement",
        ),
        pytest.param(
            {"check_quality": False, "track_ip": [_target("down"), _target("down")]},
            (None, None),
            id="without check_quality a dead line also reads 0",
        ),
        pytest.param(
            {"track_ip": [_target("up"), _target("skipped")]},
            (None, None),
            id="version without the field, only zeros",
        ),
        pytest.param(
            {"track_ip": [_target("up", 12, 0), _target("up", 18, 0)]},
            (15.0, 0.0),
            id="version without the field, measuring",
        ),
        pytest.param(
            {"track_ip": [_target("down", NO_REPLY, 100)]},
            (None, 100.0),
            id="version without the field, dead line",
        ),
        pytest.param(
            {
                "check_quality": True,
                "track_ip": [
                    _target("up", 12, 0),
                    _target("up", 18, 0),
                    _target("skipped", 500, 100),
                ],
            },
            (15.0, 0.0),
            id="skipped targets keep old values and do not count",
        ),
        pytest.param(
            {
                "check_quality": True,
                "track_ip": [
                    _target("up", 20, 0),
                    _target("down", NO_REPLY, 100),
                ],
            },
            (20.0, 50.0),
            id="a silent target counts as loss, not as latency",
        ),
        pytest.param(
            {
                "check_quality": True,
                "track_ip": [_target("down", 1500, 0), _target("up", 100, 0)],
            },
            (800.0, 0.0),
            id="a target that answers too slowly counts with its latency",
        ),
        pytest.param(
            {
                "check_quality": True,
                "track_ip": [
                    _target("down", NO_REPLY, 100),
                    _target("down", NO_REPLY, 100),
                ],
            },
            (None, 100.0),
            id="dead line has loss but no latency",
        ),
        pytest.param(
            {
                "check_quality": True,
                "track_ip": [
                    _target("up", 11, 10),
                    _target("up", 12, 10),
                    _target("up", 14, 20),
                ],
            },
            (12.3, 13.3),
            id="rounded to one decimal",
        ),
        pytest.param(
            {"check_quality": True, "track_ip": [_target("unknown", 5, 5)]},
            (None, None),
            id="nothing probed yet",
        ),
        pytest.param({"check_quality": True, "track_ip": []}, (None, None), id="empty"),
        pytest.param(
            {"check_quality": True, "track_ip": None}, (None, None), id="null"
        ),
        pytest.param({"check_quality": True}, (None, None), id="no targets"),
        pytest.param({}, (None, None), id="tracking disabled"),
        pytest.param(
            {
                "check_quality": True,
                "track_ip": [
                    "unexpected",
                    {"status": "up"},
                    _target("up", None, None),
                ],
            },
            (None, None),
            id="malformed targets must not abort the status reply",
        ),
    ],
)
def test_parse_mwan_track_metrics(
    iface_data: dict[str, Any], expected: tuple[float | None, float | None]
) -> None:
    """Only real measurements become values."""
    assert parse_mwan_track_metrics(iface_data) == expected


def _assert_metrics(statuses: list[MwanStatus]) -> None:
    """Check the metrics parsed from MWAN_REPLY."""
    by_name = {status.interface_name: status for status in statuses}
    assert (by_name["wan"].latency, by_name["wan"].packet_loss) == (None, None)
    assert (by_name["backup"].latency, by_name["backup"].packet_loss) == (45.0, 0.0)


async def test_ubus_reads_the_metrics_from_the_tracking_targets() -> None:
    """The reply has no interface-level metrics, only the track_ip entries."""
    client = UbusClient(
        MagicMock(),
        MagicMock(),
        host="192.168.1.1",
        username="ha-user",
        password="password",
    )
    client._session_id = "test_token"

    with patch.object(client, "_call", new_callable=AsyncMock, return_value=MWAN_REPLY):
        statuses, _ = await client.get_mwan_status()

    _assert_metrics(statuses)


@pytest.mark.parametrize(
    ("client_class", "runner"),
    [(LuciRpcClient, "execute_command"), (SshClient, "_exec")],
)
async def test_shell_backends_read_the_metrics_from_the_tracking_targets(
    client_class: type[LuciRpcClient] | type[SshClient], runner: str
) -> None:
    """LuCI RPC and SSH read the same reply."""
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
        statuses, _ = await client.get_mwan_status()

    _assert_metrics(statuses)


def _sensor(
    metric: str, *statuses: MwanStatus, pkgs: Any = None, iface: str = "wan"
) -> OpenWrtMwanMetricSensor:
    """Create a metric sensor over the given MWAN3 interfaces."""
    data = OpenWrtData()
    data.mwan_status = list(statuses)
    coordinator = MagicMock()
    coordinator.data = data
    coordinator.last_update_success = True
    entry = MagicMock()
    entry.entry_id = "test"
    return OpenWrtMwanMetricSensor(
        coordinator, entry, iface, metric, pkgs or MagicMock(mwan3=True)
    )


def test_sensor_shows_a_measurement() -> None:
    """A measured value is shown, kept as statistics and enabled by default."""
    status = MwanStatus(interface_name="wan", latency=15.0, packet_loss=0.0)

    latency = _sensor("latency", status)
    loss = _sensor("packet_loss", status)

    assert (latency.native_value, latency.available) == (15.0, True)
    # 0 % loss is a measurement, unlike a missing value.
    assert (loss.native_value, loss.available) == (0.0, True)
    assert latency.entity_registry_enabled_default is True
    assert latency._attr_state_class == SensorStateClass.MEASUREMENT
    assert loss._attr_state_class == SensorStateClass.MEASUREMENT


def test_sensor_without_a_measurement_is_unavailable() -> None:
    """Without check_quality the sensor is not stuck on unknown, and starts disabled."""
    sensor = _sensor("latency", MwanStatus(interface_name="wan"))

    assert sensor.native_value is None
    assert sensor.available is False
    assert sensor.entity_registry_enabled_default is False


def test_sensors_on_a_dead_line_with_check_quality() -> None:
    """A dead line has a packet loss, but no latency to show."""
    status = MwanStatus(interface_name="wan", latency=None, packet_loss=100.0)

    assert _sensor("latency", status).available is False
    loss = _sensor("packet_loss", status)
    assert (loss.native_value, loss.available) == (100.0, True)


def test_sensor_is_unavailable_when_the_router_cannot_be_reached() -> None:
    """A failed update wins over the last known value."""
    sensor = _sensor(
        "latency", MwanStatus(interface_name="wan", latency=15.0, packet_loss=0.0)
    )
    sensor.coordinator.last_update_success = False

    assert sensor.available is False


def test_sensor_stays_disabled_by_default_without_mwan3_package() -> None:
    """The package check keeps applying when a value is present."""
    sensor = _sensor(
        "latency",
        MwanStatus(interface_name="wan", latency=15.0),
        pkgs=MagicMock(mwan3=False),
    )

    assert sensor.entity_registry_enabled_default is False


async def test_reply_without_check_quality_reaches_the_sensor_as_unavailable() -> None:
    """From the ubus reply to the sensor, zeros never show up as a value."""
    client = UbusClient(
        MagicMock(),
        MagicMock(),
        host="192.168.1.1",
        username="ha-user",
        password="password",
    )
    client._session_id = "test_token"
    with patch.object(client, "_call", new_callable=AsyncMock, return_value=MWAN_REPLY):
        statuses, _ = await client.get_mwan_status()

    assert _sensor("packet_loss", *statuses).available is False
    measured = _sensor("packet_loss", *statuses, iface="backup")
    assert (measured.native_value, measured.available) == (0.0, True)
