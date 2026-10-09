# Configuration

> 🛡️ **Security Note**: Before configuring the integration, please read our [Security Best Practices](https://github.com/FaserF/ha-openwrt/blob/main/SECURITY.md) regarding dedicated accounts, restricting permissions, and connection methods. Using `root` is supported but not recommended.

Adding your OpenWrt router is entirely done via the UI. **No YAML configuration is required.**

1. Navigate to **Settings > Devices & Services** in Home Assistant.
2. Click **Add Integration** and search for **OpenWrt**.
3. Follow the guided setup:
    - Select your connection method: **Ubus (HTTP/HTTPS)** is highly recommended.
    - Enter your router's IP/Hostname, Username (usually `root`), and Password.
    - For Ubus, ensure the `uhttpd-mod-ubus` package is installed on your router.
    - **Secure Provisioning**: If connecting as `root`, the integration will offer to automatically create a dedicated, least-privilege `homeassistant` user and configure all required ACL permissions for you.
    - **Connection Diagnostics**: If setup fails, a detailed diagnostic report will be displayed to help you identify the cause (e.g. firewall blocking, missing packages, or incorrect ACLs).

## Supported Connection Methods

| Feature | **Ubus (Recommended)** | **LuCI RPC** | **SSH** |
|:--- |:---:|:---:|:---:|
| **Performance** | 🚀 Very Fast | 🚄 Fast | 🐌 Slow |
| **Ease of Setup** | ✅ Easy | ✅ Easy | ⚠️ Complex |
| **Permissions** | 🛡️ Strict (ACLs) | 🔓 Permissive | 👑 Full (Root) |
| **Reliability** | ✅ High | ✅ High | ⚠️ Moderate |
| **Device Tracking** | ✅ Instant | ✅ Instant | ✅ Fast |
| **Backups/Update** | ✅ Full | ✅ Full | ❌ Limited |

### 🔑 Which method should I choose?
1. **Ubus (HTTP/HTTPS)**: The gold standard. If your router supports it and permissions are set up correctly, use this. It's the most stable and efficient.
2. **LuCI RPC**: The perfect fallback. If Ubus is giving you "Access Denied" errors (common on newer OpenWrt SNAPSHOTs or restricted firmware), **switch to LuCI RPC**. It often has more permissive default access to system sensors like temperature and client lists.
3. **SSH**: Use only if HTTP/HTTPS is not possible or if you need to bypass all RPC restrictions entirely. Note that SSH causes higher CPU load on the router during polling.

### ⚠️ Known Ubus Limitations

When using the **Ubus** connection method, some sensors may be unavailable (showing as 0 or missing) depending on your OpenWrt version and RPC ACL configuration:

- **CPU Usage**: Many OpenWrt releases do not expose CPU statistics directly via Ubus. The integration attempts to read `/proc/stat`, but this is often restricted by default `rpcd` ACLs. If CPU usage stays at 0%, this is likely a permission limitation.
- **System Temperature**: Access to thermal data (e.g., `/sys/class/thermal/...`) is frequently restricted via Ubus. If the temperature sensor is missing, it's due to these security restrictions.
- **Disk/Storage Usage**: Similar to CPU/Temp, detailed filesystem info might be restricted.

**Solution**: If these sensors are vital for you, switch the connection method to **LuCI RPC** or **SSH**, or manually grant `file` read access to `/proc/stat` and `/sys` in your router's `/etc/config/rpcd` or `/usr/share/rpcd/acl.d/`.

## Required Permissions

If you are using a non-root user (e.g. for security reasons), you need to grant specific OpenWrt permissions (via `rpcd` ACLs or LuCI) to utilize all features of this integration:

| Subsystem | Description | Write Permission Required for |
|-----------|-------------|-------------------------------|
| **System** | Read router info, stats (Hostname, Load, Memory, Temp, Storage) | Rebooting router, sysupgrade, backups |
| **Network** | Read interfaces, bytes/packets counters, speeds | Toggling & reconnecting interfaces |
| **Wireless** | Read WiFi radios, SSIDs, signal levels, client lists | Toggling radios/SSIDs, WPS control |
| **Firewall** | Read firewall rules & port forwards | Toggling rules/forwards, Parental Control (Device Blocking) |
| **Devices** | Read DHCP Leases, ARP/Neighbor table (Connected devices) | Wake on LAN, Kicking wireless clients |
| **VPN** | Read WireGuard, OpenVPN & Tailscale status (Tailscale via `tailscale status --json`, requires `file.exec` for `/bin/sh`) | - |
| **UPnP** | Read active UPnP/NAT-PMP port mappings | - |
| **SQM** | Read SQM instance status | Toggling SQM, Changing bandwidth limits |
| **Services** | Read active system services (OpenVPN, AdGuard, etc.) | Toggling & restarting services |
| **LEDs** | Read current state of router LEDs | Toggling LEDs, changing brightness |
| **MWAN3** | Read Multi-WAN load balancing status | - |
| **MQTT Presence** | Automatic deployment of presence scripts | Deploying/Updating scripts (requires `file.exec`) |
| **NLBWMon** | Top Bandwidth Hosts sensor — reads per-host traffic via `nlbw` CLI (requires `file.exec` for `/usr/sbin/nlbw`) | - |

During setup, the integration will check your user's permissions and display a summary of available features.

## Required Packages

Some features require additional OpenWrt packages to be installed on your router. During setup, the integration will check if these are installed.

| Package | Missing Features |
|---------|------------------|
| **sqm-scripts** | SQM QoS Settings (Limits, Toggles) |
| **mwan3** | MWAN3 Sensors (Load balancing status) |
| **iwinfo** | Enhanced WiFi Info (Bitrate, detailed signal diagnostics) |
| **etherwake** | Wake on LAN functionality |
| **wireguard-tools** | WireGuard VPN Sensors |
| **openvpn** | OpenVPN Sensors |
| **tailscale** | Tailscale VPN Sensors (enable the VPN option) |
| **kmod-batman-adv** | Batman-adv Mesh Support (Kernel module) |
| **batctl-full** | Batman-adv Control (Required for mesh data) |
| **nlbwmon** | NLBWMon Top Bandwidth Hosts sensor (opt-in, requires `file.exec` rpcd ACL) |
