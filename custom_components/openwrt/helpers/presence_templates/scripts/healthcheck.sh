#!/bin/sh
set -eu

# Quick health check for dependencies and hostapd control sockets.

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing: $1"; exit 1; }; }

need hostapd_cli
need iw
need mosquitto_pub

# Interface names come from the live hostapd control sockets (one socket per AP
# interface), never from a snapshot. The check passes when at least one socket
# responds to a ping; there is no point failing deployment for an interface that
# is simply not present on this build.
active=0
for sock in /var/run/hostapd/*; do
  [ -e "$sock" ] || continue
  iface=$(basename "$sock")
  [ "$iface" = "global" ] && continue
  printf "%s: " "$iface"
  if hostapd_cli -i "$iface" ping 2>/dev/null | grep -q PONG; then
    echo "PONG"
    active=1
  else
    echo "NO-PONG (stale socket or hostapd not ready)"
  fi
done

if [ "$active" -ne 1 ]; then
  echo "FAIL: no hostapd control socket responds to ping in /var/run/hostapd/"
  exit 1
fi

echo "OK: dependencies present and hostapd control reachable"
