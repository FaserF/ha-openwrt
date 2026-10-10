"""Tailscale VPN sensors for OpenWrt."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfInformation

from ..api.base import OpenWrtData, TailscalePeer, TailscaleStatus
from ..api.tailscale import STATE_OPTIONS
from ..const import CONF_ENABLE_VPN
from ..coordinator import OpenWrtDataCoordinator
from .base import OpenWrtSensorDescription, OpenWrtSensorEntity, _bytes_to_mb


def tailscale_entities_enabled(data: OpenWrtData | None, entry: ConfigEntry) -> bool:
    """Return whether Tailscale entities should be created.

    The option default matches the feature-disable cleanup in __init__.py so
    entities are never created and removed again on every reload.
    """
    return bool(
        data is not None
        and data.permissions.read_vpn
        and data.packages.tailscale
        and data.tailscale is not None
        and entry.options.get(CONF_ENABLE_VPN, False)
    )


def find_tailscale_peer(data: OpenWrtData, node_id: str) -> TailscalePeer | None:
    """Return the peer with the given StableNodeID, if still present."""
    if data.tailscale is None:
        return None
    return next((p for p in data.tailscale.peers if p.node_id == node_id), None)


def _ts(data: OpenWrtData) -> TailscaleStatus:
    # Only called when available_fn passed, so the status is present.
    return data.tailscale or TailscaleStatus()


def _ts_available(data: OpenWrtData) -> bool:
    return data.tailscale is not None


def _first_ip(node: TailscalePeer, ipv6: bool) -> str | None:
    return next((ip for ip in node.ip_addresses if (":" in ip) == ipv6), None)


def _state_attrs(data: OpenWrtData) -> dict[str, Any]:
    ts = _ts(data)
    return {
        "version": ts.version,
        "needs_login": ts.needs_login,
        "exit_node_in_use": ts.exit_node_in_use,
    }


def _ip_attrs(data: OpenWrtData) -> dict[str, Any]:
    ts = _ts(data)
    return {
        "ipv6": _first_ip(ts.self_node, ipv6=True),
        "dns_name": ts.self_node.dns_name,
        "advertised_routes": ts.advertised_routes,
    }


def _peers_attrs(data: OpenWrtData) -> dict[str, Any]:
    peers = _ts(data).peers
    online = sum(1 for p in peers if p.online)
    return {"total": len(peers), "offline": len(peers) - online}


def _counter_mb(value: int | None) -> float | None:
    return _bytes_to_mb(value) if value is not None else None


def _async_setup_tailscale_sensors(
    coordinator: OpenWrtDataCoordinator,
    entry: ConfigEntry,
    entities: list[SensorEntity],
    tracked_keys: set[str],
) -> None:
    """Set up Tailscale VPN sensors."""
    data = coordinator.data
    if not tailscale_entities_enabled(data, entry):
        return
    ts = _ts(data)

    descriptions: list[OpenWrtSensorDescription] = [
        OpenWrtSensorDescription(
            key="tsvpn_backend_state",
            name="Tailscale VPN State",
            translation_key="tsvpn_backend_state",
            device_class=SensorDeviceClass.ENUM,
            options=STATE_OPTIONS,
            value_fn=lambda d: _ts(d).backend_state,
            attrs_fn=_state_attrs,
            available_fn=_ts_available,
        ),
        OpenWrtSensorDescription(
            key="tsvpn_ip",
            name="Tailscale VPN IP",
            translation_key="tsvpn_ip",
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=lambda d: _first_ip(_ts(d).self_node, ipv6=False),
            attrs_fn=_ip_attrs,
            available_fn=_ts_available,
        ),
        OpenWrtSensorDescription(
            key="tsvpn_home_derp",
            name="Tailscale VPN Home DERP",
            translation_key="tsvpn_home_derp",
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
            value_fn=lambda d: _ts(d).self_node.relay or None,
            available_fn=_ts_available,
        ),
        OpenWrtSensorDescription(
            key="tsvpn_peers_online",
            name="Tailscale VPN Peers Online",
            translation_key="tsvpn_peers_online",
            state_class=SensorStateClass.MEASUREMENT,
            value_fn=lambda d: sum(1 for p in _ts(d).peers if p.online),
            attrs_fn=_peers_attrs,
            available_fn=_ts_available,
        ),
    ]

    # Only when key expiry is enabled for the router node.
    if ts.self_node.key_expiry is not None:
        descriptions.append(
            OpenWrtSensorDescription(
                key="tsvpn_key_expiry",
                name="Tailscale VPN Key Expiry",
                translation_key="tsvpn_key_expiry",
                device_class=SensorDeviceClass.TIMESTAMP,
                value_fn=lambda d: _ts(d).self_node.key_expiry,
                available_fn=_ts_available,
            )
        )

    # tailscale0 counters; disabled by default because routers with a UCI
    # network.tailscale section already get generic interface traffic sensors.
    if ts.rx_bytes is not None and ts.tx_bytes is not None:
        for direction in ("rx", "tx"):
            descriptions.append(
                OpenWrtSensorDescription(
                    key=f"tsvpn_{direction}",
                    name=f"Tailscale VPN Tunnel {direction.upper()}",
                    translation_key=f"tsvpn_{direction}",
                    native_unit_of_measurement=UnitOfInformation.MEGABYTES,
                    device_class=SensorDeviceClass.DATA_SIZE,
                    state_class=SensorStateClass.TOTAL_INCREASING,
                    entity_category=EntityCategory.DIAGNOSTIC,
                    entity_registry_enabled_default=False,
                    value_fn=lambda d, attr=f"{direction}_bytes": _counter_mb(
                        getattr(_ts(d), attr)
                    ),
                    available_fn=_ts_available,
                )
            )

    for description in descriptions:
        if description.key not in tracked_keys:
            tracked_keys.add(description.key)
            entities.append(OpenWrtSensorEntity(coordinator, entry, description))
