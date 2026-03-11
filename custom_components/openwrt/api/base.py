"""Base client interface for OpenWrt API communication."""

from __future__ import annotations

import abc
import json
import logging
import re
from dataclasses import dataclass, field

_LOGGER = logging.getLogger(__name__)


@dataclass
class DeviceInfo:
    """OpenWrt device information."""

    hostname: str = ""
    model: str = ""
    board_name: str = ""
    firmware_version: str = ""
    kernel_version: str = ""
    architecture: str = ""
    target: str = ""
    mac_address: str = ""
    uptime: int = 0
    local_time: str = ""
    release_distribution: str = "OpenWrt"
    release_version: str = ""
    release_revision: str = ""


@dataclass
class WirelessInterface:
    """Wireless interface information."""

    name: str = ""
    ssid: str = ""
    mode: str = ""
    channel: int = 0
    frequency: str = ""
    signal: int = 0
    noise: int = 0
    bitrate: float = 0.0
    encryption: str = ""
    clients_count: int = 0
    enabled: bool = True
    up: bool = False
    radio: str = ""
    htmode: str = ""
    txpower: int = 0
    mesh_id: str = ""
    mesh_fwding: bool = False


@dataclass
class NetworkInterface:
    """Network interface information."""

    name: str = ""
    up: bool = False
    mac_address: str = ""
    ipv4_address: str = ""
    ipv6_address: str = ""
    rx_bytes: int = 0
    tx_bytes: int = 0
    rx_packets: int = 0
    tx_packets: int = 0
    rx_errors: int = 0
    tx_errors: int = 0
    rx_dropped: int = 0
    tx_dropped: int = 0
    collisions: int = 0
    multicast: int = 0
    rx_rate: float = 0.0
    tx_rate: float = 0.0
    speed: str = ""
    duplex: str = ""
    protocol: str = ""
    device: str = ""
    dns_servers: list[str] = field(default_factory=list)
    uptime: int = 0


@dataclass
class ConnectedDevice:
    """Connected device (client) information."""

    mac: str = ""
    ip: str = ""
    hostname: str = ""
    interface: str = ""
    connected_via: str = ""
    rx_bytes: int = 0
    tx_bytes: int = 0
    rx_rate: int = 0
    tx_rate: int = 0
    signal: int = 0
    noise: int = 0
    is_wireless: bool = False
    connected: bool = True
    connection_type: str = ""  # e.g. "wired", "2.4GHz", "5GHz", "6GHz"
    connection_info: str = ""  # e.g. "802.11ax", "1000Mbps"
    uptime: int = 0


@dataclass
class SystemResources:
    """System resource information."""

    cpu_usage: float = 0.0
    memory_total: int = 0
    memory_used: int = 0
    memory_free: int = 0
    memory_buffered: int = 0
    memory_cached: int = 0
    swap_total: int = 0
    swap_used: int = 0
    swap_free: int = 0
    load_1min: float = 0.0
    load_5min: float = 0.0
    load_15min: float = 0.0
    uptime: int = 0
    processes: int = 0
    temperature: float | None = None
    filesystem_total: int = 0
    filesystem_used: int = 0
    filesystem_free: int = 0


@dataclass
class MwanStatus:
    """MWAN3 multi-wan status."""

    interface_name: str = ""
    status: str = ""
    online_ratio: float = 0.0
    uptime: int = 0
    enabled: bool = False


@dataclass
class DhcpLease:
    """DHCP lease entry."""

    hostname: str = ""
    mac: str = ""
    ip: str = ""
    expires: int = 0


@dataclass
class WpsStatus:
    """WPS status."""

    enabled: bool = False
    status: str = "disabled"


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


@dataclass
class ServiceInfo:
    """System service information."""

    name: str = ""
    enabled: bool = False
    running: bool = False


@dataclass
class LedInfo:
    """Router LED information."""

    name: str = ""
    brightness: int = 0
    max_brightness: int = 255
    trigger: str = ""
    active: bool = False


@dataclass
class FirewallRedirect:
    """Firewall port forwarding redirect."""

    name: str = ""
    target_ip: str = ""
    target_port: str = ""
    external_port: str = ""
    protocol: str = ""
    enabled: bool = True
    section_id: str = ""


@dataclass
class AccessControl:
    """Device access control (Parental Control)."""

    mac: str = ""
    name: str = ""
    blocked: bool = False
    section_id: str = ""


@dataclass
class OpenWrtData:
    """Aggregated data from an OpenWrt device."""

    device_info: DeviceInfo = field(default_factory=DeviceInfo)
    system_resources: SystemResources = field(default_factory=SystemResources)
    wireless_interfaces: list[WirelessInterface] = field(default_factory=list)
    network_interfaces: list[NetworkInterface] = field(default_factory=list)
    connected_devices: list[ConnectedDevice] = field(default_factory=list)
    dhcp_leases: list[DhcpLease] = field(default_factory=list)
    mwan_status: list[MwanStatus] = field(default_factory=list)
    wps_status: WpsStatus = field(default_factory=WpsStatus)
    services: list[ServiceInfo] = field(default_factory=list)
    leds: list[LedInfo] = field(default_factory=list)
    firewall_redirects: list[FirewallRedirect] = field(default_factory=list)
    access_control: list[AccessControl] = field(default_factory=list)
    external_ip: str | None = None
    firmware_upgradable: bool = False
    firmware_latest_version: str = ""
    firmware_current_version: str = ""
    firmware_release_url: str = ""
    firmware_checksum: str = ""
    is_custom_build: bool = False
    installed_packages: list[str] = field(default_factory=list)
    asu_supported: bool = False
    asu_update_available: bool = False
    asu_image_status: str = ""  # e.g. "available", "building", "failed"
    asu_image_url: str | None = None
    qmodem_info: QModemInfo = field(default_factory=QModemInfo)


class OpenWrtClient(abc.ABC):
    """Abstract base class for OpenWrt API clients."""

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        port: int = 80,
        use_ssl: bool = False,
        verify_ssl: bool = False,
    ) -> None:
        """Initialize the client."""
        self.host = host
        self.username = username
        self.password = password
        self.port = port
        self.use_ssl = use_ssl
        self.verify_ssl = verify_ssl
        self._connected = False

    @property
    def connected(self) -> bool:
        """Return whether the client is connected."""
        return self._connected

    @abc.abstractmethod
    async def connect(self) -> bool:
        """Establish connection and authenticate."""

    @abc.abstractmethod
    async def disconnect(self) -> None:
        """Disconnect from the device."""

    @abc.abstractmethod
    async def get_device_info(self) -> DeviceInfo:
        """Get device information."""

    @abc.abstractmethod
    async def get_system_resources(self) -> SystemResources:
        """Get system resource usage."""

    @abc.abstractmethod
    async def get_wireless_interfaces(self) -> list[WirelessInterface]:
        """Get wireless interface information."""

    @abc.abstractmethod
    async def get_network_interfaces(self) -> list[NetworkInterface]:
        """Get network interface information."""

    @abc.abstractmethod
    async def get_connected_devices(self) -> list[ConnectedDevice]:
        """Get list of connected clients/devices."""

    async def get_neighbors(self) -> list[dict[str, str]]:
        """Get neighbor (ARP/NDP) table entries."""
        return []

    @abc.abstractmethod
    async def get_dhcp_leases(self) -> list[DhcpLease]:
        """Get DHCP lease information."""

    @abc.abstractmethod
    async def reboot(self) -> bool:
        """Reboot the device."""

    @abc.abstractmethod
    async def execute_command(self, command: str) -> str:
        """Execute a command on the device."""

    async def kick_device(self, mac_address: str, interface: str) -> bool:
        """Kick a wireless device from the network using hostapd."""
        cmd_ubus = f"ubus call hostapd.{interface} del_client '{{\"addr\":\"{mac_address}\",\"reason\":5,\"deauth\":true,\"ban_time\":60000}}'"
        try:
            output = await self.execute_command(cmd_ubus)
            if output and "Method not found" not in output and "Not found" not in output:
                return True
        except Exception:
            pass

        cmd_cli = f"hostapd_cli -i {interface} deauthenticate {mac_address}"
        try:
            output = await self.execute_command(cmd_cli)
            if output and "OK" in output:
                return True
        except Exception:
            pass

        return False

    async def get_mwan_status(self) -> list[MwanStatus]:
        """Get MWAN3 status (optional, may not be installed)."""
        return []

    async def get_wps_status(self) -> WpsStatus:
        """Get WPS status."""
        return WpsStatus()

    async def set_wps(self, enabled: bool) -> bool:
        """Enable or disable WPS."""
        return False

    async def get_services(self) -> list[ServiceInfo]:
        """Get list of system services."""
        return []

    async def manage_service(self, name: str, action: str) -> bool:
        """Manage a system service (start/stop/restart/enable/disable)."""
        return False

    async def set_wireless_enabled(self, interface: str, enabled: bool) -> bool:
        """Enable or disable a wireless interface."""
        return False

    async def manage_interface(self, name: str, action: str) -> bool:
        """Manage a network interface (up/down/reconnect)."""
        return False

    async def get_firewall_redirects(self) -> list[FirewallRedirect]:
        """Get firewall port forwarding redirects."""
        return []

    async def set_firewall_redirect_enabled(
        self, section_id: str, enabled: bool
    ) -> bool:
        """Enable or disable a firewall redirect."""
        return False

    async def get_access_control(self) -> list[AccessControl]:
        """Get list of access control rules."""
        return []

    async def set_access_control_blocked(self, mac: str, blocked: bool) -> bool:
        """Block or unblock a device's internet access."""
        return False

    async def get_external_ip(self) -> str | None:
        """Get public/external IP address."""
        return None

    async def get_leds(self) -> list[LedInfo]:
        """Get list of router LEDs."""
        return []

    async def set_led(self, name: str, brightness: int) -> bool:
        """Set LED brightness (0=off, max=on)."""
        return False

    @abc.abstractmethod
    async def install_firmware(self, url: str) -> None:
        """Install firmware from the given URL."""

    @abc.abstractmethod
    async def get_installed_packages(self) -> list[str]:
        """Get a list of installed packages on the device."""

    async def get_qmodem_info(self) -> QModemInfo:
        """Get cellular modem status from QModem's modem_ctrl ubus subsystem (if available)."""
        info = QModemInfo()
        try:
            output = await self.execute_command("ubus call modem_ctrl info")
            if not output or "Not found" in output or "Method not found" in output:
                return info

            try:
                data = json.loads(output)
            except json.JSONDecodeError:
                return info
            
            info_list = data.get("info", [])
            if not info_list:
                return info

            info.enabled = True

            for info_item in info_list:
                modem_info_list = info_item.get("modem_info", [])
                
                current_context = None
                lte_signals = {}
                nr5g_signals = {}
                
                for item in modem_info_list:
                    class_origin = item.get("class_origin", "")
                    item_key = item.get("key", "")
                    value = item.get("value", "")
                    item_type = item.get("type", "")

                    if item_key == "LTE":
                        current_context = "LTE"
                    elif item_key.startswith("NR"):
                        current_context = "NR5G"

                    if class_origin == "Base Information":
                        if item_key == "manufacturer":
                            info.manufacturer = str(value) if value else ""
                        elif item_key == "revision":
                            info.revision = str(value) if value else ""
                        elif item_key == "temperature":
                            match = re.search(r"(\d+)", str(value))
                            info.temperature = int(match.group(1)) if match else None
                        elif item_key == "voltage":
                            match = re.search(r"(\d+)", str(value))
                            info.voltage = int(match.group(1)) if match else None
                        elif item_key == "connect_status":
                            info.connect_status = str(value) if value else ""
                    elif class_origin == "SIM Information":
                        if item_key == "SIM Status":
                            info.sim_status = str(value).replace("\n", " ").strip() if value else ""
                        elif item_key == "ISP":
                            info.isp = str(value).replace("\n", " ").strip() if value else ""
                        elif item_key == "SIM Slot":
                            info.sim_slot = str(value).replace("\n", " ").strip() if value else ""

                    elif item_type == "progress_bar" and class_origin == "Cell Information":
                        if current_context == "LTE":
                            lte_signals[item_key] = value
                        elif current_context == "NR5G":
                            nr5g_signals[item_key] = value

                def extract_int(val: Any, pattern: str = r"(-?\d+)") -> int | None:
                    match = re.search(pattern, str(val))
                    return int(match.group(1)) if match else None

                if "RSRP" in lte_signals:
                    info.lte_rsrp = extract_int(lte_signals["RSRP"])
                if "RSRQ" in lte_signals:
                    info.lte_rsrq = extract_int(lte_signals["RSRQ"])
                if "RSSI" in lte_signals:
                    info.lte_rssi = extract_int(lte_signals["RSSI"])
                if "SINR" in lte_signals:
                    info.lte_sinr = extract_int(lte_signals["SINR"], r"(\d+)")

                if "RSRP" in nr5g_signals:
                    info.nr5g_rsrp = extract_int(nr5g_signals["RSRP"])
                if "RSRQ" in nr5g_signals:
                    info.nr5g_rsrq = extract_int(nr5g_signals["RSRQ"])
                if "SINR" in nr5g_signals:
                    info.nr5g_sinr = extract_int(nr5g_signals["SINR"], r"(\d+)")

        except Exception as err:
            _LOGGER.debug("Error retrieving QModem info: %s", err)
            
        return info

    async def get_all_data(self) -> OpenWrtData:
        """Get all data in one call.

        Core data (device_info, system_resources, network_interfaces, connected_devices)
        must succeed or raise an exception to trigger UpdateFailed in coordinator.
        Optional modules may fail gracefully.
        """
        data = OpenWrtData()

        data.device_info = await self.get_device_info()
        data.system_resources = await self.get_system_resources()
        data.network_interfaces = await self.get_network_interfaces()
        data.connected_devices = await self.get_connected_devices()

        try:
            data.wireless_interfaces = await self.get_wireless_interfaces()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Optional wireless info failed: %s", err)

        try:
            data.dhcp_leases = await self.get_dhcp_leases()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Optional DHCP leases failed: %s", err)

        try:
            data.mwan_status = await self.get_mwan_status()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Optional MWAN status failed: %s", err)

        try:
            data.wps_status = await self.get_wps_status()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Optional WPS status failed: %s", err)

        try:
            data.qmodem_info = await self.get_qmodem_info()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Optional QModem info failed: %s", err)

        try:
            data.services = await self.get_services()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Optional services info failed: %s", err)

        try:
            data.leds = await self.get_leds()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Optional LEDs info failed: %s", err)

        try:
            data.firewall_redirects = await self.get_firewall_redirects()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Optional firewall info failed: %s", err)

        try:
            data.access_control = await self.get_access_control()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Optional access control info failed: %s", err)

        try:
            data.external_ip = await self.get_external_ip()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Optional external IP check failed: %s", err)

        return data
