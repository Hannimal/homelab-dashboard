#!/usr/bin/env bash
# Installs the home lab dashboard on a Raspberry Pi.
# Works on Raspberry Pi OS Lite (recommended, no desktop) and on the desktop edition.
# Run as your normal desktop user (NOT with sudo); it asks for sudo when needed.
#
#   ./install-pi.sh           # install / update
#   ./install-pi.sh --demo    # install, but show fake data (to test the screen)
#   ./install-pi.sh --console # use the no-desktop kiosk even if a desktop is installed
#   ./install-pi.sh --wifi    # let the screen's Wi-Fi settings change networks (needs NetworkManager)
#   ./install-pi.sh --power   # let the screen's power menu reboot, shut down and restart (PIN-protected)
set -euo pipefail

DEST=/opt/homelab-dashboard
SRC="$(cd "$(dirname "$0")" && pwd)"
DEMO_FLAG=""
FORCE_CONSOLE=""
WIFI=""
POWER=""
for arg in "$@"; do
  case "$arg" in
    --demo) DEMO_FLAG=" --demo" ;;
    --console) FORCE_CONSOLE=1 ;;   # no-desktop kiosk even if a desktop is installed
    --wifi) WIFI=1 ;;
    --power) POWER=1 ;;
    *) echo "Unknown option: $arg (use --demo, --console, --wifi and/or --power)" >&2; exit 1 ;;
  esac
done

if [ "$(id -u)" -eq 0 ]; then
  echo "Run this as your normal desktop user, without sudo." >&2; exit 1
fi
if [ -n "$WIFI" ] && ! command -v nmcli >/dev/null; then
  echo "--wifi needs NetworkManager (nmcli), which this system does not have." >&2
  echo "Raspberry Pi OS Bookworm and newer include it." >&2; exit 1
fi

echo "==> Installing packages"
sudo apt-get update -qq
BROWSER_PKG=chromium
apt-cache show chromium >/dev/null 2>&1 || BROWSER_PKG=chromium-browser
# No desktop installed (Raspberry Pi OS Lite)? Use cage, a tiny kiosk-only Wayland compositor.
HEADLESS=""
if [ -n "$FORCE_CONSOLE" ] || { ! command -v labwc >/dev/null && ! command -v wayfire >/dev/null && ! command -v lxsession >/dev/null; }; then
  HEADLESS=1
  echo "   No desktop found - installing the minimal kiosk mode (cage + Chromium)"
fi
sudo apt-get install -y -qq --no-install-recommends python3 curl "$BROWSER_PKG" ${HEADLESS:+cage}
sudo apt-get install -y -qq brightnessctl wlr-randr 2>/dev/null || echo "   (brightnessctl / wlr-randr not available; screen sleep will only black out the page)"
sudo apt-get install -y -qq iw 2>/dev/null || echo "   (iw not available; the info panel will show less Wi-Fi detail)"
sudo apt-get install -y -qq fonts-inter 2>/dev/null || echo "   (fonts-inter not available; using the system font)"

echo "==> Copying files to $DEST"
sudo mkdir -p "$DEST/web"
sudo cp "$SRC/server.py" "$SRC/kiosk.sh" "$SRC/start-kiosk.sh" "$SRC/screen-power.sh" "$SRC/diagnose.sh" "$DEST/"
sudo cp "$SRC"/web/* "$DEST/web/"
sudo chmod +x "$DEST/server.py" "$DEST/kiosk.sh" "$DEST/start-kiosk.sh" "$DEST/screen-power.sh" "$DEST/diagnose.sh"
if [ -f "$SRC/config.json" ]; then
  sudo cp "$SRC/config.json" "$DEST/config.json"
  echo "   Using your config.json"
elif [ ! -f "$DEST/config.json" ]; then
  sudo cp "$SRC/config.example.json" "$DEST/config.json"
  echo "   No config.json found - copied the example. Edit $DEST/config.json with your server IPs."
fi
python3 -c "import json;json.load(open('$DEST/config.json'))" || { echo "config.json is not valid JSON" >&2; exit 1; }

RUN_AS="DynamicUser=yes"
# On-screen controls (Wi-Fi changes, power menu) need the service to be allowed to do one specific thing
# through polkit. A DynamicUser has no stable name to write a rule for, so the service then gets a fixed,
# login-less account. Once a rule from an earlier run exists the account is kept, so updating without
# the flags does not quietly switch a feature off.
EXISTING_RULES="$(ls /etc/polkit-1/rules.d/50-homelab-dashboard-*.rules 2>/dev/null || true)"
if [ -n "$WIFI" ] || [ -n "$POWER" ] || [ -n "$EXISTING_RULES" ]; then
  echo "==> Setting up on-screen controls"
  id -u homelab-dashboard >/dev/null 2>&1 || sudo useradd --system --no-create-home --shell /usr/sbin/nologin homelab-dashboard
  sudo gpasswd -d homelab-dashboard netdev >/dev/null 2>&1 || true   # earlier versions added it; polkit ignores it for services
  sudo mkdir -p /etc/polkit-1/rules.d
  RUN_AS="User=homelab-dashboard"
  if [ -n "$WIFI" ]; then
    # NetworkManager asks polkit before anyone may scan or join a network (membership in netdev is
    # not enough: polkit answers "auth required" to a service and NetworkManager says "Not authorized").
    sudo tee /etc/polkit-1/rules.d/50-homelab-dashboard-wifi.rules >/dev/null << 'RULES_EOF'
// Installed by homelab-dashboard (install.sh pi --wifi).
// Lets the dashboard's service user scan for and join Wi-Fi networks, and nothing else.
polkit.addRule(function(action, subject) {
  var allowed = [
    "org.freedesktop.NetworkManager.network-control",
    "org.freedesktop.NetworkManager.wifi.scan",
    "org.freedesktop.NetworkManager.settings.modify.system",
    "org.freedesktop.NetworkManager.settings.modify.own"
  ];
  if (subject.user == "homelab-dashboard" && allowed.indexOf(action.id) !== -1) {
    return polkit.Result.YES;
  }
});
RULES_EOF
  fi
  if [ -n "$POWER" ]; then
    sudo tee /etc/polkit-1/rules.d/50-homelab-dashboard-power.rules >/dev/null << 'RULES_EOF'
// Installed by homelab-dashboard (install.sh pi --power).
// Lets the dashboard's service user reboot or power off this machine, and restart its own service. Nothing else.
polkit.addRule(function(action, subject) {
  if (subject.user != "homelab-dashboard") return;
  var allowed = [
    "org.freedesktop.login1.reboot",
    "org.freedesktop.login1.reboot-multiple-sessions",
    "org.freedesktop.login1.power-off",
    "org.freedesktop.login1.power-off-multiple-sessions"
  ];
  if (allowed.indexOf(action.id) !== -1) {
    return polkit.Result.YES;
  }
  if (action.id == "org.freedesktop.systemd1.manage-units" &&
      action.lookup("unit") == "homelab-dashboard.service" && action.lookup("verb") == "restart") {
    return polkit.Result.YES;
  }
});
RULES_EOF
  fi
  # One PIN protects every on-screen control ("wifi_pin" is the older name for "pin")
  HAVE_PIN="$(python3 -c "import json;c=json.load(open('$DEST/config.json'));print(len(str(c.get('pin') or c.get('wifi_pin') or '')))")"
  if [ "$HAVE_PIN" -lt 4 ]; then
    while :; do
      read -r -s -p "   Choose a PIN (4-8 digits) to protect Wi-Fi and power controls on the screen: " SCREEN_PIN; echo
      case "$SCREEN_PIN" in
        ''|*[!0-9]*) echo "   Digits only." ;;
        *) if [ "${#SCREEN_PIN}" -ge 4 ] && [ "${#SCREEN_PIN}" -le 8 ]; then break; fi; echo "   4 to 8 digits." ;;
      esac
    done
    sudo SCREEN_PIN="$SCREEN_PIN" python3 - "$DEST/config.json" << 'PYEOF'
import json, os, sys
path = sys.argv[1]
cfg = json.load(open(path))
cfg["pin"] = os.environ["SCREEN_PIN"]
json.dump(cfg, open(path, "w"), indent=2)
PYEOF
    unset SCREEN_PIN
    echo "   PIN saved in $DEST/config.json (readable by anyone with a shell on this Pi)."
  else
    echo "   Keeping the PIN already in $DEST/config.json."
  fi
fi

echo "==> Creating the homelab-dashboard service"
sudo tee /etc/systemd/system/homelab-dashboard.service >/dev/null << EOF
[Unit]
Description=Home lab dashboard aggregator
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/bin/python3 $DEST/server.py --config $DEST/config.json$DEMO_FLAG
Restart=always
RestartSec=5
$RUN_AS
ProtectSystem=strict
ProtectHome=yes
NoNewPrivileges=yes
PrivateTmp=yes
ProtectKernelTunables=yes
ProtectControlGroups=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK
CapabilityBoundingSet=

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now homelab-dashboard
sudo systemctl restart homelab-dashboard

if [ -n "$HEADLESS" ]; then
  echo "==> Setting up kiosk on the console (no desktop)"
  MARK="# homelab-dashboard kiosk"
  # Remove blocks left by earlier versions from every login file
  for f in "$HOME/.bash_profile" "$HOME/.bash_login" "$HOME/.profile"; do
    [ -f "$f" ] && sed -i "/^$MARK/,/^fi\$/d" "$f"
  done
  # Bash reads only the FIRST of these that exists, so write to that one
  PROFILE_FILE="$HOME/.profile"
  for f in "$HOME/.bash_profile" "$HOME/.bash_login" "$HOME/.profile"; do
    if [ -f "$f" ]; then PROFILE_FILE="$f"; break; fi
  done
  cat >> "$PROFILE_FILE" << EOF
$MARK - starts the dashboard on the physical screen only (not over SSH)
if [ -z "\$WAYLAND_DISPLAY" ] && [ "\$(tty)" = "/dev/tty1" ]; then
  $DEST/start-kiosk.sh
fi
EOF
  echo "   Kiosk start added to $PROFILE_FILE"
  if command -v raspi-config >/dev/null; then
    sudo raspi-config nonint do_boot_behaviour B2 || true   # console auto-login
  else
    echo "   raspi-config not found - enable console auto-login on tty1 manually."
  fi
  # Make sure this user can open the display and touch screen
  sudo usermod -aG video,render,input "$USER" || true
else
  echo "==> Setting up kiosk autostart (desktop)"
  AUTOSTART_LINE="$DEST/kiosk.sh &"
  configured=""
  # labwc (Raspberry Pi OS default since late 2024)
  mkdir -p "$HOME/.config/labwc"
  touch "$HOME/.config/labwc/autostart"
  grep -qF "$DEST/kiosk.sh" "$HOME/.config/labwc/autostart" || echo "$AUTOSTART_LINE" >> "$HOME/.config/labwc/autostart"
  configured="labwc"
  # wayfire (older Bookworm images)
  if [ -f "$HOME/.config/wayfire.ini" ]; then
    if ! grep -qF "$DEST/kiosk.sh" "$HOME/.config/wayfire.ini"; then
      if grep -q '^\[autostart\]' "$HOME/.config/wayfire.ini"; then
        sed -i "/^\[autostart\]/a homelab = $DEST/kiosk.sh" "$HOME/.config/wayfire.ini"
      else
        printf '\n[autostart]\nhomelab = %s/kiosk.sh\n' "$DEST" >> "$HOME/.config/wayfire.ini"
      fi
    fi
    configured="$configured, wayfire"
  fi
  # X11 / LXDE (legacy images)
  if [ -d /etc/xdg/lxsession/LXDE-pi ] || [ -d "$HOME/.config/lxsession" ]; then
    mkdir -p "$HOME/.config/autostart"
    cat > "$HOME/.config/autostart/homelab-dashboard.desktop" << EOF
[Desktop Entry]
Type=Application
Name=Home lab dashboard
Exec=$DEST/kiosk.sh
EOF
    configured="$configured, X11"
  fi
  echo "   Autostart added for: $configured"

  if command -v raspi-config >/dev/null; then
    sudo raspi-config nonint do_boot_behaviour B4 || true   # boot to desktop, logged in
    sudo raspi-config nonint do_blanking 1 || true          # 1 = disable screen blanking
  else
    echo "   raspi-config not found - enable desktop auto-login and disable screen blanking manually."
  fi
fi

echo "==> Checking the service"
PORT="$(python3 -c "import json;print(json.load(open('$DEST/config.json')).get('port',8080))")"
for _ in $(seq 1 15); do
  if curl -fs "http://127.0.0.1:$PORT/api/state" >/dev/null; then
    curl -fs "http://127.0.0.1:$PORT/api/state" | python3 -c "
import json, sys
for h in json.load(sys.stdin)['hosts']:
    print('   %-12s %-16s %s' % (h['name'], h['address'], 'online' if h['online'] else 'NOT RESPONDING'))"
    echo
    echo "Done. Reboot to start the kiosk:  sudo reboot"
    echo "Settings live in $DEST/config.json (edit with: sudo nano $DEST/config.json)."
    echo "Changes apply within a few seconds; the copy in this folder is only used when you re-run the installer."
    exit 0
  fi
  sleep 1
done
echo "The service did not start. Check: journalctl -u homelab-dashboard -n 50" >&2
exit 1
