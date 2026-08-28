"""Test the OpenWrt MQTT presence detection integration."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME

from custom_components.openwrt.const import (
    CONF_MQTT_BROKER,
    CONF_MQTT_PASSWORD,
    CONF_MQTT_PORT,
    CONF_MQTT_PRESENCE,
    CONF_MQTT_USERNAME,
    CONF_REDEPLOY_MQTT,
    DOMAIN,
)
from custom_components.openwrt.device_tracker import async_setup_entry


@pytest.fixture
def mock_config_entry():
    """Mock config entry."""
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.unique_id = "11:22:33:44:55:66"
    entry.data = {
        CONF_HOST: "192.168.1.1",
        CONF_USERNAME: "root",
        CONF_PASSWORD: "password",
    }
    entry.options = {}
    entry.add_to_hass = MagicMock()
    return entry


async def test_device_tracker_skips_when_mqtt_enabled(
    hass: HomeAssistant, mock_config_entry
) -> None:
    """Test that device tracker platform skips setup if MQTT presence is enabled."""
    mock_config_entry.options = {CONF_MQTT_PRESENCE: True}

    mock_coordinator = MagicMock()
    hass.data[DOMAIN] = {mock_config_entry.entry_id: {"coordinator": mock_coordinator}}

    with patch("custom_components.openwrt.device_tracker._LOGGER.info") as mock_info:
        await async_setup_entry(hass, mock_config_entry, AsyncMock())

        mock_info.assert_called_once_with(
            "MQTT Presence Detection enabled, skipping standard device trackers for %s",
            "192.168.1.1",
        )


async def test_config_flow_mqtt_steps(hass: HomeAssistant, mock_config_entry) -> None:
    """Test the MQTT presence configuration steps in the config flow."""
    from custom_components.openwrt.config_flow import OpenWrtConfigFlow

    flow = OpenWrtConfigFlow()
    flow.hass = hass
    flow._data = {CONF_HOST: "192.168.1.1"}
    from custom_components.openwrt.api.base import OpenWrtPermissions

    flow._permissions = OpenWrtPermissions(write_mqtt=True)

    # 1. Show MQTT presence form
    result = await flow.async_step_mqtt_presence()
    assert result["type"].lower() == "form"
    assert result["step_id"] == "mqtt_presence"

    # 2. Submit MQTT presence details
    user_input = {
        CONF_MQTT_PRESENCE: True,
        CONF_MQTT_BROKER: "192.168.1.10",
        CONF_MQTT_PORT: 1883,
        CONF_MQTT_USERNAME: "user",
        CONF_MQTT_PASSWORD: "pass",
    }

    # Mock coordinator for permissions check
    hass.data.setdefault(DOMAIN, {})[mock_config_entry.entry_id] = {
        "coordinator": MagicMock(data=MagicMock(permissions=MagicMock(write_mqtt=True)))
    }
    with (
        patch(
            "custom_components.openwrt.helpers.mqtt_presence.async_deploy_mqtt_presence",
            return_value=(True, None),
        ) as mock_deploy,
        patch(
            "custom_components.openwrt.config_flow.create_client",
            return_value=AsyncMock(),
        ),
        patch.object(flow, "_create_entry", return_value=AsyncMock()) as mock_create,
    ):
        result = await flow.async_step_mqtt_presence(user_input)
        assert result["step_id"] == "mqtt_zone"

        # 3. Submit Zone selection
        result = await flow.async_step_mqtt_zone({"mqtt_zone": "zone.home"})

        assert mock_deploy.called
        assert mock_create.called


async def test_options_flow_mqtt_redeploy(
    hass: HomeAssistant, mock_config_entry
) -> None:
    """Test the MQTT presence re-deployment in options flow."""
    from custom_components.openwrt.config_flow import OpenWrtOptionsFlow

    # Register the mock entry in hass
    mock_config_entry.add_to_hass(hass)

    flow = OpenWrtOptionsFlow(mock_config_entry)
    flow.hass = hass

    # Submit redeploy option
    user_input = {
        CONF_REDEPLOY_MQTT: True,
        CONF_MQTT_PRESENCE: True,
    }

    # Mock coordinator for permissions check
    mock_coord = MagicMock(
        data=MagicMock(permissions=MagicMock(write_mqtt=True)),
        _device_history={},
    )
    hass.data.setdefault(DOMAIN, {})[mock_config_entry.entry_id] = {
        "coordinator": mock_coord
    }
    with (
        patch(
            "custom_components.openwrt.helpers.mqtt_presence.async_deploy_mqtt_presence",
            return_value=(True, None),
        ) as mock_deploy,
        patch(
            "custom_components.openwrt.config_flow.create_client",
            return_value=AsyncMock(),
        ),
        patch(
            "custom_components.openwrt.config_flow.translation.async_get_translations",
            AsyncMock(return_value={}),
        ),
    ):
        result = await flow.async_step_init(user_input)

        # Should have gone to mqtt_presence form first to confirm/update details
        assert result["step_id"] == "options_mqtt_presence"

        # Submit the details
        result = await flow.async_step_options_mqtt_presence(user_input)
        assert result["step_id"] == "options_mqtt_zone"

        # Submit Zone selection -> goes to permissions / select devices
        result = await flow.async_step_options_mqtt_zone({"mqtt_zone": "zone.home"})
        assert result["step_id"] == "options_permissions"

        # Submit permissions -> packages
        result = await flow.async_step_options_permissions({"acknowledge": True})
        assert result["step_id"] == "options_packages"

        # Submit packages -> select devices
        result = await flow.async_step_options_packages({"track_devices": True})
        assert result["step_id"] == "options_select_devices"

        # Submit whitelist selection -> triggers deploy and finishes
        result = await flow.async_step_options_select_devices(
            {"tracked_devices": ["11:22:33:44:55:66"]}
        )
        assert mock_deploy.called


async def test_options_flow_consider_home_change_triggers_redeploy(
    hass: HomeAssistant, mock_config_entry
) -> None:
    """Test that changing consider_home when MQTT is enabled auto-redeploys scripts to router."""
    from custom_components.openwrt.config_flow import OpenWrtOptionsFlow
    from custom_components.openwrt.const import CONF_CONSIDER_HOME

    mock_config_entry.options = {
        CONF_MQTT_PRESENCE: True,
        CONF_CONSIDER_HOME: 180,
    }
    mock_config_entry.add_to_hass(hass)

    flow = OpenWrtOptionsFlow(mock_config_entry)
    flow.hass = hass

    user_input = {
        CONF_MQTT_PRESENCE: True,
        CONF_CONSIDER_HOME: 300,
    }

    mock_coord = MagicMock(
        data=MagicMock(permissions=MagicMock(write_mqtt=True)),
        _device_history={},
    )
    hass.data.setdefault(DOMAIN, {})[mock_config_entry.entry_id] = {
        "coordinator": mock_coord
    }
    with (
        patch(
            "custom_components.openwrt.helpers.mqtt_presence.async_deploy_mqtt_presence",
            return_value=(True, None),
        ) as mock_deploy,
        patch(
            "custom_components.openwrt.config_flow.create_client",
            return_value=AsyncMock(),
        ),
        patch(
            "custom_components.openwrt.config_flow.translation.async_get_translations",
            AsyncMock(return_value={}),
        ),
    ):
        result = await flow.async_step_init(user_input)
        assert result["step_id"] == "options_mqtt_presence"

        result = await flow.async_step_options_mqtt_presence(user_input)
        assert result["step_id"] == "options_mqtt_zone"

        result = await flow.async_step_options_mqtt_zone({"mqtt_zone": "zone.home"})
        assert result["step_id"] == "options_permissions"

        result = await flow.async_step_options_permissions({"acknowledge": True})
        assert result["step_id"] == "options_packages"

        result = await flow.async_step_options_packages({"track_devices": True})
        assert result["step_id"] == "options_select_devices"

        result = await flow.async_step_options_select_devices(
            {"tracked_devices": ["11:22:33:44:55:66"]}
        )
        assert mock_deploy.called
        # Verify consider_home parameter passed to async_deploy_mqtt_presence was 300
        assert mock_deploy.call_args.args[4] == 300


async def test_should_redeploy_mqtt_presence_helper() -> None:
    """Test the should_redeploy_mqtt_presence helper with various option changes."""
    from custom_components.openwrt.config_flow import should_redeploy_mqtt_presence
    from custom_components.openwrt.const import (
        CONF_CONSIDER_HOME,
        CONF_MQTT_BROKER,
        CONF_MQTT_PRESENCE,
        CONF_MQTT_ZONE,
        CONF_TRACKED_DEVICES,
    )

    current = {
        CONF_MQTT_PRESENCE: True,
        CONF_CONSIDER_HOME: 180,
        CONF_MQTT_ZONE: "zone.home",
    }

    # No changes
    assert not should_redeploy_mqtt_presence(
        current, {CONF_MQTT_PRESENCE: True, CONF_CONSIDER_HOME: 180}
    )

    # Newly enabled
    assert should_redeploy_mqtt_presence(
        {CONF_MQTT_PRESENCE: False}, {CONF_MQTT_PRESENCE: True}
    )

    # consider_home changed
    assert should_redeploy_mqtt_presence(
        current, {CONF_MQTT_PRESENCE: True, CONF_CONSIDER_HOME: 300}
    )

    # zone changed
    assert should_redeploy_mqtt_presence(
        current, {CONF_MQTT_PRESENCE: True, CONF_MQTT_ZONE: "zone.work"}
    )

    # broker changed
    assert should_redeploy_mqtt_presence(
        current, {CONF_MQTT_PRESENCE: True, CONF_MQTT_BROKER: "1.2.3.4"}
    )

    # tracked devices changed
    assert should_redeploy_mqtt_presence(
        current,
        {CONF_MQTT_PRESENCE: True, CONF_TRACKED_DEVICES: ["AA:BB:CC:DD:EE:FF"]},
    )


async def test_deploy_helper_success(hass: HomeAssistant) -> None:
    """Test the deployment helper logic using local templates."""
    from custom_components.openwrt.helpers.mqtt_presence import (
        async_deploy_mqtt_presence,
    )

    hass.async_add_executor_job = AsyncMock(side_effect=lambda func, *args: func(*args))

    mock_client = AsyncMock()
    mock_client.execute_command.return_value = "OK: presence_hostapd enabled and restarted"
    mqtt_config = {
        "broker": "127.0.0.1",
        "port": 1883,
        "username": "u",
        "password": "p",
    }

    success, error = await async_deploy_mqtt_presence(
        hass, mock_client, mqtt_config
    )

    assert success is True
    assert error is None
    # Verify commands were called
    mock_client.execute_command.assert_any_call("mkdir -p /etc/presence")
    assert mock_client.execute_command.call_count >= 10


async def test_deploy_helper_install_failure(hass: HomeAssistant) -> None:
    """Test that deployment fails when install.sh reports failure."""
    from custom_components.openwrt.helpers.mqtt_presence import (
        async_deploy_mqtt_presence,
    )

    hass.async_add_executor_job = AsyncMock(side_effect=lambda func, *args: func(*args))

    mock_client = AsyncMock()
    # Simulate healthcheck failure inside install.sh
    mock_client.execute_command.side_effect = lambda cmd: (
        "FAIL: no hostapd control socket responds to ping in /var/run/hostapd/"
        if "install.sh" in cmd
        else "OK"
    )
    mqtt_config = {
        "broker": "127.0.0.1",
        "port": 1883,
        "username": "u",
        "password": "p",
    }

    success, error = await async_deploy_mqtt_presence(
        hass, mock_client, mqtt_config
    )

    assert success is False
    assert "FAIL: no hostapd control socket responds to ping" in error


async def test_deploy_helper_with_whitelist_and_consider_home(hass: HomeAssistant) -> None:
    """Test deploying presence with tracked_devices whitelist and consider_home grace period."""
    from custom_components.openwrt.helpers.mqtt_presence import (
        async_deploy_mqtt_presence,
    )

    hass.async_add_executor_job = AsyncMock(side_effect=lambda func, *args: func(*args))

    mock_client = AsyncMock()
    mqtt_config = {
        "broker": "127.0.0.1",
        "port": 1883,
        "username": "u",
        "password": "p",
    }

    executed_cmds = []

    async def capture_cmd(cmd):
        executed_cmds.append(cmd)
        return "OK"

    mock_client.execute_command.side_effect = capture_cmd

    tracked = ["11:22:33:44:55:66", "aa-bb-cc-dd-ee-ff"]
    success, error = await async_deploy_mqtt_presence(
        hass, mock_client, mqtt_config, tracked_devices=tracked, consider_home=120
    )

    assert success is True
    assert error is None

    # Verify presence.conf was written with GRACE_SECONDS=120 and ZONE_ENTITY='zone.home'
    presence_conf_cmd = next(c for c in executed_cmds if "cat <<'EOF' > /etc/presence/presence.conf" in c)
    assert "GRACE_SECONDS=120" in presence_conf_cmd or "GRACE_SECONDS='120'" in presence_conf_cmd
    assert "ZONE_ENTITY=zone.home" in presence_conf_cmd or "ZONE_ENTITY='zone.home'" in presence_conf_cmd

    # Verify presence_devices.conf was written with formatted MAC entries and lowercase topic
    dev_conf_cmd = next(c for c in executed_cmds if "cat <<'EOF' > /etc/presence/presence_devices.conf" in c)
    assert "11:22:33:44:55:66 presence/11_22_33_44_55_66" in dev_conf_cmd
    assert "AA:BB:CC:DD:EE:FF presence/aa_bb_cc_dd_ee_ff" in dev_conf_cmd

    # Verify presence_event.sh formats JSON payload with in_zones
    event_sh_cmd = next(c for c in executed_cmds if "cat <<'EOF' > /etc/presence/presence_event.sh" in c)
    assert 'payload="{\\"in_zones\\":[\\"$' in event_sh_cmd
    assert '-t "${TOPIC}/attributes"' in event_sh_cmd


async def test_mqtt_discovery_cleanup_no_colons(hass: HomeAssistant) -> None:
    """Test that MQTT discovery cleanup topics never contain colons."""
    from custom_components.openwrt.coordinator import OpenWrtDataCoordinator

    config_entry = MagicMock()
    config_entry.options = {}
    config_entry.data = {
        "host": "192.168.1.1",
        "username": "root",
        "password": "password",
    }
    config_entry.entry_id = "test_entry"

    mock_client = AsyncMock()

    with patch("custom_components.openwrt.coordinator.storage.Store") as mock_store:
        mock_store.return_value.async_load = AsyncMock(return_value={})
        coordinator = OpenWrtDataCoordinator(hass, config_entry, mock_client)

    # Set router_id (which has colons)
    coordinator.router_id = "11:22:33:44:55:66"

    # Mock the hass services async_call
    calls = []

    async def mock_async_call(domain, service, service_data, **kwargs):
        if domain == "mqtt" and service == "publish":
            calls.append(service_data)

    hass.services.async_call = mock_async_call
    hass.services.has_service = MagicMock(return_value=True)

    # Call cleanup
    await coordinator._async_discovery_mqtt_device_cleanup("AA:BB:CC:DD:EE:FF")

    # Verify calls
    assert len(calls) > 0
    for call in calls:
        topic = call["topic"]
        # Discovery topics must not contain colons
        if "device_tracker" in topic:
            assert ":" not in topic, f"Topic '{topic}' contains colons"


async def test_mqtt_discovery_cleanup_allowed_characters(hass: HomeAssistant) -> None:
    """Test that MQTT discovery cleanup topics only contain allowed characters."""
    import re

    from custom_components.openwrt.coordinator import OpenWrtDataCoordinator

    config_entry = MagicMock()
    config_entry.options = {}
    config_entry.data = {
        "host": "openwrt.local",
        "username": "root",
        "password": "password",
    }
    config_entry.entry_id = "test_entry"

    mock_client = AsyncMock()

    with patch("custom_components.openwrt.coordinator.storage.Store") as mock_store:
        mock_store.return_value.async_load = AsyncMock(return_value={})
        coordinator = OpenWrtDataCoordinator(hass, config_entry, mock_client)

    # Set router_id with dots and other characters
    coordinator.router_id = "openwrt.local"

    # Mock the hass services async_call
    calls = []

    async def mock_async_call(domain, service, service_data, **kwargs):
        if domain == "mqtt" and service == "publish":
            calls.append(service_data)

    hass.services.async_call = mock_async_call
    hass.services.has_service = MagicMock(return_value=True)

    # Call cleanup
    await coordinator._async_discovery_mqtt_device_cleanup("AA:BB:CC:DD:EE:FF")

    # Verify calls
    assert len(calls) > 0
    for call in calls:
        topic = call["topic"]
        if "device_tracker" in topic:
            parts = topic.split("/")
            node_id = parts[2]
            assert re.match(r"^[a-zA-Z0-9_-]+$", node_id), (
                f"Node ID '{node_id}' in topic '{topic}' contains illegal characters"
            )


async def test_mqtt_discovery_cleanup_active_topic_and_ownership(
    hass: HomeAssistant,
) -> None:
    """Test that MQTT cleanup clears the active discovery topic and preserves status if another entry tracks the MAC."""
    from custom_components.openwrt.const import CONF_TRACKED_DEVICES, DATA_COORDINATOR
    from custom_components.openwrt.coordinator import OpenWrtDataCoordinator

    # Entry 1
    config_entry1 = MagicMock()
    config_entry1.options = {CONF_MQTT_PRESENCE: True}
    config_entry1.data = {"host": "192.168.1.1"}
    config_entry1.entry_id = "entry_1"
    config_entry1.unique_id = "11:22:33:44:55:66"

    # Entry 2 (also tracks the same device)
    config_entry2 = MagicMock()
    config_entry2.options = {
        CONF_MQTT_PRESENCE: True,
        CONF_TRACKED_DEVICES: ["aa:bb:cc:dd:ee:01"],
    }
    config_entry2.data = {"host": "192.168.1.2"}
    config_entry2.entry_id = "entry_2"
    config_entry2.unique_id = "11:22:33:44:55:77"

    mock_client1 = AsyncMock()
    mock_client2 = AsyncMock()

    with patch("custom_components.openwrt.coordinator.storage.Store") as mock_store:
        mock_store.return_value.async_load = AsyncMock(return_value={})
        coord1 = OpenWrtDataCoordinator(hass, config_entry1, mock_client1)
        coord2 = OpenWrtDataCoordinator(hass, config_entry2, mock_client2)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN]["entry_1"] = {DATA_COORDINATOR: coord1}
    hass.data[DOMAIN]["entry_2"] = {DATA_COORDINATOR: coord2}

    published_topics = []

    async def mock_publish(domain, service, service_data, **kwargs):
        if domain == "mqtt" and service == "publish":
            published_topics.append(service_data["topic"])

    hass.services.async_call = mock_publish
    hass.services.has_service = MagicMock(return_value=True)

    # Cleanup on coord1 while coord2 still tracks aa:bb:cc:dd:ee:01
    await coord1._async_discovery_mqtt_device_cleanup("aa:bb:cc:dd:ee:01")

    # Active topic must be cleared
    assert (
        "homeassistant/device_tracker/openwrt_mqtt_aa_bb_cc_dd_ee_01/config"
        in published_topics
    )
    # Status topic must NOT be cleared because entry_2 still tracks it
    assert "presence/aa_bb_cc_dd_ee_01" not in published_topics

    # Now remove aa:bb:cc:dd:ee:01 from entry_2 whitelist
    config_entry2.options[CONF_TRACKED_DEVICES] = ["00:11:22:33:44:55"]
    published_topics.clear()

    # Cleanup again on coord1 - now status topic SHOULD be cleared
    await coord1._async_discovery_mqtt_device_cleanup("aa:bb:cc:dd:ee:01")
    assert "presence/aa_bb_cc_dd_ee_01" in published_topics


def test_presence_templates_shell_syntax() -> None:
    """Test that all shell script templates pass sh -n syntax check.

    Note: This is a static syntax check only (sh -n). It verifies shell script validity
    and parsing, but does not test runtime behavioral logic or execution outcomes.
    """
    from custom_components.openwrt.helpers.mqtt_presence import (
        FILE_TEMPLATE_MAP,
        TEMPLATES_DIR,
    )

    script_templates = [
        rel_path
        for target, rel_path in FILE_TEMPLATE_MAP.items()
        if rel_path.endswith(".sh") or rel_path.startswith("init.d/")
    ]

    assert len(script_templates) == 4, f"Expected 4 shell script templates, found {len(script_templates)}"

    for rel_path in script_templates:
        full_path = TEMPLATES_DIR / rel_path
        assert full_path.is_file(), f"Template file missing: {full_path}"

        result = subprocess.run(
            ["sh", "-n", str(full_path)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"Shell syntax check (sh -n) failed for {rel_path}:\n"
            f"stdout: {result.stdout}\n"
            f"stderr: {result.stderr}"
        )

