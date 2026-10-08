"""规则文件和定时任务：只下载/发布 config 真正引用的列表，定时任务输出进日志。"""
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "remote-root" / "usr" / "local" / "bin" / "mosctl"
APP = ROOT / "remote-root" / "etc" / "mosdns" / "manager" / "app.py"
TEMPLATE = ROOT / "remote-root" / "etc" / "mosdns" / "templates" / "default.yaml"
RULES_DIR = ROOT / "remote-root" / "etc" / "mosdns" / "rules"
LOGROTATE = ROOT / "remote-root" / "etc" / "logrotate.d" / "mosdns"
INSTALL = ROOT / "install.sh"


class RuleFilesContractTest(unittest.TestCase):
    def test_every_shipped_rule_file_is_referenced_by_the_template(self):
        referenced = set(re.findall(r"rules/([A-Za-z0-9_.-]+)", TEMPLATE.read_text(encoding="utf-8")))
        shipped = {p.name for p in RULES_DIR.iterdir() if p.is_file()}
        self.assertEqual(shipped - referenced, set(), "仓库里有模板没引用的规则文件")
        self.assertEqual(referenced - shipped, set(), "模板引用了仓库里没有的规则文件")

    def test_cli_only_downloads_referenced_lists_and_drops_obsolete_files(self):
        text = CLI.read_text(encoding="utf-8")
        self.assertIn('dl "/etc/mosdns/rules/geosite_cn.txt"', text)
        self.assertIn('dl "/etc/mosdns/rules/geosite_no_cn.txt"', text)
        self.assertNotIn('dl "/etc/mosdns/rules/geoip_cn.txt"', text)
        self.assertNotIn('dl "/etc/mosdns/rules/geosite_apple.txt"', text)
        self.assertNotIn('edit_rule "/etc/mosdns/rules/user_iot.txt"', text)
        self.assertNotIn("智能家居直连", text)
        self.assertIn("rm -f /etc/mosdns/rules/geoip_cn.txt /etc/mosdns/rules/geosite_apple.txt", text)

    def test_geo_cron_logs_and_legacy_entries_are_migrated(self):
        text = APP.read_text(encoding="utf-8")
        self.assertIn('UPDATE_LOG = "/var/log/mosctl-update.log"', text)
        self.assertIn("{GEO_UPDATE_COMMAND} >> {UPDATE_LOG} 2>&1", text)
        self.assertNotIn("{GEO_UPDATE_COMMAND} > /dev/null", text)
        self.assertIn("def migrate_cron_logging", text)
        self.assertIn('("/etc/logrotate.d/mosdns", "etc/logrotate.d/mosdns", "file", 0o644)', text)

    def test_logrotate_is_shipped_and_installed(self):
        rotate = LOGROTATE.read_text(encoding="utf-8")
        self.assertIn("/var/log/mosdns.log", rotate)
        self.assertIn("copytruncate", rotate)
        self.assertIn("/etc/logrotate.d/mosdns", INSTALL.read_text(encoding="utf-8"))
        self.assertIn("rm -f /etc/logrotate.d/mosdns", CLI.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()


class DownloadTimeoutContractTest(unittest.TestCase):
    def test_geo_downloads_have_a_timeout(self):
        text = CLI.read_text(encoding="utf-8")
        self.assertIn('WGET_TIMEOUT_OPTS=(--timeout=20 --tries=2)', text)
        body = text[text.find("wget_fetch() {"):]
        body = body[:body.find("\n}\n")]
        self.assertEqual(body.count('"${WGET_TIMEOUT_OPTS[@]}"'), 2, "两个 wget 分支都要带超时")

