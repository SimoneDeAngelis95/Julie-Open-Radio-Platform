import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from . import test_system_status
from .host_metrics import read_metrics


class HostMetricsTests(SimpleTestCase):
    def test_unconfigured_does_not_read_container_counters(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertFalse(read_metrics()["available"])

    def test_host_counters_memory_and_optional_temperature(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "stat").write_text(
                "cpu  10 2 4 70 10 1 2 1 99 99\ncpu0 0\ncpu1 0\n"
            )
            (root / "meminfo").write_text("MemTotal: 1000 kB\nMemAvailable: 400 kB\n")
            (root / "uptime").write_text("90061.42 0")
            sensor = root / "sys/class/thermal/thermal_zone0"
            sensor.mkdir(parents=True)
            (sensor / "temp").write_text("52000")
            with patch.dict("os.environ", {"JULIE_HOST_METRICS_ROOT": directory}):
                data = read_metrics()
            self.assertEqual(data["cpu"], {"total": 100, "idle": 80, "cores": 2})
            self.assertEqual(data["memory"]["used"], 600 * 1024)
            self.assertEqual(data["memory"]["percent"], 60)
            self.assertEqual(data["uptime_seconds"], 90061)
            self.assertEqual(data["temperature"], 52)

    def test_missing_and_malformed_counters_are_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "stat").write_text("not cpu")
            (root / "meminfo").write_text("MemTotal: nope")
            (root / "uptime").write_text("nan")
            with patch.dict("os.environ", {"JULIE_HOST_METRICS_ROOT": directory}):
                data = read_metrics()
            self.assertFalse(data["available"])
            self.assertIsNone(data["cpu"])
            self.assertIsNone(data["memory"])
            self.assertIsNone(data["uptime_seconds"])


class HostEndpointTests(TestCase):
    setUp = test_system_status.SystemStatusTests.setUp

    def test_metrics_access_and_no_store(self):
        with patch(
            "library.system_views.read_metrics", return_value={"available": False}
        ):
            response = self.client.get("/maintenance/status/host/")
        self.assertEqual(response.json(), {"available": False})
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(self.client.post("/maintenance/status/host/").status_code, 405)
        self.user.is_superuser = False
        self.user.save()
        self.assertEqual(self.client.get("/maintenance/status/host/").status_code, 302)
        self.client.logout()
        self.assertEqual(self.client.get("/maintenance/status/host/").status_code, 302)
