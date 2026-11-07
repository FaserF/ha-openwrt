"""Provisioning script template for OpenWrt."""

from __future__ import annotations

PROVISION_SCRIPT_TEMPLATE = """
# Dedicated Home Assistant User Provisioning Script
# This script creates a system user, sets a password, and configures RPC/LuCI ACLs.

USER='{username}'
PASS='{password}'
LOGGER="/usr/bin/logger"
[ -x "$LOGGER" ] || LOGGER="logger"
UCI="/sbin/uci"
[ -x "$UCI" ] || UCI="uci"

$LOGGER -t ha-openwrt "Starting provisioning for $USER"
echo "LOG: TRACE: Start"

# Create a safe section name (alphanumeric only)
SECTION=$(echo "$USER" | tr -cd '[:alnum:]' | tr '[:upper:]' '[:lower:]')
[ -n "$SECTION" ] || SECTION="homeassistant"

# Ensure /bin/ash is in /etc/shells (required for some LuCI setups)
if ! grep -q "^/bin/ash" /etc/shells 2>/dev/null; then
    echo "/bin/ash" >> /etc/shells
fi

if ! id "$USER" >/dev/null 2>&1; then
    if command -v adduser >/dev/null 2>&1; then
        adduser -D -s /bin/ash "$USER" >/dev/null 2>&1
    elif command -v useradd >/dev/null 2>&1; then
        useradd -m -s /bin/ash "$USER" >/dev/null 2>&1
    else
        echo "$USER:x:1001:0:HomeAssistant:/home/$USER:/bin/ash" >> /etc/passwd
        echo "$USER:x:1001:" >> /etc/group
        grep -q "^root:.*$USER" /etc/group || sed -i "s/^root:x:0:/root:x:0:$USER,/" /etc/group
    fi
fi
echo "LOG: TRACE: User verified"

sed -i "s|^$USER:[^:]*:|$USER:x:|" /etc/passwd
[ -f /etc/shadow ] && {{ grep -q "^$USER:" /etc/shadow || echo "$USER:*::0:99999:7:::" >> /etc/shadow; }}

if command -v chpasswd >/dev/null 2>&1; then
    printf "%s:%s\\n" "$USER" "$PASS" | chpasswd >/dev/null 2>&1
else
    (echo "$PASS"; sleep 1; echo "$PASS") | passwd "$USER" >/dev/null 2>&1
fi
echo "LOG: TRACE: Password set"

ACL_FILE="/usr/share/rpcd/acl.d/homeassistant.json"
mkdir -p "$(dirname "$ACL_FILE")"
if [ -f /etc/sysupgrade.conf ] && ! grep -q "$ACL_FILE" /etc/sysupgrade.conf 2>/dev/null; then
    echo "$ACL_FILE" >> /etc/sysupgrade.conf
fi
printf '{{\\n  "homeassistant": {{\\n    "description": "Home Assistant Integration",\\n    "read": {{\\n      "ubus": {{\\n        "system": ["info", "board", "logread", "upgrade"],\\n        "log": ["read"],\\n        "network": ["*"],\\n        "network.*": ["*"],\\n        "iwinfo": ["*"],\\n        "file": ["*"],\\n        "firewall": ["*"],\\n        "rc": ["*"],\\n        "service": ["*"],\\n        "system": ["*"],\\n        "uci": ["*"],\\n        "session": ["*"],\\n        "hostapd.*": ["*"],\\n        "luci": ["*"],\\n        "luci-rpc": ["*"],\\n        "mwan3": ["status"],\\n        "attendedsysupgrade": ["*"]\\n      }},\\n      "uci": ["*"],\\n      "file": {{\\n        "/etc/config/*": ["read", "stat"],\\n        "/etc/passwd": ["read"],\\n        "/etc/group": ["read"],\\n        "/etc/shadow": ["read"],\\n        "/etc/shells": ["read"],\\n        "/usr/bin/iwinfo": ["read", "stat", "exec"],\\n        "/usr/bin/etherwake": ["read", "stat", "exec"],\\n        "/usr/bin/wg": ["read", "stat", "exec"],\\n        "/usr/sbin/openvpn": ["read", "stat", "exec"],\\n        "/usr/bin/id": ["read", "stat", "exec"],\\n        "/bin/sh": ["read", "stat", "exec"],\\n        "/bin/ash": ["read", "stat", "exec"],\\n        "/bin/ls": ["read", "stat", "exec"],\\n        "/sbin/apk": ["read", "stat", "exec"],\\n        "/bin/opkg": ["read", "stat", "exec"],\\n        "/sbin/logread": ["read", "stat"],\\n        "/etc/presence/*": ["read", "stat"],\\n        "/etc/init.d/presence_hostapd": ["read", "stat", "exec"],\\n        "/usr/sbin/batctl": ["read", "stat", "exec"],\\n        "/sys/module/batman_adv": ["read", "stat"],\\n        "/bin/cat": ["read", "stat", "exec"],\\n        "/bin/grep": ["read", "stat", "exec"],\\n        "/usr/bin/awk": ["read", "stat", "exec"],\\n        "/bin/df": ["read", "stat", "exec"],\\n        "/sbin/ip": ["read", "stat", "exec"],\\n        "/usr/sbin/ip": ["read", "stat", "exec"],\\n        "/bin/ubus": ["read", "stat", "exec"],\\n        "/bin/ping": ["read", "stat", "exec"],\\n        "/usr/bin/ping": ["read", "stat", "exec"],\\n        "/usr/bin/uptime": ["read", "stat", "exec"],\\n        "/usr/bin/killall": ["read", "stat", "exec"],\\n        "/bin/chmod": ["read", "stat", "exec"],\\n        "/bin/mkdir": ["read", "stat", "exec"],\\n        "/bin/rm": ["read", "stat", "exec"],\\n        "/proc/stat": ["read"],\\n        "/proc/meminfo": ["read"],\\n        "/proc/net/arp": ["read"],\\n        "/proc/*/net/arp": ["read"],\\n        "/proc/net/dev": ["read"],\\n        "/etc/init.d/snort": ["read", "stat", "exec"],\\n        "/etc/init.d/banip": ["read", "stat", "exec"],\\n        "/proc/sys/net/netfilter/nf_conntrack_count": ["read"],\\n        "/proc/sys/net/netfilter/nf_conntrack_max": ["read"],\\n        "/usr/bin/tail": ["read", "stat", "exec"],\\n        "/usr/bin/wc": ["read", "stat", "exec"],\\n        "/tmp/dhcp.leases": ["read"],\\n        "/tmp/*": ["read", "stat"],\\n        "/sys/class/thermal/*": ["read"]\\n      }}\\n    }},\\n    "write": {{\\n      "ubus": {{\\n        "system": ["reboot", "upgrade"],\\n        "network.interface": ["up", "down", "reconnect"],\\n        "network": ["*"],\\n        "firewall": ["*"],\\n        "rc": ["*"],\\n        "service": ["*"],\\n        "uci": ["*"],\\n        "file": ["exec"],\\n        "hostapd.*": ["*"]\\n      }},\\n      "uci": ["*"],\\n      "file": {{\\n        "/bin/sh": ["exec"],\\n        "/bin/ash": ["exec"],\\n        "/usr/bin/id": ["exec"],\\n        "/sbin/apk": ["exec"],\\n        "/bin/opkg": ["exec"],\\n        "/tmp/*": ["read", "stat", "write"],\\n        "/etc/presence/*": ["read", "stat", "write"],\\n        "/etc/init.d/presence_hostapd": ["read", "stat", "write", "exec"]\\n      }}\\n    }}\\n  }}\\n}}' > "$ACL_FILE"
chmod 644 "$ACL_FILE"
echo "LOG: TRACE: ACL created"

chmod 644 "$ACL_FILE"

# Thorough cleanup of existing RPC/LuCI sections for this user
$LOGGER -t ha-openwrt "Cleaning up existing UCI RPC/LuCI sections for $USER"
for s in $($UCI show rpcd 2>/dev/null | grep "=login" | cut -d. -f2 | cut -d= -f1); do
    [ "$($UCI get rpcd.$s.username 2>/dev/null)" = "$USER" ] && $UCI delete rpcd.$s
done
for s in $($UCI show luci 2>/dev/null | grep "=user" | cut -d. -f2 | cut -d= -f1); do
    [ "$($UCI get luci.$s.username 2>/dev/null)" = "$USER" ] && $UCI delete luci.$s
done

$UCI set luci."$SECTION"=user
$UCI set luci."$SECTION".username="$USER"
$UCI set luci."$SECTION".password='*'
$UCI add_list luci."$SECTION".write="homeassistant"
$UCI add_list luci."$SECTION".read="homeassistant"
$UCI set rpcd."$SECTION"=login
$UCI set rpcd."$SECTION".username="$USER"
$UCI set rpcd."$SECTION".password="\\$p\\$$USER"
$UCI add_list rpcd."$SECTION".read="homeassistant"
$UCI add_list rpcd."$SECTION".write="homeassistant"
$UCI commit luci && $UCI commit rpcd

echo "LOG: Provisioning SUCCESS"

(
    sleep 2
    /etc/init.d/rpcd restart
    sleep 1
    /etc/init.d/uhttpd restart
) >/dev/null 2>&1 &
"""
