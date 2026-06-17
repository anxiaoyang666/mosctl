from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "remote-root" / "etc" / "mosdns" / "manager" / "app.py"
INDEX = ROOT / "remote-root" / "etc" / "mosdns" / "manager" / "templates" / "index.html"


def app_source():
    return APP.read_text(encoding="utf-8")


def index_source():
    return INDEX.read_text(encoding="utf-8")


class DeviceInsightsContractTest(unittest.TestCase):
    def test_backend_tracks_device_notes_and_domain_counts(self):
        text = app_source()

        self.assertIn("DEVICE_NOTES_FILE", text)
        self.assertIn("def read_device_notes", text)
        self.assertIn("def write_device_note", text)
        self.assertIn("domains", text)
        self.assertIn("domain_count", text)
        self.assertIn("def mihomo_controller_settings", text)
        self.assertIn("def collect_mihomo_device_traffic", text)
        self.assertIn("traffic_download", text)
        self.assertIn("traffic_upload", text)
        self.assertIn("traffic_total", text)
        self.assertIn('@app.route("/api/devices/<path:device_ip>/note"', text)

    def test_ui_exposes_notes_domain_counts_and_traffic_limit(self):
        text = index_source()

        self.assertIn("保存备注", text)
        self.assertIn("域名次数", text)
        self.assertIn("外网流量", text)
        self.assertIn("formatBytes", text)
        self.assertIn("function saveDeviceNote", text)
        self.assertIn("renderDeviceDomains", text)


if __name__ == "__main__":
    unittest.main()
