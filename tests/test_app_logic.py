"""纯逻辑函数的功能测试：备份过滤、.env 读写、URL 解析、配置/规则校验。

其他测试只 grep 源码，抓不到"last_seen 取错"这类逻辑错误，这里真的把 app.py
exec 成模块来调用。Flask 用桩替换，/etc/mosdns 指到临时目录（app.py 导入时会
调用 ensure_env() 写 .env）。
"""
from pathlib import Path
import os
import re
import sys
import tempfile
import time
import types
import unittest


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "remote-root" / "etc" / "mosdns" / "manager" / "app.py"


def load_app(tmp_dir):
    flask = types.ModuleType("flask")

    class FakeFlask:
        permanent_session_lifetime = None
        secret_key = None

        def __init__(self, *args, **kwargs):
            self.config = {}

        def route(self, *args, **kwargs):
            return lambda func: func

        def before_request(self, func):
            return func

    for name in ("render_template", "request", "jsonify", "redirect", "session"):
        setattr(flask, name, None)
    flask.Flask = FakeFlask
    sys.modules["flask"] = flask

    source = APP.read_text(encoding="utf-8").replace('MOSDNS_DIR = "/etc/mosdns"', f'MOSDNS_DIR = "{tmp_dir}"')
    module = types.ModuleType("mosctl_app_under_test")
    exec(compile(source, str(APP), "exec"), module.__dict__)
    return module


class AppLogicTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = load_app(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    # --- 备份过滤 ---

    def test_backup_lists_are_separated_by_prefix(self):
        backup_dir = Path(self.app.BACKUP_DIR)
        backup_dir.mkdir(parents=True, exist_ok=True)
        names = [
            "config.20261008100000.bak",
            "config.yaml.20261008090000.bak",  # 旧版配置备份，仍可识别
            "config.pre-web-sync.20261008080000.yaml",
            "rule-force-cn.20261008100000.bak",
            "force-cn.txt.20261008090000.bak",  # 旧版规则备份，不再列出
            "mosdns-bin.20261008100000",
            "mosdns-bin.20261008090000.bak",
            "config.yaml.webtmp",
        ]
        for name in names:
            (backup_dir / name).write_text("x", encoding="utf-8")

        config_ids = {item["id"] for item in self.app.backup_candidates()}
        self.assertEqual(
            config_ids,
            {"config.20261008100000.bak", "config.yaml.20261008090000.bak", "config.pre-web-sync.20261008080000.yaml"},
        )
        self.assertEqual(
            {item["id"] for item in self.app.kernel_backup_candidates()},
            {"mosdns-bin.20261008100000", "mosdns-bin.20261008090000.bak"},
        )
        rule_groups = self.app.rule_backup_candidates()
        self.assertEqual(list(rule_groups), ["rule-force-cn"])
        self.assertIsNone(self.app.resolve_backup("rule-force-cn.20261008100000.bak"))
        self.assertIsNone(self.app.resolve_backup("mosdns-bin.20261008100000"))
        self.assertIsNotNone(self.app.resolve_backup("config.20261008100000.bak"))

    def test_cleanup_keeps_counts_per_kind(self):
        backup_dir = Path(self.app.BACKUP_DIR)
        backup_dir.mkdir(parents=True, exist_ok=True)
        for index in range(6):
            stamp = f"202610081000{index:02d}"
            for name in (f"config.{stamp}.bak", f"rule-hosts.{stamp}.bak", f"mosdns-bin.{stamp}"):
                path = backup_dir / name
                path.write_text("x", encoding="utf-8")
                os.utime(path, (1_700_000_000 + index, 1_700_000_000 + index))

        result = self.app.cleanup_old_backups(keep_count=4)
        remaining = sorted(path.name for path in backup_dir.iterdir())
        self.assertEqual(sum(name.startswith("config.") for name in remaining), 4)
        self.assertEqual(sum(name.startswith("rule-hosts.") for name in remaining), 4)
        self.assertEqual(sum(name.startswith("mosdns-bin.") for name in remaining), self.app.KERNEL_BACKUP_KEEP_COUNT)
        self.assertEqual(result["remaining_count"], 4)
        # 最新的被保留
        self.assertIn("config.20261008100005.bak", remaining)
        self.assertNotIn("config.20261008100000.bak", remaining)

    def test_legacy_rule_backups_are_renamed_on_cleanup(self):
        backup_dir = Path(self.app.BACKUP_DIR)
        backup_dir.mkdir(parents=True, exist_ok=True)
        legacy = {
            "force-cn.txt.20261008090000.bak": "rule-force-cn.20261008090000.bak",
            "force-nocn.txt.20261008090001.bak": "rule-force-nocn.20261008090001.bak",
            "hosts.txt.20261008090002.bak": "rule-hosts.20261008090002.bak",
        }
        untouched = [
            "user_iot.txt.20261008090003.bak",  # 不在 RULE_FILES 里
            "config.yaml.20261008090004.bak",  # 配置备份，不是规则
            "hosts.txt.20261008090005.bak",  # 目标已存在，保留原文件
        ]
        for name in list(legacy) + untouched:
            (backup_dir / name).write_text(name, encoding="utf-8")
        (backup_dir / "rule-hosts.20261008090005.bak").write_text("existing", encoding="utf-8")

        self.app.cleanup_old_backups(keep_count=10)
        remaining = {path.name for path in backup_dir.iterdir()}

        for old_name, new_name in legacy.items():
            self.assertNotIn(old_name, remaining)
            self.assertIn(new_name, remaining)
            self.assertEqual((backup_dir / new_name).read_text(encoding="utf-8"), old_name)
        for name in untouched:
            self.assertIn(name, remaining)
        self.assertEqual((backup_dir / "rule-hosts.20261008090005.bak").read_text(encoding="utf-8"), "existing")
        groups = self.app.rule_backup_candidates()
        self.assertEqual(sorted(groups), ["rule-force-cn", "rule-force-nocn", "rule-hosts"])
        # 第二次运行没有东西可改名
        self.assertEqual(self.app.migrate_legacy_rule_backups(), [])

    def test_panel_managed_targets_exclude_local_state(self):
        targets = [target for target, _, _, _ in self.app.panel_managed_targets()]
        self.assertIn(self.app.MANAGER_DIR, targets)
        self.assertIn(self.app.DEFAULT_TEMPLATE_FILE, targets)
        for local_state in (self.app.ENV_FILE, self.app.CONFIG_FILE, f"{self.app.MOSDNS_DIR}/rules"):
            self.assertNotIn(local_state, targets)

    def test_session_lifetime_is_30_days(self):
        from datetime import timedelta

        self.assertEqual(self.app.app.permanent_session_lifetime, timedelta(days=30))

    def test_backup_file_returns_path_with_prefix(self):
        target = Path(self.tmp.name) / "config.yaml"
        target.write_text("a: 1\n", encoding="utf-8")
        path = self.app.backup_file(str(target), "config")
        self.assertTrue(os.path.basename(path).startswith("config."))
        self.assertTrue(path.endswith(".bak"))
        self.assertIsNone(self.app.backup_file(str(Path(self.tmp.name) / "missing"), "config"))

    # --- .env ---

    def test_write_env_appends_newline_before_new_keys_and_keeps_existing(self):
        env_file = Path(self.app.ENV_FILE)
        env_file.write_text('WEB_USER="admin"\nWEB_PORT="7840"', encoding="utf-8")  # 无结尾换行
        self.app.write_env({"GH_PROXY": "", "WEB_PORT": "7841"})
        content = env_file.read_text(encoding="utf-8")
        self.assertIn('WEB_USER="admin"\n', content)
        self.assertIn('WEB_PORT="7841"\n', content)
        self.assertIn('\nGH_PROXY=""\n', content)
        self.assertNotIn('7840"GH_PROXY', content)
        env = self.app.read_env()
        self.assertEqual(env["WEB_PORT"], "7841")
        self.assertEqual(env["GH_PROXY"], "")
        self.assertEqual(oct(env_file.stat().st_mode & 0o777), oct(0o600))
        self.assertFalse(os.path.exists(f"{self.app.ENV_FILE}.webtmp"))

    def test_gh_proxy_prefix_distinguishes_missing_and_empty(self):
        env_file = Path(self.app.ENV_FILE)
        env_file.write_text('WEB_USER="admin"\n', encoding="utf-8")
        self.assertEqual(self.app.gh_proxy_prefix(), self.app.DEFAULT_GH_PROXY)
        url = "https://github.com/x/y/archive/refs/heads/main.zip"
        self.assertEqual(self.app.github_url_candidates(url), [url, self.app.DEFAULT_GH_PROXY + url])

        self.app.write_env({"GH_PROXY": ""})
        self.assertEqual(self.app.gh_proxy_prefix(), "")
        self.assertEqual(self.app.github_url_candidates(url), [url])

        self.app.write_env({"GH_PROXY": "https://mirror.example"})
        self.assertEqual(self.app.gh_proxy_prefix(), "https://mirror.example/")
        self.assertEqual(self.app.github_url_candidates("https://example.com/a.zip"), ["https://example.com/a.zip"])

    # --- 其他纯函数 ---

    def test_upstream_values_with_yaml_breaking_characters_are_rejected(self):
        self.assertIsNone(self.app.upstream_value_error("udp://1.1.1.1"))
        for bad in ('1.1.1.1" # x', "a\\b", "1.1.1.1 #c"):
            self.assertIsNotNone(self.app.upstream_value_error(bad))
        replacer = self.app.quoted_replacer(r"\g<1>oops")
        import re

        out = re.sub(r"(a)(b)", replacer, "ab")
        self.assertEqual(out, 'a"\\g<1>oops"b')

    def test_sandbox_config_rewrites_listen_log_dump_and_api(self):
        content = (
            'log:\n  level: info\n  file: "/var/log/mosdns.log"\n\n'
            'api:\n  http: "127.0.0.1:8080"\n\nplugins:\n'
            '  - tag: cache\n    args:\n      dump_file: "/etc/mosdns/cache.dump"\n'
            '  - tag: udp_server\n    args:\n      listen: ":53"\n'
            '  - tag: tcp_server\n    args:\n      listen: ":53"\n'
        )
        rewritten, api_port = self.app.sandbox_config_text(content, self.tmp.name)
        self.assertNotIn(':53"', rewritten)
        self.assertNotIn("/var/log/mosdns.log", rewritten)
        self.assertNotIn("/etc/mosdns/cache.dump", rewritten)
        self.assertNotIn("127.0.0.1:8080", rewritten)
        self.assertIn(f'http: "127.0.0.1:{api_port}"', rewritten)
        self.assertEqual(rewritten.count('listen: "127.0.0.1:'), 2)
        self.assertIn(f'file: "{os.path.join(self.tmp.name, "mosdns.log")}"', rewritten)
        self.assertIn(f'dump_file: "{os.path.join(self.tmp.name, "cache.dump")}"', rewritten)

    def test_parse_github_contents_text_tolerates_garbage(self):
        self.assertEqual(self.app.parse_github_contents_text("not json"), "")
        self.assertEqual(self.app.parse_github_contents_text('{"encoding":"base64","content":"%%%"}'), "")
        self.assertEqual(self.app.parse_github_contents_text('{"encoding":"base64","content":"aGk="}'), "hi")


    # --- v0.3.31：上游地址校验 ---

    def test_normalize_upstream_accepts_supported_schemes_and_brackets_ipv6(self):
        normalize = self.app.normalize_upstream
        self.assertEqual(normalize("223.5.5.5", default_scheme="udp"), ("udp://223.5.5.5", None))
        self.assertEqual(normalize("2400:3200::1", default_scheme="udp"), ("udp://[2400:3200::1]", None))
        self.assertEqual(normalize("[2400:3200::1]:853", default_scheme="udp"), ("udp://[2400:3200::1]:853", None))
        self.assertEqual(normalize("tls://dns.alidns.com"), ("tls://dns.alidns.com", None))
        self.assertEqual(normalize("https://dns.google/dns-query"), ("https://dns.google/dns-query", None))
        self.assertEqual(normalize("tcp+pipeline://1.1.1.1"), ("tcp+pipeline://1.1.1.1", None))
        # 国外上游不补协议，只补端口，和模板里 "8.8.8.8:53" 的写法一致
        self.assertEqual(normalize("8.8.8.8", default_port=53), ("8.8.8.8:53", None))
        self.assertEqual(normalize("8.8.8.8:53", default_port=53), ("8.8.8.8:53", None))
        self.assertEqual(normalize("2001:4860:4860::8888", default_port=53), ("[2001:4860:4860::8888]:53", None))

    def test_normalize_upstream_rejects_bad_scheme_whitespace_and_empty_host(self):
        normalize = self.app.normalize_upstream
        for bad in ("http://1.1.1.1", "doh://x"):
            value, error = normalize(bad)
            self.assertEqual(value, "")
            self.assertIn("不支持的协议", error)
            for scheme in self.app.UPSTREAM_SCHEMES:
                self.assertIn(scheme, error)
        self.assertEqual(normalize("dns.google 53")[1], "不能包含空格")
        self.assertEqual(normalize("")[1], "不能为空")
        self.assertEqual(normalize("tls://")[1], "缺少主机名或 IP")
        self.assertIsNotNone(normalize("udp://[::1")[1])
        self.assertIsNotNone(self.app.upstream_value_error("http://1.1.1.1"))

    def test_display_upstream_round_trip(self):
        display = self.app.display_upstream
        self.assertEqual(display("udp://x", "udp"), "x")
        self.assertEqual(display("8.8.8.8:53", None), "8.8.8.8")
        self.assertEqual(display("udp://[2400:3200::1]", "udp"), "[2400:3200::1]")
        self.assertEqual(display("tls://dns.alidns.com", "udp"), "tls://dns.alidns.com")
        # 显示值再保存回去得到同样的原始值
        self.assertEqual(self.app.normalize_upstream(display("udp://[2400:3200::1]", "udp"), default_scheme="udp")[0], "udp://[2400:3200::1]")
        self.assertEqual(self.app.normalize_upstream(display("8.8.8.8:53", None), default_port=53)[0], "8.8.8.8:53")

    # --- v0.3.31：规则文件格式校验 ---

    def test_hosts_rule_validation(self):
        check = self.app.hosts_rule_error
        self.assertIsNone(check("# 注释\n\nnas.lan 10.10.30.10\ndomain:home.lan 10.10.30.1 fd00::1 # 行内注释\n"))
        self.assertEqual(check("nas.lan 10.0.0.1\n10.0.0.2 router.lan"), "格式是「域名 IP」，不是「IP 域名」（第 2 行）")
        self.assertIn("第 1 行", check("nas.lan"))
        self.assertIn("10.0.0.x", check("nas.lan 10.0.0.x"))
        self.assertEqual(self.app.rule_content_error("hosts", "10.0.0.1 nas.lan"), "格式是「域名 IP」，不是「IP 域名」（第 1 行）")

    def test_domain_rule_validation(self):
        check = self.app.domain_rule_error
        self.assertIsNone(check("qq.com\nfull:a.b.c # ok\nkeyword:cdn\nregexp:^a\\.b$\n# 注释\n"))
        self.assertIn("第 2 行", check("qq.com\nbad domain here"))
        self.assertIn("第 1 行", check("domain:"))
        self.assertIsNone(self.app.rule_content_error("force-nocn", "github.com"))
        self.assertIsNotNone(self.app.rule_content_error("force-cn", "a b"))

    # --- v0.3.31：写配置前先校验 ---

    def _write_config(self, rules_dir):
        config = (
            'log:\n  level: info\n  file: "/var/log/mosdns.log"\n\napi:\n  http: "127.0.0.1:8080"\n\nplugins:\n'
            '  - tag: cache\n    type: cache\n    args:\n      lazy_cache_ttl: 86400\n      dump_file: "/etc/mosdns/cache.dump"\n'
            '  - tag: forward_local\n    type: forward\n    args:\n      upstreams:\n        - addr: "udp://119.29.29.29" # TAG_LOCAL\n'
            '  - tag: forward_remote\n    type: forward\n    args:\n      upstreams:\n        - addr: "8.8.8.8:53" # TAG_REMOTE\n'
            f'  - tag: hosts\n    type: hosts\n    args:\n      files:\n        - "{rules_dir}/hosts.txt"\n'
            f'  - tag: force_cn\n    type: domain_set\n    args:\n      files:\n        - "{rules_dir}/force-cn.txt"\n'
        )
        Path(self.app.CONFIG_FILE).write_text(config, encoding="utf-8")
        return config

    def test_save_rule_content_validates_in_sandbox_before_writing(self):
        rules_dir = Path(self.app.RULE_FILES["hosts"]["path"]).parent
        rules_dir.mkdir(parents=True, exist_ok=True)
        (rules_dir / "force-cn.txt").write_text("qq.com\n", encoding="utf-8")
        self._write_config(str(rules_dir))
        seen = {}

        def fake_config_starts(path, wait_seconds=3.0):
            text = Path(path).read_text(encoding="utf-8")
            seen["text"] = text
            match = re.search(r'- "([^"]+/rules/hosts\.txt)"', text)
            seen["candidate"] = Path(match.group(1)).read_text(encoding="utf-8") if match else ""
            other = re.search(r'- "([^"]+/rules/force-cn\.txt)"', text)
            seen["other_is_link"] = os.path.islink(other.group(1)) if other else None
            return seen.get("result", (True, "配置可以启动"))

        self.app.config_starts = fake_config_starts
        # 格式错误：连沙箱都不用起
        ok, message, rollback = self.app.save_rule_content("hosts", "10.0.0.1 nas.lan")
        self.assertFalse(ok)
        self.assertIn("不是「IP 域名」", message)
        self.assertNotIn("text", seen)
        self.assertFalse((rules_dir / "hosts.txt").exists())

        # 沙箱校验失败：不写文件，返回 mosdns 的输出
        seen["result"] = (False, "fatal: bad hosts line")
        ok, message, rollback = self.app.save_rule_content("hosts", "nas.lan 10.0.0.1\n")
        self.assertFalse(ok)
        self.assertIn("fatal: bad hosts line", message)
        self.assertFalse((rules_dir / "hosts.txt").exists())
        # 校验用的配置指向临时目录里的候选文件，其他规则文件是指回原文件的链接
        self.assertNotIn(f'"{rules_dir}/hosts.txt"', seen["text"])
        self.assertEqual(seen["candidate"], "nas.lan 10.0.0.1\n")
        self.assertTrue(seen["other_is_link"])

        # 校验通过才写入
        seen["result"] = (True, "ok")
        ok, message, rollback = self.app.save_rule_content("hosts", "nas.lan 10.0.0.1\n")
        self.assertTrue(ok)
        self.assertEqual((rules_dir / "hosts.txt").read_text(encoding="utf-8"), "nas.lan 10.0.0.1\n")
        self.assertEqual(rollback, (None, str(rules_dir / "hosts.txt")))

    def test_update_config_values_validates_before_writing_and_restarting(self):
        original = self._write_config(f"{self.tmp.name}/rules")
        calls = []
        self.app.config_starts = lambda path, wait_seconds=3.0: (False, "line1\nplugin forward: invalid addr")
        self.app.restart_mosdns = lambda: calls.append("restart") or (True, "")

        ok, message = self.app.update_config_values("1.1.1.1", "8.8.4.4", "3600")
        self.assertFalse(ok)
        self.assertIn("未保存", message)
        self.assertIn("plugin forward: invalid addr", message)
        self.assertEqual(Path(self.app.CONFIG_FILE).read_text(encoding="utf-8"), original)
        self.assertEqual(calls, [])

        self.assertEqual(self.app.update_config_values("http://1.1.1.1", "8.8.4.4", "3600")[1][:7], "国内 DNS ")
        self.assertEqual(Path(self.app.CONFIG_FILE).read_text(encoding="utf-8"), original)

        self.app.config_starts = lambda path, wait_seconds=3.0: (True, "ok")
        ok, message = self.app.update_config_values("2400:3200::1", "8.8.4.4", "3600")
        self.assertTrue(ok, message)
        text = Path(self.app.CONFIG_FILE).read_text(encoding="utf-8")
        self.assertIn('- addr: "udp://[2400:3200::1]" # TAG_LOCAL', text)
        self.assertIn('- addr: "8.8.4.4:53" # TAG_REMOTE', text)
        self.assertIn("lazy_cache_ttl: 3600", text)
        self.assertEqual(calls, ["restart"])

    def test_restore_backup_validates_backup_before_touching_config(self):
        original = self._write_config(f"{self.tmp.name}/rules")
        backup_dir = Path(self.app.BACKUP_DIR)
        backup_dir.mkdir(parents=True, exist_ok=True)
        (backup_dir / "config.20261008100000.bak").write_text("broken: [\n", encoding="utf-8")
        checked = []
        self.app.config_starts = lambda path, wait_seconds=3.0: checked.append(path) or (False, "yaml: line 1: did not find expected node")
        self.app.restart_mosdns = lambda: (True, "")

        ok, message = self.app.restore_backup("config.20261008100000.bak")
        self.assertFalse(ok)
        self.assertIn("未恢复", message)
        self.assertIn("did not find expected node", message)
        self.assertEqual(checked, [os.path.realpath(backup_dir / "config.20261008100000.bak")])
        self.assertEqual(Path(self.app.CONFIG_FILE).read_text(encoding="utf-8"), original)

    def test_restart_resets_failed_state_first_and_tail_lines_trims(self):
        commands = []
        self.app.run_cmd = lambda args, timeout=60: commands.append(args) or (True, "")
        self.app.dns_query = lambda name, server=None, timeout=2.0: (True, name)
        ok, _ = self.app.restart_mosdns()
        self.assertTrue(ok)
        self.assertEqual(commands[:2], [["systemctl", "reset-failed", "mosdns"], ["systemctl", "restart", "mosdns"]])
        self.assertEqual(self.app.tail_lines("\n".join(str(i) for i in range(40)), 15), "\n".join(str(i) for i in range(25, 40)))

    def test_flush_cache_uses_api_then_falls_back_to_dump_and_restart(self):
        self._write_config(f"{self.tmp.name}/rules")
        self.assertEqual(self.app.config_api_address(), "127.0.0.1:8080")
        self.assertEqual(self.app.config_cache_tag(), "cache")
        self.assertEqual(self.app.config_dump_file(), "/etc/mosdns/cache.dump")

        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        urls = []
        restarts = []
        self.app.urlrequest = types.SimpleNamespace(
            Request=lambda url, headers=None: url,
            urlopen=lambda url, timeout=5: urls.append(url) or FakeResponse(),
        )
        self.app.restart_mosdns = lambda: restarts.append(1) or (True, "")
        ok, message = self.app.flush_cache()
        self.assertTrue(ok)
        self.assertEqual(urls, ["http://127.0.0.1:8080/plugins/cache/flush"])
        self.assertEqual(restarts, [])
        self.assertIn("无需重启", message)

        dump = Path(self.tmp.name) / "cache.dump"
        dump.write_text("x", encoding="utf-8")
        Path(self.app.CONFIG_FILE).write_text(
            Path(self.app.CONFIG_FILE).read_text(encoding="utf-8").replace("/etc/mosdns/cache.dump", str(dump)), encoding="utf-8"
        )

        def failing_urlopen(url, timeout=5):
            raise OSError("connection refused")

        self.app.urlrequest = types.SimpleNamespace(Request=lambda url, headers=None: url, urlopen=failing_urlopen)
        ok, message = self.app.flush_cache()
        self.assertTrue(ok)
        self.assertFalse(dump.exists())
        self.assertEqual(restarts, [1])
        self.assertIn("connection refused", message)

    def test_broadcast_rule_is_silent_when_sync_disabled(self):
        self.app.write_env({"RULE_SYNC_ENABLED": "false"})
        self.assertEqual(self.app.start_broadcast("force-cn", "qq.com"), (None, ""))
        self.assertEqual(self.app.start_broadcast("hosts", "nas.lan 10.0.0.1"), (None, ""))
        self.assertEqual(self.app.SYNC_JOBS, [])

    # --- v0.3.31：运行维护 ---

    def test_crontab_binary_missing_is_reported_not_raised(self):
        def missing_run(args, **kwargs):
            raise FileNotFoundError("crontab")

        # self.app.subprocess 就是全局 subprocess 模块，必须先存原函数，否则会污染后面的测试
        original_run = self.app.subprocess.run
        self.app.subprocess.run = missing_run
        try:
            self.assertEqual(self.app.read_crontab_state(), ([], False))
            self.assertEqual(self.app.read_crontab_lines(), [])
            schedule = self.app.read_geo_schedule()
            self.assertEqual(schedule["mode"], "disabled")
            ok, message = self.app.write_geo_schedule({"mode": "daily", "time": "02:00"})
            self.assertFalse(ok)
            self.assertIn("crontab", message)
            self.app.run_cmd = lambda args, timeout=60: (False, "")
            status = self.app.geo_update_status()
            self.assertFalse(status["cron_available"])
            self.assertFalse(status["cron_service_active"])
            self.assertIsNone(status["last_run"])
            self.assertEqual([item["name"] for item in status["files"]], ["geosite_cn.txt", "geosite_no_cn.txt"])
        finally:
            self.app.subprocess.run = original_run

    def test_parse_geo_update_log_reads_last_block(self):
        log = "\n".join(
            [
                "===== 2026-10-07 02:00:01 更新 Geo 规则 =====",
                "\x1b[1;33m⬇️  正在更新 GeoSite...\x1b[0m",
                " - geosite_cn.txt",
                "❌ 下载失败：https://example/x",
                "===== 结果: 失败 =====",
                "===== 2026-10-08 02:00:01 更新 Geo 规则 =====",
                "⬇️  正在更新 GeoSite...",
                " - geosite_cn.txt",
                " - geosite_no_cn.txt",
                "✅ 规则更新完毕！",
                "===== 结果: 成功 =====",
                "",
            ]
        )
        parsed = self.app.parse_geo_update_log(log)
        self.assertEqual(parsed["at"], int(time.mktime(time.strptime("2026-10-08 02:00:01", "%Y-%m-%d %H:%M:%S"))))
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["summary"], ["⬇️  正在更新 GeoSite...", " - geosite_cn.txt", " - geosite_no_cn.txt", "✅ 规则更新完毕！"])
        failed = self.app.parse_geo_update_log(log.split("===== 2026-10-08")[0])
        self.assertFalse(failed["ok"])
        self.assertNotIn("\x1b", "".join(failed["summary"]))
        # 老版本 CLI 没有结果行：ok 未知
        self.assertIsNone(self.app.parse_geo_update_log("===== 2026-10-08 02:00:01 更新 Geo 规则 =====\nhello\n")["ok"])
        self.assertIsNone(self.app.parse_geo_update_log(""))
        self.assertEqual(len(self.app.parse_geo_update_log("===== 2026-10-08 02:00:01 更新 Geo 规则 =====\n" + "\n".join(f"l{i}" for i in range(20)))["summary"]), 8)
        self.assertEqual(self.app.read_tail_text(f"{self.tmp.name}/missing.log"), "")

    def test_timestamps_are_epoch_or_iso_with_offset(self):
        target = Path(self.tmp.name) / "config.20261008100000.bak"
        target.write_text("x", encoding="utf-8")
        item = self.app.list_backup_files([str(target)])[0]
        self.assertIsInstance(item["mtime"], int)
        self.assertNotIn("mtime_text", item)

        health = self.app.service_health_summary(True, True, False, {"local_dns": "a", "remote_dns": "b", "ttl": "1"})
        self.assertIsInstance(health["last_checked"], int)

        info = self.app.server_time_info()
        self.assertIsInstance(info["server_time"], int)
        self.assertIsInstance(info["server_utc_offset"], int)
        self.assertRegex(info["server_tz"], r"UTC[+-]\d\d:\d\d")

        logs = self.app.normalize_log_timestamps(
            "2026-10-08T07:12:01.123+0800\tinfo\tx\n2026-10-08T07:12:01Z\tinfo\ty\nplain line"
        )
        self.assertEqual(logs.splitlines(), ["2026-10-08T07:12:01+08:00\tinfo\tx", "2026-10-08T07:12:01+00:00\tinfo\ty", "plain line"])
        self.assertEqual(self.app.parse_log_entries(logs)[0]["time"], "2026-10-08T07:12:01+08:00")


if __name__ == "__main__":
    unittest.main()


class CronLoggingMigrationTest(unittest.TestCase):
    """migrate_cron_logging 在 import 末尾执行，必须真的能跑（曾因定义顺序 NameError 被静默吞掉）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = load_app(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_rewrites_devnull_entry_and_is_called_at_import(self):
        source = APP.read_text(encoding="utf-8")
        self.assertLess(source.find("def migrate_cron_logging"), source.find("\n    migrate_cron_logging()\n"))
        self.assertLess(source.find("def read_crontab_lines"), source.find("\n    migrate_cron_logging()\n"))
        self.assertLess(source.find("def is_geo_update_cron"), source.find("\n    migrate_cron_logging()\n"))

        written = []
        real_run = self.app.subprocess.run

        def fake_run(args, **kwargs):
            if args == ["crontab", "-l"]:
                return types.SimpleNamespace(returncode=0, stdout="0 2 * * * /usr/local/bin/mosctl update > /dev/null 2>&1\n", stderr="")
            written.append(kwargs.get("input"))
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        self.app.subprocess.run = fake_run
        try:
            self.assertTrue(self.app.migrate_cron_logging())
            self.assertEqual(written, ["0 2 * * * /usr/local/bin/mosctl update >> /var/log/mosctl-update.log 2>&1\n"])
            self.app.subprocess.run = lambda args, **k: types.SimpleNamespace(returncode=0, stdout=written[0], stderr="")
            self.assertFalse(self.app.migrate_cron_logging(), "已是日志形式时不应再写 crontab")
        finally:
            self.app.subprocess.run = real_run



class SelfPeerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = load_app(self.tmp.name)
        self.app.local_ipv4_addresses = lambda: {"127.0.0.1", "localhost", "10.10.10.9"}

    def tearDown(self):
        self.tmp.cleanup()

    def test_self_peer_detected_by_ip_and_port(self):
        f = self.app.is_self_peer
        self.assertTrue(f("http://10.10.10.9:7840", "7840"))
        self.assertTrue(f("http://127.0.0.1:7840", 7840))
        self.assertFalse(f("http://10.10.10.9:7838", "7840"), "同机不同端口（另一个面板）不是自己")
        self.assertFalse(f("http://10.10.20.7:7840", "7840"))
        self.assertFalse(f("not a url", "7840"))
