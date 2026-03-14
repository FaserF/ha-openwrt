# Security Best Practices for Home Assistant OpenWrt Integration

When integrating your OpenWrt router with Home Assistant, it is highly recommended to follow security best practices to protect your network infrastructure. Giving Home Assistant root access to your router can be a significant security risk.

## 1. Do I need a dedicated account?
**Yes**, it is highly recommended to create a dedicated user account (e.g., `homeassistant`) on your OpenWrt router specifically for this integration, rather than using the default `root` account. This ensures that Home Assistant only has access to the resources it needs and follows the **Principle of Least Privilege**.

## 2. What type of connection should I use?
- **Ubus (via HTTPS)**: This is the **strongly recommended** connection method. Ubus is the built-in RPC interface for OpenWrt. It is fast, modern, and supports fine-grained access control (ACLs). You should ensure your router's uhttpd server is configured for HTTPS so that credentials and RPC traffic are encrypted between Home Assistant and OpenWrt.
- **LuCI RPC**: Acceptable as a fallback, but relies on the LuCI web interface routing.
- **SSH**: Not recommended for security-conscious setups. While SSH keys are secure, providing shell access to a non-admin service is generally harder to lock down securely compared to Ubus ACLs.

## 3. How do I restrict permissions (ACLs)?

When using the recommended Ubus connection, OpenWrt allows you to define strict **Access Control Lists (ACLs)** using the `rpcd` daemon.
Instead of adding your `homeassistant` user to the `root` group, you should define a custom read/write ACL group that only allows access to the specific Ubus namespaces the integration needs.

### Automated Secure Provisioning (Recommended)
The Home Assistant OpenWrt integration includes an **automated provisioning feature**. When you first connect as `root`, the integration will offer to automatically:
1. Create a dedicated `homeassistant` system user.
2. Generate a secure, unique password.
3. Create the necessary ACL files at `/usr/share/rpcd/acl.d/homeassistant.json`.
4. Configure UCI and RPC permissions.
5. Restart the required services (`rpcd` and `uhttpd`) to apply the changes.

This is the easiest and recommended way to secure your integration without manually editing configuration files via SSH.

### Manual Alternative: Creating a Restricted Ubus User
If you prefer to set up the permissions manually, follow these steps:

1. **Create the user** on your OpenWrt router (you may need to install `shadow-useradd` first, or manually edit `/etc/passwd` and `/etc/shadow`):
   ```bash
   opkg update && opkg install shadow-useradd
   useradd -m -s /bin/false homeassistant
   passwd homeassistant
   ```

2. **Create the ACL definition file** at `/usr/share/rpcd/acl.d/homeassistant.json`:
   ```json
   {
       "homeassistant": {
           "description": "Home Assistant Integration Access",
           "read": {
               "ubus": {
                   "system": [ "info", "board" ],
                   "network.device": [ "status" ],
                   "network.interface": [ "dump", "status" ],
                   "iwinfo": [ "info", "devices", "assoclist" ],
                   "hostapd.*": [ "get_clients" ],
                   "file": [ "read" ],
                   "dhcp": [ "ipv4leases", "ipv6leases" ]
               },
               "uci": [ "wireless", "network", "dhcp", "system" ]
           },
           "write": {
               "ubus": {
                   "system": [ "reboot" ],
                   "network.interface": [ "up", "down" ],
                   "hostapd.*": [ "bss_transition", "wnm_disassoc" ],
                   "file": [ "exec" ]
               },
               "uci": [ "wireless", "network" ]
           }
       }
   }
   ```
   *Note: This is an example. Depending on the exact features you use (e.g., toggling WiFi, rebooting, running commands), you might need to adjust the exposed methods.*

3. **Assign the ACL to the user** by editing `/etc/config/rpcd`:
   ```bash
   uci add rpcd login
   uci set rpcd.@login[-1].username='homeassistant'
   uci set rpcd.@login[-1].password='$p$homeassistant'
   uci add_list rpcd.@login[-1].read='homeassistant'
   uci add_list rpcd.@login[-1].write='homeassistant'
   uci commit rpcd
   /etc/init.d/rpcd restart
   ```

## 4. What are the disadvantages and concerns of using `root`?
Using the `root` account means Home Assistant has **unrestricted administrative access** to your network's main gateway.
- **Complete Network Compromise**: If your Home Assistant instance is somehow compromised (e.g., via an exposed dashboard, a vulnerable third-party add-on, or a leaked token), the attacker instantly gains full control over your router. They could reroute DNS, intercept traffic, open firewall ports, or install malicious firmware.
- **Accidental Damage**: A software bug in the integration, or an accidental misconfiguration in an automation on the Home Assistant side, could unintentionally change critical router settings or brick the device.
- **Auditability**: When you use a dedicated account, system logs on OpenWrt will clearly show which actions were performed by Home Assistant versus actions performed by you as the administrator. Using `root` makes it impossible to distinguish between the two.
