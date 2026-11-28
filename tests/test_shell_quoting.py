"""Values put into router shell commands stay one shell word.

H stands in for a value read from the router or typed into an action. It has
no whitespace, so it survives the whitespace splitting used to parse router
output, and it carries a quote, `;` and `$()`, so it only stays in one word
when the command quotes it.
"""

import json
import os
import shlex
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import voluptuous as vol

from custom_components.openwrt import _register_services
from custom_components.openwrt.api.base import SystemResources
from custom_components.openwrt.api.luci_rpc import LuciRpcClient
from custom_components.openwrt.api.provisioning import build_provision_script
from custom_components.openwrt.api.ssh import SshClient
from custom_components.openwrt.api.ubus import UbusClient, UbusError
from custom_components.openwrt.buttons.base import OpenWrtWakeOnLanButton
from custom_components.openwrt.const import DATA_CLIENT, DOMAIN
from custom_components.openwrt.helpers.gps import async_update_gps_location
from custom_components.openwrt.number import OpenWrtTxPowerNumber
from custom_components.openwrt.switches.hardware import OpenWrtWireGuardSwitch
from custom_components.openwrt.update import (
    OpenWrtPackageUpdateEntity,
    OpenWrtUpdateEntity,
)

H = "x';$(reboot);'"
KWARGS = {"host": "192.0.2.1", "username": "root", "password": "password"}


def words(cmd: str) -> list[str]:
    """Split like a shell: on whitespace and on ; & | ( ) < > outside quotes."""
    lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    return list(lex)


def recorder(*responses: tuple[str, str]) -> AsyncMock:
    """Record commands; answer with the output of the first matching needle."""

    async def run(cmd, *_args, **_kwargs):
        for needle, out in responses:
            if needle in cmd:
                return out
        return ""

    return AsyncMock(side_effect=run)


def commands(*mocks: AsyncMock) -> list[str]:
    """Every shell command passed to the mocks, sys.exec payloads included."""
    found = []
    for mock in mocks:
        for call in mock.call_args_list:
            args = call.args
            if args[:2] == ("sys", "exec"):
                found.append(args[2][0])
            elif args and isinstance(args[0], str):
                found.append(args[0])
    return found


def assert_quoted(*mocks: AsyncMock) -> None:
    """Every copy of H in every command stays whole inside one word."""
    carrying = [cmd for cmd in commands(*mocks) if "reboot" in cmd]
    assert carrying, "no command received the value"
    for cmd in carrying:
        assert sum(word.count(H) for word in words(cmd)) == cmd.count("reboot"), cmd


def luci() -> LuciRpcClient:
    client = LuciRpcClient(MagicMock(), MagicMock(), **KWARGS)
    client._rpc_call = AsyncMock(return_value=None)
    return client


def handler(hass: MagicMock, name: str):
    hass.services.async_register = MagicMock()
    _register_services(hass)
    for call in hass.services.async_register.call_args_list:
        if call.args[1] == name:
            return call.args[2]
    raise AssertionError(name)


# Action inputs


@pytest.mark.asyncio
async def test_manage_service_rejects_a_path(hass: MagicMock) -> None:
    """A service name is a script in /etc/init.d, never a path out of it."""
    client = MagicMock(manage_service=AsyncMock())
    hass.data = {DOMAIN: {"e": {DATA_CLIENT: client}}}
    handle = handler(hass, "manage_service")

    with pytest.raises(vol.Invalid):
        await handle(
            SimpleNamespace(
                data={
                    "entry_id": "e",
                    "service_name": "../../sbin/reboot",
                    "action": "start",
                }
            )
        )
    client.manage_service.assert_not_called()

    data = {"entry_id": "e", "service_name": "adblock-fast", "action": "stop"}
    await handle(SimpleNamespace(data=data))
    client.manage_service.assert_awaited_once_with("adblock-fast", "stop")


@pytest.mark.asyncio
async def test_wake_on_lan_action_fallback(hass: MagicMock) -> None:
    """The etherwake retry passes the same arguments, untouched."""
    client = MagicMock(execute_command=recorder(("ether-wake", "not found")))
    hass.data = {DOMAIN: {"e": {DATA_CLIENT: client}}}
    handle = handler(hass, "wake_on_lan")
    mac = "02:00:00:00:00:01"

    await handle(
        SimpleNamespace(data={"target": "e", "mac": mac, "interface": "ether-wake0"})
    )
    assert [words(c) for c in commands(client.execute_command)] == [
        ["ether-wake", "-i", "ether-wake0", mac],
        ["etherwake", "-i", "ether-wake0", mac],
    ]


@pytest.mark.asyncio
async def test_wake_on_lan_button() -> None:
    """The button quotes the interface and keeps it through the fallback."""
    mac = "02:00:00:00:00:01"
    button = SimpleNamespace(
        _client=MagicMock(execute_command=recorder(("ether-wake", "not found"))),
        _mac=mac,
        _interface=H,
    )
    await OpenWrtWakeOnLanButton.async_press(button)
    assert [words(c) for c in commands(button._client.execute_command)] == [
        ["ether-wake", "-i", H, mac],
        ["etherwake", "-i", H, mac],
    ]

    button._client.execute_command = recorder(("ether-wake", "not found"))
    button._interface = "ether-wake0"
    await OpenWrtWakeOnLanButton.async_press(button)
    assert words(commands(button._client.execute_command)[1]) == [
        "etherwake",
        "-i",
        "ether-wake0",
        mac,
    ]


@pytest.mark.asyncio
async def test_backup_action_removes_the_quoted_path(hass: MagicMock, tmp_path) -> None:
    """The remote backup path is quoted when it is removed."""
    client = MagicMock(
        create_backup=AsyncMock(return_value=f"/tmp/{H}.tar.gz"),
        download_file=AsyncMock(return_value=True),
        execute_command=recorder(),
    )
    hass.data = {DOMAIN: {"e": {DATA_CLIENT: client}}}
    hass.config.path = MagicMock(side_effect=lambda *a: os.path.join(tmp_path, *a))
    await handler(hass, "create_backup")(
        SimpleNamespace(data={"entry_id": "e", "download_path": str(tmp_path)})
    )
    assert_quoted(client.execute_command)


@pytest.mark.asyncio
async def test_update_backup_and_package_install(tmp_path) -> None:
    """The firmware backup cleanup and package install quote their values."""
    client = MagicMock(
        create_backup=AsyncMock(return_value=f"/tmp/{H}.tar.gz"),
        download_file=AsyncMock(return_value=True),
        execute_command=recorder(),
    )
    coordinator = MagicMock(client=client, async_request_refresh=AsyncMock())
    hass = MagicMock()
    hass.config.path = MagicMock(side_effect=lambda *a: os.path.join(tmp_path, *a))

    await OpenWrtUpdateEntity._async_perform_backup(
        SimpleNamespace(coordinator=coordinator, hass=hass)
    )
    assert_quoted(client.execute_command)

    client.execute_command = recorder()
    await OpenWrtPackageUpdateEntity.async_install(
        SimpleNamespace(coordinator=coordinator, _package_name=H), None, False
    )
    assert_quoted(client.execute_command)


@pytest.mark.asyncio
async def test_gps_port() -> None:
    """The serial port from the options flow is quoted."""
    client = MagicMock(execute_command=recorder(("command -v", "/usr/bin/x")))
    await async_update_gps_location(MagicMock(), client, f"/dev/{H}")
    assert_quoted(client.execute_command)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("client_cls", "transport"),
    [
        (SshClient, "_exec"),
        (LuciRpcClient, "execute_command"),
        (UbusClient, "execute_command"),
    ],
)
async def test_provisioning_keeps_a_quote_in_the_password(
    client_cls, transport
) -> None:
    """A ' in the config entry password stays inside PASS='...'."""
    client = client_cls(MagicMock(), MagicMock(), **KWARGS)
    send = recorder()
    setattr(client, transport, send)
    client._call = AsyncMock(return_value={})  # ubus tmp-file fallback
    await client.provision_user("homeassistant", H)
    script = commands(send)[0]
    line = next(ln for ln in script.splitlines() if ln.startswith("PASS="))
    assert words(line) == [f"PASS={H}"]
    assert "USER='homeassistant'" in build_provision_script("homeassistant", "pw")


# Values read from the router


@pytest.mark.asyncio
async def test_entities_quote_router_names() -> None:
    """WireGuard switch and TX power number quote the names they were given."""
    client = MagicMock(execute_command=recorder())
    switch = SimpleNamespace(
        _client=client,
        _iface_name=H,
        coordinator=MagicMock(data=None),
        async_write_ha_state=MagicMock(),
    )
    await OpenWrtWireGuardSwitch.async_turn_on(switch)
    await OpenWrtWireGuardSwitch.async_turn_off(switch)
    assert len(commands(client.execute_command)) == 2
    assert_quoted(client.execute_command)

    client.execute_command = recorder()
    number = SimpleNamespace(
        hass=MagicMock(data={DOMAIN: {"e": {DATA_CLIENT: client}}}),
        _entry=MagicMock(entry_id="e"),
        _radio=H,
        coordinator=MagicMock(async_request_refresh=AsyncMock()),
    )
    await OpenWrtTxPowerNumber.async_set_native_value(number, 15)
    assert_quoted(client.execute_command)


@pytest.mark.asyncio
async def test_luci_interface_wireless_and_firewall() -> None:
    """LuCI commands built from interface and section names."""
    client = luci()
    client.execute_command = recorder()
    client._resolve_wireless_section_luci = AsyncMock(return_value=H)
    for action in ("reconnect", "up", "down"):
        await client.manage_interface(H, action)
    await client.set_wireless_enabled("wlan0", True)
    await client.trigger_wps_push(H)
    await client.set_firewall_rule_enabled(H, True)
    await client.set_firewall_redirect_enabled(H, False)
    await client.set_led(H, 1)
    assert len(commands(client.execute_command)) == 9
    assert_quoted(client.execute_command)

    async def no_wps_start(cmd):
        if "wps_start" in cmd:
            raise RuntimeError("Method not found")
        return ""

    client.execute_command = AsyncMock(side_effect=no_wps_start)
    await client.trigger_wps_push(H)
    assert "wps_push" in commands(client.execute_command)[1]
    assert_quoted(client.execute_command)


@pytest.mark.asyncio
async def test_luci_reads_with_router_names() -> None:
    """LuCI reads that put router-provided names into commands."""
    client = luci()
    client._rpc_call = AsyncMock(
        return_value={"lan": {".type": "interface", "device": H}}
    )
    client.execute_command = recorder()
    await client.get_network_interfaces()
    assert_quoted(client.execute_command)

    client = luci()
    client.execute_command = recorder(
        ("iwinfo devices", json.dumps({"devices": [H]})),
        ("iwinfo info", '{"ssid": "s"}'),
        ("assoclist", '{"results": []}'),
    )
    await client.get_wireless_interfaces()
    sent = " ".join(commands(client.execute_command))
    assert "iwinfo info" in sent
    assert "get_clients" in sent
    assert_quoted(client.execute_command)

    client = luci()
    client._get_wireless_mapping = AsyncMock()
    client.get_dnsmasq_lease_configs = AsyncMock(return_value=[(f"/tmp/{H}", "lan")])
    client.execute_command = recorder(
        ("iwinfo 2>/dev/null", H),
        ("network.device status", json.dumps({H: {"up": True}})),
    )
    await client.get_connected_devices()
    await client._process_bridge_fdb({})
    sent = " ".join(commands(client.execute_command))
    assert "iwinfo assoclist" in sent
    assert "network.device fdb" in sent
    assert_quoted(client.execute_command)

    client = luci()
    client.dhcp_software = "dnsmasq"
    client.get_dnsmasq_lease_configs = AsyncMock(return_value=[(f"/tmp/{H}", "lan")])
    await client.get_dhcp_leases()
    assert_quoted(client._rpc_call)

    client = luci()
    client.execute_command = recorder()
    await client.user_exists(H)
    assert_quoted(client.execute_command)


@pytest.mark.asyncio
async def test_ssh_reads_with_router_names() -> None:
    """SSH and shared-base commands built from router-provided names."""
    client = SshClient(MagicMock(), MagicMock(), **KWARGS)
    client._exec = recorder(
        ("ubus list", f"hostapd.{H}"),
        ("thermal_zone", f"/sys/class/thermal/{H}"),
    )
    client.execute_command = recorder(
        ("ip route show default", f"default via {H} dev eth0"),
        ("wg show", f"{H}\tpriv\tpub\t51820\toff"),
        ("pgrep", "1 openvpn"),
        ("type tun", f"{H} UP"),
    )
    client.get_dnsmasq_lease_configs = AsyncMock(return_value=[(f"/tmp/{H}", "lan")])

    await client.get_wps_status()
    await client.set_wps(True)
    await client._fetch_dynamic_thermals_ssh(SystemResources())
    await client._get_leases_dnsmasq([])
    await client.set_led(H, 0)
    await client.get_gateway_mac()
    await client.get_vpn_status()
    await client.user_exists(H)
    await client.kick_device("02:00:00:00:00:01", H)

    sent = commands(client._exec, client.execute_command)
    assert sum("reboot" in cmd for cmd in sent) >= 15
    assert_quoted(client._exec, client.execute_command)


@pytest.mark.asyncio
async def test_ubus_reads_with_router_names() -> None:
    """Ubus shell fallbacks built from router-provided names."""
    client = UbusClient(MagicMock(), MagicMock(), **KWARGS)
    client._call = AsyncMock(side_effect=UbusError("no"))
    client.execute_command = recorder()
    client.dhcp_software = "dnsmasq"
    client.get_dnsmasq_lease_configs = AsyncMock(return_value=[(f"/tmp/{H}", "lan")])

    await client._get_mac_from_ip(H)
    await client.get_dhcp_leases()
    await client.set_led(H, 1)
    assert len(commands(client.execute_command)) == 4
    assert_quoted(client.execute_command)
