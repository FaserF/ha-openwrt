"""Services and QoS data models for OpenWrt."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class MwanPolicyInterface:
    """MWAN3 multi-wan policy interface allocation."""

    interface: str
    percent: int


@dataclass
class MwanPolicy:
    """MWAN3 multi-wan policy."""

    policy_name: str
    family: str  # "ipv4" or "ipv6"
    active_interfaces: list[MwanPolicyInterface] = field(default_factory=list)


@dataclass
class MwanStatus:
    """MWAN3 multi-wan status."""

    interface_name: str = ""
    status: str = ""
    online_ratio: float = 0.0
    online: float = 0.0
    offline: float = 0.0
    boot_online_ratio: float | None = None
    coverage_start: datetime | None = None
    uptime: int = 0
    enabled: bool = False
    latency: float | None = None
    packet_loss: float | None = None


# Latency mwan3track records for a tracking target that did not answer.
MWAN_NO_REPLY_LATENCY = 999999


def parse_mwan_track_metrics(
    iface_data: dict[str, Any],
) -> tuple[float | None, float | None]:
    """Return the latency (ms) and packet loss (%) mwan3 measured for an interface.

    mwan3 reports both per tracking target in "track_ip", and only measures them
    with check_quality enabled. Without it every target reads 0, even on a dead
    line, so those zeros must not be taken for a perfect line. Not every mwan3
    version reports check_quality; there a non-zero value shows it is measuring.

    Targets marked "skipped" were not probed in the last round and keep older
    values, so only "up" and "down" targets count.
    """
    targets = [
        target
        for target in iface_data.get("track_ip") or []
        if isinstance(target, dict) and target.get("status") in ("up", "down")
    ]
    measuring = iface_data.get("check_quality")
    if measuring is None:
        measuring = any(t.get("latency") or t.get("packetloss") for t in targets)
    if not measuring:
        return None, None

    latencies = [
        value
        for t in targets
        if isinstance(value := t.get("latency"), int | float)
        and value < MWAN_NO_REPLY_LATENCY
    ]
    losses = [
        value for t in targets if isinstance(value := t.get("packetloss"), int | float)
    ]
    return (
        round(sum(latencies) / len(latencies), 1) if latencies else None,
        round(sum(losses) / len(losses), 1) if losses else None,
    )


@dataclass
class QModemInfo:
    """Cellular modem information (QModem)."""

    enabled: bool = False
    manufacturer: str = ""
    revision: str = ""
    temperature: float | None = None
    voltage: int | None = None
    connect_status: str = ""
    sim_status: str = ""
    isp: str = ""
    sim_slot: str = ""
    lte_rsrp: int | None = None
    lte_rsrq: int | None = None
    lte_rssi: int | None = None
    lte_sinr: int | None = None
    nr5g_rsrp: int | None = None
    nr5g_rsrq: int | None = None
    nr5g_sinr: int | None = None
    gps_latitude: float | None = None
    gps_longitude: float | None = None
    gps_last_update: datetime | None = None
    gps_last_update_attempted: datetime | None = None
    gps_last_update_successful: datetime | None = None
    gps_last_update_ok: bool | None = None


@dataclass
class ServiceInfo:
    """System service information."""

    name: str = ""
    enabled: bool = False
    running: bool = False
    # True when procd tracks no resident process for this service, i.e. it is a
    # one-shot init script that applies its configuration and exits.
    one_shot: bool = False


def classify_service(
    name: str,
    rc_entry: dict[str, Any],
    service_entry: Any = None,
) -> ServiceInfo:
    """Build a ServiceInfo from procd's ``rc list`` and ``service list`` views.

    procd only reports ``running`` for services that keep a resident process.
    One-shot init scripts (adblock-fast, firewall, sysctl, sqm, custom nftables
    scripts, ...) apply their configuration and exit, so ``rc list`` reports
    them as ``running: false`` forever and a switch bound to that field can
    never turn on. For those, the boot-enabled flag is the meaningful state.

    A service is treated as procd-managed when ``service list`` reports
    ``instances`` for it -- procd keeps the instance registered even while the
    process is down, so an absent/empty ``instances`` means a genuine one-shot
    rather than a stopped daemon.

    Within an instance, ``exit_code`` disambiguates further: an instance that is
    not running but exited 0 is a one-shot that already did its work, while a
    non-zero exit means it failed or was stopped. Note that ``exit_code`` is
    reported by ``service list`` only -- ``rc list`` never carries it -- so it
    cannot be read from the rc view alone.
    """
    # Absent means "the rc view did not say", which is not the same as False --
    # the LuCI-RPC fallback path has no rc data at all.
    enabled_known = rc_entry.get("enabled")
    enabled = bool(enabled_known)

    # Fast path: procd says it is up, nothing to infer.
    if rc_entry.get("running"):
        return ServiceInfo(name=name, enabled=enabled, running=True)

    instances = None
    if isinstance(service_entry, dict):
        instances = service_entry.get("instances")

    # ubus data is external: anything that is not a dict of dicts tells us
    # nothing, and must not raise here or one bad entry fails the whole refresh.
    live = []
    if isinstance(instances, dict):
        live = [inst for inst in instances.values() if isinstance(inst, dict)]

    if live:
        if any(inst.get("running", False) for inst in live):
            return ServiceInfo(name=name, enabled=enabled, running=True)
        # Registered an instance, exited cleanly: a one-shot that completed.
        # procd keeps reporting that instance until reboot, so follow the boot
        # flag instead -- otherwise disabling the service never sticks.
        if any(inst.get("exit_code") == 0 for inst in live):
            return ServiceInfo(
                name=name,
                enabled=enabled,
                running=enabled if enabled_known is not None else True,
                one_shot=True,
            )
        return ServiceInfo(name=name, enabled=enabled, running=False)

    # No usable procd instance -> one-shot script; "running" is not meaningful.
    return ServiceInfo(name=name, enabled=enabled, running=enabled, one_shot=True)


@dataclass
class AdBlockStatus:
    """Status of the adblock package."""

    enabled: bool = False
    status: str = "disabled"
    version: str | None = None
    blocked_domains: int = 0
    last_update: str | None = None


@dataclass
class SimpleAdBlockStatus:
    """Status of the simple-adblock package."""

    enabled: bool = False
    status: str = "disabled"
    version: str | None = None
    blocked_domains: int = 0


@dataclass
class BanIpStatus:
    """Status of the ban-ip package."""

    enabled: bool = False
    status: str = "disabled"
    version: str | None = None
    banned_ips: int = 0
    blocked_packets: int = 0
    blocked_inbound: int = 0
    blocked_outbound: int = 0
    block_stats: dict[str, int] = field(default_factory=dict)


@dataclass
class VpnInterface:
    """VPN tunnel interface information."""

    name: str = ""
    type: str = ""  # "wireguard", "openvpn"
    up: bool = False
    rx_bytes: int = 0
    tx_bytes: int = 0
    peers: int = 0
    latest_handshake: int = 0  # unix timestamp
    endpoint: str = ""
    public_key: str = ""


@dataclass
class TailscalePeer:
    """A Tailscale node (the router itself or a peer)."""

    node_id: str = ""  # StableNodeID, stable across re-authentication
    hostname: str = ""
    dns_name: str = ""
    os: str = ""
    ip_addresses: list[str] = field(default_factory=list)
    online: bool = False
    active: bool = False
    exit_node: bool = False
    exit_node_option: bool = False
    relay: str = ""  # DERP region code
    connection: str = "idle"  # "direct", "relay" or "idle"
    last_seen: datetime | None = None
    key_expiry: datetime | None = None


@dataclass
class TailscaleStatus:
    """Tailscale daemon status and tailscale0 traffic counters."""

    daemon_running: bool = False
    backend_state: str = "unknown"
    version: str = ""
    needs_login: bool = False
    self_node: TailscalePeer = field(default_factory=TailscalePeer)
    advertised_routes: list[str] = field(default_factory=list)
    magic_dns_suffix: str = ""
    health: list[str] = field(default_factory=list)
    exit_node_in_use: bool = False
    peers: list[TailscalePeer] = field(default_factory=list)
    rx_bytes: int | None = None
    tx_bytes: int | None = None


@dataclass
class SqmStatus:
    """SQM (Smart Queue Management) status."""

    name: str = ""
    enabled: bool = False
    interface: str = ""
    download: int = 0  # kbit/s
    upload: int = 0  # kbit/s
    qdisc: str = ""
    script: str = ""
    section_id: str = ""


@dataclass
class LatencyResult:
    """Network latency measurement result."""

    target: str = ""
    latency_ms: float | None = None
    packet_loss: float = 0.0  # percentage
    available: bool = True


@dataclass
class BatmanOriginator:
    """Batman-adv originator (mesh node) information."""

    mac: str = ""
    last_seen: float = 0.0
    tq: int = 0
    next_hop: str = ""
    outgoing_iface: str = ""
    is_best: bool = False


@dataclass
class BatmanNeighbor:
    """Batman-adv neighbor (1-hop mesh node) information."""

    mac: str = ""
    last_seen: float = 0.0
    interface: str = ""


@dataclass
class BatmanGateway:
    """Batman-adv gateway information."""

    mac: str = ""
    tq: int = 0
    next_hop: str = ""
    outgoing_iface: str = ""
    is_selected: bool = False
    bandwidth_down: str = ""
    bandwidth_up: str = ""
