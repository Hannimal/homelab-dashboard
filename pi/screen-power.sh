#!/usr/bin/env bash
# Switches the physical display off and on to follow the dashboard's sleep state.
#
# Started by kiosk.sh, so it runs in the same session as the browser (inside cage, or the
# desktop) and can reach the display. The dashboard page decides WHEN to sleep (the "screen"
# block in config.json); this script only carries that out. It never runs anything taken
# from the network: the methods below are fixed, and config.json only picks one of them.
#
# Every switch is logged to ~/.cache/homelab-screen.log, which says which method worked.
DEST=/opt/homelab-dashboard
PORT="$(python3 -c "import json;print(json.load(open('$DEST/config.json')).get('port',8080))" 2>/dev/null || echo 8080)"
URL="http://127.0.0.1:${PORT}/api/screen"
LOG="$HOME/.cache/homelab-screen.log"
mkdir -p "$(dirname "$LOG")"
log() { printf '%s %s\n' "$(date '+%F %T')" "$*" >> "$LOG"; }

# One watcher at a time, even if the kiosk restarts
exec 9>"${XDG_RUNTIME_DIR:-/tmp}/homelab-screen.lock"
flock -n 9 || exit 0

active=""    # the method that last worked
current=on   # what we last asked the display to do
method=auto

# power on|off METHOD: try one way of switching the display; success means the command accepted it
power() {
  local want="$1" how="$2" out
  case "$how" in
    wlr-randr)   # Wayland compositors that support output management (labwc, wayfire; not every kiosk compositor)
      command -v wlr-randr >/dev/null || return 1
      out="$(wlr-randr 2>/dev/null | awk '/^[^ ]/{print $1; exit}')"
      [ -n "$out" ] && wlr-randr --output "$out" "--$want" >/dev/null 2>&1 ;;
    backlight)   # panels with a backlight device (many DSI screens, some Waveshare models)
      command -v brightnessctl >/dev/null || return 1
      if [ "$want" = off ]; then brightnessctl -q -s set 0 >/dev/null 2>&1; else brightnessctl -q -r >/dev/null 2>&1; fi ;;
    x11)         # X11 sessions
      command -v xset >/dev/null || return 1
      xset dpms force "$want" >/dev/null 2>&1 ;;
    vcgencmd)    # legacy firmware display driver; often accepted but ignored on current KMS systems
      command -v vcgencmd >/dev/null || return 1
      vcgencmd display_power "$([ "$want" = on ] && echo 1 || echo 0)" >/dev/null 2>&1 ;;
    *) return 1 ;;
  esac
}

# apply on|off: use the configured method, or in auto mode the one that worked last, then the others
apply() {
  local want="$1" m
  if [ "$method" != auto ]; then
    if power "$want" "$method"; then active="$method"; log "$want via $method"; return 0; fi
    log "$want via $method FAILED"; return 1
  fi
  if [ -n "$active" ] && power "$want" "$active"; then log "$want via $active"; return 0; fi
  for m in wlr-randr backlight x11 vcgencmd; do
    if power "$want" "$m"; then active="$m"; log "$want via $m"; return 0; fi
  done
  log "$want: none of wlr-randr, backlight, x11, vcgencmd worked (the dashboard still goes black)"
  return 1
}

# Never leave the display off if this watcher stops
trap '[ "$current" = off ] && apply on' EXIT

log "watcher started (method from config: ${method})"
# Stop when the browser (our parent, since kiosk.sh exec's into it) goes away
while kill -0 "$PPID" 2>/dev/null; do
  json="$(curl -fs --max-time 3 "$URL" 2>/dev/null)" || json=""
  want="$(printf '%s' "$json" | sed -n 's/.*"state": *"\([a-z]*\)".*/\1/p')"
  m="$(printf '%s' "$json" | sed -n 's/.*"method": *"\([a-z0-9-]*\)".*/\1/p')"
  method="${m:-auto}"
  [ "$want" = off ] || want=on     # server unreachable or unclear: fail safe to "on"
  if [ "$method" != none ] && [ "$want" != "$current" ]; then
    apply "$want"
    current="$want"
  fi
  sleep 2
done
