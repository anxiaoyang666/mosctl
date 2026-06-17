from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "remote-root" / "etc" / "mosdns" / "manager" / "app.py"
INDEX = ROOT / "remote-root" / "etc" / "mosdns" / "manager" / "templates" / "index.html"


def app_source():
    return APP.read_text(encoding="utf-8")


def index_source():
    return INDEX.read_text(encoding="utf-8")


class DeviceTrafficRefinementContractTest(unittest.TestCase):
    def test_backend_reports_domain_route_and_traffic_diagnostics(self):
        text = app_source()

        self.assertIn("def classify_device_domain", text)
        self.assertIn("route", text)
        self.assertIn("traffic_status", text)
        self.assertIn("matched_connections", text)
        self.assertIn("controller", text)
        self.assertIn("def read_mihomo_settings", text)
        self.assertIn("def write_mihomo_settings", text)
        self.assertIn('@app.route("/api/mihomo-settings"', text)
        self.assertIn('@app.route("/api/mihomo-test"', text)
        self.assertIn("def connection_target_domain", text)
        self.assertIn("def apply_domain_attributed_traffic", text)
        self.assertIn("attributed_connections", text)
        self.assertIn("traffic_estimated", text)

    def test_ui_collapses_domain_details_and_shows_traffic_status(self):
        text = index_source()

        self.assertIn("document.createElement('details')", text)
        self.assertIn("deviceDomainOpenState", text)
        self.assertIn("details.open", text)
        self.assertIn("domain-group", text)
        self.assertIn("域名明细", text)
        self.assertIn("国内", text)
        self.assertIn("国外", text)
        self.assertIn("trafficStatus", text)
        self.assertIn("mihomo 控制器", text)
        self.assertIn("未看到实时外网流量", text)
        self.assertIn("mihomoController", text)
        self.assertIn("saveMihomoSettings", text)
        self.assertIn("testMihomoController", text)
        self.assertIn("clearMihomoSecret", text)
        self.assertIn("留空保留已保存密钥", text)
        self.assertIn("域名归因", text)
        self.assertIn("估算", text)


if __name__ == "__main__":
    unittest.main()
