"""v0.3.31：设备页移除、解析策略写入前校验、运行维护时区/上次更新（grep 源码，确认不被回退）。"""
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "remote-root" / "etc" / "mosdns" / "manager" / "app.py"
INDEX = ROOT / "remote-root" / "etc" / "mosdns" / "manager" / "templates" / "index.html"
CLI = ROOT / "remote-root" / "usr" / "local" / "bin" / "mosctl"
INSTALL = ROOT / "install.sh"


def read(path):
    return path.read_text(encoding="utf-8")


class DevicesViewRemovedTest(unittest.TestCase):
    def test_backend_has_no_device_or_mihomo_code(self):
        text = read(APP)
        for name in (
            "/api/devices",
            "/api/mihomo",
            "def collect_devices",
            "def parse_device_log_clients",
            "def read_neighbor_table",
            "def mihomo_api_get",
            "def localize_wildcard_host",
            "MIHOMO_CONTROLLER",
            "DEVICE_NOTES_FILE",
            "urlunsplit",
        ):
            self.assertNotIn(name, text, name)
        self.assertNotIn("MIHOMO", read(INSTALL))

    def test_ui_has_no_devices_view(self):
        text = read(INDEX)
        for marker in ("设备状态", 'id="view-devices"', "loadDevices", "renderDevices", "mihomoController", "deviceDomainOpenState", "formatBytes"):
            self.assertNotIn(marker, text, marker)
        # 规则同步的文案仍提到 mihomo 面板，这是同步目标，不是设备页
        self.assertIn("mosctl / mihomo 面板地址", text)

    def test_readmes_drop_mihomo_keys(self):
        for name in ("README.md", "README.zh-CN.md"):
            text = read(ROOT / name)
            self.assertNotIn("MIHOMO_CONTROLLER", text)
            self.assertNotIn("MIHOMO_API_SECRET", text)


class PolicyHardeningContractTest(unittest.TestCase):
    def test_every_write_path_validates_in_sandbox_first(self):
        text = read(APP)
        self.assertIn("def config_text_starts", text)
        self.assertIn("def rule_content_starts", text)
        self.assertIn("def tail_lines", text)
        self.assertIn('tail_lines(clean_output(stdout + stderr))', text)
        # update_config_values / api_config / restore_backup / save_rule_content 都在写文件前校验
        self.assertRegex(text, r"def update_config_values[\s\S]*?config_text_starts\(new_text\)[\s\S]*?write_config_text\(new_text\)")
        self.assertRegex(text, r"def api_config[\s\S]*?config_text_starts\(content\)[\s\S]*?write_config_text\(content\)")
        self.assertRegex(text, r"def restore_backup[\s\S]*?config_starts\(source\)[\s\S]*?shutil\.copy2\(source, CONFIG_FILE\)")
        self.assertRegex(text, r"def save_rule_content[\s\S]*?rule_content_starts\(rule_id, content\)[\s\S]*?os\.replace\(tmp_file, path\)")
        # 第二道防线仍在
        self.assertIn('restart_or_rollback([(backup, CONFIG_FILE)], "配置已保存并重启 mosdns", "配置已保存")', text)

    def test_restart_resets_failed_first_everywhere(self):
        text = read(APP)
        self.assertRegex(text, r'def restart_mosdns\(\):[\s\S]*?\["systemctl", "reset-failed", "mosdns"\][\s\S]*?\["systemctl", "restart", "mosdns"\]')
        # api_control 的 restart 走 restart_mosdns，不再直接 systemctl restart
        self.assertNotIn('"restart": (["systemctl", "restart", "mosdns"], 30)', text)
        self.assertIn('if action == "restart":\n        ok, message = restart_mosdns()', text)

    def test_upstream_validation_contract(self):
        text = read(APP)
        self.assertIn('UPSTREAM_SCHEMES = ("udp", "tcp", "tls", "https", "h3", "quic", "doq", "tcp+pipeline", "tls+pipeline")', text)
        self.assertIn("def bracket_bare_ipv6", text)
        self.assertNotIn("def looks_like_host_port", text)
        self.assertIn("urlsplit(f\"{probe_scheme}://{rest}\")", text)

    def test_rule_editor_validation_and_help_text(self):
        text = read(APP)
        self.assertIn("def hosts_rule_error", text)
        self.assertIn("def domain_rule_error", text)
        self.assertIn('DOMAIN_RULE_PREFIXES = ("domain:", "full:", "keyword:", "regexp:")', text)
        self.assertIn("格式是「域名 IP」，不是「IP 域名」（第 {number} 行）", text)
        self.assertIn("默认精确匹配", text)
        self.assertIn("domain:home.lan 10.10.30.1", text)

    def test_cache_flush_uses_api_before_restart(self):
        text = read(APP)
        cli = read(CLI)
        self.assertIn("def flush_cache_via_api", text)
        self.assertIn('f"http://{address}/plugins/{tag}/flush"', text)
        self.assertNotIn('"flush": ([MOSCTL, "flush"], 60)', text)
        self.assertIn('http_get_ok "http://${api}/plugins/${tag}/flush"', cli)
        self.assertIn("config_api_addr() {", cli)
        self.assertIn("config_cache_tag() {", cli)

    def test_resolution_test_button_is_not_called_config_test(self):
        index = read(INDEX)
        self.assertIn("解析测试", index)
        self.assertNotIn("测试配置", index)
        self.assertIn("这不是配置检查", index)

    def test_sync_disabled_suffix_removed(self):
        self.assertNotIn("规则同步未启用。", read(APP))


class OperationsContractTest(unittest.TestCase):
    def test_geo_schedule_reports_last_run_files_and_cron_state(self):
        text = read(APP)
        index = read(INDEX)
        for name in ("def parse_geo_update_log", "def geo_rule_files", "def geo_update_status", "def read_crontab_state", "def cron_service_active"):
            self.assertIn(name, text)
        self.assertIn("except FileNotFoundError:", text)
        self.assertIn('"cron_available": read_crontab_state()[1]', text)
        self.assertIn("**read_geo_schedule(), **geo_update_status()", text)
        for marker in ('id="geoLastRun"', 'id="geoLastRunSummary"', 'id="geoCronWarning"', 'id="geoFiles"', "function renderGeoUpdateStatus", "上次更新："):
            self.assertIn(marker, index)

    def test_cli_prints_unambiguous_result_and_quiet_wget_off_tty(self):
        cli = read(CLI)
        self.assertIn('echo "===== 结果: 成功 ====="', cli)
        self.assertIn('echo "===== 结果: 失败 ====="', cli)
        self.assertIn("update) update_geo_rules; exit $? ;;", cli)
        self.assertIn("do_update_geo_rules() {", cli)
        self.assertIn("systemctl restart mosdns || return 1", cli)
        self.assertIn("if [ -t 1 ]; then", cli)
        self.assertIn("--progress=dot:mega", cli)
        self.assertNotIn('wget -q --show-progress -O "$output"', cli)

    def test_timestamps_are_formatted_in_browser(self):
        text = read(APP)
        index = read(INDEX)
        self.assertNotIn("mtime_text", text)
        self.assertNotIn("mtime_text", index)
        self.assertNotIn('strftime("%Y-%m-%d %H:%M:%S")', text)
        self.assertIn('"last_checked": int(time.time())', text)
        self.assertIn("def server_time_info", text)
        self.assertIn("**server_time_info(),", text)
        self.assertIn("function formatTime", index)
        self.assertIn("function formatServerClock", index)
        self.assertIn('id="geoServerTime"', index)
        self.assertIn("服务器时区：", index)
        self.assertIn("formatTime(item.mtime)", index)
        self.assertIn("formatTime(data.last_checked)", index)
        self.assertIn("formatTime(entry.time)", index)

    def test_core_upgrade_rollback_is_guarded(self):
        text = read(APP)
        self.assertIn('stop_ok, stop_message = run_cmd(["systemctl", "stop", "mosdns"], timeout=30)', text)
        self.assertIn("停止 mosdns 失败，未替换内核", text)
        self.assertIn("回滚旧内核也失败", text)
        self.assertIn("没有旧内核备份可回滚", text)

    def test_panel_upgrade_reload_polls_status(self):
        index = read(INDEX)
        self.assertIn("fetch('/api/status', {headers: {'X-Requested-With': 'XMLHttpRequest'}, cache: 'no-store'})", index)
        self.assertIn("if (res.status === 200)", index)
        self.assertIn("最多 ${limit} 秒", index)
        self.assertNotIn("秒后自动刷新", index)
        self.assertNotIn("请稍等几秒后刷新页面", read(APP))

    def test_sync_settings_form_is_repopulated_from_response(self):
        index = read(INDEX)
        self.assertRegex(index, r"async function saveSyncSettings[\s\S]*?\$\('syncToken'\)\.value = res\.token \|\| ''")

    def test_panel_version_bumped(self):
        match = re.search(r'(?m)^PANEL_VERSION = "(\d+)\.(\d+)\.(\d+)"$', read(APP))
        self.assertGreaterEqual(tuple(int(part) for part in match.groups()), (0, 3, 31))


if __name__ == "__main__":
    unittest.main()
