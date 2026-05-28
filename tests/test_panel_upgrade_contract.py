from pathlib import Path
import ast
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

        self.assertIn("def upgrade_mosctl_panel", text)
        self.assertIn('action == "upgrade_panel"', text)

    def test_panel_upgrade_schedules_web_restart_after_response(self):
        text = app_source()

        self.assertIn("schedule_web_restart", text)
        self.assertIn("systemctl restart mosdns-web", text)

    def test_app_remains_valid_python(self):
        compile(app_source(), str(APP), "exec")

    def test_panel_upgrade_ui_exists(self):
        text = index_source()

        self.assertIn("Mosctl 面板升级", text)
        self.assertIn("loadPanelUpgradeSource", text)
        self.assertIn("confirmUpgradePanel", text)
        self.assertIn("upgrade_panel", text)


if __name__ == "__main__":
    unittest.main()
