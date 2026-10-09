"""逐页检查后的修正：备份时间、改账号要当前密码、事件流合并启停噪音、最近通知、定时任务按本地时间填写。"""
from pathlib import Path
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest

from test_app_logic import load_app, ROOT

INDEX = ROOT / "remote-root" / "etc" / "mosdns" / "manager" / "templates" / "index.html"


class PanelReviewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = load_app(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    # --- 备份时间以文件名为准，新备份不会因为原文件旧而被先清理 ---
    def test_backup_time_from_name_and_fresh_backup_survives_cleanup(self):
        app = self.app
        Path(app.BACKUP_DIR).mkdir(parents=True, exist_ok=True)
        old = Path(app.BACKUP_DIR) / "config.20261001080000.bak"
        old.write_text("a")
        os.utime(old, (2000000000, 2000000000))  # 修改时间比文件名新得多
        listed = app.list_backup_files([str(old)])[0]
        self.assertEqual(listed["mtime"], int(time.mktime(time.strptime("20261001080000", "%Y%m%d%H%M%S"))))
        config = Path(self.tmp.name) / "config.yaml"
        config.write_text("x")
        os.utime(config, (1000000000, 1000000000))  # 被备份的文件很旧
        for name in ("config.20261002080000.bak", "config.20261003080000.bak"):
            (Path(app.BACKUP_DIR) / name).write_text("b")
        app.backup_keep_count = lambda: 3
        path = app.backup_file(str(config), "config")
        self.assertTrue(os.path.exists(path), "刚做的备份必须留下")
        self.assertFalse(old.exists(), "最旧的那个被清理")
        self.assertGreater(os.path.getmtime(path), 1500000000, "不再沿用原文件的修改时间")

    # --- 改账号要当前密码 ---
    def test_account_change_requires_current_password(self):
        app = self.app
        Path(app.ENV_FILE).write_text('WEB_USER=admin\nWEB_SECRET=oldpass1\n')
        self.assertEqual(app.write_account_settings({"username": "admin", "password": "newpass1", "confirm": "newpass1"}), (False, "请先输入当前密码"))
        self.assertEqual(app.write_account_settings({"current_password": "nope", "username": "admin"}), (False, "当前密码不正确"))
        ok, _ = app.write_account_settings({"current_password": "oldpass1", "username": "admin2"})
        self.assertTrue(ok)
        self.assertEqual(app.read_env()["WEB_USER"], "admin2")

    # --- 事件流 ---
    def test_restart_noise_is_grouped(self):
        def line(t, level, comp, msg, payload=""):
            return "\t".join(x for x in (f"2026-10-09T10:35:{t:02d}+00:00", level, comp, msg, payload) if x != "")
        lines = [  # 新的在前（和 /api/logs 默认顺序一致）
            line(50, "ERROR", "forward_remote", "upstream error", '{"error": "timeout"}'),
            line(48, "INFO", "", "all plugins are loaded"),
            line(48, "INFO", "", "loading plugin", '{"tag": "cache", "type": "cache"}'),
            line(48, "INFO", "", "loading plugin", '{"tag": "hosts", "type": "hosts"}'),
            line(47, "INFO", "", "all plugins were closed"),
            line(47, "WARN", "", "signal received", '{"signal": "terminated"}'),
            line(10, "INFO", "cache", "cache dumped", '{"entries": 20}'),
            line(5, "INFO", "cache", "cache dumped", '{"entries": 10}'),
        ]
        entries = self.app.parse_log_entries("\n".join(lines))
        self.assertEqual([e["summary"] for e in entries][1:], ["mosdns 已重启（重新加载 2 个模块）", "缓存已定期保存 2 次（最近一次 20 条记录）"])
        self.assertEqual(entries[0]["kind"], "error", "真正的错误单独保留")
        self.assertEqual(entries[1]["detail"], "合并了 5 行启停日志；模块：cache、hosts")

    def test_upstream_error_and_double_restart(self):
        line = '2026-10-08T23:26:47+00:00\tWARN\tforward_local\tupstream error\t{"qname": "mdap.alipay.com.", "upstream": "udp://119.29.29.29", "error": "context deadline exceeded"}'
        entry = self.app.parse_log_entries(line)[0]
        self.assertEqual(entry["summary"], "上游查询超时：mdap.alipay.com（119.29.29.29）")
        def l(t, msg, payload=""):
            return "\t".join(x for x in (f"2026-10-09T10:35:{t:02d}+00:00", "INFO", msg, payload) if x)
        lines = [l(48, "all plugins are loaded"), l(48, "loading plugin", '{"tag": "a"}'), l(47, "all plugins were closed"),
                 l(46, "all plugins are loaded"), l(46, "loading plugin", '{"tag": "a"}'), l(45, "all plugins were closed")]
        self.assertEqual(self.app.parse_log_entries("\n".join(lines))[0]["summary"], "mosdns 连续重启 2 次（每次加载 1 个模块）")

    def test_metrics_parsing(self):
        app = self.app
        app.config_api_address = lambda: "127.0.0.1:8080"
        text = ("# HELP x\nmosdns_cache_hit_total{tag=\"cache\"} 13051\nmosdns_cache_lazy_hit_total{tag=\"cache\"} 11887\n"
                "mosdns_cache_query_total{tag=\"cache\"} 14316\nmosdns_cache_size_current{tag=\"cache\"} 2167\ngo_goroutines 12\n")
        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self, n=-1): return text.encode()
        app.urlrequest.urlopen = lambda req, timeout=2: Resp()
        self.assertEqual(app.mosdns_metrics(), {"queries": 14316, "hits": 13051, "lazy_hits": 11887, "cache_size": 2167})
        def boom(*a, **k): raise OSError("down")
        app.urlrequest.urlopen = boom
        self.assertIsNone(app.mosdns_metrics())

    # --- 最近通知 ---
    def test_recent_notifications(self):
        log = Path(self.tmp.name) / "notify.log"
        log.write_text(
            "[2026-10-09 05:40:39] sent host=nf.example:8900 HTTP 200 title=⚠️ 电信 · 收到的同步规则未能应用\n"
            "[2026-10-09 06:00:00] error ValueError subject=x\n"
            "[2026-10-09 07:00:00] failed host=nf.example:8900 error=HTTP 502 title=✅ 电信 · mosdns 内核已更新\n", encoding="utf-8")
        items = self.app.recent_notifications(path=str(log))
        self.assertEqual([i["ok"] for i in items], [False, True])
        self.assertEqual(items[0]["detail"], "HTTP 502")
        self.assertEqual(items[1]["title"], "⚠️ 电信 · 收到的同步规则未能应用")
        self.assertEqual(self.app.recent_notifications(path=str(log) + ".missing"), [])


class ScheduleLocalTimeJsTest(unittest.TestCase):
    def run_js(self, call, cases):
        node = shutil.which("node")
        if not node:
            self.skipTest("node 不可用")
        html = INDEX.read_text(encoding="utf-8")
        funcs = "".join(re.search(rf"(?ms)^    function {name}\(.*?^    \}}\n", html).group(0)
                        for name in ("shiftClock", "serverLocalDelta", "describeGeoScheduleLocal"))
        funcs += "".join(re.search(rf"(?m)^    function {name}\(.*$", html).group(0) + "\n"
                         for name in ("serverClockToLocal", "localClockToServer"))
        script = "Date.prototype.getTimezoneOffset = () => -480;\n" + funcs + \
            f"\nconst cases = JSON.parse(process.argv[1]);\nprocess.stdout.write(JSON.stringify(cases.map(c => {call})));"
        return json.loads(subprocess.run([node, "-e", script, json.dumps(cases)], capture_output=True, text=True, check=True).stdout)

    def test_conversions_cross_midnight(self):
        # 浏览器北京时间，服务器 UTC（offset 0）
        self.assertEqual(self.run_js("serverClockToLocal(c, 0)", ["02:00", "19:40"]), [{"time": "10:00", "days": 0}, {"time": "03:40", "days": 1}])
        self.assertEqual(self.run_js("localClockToServer(c, 0)", ["03:40", "10:00"]), [{"time": "19:40", "days": -1}, {"time": "02:00", "days": 0}])
        self.assertEqual(self.run_js("localClockToServer(c, 28800)", ["03:40"]), [{"time": "03:40", "days": 0}])

    def test_geo_summary(self):
        out = self.run_js("describeGeoScheduleLocal(c[0], c[1], c[2])", [["daily", "10:00", "1"], ["every_6h", "10:00", "1"], ["weekly", "03:00", "0"]])
        self.assertEqual(out, ["每天 10:00 更新", "每 6 小时更新一次（04:00、10:00、16:00、22:00）", "每周日 03:00 更新"])

    def test_wiring(self):
        html = INDEX.read_text(encoding="utf-8")
        self.assertIn("time: localClockToServer($('autoUpdateTime').value", html)
        self.assertIn("$('autoUpdateTime').value = serverClockToLocal(", html)
        self.assertIn("weekday: geoServerSchedule().weekday", html)
        self.assertIn("current_password: $('accountCurrentPassword').value", html)


if __name__ == "__main__":
    unittest.main()
