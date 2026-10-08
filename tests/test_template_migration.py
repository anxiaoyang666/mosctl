"""v0.3.34：HTTPS(65) 拒绝、备用国内上游、模板版本标记与旧站点迁移。

app.py 部分沿用 test_app_logic 的桩 Flask 加载方式；CLI 部分把 mosctl 里的相关函数
抽出来用 bash 真跑（macOS 的 BSD sed 不支持 GNU 的 sed -i，测试里加一层 shim）。
"""
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import types
import unittest

from test_app_logic import load_app


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "remote-root" / "etc" / "mosdns" / "templates" / "default.yaml"
INDEX = ROOT / "remote-root" / "etc" / "mosdns" / "manager" / "templates" / "index.html"
CLI = ROOT / "remote-root" / "usr" / "local" / "bin" / "mosctl"


def plugin_block(text, tag):
    match = re.search(rf"(?ms)^  - tag: {re.escape(tag)}\n.*?(?=^  - tag: |\Z)", text)
    assert match, f"missing plugin tag {tag}"
    return match.group(0)


# 旧站点模板：api 监听 0.0.0.0、user_iot ip_set、apple 兜底，没有版本标记也没有备用上游
OLD_CONFIG = """log:
  level: info
  file: "/var/log/mosdns.log"

api:
  http: "0.0.0.0:8080"

plugins:
  - tag: cache
    type: cache
    args:
      size: 10240
      lazy_cache_ttl: 86400
      dump_file: "/etc/mosdns/cache.dump"

  - tag: user_iot
    type: ip_set
    args:
      files:
        - "/etc/mosdns/rules/user_iot.txt"

  - tag: geosite_apple
    type: domain_set
    args:
      files:
        - "/etc/mosdns/rules/geosite_apple.txt"

  - tag: forward_local
    type: forward
    args:
      upstreams:
        - addr: "udp://119.29.29.29" # TAG_LOCAL
          enable_pipeline: true

  - tag: forward_remote
    type: forward
    args:
      upstreams:
        - addr: "192.168.10.101:53" # TAG_REMOTE

  - tag: main_sequence
    type: sequence
    args:
      - matches: qname $geosite_apple
        exec: $forward_local
      - exec: $forward_remote

  - tag: udp_server
    type: udp_server
    args:
      entry: main_sequence
      listen: ":53"
"""


class TemplateFileTest(unittest.TestCase):
    def setUp(self):
        self.text = TEMPLATE.read_text(encoding="utf-8")

    def test_template_starts_with_version_marker(self):
        self.assertEqual(self.text.splitlines()[0], "# mosctl-template: 4")

    def test_https_records_rejected_right_after_hosts(self):
        block = plugin_block(self.text, "reject_https_record")
        self.assertIn("matches: qtype 65", block)
        self.assertIn("exec: reject 0", block)
        steps = re.findall(r"^      - exec: (.+)$", plugin_block(self.text, "main_sequence"), re.M)
        self.assertEqual(
            steps[:4],
            ["$hosts", "jump has_resp_sequence", "$reject_https_record", "jump has_resp_sequence"],
        )
        # 定义要在 main_sequence 之前（mosdns v5 按顺序加载插件）
        self.assertLess(self.text.index("tag: reject_https_record"), self.text.index("tag: main_sequence"))

    def test_forward_local_has_backup_and_concurrent(self):
        block = plugin_block(self.text, "forward_local")
        self.assertIn("      concurrent: 2\n", block)
        self.assertIn('        - addr: "udp://119.29.29.29" # TAG_LOCAL\n', block)
        self.assertIn('        - addr: "udp://223.5.5.5" # TAG_LOCAL_BACKUP\n', block)


class AppBackupUpstreamTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = load_app(self.tmp.name)
        self.config = Path(self.app.CONFIG_FILE)
        template = Path(self.app.DEFAULT_TEMPLATE_FILE)
        template.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(TEMPLATE, template)
        self.checked = []
        self.sandbox_result = (True, "ok")
        self.restarts = []

        def fake_config_starts(path, wait_seconds=3.0):
            self.checked.append(Path(path).read_text(encoding="utf-8"))
            return self.sandbox_result

        self.app.config_starts = fake_config_starts
        self.app.restart_mosdns = lambda: self.restarts.append(1) or (True, "")

    def tearDown(self):
        self.tmp.cleanup()

    # --- 版本标记 ---

    def test_template_version_parsing(self):
        tv = self.app.template_version
        self.assertEqual(tv("# mosctl-template: 4\nlog:\n"), 4)
        self.assertEqual(tv("log:\n  level: info\n"), 0)
        self.assertEqual(tv(""), 0)
        self.assertEqual(tv("\n  #   mosctl-template :   12   \nlog:\n"), 12)
        self.assertEqual(tv("a\nb\nc\nd\ne\n# mosctl-template: 4\n"), 0)  # 只看前 5 行

    def test_status_reports_outdated_config(self):
        self.app.jsonify = lambda data: data
        self.app.session = {"logged_in": True}
        for name in ("service_active", "service_enabled", "rescue_enabled"):
            setattr(self.app, name, lambda: False)
        self.app.get_version = lambda: "v5"
        self.app.read_env = lambda: {}
        self.app.service_health_summary = lambda *args: {}
        self.app.server_time_info = lambda: {}

        self.config.write_text(OLD_CONFIG, encoding="utf-8")
        status = self.app.api_status()
        self.assertEqual(status["config_template_version"], 0)
        self.assertEqual(status["latest_template_version"], 4)
        self.assertTrue(status["config_outdated"])
        self.assertEqual(status["local_dns_backup"], "")

        shutil.copy(TEMPLATE, self.config)
        status = self.app.api_status()
        self.assertEqual(status["config_template_version"], 4)
        self.assertFalse(status["config_outdated"])
        self.assertEqual(status["local_dns_backup"], "223.5.5.5")
        self.assertEqual(status["local_dns_backup_raw"], "udp://223.5.5.5")

    # --- 主/备用国内上游互不影响 ---

    def test_primary_and_backup_are_read_and_written_independently(self):
        shutil.copy(TEMPLATE, self.config)
        values = self.app.parse_config_values()
        self.assertEqual(values["local_dns_raw"], "udp://119.29.29.29")
        self.assertEqual(values["local_dns_backup_raw"], "udp://223.5.5.5")

        # 只改主上游，备用行不动（local_backup=None 表示不传）
        ok, message = self.app.update_config_values("1.1.1.1", "8.8.8.8", "86400")
        self.assertTrue(ok, message)
        text = self.config.read_text(encoding="utf-8")
        self.assertIn('- addr: "udp://1.1.1.1" # TAG_LOCAL\n', text)
        self.assertIn('- addr: "udp://223.5.5.5" # TAG_LOCAL_BACKUP\n', text)

        # 只改备用，主上游不动
        ok, message = self.app.update_config_values("1.1.1.1", "8.8.8.8", "86400", "114.114.114.114")
        self.assertTrue(ok, message)
        values = self.app.parse_config_values()
        self.assertEqual(values["local_dns_raw"], "udp://1.1.1.1")
        self.assertEqual(values["local_dns_backup_raw"], "udp://114.114.114.114")
        self.assertEqual(self.config.read_text(encoding="utf-8").count("TAG_LOCAL_BACKUP"), 1)

        # 备用校验和主上游一样
        ok, message = self.app.update_config_values("1.1.1.1", "8.8.8.8", "86400", "http://1.2.3.4")
        self.assertFalse(ok)
        self.assertTrue(message.startswith("备用国内 DNS "))

    def test_empty_backup_removes_line_and_lowers_concurrent(self):
        shutil.copy(TEMPLATE, self.config)
        ok, message = self.app.update_config_values("119.29.29.29", "8.8.8.8", "86400", "")
        self.assertTrue(ok, message)
        block = plugin_block(self.config.read_text(encoding="utf-8"), "forward_local")
        self.assertNotIn("TAG_LOCAL_BACKUP", block)
        self.assertIn("      concurrent: 1\n", block)
        self.assertIn('- addr: "udp://119.29.29.29" # TAG_LOCAL\n          enable_pipeline: true\n', block)
        self.assertEqual(self.app.parse_config_values()["local_dns_backup"], "")

    def test_backup_inserted_into_old_config_after_local_item(self):
        self.config.write_text(OLD_CONFIG, encoding="utf-8")
        ok, message = self.app.update_config_values("119.29.29.29", "192.168.10.101", "86400", "223.5.5.5")
        self.assertTrue(ok, message)
        block = plugin_block(self.config.read_text(encoding="utf-8"), "forward_local")
        self.assertEqual(
            block.strip("\n"),
            "  - tag: forward_local\n    type: forward\n    args:\n      concurrent: 2\n      upstreams:\n"
            '        - addr: "udp://119.29.29.29" # TAG_LOCAL\n          enable_pipeline: true\n'
            '        - addr: "udp://223.5.5.5" # TAG_LOCAL_BACKUP',
        )
        # 写之前过了沙箱
        self.assertIn("TAG_LOCAL_BACKUP", self.checked[-1])

    def test_backup_change_refused_when_sandbox_fails(self):
        self.config.write_text(OLD_CONFIG, encoding="utf-8")
        self.sandbox_result = (False, "plugin forward_local: bad")
        ok, message = self.app.update_config_values("119.29.29.29", "192.168.10.101", "86400", "223.5.5.5")
        self.assertFalse(ok)
        self.assertIn("plugin forward_local: bad", message)
        self.assertEqual(self.config.read_text(encoding="utf-8"), OLD_CONFIG)
        self.assertEqual(self.restarts, [])

    # --- 迁移 ---

    def test_migrate_old_config_keeps_values_and_gets_new_structure(self):
        self.config.write_text(OLD_CONFIG, encoding="utf-8")
        result = self.app.migrate_to_current_template()
        self.assertTrue(result["success"], result["message"])
        self.assertTrue(result["sandbox_ok"])
        self.assertEqual(
            result["carried"],
            {"local": "udp://119.29.29.29", "local_backup": "", "remote": "192.168.10.101:53", "ttl": "86400"},
        )
        text = self.config.read_text(encoding="utf-8")
        self.assertEqual(self.app.template_version(text), 4)
        self.assertIn('- addr: "udp://119.29.29.29" # TAG_LOCAL\n', text)
        self.assertIn('- addr: "udp://223.5.5.5" # TAG_LOCAL_BACKUP\n', text)
        self.assertIn('- addr: "192.168.10.101:53" # TAG_REMOTE\n', text)
        self.assertIn("lazy_cache_ttl: 86400", text)
        self.assertIn('http: "127.0.0.1:8080"', text)
        self.assertNotIn("0.0.0.0:8080", text)
        self.assertNotIn("user_iot", text)
        self.assertIn("exec: reject 0", text)
        self.assertFalse(self.app.template_version_info()["config_outdated"])
        # 旧配置进了备份
        self.assertTrue(any(p.name.startswith("config.") for p in Path(self.app.BACKUP_DIR).iterdir()))

    def test_migrate_carries_existing_backup(self):
        text = TEMPLATE.read_text(encoding="utf-8").replace("udp://223.5.5.5", "udp://114.114.114.114")
        self.config.write_text(text, encoding="utf-8")
        result = self.app.migrate_to_current_template()
        self.assertEqual(result["carried"]["local_backup"], "udp://114.114.114.114")
        self.assertIn('"udp://114.114.114.114" # TAG_LOCAL_BACKUP', self.config.read_text(encoding="utf-8"))

    def test_migrate_route_uses_lock_and_refuses_on_sandbox_failure(self):
        self.app.jsonify = lambda data: data
        self.app.session = {"logged_in": True}
        self.app.request = types.SimpleNamespace(method="POST", path="/api/config/migrate")
        self.config.write_text(OLD_CONFIG, encoding="utf-8")

        self.assertTrue(self.app.OPERATION_LOCK.acquire(blocking=False))
        try:
            body, status = self.app.api_config_migrate()
        finally:
            self.app.OPERATION_LOCK.release()
        self.assertEqual(status, 409)
        self.assertEqual(self.checked, [])

        self.sandbox_result = (False, "yaml: unknown plugin")
        result = self.app.api_config_migrate()
        self.assertFalse(result["success"])
        self.assertFalse(result["sandbox_ok"])
        self.assertIn("yaml: unknown plugin", result["message"])
        self.assertTrue(result["config_outdated"])
        self.assertEqual(self.config.read_text(encoding="utf-8"), OLD_CONFIG)
        self.assertFalse(Path(f"{self.app.CONFIG_FILE}.defaultcheck").exists())
        self.assertEqual(self.restarts, [])

    def test_restore_default_template_still_returns_pair(self):
        self.config.write_text(OLD_CONFIG, encoding="utf-8")
        ok, message = self.app.restore_default_template()
        self.assertTrue(ok, message)
        self.assertEqual(self.app.template_version(self.config.read_text(encoding="utf-8")), 4)


def cli_functions(*names):
    source = CLI.read_text(encoding="utf-8")
    chunks = []
    for name in names:
        match = re.search(rf"(?ms)^{re.escape(name)}\(\) \{{\n.*?^\}}\n", source)
        assert match, name
        chunks.append(match.group(0))
    return "\n".join(chunks)


@unittest.skipUnless(shutil.which("bash"), "需要 bash")
class CliBackupUpstreamTest(unittest.TestCase):
    # BSD sed 的 -i 需要后缀参数；GNU sed 不需要。shim 只在 macOS 上补一个空后缀
    SED_SHIM = 'if [ "$(uname)" = Darwin ]; then sed() { if [ "$1" = -i ]; then shift; command sed -i "" "$@"; else command sed "$@"; fi; }; fi\n'

    def run_bash(self, script):
        body = self.SED_SHIM + cli_functions(
            "config_tag_value", "sed_escape", "upstream_invalid", "set_config_tag_value",
            "carry_over_config_values", "template_version_of",
        ) + "\n" + script
        result = subprocess.run(["bash", "-c", body], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_cli_tag_values_are_independent(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.yaml"
            shutil.copy(TEMPLATE, config)
            out = self.run_bash(
                f'f="{config}"\n'
                'config_tag_value "$f" TAG_LOCAL; config_tag_value "$f" TAG_LOCAL_BACKUP\n'
                'set_config_tag_value "$f" TAG_LOCAL "udp://1.1.1.1"\n'
                'config_tag_value "$f" TAG_LOCAL; config_tag_value "$f" TAG_LOCAL_BACKUP\n'
                'set_config_tag_value "$f" TAG_LOCAL_BACKUP "udp://114.114.114.114"\n'
                'config_tag_value "$f" TAG_LOCAL; config_tag_value "$f" TAG_LOCAL_BACKUP\n'
                'template_version_of "$f"\n'
            )
            self.assertEqual(
                out.split(),
                [
                    "udp://119.29.29.29", "udp://223.5.5.5",
                    "udp://1.1.1.1", "udp://223.5.5.5",
                    "udp://1.1.1.1", "udp://114.114.114.114",
                    "4",
                ],
            )

    def test_cli_carry_over_from_old_config_keeps_template_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = Path(tmp) / "old.yaml"
            new = Path(tmp) / "new.yaml"
            old.write_text(OLD_CONFIG.replace("lazy_cache_ttl: 86400", "lazy_cache_ttl: 3600"), encoding="utf-8")
            shutil.copy(TEMPLATE, new)
            out = self.run_bash(f'carry_over_config_values "{old}" "{new}"\ntemplate_version_of "{old}"\n')
            self.assertEqual(out.strip(), "0")
            text = new.read_text(encoding="utf-8")
            self.assertIn('- addr: "udp://119.29.29.29" # TAG_LOCAL\n', text)
            self.assertIn('- addr: "udp://223.5.5.5" # TAG_LOCAL_BACKUP\n', text)
            self.assertIn('- addr: "192.168.10.101:53" # TAG_REMOTE\n', text)
            self.assertIn("lazy_cache_ttl: 3600", text)

    def test_cli_carry_over_keeps_old_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = Path(tmp) / "old.yaml"
            new = Path(tmp) / "new.yaml"
            old.write_text(TEMPLATE.read_text(encoding="utf-8").replace("udp://223.5.5.5", "udp://114.114.114.114"), encoding="utf-8")
            shutil.copy(TEMPLATE, new)
            self.run_bash(f'carry_over_config_values "{old}" "{new}"\n')
            text = new.read_text(encoding="utf-8")
            self.assertIn('- addr: "udp://114.114.114.114" # TAG_LOCAL_BACKUP\n', text)
            self.assertIn('- addr: "udp://119.29.29.29" # TAG_LOCAL\n', text)


class CliAndUiContractTest(unittest.TestCase):
    def test_cli_menu_and_anchored_markers(self):
        source = CLI.read_text(encoding="utf-8")
        self.assertIn('3) change_upstream "备用国内" "TAG_LOCAL_BACKUP" "udp"', source)
        self.assertIn("修改备用国内 DNS", source)
        self.assertIn("可在面板迁移", source)
        # 不再有不锚定行尾的 "addr:.*# TAG" 替换
        self.assertNotRegex(source, r"addr:\.\*# TAG_LOCAL\|")
        self.assertNotRegex(source, r"addr:\.\*\$tag_marker")

    def test_policy_view_has_backup_input_and_migrate_card(self):
        html = INDEX.read_text(encoding="utf-8")
        self.assertIn('<input id="local_dns_backup"', html)
        self.assertIn("备用国内 DNS", html)
        self.assertIn("local_dns_backup: $('local_dns_backup').value", html)
        self.assertIn('id="migrateCard" hidden', html)
        self.assertIn("迁移到最新模板", html)
        self.assertIn("'/api/config/migrate'", html)
        self.assertIn("$('migrateCard').hidden = !outdated", html)
        self.assertIn("失败则不做任何修改", html)
        policy = html[html.index('id="view-policy"'):]
        self.assertLess(policy.index('id="migrateCard"'), policy.index("DNS 参数"))


if __name__ == "__main__":
    unittest.main()
