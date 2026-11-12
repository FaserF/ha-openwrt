# Troubleshooting & FAQ

## "Access Denied" or Permission Errors

OpenWrt's `rpcd` has very strict ACLs. Even `root` is sometimes restricted via the Ubus API.

- **Solution 1**: Switch the connection method to **LuCI RPC** in the integration settings. It uses the same session as the web UI and often has more permissive defaults.
- **Solution 2**: Check the [permissions guide](permissions.md): the login must reference its ACL group, and method grants alone do not authorize file paths or UCI configurations.
- **Solution 3**: If you recently upgraded OpenWrt, your ACL files might have been lost. See the entry below.

## Router visible, but no client devices

This affects installations where only the router, radios and SSIDs appear. A
nonzero Wi-Fi client count does not prove that individual client discovery is
working: interface totals and the client list are fetched separately.

1. In the integration options, check **Enable Device Tracking** in **Packages
   Summary**. Review the tracked-device selection, **Include Wired Devices in
   Tracking**, and **Ignore devices with randomized MAC addresses**. MQTT presence
   intentionally replaces the native trackers when enabled.
2. Check [both layers of ubus authorization](permissions.md#two-authorization-layers).
   Allow `uci.get` as a ubus method as well as the `dhcp`, `wireless` and `network`
   UCI configurations. For `file.read`, authorize the actual lease file and both
   `/proc/net/arp` and its `/proc/[0-9]*/net/arp` target. Additional wireless and
   bridge methods are listed in the guide; an ACL does not add methods missing
   from the router's installed packages.
3. If settings and permissions look correct, enable debug logging from the
   integration's menu, capture one polling cycle, then disable debug logging.
   Look for `Fetch of connected_devices failed` or an access-denied error.
   Redact credentials and RPC session tokens before sharing logs. A denied
   optional source can abort client collection; the overall coordinator can
   still report a successful update while retaining an empty client list.
4. Apply the relevant grants using the [manual ACL instructions](permissions.md),
   then reload the integration to refresh its session and cached permission
   information. Expect individual client devices to appear once discovery
   succeeds and the configured filters allow them.

The [example ACL](examples/ubus-acl.json) also covers optional controls and shell
commands. Read its authority warning before installing the full profile; native
Wi-Fi discovery does not by itself require unrestricted shell execution.

## Why did my sensors stop working after an OpenWrt upgrade?

If you recently upgraded OpenWrt and noticed that system sensors like CPU, memory, load, or temperature are suddenly showing `0`, `unknown`, or `unavailable`, it is almost certainly because your **RPC ACL rules were reset** during the upgrade.

By default, OpenWrt does not preserve the custom ACL file `/usr/share/rpcd/acl.d/homeassistant.json` during a sysupgrade unless you have manually added it to `/etc/sysupgrade.conf`.

**How to fix this:**

1. **Check Repairs**: Look at **Settings > System > Repairs** in Home Assistant. The integration automatically detects missing permissions and offers a **"Refresh Permissions"** fix. Follow the steps and provide your router's root credentials to redeploy the ACLs.
2. **Options Flow**: Alternatively, go to the integration in Home Assistant, click **Configure**, and check the **"Re-deploy Home Assistant User & ACLs"** option. This will recreate the user and ACL file on your router.
3. **Manual Check**: If you set up ACLs manually, ensure the file `/usr/share/rpcd/acl.d/homeassistant.json` still exists on your router and contains the correct permissions.
4. **Persistent Fix**: To prevent this from happening in the next upgrade, the ACL path should be added to your sysupgrade configuration. The integration attempts to do this automatically during user deployment, but you can also do it manually:
    ```bash
    echo "/usr/share/rpcd/acl.d/homeassistant.json" >> /etc/sysupgrade.conf
    ```

## Sensors show "Unavailable"

- Check **Settings > System > Repairs** in Home Assistant. The integration will create a repair issue if there's a specific API or package error.
- Ensure the required packages (like `iwinfo` or `uhttpd-mod-ubus`) are installed.

## How do I use the "Internet Access" switches?

The "Internet Access" switches in Home Assistant act as a toggle for the firewall. When turned off, the router creates a temporary firewall rule to drop all traffic from that specific device's MAC address. This provides simple, reliable parental control without complex VLANs.

## Why are some Signal sensors hidden?

The integration automatically hides "STA-style" sensors (Signal, Quality, Bitrate) if the WiFi interface is in **Master/AP mode**. These values only make sense when the router itself is acting as a client (Station) or Mesh node.

## Does this work with devices behind other Access Points (e.g. Unifi, TP-Link)?

**Yes, absolutely!** A common setup is an OpenWrt router (acting as the main gateway) connected to separate Access Points via LAN cable.

Even if the WiFi on your OpenWrt router is disabled, the integration can still track mobile devices (phones, tablets) connected to those external APs. It does this by monitoring two low-level system sources:

- **ARP/Neighbor Table (`ip neigh`)**: Every device that communicates with the internet or the router itself must appear in the ARP table. The integration identifies these devices and marks them as "Home" as long as they are in an active state (`REACHABLE` or `STALE`).
- **Bridge Forwarding Database (FDB)**: The router's internal bridge/switch learns which MAC address is active on which physical LAN port. This allows the integration to "see" devices even if they haven't sent a packet to the router's IP recently.

From OpenWrt's perspective, these devices will appear as **"wired"** clients, but they will be correctly tracked as individual `device_tracker` entities in Home Assistant.

## Why does it take 10 minutes for my device to show as "Away"?

If your devices are connected via a separate Access Point or Switch, the OpenWrt router doesn't "see" them disconnect. It has to wait for the internal system tables to age out:

1. **Bridge FDB aging**: Usually takes 5 minutes (300s).
2. **ARP aging**: Usually takes another 1-2 minutes to transition to `STALE` and then `FAILED`.
3. **HA Consider Home**: Home Assistant adds another 3 minutes (default) of grace period to prevent flickering.

**How to speed this up:**

In the integration **Options**, you can enable higher precision for presence detection:

- **Trust ARP 'STALE' state**: If disabled, devices that the router hasn't verified recently will be marked as "Away" immediately. This can save several minutes but might cause "flickering" if a device is idle.
- **Trust Bridge FDB**: If disabled, the integration will only rely on active ARP communication. This is the most responsive method but might miss devices that are only talking to local devices.
- **Consider Home**: Lower this value in the options flow (e.g. to 60 seconds).

## Required MQTT Permissions

To set up and use the MQTT presence detection feature, the OpenWrt user configured in Home Assistant requires the following permissions:

* **RPC Object `file` / Method `exec`**: Required to deploy the shell scripts, create directories, and manage services.
* **RPC Object `hostapd.*` / Method `get_clients`**: Required by the script on the router to poll connected clients.

The provisioned `homeassistant` account includes broad command permissions. A custom account needs both the `file.exec` method and an executable-path grant for `/bin/sh`; see the [permissions guide](permissions.md). This permits root-equivalent shell execution on stock OpenWrt, including commands used to deploy presence scripts.
