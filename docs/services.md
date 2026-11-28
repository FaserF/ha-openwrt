# Services

The integration provides several powerful services for advanced control.

## `openwrt.reboot`

Reboots the OpenWrt router.

- **`entry_id`**: (Optional) The config entry ID of the router to reboot.

## `openwrt.manage_service`

Manage system services (init.d) on the router.

- **`service_name`**: The name of the service (e.g., `dnsmasq`).
- **`action`**: One of `start`, `stop`, `restart`, `enable`, `disable`.

## `openwrt.execute_command`

Execute arbitrary shell commands on your router.

- **`command`**: The command to run (e.g., `/etc/init.d/uhttpd restart`).

## `openwrt.uci_get`

Read a value from the UCI configuration. Returns the value as a service response.

- **`config`**, **`section`**, **`option`**: The UCI path components.

## `openwrt.uci_set`

Modify any UCI setting and commit the change immediately.

- **`config`**, **`section`**, **`option`**, **`value`**: The target setting.

## `openwrt.wake_on_lan`

Send a Magic Packet through the router to wake up a device.

- **`mac`**: Target MAC address.
- **`interface`**: (Optional) The interface to use (e.g., `br-lan`).

## `openwrt.create_backup`

Triggers a configuration backup and returns the path to the backup file.

## `openwrt.generate_report`

Generates a Markdown system report (model, firmware, resources, top processes, recent logs) and returns it as a service response.

## `openwrt.get_system_logs`

Returns recent log lines from the router as a service response.

- **`count`**: (Optional) Number of lines to return, 1 to 1000 (default: `50`).
- **`log_type`**: (Optional) `system` (logread, default) or `kernel` (dmesg).
- **`filter`**: (Optional) Only return lines matching this regular expression (case-sensitive), e.g. `hostapd` or a lowercase MAC address. Uses `logread -e` (or `grep` for kernel logs), and `count` applies to the matching lines.

## `openwrt.add_static_lease`

Adds a static DHCP lease, replacing any existing lease for the same MAC address.

- **`mac`**: MAC address of the device.
- **`ip`**: IPv4 address to assign.
- **`name`**: (Optional) Hostname for the lease.

## `openwrt.delete_static_lease`

Deletes the static DHCP lease for a MAC address.

- **`mac`**: MAC address of the device.
