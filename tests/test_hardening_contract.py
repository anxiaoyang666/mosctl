"""v0.3.27 安全/健壮性修复的契约测试（grep 源码，确认修复不被回退）。"""
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "remote-root" / "etc" / "mosdns" / "manager" / "app.py"
INDEX = ROOT / "remote-root" / "etc" / "mosdns" / "manager" / "templates" / "index.html"
INSTALL = ROOT / "install.sh"
MOSCTL = ROOT / "remote-root" / "usr" / "local" / "bin" / "mosctl"
WEB_SERVICE = ROOT / "remote-root" / "etc" / "systemd" / "system" / "mosdns-web.service"
SYSCTL = ROOT / "remote-root" / "etc" / "sysctl.d" / "99-mosdns.conf"


def read(path):
    return path.read_text(encoding="utf-8")


class HardeningContractTest(unittest.TestCase):
    def test_rule_sync_endpoint_is_hardened(self):
        text = read(APP)

        self.assertIn('app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024', text)
        self.assertIn("def json_body", text)
        self.assertNotIn("request.json", text)
        self.assertIn('secrets.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))', text)
        self.assertIn("请求体必须是 JSON 对象", text)

    def test_config_and_rule_writes_roll_back_on_restart_failure(self):
        text = read(APP)

        self.assertIn("def restart_or_rollback", text)
        self.assertIn("def quoted_replacer", text)
        self.assertIn("def upstream_value_error", text)
        # 不再用 f-string 模板做正则替换
        self.assertNotIn("rf'\\g<1>\"{", text)
        self.assertNotIn('rf"\\g<1>{', text)
        self.assertIn('restart_or_rollback([(backup, CONFIG_FILE)], "配置已保存并重启 mosdns", "配置已保存")', text)
        self.assertIn('restart_or_rollback([rollback], "规则已保存并重启 mosdns", "规则已保存")', text)

    def test_backups_are_namespaced_by_prefix(self):
        text = read(APP)
        install = read(INSTALL)

        self.assertIn('CONFIG_BACKUP_PREFIX = "config."', text)
        self.assertIn('RULE_BACKUP_PREFIX = "rule-"', text)
        self.assertIn('KERNEL_BACKUP_PREFIX = "mosdns-bin."', text)
        self.assertIn("def kernel_backup_candidates", text)
        self.assertIn("def rule_backup_candidates", text)
        self.assertIn('backup_file(CONFIG_FILE, "config")', text)
        self.assertIn("backup_file(path, rule_backup_prefix(rule_id))", text)
        self.assertIn('"$INSTALL_DIR/backup/config.$(date +%Y%m%d%H%M%S).bak"', install)
        self.assertNotIn("config.yaml.bak.$(date", install)

    def test_default_template_validator_is_sandboxed(self):
        text = read(APP)

        self.assertIn("def sandbox_config_text", text)
        self.assertIn("def free_local_port", text)
        self.assertIn("def port_open", text)
        self.assertIn("listen:", text)
        self.assertIn("dump_file:", text)
        self.assertIn('os.path.join(tmpdir, "mosdns.log")', text)

    def test_installer_keeps_existing_config_and_survives_missing_env_keys(self):
        install = read(INSTALL)

        self.assertIn("保留不覆盖", install)
        self.assertIn("env_value() {", install)
        self.assertIn("(hostname -I 2>/dev/null || true)", install)
        self.assertIn('GH_PROXY="$GH_PROXY"', install)

    def test_concurrency_and_env_write_are_locked(self):
        text = read(APP)

        self.assertIn("OPERATION_LOCK = threading.Lock()", text)
        self.assertIn("ENV_LOCK = threading.Lock()", text)
        self.assertIn("def operation_locked", text)
        self.assertIn("OPERATION_LOCK.acquire(blocking=False)", text)
        self.assertIn('OPERATION_BUSY_MESSAGE = "操作进行中，请稍后再试"', text)
        self.assertIn("os.replace(tmp_file, ENV_FILE)", text)
        for route in ("/api/control", "/api/settings", "/api/config", "/api/backups", "/api/rules/<rule_id>"):
            self.assertRegex(text, re.escape(f'@app.route("{route}"') + r'[^\n]*\n(@login_required\n)?@operation_locked')

    def test_web_auth_hygiene(self):
        text = read(APP)
        index = read(INDEX)

        self.assertIn('app.config["SESSION_COOKIE_SAMESITE"] = "Lax"', text)
        self.assertIn('app.config["SESSION_COOKIE_HTTPONLY"] = True', text)
        self.assertIn("LOGIN_MAX_FAILURES = 5", text)
        self.assertIn("LOGIN_LOCK_SECONDS = 60", text)
        self.assertIn("def require_ajax_header_for_api_writes", text)
        self.assertIn('request.headers.get("X-Requested-With", "") != "XMLHttpRequest"', text)
        self.assertIn('@app.route("/logout", methods=["POST"])', text)
        self.assertIn('updates["WEB_SESSION_SECRET"] = secrets.token_urlsafe(48)', text)
        self.assertIn('app.secret_key = updates["WEB_SESSION_SECRET"]', text)
        self.assertNotIn("os.environ", text)
        self.assertIn("'X-Requested-With': 'XMLHttpRequest'", index)
        self.assertIn('<form method="post" action="/logout"', index)
        self.assertNotIn('href="/logout"', index)
        self.assertIn("if (!res.ok)", index)
        self.assertIn("window.addEventListener('unhandledrejection'", index)

    def test_system_side_effects_are_scoped(self):
        sysctl = read(SYSCTL)
        cli = read(MOSCTL)
        service = read(WEB_SERVICE)

        self.assertNotRegex(sysctl, r"(?m)^\s*net\.ipv4\.ip_forward\s*=")
        self.assertIn("IP_FORWARD_STATE", cli)
        self.assertIn('sysctl -w "net.ipv4.ip_forward=${previous:-0}"', cli)
        self.assertIn("if [ -L /etc/resolv.conf ]; then", cli)
        self.assertNotIn("EnvironmentFile=", service)

    def test_cli_small_fixes(self):
        cli = read(MOSCTL)

        self.assertNotIn("${NC}", cli)
        self.assertIn("def sed_escape", cli.replace("sed_escape() {", "def sed_escape"))
        self.assertIn('version) echo "$SCRIPT_VER" ;;', cli)
        self.assertIn("PANEL_VERSION", cli)
        self.assertIn("fetch_url() {", cli)
        self.assertIn("env_has GH_PROXY", cli)

    def test_panel_version_bumped(self):
        text = read(APP)
        match = re.search(r'(?m)^PANEL_VERSION = "(\d+)\.(\d+)\.(\d+)"$', text)
        self.assertIsNotNone(match)
        self.assertGreaterEqual(tuple(int(part) for part in match.groups()), (0, 3, 27))


if __name__ == "__main__":
    unittest.main()
