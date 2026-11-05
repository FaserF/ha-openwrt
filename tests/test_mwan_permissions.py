"""Tests for the RPC permission the MWAN3 sensors depend on."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.openwrt.api.base import (
    PROVISION_SCRIPT_TEMPLATE,
    OpenWrtData,
    OpenWrtPackages,
    OpenWrtPermissions,
)
from custom_components.openwrt.api.luci_rpc import (
    LuciRpcAuthError,
    LuciRpcClient,
    LuciRpcConnectionError,
    LuciRpcError,
)
from custom_components.openwrt.api.ubus import UbusClient, UbusPermissionError
from custom_components.openwrt.coordinator.features import FeaturesMixin

FEATURES = "custom_components.openwrt.coordinator.features"
LUCI_MWAN_STATUS_COMMAND = "ubus call mwan3 status 2>/dev/null || echo unavailable"


def _client(username: str = "ha-user") -> UbusClient:
    """Return a ubus client with an open session."""
    client = UbusClient(
        MagicMock(),
        MagicMock(),
        host="192.168.1.1",
        username=username,
        password="password",
    )
    client._session_id = "test_token"
    return client


def _ubus_without_session_list(*, mwan3_allowed: bool) -> Any:
    """Return a ubus stand-in that denies session list and maybe the mwan3 call."""

    def answer(obj: str, method: str, params: dict[str, Any] | None = None) -> dict:
        if obj == "session" or (obj == "mwan3" and not mwan3_allowed):
            msg = "Access denied"
            raise UbusPermissionError(msg)
        return {}

    return answer


def test_provisioned_acl_is_valid_and_allows_the_mwan3_status_call() -> None:
    """No other entry of the ACL covers mwan3, and rpcd needs valid JSON."""
    script = PROVISION_SCRIPT_TEMPLATE.format(
        username="homeassistant", password="secret"
    )
    match = re.search(r"^printf '(.*)' > \"\$ACL_FILE\"$", script, re.MULTILINE)
    assert match is not None

    acl = json.loads(match.group(1).replace("\\n", "\n"))

    assert acl["homeassistant"]["read"]["ubus"]["mwan3"] == ["status"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ubus_acl", "allowed"),
    [
        ({"mwan3": ["status"]}, True),
        ({"mwan3": ["*"]}, True),
        ({"*": ["*"]}, True),
        ({"mwan3": ["other"]}, False),
        ({"network": ["*"]}, False),
    ],
)
async def test_session_acl_decides_whether_mwan3_can_be_read(
    ubus_acl: dict[str, list[str]], allowed: bool
) -> None:
    """Reading the UCI config does not count: the sensors need the status call."""
    client = _client()
    session = {"acls": {"ubus": ubus_acl, "uci": {"mwan3": ["read"]}}}

    with patch.object(client, "_call", new_callable=AsyncMock, return_value=session):
        perms = await client.check_permissions()

    assert perms.read_mwan is allowed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("username", "mwan3_allowed", "expected"),
    [
        ("ha-user", True, True),
        ("ha-user", False, False),
        ("root", False, True),
    ],
)
async def test_probes_try_the_mwan3_status_call(
    username: str, mwan3_allowed: bool, expected: bool
) -> None:
    """Without session list access the call itself decides, not the UCI config."""
    client = _client(username)

    with patch.object(
        client,
        "_call",
        new_callable=AsyncMock,
        side_effect=_ubus_without_session_list(mwan3_allowed=mwan3_allowed),
    ):
        perms = await client.check_permissions()

    assert perms.read_mwan is expected


def _luci_client() -> LuciRpcClient:
    """Return a LuCI RPC client."""
    return LuciRpcClient(
        MagicMock(),
        MagicMock(),
        host="192.168.1.1",
        username="ha-user",
        password="password",
    )


def _luci_denying(*denied: str) -> Any:
    """Return a LuCI RPC stand-in that denies the given modules and mwan3 UCI."""

    def answer(module: str, method: str, params: list[Any] | None = None) -> dict:
        if module in denied or (module == "uci" and params == ["mwan3"]):
            msg = "Access denied"
            raise LuciRpcError(msg)
        return {}

    return answer


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("output", "expected"),
    [
        pytest.param('{"interfaces": {}}', True, id="status readable"),
        pytest.param("unavailable", True, id="mwan3 not installed"),
        pytest.param("", False, id="command could not run"),
    ],
)
async def test_luci_read_mwan_follows_the_status_command(
    output: str, expected: bool
) -> None:
    """LuCI RPC runs this command for the status, so UCI access does not count."""
    client = _luci_client()

    with (
        patch.object(
            client, "_rpc_call", new_callable=AsyncMock, side_effect=_luci_denying()
        ),
        patch.object(
            client, "execute_command", new_callable=AsyncMock, return_value=output
        ) as execute,
    ):
        perms = await client.check_permissions()

    assert perms.read_mwan is expected
    execute.assert_awaited_once_with(LUCI_MWAN_STATUS_COMMAND)


@pytest.mark.asyncio
async def test_luci_account_that_cannot_run_commands_cannot_read_mwan3() -> None:
    """With both ways of running a command denied, there is no status to read."""
    client = _luci_client()

    with patch.object(
        client,
        "_rpc_call",
        new_callable=AsyncMock,
        side_effect=_luci_denying("sys", "ubus"),
    ):
        perms = await client.check_permissions()

    assert perms.read_mwan is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        pytest.param(LuciRpcConnectionError("Connection lost"), True, id="connection"),
        pytest.param(LuciRpcAuthError("Access denied"), False, id="denied"),
    ],
)
async def test_luci_only_a_denial_counts_as_missing_permission(
    error: LuciRpcError, expected: bool
) -> None:
    """A lost connection says nothing about the permission and must not raise a repair."""
    client = _luci_client()

    with (
        patch.object(
            client, "_rpc_call", new_callable=AsyncMock, side_effect=_luci_denying()
        ),
        patch.object(
            client, "execute_command", new_callable=AsyncMock, side_effect=error
        ),
    ):
        perms = await client.check_permissions()

    assert perms.read_mwan is expected


@pytest.fixture
def repairs() -> Iterator[tuple[MagicMock, MagicMock]]:
    """Patch the functions that create and delete the repair issue."""
    with (
        patch(f"{FEATURES}.async_create_stale_permissions_repair") as create,
        patch(f"{FEATURES}.async_delete_stale_permissions_repair") as delete,
    ):
        yield create, delete


def _coordinator(username: str = "homeassistant") -> MagicMock:
    """Return a stand-in for the coordinator running the permission check."""
    coordinator = MagicMock()
    coordinator.config_entry.data = {"username": username}
    coordinator._last_version = None
    coordinator._stale_permissions_reason = None
    return coordinator


def _data(
    *,
    mwan3: bool | None = True,
    read_mwan: bool = True,
    read_system: bool = True,
) -> OpenWrtData:
    """Return coordinator data with the given package and permission state."""
    data = OpenWrtData()
    data.packages = OpenWrtPackages(mwan3=mwan3)
    data.permissions = OpenWrtPermissions(read_system=read_system, read_mwan=read_mwan)
    return data


def test_denied_mwan3_status_raises_a_repair(
    repairs: tuple[MagicMock, MagicMock],
) -> None:
    """An ACL from before the fix only changes when the user is provisioned again."""
    create, delete = repairs

    FeaturesMixin._async_check_stale_permissions(_coordinator(), _data(read_mwan=False))

    create.assert_called_once()
    delete.assert_not_called()


@pytest.mark.parametrize(
    ("mwan3", "read_mwan"),
    [(True, True), (False, False), (None, False)],
)
def test_no_repair_unless_mwan3_is_installed_and_denied(
    repairs: tuple[MagicMock, MagicMock], mwan3: bool | None, read_mwan: bool
) -> None:
    """A router without mwan3 does not need the permission."""
    create, delete = repairs

    FeaturesMixin._async_check_stale_permissions(
        _coordinator(), _data(mwan3=mwan3, read_mwan=read_mwan)
    )

    create.assert_not_called()
    delete.assert_called_once()


def test_root_never_gets_the_repair(repairs: tuple[MagicMock, MagicMock]) -> None:
    """Root is not limited by an ACL that provisioning could repair."""
    create, delete = repairs

    FeaturesMixin._async_check_stale_permissions(
        _coordinator("root"), _data(read_mwan=False)
    )

    create.assert_not_called()
    delete.assert_called_once()


def test_missing_system_permission_is_still_reported(
    repairs: tuple[MagicMock, MagicMock], caplog: pytest.LogCaptureFixture
) -> None:
    """The existing check keeps working, and names the sensors it affects."""
    create, _ = repairs

    with caplog.at_level(logging.WARNING, logger=FEATURES):
        FeaturesMixin._async_check_stale_permissions(
            _coordinator(), _data(read_system=False)
        )

    create.assert_called_once()
    assert "system sensors" in caplog.text


def test_missing_permission_is_logged_once_per_occurrence(
    repairs: tuple[MagicMock, MagicMock], caplog: pytest.LogCaptureFixture
) -> None:
    """The check runs on every update, so a lasting problem must not flood the log."""
    create, _ = repairs
    coordinator = _coordinator()

    def warnings_after_update(read_mwan: bool) -> int:
        FeaturesMixin._async_check_stale_permissions(
            coordinator, _data(read_mwan=read_mwan)
        )
        return sum("mwan3 status" in r.getMessage() for r in caplog.records)

    with caplog.at_level(logging.WARNING, logger=FEATURES):
        assert warnings_after_update(read_mwan=False) == 1
        assert warnings_after_update(read_mwan=False) == 1
        # The repair stays in place while the problem lasts.
        assert create.call_count == 2
        assert warnings_after_update(read_mwan=True) == 1
        # Fixed and lost again, for example by a firmware upgrade.
        assert warnings_after_update(read_mwan=False) == 2
