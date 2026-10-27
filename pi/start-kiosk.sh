#!/usr/bin/env bash
# Started from the login profile on tty1 (no-desktop mode).
# Runs the dashboard in cage, logs everything, retries, and if it keeps failing
# leaves you at a shell with the reason on screen instead of looping silently.
LOG="$HOME/.cache/homelab-kiosk.log"
mkdir -p "$(dirname "$LOG")"

if ! command -v cage >/dev/null; then
  echo "cage is not installed. Re-run ./install-pi.sh" | tee -a "$LOG"
  exit 1
fi

for attempt in 1 2 3; do
  echo "=== $(date '+%F %T') starting kiosk (attempt $attempt)" >> "$LOG"
  cage -s -- /opt/homelab-dashboard/kiosk.sh >> "$LOG" 2>&1
  echo "=== cage exited with code $?" >> "$LOG"
  sleep 3
done

clear
echo "The dashboard failed to start 3 times. Last lines of $LOG:"
echo
tail -n 25 "$LOG"
echo
echo "Full check: /opt/homelab-dashboard/diagnose.sh"
