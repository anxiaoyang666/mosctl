from pathlib import Path
import ast
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "remote-root" / "etc" / "mosdns" / "manager" / "app.py"
INDEX = ROOT / "remote-root" / "etc" / "mosdns" / "manager" / "templates" / "index.html"


def app_source():
    return APP.read_text(encoding="utf-8")


def app_tree():
    return ast.parse(app_source())


def index_source():
    return INDEX.read_text(encoding="utf-8")


class PanelUpgradeContractTest(unittest.TestCase):
    def test_panel_upgrade_excludes_local_state(self):
        text = app_source()

        self.assertIn("PANEL_UPGRADE_EXCLUDES", text)
        self.assertIn(".env", text)
        self.assertIn("config.yaml", text)
        self.assertIn("/etc/mosdns/rules", text)

    def test_panel_upgrade_endpoint_exists(self):
        text = app_source()

        self.assertIn("PANEL_VERSION", text)
        self.assertIn("def panel_version_tuple", text)
        self.assertIn("def parse_panel_version", text)
        self.assertIn("def upgrade_mosctl_panel", text)
        self.assertIn('action == "upgrade_panel"', text)
        self.assertIn("当前已是最新版本", text)

    def test_panel_upgrade_schedules_web_restart_after_response(self):
        text = app_source()

        self.assertIn("schedule_web_restart", text)
        self.assertIn("systemctl restart mosdns-web", text)

    def test_app_remains_valid_python(self):
        compile(app_source(), str(APP), "exec")

    def test_panel_upgrade_ui_exists(self):
        text = index_source()

        self.assertIn("panelRepo", text)
        self.assertIn("loadPanelUpgradeSource", text)
        self.assertIn("confirmUpgradePanel", text)
        self.assertIn("handlePanelVersionClick", text)
        self.assertIn("setTimeout(() => location.reload(), 5000)", text)
        self.assertIn("upgrade_panel", text)

    def test_panel_version_rolls_forward_for_upgrade_detection(self):
        text = app_source()
        match = re.search(r'(?m)^PANEL_VERSION = "(\d+)\.(\d+)\.(\d+)"$', text)

        self.assertIsNotNone(match)
        version = tuple(int(part) for part in match.groups())
        self.assertGreaterEqual(version, (0, 3, 1))

    def test_sidebar_version_label_does_not_repeat_product_name(self):
        text = index_source()

        self.assertNotIn("'Mosctl v' + (res.panel_version", text)
        self.assertNotIn("'Mosctl v' + (res.current_version", text)
        self.assertIn("'v' + (res.panel_version || '--')", text)
        self.assertIn("'v' + (res.current_version || '--') + ' 可更新'", text)


if __name__ == "__main__":
    unittest.main()
