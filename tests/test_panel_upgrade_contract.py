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
        self.assertGreaterEqual(version, (0, 3, 9))

    def test_logs_are_explained_for_web_dashboard(self):
        text = app_source()
        index = index_source()

        self.assertIn("def explain_log_line", text)
        self.assertIn("def parse_log_entries", text)
        self.assertIn('"entries": parse_log_entries(logs)', text)
        self.assertIn("renderDashboardLogs", index)
        self.assertIn("log-card", index)
        self.assertIn("log-summary", index)
        self.assertIn("log-detail", index)
        self.assertIn("未知日志", index)

    def test_panel_upgrade_checks_proxy_source_before_github_direct(self):
        text = app_source()

        self.assertIn('read_url_text([f"https://gh-proxy.com/{raw_url}", raw_url]', text)
        self.assertIn('download_file([f"https://gh-proxy.com/{archive_url}", archive_url]', text)
        self.assertNotIn('read_url_text([raw_url, f"https://gh-proxy.com/{raw_url}"]', text)
        self.assertNotIn('download_file([archive_url, f"https://gh-proxy.com/{archive_url}"]', text)

    def test_core_version_check_uses_proxy_source_before_github_api(self):
        text = app_source()

        self.assertIn('f"https://gh-proxy.com/{MOSDNS_RELEASE_API}",\n        MOSDNS_RELEASE_API,', text)
        self.assertNotIn('MOSDNS_RELEASE_API,\n        f"https://gh-proxy.com/{MOSDNS_RELEASE_API}",', text)

    def test_sidebar_version_label_does_not_repeat_product_name(self):
        text = index_source()

        self.assertNotIn("'Mosctl v' + (res.panel_version", text)
        self.assertNotIn("'Mosctl v' + (res.current_version", text)
        self.assertIn("'v' + (res.panel_version || '--')", text)
        self.assertIn("'v' + (res.current_version || '--') + ' 可更新'", text)


    def test_sidebar_brand_uses_reference_style_version_badge(self):
        text = index_source()

        self.assertIn(".brand-copy", text)
        self.assertIn("width: 40px;", text)
        self.assertIn("height: 40px;", text)
        self.assertIn("font-size: 22px;", text)
        self.assertIn("font-size: 13px;", text)
        self.assertIn("min-height: 46px;", text)
        self.assertIn("width: 280px;", text)
        self.assertIn("left: 72px;", text)
        self.assertIn("top: 70px;", text)
        self.assertNotIn("width: 64px;", text)
        self.assertNotIn("width: 48px;", text)
        self.assertNotIn("font-size: 31px;", text)
        self.assertIn("border-radius: 999px;", text)
        self.assertIn("background: #f2f4f7;", text)
        self.assertIn('class="brand-copy"', text)

    def test_sidebar_version_brand_is_subtle_and_update_state_is_amber(self):
        text = index_source()

        self.assertIn("font-weight: 650;", text)
        self.assertIn("font-weight: 500;", text)
        self.assertIn("font-weight: 680;", text)
        self.assertNotIn("font-weight: 780;", text)
        self.assertNotIn("font-weight: 760;", text)
        self.assertNotIn("font-weight: 900;", text)
        self.assertIn("background: #fffbeb;", text)
        self.assertIn("color: #b45309;", text)
        self.assertIn("version-dot", text)
        self.assertIn("@keyframes versionPulse", text)
        self.assertIn("animation: versionPulse 1.6s ease-in-out infinite;", text)

    def test_sidebar_version_badge_opens_version_popover(self):
        text = index_source()

        self.assertIn('id="versionPopover"', text)
        self.assertIn('id="versionPopoverCurrent"', text)
        self.assertIn('id="versionPopoverState"', text)
        self.assertIn('class="version-popover"', text)
        self.assertIn("function renderVersionPopover", text)
        self.assertIn("function toggleVersionPopover", text)
        self.assertIn("function refreshPanelVersionPopover", text)
        self.assertIn("function openPanelRelease", text)
        self.assertIn("versionPopover.classList.toggle('show'", text)
        self.assertIn("document.addEventListener('click'", text)
        self.assertIn("versionPopover.contains(event.target)", text)
        self.assertIn("versionText.contains(event.target)", text)


if __name__ == "__main__":
    unittest.main()
