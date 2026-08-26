#!/bin/sh
set -eu

# Quick health check for dependencies and hostapd control sockets.

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing: $1"; exit 1; }; }

need hostapd_cli
need iw
need mosquitto_pub

. /etc/presence/presence.conf

echo "Interfaces: ${IFACES:-}"
for i in ${IFACES:-}; do
  printf "%s: " "$i"
  if hostapd_cli -i "$i" ping 2>/dev/null | grep -q PONG; then
    echo "PONG"
  else
    echo "NO-PONG (check ctrl socket / interface name)"
    exit 1
  fi
done

echo "OK: dependencies present and hostapd control reachable"
