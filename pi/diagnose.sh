#!/usr/bin/env bash
# Checks every piece of the Pi kiosk setup and says which one is broken.
# Run as your normal user (works over SSH):  /opt/homelab-dashboard/diagnose.sh
DEST=/opt/homelab-dashboard
ok()   { printf '  \033[32mOK\033[0m    %s\n' "$1"; }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; [ -n "${2:-}" ] && printf '        fix: %s\n' "$2"; }
info() { printf '        %s\n' "$1"; }

echo "Dashboard service"
if systemctl is-active --quiet homelab-dashboard; then ok "homelab-dashboard is running"
else bad "homelab-dashboard is not running" "journalctl -u homelab-dashboard -n 30"; fi
PORT="$(python3 -c "import json;print(json.load(open('$DEST/config.json')).get('port',8080))" 2>/dev/null)"
if [ -z "$PORT" ]; then bad "$DEST/config.json is missing or not valid JSON" "sudo nano $DEST/config.json"; PORT=8080; fi
if curl -fs "http://127.0.0.1:$PORT/api/state" >/dev/null; then ok "API answers on port $PORT"
else bad "API does not answer on port $PORT"; fi

echo "Kiosk programs"
if command -v cage >/dev/null; then ok "cage installed"; else bad "cage missing" "sudo apt install cage"; fi
if command -v chromium >/dev/null || command -v chromium-browser >/dev/null; then ok "chromium installed"
else bad "chromium missing" "sudo apt install chromium (or chromium-browser)"; fi
[ -x "$DEST/start-kiosk.sh" ] && ok "start-kiosk.sh present" || bad "start-kiosk.sh missing" "re-run ./install-pi.sh"

echo "Login and autostart"
if grep -qs "autologin" /etc/systemd/system/getty@tty1.service.d/*.conf; then
  ok "console auto-login on tty1 ($(grep -ho 'autologin [^ ]*' /etc/systemd/system/getty@tty1.service.d/*.conf | head -1))"
else bad "console auto-login is not enabled" "sudo raspi-config nonint do_boot_behaviour B2"; fi
info "boots to: $(systemctl get-default)"
PF=""; for f in "$HOME/.bash_profile" "$HOME/.bash_login" "$HOME/.profile"; do [ -f "$f" ] && { PF="$f"; break; }; done
info "bash reads this login file: ${PF:-none}"
if [ -n "$PF" ] && grep -q "homelab-dashboard kiosk" "$PF"; then ok "kiosk start block found in $PF"
else bad "kiosk start block is not in the file bash actually reads (${PF:-none})" "re-run ./install-pi.sh"; fi
for f in "$HOME/.bash_profile" "$HOME/.bash_login" "$HOME/.profile"; do
  [ "$f" != "$PF" ] && grep -qs "homelab-dashboard kiosk" "$f" && info "note: an unused old block is also in $f"
done
for g in video render input; do id -nG | grep -qw $g && ok "user is in group $g" || bad "user not in group $g" "sudo usermod -aG $g $USER, then reboot"; done
if command -v labwc >/dev/null || command -v wayfire >/dev/null || command -v lxsession >/dev/null; then
  info "a desktop is installed on this Pi; use ./install-pi.sh --console to force the no-desktop kiosk"
fi

echo "Last kiosk log ($HOME/.cache/homelab-kiosk.log)"
if [ -f "$HOME/.cache/homelab-kiosk.log" ]; then tail -n 15 "$HOME/.cache/homelab-kiosk.log" | sed 's/^/        /'
else info "no log yet - the kiosk has not tried to start since this version was installed"; fi
