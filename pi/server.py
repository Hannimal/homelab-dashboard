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
import glob
import hmac
import json
import logging
import os
import random
import re
import shutil
import subprocess
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
    "pin": "",
    "wifi_pin": "",   # older name for "pin"; still honoured
    "screen": {"idle_minutes": 0, "off_at": "", "on_at": "", "wake_seconds": 60,
               "wake_on_alert": False, "method": "auto"},
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
    check_screen(cfg["screen"])
    return cfg


SCREEN_METHODS = ("auto", "none", "wlr-randr", "backlight", "x11", "vcgencmd")


def check_screen(screen):
    """Stop a bad "screen" block from being applied; the old config stays in force."""
    def time_ok(v):
        return v == "" or bool(re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", str(v)))
    idle, wake = screen.get("idle_minutes"), screen.get("wake_seconds")
    if isinstance(idle, bool) or not isinstance(idle, (int, float)) or idle < 0:
        raise SystemExit('screen.idle_minutes must be a number, 0 or more (0 = never sleep when idle)')
    if isinstance(wake, bool) or not isinstance(wake, (int, float)) or not 5 <= wake <= 3600:
        raise SystemExit("screen.wake_seconds must be between 5 and 3600")
    if not time_ok(screen.get("off_at")) or not time_ok(screen.get("on_at")):
        raise SystemExit('screen.off_at and screen.on_at must look like "23:00"')
    if bool(screen.get("off_at")) != bool(screen.get("on_at")):
        raise SystemExit("screen.off_at and screen.on_at must be set together")
    if screen.get("method") not in SCREEN_METHODS:
        raise SystemExit("screen.method must be one of: " + ", ".join(SCREEN_METHODS))
    if not isinstance(screen.get("wake_on_alert"), bool):
        raise SystemExit("screen.wake_on_alert must be true or false")


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
        # Glances shows the Docker health state INSTEAD of "running" for containers that have a
        # healthcheck. Healthy and still-starting ones are running normally; "unhealthy" is a real problem.
        if status in ("healthy", "starting"):
            status = "running"
        running = status in ("running", "unhealthy")
        containers.append({
            "name": name,
            "status": status,
            "cpu": f"{c['cpu_percent']:.1f}%" if running and isinstance(c.get("cpu_percent"), (int, float)) else "—",
            "mem": human_bytes(c.get("memory_usage")) if running and c.get("memory_usage") else "—",
            "uptime": short_uptime(c.get("uptime")) if running or status == "restarting" else "—",
        })
    order = {"restarting": 0, "dead": 0, "unhealthy": 0, "exited": 1, "paused": 2, "created": 3, "running": 4}
    containers.sort(key=lambda c: (order.get(c["status"], 1), c["name"]))
    out["containers"] = containers
    out["running"] = sum(1 for c in containers if c["status"] in ("running", "unhealthy"))
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


# --------------------------------------------------------------------------- the Pi itself

def read_text(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return None


def read_number(path):
    try:
        return float(read_text(path))
    except (TypeError, ValueError):
        return None


def run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=2).stdout
    except (OSError, subprocess.SubprocessError):
        return None


def parse_cpu_stat(text):
    """(busy, total) jiffies from the first line of /proc/stat, or None."""
    try:
        parts = [int(x) for x in text.splitlines()[0].split()[1:9]]
        idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
    except (AttributeError, IndexError, ValueError):
        return None
    total = sum(parts)
    return total - idle, total


def parse_meminfo(text):
    """(used, total) bytes from /proc/meminfo; 'used' means not available."""
    vals = {}
    for line in (text or "").splitlines():
        key, _, rest = line.partition(":")
        m = re.match(r"\s*(\d+)", rest)
        if m:
            vals[key] = int(m.group(1)) * 1024
    if "MemTotal" not in vals or "MemAvailable" not in vals:
        return None
    return vals["MemTotal"] - vals["MemAvailable"], vals["MemTotal"]


def parse_ip_addr(text):
    """{interface: ipv4} from `ip -4 -o addr show` (first address wins)."""
    out = {}
    for line in (text or "").splitlines():
        f = line.split()
        if len(f) >= 4 and f[2] == "inet":
            out.setdefault(f[1], f[3].split("/")[0])
    return out


def parse_iw_link(text):
    """SSID, signal (dBm) and bitrate (Mbit/s) from `iw dev X link`; None when not connected."""
    if not text or "Not connected" in text:
        return None
    ssid = re.search(r"SSID:\s*(.+)", text)
    sig = re.search(r"signal:\s*(-?\d+)", text)
    rate = re.search(r"tx bitrate:\s*([\d.]+)", text)
    return {"ssid": ssid.group(1).strip() if ssid else None,
            "signal": int(sig.group(1)) if sig else None,
            "bitrate": round(float(rate.group(1))) if rate else None}


def parse_proc_wireless(text):
    """{interface: signal dBm} from /proc/net/wireless (drivers that report dBm only)."""
    out = {}
    for line in (text or "").splitlines()[2:]:
        name, _, rest = line.partition(":")
        f = rest.split()
        if len(f) >= 3:
            try:
                level = float(f[2].rstrip("."))
            except ValueError:
                continue
            if level < 0:
                out[name.strip()] = round(level)
    return out


def power_status(value):
    """Interpret the Pi firmware's get_throttled bit field -> (level, text), or None."""
    if value is None:
        return None
    now = [label for bit, label in ((0x1, "under-voltage"), (0x2, "frequency capped"),
                                    (0x4, "throttled"), (0x8, "temperature limit")) if value & bit]
    if now:
        return "warn", "Now: " + ", ".join(now)
    if value & 0xF0000:
        return "note", "OK now, issues since boot"
    return "ok", "OK"


def read_fan():
    """{'rpm', 'pct'} for the first fan the kernel exposes, or None."""
    for hw in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        rpm = read_number(f"{hw}/fan1_input")
        if rpm is not None:
            pwm = read_number(f"{hw}/pwm1")
            return {"rpm": int(rpm), "pct": round(pwm / 255 * 100) if pwm is not None else None}
    for dev in sorted(glob.glob("/sys/class/thermal/cooling_device*")):
        if "fan" in (read_text(f"{dev}/type") or "").lower():
            cur, top = read_number(f"{dev}/cur_state"), read_number(f"{dev}/max_state")
            if cur is not None and top:
                return {"rpm": None, "pct": round(cur / top * 100)}
    return None


def read_network():
    """Ethernet and Wi-Fi interfaces with link state, address and signal."""
    ips = parse_ip_addr(run(["ip", "-4", "-o", "addr", "show"]))
    signals = parse_proc_wireless(read_text("/proc/net/wireless"))
    nets = []
    for path in sorted(glob.glob("/sys/class/net/*")):
        name = os.path.basename(path)
        wifi = os.path.isdir(f"{path}/wireless")
        if not wifi and not re.match(r"(eth|en)", name):
            continue
        up = read_text(f"{path}/operstate") == "up"
        net = {"name": name, "kind": "wifi" if wifi else "ethernet", "up": up, "ip": ips.get(name)}
        if wifi:
            link = (parse_iw_link(run(["iw", "dev", name, "link"])) if up else None) or {}
            net["ssid"] = link.get("ssid")
            net["signal"] = link.get("signal", signals.get(name) if up else None)
            net["bitrate"] = link.get("bitrate")
        else:
            speed = read_number(f"{path}/speed")
            net["speed"] = int(speed) if up and speed and speed > 0 else None
        nets.append(net)
    return nets


def read_throttled():
    paths = ["/sys/devices/platform/soc/soc:firmware/get_throttled"] + \
        glob.glob("/sys/devices/platform/*/*firmware*/get_throttled")
    for path in paths:
        text = read_text(path)
        if text:
            try:
                return int(text, 16)
            except ValueError:
                pass
    return None


class PiInfo:
    """Facts about the Pi that runs the dashboard (not the monitored servers)."""
    CACHE_S = 4

    def __init__(self, thresholds, demo=False):
        self.thresholds = thresholds
        self.demo = demo
        self.lock = threading.Lock()
        self.cpu_prev = None
        self.cpu_pct = None
        self.cache = (0.0, None)

    def sample_cpu(self):
        """Called every poll so CPU % is measured over a few seconds, not since boot."""
        cur = parse_cpu_stat(read_text("/proc/stat"))
        with self.lock:
            if cur and self.cpu_prev and cur[1] > self.cpu_prev[1]:
                self.cpu_pct = round(100 * (cur[0] - self.cpu_prev[0]) / (cur[1] - self.cpu_prev[1]), 1)
            if cur:
                self.cpu_prev = cur

    def snapshot(self):
        now = time.time()
        with self.lock:
            stamp, data = self.cache
            if data is not None and now - stamp < self.CACHE_S:
                return data
            cpu_pct = self.cpu_pct
        data = self.demo_data() if self.demo else self.gather(cpu_pct)
        data["thresholds"] = dict(self.thresholds())
        with self.lock:
            self.cache = (now, data)
        return data

    def gather(self, cpu_pct):
        mem = parse_meminfo(read_text("/proc/meminfo"))
        try:
            du = shutil.disk_usage("/")
            disk = {"used": du.used, "total": du.total, "pct": round(du.used / du.total * 100, 1)}
        except OSError:
            disk = None
        temp = read_number("/sys/class/thermal/thermal_zone0/temp")
        freq = read_number("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq")
        up = (read_text("/proc/uptime") or "").split()
        power = power_status(read_throttled())
        os_name = next((l.split("=", 1)[1].strip('"') for l in (read_text("/etc/os-release") or "").splitlines()
                        if l.startswith("PRETTY_NAME=")), None)
        try:
            load1 = round(os.getloadavg()[0], 2)
        except OSError:
            load1 = None
        return {
            "hostname": os.uname().nodename,
            "model": (read_text("/proc/device-tree/model") or "").strip("\x00") or None,
            "os": os_name,
            "kernel": os.uname().release,
            "uptime": human_uptime(int(float(up[0]))) if up else None,
            "cpu": cpu_pct, "load1": load1, "cores": os.cpu_count(),
            "mhz": round(freq / 1000) if freq else None,
            "mem": {"used": mem[0], "total": mem[1], "pct": round(mem[0] / mem[1] * 100, 1)} if mem else None,
            "disk": disk,
            "temp": round(temp / 1000) if temp is not None else None,
            "fan": read_fan(),
            "power": {"level": power[0], "text": power[1]} if power else None,
            "network": read_network(),
        }

    def demo_data(self):
        gib = 1024 ** 3
        return {
            "hostname": "homelab-pi", "model": "Raspberry Pi 5 Model B Rev 1.0",
            "os": "Debian GNU/Linux 12 (bookworm)", "kernel": "6.6.51+rpt-rpi-2712", "uptime": "12 days",
            "cpu": round(random.uniform(6, 14), 1), "load1": 0.21, "cores": 4, "mhz": 1500,
            "mem": {"used": 0.9 * gib, "total": 4 * gib, "pct": 22.5},
            "disk": {"used": 6.2 * gib, "total": 58 * gib, "pct": 10.7},
            "temp": 52, "fan": {"rpm": 2380, "pct": 35},
            "power": {"level": "ok", "text": "OK"},
            "network": [
                {"name": "eth0", "kind": "ethernet", "up": True, "ip": "192.168.1.50", "speed": 1000},
                {"name": "wlan0", "kind": "wifi", "up": True, "ip": "192.168.1.51",
                 "ssid": "HomeNet", "signal": -54, "bitrate": 433},
            ],
        }


# --------------------------------------------------------------------------- Wi-Fi setup

class ActionError(Exception):
    """A refused or failed action, carrying the HTTP status and a message that is safe to show."""
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


WifiError = ActionError   # the name the Wi-Fi code and its tests use


def split_nmcli(line):
    """Split one line of `nmcli -t` output on unescaped ':' and unescape each field."""
    fields, cur, i = [], [], 0
    while i < len(line):
        c = line[i]
        if c == "\\" and i + 1 < len(line):
            cur.append(line[i + 1])
            i += 2
            continue
        if c == ":":
            fields.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    fields.append("".join(cur))
    return fields


def parse_wifi_list(text):
    """Nearby networks from `nmcli -t -f IN-USE,SSID,SIGNAL,SECURITY device wifi list`.

    One entry per SSID (strongest signal), the connected one first, then by signal.
    """
    best = {}
    for line in (text or "").splitlines():
        f = split_nmcli(line)
        if len(f) < 4 or not f[1]:
            continue
        try:
            signal = int(f[2])
        except ValueError:
            signal = 0
        net = best.setdefault(f[1], {"ssid": f[1], "signal": signal,
                                     "secure": f[3].strip() not in ("", "--"), "active": False})
        net["signal"] = max(net["signal"], signal)
        net["active"] = net["active"] or f[0].strip() == "*"
    return sorted(best.values(), key=lambda n: (not n["active"], -n["signal"], n["ssid"].lower()))


def validate_wifi_input(ssid, password):
    """An error message, or None when the network name and password look usable."""
    if not isinstance(ssid, str) or not 1 <= len(ssid.encode("utf-8")) <= 32 or any(ord(c) < 32 for c in ssid):
        return "The network name must be 1 to 32 characters."
    if ssid.startswith("-"):
        return "Network names starting with '-' are not supported here."
    if not isinstance(password, str) or any(ord(c) < 32 for c in password) \
            or (password and not 8 <= len(password) <= 63):
        return "The password must be 8 to 63 characters (empty for an open network)."
    return None


def clean_nmcli_error(text):
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return re.sub(r"^Error:\s*", "", line)[:160]
    return "Could not connect."


DEMO_WIFI = [
    {"ssid": "HomeNet", "signal": 82, "secure": True, "active": True},
    {"ssid": "Neighbour 5G", "signal": 61, "secure": True, "active": False},
    {"ssid": "CafeGuest", "signal": 38, "secure": False, "active": False},
]


class PinGate:
    """The PIN in config.json, checked in constant time, with a lockout after repeated wrong tries.

    One gate guards every on-screen action (Wi-Fi changes and the power menu), so the lockout is shared
    and guesses can't be spread across features. With no PIN set, everything behind it stays off.
    """
    MAX_FAILS = 5
    LOCK_S = 60
    WINDOW_S = 300

    def __init__(self, get_cfg, demo=False):
        self.get_cfg = get_cfg
        self.demo = demo
        self.lock = threading.Lock()
        self.fails = []
        self.locked_until = 0.0

    def configured_pin(self):
        cfg = self.get_cfg()
        pin = str(cfg.get("pin") or cfg.get("wifi_pin") or "")
        if self.demo and not pin:
            return "1234"
        return pin if len(pin) >= 4 else ""

    def check(self, pin):
        now = time.time()
        with self.lock:
            if now < self.locked_until:
                raise ActionError(429, f"Too many wrong PINs. Try again in {int(self.locked_until - now) + 1} s.")
            self.fails = [t for t in self.fails if now - t < self.WINDOW_S]
            ok = isinstance(pin, str) and hmac.compare_digest(pin.encode(), self.configured_pin().encode())
            if not ok:
                self.fails.append(now)
                log.warning("Wrong PIN entered on the screen (%d of %d)", len(self.fails), self.MAX_FAILS)
                if len(self.fails) >= self.MAX_FAILS:
                    self.locked_until = now + self.LOCK_S
                    self.fails = []
                raise ActionError(403, "Wrong PIN.")
            self.fails = []


class WifiSetup:
    """Scan and join Wi-Fi networks through NetworkManager (nmcli).

    Off until a PIN is set in the config. Every action needs that PIN, wrong
    guesses lock the feature for a minute, and the HTTP layer only accepts requests
    coming from the Pi itself. The password is handed to nmcli on stdin, never on
    the command line, and is never logged.
    """
    MAX_FAILS = PinGate.MAX_FAILS
    SCAN_TIMEOUT_S = 20

    def __init__(self, get_cfg, demo=False, gate=None):
        self.demo = demo
        self.gate = gate or PinGate(get_cfg, demo)
        self.busy = threading.Lock()

    def configured_pin(self):
        return self.gate.configured_pin()

    def status(self):
        """'ready', 'no_pin' (not configured) or 'no_nmcli' (NetworkManager missing)."""
        if not self.configured_pin():
            return "no_pin"
        if not self.demo and not shutil.which("nmcli"):
            return "no_nmcli"
        return "ready"

    def authorise(self, pin):
        state = self.status()
        if state != "ready":
            raise ActionError(503, "Wi-Fi setup is not enabled." if state == "no_pin"
                              else "NetworkManager (nmcli) is not installed.")
        self.gate.check(pin)

    def scan(self):
        if self.demo:
            return DEMO_WIFI
        # A fresh scan can take several seconds. If it is refused or times out, fall back to
        # NetworkManager's cached list, and report nmcli's own message if that fails too.
        problem = "Scanning timed out."
        for rescan in ("yes", "no"):
            try:
                r = subprocess.run(["nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "device", "wifi", "list",
                                    "--rescan", rescan], capture_output=True, text=True, timeout=self.SCAN_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                continue
            except OSError:
                raise WifiError(503, "NetworkManager (nmcli) is not available.")
            if r.returncode == 0:
                return parse_wifi_list(r.stdout)
            problem = clean_nmcli_error(r.stderr or r.stdout)
        raise WifiError(502, problem)

    def connect(self, ssid, password, hidden=False):
        error = validate_wifi_input(ssid, password)
        if error:
            raise WifiError(400, error)
        if not self.busy.acquire(blocking=False):
            raise WifiError(409, "Another connection attempt is in progress.")
        try:
            if self.demo:
                time.sleep(0.5)
                return
            cmd = ["nmcli", "--ask", "device", "wifi", "connect", ssid] + (["hidden", "yes"] if hidden else [])
            try:
                r = subprocess.run(cmd, input=password + "\n" if password else "",
                                   capture_output=True, text=True, timeout=45)
            except subprocess.TimeoutExpired:
                raise WifiError(504, "Timed out while connecting.")
            except OSError:
                raise WifiError(503, "NetworkManager (nmcli) is not available.")
            if r.returncode != 0:
                raise WifiError(502, clean_nmcli_error(r.stderr or r.stdout))
        finally:
            self.busy.release()


class PowerControl:
    """Reboot, shut down or restart the dashboard from the screen, behind the same PIN as Wi-Fi setup.

    The commands are fixed: nothing from a request is ever placed on a command line. They run as the
    dashboard's own user, so they only work once install.sh --power has let that user do exactly these
    things through polkit.
    """
    COMMANDS = {
        "reboot": ["systemctl", "reboot"],
        "shutdown": ["systemctl", "poweroff"],
        "restart": ["systemctl", "restart", "--no-block", "homelab-dashboard.service"],
    }

    def __init__(self, gate, demo=False):
        self.gate = gate
        self.demo = demo

    def status(self):
        """'ready', 'no_pin' or 'no_systemctl'."""
        if not self.gate.configured_pin():
            return "no_pin"
        if not self.demo and not shutil.which("systemctl"):
            return "no_systemctl"
        return "ready"

    def run(self, pin, action):
        state = self.status()
        if state != "ready":
            raise ActionError(503, "The power menu is not enabled." if state == "no_pin"
                              else "systemctl is not available.")
        self.gate.check(pin)
        if action == "check":
            return
        if not isinstance(action, str) or action not in self.COMMANDS:
            raise ActionError(400, "Unknown action.")
        cmd = self.COMMANDS[action]
        if self.demo:
            log.info("demo: would run %s", " ".join(cmd))
            return
        log.warning("Power action '%s' requested from the screen", action)
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        except subprocess.TimeoutExpired:
            raise ActionError(504, "The request timed out.")
        except OSError:
            raise ActionError(503, "systemctl is not available.")
        if r.returncode != 0:
            msg = clean_nmcli_error(r.stderr or r.stdout)
            if any(w in msg.lower() for w in ("authentication", "access denied", "not authorized", "permission")):
                msg = "Not allowed yet. Re-run install.sh pi with --power on the Pi."
            raise ActionError(502, msg)


class ScreenState:
    """Whether the dashboard page says the screen is asleep.

    The page reports its state when it changes and every 30 s. If the reports stop
    (browser crashed, page closed) the answer falls back to "on", so a failure can
    never leave the display dark.
    """
    STALE_S = 90

    def __init__(self):
        self.lock = threading.Lock()
        self.state = "on"
        self.stamp = time.time()

    def report(self, state):
        with self.lock:
            self.state = state
            self.stamp = time.time()

    def current(self):
        with self.lock:
            fresh = time.time() - self.stamp < self.STALE_S
            return self.state if fresh else "on"


class Collector:
    def __init__(self, cfg, demo=False, config_path=None):
        self.cfg = cfg
        self.demo = demo
        self.config_path = config_path
        self.config_mtime = os.path.getmtime(config_path) if config_path else None
        self.config_error = None
        self.streaks = {}
        self.pi = PiInfo(lambda: self.cfg["thresholds"], demo=demo)
        gate = PinGate(lambda: self.cfg, demo=demo)
        self.wifi = WifiSetup(lambda: self.cfg, demo=demo, gate=gate)
        self.power = PowerControl(gate, demo=demo)
        self.screen = ScreenState()
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
        self.pi.sample_cpu()
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
            "screen": cfg["screen"],
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

        def send_json(self, obj, status=200):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            route = self.path.split("?")[0]
            if route == "/api/state":
                return self.send_json(collector.snapshot())
            if route == "/api/pi":
                return self.send_json({**collector.pi.snapshot(), "wifi_setup": collector.wifi.status(),
                                       "power_setup": collector.power.status()})
            if route == "/api/screen":
                return self.send_json({"state": collector.screen.current(), "method": collector.cfg["screen"]["method"]})
            if self.path == "/healthz":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")
                return
            return super().do_GET()

        def local_request(self):
            """True only for a same-origin request from the Pi's own browser (no LAN or DNS-rebinding callers)."""
            host = self.headers.get("Host") or ""
            name = host.rsplit(":", 1)[0].strip("[]").lower()
            origin = self.headers.get("Origin")
            return (self.client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1")
                    and name in ("127.0.0.1", "localhost", "::1")
                    and (origin is None or origin == f"http://{host}"))

        def read_json_body(self):
            """The request's JSON object, or None when it is missing, too big, not JSON or not an object."""
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if not 0 < length <= 4096 or "application/json" not in (self.headers.get("Content-Type") or ""):
                    return None
                body = json.loads(self.rfile.read(length))
            except ValueError:
                return None
            return body if isinstance(body, dict) else None

        def do_POST(self):
            route = self.path.split("?")[0]
            if route not in ("/api/wifi/scan", "/api/wifi/connect", "/api/screen", "/api/power"):
                return self.send_json({"error": "Not found."}, 404)
            if not self.local_request():
                return self.send_json({"error": "This only works on the Pi's own screen."}, 403)
            body = self.read_json_body()
            if body is None:
                return self.send_json({"error": "Bad request."}, 400)
            if route == "/api/screen":
                if body.get("state") not in ("on", "off"):
                    return self.send_json({"error": "state must be on or off."}, 400)
                collector.screen.report(body["state"])
                return self.send_json({"ok": True})
            if route == "/api/power":
                try:
                    collector.power.run(body.get("pin"), body.get("action"))
                    return self.send_json({"ok": True})
                except ActionError as e:
                    return self.send_json({"error": e.message}, e.status)
            wifi = collector.wifi
            try:
                wifi.authorise(body.get("pin"))
                if route.endswith("/scan"):
                    return self.send_json({"networks": wifi.scan()})
                wifi.connect(body.get("ssid"), body.get("password") or "", body.get("hidden") is True)
                return self.send_json({"ok": True})
            except WifiError as e:
                return self.send_json({"error": e.message}, e.status)

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
