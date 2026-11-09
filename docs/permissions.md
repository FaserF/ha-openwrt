# ubus permissions

Use this guide to give an existing rpcd login the permissions needed by the
OpenWrt integration over HTTP/HTTPS, including client discovery. Download the
[example ACL](examples/ubus-acl.json), review its authority below, then install it
and assign its `homeassistant` group to your login. You need administrative access
to the router, a working ubus HTTP endpoint, and rpcd's file plugin for file calls.
For general setup, see [Configuration](configuration.md).

**The example is a broad, full-feature profile with root-equivalent shell access
on stock OpenWrt. It is not a read-only or least-privilege profile.** Optional
features and commands are included; client tracking alone does not require all
of this authority. The native discovery subset below explains what can work
without shell access and where that approach has limits.

## Install and assign the example

1. Back up `/etc/config/rpcd` and any ACL file you will edit. Save the downloaded
   JSON as `/usr/share/rpcd/acl.d/homeassistant.json`. If `homeassistant` already
   exists in an ACL file, replace that group's definition there instead of
   creating another copy; preserve unrelated groups. Definitions with the same
   name across files accumulate grants.
2. Edit the **existing** `config login` section for the username used by Home
   Assistant in `/etc/config/rpcd`. Keep its username, password setting and other
   group assignments. Ensure these entries exist once in that section:

   ```text
   list read 'homeassistant'
   list write 'homeassistant'
   ```

   These lines belong inside that login section; do not append a second
   `config login` for the same username. The JSON's top-level key is the group
   name, independent of the filename. Avoid wildcard group assignments such as
   `'*'`; review other assigned groups because their permissions also apply.
3. Restart rpcd to reload the ACL:

   ```sh
   /etc/init.d/rpcd restart
   ```

   This interrupts active RPC sessions, including other consumers such as LuCI;
   they must reconnect. An ACL-only change normally needs no uhttpd restart.
4. In Home Assistant, reload the OpenWrt integration to reconnect and refresh
   cached permission and package/capability probes. Check both individual client
   devices and their updates, not just Wi-Fi client totals.
5. To preserve the custom ACL during a normal configuration-preserving firmware
   upgrade, add its actual installed path to `/etc/sysupgrade.conf` once:

   ```text
   /usr/share/rpcd/acl.d/homeassistant.json
   ```

   Recheck the file and login after upgrades. Upgrades that discard configuration
   will not preserve this setup. Re-running user provisioning can replace the
   generated ACL, so review it again afterward.

## Two authorization layers

HTTP ubus first checks the object and method. UCI and file handlers then check
the specific resource. Both must be granted:

| Operation | ubus method grant | Resource grant |
| --- | --- | --- |
| Read UCI | `read.ubus.uci: ["get"]` | Config name in `read.uci`, such as `dhcp` |
| Change UCI | Applicable method in `write.ubus.uci` | Config name in `write.uci` |
| Read file | `read.ubus.file: ["read"]` | Path with `read` in a `file` map |
| Inspect metadata | `read.ubus.file: ["stat"]` | Path with **`list`**, not `stat`, in a `file` map |
| List directory | `read.ubus.file: ["list"]` | Directory with `list` in a `file` map |
| Write file | `write.ubus.file: ["write"]` | Path with `write` in a `file` map |
| Execute shell | `write.ubus.file: ["exec"]` | `/bin/sh` with `exec` in a `file` map |

For example, naming `dhcp` in `read.uci` does not authorize `uci.get` by itself.
The login's `read` and `write` lists select ACL blocks; a write group also loads
its read block. These labels are not an operating-system sandbox: a literal
`exec` grant in a loaded read block still permits execution.

## Native client discovery without shell execution

These read operations cover native discovery sources, rather than a second
complete ACL profile:

| Source | Methods and resources |
| --- | --- |
| Static leases and dnsmasq leasefile location | `uci.get`; `read.uci` includes `dhcp` |
| Wireless configuration and interfaces | `network.wireless.status`, `uci.get` with `wireless` read access |
| Wireless stations and AP details | `iwinfo.devices/info/assoclist`; `hostapd.*.get_clients/get_status` |
| odhcpd leases | `dhcp.ipv4leases/ipv6leases`, when available |
| dnsmasq leases | `file.read` and read permission on the configured leasefile; default `/tmp/dhcp.leases` |
| Neighbors, local addresses and bridge topology | `network.device.status/fdb`, `network.interface.dump` |
| IPv4 ARP fallback | `file.read` with both `/proc/net/arp` and `/proc/[0-9]*/net/arp` read grants |

Connection, identity and permission checks also use `system.info/board` and
`session.access/list`. Other package probes can need additional UCI or file
grants; use the inventory below when inspecting a denied call.

Without command execution, the `ip neigh show` fallback is unavailable, so wired
and IPv6 discovery can be less complete when native ubus data is insufficient.
Package detection, command-based monitoring, controls and provisioning can also
be unavailable. Denying execution may produce expected fallback failures.

**This subset does not guarantee a successful collector on every router.** An
authorization error from an optional query, including wireless station or bridge
queries, can propagate and abort client collection. The aggregator then keeps the
previous client list, initially empty, even while separate Wi-Fi totals update.
Missing methods and permission denials are different conditions; granting a
method neither installs its package nor makes an unsupported method exist.

## Full example: feature and resource inventory

The JSON is the exact grant list. It includes native calls, capability probes,
fallbacks and optional controls:

| Feature | Read methods | Control methods |
| --- | --- | --- |
| Identity and resources | `system.info/board` | `system.reboot` |
| Interfaces and topology | `network.device.status/fdb`, `network.interface.dump`, `lldp.show` | `network.interface.up/down` |
| Wi-Fi and clients | `network.wireless.status`, `iwinfo.devices/info/assoclist`, `hostapd.*.get_clients/get_status/wps_status` | `network.wireless.notify`, `hostapd.*.del_client/wps_start/wps_cancel/wps_push` |
| DHCP | `dhcp.ipv4leases/ipv6leases` | Static lease controls use shell commands |
| Configuration | `uci.get` | `uci.add/set/commit/revert` |
| Sessions and permission checks | `session.access/list` | — |
| Services | `rc.list`, `service.list` | `rc.init`, `service.reloading` |
| Optional monitoring | `mwan3.status`, `upnp.get_mappings`, `adblock.status`, `nlbwmon.get_data`, `luci.getMountPoints` | Package-specific shell commands |
| Files, probes and command fallbacks | `file.read/list/stat` | `file.write/exec`, `sys.exec` |

`session.login` uses OpenWrt's default unauthenticated ACL. JSON-RPC `list`, used
to enumerate ubus objects, is distinct from `session.list`. The integration does not need `session.destroy` for its inspected session
handling.

Native UCI reads cover `wireless`, `network`, `dhcp`, `system`, `firewall`, `sqm`,
`mwan3`, `openvpn`, `attendedsysupgrade` and `tailscale`. Native writes cover
`wireless`, `firewall`, `sqm` and `system`. Keep the `system` write grant for LED
capability detection: the current session ACL parser uses it to enable LED
controls, so authorizing brightness file writes alone is insufficient.

File grants cover identity files, `/proc/stat`, ARP and conntrack counters, DHCP
leases, adblock runtime data, reboot markers, temperature sensors and generated
`/tmp/backup-ha-*.tar.gz` backups. Metadata grants cover package probes and
`/etc/config`. Write paths cover LED brightness and `/tmp/ha_provision.sh`.
Direct execution grants cover `lsusb`, `id`, `batctl`, `nlbw`, `wc`, `tail` and
init scripts; most other commands run through `/bin/sh -c`.

### Shell authority and custom paths

A bare `/bin/sh` execution grant permits arbitrary arguments. On stock OpenWrt,
commands run as root; file and UCI allowlists do not constrain what the shell can
read or change. Generic `openwrt.uci_get`/`openwrt.uci_set` services and static
lease controls use this shell path. Removing a config from `write.uci` therefore
does not prevent shell-based changes to it. See the [OpenWrt shell-execution
advisory](https://github.com/openwrt/luci/security/advisories/GHSA-vj96-f37g-37f6).

rpcd checks both the requested file path and its resolved target for reads,
metadata and writes. The example includes process-directory ARP targets,
`/var/run` alternatives under `/tmp/run`, physical sysfs temperature/LED targets
and `/bin/busybox` metadata access for applet symlinks. Execution checks the named
executable, without resolving its symlinks; permitting a downstream binary does
not authorize the integration's `/bin/sh` wrapper.

Adjust the file maps for custom dnsmasq leasefiles, arbitrary download paths,
nonstandard package locations or different symlink targets. Authorize both the
requested and resolved paths with the matching permission. The supplied sysfs
wildcards cover common layouts but grant access to matching paths beyond a
single sensor; narrow them to your hardware if needed. This example cannot
guarantee coverage of every router layout.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Login succeeds, calls return permission denied | Confirm the username's existing login selects the JSON group, then check both authorization gates. Local root `ubus call` success does not prove HTTP-session authorization. |
| Wi-Fi totals appear but no individual clients | Enable integration debug logging and inspect the failing collector call. Check `uci.get` plus `dhcp` read access, wireless methods and leasefile access; optional method denials can abort the collector. |
| File read/stat denied despite a matching path | Check the resolved target too. For `file.stat`, grant file-scope `list`; do not substitute `stat`. |
| Commands fail although a binary is allowed | Most calls execute `/bin/sh -c`; review the shell authority before granting it. |
| LEDs missing or controls unavailable | Check `system` UCI read/write capability grants, shell-based LED discovery and both brightness write paths. |
| Package or object not found | Check package installation and router support; ACL grants cannot add methods. |
| Changes appear ineffective | Restart rpcd, then reload the integration to refresh sessions and cached probes. Check for duplicate group definitions or broader groups on the login. |
| Permissions disappear after upgrade/provisioning | Check `/etc/sysupgrade.conf` and whether provisioning replaced the ACL. |

## Sources and verification scope

This guide and example were checked against integration commit
[`1f093ade4f5522bd52acf4814236a588319e94bc`](https://github.com/FaserF/ha-openwrt/tree/1f093ade4f5522bd52acf4814236a588319e94bc),
including [ubus calls and probes](https://github.com/FaserF/ha-openwrt/tree/1f093ade4f5522bd52acf4814236a588319e94bc/custom_components/openwrt/api/ubus)
and [shared command paths](https://github.com/FaserF/ha-openwrt/tree/1f093ade4f5522bd52acf4814236a588319e94bc/custom_components/openwrt/api/client).
Authorization details come from OpenWrt's [HTTP ubus handler](https://github.com/openwrt/uhttpd/blob/373145f72c884c36a2b16f7f47e74ffae06bd754/ubus.c),
[rpcd UCI handler](https://github.com/openwrt/rpcd/blob/d99f703e4035d03fbcfbada3cb697db346c2b984/uci.c),
[rpcd file handler](https://github.com/openwrt/rpcd/blob/d99f703e4035d03fbcfbada3cb697db346c2b984/file.c)
and [rpcd session ACL loading](https://github.com/openwrt/rpcd/blob/d99f703e4035d03fbcfbada3cb697db346c2b984/session.c).
These are source checks, not a successful router authorization test. Installed
rpcd revisions and custom layouts may differ. This guide covers the integration's
ubus backend, not LuCI RPC or SSH authentication.
