from pathlib import Path
import ast
import unittest


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "remote-root" / "etc" / "mosdns" / "manager" / "app.py"
INDEX = ROOT / "remote-root" / "etc" / "mosdns" / "manager" / "templates" / "index.html"
LOGIN = ROOT / "remote-root" / "etc" / "mosdns" / "manager" / "templates" / "login.html"


def app_source():
    return APP.read_text(encoding="utf-8")


def index_source():
    return INDEX.read_text(encoding="utf-8")


def login_source():
    return LOGIN.read_text(encoding="utf-8")


class ConsoleRedesignContractTest(unittest.TestCase):
    def test_status_api_exposes_health_summary(self):
        text = app_source()
        tree = ast.parse(text)

        self.assertIn("def service_health_summary", text)
        self.assertIn('"health": service_health_summary(', text)
        self.assertIn('"state":', text)
        self.assertIn('"issues":', text)
        self.assertIn('"last_checked":', text)
        self.assertIn('"tone":', text)
        self.assertTrue(any(isinstance(node, ast.FunctionDef) and node.name == "service_health_summary" for node in tree.body))

    def test_console_information_architecture_exists(self):
        text = index_source()

        for label in ("态势总览", "解析策略", "运行维护", "高级配置", "系统控制"):
            self.assertIn(label, text)

        for view_id in (
            'id="view-overview"',
            'id="view-policy"',
            'id="view-operations"',
            'id="view-advanced"',
            'id="view-control"',
        ):
            self.assertIn(view_id, text)

    def test_overview_contains_operations_console_components(self):
        text = index_source()

        for marker in (
            "status-ribbon",
            "health-banner",
            "resolver-flow",
            "incident-feed",
            "quick-remediation",
            "risk-action",
        ):
            self.assertIn(marker, text)

    def test_existing_upgrade_and_version_contract_hooks_remain(self):
        text = index_source()

        for hook in (
            "panelRepo",
            "loadPanelUpgradeSource",
            "confirmUpgradePanel",
            "handlePanelVersionClick",
            "upgrade_panel",
            "startReloadCountdown",
        ):
            self.assertIn(hook, text)

    def test_login_page_matches_console_direction(self):
        text = login_source()

        self.assertIn("control-room", text)
        self.assertIn("Mosctl 控制台", text)
        self.assertIn("DNS 运维入口", text)
        self.assertIn("status-strip", text)


if __name__ == "__main__":
    unittest.main()
