"""OpenWrt sensors package."""

from __future__ import annotations

from .bandwidth import (
    OpenWrtNlbwmonRxSensor,
    OpenWrtNlbwmonTopHostsSensor,
    OpenWrtNlbwmonTxSensor,
    _create_nlbwmon_sensors,
)
from .base import (
    OpenWrtSensorDescription,
    OpenWrtSensorEntity,
    OpenWrtStorageSensorDescription,
    _bytes_to_mb,
    _format_bytes,
)
from .device import (
    OpenWrtDeviceSensor,
    _create_device_sensors,
    _get_device_display_name,
)
from .mesh import (
    _create_batman_neighbor_sensors,
    _get_batman_global_sensors,
)
from .mwan import (
    OpenWrtMwanMetricSensor,
    _create_mwan_sensors,
)
from .network import (
    _async_setup_network_sensors,
    _create_net_address_sensors,
    _create_net_rate_sensors,
    _create_net_sensors,
    _create_net_status_sensors,
    _create_net_traffic_sensors,
    _create_vpn_sensors,
)
from .qmodem import (
    OpenWrtQModemSensorEntity,
    _get_qmodem_sensors,
)
from .services import (
    OpenWrtSnortSensor,
    _async_setup_specialized_sensors,
    _create_lldp_sensors,
    _create_sqm_sensors,
    _get_adblock_sensors,
    _get_banip_sensors,
    _get_simple_adblock_sensors,
    _get_upnp_sensors,
)
from .storage import (
    OpenWrtStorageSensor,
    _async_setup_storage_sensors,
)
from .system import (
    OpenWrtTemperatureSensor,
    _async_setup_system_sensors,
    _get_system_sensors,
)
from .wireguard import (
    OpenWrtWireGuardPeerSensor,
    _async_setup_wireguard_sensors,
)
from .wireless import (
    OpenWrtWifiSensorEntity,
    _async_setup_wireless_sensors,
    _create_wifi_base_sensors,
    _create_wifi_sensors,
    _create_wifi_station_sensors,
)

__all__ = [
    "OpenWrtDeviceSensor",
    "OpenWrtMwanMetricSensor",
    "OpenWrtNlbwmonRxSensor",
    "OpenWrtNlbwmonTopHostsSensor",
    "OpenWrtNlbwmonTxSensor",
    "OpenWrtQModemSensorEntity",
    "OpenWrtSensorDescription",
    "OpenWrtSensorEntity",
    "OpenWrtSnortSensor",
    "OpenWrtStorageSensor",
    "OpenWrtStorageSensorDescription",
    "OpenWrtTemperatureSensor",
    "OpenWrtWifiSensorEntity",
    "OpenWrtWireGuardPeerSensor",
    "_async_setup_network_sensors",
    "_async_setup_specialized_sensors",
    "_async_setup_storage_sensors",
    "_async_setup_system_sensors",
    "_async_setup_wireguard_sensors",
    "_async_setup_wireless_sensors",
    "_bytes_to_mb",
    "_create_batman_neighbor_sensors",
    "_create_device_sensors",
    "_create_lldp_sensors",
    "_create_mwan_sensors",
    "_create_net_address_sensors",
    "_create_net_rate_sensors",
    "_create_net_sensors",
    "_create_net_status_sensors",
    "_create_net_traffic_sensors",
    "_create_nlbwmon_sensors",
    "_create_sqm_sensors",
    "_create_vpn_sensors",
    "_create_wifi_base_sensors",
    "_create_wifi_sensors",
    "_create_wifi_station_sensors",
    "_format_bytes",
    "_get_adblock_sensors",
    "_get_banip_sensors",
    "_get_batman_global_sensors",
    "_get_device_display_name",
    "_get_qmodem_sensors",
    "_get_simple_adblock_sensors",
    "_get_system_sensors",
    "_get_upnp_sensors",
]
