#!/usr/bin/env bash
# Push the current code to the Pi or a server and run its installer.
#
#   ./deploy.sh pi     hannimal@10.1.2.2 [--demo] [--console]
#   ./deploy.sh server you@192.168.1.11  [PI_IP]
#
# Replaces the remote folder each time (no nested-folder surprises).
# Your live settings in /opt/homelab-dashboard/config.json are never touched
# unless you have a pi/config.json locally (that file is git-ignored).
set -euo pipefail
cd "$(dirname "$0")"

target="${1:-}"; host="${2:-}"
if [ -z "$target" ] || [ -z "$host" ]; then
  sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 1
fi
shift 2

case "$target" in
  pi)
    echo "==> Copying pi/ to $host:~/homelab-dashboard"
    tar -C pi --exclude='__pycache__' -czf - . | ssh "$host" 'rm -rf ~/homelab-dashboard && mkdir -p ~/homelab-dashboard && tar -C ~/homelab-dashboard -xzf -'
    echo "==> Running the installer"
    ssh -t "$host" "cd ~/homelab-dashboard && ./install-pi.sh $*"
    ;;
  server)
    echo "==> Copying servers/ to $host:~/homelab-agent"
    tar -C servers -czf - . | ssh "$host" 'rm -rf ~/homelab-agent && mkdir -p ~/homelab-agent && tar -C ~/homelab-agent -xzf -'
    echo "==> Running the installer"
    ssh -t "$host" "cd ~/homelab-agent && sudo ./install-agent.sh $*"
    ;;
  *)
    echo "First argument must be 'pi' or 'server'" >&2; exit 1 ;;
esac
