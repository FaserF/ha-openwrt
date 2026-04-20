"""Button platform for OpenWrt integration."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any, cast

from homeassistant.components.button import (
    ButtonDeviceClass,
    ButtonEntity,
    ButtonEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api.base import OpenWrtClient
from .const import (
    CONF_TRACK_DEVICES,
    CONF_TRACK_WIRED,
    DATA_CLIENT,
    DATA_COORDINATOR,
    DEFAULT_TRACK_DEVICES,
    DEFAULT_TRACK_WIRED,
    DOMAIN,
)
from .coordinator import OpenWrtDataCoordinator


@dataclass(frozen=True, kw_only=True)
class OpenWrtButtonDescription(ButtonEntityDescription):
    """Describe an OpenWrt button."""

    press_fn: Callable[[OpenWrtClient], Coroutine[Any, Any, Any]]


BUTTONS: tuple[OpenWrtButtonDescription, ...] = (
    OpenWrtButtonDescription(
        key="reboot",
        name="Reboot",
        translation_key="reboot",
        device_class=ButtonDeviceClass.RESTART,
        entity_category=EntityCategory.CONFIG,
        press_fn=lambda client: client.reboot(),
    ),
    OpenWrtButtonDescription(
        key="wps_start",
        name="Start WPS",
        translation_key="wps_start",
        entity_category=EntityCategory.CONFIG,
        press_fn=lambda client: client.set_wps(True),
    ),
    OpenWrtButtonDescription(
        key="wps_cancel",
        name="Cancel WPS",
        translation_key="wps_cancel",
        entity_category=EntityCategory.CONFIG,
        press_fn=lambda client: client.set_wps(False),
    ),
    OpenWrtButtonDescription(
        key="create_backup",
        name="Create Backup",
        translation_key="create_backup",
        entity_category=EntityCategory.CONFIG,
        press_fn=lambda client: client.create_backup(),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up OpenWrt buttons."""
    coordinator: OpenWrtDataCoordinator = hass.data[DOMAIN][entry.entry_id][
        DATA_COORDINATOR
    ]
    client: OpenWrtClient = hass.data[DOMAIN][entry.entry_id][DATA_CLIENT]

    tracked_keys: set[str] = set()

    def _async_add_new_entities() -> None:
        """Add new entities when devices are discovered."""
        if coordinator.data is None:
            return

        new_entities: list[ButtonEntity] = []

        # 1. Static buttons
        _add_static_buttons(coordinator, entry, client, tracked_keys, new_entities)

        # 2. Service buttons
        if coordinator.data.permissions.read_services:
            _add_service_buttons(coordinator, entry, client, tracked_keys, new_entities)

        # 3. Interface buttons
        _add_interface_buttons(coordinator, entry, client, tracked_keys, new_entities)

        # 4. Device-specific buttons (WoL, Kick)
        _add_device_buttons(coordinator, entry, client, tracked_keys, new_entities)

        if new_entities:
            async_add_entities(new_entities)

    # Register listener and run initial discovery
    entry.async_on_unload(coordinator.async_add_listener(_async_add_new_entities))
    _async_add_new_entities()

    @callback
    def _async_cleanup_entities() -> None:
        """Clean up orphaned or incorrect entities."""
        ent_reg = er.async_get(hass)
        entries = er.async_entries_for_config_entry(ent_reg, entry.entry_id)

        for ent in entries:
            if ent.domain != "button":
                continue

            unique_id = ent.unique_id
            # Cleanup incorrectly created WoL buttons for wireless devices
            if "_wol_" in unique_id:
                mac = unique_id.split("_wol_")[-1].lower()
                if mac in coordinator._device_history:
                    if coordinator._device_history[mac].get("is_wireless"):
                        ent_reg.async_remove(ent.entity_id)
                        continue

    hass.add_job(_async_cleanup_entities)
    _async_add_new_entities()


def _add_static_buttons(
    coordinator: OpenWrtDataCoordinator,
    entry: ConfigEntry,
    client: OpenWrtClient,
    tracked_keys: set[str],
    new_entities: list[ButtonEntity],
) -> None:
    """Add static buttons based on permissions."""
    perms = coordinator.data.permissions
    for description in BUTTONS:
        if description.key in tracked_keys:
            continue
        if description.key == "reboot" and not perms.write_system:
            continue
        if description.key in ("wps_start", "wps_cancel") and not perms.write_wireless:
            continue
        if description.key == "create_backup" and not perms.write_system:
            continue

        tracked_keys.add(description.key)
        new_entities.append(
            OpenWrtButtonEntity(coordinator, entry, description, client)
        )


def _add_service_buttons(
    coordinator: OpenWrtDataCoordinator,
    entry: ConfigEntry,
    client: OpenWrtClient,
    tracked_keys: set[str],
    new_entities: list[ButtonEntity],
) -> None:
    """Add buttons for managing services."""
    for service in coordinator.data.services:
        if not service.name:
            continue
        for action in ("restart", "stop"):
            key = f"{action}_{service.name}"
            if key in tracked_keys:
                continue

            tracked_keys.add(key)
            new_entities.append(
                OpenWrtButtonEntity(
                    coordinator,
                    entry,
                    OpenWrtButtonDescription(
                        key=key,
                        name=f"{action.capitalize()} {service.name}",
                        translation_key=f"service_{action}",
                        translation_placeholders={"service": service.name},
                        entity_category=EntityCategory.CONFIG,
                        press_fn=lambda c, n=service.name, a=action: c.manage_service(
                            n, a
                        ),
                    ),
                    client,
                )
            )


def _add_interface_buttons(
    coordinator: OpenWrtDataCoordinator,
    entry: ConfigEntry,
    client: OpenWrtClient,
    tracked_keys: set[str],
    new_entities: list[ButtonEntity],
) -> None:
    """Add buttons for managing network interfaces."""
    for iface in coordinator.data.network_interfaces:
        if iface.name in ("wan", "wan6"):
            key = f"reconnect_{iface.name}"
            if key in tracked_keys:
                continue

            tracked_keys.add(key)
            new_entities.append(
                OpenWrtButtonEntity(
                    coordinator,
                    entry,
                    OpenWrtButtonDescription(
                        key=key,
                        name=iface.name.upper(),
                        translation_key="interface_reconnect",
                        translation_placeholders={"interface": iface.name.upper()},
                        entity_category=EntityCategory.CONFIG,
                        press_fn=lambda c, n=iface.name: c.manage_interface(
                            n, "reconnect"
                        ),
                    ),
                    client,
                )
            )


def _add_device_buttons(
    coordinator: OpenWrtDataCoordinator,
    entry: ConfigEntry,
    client: OpenWrtClient,
    tracked_keys: set[str],
    new_entities: list[ButtonEntity],
) -> None:
    """Add buttons for individual devices (WoL, Kick)."""
    unique_devices = _get_unique_devices(coordinator)
    perms = coordinator.data.permissions
    pkgs = coordinator.data.packages
    router_hostname = (
        coordinator.data.device_info.hostname if coordinator.data.device_info else ""
    )

    track_devices = entry.options.get(
        CONF_TRACK_DEVICES,
        entry.data.get(CONF_TRACK_DEVICES, DEFAULT_TRACK_DEVICES),
    )
    if not track_devices:
        return

    track_wired = entry.options.get(
        CONF_TRACK_WIRED,
        entry.data.get(CONF_TRACK_WIRED, DEFAULT_TRACK_WIRED),
    )

    for mac_lower, info in unique_devices.items():
        mac = info["mac"]
        if not track_wired and not info["is_wireless"]:
            continue
        hostname = info["hostname"]
        dev_name = (
            hostname
            if hostname and hostname != "*" and hostname != router_hostname
            else mac
        )

        # WoL Button
        wol_key = f"wol_{mac_lower}"
        if (
            wol_key not in tracked_keys
            and pkgs.etherwake is not False
            and not info["is_wireless"]
        ):
            tracked_keys.add(wol_key)
            new_entities.append(
                OpenWrtWakeOnLanButton(
                    coordinator, entry, client, mac, dev_name, info["interface"]
                )
            )

        # Kick Button
        kick_key = f"kick_{mac_lower}_{info['interface']}"
        if (
            kick_key not in tracked_keys
            and info["active"]
            and perms.read_wireless
            and info["is_wireless"]
            and info["interface"]
            and pkgs.iwinfo is not False
        ):
            tracked_keys.add(kick_key)
            new_entities.append(
                OpenWrtKickButton(
                    coordinator, entry, client, mac, info["interface"], dev_name
                )
            )


def _get_unique_devices(
    coordinator: OpenWrtDataCoordinator,
) -> dict[str, dict[str, Any]]:
    """Aggregate unique devices from connected devices and DHCP leases."""
    unique: dict[str, dict[str, Any]] = {}

    for device in coordinator.data.connected_devices:
        if not device.mac:
            continue
        mac_lower = device.mac.lower()
        is_wireless = device.is_wireless
        if not is_wireless and device.interface:
            iface_lower = device.interface.lower()
            if any(k in iface_lower for k in ("wlan", "ap", "radio")):
                is_wireless = True
        unique[mac_lower] = {
            "mac": device.mac,
            "hostname": device.hostname,
            "is_wireless": is_wireless,
            "interface": device.interface,
            "active": True,
        }

    for lease in coordinator.data.dhcp_leases:
        if not lease.mac:
            continue
        mac_lower = lease.mac.lower()
        if mac_lower not in unique:
            # Check history to see if it's a known wireless device
            hist_wireless = False
            if mac_lower in coordinator._device_history:
                hist_wireless = coordinator._device_history[mac_lower].get(
                    "is_wireless", False
                )

            unique[mac_lower] = {
                "mac": lease.mac,
                "hostname": lease.hostname,
                "is_wireless": hist_wireless,
                "interface": None,
                "active": False,
            }
        elif not unique[mac_lower]["hostname"] or unique[mac_lower]["hostname"] == "*":
            unique[mac_lower]["hostname"] = lease.hostname
    return unique


class OpenWrtButtonEntity(CoordinatorEntity[OpenWrtDataCoordinator], ButtonEntity):
    """Representation of an OpenWrt button."""

    entity_description: OpenWrtButtonDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: OpenWrtDataCoordinator,
        entry: ConfigEntry,
        description: OpenWrtButtonDescription,
        client: OpenWrtClient,
    ) -> None:
        """Initialize the button."""
        super().__init__(coordinator)
        self.entity_description = description
        self._client = client
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, cast(str, entry.unique_id or entry.data[CONF_HOST]))},
        )

    async def async_press(self) -> None:
        """Handle the button press."""
        try:
            await self.entity_description.press_fn(self._client)
        except Exception as err:
            msg = f"Failed to execute {self.entity_description.key}: {err}"
            raise HomeAssistantError(
                msg,
            ) from err
        await self.coordinator.async_request_refresh()


class OpenWrtWakeOnLanButton(CoordinatorEntity[OpenWrtDataCoordinator], ButtonEntity):
    """Representation of an OpenWrt Wake on LAN button."""

    _attr_has_entity_name = True
    _attr_translation_key = "wake_on_lan"

    def __init__(
        self,
        coordinator: OpenWrtDataCoordinator,
        entry: ConfigEntry,
        client: OpenWrtClient,
        mac: str,
        name: str,
        interface: str | None = None,
    ) -> None:
        """Initialize the button."""
        super().__init__(coordinator)
        self._client = client
        self._mac = mac.lower()
        self._interface = interface
        self._attr_unique_id = f"{entry.entry_id}_{self._mac}_wol"
        self._entry = entry
        self._initial_name = name

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information."""
        via_device = (
            DOMAIN,
            cast(str, self._entry.unique_id or self._entry.data[CONF_HOST]),
        )
        if self.coordinator.data:
            for device in self.coordinator.data.connected_devices:
                if (
                    device.mac
                    and device.mac.lower() == self._mac
                    and device.is_wireless
                    and device.interface
                ):
                    via_device = (
                        DOMAIN,
                        f"{self._entry.unique_id}_ap_{device.interface}",
                    )
                    break

        return DeviceInfo(
            connections={(dr.CONNECTION_NETWORK_MAC, self._mac)},
            name=self._initial_name,
            via_device=via_device,
        )

    async def async_press(self) -> None:
        """Press the button."""
        # Use ether-wake with optional interface
        # We try both names as some distros use one or the other
        command = f"ether-wake {self._mac}"
        if self._interface:
            command = f"ether-wake -i {self._interface} {self._mac}"

        try:
            output = await self._client.execute_command(command)
            if output and "not found" in output.lower():
                # Try etherwake (without hyphen)
                command = command.replace("ether-wake", "etherwake")
                await self._client.execute_command(command)
        except Exception as err:
            if "not found" in str(err).lower():
                msg = (
                    "Wake on LAN command (ether-wake/etherwake) not found on router. "
                    "Please install the 'etherwake' package on OpenWrt."
                )
                raise HomeAssistantError(
                    msg,
                ) from err
            msg = f"Failed to send WoL packet: {err}"
            raise HomeAssistantError(msg) from err


class OpenWrtKickButton(CoordinatorEntity[OpenWrtDataCoordinator], ButtonEntity):
    """Representation of an OpenWrt kick device button."""

    _attr_has_entity_name = True
    _attr_translation_key = "kick_device"
    _attr_entity_registry_enabled_default = False

    def __init__(
        self,
        coordinator: OpenWrtDataCoordinator,
        entry: ConfigEntry,
        client: OpenWrtClient,
        mac: str,
        interface: str,
        hostname: str,
    ) -> None:
        """Initialize the button."""
        super().__init__(coordinator)
        self._client = client
        self._mac = mac.lower()
        self._interface = interface
        self._attr_name = f"Disconnect {hostname}"
        self._attr_unique_id = f"{entry.entry_id}_{self._mac}_kick_{interface}"
        self._entry = entry
        self._initial_name = hostname

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information."""
        via_device = (
            DOMAIN,
            cast(str, self._entry.unique_id or self._entry.data[CONF_HOST]),
        )
        if self.coordinator.data:
            for device in self.coordinator.data.connected_devices:
                if (
                    device.mac
                    and device.mac.lower() == self._mac
                    and device.is_wireless
                    and device.interface
                ):
                    via_device = (
                        DOMAIN,
                        f"{self._entry.unique_id}_ap_{device.interface}",
                    )
                    break

        return DeviceInfo(
            connections={("mac", self._mac)},
            name=self._initial_name,
            via_device=via_device,
        )

    async def async_press(self) -> None:
        """Press the button to disconnect the device."""
        try:
            success = await self._client.kick_device(self._mac, self._interface)
            if not success:
                msg = f"Failed to disconnect {self._mac} from {self._interface}. Ensure hostapd is running."
                raise HomeAssistantError(
                    msg,
                )
        except Exception as err:
            msg = f"Failed to execute device kick: {err}"
            raise HomeAssistantError(msg) from err
        await self.coordinator.async_request_refresh()
