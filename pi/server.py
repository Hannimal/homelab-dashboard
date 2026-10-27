#!/usr/bin/env python3
"""
Home lab dashboard - aggregator for the Raspberry Pi.

Polls the Glances agent on every server, normalises the data, works out alerts,
and serves the dashboard page plus one JSON endpoint (/api/state).

Standard library only - no pip installs needed.

    python3 server.py                 # uses config.json next to this file
    python3 server.py --config x.json
    python3 server.py --demo          # fake data, to test the screen without servers
"""

import argparse
import json
import logging
import os
import random
import re
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")

DEFAULTS = {
    "listen": "127.0.0.1",
    "port": 8080,
    "poll_seconds": 5,
    "timeout_seconds": 3,
    "thresholds": {"cpu_warn": 90, "mem_warn": 90, "disk_warn": 85, "temp_warn": 80},
    "hide_containers": ["glances-agent"],
    "expected_stopped": [],
    "alert_confirm_samples": 2,
    "hosts": [],
}

# Filesystems that never represent a real disk the user cares about
IGNORED_FS_TYPES = {"squashfs", "overlay", "tmpfs", "devtmpfs", "iso9660", "ramfs", "nsfs", "fuse.lxcfs"}

log = logging.getLogger("homelab")


# --------------------------------------------------------------------------- helpers

def load_config(path):
    cfg = json.loads(json.dumps(DEFAULTS))  # deep copy
    with open(path, encoding="utf-8") as f:
        user = json.load(f)
    for k, v in user.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    if not cfg["hosts"]:
        raise SystemExit(f"No hosts defined in {path}")
    return cfg


def fetch_json(url, timeout):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def parse_uptime(text):
    """Glances returns str(timedelta): '23 days, 4:05:12' or '4:05:12'. Returns seconds."""
    if not isinstance(text, str):
        return None
    days = 0
    m = re.match(r"\s*(\d+)\s+days?,\s*(.*)", text)
    if m:
        days, text = int(m.group(1)), m.group(2)
    parts = text.strip().split(":")
    try:
        h, mnt, s = (int(float(p)) for p in parts[-3:]) if len(parts) >= 3 else (0, 0, 0)
    except ValueError:
        h = mnt = s = 0
    return days * 86400 + h * 3600 + mnt * 60 + s


def human_uptime(seconds):
    if seconds is None:
        return ""
    d = seconds // 86400
    if d >= 2:
        return f"{d} days"
    if d == 1:
        return "1 day"
    h = seconds // 3600
    if h >= 1:
        return f"{h} hour{'s' if h > 1 else ''}"
    return f"{max(1, seconds // 60)} min"


def human_bytes(n):
    if n is None:
        return "—"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB", "MB") else f"{n:.1f} {unit}"
        n /= 1024
    return "—"


def short_uptime(text):
    """Compact Glances' container uptime ('2 days', 'an hour', 'just now')."""
    if not text:
        return "—"
    t = str(text).replace("an hour", "1h").replace("a minute", "1m").replace("a day", "1d").replace("a week", "1w").replace("a month", "1mo").replace("a year", "1y")
    t = re.sub(r"(\d+)\s*days?", r"\1d", t)
    t = re.sub(r"(\d+)\s*weeks?", r"\1w", t)
    t = re.sub(r"(\d+)\s*months?", r"\1mo", t)
    t = re.sub(r"(\d+)\s*hours?", r"\1h", t)
    t = re.sub(r"(\d+)\s*minutes?", r"\1m", t)
    t = re.sub(r"(\d+)\s*seconds?", r"\1s", t)
    return t.replace("just now", "now").replace(" ago", "")


# --------------------------------------------------------------------------- collection

# Glances reports every temperature as "temperature_core", including NVMe drives,
# GPUs and Wi-Fi cards. These labels are not the CPU, so they are skipped when
# something else is available.
NON_CPU_SENSOR = re.compile(r"nvme|composite|drive|gpu|wifi|iwl|amdgpu|sensor \d", re.I)


def cpu_temp(sensors):
    """Hottest CPU temperature in °C from Glances' /sensors list, or None."""
    temps = [s for s in (sensors or [])
             if s.get("type") == "temperature_core"
             and isinstance(s.get("value"), (int, float)) and s["value"] > 0]
    cpu = [s for s in temps if not NON_CPU_SENSOR.search(str(s.get("label", "")))]
    chosen = cpu or temps
    return round(max(s["value"] for s in chosen)) if chosen else None


ENDPOINTS = ("cpu", "mem", "fs", "uptime", "sensors", "containers", "load")


def collect_host(host, cfg):
    """Fetch one host's stats from its Glances API and normalise them."""
    base = f"http://{host['address']}:{host.get('port', 61208)}/api/4"
    timeout = cfg["timeout_seconds"]
    out = {"name": host["name"], "address": host["address"], "online": False}

    raw = {}
    try:
        with ThreadPoolExecutor(max_workers=len(ENDPOINTS)) as pool:
            futures = {ep: pool.submit(fetch_json, f"{base}/{ep}", timeout) for ep in ENDPOINTS}
            for ep, fut in futures.items():
                try:
                    raw[ep] = fut.result()
                except Exception as e:  # one plugin failing should not take the host down
                    raw[ep] = None
                    if ep in ("cpu", "mem"):
                        raise e
    except Exception as e:
        out["error"] = str(e)
        return out

    out["online"] = True
    cpu = raw.get("cpu") or {}
    mem = raw.get("mem") or {}
    out["cpu"] = round(float(cpu.get("total", 0.0)), 1)
    out["cores"] = cpu.get("cpucore")
    out["mem_used"] = mem.get("used")
    out["mem_total"] = mem.get("total")
    out["mem_pct"] = round(float(mem.get("percent", 0.0)), 1)
    out["load1"] = (raw.get("load") or {}).get("min1")

    secs = parse_uptime(raw.get("uptime"))
    out["uptime"] = human_uptime(secs)

    # Disks: dedupe by device (bind mounts inside the agent container all point
    # at the same host disk), optionally restricted to the mounts listed in config.
    wanted = set(host.get("disk_mounts") or [])
    disks = {}
    for fs in raw.get("fs") or []:
        if fs.get("fs_type") in IGNORED_FS_TYPES or not fs.get("size"):
            continue
        mnt = fs.get("mnt_point", "")
        if wanted and mnt not in wanted:
            continue
        dev = fs.get("device_name") or mnt
        label = mnt if mnt not in ("/etc/hostname", "/etc/hosts", "/etc/resolv.conf") else "/"
        cur = disks.get(dev)
        if cur is None or (cur["mount"] != "/" and label == "/"):
            disks[dev] = {"mount": label, "pct": round(float(fs.get("percent", 0)), 1),
                          "used": fs.get("used"), "size": fs.get("size")}
    out["disks"] = sorted(disks.values(), key=lambda d: -d["pct"])

    out["temp"] = cpu_temp(raw.get("sensors"))

    hide = set(cfg["hide_containers"])
    containers = []
    for c in raw.get("containers") or []:
        name = c.get("name") or "?"
        if name in hide:
            continue
        status = (c.get("status") or "unknown").lower()
        running = status == "running"
        containers.append({
            "name": name,
            "status": status,
            "cpu": f"{c['cpu_percent']:.1f}%" if running and isinstance(c.get("cpu_percent"), (int, float)) else "—",
            "mem": human_bytes(c.get("memory_usage")) if running and c.get("memory_usage") else "—",
            "uptime": short_uptime(c.get("uptime")) if running or status == "restarting" else "—",
        })
    order = {"restarting": 0, "dead": 0, "exited": 1, "paused": 2, "created": 3, "running": 4}
    containers.sort(key=lambda c: (order.get(c["status"], 1), c["name"]))
    out["containers"] = containers
    out["running"] = sum(1 for c in containers if c["status"] == "running")
    out["total"] = len(containers)
    return out


def evaluate(host, cfg, host_cfg=None):
    """Attach status + alert list to a host.

    Each alert carries a stable `key` (independent of the current value) so the
    collector can tell whether the same problem persisted across polls.
    """
    t = cfg["thresholds"]
    alerts = []
    if not host["online"]:
        host["status"] = "down"
        host["alerts"] = [{"host": host["name"], "level": "down", "key": "down", "text": "Agent not responding"}]
        return host

    def add(key, text):
        alerts.append({"host": host["name"], "level": "warn", "key": key, "text": text})

    if host["cpu"] >= t["cpu_warn"]:
        add("cpu", f"CPU at {host['cpu']:.0f}%")
    if host["mem_pct"] >= t["mem_warn"]:
        add("mem", f"Memory at {host['mem_pct']:.0f}%")
    for d in host["disks"]:
        if d["pct"] >= t["disk_warn"]:
            add(f"disk:{d['mount']}", f"Disk {d['mount']} at {d['pct']:.0f}%")
    if host.get("temp") is not None and host["temp"] >= t["temp_warn"]:
        add("temp", f"CPU temperature {host['temp']}°C")
    ok_stopped = set(cfg["expected_stopped"]) | set((host_cfg or {}).get("expected_stopped") or [])
    for c in host["containers"]:
        if c["status"] == "running" or c["name"] in ok_stopped:
            continue
        add(f"container:{c['name']}:{c['status']}", f"{c['name']} is {c['status']}")

    host["alerts"] = alerts
    host["status"] = "warn" if alerts else "ok"
    return host


def debounce(hosts, streaks, confirm):
    """Only report an alert once it has been seen in `confirm` consecutive polls.

    `streaks` maps (host, key) -> consecutive count and is updated in place; keys
    that did not fire this poll are dropped, so a recovered problem starts over.
    A host that is offline but not yet confirmed shows as "warn" rather than "ok".
    """
    seen = set()
    for h in hosts:
        confirmed = []
        for a in h["alerts"]:
            k = (a["host"], a["key"])
            seen.add(k)
            streaks[k] = streaks.get(k, 0) + 1
            if streaks[k] >= confirm:
                confirmed.append(a)
        h["alerts"] = confirmed
        if any(a["level"] == "down" for a in confirmed):
            h["status"] = "down"
        elif confirmed or not h["online"]:
            h["status"] = "warn"
        else:
            h["status"] = "ok"
    for k in [k for k in streaks if k not in seen]:
        del streaks[k]
    return hosts


class Collector:
    def __init__(self, cfg, demo=False, config_path=None):
        self.cfg = cfg
        self.demo = demo
        self.config_path = config_path
        self.config_mtime = os.path.getmtime(config_path) if config_path else None
        self.config_error = None
        self.streaks = {}
        self.lock = threading.Lock()
        self.state = {"hosts": [], "alerts": [], "updated": None}

    def reload_config_if_changed(self):
        """Pick up edits to config.json without a restart (listen/port still need one)."""
        if not self.config_path:
            return
        try:
            mtime = os.path.getmtime(self.config_path)
        except OSError as e:
            self.config_error = f"Can't read {self.config_path}: {e.strerror}"
            return
        if mtime == self.config_mtime:
            return
        self.config_mtime = mtime
        try:
            new = load_config(self.config_path)
        except (ValueError, SystemExit) as e:
            # Keep running on the last good config and say why on the screen
            self.config_error = f"config.json not applied: {e}"
            log.error(self.config_error)
            return
        if (new["listen"], new["port"]) != (self.cfg["listen"], self.cfg["port"]):
            log.warning("listen/port changed - restart the service to apply: sudo systemctl restart homelab-dashboard")
            new["listen"], new["port"] = self.cfg["listen"], self.cfg["port"]
        self.cfg = new
        self.config_error = None
        log.info("Reloaded config: %s", ", ".join(f"{h['name']}={h['address']}" for h in new["hosts"]))

    def poll_once(self):
        self.reload_config_if_changed()
        cfg = self.cfg  # one config for the whole poll, even if a reload lands meanwhile
        hosts_cfg = cfg["hosts"]
        if self.demo:
            hosts = [demo_host(h, i) for i, h in enumerate(hosts_cfg)]
        else:
            with ThreadPoolExecutor(max_workers=max(1, len(hosts_cfg))) as pool:
                hosts = list(pool.map(lambda h: collect_host(h, cfg), hosts_cfg))
        hosts = [evaluate(h, cfg, hc) for h, hc in zip(hosts, hosts_cfg)]
        hosts = debounce(hosts, self.streaks, max(1, int(cfg["alert_confirm_samples"])))
        alerts = [{k: v for k, v in a.items() if k != "key"} for h in hosts for a in h["alerts"]]
        for h in hosts:
            h["alerts"] = [{k: v for k, v in a.items() if k != "key"} for a in h["alerts"]]
        state = {
            "hosts": hosts,
            "alerts": alerts,
            "updated": time.time(),
            "config_error": self.config_error,
            "summary": {
                "hosts": len(hosts),
                "online": sum(1 for h in hosts if h["online"]),
                "running": sum(h.get("running", 0) for h in hosts),
                "total": sum(h.get("total", 0) for h in hosts),
            },
        }
        with self.lock:
            self.state = state

    def run(self):
        while True:
            start = time.time()
            try:
                self.poll_once()
            except Exception:
                log.exception("Polling failed")
            time.sleep(max(0.5, self.cfg["poll_seconds"] - (time.time() - start)))

    def snapshot(self):
        with self.lock:
            return self.state


# --------------------------------------------------------------------------- demo data

DEMO = [
    {"cpu": 18, "mem": (9.1, 16), "disk": 41, "temp": 48, "up": "23 days", "containers": [
        ("traefik", "running"), ("home-assistant", "running"), ("mosquitto", "running"),
        ("zigbee2mqtt", "running"), ("pihole", "running"), ("uptime-kuma", "running"),
        ("vaultwarden", "running")]},
    {"cpu": 62, "mem": (21.4, 32), "disk": 58, "temp": 61, "up": "7 days", "containers": [
        ("jellyfin", "running"), ("sonarr", "running"), ("radarr", "running"),
        ("qbittorrent", "running"), ("prowlarr", "running"), ("postgres", "running"),
        ("grafana", "exited")]},
    {"cpu": 9, "mem": (5.2, 8), "disk": 91, "temp": 44, "up": "41 days", "containers": [
        ("immich-ml", "restarting"), ("immich-server", "running"), ("immich-redis", "running"),
        ("nextcloud", "running"), ("mariadb", "running"), ("syncthing", "running"),
        ("restic-backup", "running")]},
]


def demo_host(h, i):
    d = DEMO[i % len(DEMO)]
    j = lambda v, r: max(0, min(100, v + random.uniform(-r, r)))
    used, total = d["mem"]
    containers = []
    for name, st in d["containers"]:
        run = st == "running"
        containers.append({"name": name, "status": st,
                           "cpu": f"{random.uniform(0.1, 6):.1f}%" if run else "—",
                           "mem": f"{random.randint(20, 900)} MB" if run else "—",
                           "uptime": d["up"].split()[0] + "d" if run else ("2m" if st == "restarting" else "—")})
    return {"name": h["name"], "address": h["address"], "online": True,
            "cpu": round(j(d["cpu"], 4), 1), "cores": 8,
            "mem_used": used * 1024**3, "mem_total": total * 1024**3, "mem_pct": round(used / total * 100, 1),
            "load1": 0.5, "uptime": d["up"],
            "disks": [{"mount": "/", "pct": float(d["disk"]), "used": None, "size": None}],
            "temp": d["temp"], "containers": containers,
            "running": sum(1 for c in containers if c["status"] == "running"), "total": len(containers)}


# --------------------------------------------------------------------------- web server

def make_handler(collector):
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=WEB_DIR, **kw)

        def end_headers(self):
            # The page only ever talks to this server: no framing, no outside requests.
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                             "style-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            super().end_headers()

        def do_GET(self):
            if self.path.split("?")[0] == "/api/state":
                body = json.dumps(collector.snapshot()).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/healthz":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")
                return
            return super().do_GET()

        def log_message(self, fmt, *args):  # keep the journal quiet
            pass

    return Handler


def main():
    ap = argparse.ArgumentParser(description="Home lab dashboard aggregator")
    ap.add_argument("--config", default=os.path.join(BASE_DIR, "config.json"))
    ap.add_argument("--demo", action="store_true", help="serve fake data")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(args.config)
    collector = Collector(cfg, demo=args.demo, config_path=args.config)
    collector.poll_once()
    threading.Thread(target=collector.run, daemon=True).start()

    server = ThreadingHTTPServer((cfg["listen"], cfg["port"]), make_handler(collector))
    log.info("Dashboard on http://%s:%s (%s)", cfg["listen"], cfg["port"], "demo" if args.demo else
             ", ".join(f"{h['name']}={h['address']}" for h in cfg["hosts"]))
    server.serve_forever()


if __name__ == "__main__":
    main()
