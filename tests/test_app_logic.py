"""纯逻辑函数的功能测试：设备归因、备份过滤、.env 读写、URL 解析。

其他测试只 grep 源码，抓不到"last_seen 取错"这类逻辑错误，这里真的把 app.py
exec 成模块来调用。Flask 用桩替换，/etc/mosdns 指到临时目录（app.py 导入时会
调用 ensure_env() 写 .env）。
"""
from pathlib import Path
import os
import sys
import tempfile
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


def device(ip, **extra):
    item = {
        "ip": ip,
        "last_seen": "",
        "last_query": "",
        "query_count": 0,
        "domain_count": 0,
        "domains": [],
    }
    item.update(extra)
    return item


class AppLogicTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = load_app(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    # --- 设备 / 流量归因 ---

    def test_last_seen_is_max_timestamp_regardless_of_log_order(self):
        logs = "\n".join(
            [
                '2026-10-08 10:00:05 {"client":"10.0.0.5","qname":"a.example.com."}',
                '2026-10-08 10:00:09 {"client":"10.0.0.5","qname":"b.example.com."}',
                '2026-10-08 10:00:07 {"client":"10.0.0.5","qname":"a.example.com."}',
            ]
        )
        devices = {item["ip"]: item for item in self.app.parse_device_log_clients(logs)}
        self.assertEqual(devices["10.0.0.5"]["last_seen"], "2026-10-08 10:00:09")
        self.assertEqual(devices["10.0.0.5"]["query_count"], 3)

    def test_domain_attribution_moves_bytes_off_gateway_without_double_counting(self):
        gateway = "10.0.0.1"
        phone = "10.0.0.5"
        devices = {
            gateway: device(gateway, traffic_download=1000, traffic_upload=100, connections=2, traffic_total=1100),
            phone: device(phone, query_count=3, domain_count=1, domains=[{"domain": "video.example.com", "count": 3}]),
        }
        connections = [
            {"metadata": {"sourceIP": gateway, "host": "cdn.video.example.com"}, "download": 600, "upload": 40},
            {"metadata": {"sourceIP": gateway, "host": "unknown.invalid"}, "download": 400, "upload": 60},
        ]
        attributed = self.app.apply_domain_attributed_traffic(devices, connections, {gateway})

        self.assertEqual(attributed, 1)
        self.assertEqual(devices[phone]["traffic_download"], 600)
        self.assertEqual(devices[phone]["traffic_upload"], 40)
        self.assertEqual(devices[phone]["connections"], 1)
        self.assertTrue(devices[phone]["traffic_estimated"])
        self.assertEqual(devices[gateway]["traffic_download"], 400)
        self.assertEqual(devices[gateway]["traffic_upload"], 60)
        self.assertEqual(devices[gateway]["connections"], 1)
        total = sum(int(item.get("traffic_total") or 0) for item in devices.values())
        self.assertEqual(total, 1100)

    def test_source_ip_ignores_host_and_inbound_fields(self):
        self.assertEqual(
            self.app.connection_source_ip({"metadata": {"host": "1.2.3.4:443", "inboundIp": "10.0.0.1", "inboundIP": "10.0.0.1"}}),
            "",
        )
        self.assertEqual(self.app.connection_source_ip({"metadata": {"sourceIP": "10.0.0.7", "host": "1.2.3.4"}}), "10.0.0.7")
        self.assertEqual(self.app.connection_source_ip({"sourceIP": "10.0.0.8:51000"}), "10.0.0.8")

    def test_domain_cap_is_display_only(self):
        lines = [
            f'2026-10-08 10:00:{index:02d} {{"client":"10.0.0.9","qname":"host{index}.example.com."}}'
            for index in range(15)
        ]
        devices = {item["ip"]: item for item in self.app.parse_device_log_clients("\n".join(lines))}
        all_domains = devices["10.0.0.9"]["domains"]
        self.assertEqual(len(all_domains), 15)
        self.assertEqual(devices["10.0.0.9"]["domain_count"], 15)
        # 归因索引用全部域名
        index = self.app.device_domain_index(devices)
        self.assertEqual(len(index), 15)
        # 展示时截断到 12
        self.assertEqual(len(self.app.display_domains(all_domains)), self.app.DEVICE_DOMAIN_DISPLAY_LIMIT)
        self.assertEqual(self.app.DEVICE_DOMAIN_DISPLAY_LIMIT, 12)

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

    def test_wildcard_controller_host_is_localized_by_hostname_only(self):
        self.assertEqual(self.app.localize_wildcard_host("http://0.0.0.0:9090"), "http://127.0.0.1:9090")
        self.assertEqual(self.app.localize_wildcard_host("http://[::]:9090"), "http://127.0.0.1:9090")
        self.assertEqual(self.app.localize_wildcard_host("http://10.0.0.0:9090"), "http://10.0.0.0:9090")
        self.assertEqual(self.app.localize_wildcard_host("http://10.0.0.0:9090/path"), "http://10.0.0.0:9090/path")

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


if __name__ == "__main__":
    unittest.main()
