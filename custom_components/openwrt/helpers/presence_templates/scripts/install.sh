#!/bin/sh
set -eu

# Install dependencies (OpenWrt 25 uses apk, older uses opkg)
if command -v apk >/dev/null 2>&1; then
    apk update
    apk add hostapd-utils mosquitto-client-ssl iw
else
    opkg update
    opkg install hostapd-utils mosquitto-client-ssl iw
fi

# Config / helper scripts
[ -f /etc/presence/presence_mqtt.conf ] && chmod 600 /etc/presence/presence_mqtt.conf || true
[ -f /etc/presence/presence_devices.conf ] && chmod 600 /etc/presence/presence_devices.conf || true
[ -f /etc/presence/presence.conf ] && chmod 600 /etc/presence/presence.conf || true

[ -f /etc/presence/presence_event.sh ] && chmod 700 /etc/presence/presence_event.sh || true

# Init script
if [ -f /etc/init.d/presence_hostapd ]; then
    sed -i 's/\r$//' /etc/init.d/presence_hostapd 2>/dev/null || true
    chmod 755 /etc/init.d/presence_hostapd
else
    echo "Error: /etc/init.d/presence_hostapd missing"
    exit 1
fi

/etc/init.d/presence_hostapd enable
/etc/init.d/presence_hostapd restart

echo "OK: presence_hostapd enabled and restarted"
