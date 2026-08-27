#!/bin/sh
set -eu

# This script is invoked by hostapd_cli -a as:
#   presence_event.sh <iface> <event> <mac> [extra...]

CONF="/etc/presence/presence.conf"
MQTT_CONF="/etc/presence/presence_mqtt.conf"
DEV_CONF="/etc/presence/presence_devices.conf"

[ -f "$CONF" ] || exit 0
[ -f "$MQTT_CONF" ] || exit 0
[ -f "$DEV_CONF" ] || exit 0

# shellcheck disable=SC1090
. "$CONF"
# shellcheck disable=SC1090
. "$MQTT_CONF"

LOGTAG="presence_event"
log() {
  [ "${DEBUG:-0}" -eq 1 ] || return 0
  logger -t "$LOGTAG" "$*"
}

HOST_ID="$(cat /proc/sys/kernel/hostname 2>/dev/null || echo openwrt)"
STATE_DIR="/tmp/presence_state"
mkdir -p "$STATE_DIR"

IFACE="${1:-}"
EVENT="${2:-}"
shift 2 || true
REST="$*"

# Extract MAC with colons from the remaining args (hostapd_cli may append key=value tokens)
MAC_RAW="$(printf '%s\n' "$REST" | grep -Eoi '([0-9a-f]{2}:){5}[0-9a-f]{2}' | head -n 1 || true)"
MAC="$(printf '%s' "$MAC_RAW" | tr '[:lower:]' '[:upper:]')"

log "iface=$IFACE event=$EVENT rest='$REST' mac='$MAC'"

# Ignore events without a MAC (e.g. EAPOL-4WAY-HS-COMPLETED without args on some builds)
[ -n "$MAC" ] || exit 0

STATE_FILE="$STATE_DIR/${MAC}.state"

# Map MAC -> topic (case-insensitive)
TOPIC="$(awk -v m="$MAC" '
  BEGIN { mm=toupper(m) }
  /^[[:space:]]*#/ { next }
  NF>=2 {
    if (toupper($1) == mm) { print $2; exit }
  }
' "$DEV_CONF")"

log "mapped mac=$MAC -> topic='$TOPIC'"

# If TOPIC is empty and presence_devices.conf contains non-comment entries -> MAC is not whitelisted, ignore.
# If TOPIC is empty and presence_devices.conf has no device entries -> fallback to auto-topic presence/${SAFE_MAC}.
if [ -z "$TOPIC" ]; then
  if grep -qE '^[[:space:]]*[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}' "$DEV_CONF" 2>/dev/null; then
    log "mac=$MAC not in whitelist -> ignore"
    exit 0
  fi
  SAFE_MAC="$(echo "$MAC" | tr '[:upper:]' '[:lower:]' | tr ':' '_')"
  TOPIC="presence/${SAFE_MAC}"
fi

# Enrich TOPIC with prefix
[ -n "${TOPIC_PREFIX:-}" ] && TOPIC="${TOPIC_PREFIX%/}/$TOPIC"

publish() {
  state_val="$1"
  if [ "$state_val" = "home" ]; then
    zone_val="${ZONE_ENTITY:-zone.home}"
    payload="{\"in_zones\":[\"${zone_val}\"]}"
    mqtt_state="home"
  else
    payload="{\"in_zones\":[]}"
    mqtt_state="not_home"
  fi

  log "publish topic='$TOPIC' state='$mqtt_state' payload='$payload'"
  mosquitto_pub \
    -h "$BROKER" -p "$PORT" \
    -u "$USER" -P "$PASS" \
    -i "ap-presence-$HOST_ID-$IFACE" \
    -q "${QOS:-1}" -r \
    --keepalive 30 \
    --will-topic "${TOPIC}/status" --will-payload "unknown" --will-retain \
    -t "${TOPIC}" -m "$mqtt_state" >/dev/null

  mosquitto_pub \
    -h "$BROKER" -p "$PORT" \
    -u "$USER" -P "$PASS" \
    -i "ap-presence-$HOST_ID-$IFACE-attr" \
    -q "${QOS:-1}" -r \
    --keepalive 30 \
    -t "${TOPIC}/attributes" -m "$payload" >/dev/null
}

is_seen_anywhere() {
  # Roaming check across local radios on the same AP
  for i in ${IFACES:-}; do
    if iw dev "$i" station dump 2>/dev/null \
      | awk '/^Station/ {print toupper($2)}' \
      | grep -Fxq "$MAC"; then
      return 0
    fi
  done
  return 1
}

case "$EVENT" in
  AP-STA-CONNECTED)
    # Cancel grace timer and publish home immediately
    rm -f "$STATE_FILE" 2>/dev/null || true
    publish "home"
    ;;

  AP-STA-DISCONNECTED)
    # Start grace timer; only publish not_home if still absent after GRACE_SECONDS
    date +%s > "$STATE_FILE"
    log "start grace timer (${GRACE_SECONDS:-0}s) for mac=$MAC"

    (
      sleep "${GRACE_SECONDS:-0}"

      if [ -f "$STATE_FILE" ]; then
        # If the client is visible again (roamed back or reconnected), suppress not_home
        if is_seen_anywhere; then
          log "mac=$MAC reappeared during grace -> suppress not_home"
          rm -f "$STATE_FILE" 2>/dev/null || true
          exit 0
        fi

        log "grace expired for mac=$MAC -> publish not_home"
        rm -f "$STATE_FILE" 2>/dev/null || true
        publish "not_home"
      fi
    ) &

    ;;

  *)
    # Ignore other events
    exit 0
    ;;
esac

exit 0
