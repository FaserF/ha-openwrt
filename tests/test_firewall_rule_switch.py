"""Tests for OpenWrt firewall rule switch entity."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.openwrt.api.base import FirewallRule, OpenWrtData
from custom_components.openwrt.switch import OpenWrtFirewallRuleSwitch


@pytest.mark.asyncio
async def test_firewall_rule_switch_turn_on_off_success() -> None:
    """Test successful turn_on and turn_off on OpenWrtFirewallRuleSwitch."""
    rule = FirewallRule(
        section_id="rule_1",
        name="Allow-SSH",
        enabled=False,
        src="wan",
        dest="lan",
        target="ACCEPT",
    )
    coordinator = MagicMock()
    coordinator.data = OpenWrtData(firewall_rules=[rule])
    coordinator.async_request_refresh = MagicMock()

    client = MagicMock()
    client.set_firewall_rule_enabled = AsyncMock(return_value=True)

    entry = MagicMock()
    entry.entry_id = "test_entry"

    entity = OpenWrtFirewallRuleSwitch(
        coordinator, entry, client, "rule_1", "Allow-SSH"
    )
    entity.async_write_ha_state = MagicMock()

    assert entity.is_on is False

    await entity.async_turn_on()

    client.set_firewall_rule_enabled.assert_called_once_with("rule_1", True)
    assert rule.enabled is True
    entity.async_write_ha_state.assert_called_once()
    coordinator.hass.async_create_task.assert_called_once()

    client.set_firewall_rule_enabled.reset_mock()
    entity.async_write_ha_state.reset_mock()
    coordinator.hass.async_create_task.reset_mock()

    await entity.async_turn_off()

    client.set_firewall_rule_enabled.assert_called_once_with("rule_1", False)
    assert rule.enabled is False
    entity.async_write_ha_state.assert_called_once()
    coordinator.hass.async_create_task.assert_called_once()


@pytest.mark.asyncio
async def test_firewall_rule_switch_turn_on_falsy_return() -> None:
    """Test turn_on raises HomeAssistantError when set_firewall_rule_enabled returns False."""
    rule = FirewallRule(
        section_id="rule_1",
        name="Allow-SSH",
        enabled=False,
    )
    coordinator = MagicMock()
    coordinator.data = OpenWrtData(firewall_rules=[rule])

    client = MagicMock()
    client.set_firewall_rule_enabled = AsyncMock(return_value=False)

    entry = MagicMock()
    entry.entry_id = "test_entry"

    entity = OpenWrtFirewallRuleSwitch(
        coordinator, entry, client, "rule_1", "Allow-SSH"
    )
    entity.async_write_ha_state = MagicMock()

    with pytest.raises(HomeAssistantError) as exc_info:
        await entity.async_turn_on()

    assert "Router rejected request" in str(exc_info.value)
    assert rule.enabled is False
    entity.async_write_ha_state.assert_not_called()


@pytest.mark.asyncio
async def test_firewall_rule_switch_turn_off_falsy_return() -> None:
    """Test turn_off raises HomeAssistantError when set_firewall_rule_enabled returns False."""
    rule = FirewallRule(
        section_id="rule_1",
        name="Allow-SSH",
        enabled=True,
    )
    coordinator = MagicMock()
    coordinator.data = OpenWrtData(firewall_rules=[rule])

    client = MagicMock()
    client.set_firewall_rule_enabled = AsyncMock(return_value=False)

    entry = MagicMock()
    entry.entry_id = "test_entry"

    entity = OpenWrtFirewallRuleSwitch(
        coordinator, entry, client, "rule_1", "Allow-SSH"
    )
    entity.async_write_ha_state = MagicMock()

    with pytest.raises(HomeAssistantError) as exc_info:
        await entity.async_turn_off()

    assert "Router rejected request" in str(exc_info.value)
    assert rule.enabled is True
    entity.async_write_ha_state.assert_not_called()


@pytest.mark.asyncio
async def test_firewall_rule_switch_exception_handling() -> None:
    """Test turn_on/off raises HomeAssistantError when set_firewall_rule_enabled raises an Exception."""
    rule = FirewallRule(
        section_id="rule_1",
        name="Allow-SSH",
        enabled=False,
    )
    coordinator = MagicMock()
    coordinator.data = OpenWrtData(firewall_rules=[rule])

    client = MagicMock()
    client.set_firewall_rule_enabled = AsyncMock(
        side_effect=RuntimeError("Connection lost")
    )

    entry = MagicMock()
    entry.entry_id = "test_entry"

    entity = OpenWrtFirewallRuleSwitch(
        coordinator, entry, client, "rule_1", "Allow-SSH"
    )
    entity.async_write_ha_state = MagicMock()

    with pytest.raises(HomeAssistantError) as exc_info:
        await entity.async_turn_on()

    assert "Failed to enable firewall rule" in str(exc_info.value)
    assert rule.enabled is False
    entity.async_write_ha_state.assert_not_called()
