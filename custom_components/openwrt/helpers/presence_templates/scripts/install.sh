#!/bin/sh
set -eu

has_cmd() {
    command -v "$1" >/dev/null 2>&1
}

# Install dependencies if missing (OpenWrt 25 uses apk, older uses opkg)
if has_cmd hostapd_cli && has_cmd iw && has_cmd mosquitto_pub; then
    echo "OK: dependencies already installed"
else
    if has_cmd apk; then
        apk update
        apk add hostapd-utils mosquitto-client-ssl iw
    else
        opkg update
        opkg install hostapd-utils mosquitto-client-ssl iw
    fi
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

# Run healthcheck before enabling/starting service
if [ -f /etc/presence/healthcheck.sh ]; then
    sh /etc/presence/healthcheck.sh
fi

killall -9 hostapd_cli 2>/dev/null || true
/etc/init.d/presence_hostapd enable
/etc/init.d/presence_hostapd restart

echo "OK: presence_hostapd enabled and restarted"
