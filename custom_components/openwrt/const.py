"""Constants for the OpenWrt integration."""

from __future__ import annotations

from typing import Final

from homeassistant.const import Platform

DOMAIN: Final = "openwrt"
DOCS_URL: Final = "https://github.com/FaserF/ha-openwrt"

CONF_HOST: Final = "host"
CONF_USERNAME: Final = "username"
CONF_PASSWORD: Final = "password"
CONF_PORT: Final = "port"
CONF_USE_SSL: Final = "use_ssl"
CONF_VERIFY_SSL: Final = "verify_ssl"
CONF_CONNECTION_TYPE: Final = "connection_type"
CONF_SSH_KEY: Final = "ssh_key"
CONF_UBUS_PATH: Final = "ubus_path"
CONF_UPDATE_INTERVAL: Final = "update_interval"
CONF_CONSIDER_HOME: Final = "consider_home"
CONF_TRACK_DEVICES: Final = "track_devices"
CONF_TRACK_WIRED: Final = "track_wired"
CONF_INTERFACE: Final = "interface"
CONF_SKIP_RANDOM_MAC: Final = "skip_random_mac"

CONNECTION_TYPE_UBUS: Final = "ubus"
CONNECTION_TYPE_LUCI_RPC: Final = "luci_rpc"
CONNECTION_TYPE_SSH: Final = "ssh"

DEFAULT_PORT_UBUS: Final = 80
DEFAULT_PORT_UBUS_SSL: Final = 443
DEFAULT_PORT_SSH: Final = 22
DEFAULT_PORT_LUCI_RPC: Final = 80
DEFAULT_USERNAME: Final = "root"
DEFAULT_UBUS_PATH: Final = "/ubus"
DEFAULT_UPDATE_INTERVAL: Final = 30
DEFAULT_CONSIDER_HOME: Final = 180
DEFAULT_TRACK_DEVICES: Final = True
DEFAULT_TRACK_WIRED: Final = True
DEFAULT_USE_SSL: Final = False
DEFAULT_VERIFY_SSL: Final = False
DEFAULT_SKIP_RANDOM_MAC: Final = False

DHCP_SOFTWARE_AUTO: Final = "auto"
DHCP_SOFTWARE_DNSMASQ: Final = "dnsmasq"
DHCP_SOFTWARE_ODHCPD: Final = "odhcpd"
DHCP_SOFTWARE_NONE: Final = "none"

DHCP_SOFTWARE_LIST: Final = [
    DHCP_SOFTWARE_AUTO,
    DHCP_SOFTWARE_DNSMASQ,
    DHCP_SOFTWARE_ODHCPD,
    DHCP_SOFTWARE_NONE,
]

CONF_DHCP_SOFTWARE: Final = "dhcp_software"

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.DEVICE_TRACKER,
    Platform.EVENT,
    Platform.LIGHT,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.UPDATE,
    Platform.IMAGE,
]

DATA_COORDINATOR: Final = "coordinator"
DATA_CLIENT: Final = "client"

ATTR_MANUFACTURER: Final = "OpenWrt"
ATTR_MODEL: Final = "Router"

OPENWRT_FIRMWARE_API: Final = "https://firmware-selector.openwrt.org/data/overview.json"
OPENWRT_RELEASE_API: Final = "https://downloads.openwrt.org/.versions.json"

CONF_CUSTOM_FIRMWARE_REPO: Final = "custom_firmware_repo"
CONF_CUSTOM_FIRMWARE_PATTERN: Final = "custom_firmware_pattern"
CONF_ASU_URL: Final = "asu_url"
CONF_TARGET_OVERRIDE: Final = "target_override"
CONF_AUTO_BACKUP: Final = "auto_backup"

SENSOR_TYPE_SYSTEM: Final = "system"
SENSOR_TYPE_NETWORK: Final = "network"
SENSOR_TYPE_WIRELESS: Final = "wireless"
SENSOR_TYPE_CLIENTS: Final = "clients"

SIGNAL_STRENGTH_MAP: Final = {
    range(-50, 0): "Excellent",
    range(-60, -50): "Good",
    range(-70, -60): "Fair",
    range(-80, -70): "Weak",
    range(-100, -80): "Very Weak",
}

SERVICE_REBOOT: Final = "reboot"
SERVICE_EXEC: Final = "execute_command"
SERVICE_INIT: Final = "manage_service"
SERVICE_WOL: Final = "wake_on_lan"
SERVICE_UCI_GET: Final = "uci_get"
SERVICE_UCI_SET: Final = "uci_set"
SERVICE_BACKUP: Final = "create_backup"
SERVICE_GENERATE_REPORT: Final = "generate_report"
