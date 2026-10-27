#!/usr/bin/env bash
# Opens the dashboard full screen. Started automatically at desktop login.
PORT="$(python3 -c 'import json;print(json.load(open("/opt/homelab-dashboard/config.json")).get("port",8080))' 2>/dev/null || echo 8080)"
URL="http://127.0.0.1:${PORT}/"

# Wait for the dashboard service (up to 60 s) so Chromium doesn't show an error page
for _ in $(seq 1 60); do
  curl -fs "${URL}healthz" >/dev/null 2>&1 && break
  sleep 1
done

BROWSER="$(command -v chromium || command -v chromium-browser)"
if [ -z "$BROWSER" ]; then
  echo "Chromium not found" >&2; exit 1
fi

# Dedicated Chromium profile for the kiosk. Clear stale locks first: if Chromium
# was killed (power cut) or the hostname changed, the old lock blocks startup.
PROFILE_DIR="$HOME/.cache/homelab-kiosk-chromium"
mkdir -p "$PROFILE_DIR"
rm -f "$PROFILE_DIR"/SingletonLock "$PROFILE_DIR"/SingletonCookie "$PROFILE_DIR"/SingletonSocket

EXTRA=(--user-data-dir="$PROFILE_DIR")
# Running under cage (no desktop): talk Wayland directly
[ -n "${WAYLAND_DISPLAY:-}" ] && EXTRA+=(--ozone-platform=wayland)

exec "$BROWSER" "${EXTRA[@]}" \
  --kiosk "$URL" \
  --incognito \
  --noerrdialogs \
  --disable-infobars \
  --no-first-run \
  --disable-features=Translate,TranslateUI \
  --disable-pinch \
  --overscroll-history-navigation=0 \
  --check-for-update-interval=31536000 \
  --password-store=basic
