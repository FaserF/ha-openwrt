# OpenWrt for Home Assistant

A secure, production-ready Home Assistant integration for OpenWrt devices. Monitor system resources, track connected devices, manage WiFi radios, execute commands, and natively update firmware directly from Home Assistant.

## ✨ Features

- **VPN Monitoring**:
    - Tracks status (Up/Down) for WireGuard and OpenVPN tunnels.
    - Monitors throughput (RX/TX) and detailed WireGuard peer statistics (handshake, transfer, allowed IPs).
    - Tailscale: backend state, node IP, key expiry, problem indicator (health warnings / login required) and online peer count. Per-peer connectivity sensors and tunnel traffic counters are created disabled by default.
- **Network & Connectivity**:
    - **Latency/Ping**: Monitor network latency to a target (e.g. 8.8.8.8) with packet loss tracking.
    - **DHCP Monitoring**: Track the number of active DHCP leases.
    - **Public IP**: Track your external WAN IP address with change detection.
    - **Advanced Interface Diagnostics**: Individual sensors/attributes for IPv6 addresses, link speed (Mbps), duplex mode, and interface uptime.
- **4G/5G QModem Support (ModemManager integration)**:
    - Comprehensive signal diagnostics: RSRP, RSRQ, RSSI, SINR for LTE and 5G.
    - Device health: Modem temperature, voltage, and ISP detection.
    - SIM status: Tracking SIM slots and connectivity state.
- **Configurable Control**:
    - **WiFi TX Power**: Native slider to control transmission power of WiFi radios.
    - **SQM (Smart Queue Management)**:
        - Control enabled state of SQM instances.
        - Set download and upload limits (Mbps) via native number sliders.
        - Diagnostic sensors for configured interface, qdisc, and setup script.
    - **Service Management**: Monitor, start, stop, and restart system services (e.g., AdGuard Home, OpenVPN, Samba).
    - **Security & Ad-Blocking**:
        - **AdBlock/Simple-AdBlock**: Track status, blocked domain counts, and toggle filtering.
        - **Ban-IP**: Monitor banned IP counts and service state.
- **System Monitoring**:
    - **Resource Usage**: Monitor CPU usage, Memory (Total/Used/Free/Cached/Buffered), and Swap.
    - **Storage**: Track disk usage and free space for multiple mount points.
    - **Process Monitoring**: Tracks top CPU and memory consuming processes for real-time performance troubleshooting.
    - **System Logs**: Diagnostic sensor that monitors for critical system errors.
    - **USB Devices**: Monitor connected hardware via USB ports.
    - **Reboot Required**: Alerts you when the router needs a restart (e.g., after kernel updates).
- **Backup & Commands**: Trigger configuration backups or execute arbitrary shell commands directly from HA.
- **Parental Control & Device Management**:
    - **Internet Access Control**: Per-device "Internet Access" switches to block/allow traffic (Fritz!Box style).
    - **Wireless Management**: WPS control switches and buttons to disconnect specific wireless clients.
- **Smart Tracking & Events**:
    - **Multi-source Device Tracking**: Combines DHCP leases with ARP/NDP tables (`ip neigh`) for instant and reliable presence detection.
    - **Persistent History**: Tracks `initially_seen` and `last_seen` timestamps for every device, persisting across Home Assistant restarts.
    - **Connection Type Detection**: Automatically identifies if a device is connected via `wired` or a specific WiFi band (`2.4GHz`, `5GHz`, `6GHz`).
    - **Topology Mapping**: Wireless clients are automatically linked to their respective Access Point via the `via_device` attribute.
    - **Infrastructure Filtering**: Automatically identifies and filters out the router's own network interfaces to prevent circular self-tracking.
    - **New Device Event**: Fires `openwrt_new_device` when previously unknown MAC addresses are discovered.
- **MQTT Presence Detection (Optional)**:
    - Integration with the third-party [OpenWRT_HA_Presence](https://github.com/f45tb00t/OpenWRT_HA_Presence) scripts.
    - High-performance, low-latency tracking via MQTT events instead of polling.
    - Automatic script deployment and configuration directly from the Home Assistant UI.
- **Batman-adv Mesh Support**:
    - **Topology Overview**: Monitor mesh neighbors, originators (nodes), and gateways.
    - **Link Quality**: Track Transmit Quality (TQ) sensors for each mesh neighbor.
    - **Client Routing**: Automatically track which mesh node a mobile client is currently connected to.
    - **Status**: Monitor mesh activity status.

### Why use this integration?

While you can monitor routers via SNMP or ping trackers, this integration uses native OpenWrt APIs (Ubus/RPC) to provide deep, reliable integration without the overhead of polling generic network protocols. This means instant device tracking via modern ARP/NDP tables, full control over firewall rules and radios, and even the ability to compile firmware directly from your dashboard.

Supports **OpenWrt 25.12** and newer (older versions are supported via `opkg` fallback). This integration natively supports both `apk` and `opkg` package managers, ensuring future compatibility with the latest OpenWrt releases.
