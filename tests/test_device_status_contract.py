from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "remote-root" / "etc" / "mosdns" / "manager" / "app.py"
INDEX = ROOT / "remote-root" / "etc" / "mosdns" / "manager" / "templates" / "index.html"


def app_source():
    return APP.read_text(encoding="utf-8")


def index_source():
    return INDEX.read_text(encoding="utf-8")


class DeviceStatusContractTest(unittest.TestCase):
    def test_backend_collects_devices_from_dns_logs_and_neighbor_table(self):
        text = app_source()

        self.assertIn("def parse_device_log_clients", text)
        self.assertIn("def read_neighbor_table", text)
        self.assertIn("def collect_devices", text)
        self.assertIn('@app.route("/api/devices")', text)
        self.assertIn("ip", text)
        self.assertIn("mac", text)
        self.assertIn("last_seen", text)
        self.assertIn("query_count", text)
        self.assertIn("online", text)
        self.assertIn('"devices": collect_devices()', text)

    def test_ui_exposes_device_status_view(self):
        text = index_source()

        self.assertIn("设备状态", text)
        self.assertIn('id="view-devices"', text)
        self.assertIn('id="deviceList"', text)
        self.assertIn("function loadDevices", text)
        self.assertIn("renderDevices", text)
        self.assertIn("api('/api/devices', {silent: true})", text)
        self.assertIn("最近查询", text)


if __name__ == "__main__":
    unittest.main()
