#!/bin/sh
set -eu

# This script is invoked by hostapd_cli -a as:
#   presence_event.sh <iface> <event> <mac> [extra...]
# and re-invoked by itself in grace mode as:
#   presence_event.sh presence_grace_<MAC> <MAC>
# The grace-mode marker is a real argv element so pkill -f "presence_grace_<MAC>"
# (used by the AP-STA-CONNECTED / AP-STA-DISCONNECTED branches) can find the timer.

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

# Map MAC -> topic (case-insensitive). Shared by event dispatch and grace mode.
# Sets TOPIC to "" when the MAC is not whitelisted (publish then no-ops).
resolve_topic() {
  TOPIC="$(awk -v m="$MAC" '
    BEGIN { mm=toupper(m) }
    /^[[:space:]]*#/ { next }
    NF>=2 {
      if (toupper($1) == mm) { print $2; exit }
    }
  ' "$DEV_CONF")"

  if [ -z "$TOPIC" ]; then
    # presence_devices.conf has entries but MAC is not listed -> not whitelisted.
    if grep -qE '^[[:space:]]*[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}' "$DEV_CONF" 2>/dev/null; then
      TOPIC=""
      return
    fi
    # No device entries at all -> fallback to auto-topic presence/<safe_mac>.
    TOPIC="presence/$(printf '%s' "$MAC" | tr '[:upper:]' '[:lower:]' | tr ':' '_')"
  fi

  # Enrich TOPIC with prefix
  if [ -n "${TOPIC_PREFIX:-}" ]; then
    TOPIC="${TOPIC_PREFIX%/}/$TOPIC"
  fi
}

publish() {
  state_val="$1"
  # Not whitelisted -> nothing to publish.
  [ -n "$TOPIC" ] || return 0

  if [ "$state_val" = "home" ]; then
    zone_val="${ZONE_ENTITY:-zone.home}"
    payload="{\"in_zones\":[\"${zone_val}\"]}"
    mqtt_state="${ZONE_NAME:-home}"
  else
    payload='{"in_zones":[]}'
    mqtt_state="not_home"
  fi

  log "publish topic='$TOPIC' state='$mqtt_state' payload='$payload'"
  mosquitto_pub \
    -h "$BROKER" -p "$PORT" \
    -u "$USER" -P "$PASS" \
    -i "ap-presence-$HOST_ID-$$" \
    -q "${QOS:-1}" -r \
    --keepalive 30 \
    --will-topic "${TOPIC}/status" --will-payload "unknown" --will-retain \
    -t "${TOPIC}" -m "$mqtt_state" >/dev/null

  mosquitto_pub \
    -h "$BROKER" -p "$PORT" \
    -u "$USER" -P "$PASS" \
    -i "ap-presence-$HOST_ID-$$-attr" \
    -q "${QOS:-1}" -r \
    --keepalive 30 \
    -t "${TOPIC}/attributes" -m "$payload" >/dev/null
}

is_seen_anywhere() {
  # Roaming check across local radios on the same AP. Interface names come from
  # the live hostapd control sockets (one socket per AP interface) so the check
  # never depends on a stale interface snapshot.
  for sock in /var/run/hostapd/*; do
    [ -e "$sock" ] || continue
    iface=$(basename "$sock")
    [ "$iface" = "global" ] && continue
    if iw dev "$iface" station dump 2>/dev/null \
      | awk '/^Station/ {print toupper($2)}' \
      | grep -Fxq "$MAC"; then
      return 0
    fi
  done
  return 1
}

# --- Grace-mode entry -----------------------------------------------------
# Runs as a detached process. After this script exits, the timer is orphaned
# and reaped by procd (PID 1), so it does not linger as a zombie.
case "${1:-}" in
  presence_grace_*)
    MAC="$2"
    resolve_topic
    log "grace timer fired for mac=$MAC"
    sleep "${GRACE_SECONDS:-0}"
    # If the client is visible again (roamed back or reconnected), suppress not_home.
    if is_seen_anywhere; then
      log "mac=$MAC reappeared during grace -> suppress not_home"
      exit 0
    fi
    log "grace expired for mac=$MAC -> publish not_home"
    publish "not_home"
    exit 0
    ;;
esac

# --- hostapd event dispatch ------------------------------------------------
IFACE="${1:-}"
EVENT="${2:-}"
shift 2 || true
REST="$*"

# Extract MAC with colons from the remaining args (hostapd_cli may append key=value tokens)
MAC="$(printf '%s\n' "$REST" | grep -Eoi '([0-9a-f]{2}:){5}[0-9a-f]{2}' | head -n 1 \
  | tr '[:lower:]' '[:upper:]' || true)"

log "iface=$IFACE event=$EVENT rest='$REST' mac='$MAC'"

# Ignore events without a MAC (e.g. EAPOL-4WAY-HS-COMPLETED without args on some builds)
[ -n "$MAC" ] || exit 0

resolve_topic
log "mapped mac=$MAC -> topic='$TOPIC'"

# Not whitelisted -> ignore entirely.
[ -n "$TOPIC" ] || exit 0

case "$EVENT" in
  AP-STA-CONNECTED)
    # Cancel any existing grace timer process for this MAC
    pkill -f "presence_grace_${MAC}" 2>/dev/null || true
    publish "home"
    ;;

  AP-STA-DISCONNECTED)
    # Cancel previous grace timer process for this MAC to restart countdown
    pkill -f "presence_grace_${MAC}" 2>/dev/null || true
    log "start grace timer (${GRACE_SECONDS:-0}s) for mac=$MAC"

    # Start the timer as a detached process whose argv carries the marker, so a
    # later AP-STA-CONNECTED pkill can cancel it. setsid detaches the session so
    # killing the hostapd process group cannot take the timer down with it.
    if command -v setsid >/dev/null 2>&1; then
      setsid /bin/sh "$0" "presence_grace_${MAC}" "$MAC" >/dev/null 2>&1 &
    else
      /bin/sh "$0" "presence_grace_${MAC}" "$MAC" >/dev/null 2>&1 &
    fi
    ;;

  *)
    # Ignore other events
    exit 0
    ;;
esac

exit 0