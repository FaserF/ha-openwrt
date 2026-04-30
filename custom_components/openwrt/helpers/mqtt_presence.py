"""Helper for deploying MQTT presence detection scripts to OpenWrt."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from ..api.base import OpenWrtClient

_LOGGER = logging.getLogger(__name__)

# Pin to a specific commit for security (MIT Licensed scripts from f45tb00t/OpenWRT_HA_Presence)
# Commit: 818d73bcef3a4f47754ff931243693c11c6a6cd0 (pinned on 2026-04-30)
# renovate: datasource=github-commits depName=f45tb00t/OpenWRT_HA_Presence
REPO_URL = "https://raw.githubusercontent.com/f45tb00t/OpenWRT_HA_Presence/818d73bcef3a4f47754ff931243693c11c6a6cd0"
FILES_TO_DEPLOY = [
    "etc/presence/presence_event.sh",
    "etc/presence/presence.conf",
    "etc/presence/presence_mqtt.conf",
    "etc/presence/presence_devices.conf",
    "etc/presence/install.sh",
    "etc/presence/healthcheck.sh",
    "etc/init.d/presence_hostapd",
]


def escape_shell_value(value: Any) -> str:
    """Escape a value for use in a double-quoted shell string."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


async def async_deploy_mqtt_presence(
    hass: HomeAssistant,
    client: OpenWrtClient,
    mqtt_config: dict[str, Any],
) -> tuple[bool, str | None]:
    """Download and deploy MQTT presence scripts to the router."""
    session = async_get_clientsession(hass)

    try:
        # 1. Ensure directory exists
        await client.execute_command("mkdir -p /etc/presence")

        # 2. Download and write each file
        for file_path in FILES_TO_DEPLOY:
            url = f"{REPO_URL}/{file_path}"
            async with session.get(url) as resp:
                if resp.status != 200:
                    return False, f"Failed to download {file_path} from GitHub"
                content = await resp.text()

            # Apply MQTT config to etc/presence/presence_mqtt.conf
            if file_path == "etc/presence/presence_mqtt.conf":
                content = content.replace(
                    'BROKER="192.168.1.10"',
                    f'BROKER="{escape_shell_value(mqtt_config["broker"])}"',
                )
                content = content.replace(
                    'PORT="1883"', f'PORT="{escape_shell_value(mqtt_config["port"])}"'
                )
                content = content.replace(
                    'USER="presence"',
                    f'USER="{escape_shell_value(mqtt_config["username"])}"',
                )
                content = content.replace(
                    'PASS="change_me"',
                    f'PASS="{escape_shell_value(mqtt_config["password"])}"',
                )

            # Write file to router via heredoc for robustness
            cmd = f"cat <<'EOF' > /{file_path}\n{content}\nEOF"

            await client.execute_command(cmd)

        # 3. Set permissions
        await client.execute_command(
            "chmod +x /etc/presence/*.sh /etc/init.d/presence_hostapd"
        )
        await client.execute_command("chmod 600 /etc/presence/presence_mqtt.conf")

        # 4. Run install script
        install_output = await client.execute_command("sh /etc/presence/install.sh")
        _LOGGER.debug("MQTT Presence install output: %s", install_output)

        # 5. Verify healthcheck
        health_output = await client.execute_command("sh /etc/presence/healthcheck.sh")
        if "HEALTHCHECK SUCCESS" not in health_output and "OK" not in health_output:
            _LOGGER.error("MQTT Presence healthcheck failed: %s", health_output)
            return False, f"Healthcheck failed: {health_output}"

        # Start/Enable service
        await client.execute_command("/etc/init.d/presence_hostapd enable")
        await client.execute_command("/etc/init.d/presence_hostapd restart")

        return True, None

    except Exception as err:
        _LOGGER.exception("Failed to deploy MQTT presence: %s", err)
        return False, str(err)


async def async_remove_mqtt_presence(
    client: OpenWrtClient,
) -> tuple[bool, str | None]:
    """Stop service and remove MQTT presence scripts from the router."""
    try:
        # 1. Stop and disable service
        await client.execute_command("/etc/init.d/presence_hostapd stop")
        await client.execute_command("/etc/init.d/presence_hostapd disable")

        # 2. Remove files
        await client.execute_command("rm -rf /etc/presence")
        await client.execute_command("rm /etc/init.d/presence_hostapd")

        return True, None

    except Exception as err:
        _LOGGER.exception("Failed to remove MQTT presence: %s", err)
        return False, str(err)
