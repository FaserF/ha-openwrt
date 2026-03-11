# OpenWrt (for Homeassistant)

[![GitHub Release](https://img.shields.io/github/release/FaserF/ha-openwrt.svg?style=flat-square)](https://github.com/FaserF/ha-openwrt/releases)
[![License](https://img.shields.io/github/license/FaserF/ha-openwrt.svg?style=flat-square)](LICENSE)
[![hacs](https://img.shields.io/badge/HACS-custom-orange.svg?style=flat-square)](https://hacs.xyz)
[![CI Orchestrator](https://github.com/FaserF/ha-openwrt/actions/workflows/ci-orchestrator.yml/badge.svg)](https://github.com/FaserF/ha-openwrt/actions/workflows/ci-orchestrator.yml)

A secure, production-ready Home Assistant integration for OpenWrt devices. Monitor system resources, track connected devices, manage WiFi radios, execute commands, and natively update firmware directly from Home Assistant.

### Why use this integration?
While you can monitor routers via SNMP or ping trackers, this integration uses native OpenWrt APIs (Ubus/RPC) to provide deep, reliable integration without the overhead of polling generic network protocols. This means instant device tracking via modern ARP/NDP tables, full control over firewall rules and radios, and even the ability to compile firmware directly from your dashboard.

Supports **OpenWrt 25.12** and newer (older versions may also work, but are not directly tested and supported within this integration; update to the latest release if possible).

## ✨ Features

- **VPN Monitoring**: 
  - Tracks status (Up/Down) for WireGuard and OpenVPN tunnels.
  - Monitors throughput (RX/TX) and WireGuard peer counts with handshake details.
- **Network Health**: 
  - **Latency/Ping**: Monitor network latency to a target (e.g. 8.8.8.8) with packet loss tracking.
  - **DHCP Monitoring**: Track the number of active DHCP leases.
  - **Advanced Interface Diagnostics**: Individual sensors/attributes for IPv6 addresses, link speed (Mbps), duplex mode, and interface uptime.
- **Configurable Control**:
  - **WiFi TX Power**: Native slider to control transmission power of WiFi radios.
  - **SQM (Smart Queue Management)**: 
    - Control enabled state of SQM instances.
    - Set download and upload limits (Mbps) via native number sliders.
    - Diagnostic sensors for configured interface, qdisc, and setup script.
  - **Backup Service**: Trigger full router configuration backups (`sysupgrade -b`) directly from Home Assistant.
- **Smart Events**: 
  - Fires Home Assistant events (`openwrt_new_device`) when new, previously unknown MAC addresses connect to the network.
- **HA Repairs**: Native repair integration for authentication failures, connection issues, and missing packages.
- **Optimized Polling**: Parallel API calls, deduplicated wireless queries, and selective polling (slow-changing data like firewall rules is cached and refreshed less often) minimize router load.
- **Full Localization**: All entities support Home Assistant's native translation framework with English and German included out of the box.


## ❤️ Support This Project

> I maintain this integration in my **free time alongside my regular job** — bug hunting, new features, testing on real devices. Test hardware costs money, and every donation helps me stay independent and dedicate more time to open-source work.
>
> **This project is and will always remain 100% free.** There are no "Premium Upgrades", paid features, or subscriptions. Every feature is available to everyone.
>
> Donations are completely voluntary — but the more support I receive, the less I depend on other income sources and the more time I can realistically invest into these projects. 💪

<div align="center">

[![GitHub Sponsors](https://img.shields.io/badge/Sponsor%20on-GitHub-%23EA4AAA?style=for-the-badge&logo=github-sponsors&logoColor=white)](https://github.com/sponsors/FaserF)&nbsp;&nbsp;
[![PayPal](https://img.shields.io/badge/Donate%20via-PayPal-%2300457C?style=for-the-badge&logo=paypal&logoColor=white)](https://paypal.me/FaserF)

</div>


## 📦 Installation

### HACS (Recommended)

This integration is fully compatible with [HACS](https://hacs.xyz/).

1. Open HACS in Home Assistant.
2. Search for "OpenWrt".
3. Install and restart Home Assistant.

### Manual Installation

1. Download the latest release from the [Releases page](https://github.com/FaserF/ha-openwrt/releases).
2. Extract the `custom_components/openwrt` folder into your Home Assistant's `custom_components` directory.
3. Restart Home Assistant.

## ⚙️ Configuration

> 🛡️ **Security Note**: Before configuring the integration, please read our [Security Best Practices](SECURITY.md) regarding dedicated accounts, restricting permissions, and connection methods. Using `root` is supported but not recommended.

Adding your OpenWrt router is entirely done via the UI. **No YAML configuration is required.**

1. Navigate to **Settings > Devices & Services** in Home Assistant.
2. Click **Add Integration** and search for **OpenWrt**.
3. Follow the guided setup:
   - Select your connection method: **Ubus (HTTP/HTTPS)** is highly recommended.
   - Enter your router's IP/Hostname, Username (usually `root`), and Password.
   - For Ubus, ensure the `rpcd` package is installed on your router.

### Supported Connection Methods

| Feature | **Ubus (Recommended)** | **LuCI RPC** | **SSH** |
|:--- |:---:|:---:|:---:|
| **Performance** | 🚀 Very Fast | 🚄 Fast | 🐌 Slow |
| **Ease of Setup** | ✅ Easy | ✅ Easy | ⚠️ Complex |
| **Permissions** | 🛡️ Strict (ACLs) | 🔓 Permissive | 👑 Full (Root) |
| **Reliability** | ✅ High | ✅ High | ⚠️ Moderate |
| **Device Tracking** | ✅ Instant | ✅ Instant | ✅ Fast |
| **Backups/Update** | ✅ Full | ✅ Full | ❌ Limited |

#### 🔑 Which method should I choose?

1.  **Ubus (HTTP/HTTPS)**: The gold standard. If your router supports it and permissions are set up correctly, use this. It's the most stable and efficient.
2.  **LuCI RPC**: The perfect fallback. If Ubus is giving you "Access Denied" errors (common on newer OpenWrt SNAPSHOTs or restricted firmware), **switch to LuCI RPC**. It often has more permissive default access to system sensors like temperature and client lists.
3.  **SSH**: Use only if HTTP/HTTPS is not possible or if you need to bypass all RPC restictions entirely. Note that SSH causes higher CPU load on the router during polling.

### Required Permissions

If you are using a non-root user (e.g. for security reasons), you need to grant specific OpenWrt permissions (via `rpcd` ACLs or LuCI) to utilize all features of this integration:

| Subsystem | Description | Write Permission Required for |
|-----------|-------------|-------------------------------|
| **System** | Read router info, stats (Hostname, Load, Memory, Temp, Storage) | Rebooting router, sysupgrade, backups |
| **Network** | Read interfaces, bytes/packets counters, speeds | Toggling & reconnecting interfaces |
| **Wireless** | Read WiFi radios, SSIDs, signal levels, client lists | Toggling radios/SSIDs, WPS control |
| **Firewall** | Read firewall rules & port forwards | Toggling rules/forwards, Parental Control (Device Blocking) |
| **Devices** | Read DHCP Leases, ARP/Neighbor table (Connected devices) | Wake on LAN, Kicking wireless clients |
| **VPN** | Read WireGuard & OpenVPN status | - |
| **SQM** | Read SQM instance status | Toggling SQM, Changing bandwidth limits |
| **Services** | Read active system services (OpenVPN, AdGuard, etc.) | Toggling & restarting services |
| **LEDs** | Read current state of router LEDs | Toggling LEDs, changing brightness |
| **MWAN3** | Read Multi-WAN load balancing status | - |

During setup, the integration will check your user's permissions and display a summary of available features.

### Required Packages

Some features require additional OpenWrt packages to be installed on your router. During setup, the integration will check if these are installed.

| Package | Missing Features |
|---------|------------------|
| **sqm-scripts** | SQM QoS Settings (Limits, Toggles) |
| **mwan3** | MWAN3 Sensors (Load balancing status) |
| **iwinfo** | Enhanced WiFi Info (Bitrate, detailed signal diagnostics) |
| **etherwake** | Wake on LAN functionality |
| **wireguard-tools** | WireGuard VPN Sensors |
| **openvpn** | OpenVPN Sensors |

## 🛠️ Options Flow

After configuration, click **Configure** on the integration page to adjust:
- **Update Interval**: How frequently to poll data (default 30s).
- **Device Tracking**: Enable/disable device tracking and include/exclude wired devices.
- **Consider Home**: Set the grace period (in seconds) for device presence detection to prevent "presence flickering".
- **DHCP Software**: Manual selection of DHCP software (`dnsmasq`, `odhcpd`, or `auto-detect`) for more reliable device identification across different OpenWrt setups.
- **Advanced Tracking**: Integration with `ip neigh` (ARP/NDP) to track connected devices even if they are not active in the DHCP lease table (e.g., static IPs or wired devices).
- **Firewall Rule Control**: Expose named OpenWrt firewall rules as switches in Home Assistant. This allows for granular control over internet access, port forwards, and more directly from your dashboard.
- **Custom Firmware Repo**: Provide a GitHub repo (e.g., `owner/repo`) if you use custom OpenWrt community builds.

## 📖 Automation Examples

<details>
<summary><strong>🔄 Reboot Router Weekly</strong></summary>

```yaml
alias: "Router: Weekly Reboot"
trigger:
  - platform: time
    at: "03:00:00"
condition:
  - condition: time
    weekday:
      - sun
action:
  - device_id: <YOUR_OPENWRT_DEVICE_ID>
    domain: button
    entity_id: button.openwrt_reboot_router
    type: press
```
</details>

<details>
<summary><strong>🚨 Notification on WAN Disconnect</strong></summary>

```yaml
alias: "Router: WAN Disconnect Notification"
trigger:
  - platform: state
    entity_id: binary_sensor.openwrt_wan_connected
    to: "off"
    for:
      minutes: 1
action:
  - service: notify.notify
    data:
      title: "🚨 Internet Connection Lost"
      message: "The main WAN interface on the OpenWrt router went down."
```
</details>

<details>
<summary><strong>🔄 Firmware Update Notification</strong></summary>

```yaml
alias: "Router: Firmware Update Available"
trigger:
  - platform: state
    entity_id: update.openwrt_firmware
    attribute: latest_version
action:
  - service: notify.notify
    data:
      title: "🔄 OpenWrt Update Available"
      message: >-
        A new firmware update ({{ state_attr('update.openwrt_firmware', 'latest_version') }})
        is available for your router!
```
</details>

<details>
<summary><strong>📡 Toggle Guest WiFi via Dashboard</strong></summary>

```yaml
alias: "Router: Toggle Guest WiFi"
trigger:
  - platform: state
    entity_id: input_boolean.guest_wifi_toggle
action:
  - service: switch.turn_{{ trigger.to_state.state }}
    target:
      entity_id: switch.openwrt_wireless_guest
```
</details>

<details>
<summary><strong>🖥️ Execute Custom Command on Router</strong></summary>

```yaml
alias: "Router: Clear DNS Cache"
trigger:
  - platform: state
    entity_id: input_button.clear_router_dns
action:
  - service: openwrt.execute_command
    data:
      command: "/etc/init.d/dnsmasq restart"
    target:
      device_id: <YOUR_OPENWRT_DEVICE_ID>
```
</details>

<details>
<summary><strong>💡 LED Night Mode - Turn off LEDs at Night</strong></summary>

Turn off all router LEDs after midnight and turn them back on in the morning.

```yaml
alias: "Router: LED Night Mode Off"
trigger:
  - platform: time
    at: "00:00:00"
action:
  - service: light.turn_off
    target:
      entity_id:
        - light.openwrt_led_power
        - light.openwrt_led_wan
        - light.openwrt_led_wireless

---

alias: "Router: LED Morning Mode On"
trigger:
  - platform: time
    at: "07:00:00"
action:
  - service: light.turn_on
    target:
      entity_id:
        - light.openwrt_led_power
        - light.openwrt_led_wan
        - light.openwrt_led_wireless
```
</details>

<details>
<summary><strong>🌐 Port Forwarding Security: Disable at Night</strong></summary>

Automatically disable sensitive port forwarding rules during night hours to reduce your attack surface.

```yaml
alias: "Security: Disable Port Forwards (Night)"
trigger:
  - platform: time
    at: "23:00:00"
action:
  - service: switch.turn_off
    target:
      entity_id:
        - switch.openwrt_port_forward_ssh_external
        - switch.openwrt_port_forward_vpn_server
```
</details>

<details>
<summary><strong>👶 Parental Control: Internet Schedule</strong></summary>

Automatically disable internet access for specific devices during homework or bed time. Uses the Fritz-style "Internet Access" switches.

```yaml
alias: "Guard: Child Internet Off (Bedtime)"
trigger:
  - platform: time
    at: "20:30:00"
action:
  - service: switch.turn_off
    target:
      entity_id:
        - switch.openwrt_internet_access_ipad_kids
        - switch.openwrt_internet_access_gaming_pc
```
</details>

<details>
<summary><strong>🏎️ Dynamic Bandwidth Alert (Mbps)</strong></summary>

Get notified if a specific interface exceeds a throughput threshold (e.g. 100 Mbps) for longer than 10 minutes.

```yaml
alias: "Network: High Sustained Throughput"
trigger:
  - platform: numeric_state
    entity_id: sensor.openwrt_wan_rx_rate
    above: 100
    for:
      minutes: 10
action:
  - service: notify.mobile_app_faserf
    data:
      title: "🏎️ Sustained High Download Rate"
      message: "WAN interface has been saturating over 100Mbps for 10 minutes."
```
</details>

<details>
<summary><strong>🔁 Auto-Reconnect on High Packet Errors</strong></summary>

If the WAN interface accumulates more than 500 errors (monitored via the consolidated attributes), trigger an interface reconnect.

```yaml
alias: "Network: Reconnect on Errors"
trigger:
  - platform: template
    value_template: "{{ state_attr('sensor.openwrt_wan_rx', 'errors') | int > 500 }}"
action:
  - service: button.press
    target:
      entity_id: button.openwrt_reconnect_wan
```
</details>

<details>
<summary><strong>🚨 Notification on Public IP Change</strong></summary>

Useful for home lab users without DDNS. Get notified as soon as your router gets a new external IP address.

```yaml
alias: "Network: Public IP Changed"
trigger:
  - platform: state
    entity_id: sensor.openwrt_public_ip
action:
  - service: notify.notify
    data:
      title: "🌐 Router IP Updated"
      message: "The new public IP address is {{ trigger.to_state.state }}"
```
</details>

```yaml
alias: "Router: Network Error Alert"
trigger:
  - platform: template
    value_template: "{{ state_attr('sensor.openwrt_wan_rx', 'errors') | int > 100 }}"
action:
  - service: notify.notify
    data:
      title: "⚠️ Network Errors Detected"
      message: >-
        More than 100 RX errors detected on WAN.
        This may indicate cable or hardware issues.
```
</details>

<details>
<summary><strong>🖥️ Wake on LAN: Wake PC via OpenWrt</strong></summary>

Wakes up your PC when you arrive home or via an input button.

```yaml
alias: "Automation: Wake Gaming PC"
trigger:
  - platform: state
    entity_id: input_button.wake_pc
action:
  - service: openwrt.wake_on_lan
    data:
      target: <YOUR_OPENWRT_ENTRY_ID>
      mac: "AA:BB:CC:DD:EE:FF"
      interface: "br-lan"
```
</details>

<details>
<summary><strong>🧠 High Resource Usage Alert (CPU/Memory)</strong></summary>

Get notified early if your router is struggling with high load, potentially preventing network outages or indicating a runaway background process.

```yaml
alias: "Router: High Resource Usage Alert"
trigger:
  - platform: numeric_state
    entity_id: sensor.openwrt_cpu_load_1m
    above: 4.0
    for:
      minutes: 5
  - platform: numeric_state
    entity_id: sensor.openwrt_memory_usage
    above: 90
    for:
      minutes: 5
action:
  - service: notify.notify
    data:
      title: "⚠️ Router Overload Warning"
      message: >-
        The OpenWrt router is experiencing sustained high resource usage!
        Trigger: {{ trigger.entity_id }} is currently at {{ trigger.to_state.state }}.
```
</details>

<details>
<summary><strong>🙋‍♂️ Guest WiFi Automation Based on Presence</strong></summary>

Automatically enable the Guest WiFi when a specific "Guest Mode" input boolean is turned on, or disable it when everyone leaves the house to improve security and reduce airtime congestion.

```yaml
alias: "WiFi: Auto-Disable Guest Network"
trigger:
  - platform: state
    entity_id: zone.home
    to: "0"  # Everyone left home
    for:
      minutes: 10
action:
  - service: switch.turn_off
    target:
      entity_id: switch.openwrt_wireless_guest
```
</details>

<details>
<summary><strong>🔁 Daily Router Reboot (Scheduled Maintenance)</strong></summary>

Some specific setups or failing modems require a daily reboot. You can easily schedule this via Home Assistant natively rather than relying on OpenWrt cronjobs.

```yaml
alias: "Router: Daily Maintenance Reboot"
trigger:
  - platform: time
    at: "04:00:00"
action:
  - service: button.press
    target:
      entity_id: button.openwrt_reboot_router
```
</details>

<details>
<summary><strong>🔐 VPN Failure Alert</strong></summary>

Get notified immediately if a specific VPN tunnel (WireGuard or OpenVPN) goes down.

```yaml
alias: "Security: VPN Tunnel Down"
trigger:
  - platform: state
    entity_id: binary_sensor.openwrt_vpn_wg0_up
    to: "off"
    for:
      seconds: 30
action:
  - service: notify.notify
    data:
      title: "🔐 VPN Outage"
      message: "VPN Interface wg0 has disconnected!"
```
</details>

<details>
<summary><strong>📡 New Device Connection Alert</strong></summary>

Use the `openwrt_new_device` event to get notified whenever a new, previously unknown device connects to your network for the first time.

```yaml
alias: "Security: New Device Detected"
trigger:
  - platform: event
    event_type: openwrt_new_device
action:
  - service: notify.notify
    data:
      title: "📡 New Device Found"
      message: "A new device with MAC {{ trigger.event.data.mac }} connected to {{ trigger.event.data.host }}."
```
</details>

<details>
<summary><strong>📦 Automatic Backup Before Update</strong></summary>

Automatically trigger a configuration backup right before a firmware update to ensure you can always restore your settings even if a flash goes wrong.

```yaml
alias: "System: Auto-Backup on Update"
trigger:
  - platform: state
    entity_id: update.openwrt_firmware
    to: "installing"
action:
  - service: openwrt.create_backup
    data:
      entry_id: <YOUR_OPENWRT_ENTRY_ID>
```
</details>

<details>
<summary><strong>📉 High Latency Notification</strong></summary>

Monitor your internet connection quality and get notified if latency increases significantly, which might indicate ISP issues or network congestion.

```yaml
alias: "Health: High WAN Latency"
trigger:
  - platform: numeric_state
    entity_id: sensor.openwrt_wan_latency
    above: 50
    for:
      minutes: 5
action:
  - service: notify.notify
    data:
      title: "📉 Network Latency Spike"
      message: "Current WAN latency is {{ states('sensor.openwrt_wan_latency') }}ms."
```
</details>

<details>
<summary><strong>🏎️ SQM Night Mode (Speed Boost)</strong></summary>

Automatically increase SQM bandwidth limits during night hours when network contention is lower.

```yaml
alias: "Network: SQM Night Speed Boost"
trigger:
  - platform: time
    at: "01:00:00"
action:
  - service: number.set_value
    target:
      entity_id: number.openwrt_sqm_eth1_download
    data:
      value: 200
  - service: number.set_value
    target:
      entity_id: number.openwrt_sqm_eth1_upload
    data:
      value: 100

---

alias: "Network: SQM Day Speed Limit"
trigger:
  - platform: time
    at: "08:00:00"
action:
  - service: number.set_value
    target:
      entity_id: number.openwrt_sqm_eth1_download
    data:
      value: 100
  - service: number.set_value
    target:
      entity_id: number.openwrt_sqm_eth1_upload
    data:
      value: 50
```
</details>

<details>
<summary><strong>⚡ WiFi Optimizer (Channel Scan)</strong></summary>

Trigger a wireless optimization scan via custom command if high latency or packet loss is detected on a wireless interface.

```yaml
alias: "WiFi: Optimize on High Latency"
trigger:
  - platform: numeric_state
    entity_id: sensor.openwrt_wan_latency
    above: 100
    for:
      minutes: 2
action:
  - service: openwrt.execute_command
    data:
      command: "wifi down && wifi up"
      entry_id: <YOUR_OPENWRT_ENTRY_ID>
```
</details>

## 🧑‍💻 Development

This project uses modern Python development tools:
- `ruff` for linting and formatting
- `mypy` for static typing
- `pytest` for unit testing

### Setup

```bash
python3 -m venv venv
source venv/bin/activate
make install
```

### Pre-commit

Before committing, run tests and linters:
```bash
make check
```

## 💖 Credits & Acknowledgements

This integration was built from the ground up to replace and modernize the deprecated community project, ensuring long-term maintainability and eliminating persistent edge-case bugs.

A special thanks to:
- **[kvj/hass_openwrt](https://github.com/kvj/hass_openwrt)**: The original repository which served as the inspiration and reference for OpenWrt integration concepts.
- **[Home Assistant `fritz` Integration](https://github.com/home-assistant/core/tree/dev/homeassistant/components/fritz)**: The official Fritz!Box integration, which served as the gold standard for feature parity, particularly regarding the robust `device_tracker` scanner implementation and multi-platform architecture.

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
