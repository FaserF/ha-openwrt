"""Parsing helpers for Tailscale status output."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from .base import TailscalePeer, TailscaleStatus

_LOGGER = logging.getLogger(__name__)

RC_MARKER = "__TS_RC__"

# One round-trip on every transport: the exit code is echoed explicitly because
# SSH and LuCI-RPC do not expose it, and the trailing cat keeps ubus returning
# stdout even when tailscale itself failed.
TAILSCALE_STATUS_COMMAND = (
    "tailscale status --json 2>&1; "
    f'echo "{RC_MARKER}$?"; '
    "cat /sys/class/net/tailscale0/statistics/rx_bytes "
    "/sys/class/net/tailscale0/statistics/tx_bytes 2>/dev/null"
)

BACKEND_STATES: dict[str, str] = {
    "NoState": "no_state",
    "NeedsLogin": "needs_login",
    "NeedsMachineAuth": "needs_machine_auth",
    "Stopped": "stopped",
    "Starting": "starting",
    "Running": "running",
}
STATE_DAEMON_UNAVAILABLE = "daemon_unavailable"
STATE_UNKNOWN = "unknown"
STATE_OPTIONS: list[str] = [
    *BACKEND_STATES.values(),
    STATE_DAEMON_UNAVAILABLE,
    STATE_UNKNOWN,
]

_DAEMON_DOWN_MARKERS = (
    "failed to connect to local tailscaled",
    "doesn't appear to be running",
)
_ZERO_TIME_PREFIX = "0001-01-01"


def normalize_backend_state(state: str) -> str:
    """Map a Tailscale BackendState to a stable snake_case value."""
    return BACKEND_STATES.get(state, STATE_UNKNOWN)


def _parse_time(value: Any) -> datetime | None:
    """Parse a Go RFC 3339 timestamp, treating the zero time as unknown."""
    if not isinstance(value, str) or not value or value.startswith(_ZERO_TIME_PREFIX):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        _LOGGER.debug("Unparsable Tailscale timestamp: %s", value)
        return None


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item]


def _parse_node(raw: Any) -> TailscalePeer:
    """Build a node from a Self/Peer object, keeping only non-sensitive fields."""
    node = raw if isinstance(raw, dict) else {}
    cur_addr = node.get("CurAddr") or ""
    active = bool(node.get("Active"))
    online = bool(node.get("Online"))
    if not online:
        # A just-disconnected peer can still report an active relayed session.
        connection = "idle"
    elif cur_addr:
        connection = "direct"
    elif active:
        connection = "relay"
    else:
        connection = "idle"
    return TailscalePeer(
        node_id=str(node.get("ID") or ""),
        hostname=str(node.get("HostName") or ""),
        dns_name=str(node.get("DNSName") or "").rstrip("."),
        os=str(node.get("OS") or ""),
        ip_addresses=_str_list(node.get("TailscaleIPs")),
        online=online,
        active=active,
        exit_node=bool(node.get("ExitNode")),
        exit_node_option=bool(node.get("ExitNodeOption")),
        relay=str(node.get("Relay") or ""),
        connection=connection,
        last_seen=_parse_time(node.get("LastSeen")),
        key_expiry=_parse_time(node.get("KeyExpiry")),
    )


def _parse_counter(lines: list[str], index: int) -> int | None:
    if len(lines) > index and lines[index].strip().isdigit():
        return int(lines[index].strip())
    return None


def parse_tailscale_output(raw: str | None) -> TailscaleStatus | None:
    """Parse the output of TAILSCALE_STATUS_COMMAND.

    Returns None when the result is unknown (transport or permission failure,
    missing binary, truncated output) so the caller can keep previous data.
    """
    if not raw or RC_MARKER not in raw:
        return None

    body, _, tail = raw.partition(RC_MARKER)
    tail_lines = tail.strip().splitlines()
    try:
        rc = int(tail_lines[0].strip()) if tail_lines else -1
    except ValueError:
        rc = -1

    if rc != 0:
        lowered = body.lower()
        if any(marker in lowered for marker in _DAEMON_DOWN_MARKERS):
            return TailscaleStatus(
                daemon_running=False, backend_state=STATE_DAEMON_UNAVAILABLE
            )
        _LOGGER.debug("tailscale status failed (rc=%s): %s", rc, body.strip()[:200])
        return None

    start = body.find("{")
    if start < 0:
        _LOGGER.debug("tailscale status returned no JSON: %s", body.strip()[:200])
        return None
    try:
        data, _ = json.JSONDecoder().raw_decode(body, start)
    except ValueError as err:
        _LOGGER.debug("Invalid tailscale status JSON: %s", err)
        return None
    if not isinstance(data, dict):
        _LOGGER.debug("Unexpected tailscale status payload type: %s", type(data))
        return None

    self_raw = data.get("Self")
    self_node = _parse_node(self_raw)
    peers_raw = data.get("Peer") or {}
    peers = [
        _parse_node(peer)
        for peer in (peers_raw.values() if isinstance(peers_raw, dict) else [])
    ]
    peers = [peer for peer in peers if peer.node_id]
    peers.sort(key=lambda peer: (peer.hostname.lower(), peer.node_id))

    backend_state = normalize_backend_state(str(data.get("BackendState") or ""))
    advertised = (
        _str_list(self_raw.get("PrimaryRoutes")) if isinstance(self_raw, dict) else []
    )

    return TailscaleStatus(
        daemon_running=True,
        backend_state=backend_state,
        version=str(data.get("Version") or ""),
        needs_login=bool(data.get("AuthURL")) or backend_state == "needs_login",
        self_node=self_node,
        advertised_routes=advertised,
        magic_dns_suffix=str(data.get("MagicDNSSuffix") or ""),
        health=_str_list(data.get("Health")),
        exit_node_in_use=bool(data.get("ExitNodeStatus")),
        peers=peers,
        rx_bytes=_parse_counter(tail_lines, 1),
        tx_bytes=_parse_counter(tail_lines, 2),
    )
