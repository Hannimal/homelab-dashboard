# Home lab dashboard

A small status screen for a 5" Raspberry Pi display: one Glances agent per Docker server, a lightweight aggregator on the Pi, and the minimal dashboard you approved, running full screen in Chromium.

```
 node-01 ─┐  Glances agent (Docker, :61208)
 node-02 ─┼──────────────►  Pi: server.py (:8080)  ──►  Chromium kiosk on the Waveshare
 node-03 ─┘                 polls every 5 s, works out alerts
```

Nothing to compile and no pip packages: the agent is the official `nicolargo/glances` image, and the Pi side is plain Python 3 plus one HTML file.

## Morning checklist (about 15 minutes)

### 1. Each of the 3 servers

Copy the `servers` folder over and run the installer. Passing the Pi's IP opens the port only to the Pi when `ufw` is active.

```bash
scp -r servers you@192.168.1.11:~/homelab-agent
ssh you@192.168.1.11
cd ~/homelab-agent && sudo ./install-agent.sh 192.168.1.50   # ← your Pi's IP
```

It finishes with "Agent running on …". Repeat for the other two servers.

### 2. The Raspberry Pi

No desktop is needed. The recommended base is **Raspberry Pi OS Lite (64-bit)**. On it, the dashboard runs in `cage`, a tiny Wayland compositor that shows a single full-screen app, so there's no desktop, window manager or login screen.

1. Flash Raspberry Pi OS Lite (64-bit) with Raspberry Pi Imager. In Imager's settings, set the hostname, your user and password, Wi-Fi if needed, and turn on SSH.
2. Boot the Pi with the screen attached, then copy the files over and install:

```bash
scp -r pi you@192.168.1.50:~/homelab-dashboard
ssh you@192.168.1.50
cd ~/homelab-dashboard
cp config.example.json config.json
nano config.json          # put in your 3 server names and IPs
./install-pi.sh           # run as your normal user, not sudo
sudo reboot
```

The installer detects that there's no desktop. It installs only `python3`, `curl`, `chromium` and `cage`, and turns on console auto-login. When the Pi logs in on the physical screen, the dashboard starts. SSH logins are unaffected and just give you a normal shell.

It prints each host as `online` or `NOT RESPONDING` before you reboot, so you know it works before looking at the screen. To try the screen before the servers are ready, run `./install-pi.sh --demo`. It shows the mockup's sample data. Run `./install-pi.sh` again later to switch to real data.

If the Pi already runs the desktop edition, the same installer adds the dashboard to the desktop's autostart instead.

**Which Pi.** Chromium is the heaviest part of this setup. A Pi 3B+, 4 or 5 runs it comfortably. A Pi Zero 2 W or 3A+ (512 MB) works, but only just.

## Using the screen

- **Overview** shows one column per server with CPU, memory, the fullest disk, container count and CPU temperature. Tap a server to see its containers.
- **Containers** lists every container on the selected server, with problems at the top. Tap the server names at the top to switch.
- **Alerts** lists everything that needs attention.
- The screen goes back to Overview after 90 seconds without a touch.

A server turns amber ("Attention") when any of these happen:

- CPU, memory, disk or temperature passes a threshold.
- A container is exited, restarting or dead.

It turns red ("Offline") when its agent stops answering. If the data stops updating, or the Pi service can't be reached, a red "stale" or "service unreachable" note appears next to the title on every screen.

## Settings (`/opt/homelab-dashboard/config.json` on the Pi)

| Key | What it does |
| --- | --- |
| `hosts` | Name and IP of each server. Add a 4th server here and the layout adapts. |
| `thresholds` | Warning levels for CPU %, memory %, disk % and temperature °C. |
| `hide_containers` | Containers never shown (the agent itself by default). |
| `expected_stopped` | Containers that are allowed to be stopped, such as one-shot backup jobs, so they don't raise alerts. Also accepted inside a single host entry (`"expected_stopped": ["backup"]`) to apply to that host only. |
| `alert_confirm_samples` | How many polls in a row a problem must last before it shows (default 2, about 10 s). Stops a value hovering at a threshold from flipping the screen back and forth. Set to 1 for instant alerts. |
| `disk_mounts` (per host) | Limit which mount points count as disks, e.g. `["/", "/host/data"]`. |
| `listen` / `port` | Where the Pi serves the page. Set `listen` to `"0.0.0.0"` to open the dashboard from your laptop too. |

Edit it on the Pi with `sudo nano /opt/homelab-dashboard/config.json`. Changes apply within a few seconds, with no restart needed. The only exceptions are `listen` and `port`, which need `sudo systemctl restart homelab-dashboard`.

The `config.json` in the folder you copied over is only read when you run `./install-pi.sh`, which copies it into `/opt`. Editing that copy does nothing until you re-run the installer.

If you save invalid JSON, such as a missing quote or a trailing comma, the dashboard keeps running on the last good settings and shows the error in red at the bottom of the screen.

**Extra data disks.** The agent sees the server's root disk automatically. To include another disk such as `/mnt/data`, uncomment the example volume line in `/opt/homelab-agent/docker-compose.yml` on that server. Then run `docker compose up -d` in that folder.

## The Waveshare 5" display

Most Waveshare 5" screens work out of the box on current Raspberry Pi OS. If yours shows a wrong resolution or a black border:

- **HDMI models:** add `video=HDMI-A-1:800x480M@60` to the end of the single line in `/boot/firmware/cmdline.txt`, then reboot. Touch works over the USB cable.
- **DSI models:** follow the `dtoverlay` line on your model's Waveshare wiki page.

The dashboard scales itself to fit whatever resolution the screen reports, so it never gets cut off.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| A server shows Offline | From the Pi: `curl http://SERVER_IP:61208/api/4/status`. On the server: `docker logs glances-agent`. |
| "Can't reach the dashboard service" | `systemctl status homelab-dashboard` and `journalctl -u homelab-dashboard -n 50` |
| Containers list is empty | The agent can't read Docker. Check that `/var/run/docker.sock` exists on that server. |
| No temperature shown | That machine exposes no CPU sensor to Glances. This is common in VMs, and harmless. |
| Kiosk didn't open after reboot | Over SSH, run `/opt/homelab-dashboard/diagnose.sh`. It checks each piece and prints the fix. The kiosk's own log is `~/.cache/homelab-kiosk.log`. |
| Pi has a desktop installed but you want the console kiosk | `./install-pi.sh --console` |
| Screen goes black after a while | `sudo raspi-config` → Display Options → Screen Blanking → No |

## Security notes

- The agent image is pinned (`nicolargo/glances:4.5.8`) because the dashboard depends on its JSON format. To upgrade, change the tag in `servers/docker-compose.yml`, run the demo or a real poll against it, then redeploy.
- The Glances API has no password. It is read-only, but anyone on your LAN can see the stats. The `ufw` rule from step 1 limits it to the Pi. The installer prints a warning when it couldn't add that rule (no Pi IP given, or `ufw` missing or inactive), because the port is then open to the whole network.
- Mounting the Docker socket gives the agent container full control of Docker, even with `:ro`, because that flag only stops the file being replaced, not API calls. Glances only reads from it, but keep the image from a trusted source, as this setup does.
- On the Pi the aggregator runs as a locked-down throwaway user (`DynamicUser`) and by default listens only on the Pi itself.

## Working on the code

This folder is a git repository. Your real `pi/config.json`, which holds your IPs, is git-ignored. Only `config.example.json` is tracked.

To push the current code to a machine and run its installer, use `deploy.sh`. It replaces the remote folder each time, so the nested-folder problem with `scp` can't happen.

```bash
./deploy.sh pi hannimal@10.1.2.2 --console
./deploy.sh server you@192.168.1.11 10.1.2.2   # last argument: the Pi's IP, for the firewall rule
```

Re-deploying the Pi keeps your live settings in `/opt/homelab-dashboard/config.json`.

To test the dashboard on your computer without a Pi:

```bash
cd pi && cp config.example.json config.json && python3 server.py --demo
# open http://127.0.0.1:8080
```

Tests (standard library only) and the checks CI runs:

```bash
python3 -m unittest discover -s tests -v
shellcheck -S warning deploy.sh pi/*.sh servers/*.sh
```

### Layout

| Path | What it is |
| --- | --- |
| `servers/` | Glances agent: `docker-compose.yml`, `glances.conf`, `install-agent.sh` |
| `pi/server.py` | Aggregator. Polls the agents, works out alerts, serves `/api/state` and the page. Standard library only. |
| `pi/web/index.html` | The dashboard, designed at 800×480 |
| `pi/kiosk.sh`, `pi/start-kiosk.sh` | Start Chromium in cage, with logging and retries |
| `pi/install-pi.sh`, `pi/diagnose.sh` | Pi installer and health check |
| `deploy.sh` | Push code to the Pi or a server and install |
| `tests/` | Unit tests for the aggregator |
| `.github/workflows/ci.yml` | Runs the tests, ShellCheck and a compose-file check |

## Uninstall

- **Server:** `cd /opt/homelab-agent && sudo docker compose down && sudo rm -rf /opt/homelab-agent`
- **Pi:** `sudo systemctl disable --now homelab-dashboard && sudo rm -rf /opt/homelab-dashboard /etc/systemd/system/homelab-dashboard.service`, then remove the "homelab-dashboard kiosk" block from `~/.profile` (Lite) or the `kiosk.sh` line from `~/.config/labwc/autostart` (desktop).
