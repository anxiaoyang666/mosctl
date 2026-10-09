"""通知：标题/正文、Webhook 地址处理、POST 载荷、日志、去重、mosctl update 钩子、设置校验和页面卡片。"""
from pathlib import Path
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
import unittest.mock
from urllib import error

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_auto_update import APP, CLI, INDEX, MANAGER, load_app  # noqa: E402


NOTIFY_CLI = MANAGER / "notify_cli.py"
URL = "https://hook.example.com:8443/send/abc?key=SECRET123"
DAY = 86400
NOW = 1_800_000_000


class FakeResponse:
    def __init__(self, status=200):
        self.status = status

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = load_app(self.tmp.name)
        self.app.NOTIFY_LOG = os.path.join(self.tmp.name, "notify.log")
        self.requests = []
        self.status = 200

        def fake_urlopen(req, timeout):
            self.requests.append({"url": req.full_url, "method": req.get_method(), "headers": dict(req.header_items()),
                                  "body": json.loads(req.data.decode("utf-8")), "timeout": timeout})
            if isinstance(self.status, Exception):
                raise self.status
            return FakeResponse(self.status)

        self.app.notify_urlopen = fake_urlopen

    def tearDown(self):
        self.tmp.cleanup()

    def enable(self, site="联通", url=URL):
        self.app.write_env({"SITE_NAME": site, "NOTIFY_ENABLED": "true", "NOTIFY_API_URL": url})

    def log_text(self):
        try:
            return Path(self.app.NOTIFY_LOG).read_text(encoding="utf-8")
        except OSError:
            return ""


class BuilderTest(Base):
    def test_title_and_body(self):
        title, content = self.app.build_notification(
            "success", "mosdns 内核已更新", ["v5.3.4 → v5.3.5", "健康检查通过：服务、国内解析、国外解析"], site="联通", now=NOW
        )
        self.assertEqual(title, "✅ 联通 · mosdns 内核已更新")
        body, stamp = content.split("\n\n")
        self.assertEqual(body, "v5.3.4 → v5.3.5\n健康检查通过：服务、国内解析、国外解析")
        self.assertRegex(stamp, r"^📅 \d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")

    def test_icons_line_limit_and_cleanup(self):
        icons = {level: self.app.build_notification(level, "x", [], site="s")[0][0] for level in ("success", "warning", "failure", "info")}
        self.assertEqual(icons, {"success": "✅", "warning": "⚠", "failure": "❌", "info": "🔔"})
        self.assertTrue(self.app.build_notification("warning", "x", [], site="s")[0].startswith("⚠️ s · "))
        _, content = self.app.build_notification("info", "x", ["a：", "", "b\nraw second line", "c", "d", "e"], site="s")
        self.assertEqual(content.split("\n\n")[0], "a\nb\nc\nd", "去掉结尾冒号、空行、多余行，每项只取第一行，最多 4 行")
        _, content = self.app.build_notification("info", "x", [], site="s")
        self.assertTrue(content.startswith("📅 "))

    def test_site_name_default(self):
        self.assertEqual(self.app.site_name({}), "mosdns")
        self.assertEqual(self.app.site_name({"SITE_NAME": "  "}), "mosdns")
        self.assertEqual(self.app.site_name({"SITE_NAME": "熙国"}), "熙国")


class UrlAndSettingsTest(Base):
    def test_url_validation(self):
        for good in ("http://10.0.0.2:8080/hook", URL, "https://[fd00::1]/x"):
            self.assertIsNone(self.app.notify_url_error(good), good)
        for bad in ("", "ftp://x/y", "https://", "hook.example.com/x", "https://a b/x", 'https://x/"y', "https://x/$y",
                    "https://x/`y`", "https://x/\ny", "https://" + "a" * 600):
            self.assertTrue(self.app.notify_url_error(bad), bad)

    def test_full_url_never_returned(self):
        self.enable()
        settings = self.app.read_notify_settings()
        self.assertNotIn("SECRET123", json.dumps(settings, ensure_ascii=False))
        self.assertNotIn("/send/abc", json.dumps(settings, ensure_ascii=False))
        self.assertEqual(settings["url_host"], "hook.example.com:8443")
        self.assertEqual(settings["url_display"], "hook.example.com:8443 已设置")
        self.assertTrue(settings["enabled"])
        self.assertEqual(self.app.read_notify_settings({})["url_display"], "未设置")

    def test_settings_validation(self):
        validate = self.app.validate_notify_settings
        updates, err = validate({"site_name": "联通", "enabled": True, "url": URL}, env={})
        self.assertIsNone(err)
        self.assertEqual(updates, {"SITE_NAME": "联通", "NOTIFY_ENABLED": "true", "NOTIFY_API_URL": URL})
        # URL 留空沿用已保存的
        updates, err = validate({"site_name": "", "enabled": "true", "url": ""}, env={"NOTIFY_API_URL": URL})
        self.assertIsNone(err)
        self.assertNotIn("NOTIFY_API_URL", updates)
        for bad in (
            {"site_name": "a" * 21, "enabled": False},
            {"site_name": 'a"b', "enabled": False},
            {"site_name": "a'b", "enabled": False},
            {"site_name": "a\nb", "enabled": False},
            {"site_name": "x", "enabled": "yes"},
            {"site_name": "x", "enabled": False, "url": "ftp://x"},
            {"site_name": "x", "enabled": True, "url": ""},
            "not a dict",
        ):
            updates, err = validate(bad, env={})
            self.assertIsNone(updates, bad)
            self.assertTrue(err, bad)
        updates, err = validate({"site_name": "x", "enabled": False, "clear_url": True}, env={"NOTIFY_API_URL": URL})
        self.assertEqual(updates["NOTIFY_API_URL"], "")

    def test_save_keeps_url_when_blank(self):
        ok, _ = self.app.save_notify_settings({"site_name": "电信", "enabled": True, "url": URL})
        self.assertTrue(ok)
        ok, _ = self.app.save_notify_settings({"site_name": "工厂", "enabled": True, "url": ""})
        self.assertTrue(ok)
        env = self.app.read_env()
        self.assertEqual((env["SITE_NAME"], env["NOTIFY_API_URL"], env["NOTIFY_ENABLED"]), ("工厂", URL, "true"))


class SendTest(Base):
    def test_payload_shape_and_direct_timeout(self):
        self.enable()
        result = self.app.notify_event("failure", "Geo 规则更新失败", ["geosite_cn.txt 下载失败"])
        self.assertTrue(result["success"])
        self.assertEqual(len(self.requests), 1)
        req = self.requests[0]
        self.assertEqual((req["url"], req["method"], req["timeout"]), (URL, "POST", 15))
        self.assertEqual(set(req["body"]), {"title", "content"})
        self.assertEqual(req["body"]["title"], "❌ 联通 · Geo 规则更新失败")
        self.assertTrue(req["body"]["content"].startswith("geosite_cn.txt 下载失败\n\n📅 "))
        self.assertIn("application/json", req["headers"].get("Content-type", ""))

    def test_opener_has_no_proxy(self):
        source = APP.read_text(encoding="utf-8")
        self.assertIn("urlrequest.build_opener(urlrequest.ProxyHandler({}))", source)

    def test_disabled_or_missing_url_sends_nothing(self):
        self.assertTrue(self.app.notify_event("info", "x", [])["skipped"])
        self.app.write_env({"NOTIFY_ENABLED": "true", "NOTIFY_API_URL": ""})
        self.assertTrue(self.app.notify_event("info", "x", [])["skipped"])
        self.assertEqual(self.requests, [])
        self.assertEqual(self.log_text(), "")

    def test_log_has_host_but_never_path_or_query(self):
        self.enable()
        self.app.notify_event("info", "通知测试", ["通知通道正常"])
        self.status = error.HTTPError(URL, 404, "Not Found", {}, None)
        result = self.app.notify_event("info", "通知测试", ["通知通道正常"])
        self.assertFalse(result["success"])
        self.assertEqual(result["message"], "发送失败：HTTP 404")
        self.status = error.URLError(f"cannot reach {URL}")
        self.app.notify_event("info", "x", [])
        self.status = RuntimeError(f"weird failure for /send/abc?key=SECRET123")
        self.app.notify_event("info", "x", [])
        log = self.log_text()
        self.assertEqual(log.count("host=hook.example.com:8443"), 4)
        self.assertIn("sent host=hook.example.com:8443 HTTP 200", log)
        self.assertIn("failed host=hook.example.com:8443 error=HTTP 404", log)
        self.assertNotIn("SECRET123", log)
        self.assertNotIn("/send/abc", log)

    def test_never_raises(self):
        self.enable()

        def broken_env():
            raise OSError("disk gone")

        self.app.read_env = broken_env
        result = self.app.notify_event("info", "x", [])
        self.assertFalse(result["success"])

    def test_test_notification_uses_form_values_and_ignores_switch(self):
        self.app.write_env({"SITE_NAME": "联通", "NOTIFY_ENABLED": "false", "NOTIFY_API_URL": URL})
        result = self.app.send_test_notification({})
        self.assertEqual((result["success"], result["message"], result["host"]), (True, "已发送（HTTP 200）", "hook.example.com:8443"))
        self.assertEqual(self.requests[-1]["body"]["title"], "🔔 联通 · 通知测试")
        self.assertTrue(self.requests[-1]["body"]["content"].startswith("通知通道正常\n\n📅 "))
        self.assertNotIn("SECRET123", json.dumps(result, ensure_ascii=False))
        result = self.app.send_test_notification({"site_name": "熙国", "url": "http://10.0.0.9/hook"})
        self.assertEqual(self.requests[-1]["url"], "http://10.0.0.9/hook")
        self.assertEqual(self.requests[-1]["body"]["title"], "🔔 熙国 · 通知测试")
        self.assertFalse(self.app.send_test_notification({"url": "ftp://x"})["success"])
        self.app.write_env({"NOTIFY_API_URL": ""})
        self.assertEqual(self.app.send_test_notification({})["message"], "请先填写 Webhook 地址")


class DedupeTest(Base):
    def titles(self):
        return [req["body"]["title"] for req in self.requests]

    def test_failure_sequence(self):
        self.enable()
        fail = lambda now: self.app.notify_failure("geo", "failure", "Geo 规则更新失败", ["geosite_cn.txt 下载失败"], now=now)
        ok = lambda now: self.app.notify_recovery("geo", "Geo 规则更新已恢复", ["规则文件已更新，mosdns 已重启"], now=now)
        self.assertEqual(ok(NOW), "none", "之前没失败，成功不发")
        self.assertEqual(fail(NOW), "notified")
        self.assertEqual(fail(NOW + DAY), "suppressed")
        self.assertEqual(fail(NOW + 2 * DAY), "suppressed")
        self.assertEqual(fail(NOW + 3 * DAY), "reminded")
        self.assertIn("持续失败第 4 天", self.requests[-1]["body"]["content"])
        self.assertEqual(fail(NOW + 4 * DAY), "suppressed")
        self.assertEqual(ok(NOW + 5 * DAY), "recovered")
        self.assertEqual(ok(NOW + 6 * DAY), "none")
        self.assertEqual(fail(NOW + 7 * DAY), "notified")
        self.assertEqual(self.titles(), [
            "❌ 联通 · Geo 规则更新失败",
            "❌ 联通 · Geo 规则更新失败",
            "✅ 联通 · Geo 规则更新已恢复",
            "❌ 联通 · Geo 规则更新失败",
        ])
        state = json.loads(Path(self.app.NOTIFY_STATE_FILE).read_text(encoding="utf-8"))
        self.assertTrue(state["geo"]["failing"])

    def test_keys_are_independent_and_disabled_does_nothing(self):
        self.assertEqual(self.app.notify_failure("geo", "failure", "x", [], now=NOW), "disabled")
        self.assertFalse(os.path.exists(self.app.NOTIFY_STATE_FILE))
        self.enable()
        self.assertEqual(self.app.notify_failure("geo", "failure", "a", [], now=NOW), "notified")
        self.assertEqual(self.app.notify_failure("rule_sync", "warning", "b", [], now=NOW), "notified")
        self.assertEqual(len(self.requests), 2)

    def test_unsent_first_failure_is_retried(self):
        self.enable()
        self.status = error.URLError("down")
        self.assertEqual(self.app.notify_failure("geo", "failure", "x", [], now=NOW), "notified")
        self.status = 200
        self.assertEqual(self.app.notify_failure("geo", "failure", "x", [], now=NOW + 60), "reminded")
        self.assertEqual(self.app.notify_failure("geo", "failure", "x", [], now=NOW + 120), "suppressed")


class EventHookTest(Base):
    def capture(self):
        sent = []
        self.app.notify_event = lambda level, subject, lines=None, background=False, env=None: sent.append((level, subject, lines))
        return sent

    def test_restart_failure_rollback_notifies(self):
        sent = self.capture()
        target = os.path.join(self.tmp.name, "config.yaml")
        backup = os.path.join(self.tmp.name, "config.bak")
        Path(target).write_text("new", encoding="utf-8")
        Path(backup).write_text("old", encoding="utf-8")
        cases = [
            ([(False, "Job failed"), (True, "")], [(backup, target)], ["改动后 mosdns 重启失败", "已恢复修改前的文件，服务已重启"]),
            ([(False, "Job failed"), (False, "again")], [(backup, target)],
             ["改动后 mosdns 重启失败", "已恢复修改前的文件，但重启仍失败", "请立即检查，必要时启用救援模式"]),
            ([(False, "Job failed")], [], ["改动后 mosdns 重启失败", "没有可回滚的备份，请立即检查"]),
        ]
        for restarts, rollbacks, lines in cases:
            sent.clear()
            results = iter(restarts)
            self.app.restart_mosdns = lambda: next(results)
            result = self.app.restart_or_rollback(rollbacks, "ok", "配置已保存")
            ok, _ = self.app.notify_if_rolled_back("DNS 参数修改已回滚", result)
            self.assertFalse(ok)
            self.assertEqual(sent, [("failure", "DNS 参数修改已回滚", lines)])
        sent.clear()
        self.assertEqual(self.app.notify_if_rolled_back("DNS 参数修改已回滚", (True, "ok")), (True, "ok"))
        self.assertEqual(sent, [], "重启成功不发")

    def test_policy_callers_wrap_result(self):
        source = APP.read_text(encoding="utf-8")
        for subject in ('"DNS 参数修改已回滚"', '"config.yaml 修改已回滚"', 'f"{meta[\'label\']}规则修改已回滚"', '"恢复默认配置已回滚"'):
            self.assertIn(f"notify_if_rolled_back({subject}", source)
        # 收到同步规则的回滚由 notify_rule_sync_receive 去重发送，不走这里
        self.assertIn('return restart_or_rollback(rollbacks, "已同步规则：" + ", ".join(applied), "规则已写入")', source)

    def test_rule_sync_receive(self):
        calls = []
        self.app.notify_failure = lambda *a, **k: calls.append(("fail", a, k))
        self.app.notify_recovery = lambda *a, **k: calls.append(("ok", a, k))
        self.app.notify_rule_sync_receive(False, "规则校验失败，未保存：\nplugin xyz: bad line 3", "10.0.0.2")
        self.app.notify_rule_sync_receive(True, "已同步规则：force-cn", "10.0.0.2")
        self.assertEqual(calls[0][1], ("rule_sync", "warning", "收到的同步规则未能应用", ["来源：10.0.0.2", "规则校验失败，未保存", "本机规则保持不变"]))
        self.assertTrue(calls[0][2]["background"])
        self.assertEqual(calls[1][1], ("rule_sync", "规则同步已恢复", ["其他面板推送的规则已正常应用"]))
        source = APP.read_text(encoding="utf-8")
        self.assertIn('ok, message = apply_synced_rules(data.get("rules"))\n    notify_rule_sync_receive(ok, message, client_address())', source)
        # 连通性测试（空规则）直接回成功，不进入告警
        self.assertIn('if data.get("rules") == {}:\n        # 对端面板的「测试连通性」只发空规则：密钥已通过就算成功，不当作同步失败去告警\n        return jsonify({"success": True, "message": SYNC_PING_MESSAGE})', source)

    def test_routes(self):
        source = APP.read_text(encoding="utf-8")
        self.assertRegex(source, r'@app.route\("/api/notify-settings", methods=\["GET", "POST"\]\)\n@login_required\n')
        self.assertRegex(source, r'@app.route\("/api/notify-test", methods=\["POST"\]\)\n@login_required\n')
        route = source[source.index("def api_notify_settings"):source.index("def api_notify_test")]
        self.assertNotIn("NOTIFY_API_URL", route)


class NotifyCliTest(Base):
    def load_cli(self):
        module = types.ModuleType("mosctl_notify_cli")
        module.__dict__["__file__"] = str(NOTIFY_CLI)
        exec(compile(NOTIFY_CLI.read_text(encoding="utf-8"), str(NOTIFY_CLI), "exec"), module.__dict__)
        module.core = self.app
        return module

    def test_cli_dedupes_and_always_exits_zero(self):
        self.enable()
        cli = self.load_cli()
        self.assertEqual(cli.main(["--key", "geo", "--event", "fail", "Geo 规则更新失败", "geosite_cn.txt 下载失败"]), 0)
        self.assertEqual(cli.main(["--key", "geo", "--event", "fail", "Geo 规则更新失败", "geosite_cn.txt 下载失败"]), 0)
        self.assertEqual(cli.main(["--key", "geo", "--event", "ok", "Geo 规则更新已恢复", "规则文件已更新，mosdns 已重启"]), 0)
        self.assertEqual([r["body"]["title"] for r in self.requests], ["❌ 联通 · Geo 规则更新失败", "✅ 联通 · Geo 规则更新已恢复"])
        with unittest.mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(cli.main(["--bogus"]), 0)
        self.app.notify_failure = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
        self.assertEqual(cli.main(["--key", "geo", "--event", "fail", "x"]), 0)


@unittest.skipUnless(shutil.which("bash"), "需要 bash")
class GeoCliHookTest(unittest.TestCase):
    def run_update(self, body_of_do_update):
        source = CLI.read_text(encoding="utf-8")
        chunks = []
        for name in ("notify_cli", "update_geo_rules"):
            match = re.search(rf"(?ms)^{name}\(\) \{{\n.*?^\}}\n", source)
            self.assertIsNotNone(match, name)
            chunks.append(match.group(0))
        with tempfile.TemporaryDirectory() as tmp:
            bindir = Path(tmp) / "bin"
            bindir.mkdir()
            record = Path(tmp) / "calls.txt"
            fake = bindir / "python3"
            fake.write_text(f'#!/bin/bash\nfor a in "$@"; do printf "%s|" "$a"; done >> "{record}"\necho >> "{record}"\necho noisy\n', encoding="utf-8")
            fake.chmod(0o755)
            notify_cli = Path(tmp) / "notify_cli.py"
            notify_cli.write_text("", encoding="utf-8")
            script = (
                f'RED=""; PLAIN=""; NOTIFY_CLI="{notify_cli}"\n'
                + "\n".join(chunks)
                + f"\ndo_update_geo_rules() {{\n{body_of_do_update}\n}}\nupdate_geo_rules\necho rc=$?\n"
            )
            env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}")
            result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)
            calls = record.read_text(encoding="utf-8").splitlines() if record.exists() else []
            return result.stdout, calls

    def test_failure_reports_reason(self):
        out, calls = self.run_update('GEO_FAIL_REASON="geosite_cn.txt 下载失败"; return 1')
        self.assertEqual(len(calls), 1)
        args = calls[0].split("|")
        self.assertEqual(args[1:6], ["--key", "geo", "--event", "fail", "Geo 规则更新失败"])
        self.assertEqual(args[6], "geosite_cn.txt 下载失败")
        self.assertIn("===== 结果: 失败 =====", out)
        self.assertNotIn("noisy", out, "通知输出不能混进更新日志")
        self.assertTrue(out.rstrip().endswith("rc=1"))

    def test_failure_without_reason_and_success(self):
        _, calls = self.run_update("return 1")
        self.assertEqual(calls[0].split("|")[6], "更新失败，详情见更新日志")
        out, calls = self.run_update("return 0")
        self.assertEqual(calls[0].split("|")[1:6], ["--key", "geo", "--event", "ok", "Geo 规则更新已恢复"])
        self.assertIn("===== 结果: 成功 =====", out)
        self.assertTrue(out.rstrip().endswith("rc=0"))

    def test_missing_notify_script_is_skipped(self):
        source = CLI.read_text(encoding="utf-8")
        self.assertIn('NOTIFY_CLI="/etc/mosdns/manager/notify_cli.py"', source)
        self.assertIn('[ -f "$NOTIFY_CLI" ] || return 0', source)
        self.assertIn('GEO_FAIL_REASON="$(basename "$target") 下载失败"', source)
        self.assertIn('GEO_FAIL_REASON="规则已下载，但 mosdns 重启失败"', source)


class ContractTest(unittest.TestCase):
    def test_ui_card(self):
        index = INDEX.read_text(encoding="utf-8")
        view = index[index.index('id="view-operations"'):index.index('id="view-advanced"')]
        for marker in ("<h3>通知</h3>", 'id="notifyEnabled"', 'id="notifySiteName"', 'id="notifyUrl" type="password"',
                       'id="notifyUrlHint"', "发送测试通知", "sendTestNotification()", "saveNotifySettings()", 'id="notifyTestResult"'):
            self.assertIn(marker, view)
        self.assertIn("'/api/notify-settings'", index)
        self.assertIn("'/api/notify-test'", index)
        self.assertIn("loadNotifySettings();", index)
        self.assertIn("$('notifyUrl').value = '';", index, "保存后不回填地址")

    def test_logrotate_uninstall_installer(self):
        root = APP.parents[4]
        self.assertIn("/var/log/mosctl-notify.log", (root / "remote-root/etc/logrotate.d/mosdns").read_text(encoding="utf-8"))
        self.assertIn("/var/log/mosctl-notify.log", CLI.read_text(encoding="utf-8"))
        install = (root / "install.sh").read_text(encoding="utf-8")
        for key in ("SITE_NAME", "NOTIFY_ENABLED", "NOTIFY_API_URL"):
            self.assertIn(key, install)


if __name__ == "__main__":
    unittest.main()
