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
