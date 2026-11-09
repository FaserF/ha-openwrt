# Security Best Practices for Home Assistant OpenWrt Integration

When integrating your OpenWrt router with Home Assistant, it is highly recommended to follow security best practices to protect your network infrastructure. Giving Home Assistant root access to your router can be a significant security risk.

## 1. Do I need a dedicated account?
A dedicated account (for example, `homeassistant`) lets you rotate or revoke integration credentials separately from the router administrator account. It does not by itself restrict what Home Assistant can do. The automatic provisioning profile authorizes shell execution through rpcd, which normally runs as root: the provisioned account has root-equivalent command authority and is not a least-privilege or read-only account.

## 2. What type of connection should I use?
- **Ubus (via HTTPS)**: This is the **strongly recommended** connection method. Ubus is the built-in RPC interface for OpenWrt. It is fast, modern, and supports fine-grained access control (ACLs). You should ensure your router's uhttpd server is configured for HTTPS so that credentials and RPC traffic are encrypted between Home Assistant and OpenWrt.
- **LuCI RPC**: Acceptable as a fallback, but relies on the LuCI web interface routing.
- **SSH**: Not recommended for security-conscious setups. While SSH keys are secure, providing shell access to a non-admin service is generally harder to lock down securely compared to Ubus ACLs.
  > **Security Feature**: To prevent Man-in-the-Middle (MITM) attacks, the integration enforces **SSH Host Key Pinning**. Upon the first successful connection, the router's SSH host key is securely stored in Home Assistant's registry. Subsequent connection attempts with modified or mismatched host keys are strictly rejected.

## 3. How do I restrict permissions (ACLs)?

When using the recommended Ubus connection, OpenWrt allows you to define strict **Access Control Lists (ACLs)** using the `rpcd` daemon.
Instead of adding your `homeassistant` user to the `root` group, you should define a custom read/write ACL group that only allows access to the specific Ubus namespaces the integration needs.

### Automated Account Provisioning
The Home Assistant OpenWrt integration includes an **automated provisioning feature**. When you first connect as `root`, the integration will offer to automatically:
1. Create a dedicated `homeassistant` system user.
2. Generate a secure, unique password.
3. Create the necessary ACL files at `/usr/share/rpcd/acl.d/homeassistant.json`.
4. Configure UCI and RPC permissions.
5. Restart the required services (`rpcd` and `uhttpd`) to apply the changes.

This separates integration credentials from your root password, but grants broad permissions for the supported monitoring and control features. Read the [permissions guide](docs/permissions.md) before granting this level of access. HTTPS protects credentials in transit; it does not reduce the authority granted by the ACL.

### Manual Alternative: Creating a Dedicated Ubus User
If you prefer to set up the permissions manually, follow these steps:

1. **Create the user** on your OpenWrt router (you may need to install `shadow-useradd` first, or manually edit `/etc/passwd` and `/etc/shadow`):
   ```bash
   opkg update && opkg install shadow-useradd
   useradd -m -s /bin/false homeassistant
   passwd homeassistant
   ```

2. **Choose the ACL permissions** using the [permissions guide](docs/permissions.md)
   and [example ACL](docs/examples/ubus-acl.json). Method grants, UCI configuration
   grants, and file-path grants are separate checks. The full-feature example
   authorizes `/bin/sh` and is root-equivalent; a narrower profile supports only
   the operations it permits. Preserve unrelated groups when updating an existing
   ACL file.

3. **Assign the ACL to the user** in `/etc/config/rpcd`. The following creates a new login; for an existing login, update its group assignments instead of appending a duplicate:
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
- **Credential separation**: A dedicated account lets you rotate or revoke integration credentials independently. It does not change the identity of commands executed by rpcd: with shell execution allowed, those commands still run with the daemon's privileges, normally root.

## 5. Security & Trust FAQ

### Why is this not an official Home Assistant Core integration?
Maintaining a core integration in the official Home Assistant repository comes with significant trade-offs:
- **High Maintenance Overhead**: Every minor change to the Home Assistant core API requires rapid updates to the integration.
- **Slow Release Cycle**: PR reviews in the official Home Assistant repository are thorough but can take several months. This makes it extremely difficult to roll out quick bug fixes, performance improvements, or support for newer OpenWrt releases.
- **Agility & Marketplace**: Keeping it as a community integration in the Home Assistant Marketplace (formerly HACS) allows the project to remain agile, issue updates immediately when OpenWrt features change, and support older/newer Home Assistant versions without being constrained by core release schedules.

### What security reviews has this integration undergone?
When this integration was submitted to the default HACS repository, it underwent code review by Frenck, a lead developer of the Home Assistant Core team. The security feedback provided during this process (e.g., ensuring secure session handling, avoiding shell execution where possible, and strict credential containment) was fully resolved and verified before the integration was officially accepted into HACS (see [HACS PR #6270](https://github.com/hacs/default/pull/6270#pullrequestreview-4284547826)).

### How is the risk of supply chain attacks mitigated?
- **Pinned Versions**: Using the Marketplace / HACS, you are not forced to auto-update. You can pin the integration to a specific release version and update only after reading the changelog or when you feel comfortable.
- **Minimal Dependencies**: The integration relies heavily on native Python/Home Assistant libraries rather than external third-party packages, reducing the risk of a compromised dependency.
- **Open Source Community**: All PRs, commits, and releases are public, allowing auditability by the community.

### What about the third-party MQTT Presence script?
The MQTT Presence Detection feature integrates with [OpenWRT_HA_Presence](https://github.com/f45tb00t/OpenWRT_HA_Presence).
- **Completely Optional**: MQTT Presence is opt-in. Its deployment needs `file.exec` plus an executable-path grant for `/bin/sh`. Other features, such as backups and command-based monitoring, also execute commands, so disabling MQTT Presence does not remove their command-permission requirements.
- **Vetted Source**: The presence scripts are lightweight and open-source, allowing you to review them before deploying them to your router.
