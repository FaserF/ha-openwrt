# Automation Examples

Here are some helpful automations to get you started with controlling and monitoring your OpenWrt device via Home Assistant.

## 🔄 Reboot Router Weekly

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

## 🚨 Notification on WAN Disconnect

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

## 🔌 Power-Cycle the Modem Only When the WAN Is Really Down

With mwan3, the `MWAN <iface> Online` binary sensor is also `off` while a line that answers again is still in its recovery checks. The `MWAN <iface> Status` sensor tells the two apart: `offline` and `disabled` mean the line is down (mwan3 switches between them during an outage), `connecting` means it is recovering. A template trigger keeps counting while the state changes between `offline` and `disabled`:

```yaml
alias: "Network: Power-Cycle Modem When WAN Is Down"
trigger:
  - platform: template
    value_template: "{{ states('sensor.openwrt_mwan_wan_status') in ['offline', 'disabled'] }}"
    for:
      minutes: 30
action:
  - service: switch.turn_off
    target:
      entity_id: switch.modem_plug
  - delay:
      seconds: 10
  - service: switch.turn_on
    target:
      entity_id: switch.modem_plug
```

A failed check during the recovery restarts mwan3's count but keeps the state at `connecting`, so a line that only answers now and then can stay there. A second trigger on the binary sensor being `off` for, say, two hours covers that case.

Note that Home Assistant does not keep a pending `for` across a restart or an automation reload. After a restart the count starts over once the integration reports the state; after a reload during an outage, the status must leave both `offline` and `disabled` and return to one of them before the trigger fires.

## 🔄 Firmware Update Notification

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

## 📡 Toggle Guest WiFi via Dashboard

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

## 🖥️ Execute Custom Command on Router

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

## 💡 LED Night Mode - Turn off LEDs at Night

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

## 🌐 Port Forwarding Security: Disable at Night

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

## 👶 Parental Control: Internet Schedule

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

## 🏎️ Dynamic Bandwidth Alert (Mbps)

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

## 📊 Alert When a Single Host Dominates Bandwidth

Trigger a notification when the top bandwidth consumer has transferred more than 10 GB in the current accounting period. Requires `nlbwmon` and the **Enable NLBWMon Top Hosts Sensor** option to be turned on.

```yaml
alias: "Network: Top Bandwidth Host Over 10 GB"
trigger:
  - platform: template
    value_template: >-
      {% set top = state_attr('sensor.openwrt_top_bandwidth_hosts', 'top_hosts') %}
      {{ top and top | length > 0 and top[0].total_bytes | int > 10737418240 }}
action:
  - service: notify.notify
    data:
      title: "📊 Heavy Bandwidth Consumer"
      message: >-
        {{ state_attr('sensor.openwrt_top_bandwidth_hosts', 'top_hosts')[0].hostname }}
        has used {{ state_attr('sensor.openwrt_top_bandwidth_hosts', 'top_hosts')[0].total }}
        (↓ {{ state_attr('sensor.openwrt_top_bandwidth_hosts', 'top_hosts')[0].download }}
        ↑ {{ state_attr('sensor.openwrt_top_bandwidth_hosts', 'top_hosts')[0].upload }}).
```

## 🔁 Auto-Reconnect on High Packet Errors

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

## 🚨 Notification on Public IP Change

Get notified as soon as your router gets a new external IP address.

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

## 🖥️ Wake on LAN: Wake PC via OpenWrt

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

## 🧠 High Resource Usage Alert (CPU/Memory)

Get notified early if your router is struggling with high load.

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

## 🙋‍♂️ Guest WiFi Automation Based on Presence

Automatically disable the Guest WiFi when everyone leaves the house to improve security.

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

## 🔐 VPN Failure Alert

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

## 📡 New Device Connection Alert

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

## 📦 Automatic Backup Before Update

Automatically trigger a configuration backup right before a firmware update.

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

## 📉 High Latency Notification

Monitor your internet connection quality and get notified if latency increases significantly.

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

## 🏎️ SQM Night Mode (Speed Boost)

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

## ⚡ WiFi Optimizer (Channel Scan)

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

## 📶 Modem: SMS Notification on ISP Change

If you have a dual-SIM or roaming modem, get notified when the carrier changes.

```yaml
alias: "Modem: ISP Notfier"
trigger:
  - platform: state
    entity_id: sensor.openwrt_qmodem_isp
action:
  - service: notify.notify
    data:
      title: "🌍 Modem Switched Network"
      message: "The router is now connected via {{ states('sensor.openwrt_qmodem_isp') }}."
```
