#!/usr/bin/env bash
# Install or update the home lab dashboard straight from GitHub.
#
#   ./install.sh pi     [--demo] [--console]    # on the Raspberry Pi (as your normal user)
#   ./install.sh server [PI_IP]                 # on each Docker server (as your normal user; uses sudo)
#
# Clones the repo into ~/homelab-dashboard-src (or fast-forwards it when it is
# already there), then runs the matching installer from that checkout. Running it
# again is how you update.
#
# The public repo needs no credentials. For a private fork, set REPO_URL to the fork
# and give git access with one of:
#   * REPO_TOKEN=<fine-grained token with read-only "Contents" access>   (simplest)
#   * REPO_URL=git@github.com:you/homelab-dashboard.git                  (SSH key or deploy key)
#   * a git credential helper or `gh auth login` already set up on the machine
#
# Other settings: INSTALL_DIR (default ~/homelab-dashboard-src), REPO_REF (branch or tag, default main).
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/Hannimal/homelab-dashboard.git}"
REPO_REF="${REPO_REF:-main}"
INSTALL_DIR="${INSTALL_DIR:-$HOME/homelab-dashboard-src}"

usage() { sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 1; }
target="${1:-}"
[ -n "$target" ] || usage
shift
case "$target" in pi|server) ;; *) usage ;; esac

if [ "$(id -u)" -eq 0 ]; then
  echo "Run this as your normal user, without sudo; it asks for sudo when needed." >&2; exit 1
fi

if ! command -v git >/dev/null; then
  echo "==> Installing git"
  sudo apt-get update -qq && sudo apt-get install -y -qq git
fi

# A token is passed per command and never written into the clone's remote URL.
git_auth=()
if [ -n "${REPO_TOKEN:-}" ]; then
  basic="$(printf 'x-access-token:%s' "$REPO_TOKEN" | base64 | tr -d '\n')"
  git_auth=(-c "http.extraheader=Authorization: Basic $basic")
fi
git() { command git ${git_auth[@]+"${git_auth[@]}"} "$@"; }

if [ -d "$INSTALL_DIR/.git" ]; then
  echo "==> Updating $INSTALL_DIR ($REPO_REF)"
  git -C "$INSTALL_DIR" fetch --quiet origin "$REPO_REF"
  git -C "$INSTALL_DIR" checkout --quiet "$REPO_REF" 2>/dev/null || git -C "$INSTALL_DIR" checkout --quiet -B "$REPO_REF" FETCH_HEAD
  git -C "$INSTALL_DIR" merge --ff-only --quiet FETCH_HEAD
else
  echo "==> Cloning into $INSTALL_DIR ($REPO_REF)"
  git clone --quiet --branch "$REPO_REF" "$REPO_URL" "$INSTALL_DIR"
fi
echo "   At $(git -C "$INSTALL_DIR" rev-parse --short HEAD): $(git -C "$INSTALL_DIR" log -1 --format=%s)"

case "$target" in
  pi)
    cd "$INSTALL_DIR/pi"
    exec ./install-pi.sh "$@"
    ;;
  server)
    cd "$INSTALL_DIR/servers"
    exec sudo ./install-agent.sh "$@"
    ;;
esac
