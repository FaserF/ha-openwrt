"""Tests for OpenWrt static DHCP lease services."""

import shlex
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.openwrt import (
    _register_services,
)
from custom_components.openwrt.const import DATA_CLIENT, DOMAIN


@pytest.mark.asyncio
async def test_add_static_lease_service(hass) -> None:
    """Test the add_static_lease service handler."""
    mock_client = MagicMock()
    mock_client.execute_command = AsyncMock()

    hass.data = {
        DOMAIN: {
            "test_entry_id": {
                DATA_CLIENT: mock_client,
            }
        }
    }

    _register_services(hass)

    # Find the registered add_static_lease handler
    add_handler = None
    for call in hass.services.async_register.call_args_list:
        if call[0][1] == "add_static_lease":
            add_handler = call[0][2]
            break

    assert add_handler is not None

    # Call the handler with mock data
    call_data = MagicMock()
    call_data.data = {
        "entry_id": "test_entry_id",
        "mac": "aa:bb:cc:dd:ee:ff",
        "ip": "192.0.2.10",
        "name": "test-device",
    }

    await add_handler(call_data)

    # Verify execute_command was called with script containing the set commands
    mock_client.execute_command.assert_called_once()
    script = mock_client.execute_command.call_args[0][0]
    assert "uci add dhcp host" in script
    assert "uci set dhcp.@host[-1].mac=aa:bb:cc:dd:ee:ff" in script
    assert "uci set dhcp.@host[-1].ip=192.0.2.10" in script
    assert "uci set dhcp.@host[-1].name=test-device" in script
    assert "uci commit dhcp" in script


@pytest.mark.asyncio
async def test_delete_static_lease_service(hass) -> None:
    """Test the delete_static_lease service handler."""
    mock_client = MagicMock()
    mock_client.execute_command = AsyncMock()

    hass.data = {
        DOMAIN: {
            "test_entry_id": {
                DATA_CLIENT: mock_client,
            }
        }
    }

    _register_services(hass)

    # Find the registered delete_static_lease handler
    del_handler = None
    for call in hass.services.async_register.call_args_list:
        if call[0][1] == "delete_static_lease":
            del_handler = call[0][2]
            break

    assert del_handler is not None

    # Call the handler with mock data
    call_data = MagicMock()
    call_data.data = {
        "entry_id": "test_entry_id",
        "mac": "aa:bb:cc:dd:ee:ff",
    }

    await del_handler(call_data)

    # Verify execute_command was called with script containing the delete command
    mock_client.execute_command.assert_called_once()
    script = mock_client.execute_command.call_args[0][0]
    assert "m=aa:bb:cc:dd:ee:ff\n" in script
    assert "uci commit dhcp" in script


@pytest.mark.asyncio
async def test_static_lease_inputs_are_shell_quoted(hass) -> None:
    """Shell syntax in the lease fields reaches uci as data, not as commands."""
    mock_client = MagicMock()
    mock_client.execute_command = AsyncMock()
    hass.data = {DOMAIN: {"test_entry_id": {DATA_CLIENT: mock_client}}}
    _register_services(hass)
    handlers = {
        call[0][1]: call[0][2] for call in hass.services.async_register.call_args_list
    }
    fields = {"mac": '$(reboot)"; reboot; "', "ip": "`reboot`", "name": "x'; reboot; '"}

    call_data = MagicMock()
    call_data.data = {"entry_id": "test_entry_id", **fields}
    await handlers["add_static_lease"](call_data)
    await handlers["delete_static_lease"](call_data)

    add_script, del_script = (
        call[0][0] for call in mock_client.execute_command.call_args_list
    )
    for script in (add_script, del_script):
        assert shlex.split(script.splitlines()[0]) == [f"m={fields['mac']}"]
    uci_sets = [
        shlex.split(line)
        for line in add_script.splitlines()
        if line.startswith("uci set")
    ]
    assert uci_sets == [
        ["uci", "set", f"dhcp.@host[-1].{key}={value}"] for key, value in fields.items()
    ]


@pytest.mark.asyncio
async def test_uci_and_wol_inputs_are_shell_quoted(hass) -> None:
    """Shell syntax in the UCI and Wake on LAN fields is passed as data."""
    mock_client = MagicMock()
    mock_client.execute_command = AsyncMock(return_value="")
    hass.data = {DOMAIN: {"test_entry_id": {DATA_CLIENT: mock_client}}}
    _register_services(hass)
    handlers = {
        call[0][1]: call[0][2] for call in hass.services.async_register.call_args_list
    }
    evil = "x'; reboot; $(reboot)"

    async def command(service: str, data: dict) -> list[str]:
        call_data = MagicMock()
        call_data.data = data
        mock_client.execute_command.reset_mock()
        await handlers[service](call_data)
        return shlex.split(mock_client.execute_command.call_args[0][0])

    entry = {"entry_id": "test_entry_id"}
    assert await command(
        "uci_set", {**entry, "config": "c", "section": "s", "value": evil}
    ) == [
        "uci",
        "set",
        f"c.s={evil}",
        "&&",
        "uci",
        "commit",
        "c",
        "&&",
        "reload_config",
    ]
    assert await command("uci_get", {**entry, "config": evil}) == ["uci", "get", evil]
    assert await command(
        "wake_on_lan", {"target": "test_entry_id", "mac": evil, "interface": evil}
    ) == ["ether-wake", "-i", evil, evil]
