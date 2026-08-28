"""Helper for deploying MQTT presence detection scripts to OpenWrt."""

from __future__ import annotations

import logging
from pathlib import Path
from string import Template
from typing import Any

from homeassistant.core import HomeAssistant

from ..api.base import OpenWrtClient
from ..const import DEFAULT_CONSIDER_HOME

_LOGGER = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "presence_templates"

# Map router target file_path -> local template path relative to TEMPLATES_DIR
FILE_TEMPLATE_MAP = {
    "etc/presence/presence_event.sh": "scripts/presence_event.sh",
    "etc/presence/presence.conf": "conf/presence.conf",
    "etc/presence/presence_mqtt.conf": "conf/presence_mqtt.conf",
    "etc/presence/presence_devices.conf": "conf/presence_devices.conf",
    "etc/presence/install.sh": "scripts/install.sh",
    "etc/presence/healthcheck.sh": "scripts/healthcheck.sh",
    "etc/init.d/presence_hostapd": "init.d/presence_hostapd",
}


def escape_shell_value(value: Any) -> str:
    """Escape a value for use in a double-quoted shell string."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


async def async_deploy_mqtt_presence(
    hass: HomeAssistant,
    client: OpenWrtClient,
    mqtt_config: dict[str, Any],
    tracked_devices: list[str] | set[str] | None = None,
    consider_home: int | None = None,
) -> tuple[bool, str | None]:
    """Deploy MQTT presence scripts to the router using bundled templates."""
    # Normalize tracked_devices MAC addresses to uppercase (for conf matching)
    normalized_macs: set[str] = set()
    if tracked_devices:
        for mac in tracked_devices:
            if isinstance(mac, str) and mac.strip():
                clean_mac = mac.strip().lower().replace("-", ":")
                if len(clean_mac) == 12 and ":" not in clean_mac:
                    clean_mac = ":".join(clean_mac[i : i + 2] for i in range(0, 12, 2))
                if len(clean_mac) == 17:
                    normalized_macs.add(clean_mac.upper())

    # Determine GRACE_SECONDS from consider_home option/default
    grace_seconds = (
        consider_home
        if consider_home is not None and consider_home > 0
        else DEFAULT_CONSIDER_HOME
    )

    try:
        # Ensure directory exists
        await client.execute_command("mkdir -p /etc/presence")

        # Discover active wireless interfaces to configure presence.conf
        ifaces_output = await client.execute_command(
            "ls -1 /var/run/hostapd/ 2>/dev/null || true"
        )
        valid_ifaces = []
        if ifaces_output:
            for line in ifaces_output.splitlines():
                line = line.strip()
                if line and line != "global" and "No such file" not in line:
                    valid_ifaces.append(line)

        ifaces_str = " ".join(valid_ifaces) if valid_ifaces else "wl0-ap0 wl1-ap0"

        # Read and format local template files
        for target_file, rel_template_path in FILE_TEMPLATE_MAP.items():
            template_file = TEMPLATES_DIR / rel_template_path

            if not template_file.is_file():
                return False, f"Template file missing: {rel_template_path}"

            raw_content = await hass.async_add_executor_job(
                template_file.read_text, "utf-8"
            )

            # Apply string.Template substitutions to configuration files
            if target_file == "etc/presence/presence_mqtt.conf":
                tmpl = Template(raw_content)
                content = tmpl.substitute(
                    BROKER=escape_shell_value(mqtt_config["broker"]),
                    PORT=escape_shell_value(mqtt_config["port"]),
                    USER=escape_shell_value(mqtt_config["username"]),
                    PASS=escape_shell_value(mqtt_config["password"]),
                )

            elif target_file == "etc/presence/presence.conf":
                zone_entity = mqtt_config.get("zone", "zone.home")
                zone_name = "home"
                if zone_entity != "zone.home":
                    if zone_state := hass.states.get(zone_entity):
                        zone_name = zone_state.name or zone_entity
                    else:
                        zone_name = zone_entity.split(".")[-1]

                tmpl = Template(raw_content)
                content = tmpl.substitute(
                    GRACE_SECONDS=str(grace_seconds),
                    IFACES=ifaces_str,
                    ZONE_ENTITY=escape_shell_value(zone_entity),
                    ZONE_NAME=escape_shell_value(zone_name),
                )

            elif target_file == "etc/presence/presence_devices.conf":
                if normalized_macs:
                    device_lines = []
                    for mac in sorted(normalized_macs):
                        safe_mac = mac.lower().replace(":", "_")
                        device_lines.append(f"{mac} presence/{safe_mac}")
                    mappings_str = "\n".join(device_lines)
                else:
                    mappings_str = ""
                tmpl = Template(raw_content)
                content = tmpl.substitute(DEVICE_MAPPINGS=mappings_str)

            else:
                # Pure static scripts (.sh / init scripts) are written directly without modification
                content = raw_content

            # Write file to router via heredoc for robustness
            cmd = f"cat <<'EOF' > /{target_file}\n{content}\nEOF"
            await client.execute_command(cmd)

        # Set permissions
        await client.execute_command(
            "chmod +x /etc/presence/*.sh /etc/init.d/presence_hostapd"
        )
        await client.execute_command("chmod 600 /etc/presence/presence_mqtt.conf")

        # Run install script (installs deps, checks health, then enables and starts service)
        install_output = await client.execute_command("sh /etc/presence/install.sh")
        _LOGGER.debug("MQTT Presence install output: %s", install_output)

        return True, None

    except Exception as err:
        _LOGGER.exception("Failed to deploy MQTT presence: %s", err)
        return False, str(err)


async def async_remove_mqtt_presence(
    client: OpenWrtClient,
) -> tuple[bool, str | None]:
    """Stop service and remove MQTT presence scripts from the router."""
    try:
        # Stop and disable service
        await client.execute_command(
            "/etc/init.d/presence_hostapd stop 2>/dev/null || true"
        )
        await client.execute_command(
            "/etc/init.d/presence_hostapd disable 2>/dev/null || true"
        )
        # Ensure any background hostapd_cli processes are killed
        await client.execute_command("killall -9 hostapd_cli 2>/dev/null || true")

        # Remove files
        await client.execute_command("rm -rf /etc/presence 2>/dev/null || true")
        await client.execute_command(
            "rm -f /etc/init.d/presence_hostapd 2>/dev/null || true"
        )

        return True, None

    except Exception as err:
        _LOGGER.exception("Failed to remove MQTT presence: %s", err)
        return False, str(err)
