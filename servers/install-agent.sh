#!/usr/bin/env bash
# Installs the Glances agent on a Docker host.
# Usage: sudo ./install-agent.sh [PI_IP]
#   PI_IP (optional) - if given and ufw is active, port 61208 is opened only to the Pi.
set -euo pipefail

DEST=/opt/homelab-agent
PI_IP="${1:-}"
cd "$(dirname "$0")"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run with sudo: sudo ./install-agent.sh [PI_IP]" >&2; exit 1
fi
if ! command -v docker >/dev/null; then
  echo "Docker is not installed on this host." >&2; exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "The Docker Compose plugin is missing (docker compose)." >&2; exit 1
fi

mkdir -p "$DEST"
cp docker-compose.yml glances.conf "$DEST/"
TZ_NAME="$(cat /etc/timezone 2>/dev/null || timedatectl show -p Timezone --value 2>/dev/null || readlink /etc/localtime 2>/dev/null | sed 's|.*/zoneinfo/||' || true)"
[ -n "$TZ_NAME" ] || TZ_NAME=Etc/UTC
echo "TZ=$TZ_NAME" > "$DEST/.env"

cd "$DEST"
docker compose pull
docker compose up -d

if [ -n "$PI_IP" ] && command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow from "$PI_IP" to any port 61208 proto tcp comment 'homelab dashboard'
  echo "Firewall: port 61208 opened for $PI_IP only."
else
  echo "WARNING: no firewall rule was added. The Glances API (port 61208, no password)" >&2
  echo "         is reachable by everything on this network. Re-run with the Pi's IP while" >&2
  echo "         ufw is active, or restrict the port yourself." >&2
fi

echo -n "Waiting for the API"
for _ in $(seq 1 30); do
  if curl -fs http://127.0.0.1:61208/api/4/status >/dev/null 2>&1; then
    echo " - ready."
    echo "Agent running on $(hostname): http://$(hostname -I | awk '{print $1}'):61208/api/4/cpu"
    if curl -fs http://127.0.0.1:61208/api/4/sensors | grep -q '"temperature_core"'; then
      echo "CPU temperature: available."
    else
      echo "Note: this machine exposes no temperature sensor to Glances (common in VMs and LXC)." >&2
      echo "      The dashboard works without it and simply hides the temperature row." >&2
      echo "      Check on the host: ls /sys/class/hwmon /sys/class/thermal" >&2
    fi
    exit 0
  fi
  echo -n "."; sleep 2
done
echo
echo "The agent did not answer yet. Check: docker logs glances-agent" >&2
exit 1
