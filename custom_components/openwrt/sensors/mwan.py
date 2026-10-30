"""MWAN3 multi-WAN sensors for OpenWrt."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_HOST,
    PERCENTAGE,
    EntityCategory,
)
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ..api.base import OpenWrtData
from ..const import DOMAIN
from ..coordinator import OpenWrtDataCoordinator
from .base import OpenWrtSensorDescription, OpenWrtSensorEntity

_LOGGER = logging.getLogger(__name__)

# Tracking states mwan3track writes for an interface. "connecting" means the
# link answers again but has not yet passed the configured number of
# successful checks, so mwan3 does not route traffic over it yet.
MWAN_STATES = ["online", "offline", "connecting", "disconnecting", "disabled"]


def _mwan_policy_status(data: OpenWrtData, policy_name: str, family: str) -> str | None:
    """Return the state (active interface) of an MWAN3 policy."""
    policy = next(
        (
            p
            for p in data.mwan_policies
            if p.policy_name == policy_name and p.family == family
        ),
        None,
    )
    if not policy or not policy.active_interfaces:
        return "none"

    # Normally mwan3 reports one primary active interface. If there are multiple,
    # we return the first one with weight > 0, or just the first one.
    active = [i.interface for i in policy.active_interfaces if i.percent > 0]
    if active:
        return active[0]
    return policy.active_interfaces[0].interface


def _mwan_policy_attrs(
    data: OpenWrtData, policy_name: str, family: str
) -> dict[str, Any]:
    """Return attributes (interface percentages) for an MWAN3 policy."""
    policy = next(
        (
            p
            for p in data.mwan_policies
            if p.policy_name == policy_name and p.family == family
        ),
        None,
    )
    attrs: dict[str, Any] = {"active_interfaces": 0}
    if not policy:
        return attrs

    attrs["active_interfaces"] = len(policy.active_interfaces)
    for iface in policy.active_interfaces:
        attrs[f"{iface.interface}"] = f"{iface.percent}%"
    return attrs


def _create_mwan_policy_sensors(
    coordinator: OpenWrtDataCoordinator,
    entry: ConfigEntry,
    policy_name: str,
    family: str,
) -> list[OpenWrtSensorEntity]:
    """Create sensors for an MWAN3 policy."""
    return [
        OpenWrtSensorEntity(
            coordinator,
            entry,
            OpenWrtSensorDescription(
                key=f"mwan_policy_{family}_{policy_name}_status",
                translation_key="mwan_policy_status",
                translation_placeholders={"policy": policy_name, "family": family},
                name=f"MWAN Policy {family} {policy_name}",
                entity_category=EntityCategory.DIAGNOSTIC,
                icon="mdi:router-network",
                value_fn=lambda data, p=policy_name, f=family: _mwan_policy_status(
                    data, p, f
                ),
                attrs_fn=lambda data, p=policy_name, f=family: _mwan_policy_attrs(
                    data, p, f
                ),
            ),
        ),
    ]


def _mwan_status(data: OpenWrtData, iface_name: str) -> str | None:
    """Return the tracking state of an MWAN3 interface.

    mwan3 reports "unknown" before the first check; that and any state not in
    MWAN_STATES become None, so the enum sensor shows unknown.
    """
    status = next(
        (m.status for m in data.mwan_status if m.interface_name == iface_name),
        None,
    )
    return status if status in MWAN_STATES else None


def _create_mwan_sensors(
    coordinator: OpenWrtDataCoordinator,
    entry: ConfigEntry,
    iface_name: str,
) -> list[OpenWrtSensorEntity]:
    """Create sensors for an MWAN3 interface."""
    return [
        OpenWrtSensorEntity(
            coordinator,
            entry,
            OpenWrtSensorDescription(
                key=f"mwan_{iface_name}_ratio",
                translation_key="mwan_ratio",
                translation_placeholders={"interface": iface_name},
                name=f"MWAN {iface_name} Online Ratio",
                native_unit_of_measurement=PERCENTAGE,
                state_class=SensorStateClass.MEASUREMENT,
                suggested_display_precision=2,
                value_fn=lambda data, n=iface_name: next(
                    (
                        m.boot_online_ratio * 100
                        for m in data.mwan_status
                        if m.interface_name == n and m.boot_online_ratio is not None
                    ),
                    None,
                ),
                attrs_fn=lambda data, n=iface_name: next(
                    (
                        {
                            "coverage_start": (
                                m.coverage_start.isoformat()
                                if m.coverage_start
                                else None
                            )
                        }
                        for m in data.mwan_status
                        if m.interface_name == n
                    ),
                    {},
                ),
            ),
        ),
        OpenWrtSensorEntity(
            coordinator,
            entry,
            OpenWrtSensorDescription(
                key=f"mwan_{iface_name}_status",
                translation_key="mwan_status",
                translation_placeholders={"interface": iface_name},
                name=f"MWAN {iface_name} Status",
                device_class=SensorDeviceClass.ENUM,
                options=MWAN_STATES,
                entity_category=EntityCategory.DIAGNOSTIC,
                value_fn=lambda data, n=iface_name: _mwan_status(data, n),
            ),
        ),
    ]


class OpenWrtMwanMetricSensor(CoordinatorEntity[OpenWrtDataCoordinator], SensorEntity):
    """MWAN3 metric sensor (latency, packet loss)."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: OpenWrtDataCoordinator,
        entry: ConfigEntry,
        iface: str,
        metric: str,
        pkgs: Any = None,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._iface = iface
        self._metric = metric
        self._attr_unique_id = f"{entry.entry_id}_mwan_{iface}_{metric}"
        self._attr_name = f"MWAN {iface} {metric.replace('_', ' ').title()}"
        self._attr_entity_registry_enabled_default = bool(pkgs and pkgs.mwan3 is True)
        if metric == "latency":
            self._attr_native_unit_of_measurement = "ms"
            self._attr_icon = "mdi:timer-outline"
        else:
            self._attr_native_unit_of_measurement = "%"
            self._attr_icon = "mdi:packet-stack"

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.unique_id or entry.data[CONF_HOST])},
        )

    @property
    def native_value(self) -> float | None:
        """Return native value."""
        if not self.coordinator.data:
            return None
        for m in self.coordinator.data.mwan_status:
            if m.interface_name == self._iface:
                return getattr(m, self._metric)
        return None
