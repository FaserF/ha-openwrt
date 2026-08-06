"""Test one-shot service detection for the service switches.

procd only reports ``running`` for services that keep a resident process. A
one-shot init script (adblock-fast, firewall, sysctl, custom nftables scripts)
applies its config and exits, so ``rc list`` reports ``running: false`` forever
and a switch bound to that field can never turn on.

The fixtures below are taken verbatim from a live OpenWrt 25.12.5 router
(GL.iNet GL-MT6000).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.openwrt.api.base import ServiceInfo, classify_service

# --- Real payloads from `ubus call rc list` -------------------------------
# Note: `rc list` carries no `exit_code` on this firmware (0 occurrences across
# all 46 entries), which is why the old heuristic -- which read exit_code from
# the rc view -- could never fire. exit_code lives in `service list` instead.
RC_LIST = {
    "dnsmasq": {"start": 19, "enabled": True, "running": True},
    "nlbwmon": {"start": 80, "enabled": True, "running": True},
    "adblock-fast": {"start": 20, "enabled": True, "running": False},
    "nft-limiter": {"start": 99, "enabled": True, "running": False},
    "firewall": {"start": 19, "enabled": True, "running": False},
    "sysctl": {"start": 11, "enabled": True},  # no "running" key at all
    "ubihealthd": {"start": 20, "enabled": True, "running": False},
    "disabled-daemon": {"start": 50, "enabled": False, "running": False},
}

# --- Real payloads from `ubus call service list` --------------------------
SERVICE_LIST = {
    "dnsmasq": {"instances": {"cfg01411c": {"running": True, "pid": 3011}}},
    "nlbwmon": {"instances": {"instance1": {"running": True, "pid": 5807}}},
    # Publishes status data but owns no procd instance -> one-shot.
    "adblock-fast": {"data": {"entries": 119929, "status": "statusSuccess"}},
    "nft-limiter": {},
    "firewall": {},
    # "sysctl" is absent from service list entirely.
    # procd keeps the instance registered for a daemon that is down; exit_code 1
    # marks it as failed/stopped rather than a completed one-shot.
    "ubihealthd": {"instances": {"instance1": {"running": False, "exit_code": 1}}},
    "disabled-daemon": {"instances": {"instance1": {"running": False}}},
    # Registered an instance and exited 0 -> one-shot that already ran.
    "usbmode": {"instances": {"instance1": {"running": False, "exit_code": 0}}},
}
RC_LIST["usbmode"] = {"start": 20, "enabled": True, "running": False}


def _classify(name: str) -> ServiceInfo:
    return classify_service(name, RC_LIST[name], SERVICE_LIST.get(name))


@pytest.mark.parametrize("name", ["dnsmasq", "nlbwmon"])
def test_running_daemon_is_on(name: str) -> None:
    """A daemon with a live procd instance stays on and is not a one-shot."""
    svc = _classify(name)
    assert svc.running is True
    assert svc.one_shot is False


@pytest.mark.parametrize("name", ["adblock-fast", "nft-limiter", "firewall"])
def test_enabled_one_shot_is_on(name: str) -> None:
    """The regression: enabled one-shots must report on, not off."""
    svc = _classify(name)
    assert svc.one_shot is True
    assert svc.enabled is True
    assert svc.running is True, f"{name} should read as on when enabled"


def test_one_shot_without_running_key() -> None:
    """sysctl has no 'running' key and is absent from service list."""
    svc = _classify("sysctl")
    assert svc.one_shot is True
    assert svc.running is True


def test_stopped_daemon_stays_off() -> None:
    """A daemon that is down keeps its procd instance, so it must stay off.

    This is what stops the one-shot fallback from reporting every enabled
    service as on. exit_code 1 marks it failed rather than completed.
    """
    svc = _classify("ubihealthd")
    assert svc.one_shot is False
    assert svc.enabled is True
    assert svc.running is False


def test_completed_one_shot_with_instance_is_on() -> None:
    """An instance that exited 0 is a one-shot that already did its work."""
    svc = _classify("usbmode")
    assert svc.one_shot is True
    assert svc.running is True


def test_disabled_one_shot_is_off() -> None:
    """A one-shot that is not enabled at boot reads as off."""
    svc = classify_service("nft-limiter", {"enabled": False, "running": False}, {})
    assert svc.one_shot is True
    assert svc.running is False


def test_missing_service_entry_treated_as_one_shot() -> None:
    """No service-list data at all still yields the enabled-based fallback."""
    svc = classify_service("custom-script", {"enabled": True, "running": False}, None)
    assert svc.one_shot is True
    assert svc.running is True


def test_malformed_service_entry_does_not_raise() -> None:
    """Defensive: service list values are not guaranteed to be dicts."""
    svc = classify_service("weird", {"enabled": True}, "not-a-dict")
    assert svc.one_shot is True
    assert svc.running is True


@pytest.mark.asyncio
async def test_one_shot_toggle_also_flips_boot_flag() -> None:
    """Turning a one-shot on must enable it, or the next poll reverts it."""
    from custom_components.openwrt.switch import OpenWrtServiceSwitch

    coordinator = MagicMock()
    coordinator.data.services = [
        ServiceInfo(name="nft-limiter", enabled=False, running=False, one_shot=True),
    ]
    entry = MagicMock()
    entry.entry_id = "e1"
    entry.unique_id = "router1"
    client = AsyncMock()

    switch = OpenWrtServiceSwitch(coordinator, entry, client, "nft-limiter")
    switch.async_write_ha_state = MagicMock()

    await switch.async_turn_on()

    actions = [c.args[1] for c in client.manage_service.call_args_list]
    assert actions == ["enable", "start"]
    assert switch.is_on is True

    client.manage_service.reset_mock()
    await switch.async_turn_off()
    actions = [c.args[1] for c in client.manage_service.call_args_list]
    assert actions == ["disable", "stop"]
    assert switch.is_on is False


@pytest.mark.asyncio
async def test_daemon_toggle_keeps_start_stop_only() -> None:
    """A normal daemon must not have its boot flag touched."""
    from custom_components.openwrt.switch import OpenWrtServiceSwitch

    coordinator = MagicMock()
    coordinator.data.services = [
        ServiceInfo(name="dnsmasq", enabled=True, running=True, one_shot=False),
    ]
    entry = MagicMock()
    entry.entry_id = "e1"
    entry.unique_id = "router1"
    client = AsyncMock()

    switch = OpenWrtServiceSwitch(coordinator, entry, client, "dnsmasq")
    switch.async_write_ha_state = MagicMock()

    await switch.async_turn_off()

    actions = [c.args[1] for c in client.manage_service.call_args_list]
    assert actions == ["stop"]
