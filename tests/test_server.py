import copy
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pi"))
import server  # noqa: E402


def cfg(**over):
    c = copy.deepcopy(server.DEFAULTS)
    c["hosts"] = [{"name": "a", "address": "10.0.0.1"}]
    c.update(over)
    return c


def host(**over):
    h = {"name": "a", "address": "10.0.0.1", "online": True, "cpu": 10.0, "mem_pct": 20.0,
         "disks": [{"mount": "/", "pct": 30.0}], "temp": 40, "containers": []}
    h.update(over)
    return h


def ctr(name, status="running"):
    return {"name": name, "status": status}


class Formatting(unittest.TestCase):
    def test_parse_uptime(self):
        self.assertEqual(server.parse_uptime("4:05:12"), 4 * 3600 + 5 * 60 + 12)
        self.assertEqual(server.parse_uptime("23 days, 4:05:12"), 23 * 86400 + 4 * 3600 + 5 * 60 + 12)
        self.assertEqual(server.parse_uptime("1 day, 0:00:01"), 86401)
        self.assertIsNone(server.parse_uptime(None))

    def test_human_uptime(self):
        self.assertEqual(server.human_uptime(None), "")
        self.assertEqual(server.human_uptime(30), "1 min")
        self.assertEqual(server.human_uptime(3 * 3600), "3 hours")
        self.assertEqual(server.human_uptime(86400), "1 day")
        self.assertEqual(server.human_uptime(5 * 86400), "5 days")

    def test_human_bytes(self):
        self.assertEqual(server.human_bytes(None), "—")
        self.assertEqual(server.human_bytes(512), "512 B")
        self.assertEqual(server.human_bytes(5 * 1024 ** 2), "5 MB")
        self.assertEqual(server.human_bytes(3 * 1024 ** 3), "3.0 GB")

    def test_short_uptime(self):
        self.assertEqual(server.short_uptime("2 days"), "2d")
        self.assertEqual(server.short_uptime("an hour"), "1h")
        self.assertEqual(server.short_uptime("3 minutes"), "3m")
        self.assertEqual(server.short_uptime(""), "—")


class CpuTemp(unittest.TestCase):
    def s(self, label, value, type_="temperature_core"):
        return {"label": label, "value": value, "type": type_}

    def test_none_without_sensors(self):
        self.assertIsNone(server.cpu_temp(None))
        self.assertIsNone(server.cpu_temp([]))
        self.assertIsNone(server.cpu_temp([self.s("x", 0)]))

    def test_raspberry_pi(self):
        self.assertEqual(server.cpu_temp([self.s("cpu_thermal 0", 52.6)]), 53)

    def test_prefers_cpu_over_nvme_and_wifi(self):
        sensors = [self.s("Package id 0", 55), self.s("Core 1", 58), self.s("Composite", 71), self.s("iwlwifi_1", 66)]
        self.assertEqual(server.cpu_temp(sensors), 58)

    def test_falls_back_to_any_temperature(self):
        self.assertEqual(server.cpu_temp([self.s("Composite", 40)]), 40)

    def test_ignores_non_temperature_types(self):
        self.assertIsNone(server.cpu_temp([self.s("fan1", 3000, "fan_speed")]))


class Evaluate(unittest.TestCase):
    def test_healthy(self):
        h = server.evaluate(host(), cfg())
        self.assertEqual((h["status"], h["alerts"]), ("ok", []))

    def test_thresholds(self):
        h = server.evaluate(host(cpu=95, mem_pct=91, temp=85, disks=[{"mount": "/data", "pct": 90}]), cfg())
        self.assertEqual(h["status"], "warn")
        self.assertEqual({a["key"] for a in h["alerts"]}, {"cpu", "mem", "temp", "disk:/data"})

    def test_offline(self):
        h = server.evaluate(host(online=False), cfg())
        self.assertEqual(h["status"], "down")
        self.assertEqual(h["alerts"][0]["level"], "down")

    def test_stopped_container_alerts(self):
        h = server.evaluate(host(containers=[ctr("web"), ctr("job", "exited")]), cfg())
        self.assertEqual([a["text"] for a in h["alerts"]], ["job is exited"])

    def test_expected_stopped_global_and_per_host(self):
        containers = [ctr("job", "exited"), ctr("other", "exited")]
        self.assertEqual(len(server.evaluate(host(containers=containers), cfg())["alerts"]), 2)
        g = server.evaluate(host(containers=containers), cfg(expected_stopped=["job"]))
        self.assertEqual([a["text"] for a in g["alerts"]], ["other is exited"])
        per = server.evaluate(host(containers=containers), cfg(), {"expected_stopped": ["other"]})
        self.assertEqual([a["text"] for a in per["alerts"]], ["job is exited"])

    def test_per_host_setting_does_not_leak_to_other_hosts(self):
        containers = [ctr("job", "exited")]
        server.evaluate(host(containers=containers), cfg(), {"expected_stopped": ["job"]})
        again = server.evaluate(host(containers=containers), cfg(), {"name": "b"})
        self.assertEqual(len(again["alerts"]), 1)


class Debounce(unittest.TestCase):
    def poll(self, streaks, confirm=2, **over):
        hosts = [server.evaluate(host(**over), cfg())]
        return server.debounce(hosts, streaks, confirm)[0]

    def test_needs_consecutive_samples(self):
        streaks = {}
        first = self.poll(streaks, cpu=95)
        self.assertEqual((first["status"], first["alerts"]), ("ok", []))
        second = self.poll(streaks, cpu=96)
        self.assertEqual(second["status"], "warn")
        self.assertEqual(len(second["alerts"]), 1)

    def test_flapping_never_alerts(self):
        streaks = {}
        for cpu in (95, 10, 95, 10, 95):
            self.assertEqual(self.poll(streaks, cpu=cpu)["alerts"], [])

    def test_recovery_resets_streak(self):
        streaks = {}
        self.poll(streaks, cpu=95)
        self.poll(streaks, cpu=95)
        self.poll(streaks, cpu=10)
        self.assertEqual(streaks, {})

    def test_confirm_one_is_immediate(self):
        self.assertEqual(len(self.poll({}, confirm=1, cpu=95)["alerts"]), 1)

    def test_unconfirmed_offline_is_warn_then_down(self):
        streaks = {}
        self.assertEqual(self.poll(streaks, online=False)["status"], "warn")
        self.assertEqual(self.poll(streaks, online=False)["status"], "down")


class Collect(unittest.TestCase):
    PAYLOAD = {
        "cpu": {"total": 12.34, "cpucore": 4},
        "mem": {"used": 2 * 1024 ** 3, "total": 8 * 1024 ** 3, "percent": 25.0},
        "fs": [
            {"fs_type": "ext4", "mnt_point": "/", "device_name": "sda1", "size": 100, "used": 40, "percent": 40.0},
            {"fs_type": "ext4", "mnt_point": "/etc/hosts", "device_name": "sda1", "size": 100, "used": 40, "percent": 40.0},
            {"fs_type": "tmpfs", "mnt_point": "/run", "device_name": "tmpfs", "size": 10, "percent": 1.0},
        ],
        "uptime": "2 days, 1:00:00",
        "sensors": [{"type": "temperature_core", "value": 55}, {"type": "temperature_core", "value": 61}],
        "containers": [
            {"name": "glances-agent", "status": "running"},
            {"name": "web", "status": "running", "cpu_percent": 1.5, "memory_usage": 50 * 1024 ** 2, "uptime": "2 days"},
            {"name": "db", "status": "exited"},
        ],
        "load": {"min1": 0.4},
    }

    def fake(self, payload):
        def fetch(url, timeout):
            ep = url.rsplit("/", 1)[1]
            if payload.get(ep) is KeyError:
                raise OSError("boom")
            return payload.get(ep)
        return fetch

    def collect(self, payload):
        with mock.patch.object(server, "fetch_json", self.fake(payload)):
            return server.collect_host({"name": "a", "address": "10.0.0.1"}, cfg())

    def test_normalises(self):
        h = self.collect(self.PAYLOAD)
        self.assertTrue(h["online"])
        self.assertEqual((h["cpu"], h["temp"], h["uptime"]), (12.3, 61, "2 days"))
        self.assertEqual(len(h["disks"]), 1)  # bind-mount dup and tmpfs dropped
        self.assertEqual([c["name"] for c in h["containers"]], ["db", "web"])  # agent hidden, problems first
        self.assertEqual((h["running"], h["total"]), (1, 2))

    def test_optional_plugin_failure_keeps_host_online(self):
        h = self.collect({**self.PAYLOAD, "sensors": KeyError})
        self.assertTrue(h["online"])
        self.assertIsNone(h["temp"])

    def test_core_plugin_failure_marks_offline(self):
        h = self.collect({**self.PAYLOAD, "cpu": KeyError})
        self.assertFalse(h["online"])
        self.assertIn("boom", h["error"])

    def test_disk_mounts_filter(self):
        with mock.patch.object(server, "fetch_json", self.fake(self.PAYLOAD)):
            h = server.collect_host({"name": "a", "address": "x", "disk_mounts": ["/nope"]}, cfg())
        self.assertEqual(h["disks"], [])


class Config(unittest.TestCase):
    def test_load_merges_defaults(self):
        import json, tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"hosts": [{"name": "a", "address": "1"}], "thresholds": {"cpu_warn": 50}}, f)
        try:
            c = server.load_config(f.name)
        finally:
            os.unlink(f.name)
        self.assertEqual(c["thresholds"]["cpu_warn"], 50)
        self.assertEqual(c["thresholds"]["mem_warn"], 90)
        self.assertEqual(c["alert_confirm_samples"], 2)

    def test_no_hosts_is_an_error(self):
        import json, tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({}, f)
        try:
            with self.assertRaises(SystemExit):
                server.load_config(f.name)
        finally:
            os.unlink(f.name)


class Http(unittest.TestCase):
    def test_security_headers_and_endpoints(self):
        import threading
        import urllib.request
        c = server.Collector(cfg(), demo=True)
        c.poll_once()
        httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(c))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            base = f"http://127.0.0.1:{httpd.server_address[1]}"
            for path in ("/", "/api/state", "/healthz"):
                with urllib.request.urlopen(base + path) as r:
                    self.assertEqual(r.status, 200, path)
                    self.assertIn("frame-ancestors 'none'", r.headers["Content-Security-Policy"], path)
                    self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff", path)
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(base + "/../server.py")
            self.assertEqual(e.exception.code, 404)
            with self.assertRaises(urllib.error.HTTPError):
                urllib.request.urlopen(base + "/%2e%2e/server.py")
        finally:
            httpd.shutdown()
            httpd.server_close()


class PiInfoParsing(unittest.TestCase):
    def test_cpu_stat(self):
        self.assertEqual(server.parse_cpu_stat("cpu  100 0 50 800 50 0 0 0 0 0\ncpu0 1 2"), (150, 1000))
        self.assertIsNone(server.parse_cpu_stat("nonsense"))
        self.assertIsNone(server.parse_cpu_stat(None))

    def test_meminfo(self):
        text = "MemTotal:        4000000 kB\nMemFree: 1 kB\nMemAvailable:    3000000 kB\n"
        self.assertEqual(server.parse_meminfo(text), (1000000 * 1024, 4000000 * 1024))
        self.assertIsNone(server.parse_meminfo("MemTotal: 1 kB"))

    def test_ip_addr(self):
        text = ("2: eth0    inet 10.1.2.2/24 brd 10.1.2.255 scope global eth0\\       valid_lft forever\n"
                "3: wlan0    inet 10.1.2.3/24 brd 10.1.2.255 scope global wlan0\n"
                "3: wlan0    inet 169.254.1.1/16 scope link wlan0\n")
        self.assertEqual(server.parse_ip_addr(text), {"eth0": "10.1.2.2", "wlan0": "10.1.2.3"})

    def test_iw_link(self):
        text = "Connected to aa:bb (on wlan0)\n\tSSID: Home Net\n\tsignal: -52 dBm\n\ttx bitrate: 433.3 MBit/s\n"
        self.assertEqual(server.parse_iw_link(text), {"ssid": "Home Net", "signal": -52, "bitrate": 433})
        self.assertIsNone(server.parse_iw_link("Not connected."))
        self.assertIsNone(server.parse_iw_link(None))

    def test_proc_wireless(self):
        text = ("Inter-| sta-|   Quality        |   Discarded packets\n face | tus | link level noise |\n"
                " wlan0: 0000   70.  -40.  -256        0      0\n")
        self.assertEqual(server.parse_proc_wireless(text), {"wlan0": -40})

    def test_power_status(self):
        self.assertIsNone(server.power_status(None))
        self.assertEqual(server.power_status(0), ("ok", "OK"))
        self.assertEqual(server.power_status(0x50000)[0], "note")
        self.assertEqual(server.power_status(0x50005), ("warn", "Now: under-voltage, throttled"))

    def test_demo_snapshot_shape(self):
        info = server.PiInfo(lambda: {"cpu_warn": 90}, demo=True).snapshot()
        self.assertEqual(info["thresholds"], {"cpu_warn": 90})
        self.assertEqual({n["kind"] for n in info["network"]}, {"ethernet", "wifi"})


class WifiSetup(unittest.TestCase):
    def make(self, pin="4321"):
        return server.WifiSetup(lambda: {"wifi_pin": pin}, demo=True)

    def test_nmcli_split_and_list(self):
        self.assertEqual(server.split_nmcli(r"*:Home\:Net:82:WPA2"), ["*", "Home:Net", "82", "WPA2"])
        text = "*:Home:82:WPA2\n :Home:60:WPA2\n :Cafe:38:--\n ::50:WPA2\n"
        nets = server.parse_wifi_list(text)
        self.assertEqual([(n["ssid"], n["signal"], n["secure"], n["active"]) for n in nets],
                         [("Home", 82, True, True), ("Cafe", 38, False, False)])

    def test_validation(self):
        v = server.validate_wifi_input
        self.assertIsNone(v("Home", "password1"))
        self.assertIsNone(v("Open", ""))
        self.assertIn("1 to 32", v("", "password1"))
        self.assertIn("1 to 32", v("x" * 33, "password1"))
        self.assertIn("-", v("-evil", "password1"))
        self.assertIn("8 to 63", v("Home", "short"))
        self.assertIn("8 to 63", v("Home", "password1\nsecond line"))

    def test_disabled_without_pin(self):
        for pin in ("", "123", None):
            ws = server.WifiSetup(lambda: {"wifi_pin": pin}, demo=False)
            self.assertEqual(ws.status(), "no_pin")
            with self.assertRaises(server.WifiError) as e:
                ws.authorise("1234")
            self.assertEqual(e.exception.status, 503)

    def test_demo_mode_defaults_to_pin_1234(self):
        ws = server.WifiSetup(lambda: {}, demo=True)
        self.assertEqual(ws.status(), "ready")
        ws.authorise("1234")

    def test_pin_check_and_lockout(self):
        ws = self.make()
        ws.authorise("4321")
        for _ in range(ws.MAX_FAILS):
            with self.assertRaises(server.WifiError) as e:
                ws.authorise("0000")
        self.assertEqual(e.exception.status, 403)
        with self.assertRaises(server.WifiError) as e:
            ws.authorise("4321")  # even the right PIN is refused while locked
        self.assertEqual(e.exception.status, 429)

    def test_non_string_pin_rejected(self):
        with self.assertRaises(server.WifiError):
            self.make().authorise(4321)

    def test_connect_runs_nmcli_with_password_on_stdin_only(self):
        ws = server.WifiSetup(lambda: {"wifi_pin": "4321"}, demo=False)
        done = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(server.subprocess, "run", return_value=done) as run, \
                mock.patch.object(server.shutil, "which", return_value="/usr/bin/nmcli"):
            ws.connect("Home Net", "s3cret-pass", hidden=True)
        cmd = run.call_args.args[0]
        self.assertEqual(cmd, ["nmcli", "--ask", "device", "wifi", "connect", "Home Net", "hidden", "yes"])
        self.assertNotIn("s3cret-pass", " ".join(cmd))
        self.assertEqual(run.call_args.kwargs["input"], "s3cret-pass\n")

    def real(self):
        return server.WifiSetup(lambda: {"wifi_pin": "4321"}, demo=False)

    def test_scan_waits_for_a_slow_rescan(self):
        ok = mock.Mock(returncode=0, stdout="*:Home:82:WPA2\n", stderr="")
        with mock.patch.object(server.subprocess, "run", return_value=ok) as run:
            nets = self.real().scan()
        self.assertEqual(nets[0]["ssid"], "Home")
        self.assertGreaterEqual(run.call_args.kwargs["timeout"], 15)

    def test_scan_falls_back_to_cached_list_when_rescan_times_out(self):
        import subprocess
        ok = mock.Mock(returncode=0, stdout=":Cafe:40:--\n", stderr="")
        effects = [subprocess.TimeoutExpired("nmcli", 20), ok]
        with mock.patch.object(server.subprocess, "run", side_effect=effects) as run:
            nets = self.real().scan()
        self.assertEqual([n["ssid"] for n in nets], ["Cafe"])
        self.assertEqual([c.args[0][-1] for c in run.call_args_list], ["yes", "no"])

    def test_scan_reports_nmcli_error(self):
        bad = mock.Mock(returncode=1, stdout="", stderr="Error: Not authorized to control networking.\n")
        with mock.patch.object(server.subprocess, "run", return_value=bad):
            with self.assertRaises(server.WifiError) as e:
                self.real().scan()
        self.assertEqual(e.exception.message, "Not authorized to control networking.")

    def test_scan_timeout_and_missing_nmcli(self):
        import subprocess
        with mock.patch.object(server.subprocess, "run", side_effect=subprocess.TimeoutExpired("nmcli", 20)):
            with self.assertRaises(server.WifiError) as e:
                self.real().scan()
        self.assertEqual(e.exception.message, "Scanning timed out.")
        with mock.patch.object(server.subprocess, "run", side_effect=FileNotFoundError):
            with self.assertRaises(server.WifiError) as e:
                self.real().scan()
        self.assertEqual(e.exception.status, 503)

    def test_connect_error_is_cleaned(self):
        ws = server.WifiSetup(lambda: {"wifi_pin": "4321"}, demo=False)
        bad = mock.Mock(returncode=4, stdout="", stderr="Error: Connection activation failed: Secrets were required\n")
        with mock.patch.object(server.subprocess, "run", return_value=bad):
            with self.assertRaises(server.WifiError) as e:
                ws.connect("Home", "wrongpass1")
        self.assertEqual(e.exception.message, "Connection activation failed: Secrets were required")


class WifiHttp(unittest.TestCase):
    def post(self, base, path, body, headers=None, raw=None):
        import json
        import urllib.error
        import urllib.request
        data = raw if raw is not None else json.dumps(body).encode()
        req = urllib.request.Request(base + path, data=data, method="POST",
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def setUp(self):
        import threading
        self.collector = server.Collector(cfg(wifi_pin="4321"), demo=True)
        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(self.collector))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def test_scan_and_connect_with_pin(self):
        status, body = self.post(self.base, "/api/wifi/scan", {"pin": "4321"})
        self.assertEqual(status, 200)
        self.assertEqual(body["networks"][0]["ssid"], "HomeNet")
        status, body = self.post(self.base, "/api/wifi/connect", {"pin": "4321", "ssid": "HomeNet", "password": "password123"})
        self.assertEqual((status, body), (200, {"ok": True}))

    def test_wrong_pin_and_bad_input(self):
        self.assertEqual(self.post(self.base, "/api/wifi/scan", {"pin": "0000"})[0], 403)
        self.assertEqual(self.post(self.base, "/api/wifi/scan", {})[0], 403)
        self.assertEqual(self.post(self.base, "/api/wifi/connect", {"pin": "4321", "ssid": "-x", "password": ""})[0], 400)
        self.assertEqual(self.post(self.base, "/api/wifi/scan", None, raw=b"not json")[0], 400)

    def test_rejects_foreign_host_and_origin(self):
        evil_host = self.post(self.base, "/api/wifi/scan", {"pin": "4321"}, headers={"Host": "attacker.example"})
        self.assertEqual(evil_host[0], 403)
        evil_origin = self.post(self.base, "/api/wifi/scan", {"pin": "4321"}, headers={"Origin": "http://attacker.example"})
        self.assertEqual(evil_origin[0], 403)

    def test_requires_json_content_type(self):
        import urllib.error
        import urllib.request
        req = urllib.request.Request(self.base + "/api/wifi/scan", data=b'{"pin":"4321"}', method="POST",
                                     headers={"Content-Type": "text/plain"})
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req)
        self.assertEqual(e.exception.code, 400)

    def test_pi_endpoint_reports_setup_state(self):
        import json
        import urllib.request
        with urllib.request.urlopen(self.base + "/api/pi") as r:
            data = json.loads(r.read())
        self.assertEqual(data["wifi_setup"], "ready")
        self.assertIn("network", data)

    def test_unknown_post_route(self):
        self.assertEqual(self.post(self.base, "/api/state", {})[0], 404)


class PollOnce(unittest.TestCase):
    def test_demo_poll_strips_internal_keys_and_debounces(self):
        c = server.Collector(cfg(hosts=[{"name": "n", "address": "x"}] * 3), demo=True)
        c.poll_once()
        first = c.snapshot()
        self.assertEqual(first["alerts"], [])  # demo problems are not yet confirmed
        c.poll_once()
        second = c.snapshot()
        self.assertTrue(second["alerts"])
        self.assertTrue(all("key" not in a for a in second["alerts"]))
        self.assertTrue(all("key" not in a for h in second["hosts"] for a in h["alerts"]))


if __name__ == "__main__":
    unittest.main()
