"""Aggregated OpenWrt data model and permissions/packages."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .network import (
    AccessControl,
    ConnectedDevice,
    DhcpLease,
    FirewallRedirect,
    FirewallRule,
    IpNeighbor,
    LldpNeighbor,
    NetworkInterface,
    NlbwmonTraffic,
    UpnpMapping,
    WifiCredentials,
    WireGuardInterface,
    WirelessInterface,
    WpsStatus,
)
from .services import (
    AdBlockStatus,
    BanIpStatus,
    BatmanGateway,
    BatmanNeighbor,
    BatmanOriginator,
    LatencyResult,
    MwanPolicy,
    MwanStatus,
    QModemInfo,
    ServiceInfo,
    SimpleAdBlockStatus,
    SqmStatus,
    TailscaleStatus,
    VpnInterface,
)
from .system import DeviceInfo, LedInfo, SystemResources


@dataclass
class OpenWrtPermissions:
    """Permissions granted to the current user."""

    read_system: bool = False
    write_system: bool = False
    read_network: bool = False
    write_network: bool = False
    read_firewall: bool = False
    write_firewall: bool = False
    read_wireless: bool = False
    write_wireless: bool = False
    read_services: bool = False
    write_services: bool = False
    read_sqm: bool = False
    write_sqm: bool = False
    read_vpn: bool = False
    write_vpn: bool = False
    read_mwan: bool = False
    read_led: bool = False
    write_led: bool = False
    read_devices: bool = False
    write_devices: bool = False
    write_access_control: bool = False
    read_batman: bool = False
    write_mqtt: bool = False


@dataclass
class OpenWrtPackages:
    """Installed packages on the OpenWrt device. None means unknown."""

    sqm_scripts: bool | None = None
    mwan3: bool | None = None
    iwinfo: bool | None = None
    etherwake: bool | None = None
    wireguard: bool | None = None
    openvpn: bool | None = None
    luci_mod_rpc: bool | None = None
    asu: bool | None = None
    adblock: bool | None = None
    adblock_fast: bool | None = None
    simple_adblock: bool | None = None
    ban_ip: bool | None = None
    miniupnpd: bool | None = None
    nlbwmon: bool | None = None
    pbr: bool | None = None
    adguardhome: bool | None = None
    unbound: bool | None = None
    batman_adv: bool | None = None
    batctl: bool | None = None
    snort: bool | None = None

    dhcp: bool | None = None
    wireless: bool | None = None
    lldp: bool | None = None
    stty: bool | None = None
    timeout: bool | None = None
    tailscale: bool | None = None
    openssl_util: bool | None = None
    coreutils_base64: bool | None = None


@dataclass
class OpenWrtData:
    """Aggregated data from an OpenWrt device."""

    device_info: DeviceInfo = field(default_factory=DeviceInfo)
    local_macs: set[str] = field(default_factory=set)
    local_ips: set[str] = field(default_factory=set)
    system_resources: SystemResources = field(default_factory=SystemResources)
    wireless_interfaces: list[WirelessInterface] = field(default_factory=list)
    network_interfaces: list[NetworkInterface] = field(default_factory=list)
    connected_devices: list[ConnectedDevice] = field(default_factory=list)
    all_connected_devices: list[ConnectedDevice] = field(default_factory=list)
    dhcp_leases: list[DhcpLease] = field(default_factory=list)
    reboot_required: bool = False
    system_logs: list[str] = field(default_factory=list)
    dmesg_logs: list[str] = field(default_factory=list)
    ip_neighbors: list[IpNeighbor] = field(default_factory=list)
    mwan_status: list[MwanStatus] = field(default_factory=list)
    mwan_policies: list[MwanPolicy] = field(default_factory=list)
    wps_status: WpsStatus = field(default_factory=WpsStatus)
    adblock: AdBlockStatus = field(default_factory=AdBlockStatus)
    adblock_fast: SimpleAdBlockStatus = field(default_factory=SimpleAdBlockStatus)
    simple_adblock: SimpleAdBlockStatus = field(default_factory=SimpleAdBlockStatus)
    ban_ip: BanIpStatus = field(default_factory=BanIpStatus)
    services: list[ServiceInfo] = field(default_factory=list)
    leds: list[LedInfo] = field(default_factory=list)
    firewall_redirects: list[FirewallRedirect] = field(default_factory=list)
    firewall_rules: list[FirewallRule] = field(default_factory=list)
    access_control: list[AccessControl] = field(default_factory=list)
    vpn_interfaces: list[VpnInterface] = field(default_factory=list)
    tailscale: TailscaleStatus | None = None
    latency: LatencyResult = field(default_factory=LatencyResult)
    external_ip: str | None = None
    firmware_upgradable: bool = False
    firmware_latest_version: str = ""
    firmware_current_version: str = ""
    firmware_release_url: str = ""
    firmware_install_url: str = ""
    firmware_checksum: str = ""
    is_custom_build: bool = False
    installed_packages: list[str] = field(default_factory=list)
    upgradeable_packages: dict[str, str] = field(default_factory=dict)
    asu_supported: bool = False
    asu_update_available: bool = False
    asu_status: str = ""  # one of const.ASU_STATUS_*; "" = not checked
    asu_status_reason: str = ""
    asu_target_version: str = ""  # version string to request from ASU
    asu_profile: str = ""  # ASU profile name that matched this board
    asu_image_status: str = ""  # e.g. "available", "building", "failed"
    asu_image_url: str | None = None
    lldp_neighbors: list[LldpNeighbor] = field(default_factory=list)
    qmodem_info: QModemInfo = field(default_factory=QModemInfo)
    wireguard_interfaces: list[WireGuardInterface] = field(default_factory=list)
    upnp_mappings: list[UpnpMapping] = field(default_factory=list)
    nlbwmon_traffic: dict[str, NlbwmonTraffic] = field(default_factory=dict)
    nlbwmon_top_hosts: dict[str, Any] = field(default_factory=dict)
    wifi_credentials: list[WifiCredentials] = field(default_factory=list)
    sqm: list[SqmStatus] = field(default_factory=list)
    packages: OpenWrtPackages = field(default_factory=OpenWrtPackages)
    permissions: OpenWrtPermissions = field(default_factory=OpenWrtPermissions)
    mqtt_presence_status: str | None = None
    mqtt_presence_logs: list[str] | None = None
    snort_status: dict[str, Any] = field(default_factory=dict)
    batman_mesh_active: bool = False
    # True: batctl tables read; False: batctl-tiny (no tables);
    # None: not read yet, or the tables could not be read
    batman_tables_supported: bool | None = None
    batman_originators: list[BatmanOriginator] = field(default_factory=list)
    batman_neighbors: list[BatmanNeighbor] = field(default_factory=list)
    batman_gateways: list[BatmanGateway] = field(default_factory=list)
    batman_translation_table: dict[str, str] = field(default_factory=dict)
    boot_time: datetime | None = None
