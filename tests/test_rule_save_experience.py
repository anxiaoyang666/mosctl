"""保存规则的体验：页面版本过期提示、保存耗时（timings）、持久结果区、重启就绪检查、不阻塞的 ExecStartPost。

后端部分 exec app.py（Flask 用桩、目录指到临时目录），沙箱/重启/systemctl/DNS 都换成假的；
前端部分是对 index.html 的契约检查。
"""
from pathlib import Path
import re
import sys
import tempfile
import time
import types
import unittest


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "remote-root" / "etc" / "mosdns" / "manager" / "app.py"
INDEX = APP.parent / "templates" / "index.html"
SERVICE = ROOT / "remote-root" / "etc" / "systemd" / "system" / "mosdns.service"
PORT = "7840"
SELF_PEER = f"http://10.10.10.9:{PORT}"


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
    module = types.ModuleType("mosctl_rule_save_experience_test")
    exec(compile(source, str(APP), "exec"), module.__dict__)
    return module


class AppTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        app = self.app = load_app(self.tmp.name)
        app.local_ipv4_addresses = lambda: {"127.0.0.1", "localhost", "10.10.10.9"}
        self.body = {"content": "qq.com\n"}
        app.request = types.SimpleNamespace(
            host_url=SELF_PEER + "/",
            method="POST",
            path="/api/rules/force-cn",
            headers={"X-Requested-With": "XMLHttpRequest"},
            get_json=lambda silent=True: self.body,
        )
        app.session = {"logged_in": True}
        app.jsonify = lambda payload: payload
        app.write_env({"WEB_PORT": PORT, "RULE_SYNC_ENABLED": "false"})

    def tearDown(self):
        self.tmp.cleanup()

    def write_rule(self, rule_id, content):
        path = Path(self.app.RULE_FILES[rule_id]["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path


class PanelVersionTest(AppTestBase):
    def test_index_renders_panel_version_into_template(self):
        rendered = []
        self.app.render_template = lambda name, **kwargs: rendered.append((name, kwargs)) or "page"
        self.assertEqual(self.app.index(), "page")
        name, kwargs = rendered[0]
        self.assertEqual(name, "index.html")
        self.assertEqual(kwargs["panel_version"], self.app.PANEL_VERSION)
        self.assertIn("rule_files", kwargs)

    def test_template_embeds_version_and_status_poll_shows_banner(self):
        html = INDEX.read_text(encoding="utf-8")
        self.assertIn('<meta name="mosctl-version" content="{{ panel_version }}">', html)
        self.assertIn("document.querySelector('meta[name=\"mosctl-version\"]')", html)
        self.assertIn('id="staleBanner"', html)
        self.assertIn("面板已升级到 v${serverVersion}，当前页面是旧版本，请刷新", html)
        banner = html[html.find('id="staleBanner"'): html.find('class="skip-link"')]
        self.assertIn("location.reload()", banner)
        self.assertIn(">刷新</button>", banner)
        self.assertIn("stale-refresh", banner)
        # setBusy 不能把刷新按钮也禁用
        set_busy = html[html.find("function setBusy"): html.find("function toast")]
        self.assertIn("stale-refresh", set_busy)
        load_status = html[html.find("async function loadStatus"): html.find("async function control")]
        self.assertIn("checkPanelVersion(res.panel_version)", load_status)
        check = html[html.find("function checkPanelVersion"): html.find("function startReloadCountdown")]
        # 旧后端没有 panel_version 字段时不误报
        self.assertIn("typeof serverVersion !== 'string' || !serverVersion || !PAGE_PANEL_VERSION", check)

    def test_api_tolerates_non_object_payloads(self):
        html = INDEX.read_text(encoding="utf-8")
        api_fn = html[html.find("async function api("): html.find("window.addEventListener('unhandledrejection'")]
        self.assertIn("if (!data || typeof data !== 'object' || Array.isArray(data)) return {};", api_fn)


class RuleSaveTimingsTest(AppTestBase):
    def test_changed_rule_reports_validate_and_restart_timings(self):
        app = self.app
        self.write_rule("force-cn", "qq.com\n")
        self.body = {"content": "qq.com\nbaidu.com\n"}

        def fake_save(rule_id, content):
            time.sleep(0.03)
            return True, "规则已保存", None

        def fake_restart(rollbacks, ok_message, prefix):
            time.sleep(0.05)
            return True, ok_message

        app.save_rule_content = fake_save
        app.restart_or_rollback = fake_restart
        result = app.api_rules("force-cn")
        self.assertTrue(result["success"])
        self.assertFalse(result["unchanged"])
        self.assertIsNone(result["sync_job"])
        self.assertEqual(result["message"], "规则已保存并重启 mosdns")
        self.assertEqual(set(result["timings"]), {"validate_ms", "restart_ms"})
        self.assertGreaterEqual(result["timings"]["validate_ms"], 25)
        self.assertGreaterEqual(result["timings"]["restart_ms"], 45)
        self.assertIsInstance(result["timings"]["restart_ms"], int)

    def test_validation_failure_still_reports_timing(self):
        app = self.app
        app.save_rule_content = lambda rule_id, content: (False, "规则校验失败，未保存：\nbad", None)
        app.restart_or_rollback = lambda *a: self.fail("校验失败不能重启")
        self.body = {"content": "qq.com\nbaidu.com\n"}
        result = app.api_rules("force-cn")
        self.assertFalse(result["success"])
        self.assertIn("规则校验失败", result["message"])
        self.assertEqual(result["timings"]["restart_ms"], 0)
        self.assertIn("validate_ms", result["timings"])

    def test_unchanged_content_skips_restart_and_still_syncs(self):
        app = self.app
        app.write_env({
            "RULE_SYNC_ENABLED": "true",
            "RULE_SYNC_TOKEN": "sync-token-for-tests",
            "RULE_SYNC_PEERS": SELF_PEER,
        })
        self.write_rule("force-cn", "qq.com\nbaidu.com\n")
        self.body = {"content": "baidu.com\n# 注释\nqq.com\n"}
        app.save_rule_content = lambda *a: self.fail("内容没变不能写文件")
        app.restart_or_rollback = lambda *a: self.fail("内容没变不能重启")
        result = app.api_rules("force-cn")
        self.assertTrue(result["success"])
        self.assertTrue(result["unchanged"])
        self.assertTrue(result["message"].startswith("规则内容没有变化，未重启"), result["message"])
        self.assertEqual(result["timings"], {"validate_ms": 0, "restart_ms": 0})
        self.assertTrue(result["sync_job"], "内容没变也要推送给其他节点")
        deadline = time.time() + 5
        while time.time() < deadline:
            job = app.find_sync_job(result["sync_job"])
            if job and job["finished_at"]:
                break
            time.sleep(0.01)
        self.assertEqual(job["results"], [{"peer": SELF_PEER, "success": True, "message": "本机（跳过）"}])


class RestartReadinessTest(AppTestBase):
    def setUp(self):
        super().setUp()
        app = self.app
        app.RESTART_READY_INTERVAL = 0
        self.commands = []
        self.restart_ok = True

        def fake_run_cmd(args, timeout=60):
            self.commands.append(args)
            if args[:2] == ["systemctl", "restart"]:
                return (True, "") if self.restart_ok else (False, "Job for mosdns.service failed")
            return True, ""

        app.run_cmd = fake_run_cmd

    def test_polls_until_active_and_dns_answers(self):
        app = self.app
        active = iter([False, False, True, True, True])
        answers = iter([(False, "www.baidu.com：timed out"), (True, "www.baidu.com：1 条应答")])
        queried = []
        app.service_active = lambda: next(active)

        def fake_dns(name, server=None, timeout=2.0):
            queried.append((name, server, timeout))
            return next(answers)

        app.dns_query = fake_dns
        ok, message = app.restart_mosdns()
        self.assertTrue(ok, message)
        self.assertRegex(message, r"耗时 \d+\.\ds")
        self.assertEqual(self.commands[:2], [["systemctl", "reset-failed", "mosdns"], ["systemctl", "restart", "mosdns"]])
        self.assertEqual(len(queried), 2, "服务 active 之前不查 DNS，查到应答就停")
        self.assertEqual(queried[0][0], app.RESTART_READY_DOMAIN)
        self.assertIsNone(queried[0][1], "走 dns_query 默认的 127.0.0.1:53")
        self.assertLessEqual(queried[0][2], 0.5)

    def test_returns_failure_when_dns_never_answers(self):
        app = self.app
        app.RESTART_READY_TIMEOUT = 0.05
        app.service_active = lambda: True
        app.dns_query = lambda name, server=None, timeout=2.0: (False, f"{name}：timed out")
        ok, message = app.restart_mosdns()
        self.assertFalse(ok)
        self.assertIn("未就绪", message)
        self.assertIn("DNS 查询失败", message)

    def test_returns_failure_when_service_not_active(self):
        app = self.app
        app.RESTART_READY_TIMEOUT = 0.05
        app.service_active = lambda: False
        app.dns_query = lambda *a, **k: self.fail("服务没起来不该查 DNS")
        ok, message = app.restart_mosdns()
        self.assertFalse(ok)
        self.assertIn("mosdns 服务不是 active", message)

    def test_systemctl_failure_skips_polling(self):
        app = self.app
        self.restart_ok = False
        app.service_active = lambda: self.fail("systemctl restart 失败时不轮询")
        ok, message = app.restart_mosdns()
        self.assertFalse(ok)
        self.assertIn("Job for mosdns.service failed", message)

    def test_not_ready_after_restart_triggers_rollback(self):
        app = self.app
        app.RESTART_READY_TIMEOUT = 0
        target = Path(self.tmp.name) / "rules" / "force-cn.txt"
        backup = Path(self.tmp.name) / "force-cn.bak"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("broken\n", encoding="utf-8")
        backup.write_text("qq.com\n", encoding="utf-8")
        app.service_active = lambda: True
        answers = iter([(False, "x：timed out"), (True, "x：1 条应答")])
        app.dns_query = lambda name, server=None, timeout=2.0: next(answers)
        ok, message = app.restart_or_rollback([(str(backup), str(target))], "规则已保存并重启 mosdns", "规则已保存")
        self.assertFalse(ok)
        self.assertIn("已回滚到修改前的文件", message)
        self.assertEqual(target.read_text(encoding="utf-8"), "qq.com\n")
        restarts = [cmd for cmd in self.commands if cmd[:2] == ["systemctl", "restart"]]
        self.assertEqual(len(restarts), 2)


class FastRestartContractTest(unittest.TestCase):
    def test_exec_start_post_does_not_block_start(self):
        text = SERVICE.read_text(encoding="utf-8")
        posts = [line for line in text.splitlines() if line.startswith("ExecStartPost=")]
        self.assertEqual(len(posts), 1)
        post = posts[0]
        self.assertNotIn("'sleep 2; ", post, "不能再在前台 sleep 2 秒")
        self.assertIn("(sleep 2; /usr/local/bin/mosctl rescue disable silent) >/dev/null 2>&1 &'", post)
        self.assertTrue(post.startswith("ExecStartPost=-/bin/sh -c "))
        # 后台进程要留在服务 cgroup 里，mosdns 2 秒内挂掉时随服务一起被杀
        self.assertNotIn("KillMode=", text)
        for line in ("StartLimitIntervalSec=60", "StartLimitBurst=5", "OnFailure=mosdns-rescue.service", "Restart=on-failure"):
            self.assertIn(line, text)

    def test_panel_upgrade_installs_service_and_reloads_systemd(self):
        text = APP.read_text(encoding="utf-8")
        targets = text[text.find("def panel_managed_targets"): text.find("def backup_panel_targets")]
        self.assertIn('"etc/systemd/system/mosdns.service"', targets)
        install = text[text.find("def install_panel_payload"): text.find("def schedule_web_restart")]
        self.assertIn('run_cmd(["systemctl", "daemon-reload"], timeout=20)', install)

    def test_sandbox_polls_api_port_every_100ms(self):
        text = APP.read_text(encoding="utf-8")
        body = text[text.find("def config_starts"): text.find("def upstream_value_error")]
        self.assertIn("if port_open(api_port):\n                api_ready = True\n                break", body)
        self.assertIn("time.sleep(0.1)", body)
        self.assertNotIn("time.sleep(0.2)", body)

    def test_restart_uses_shared_dns_helper(self):
        text = APP.read_text(encoding="utf-8")
        body = text[text.find("def wait_mosdns_ready"): text.find("def tail_lines")]
        self.assertIn("service_active()", body)
        self.assertIn("dns_query(RESTART_READY_DOMAIN", body)
        self.assertIn("RESTART_READY_TIMEOUT = 5.0", text)
        self.assertIn("RESTART_READY_INTERVAL = 0.1", text)


class RuleSaveFrontendContractTest(unittest.TestCase):
    def setUp(self):
        self.html = INDEX.read_text(encoding="utf-8")

    def fn(self, start, end):
        html = self.html
        return html[html.find(start): html.find(end)]

    def test_persistent_result_area_sits_under_editor(self):
        html = self.html
        editor = html.find('<textarea id="ruleContent"')
        area = html.find('id="ruleSaveResult"')
        self.assertGreater(area, editor)
        self.assertLess(area, html.find('id="view-operations"'))
        self.assertIn('aria-live="polite"', html[area - 80: area + 120])
        show_view = self.fn("function showView", "function renderHealth")
        self.assertIn("if (name !== activeView) clearRuleSaveResult();", show_view)

    def test_staged_progress_and_summary_texts(self):
        stages = self.fn("function startRuleSaveStages", "async function saveRule")
        self.assertIn("seconds < 2 ? '校验规则…' : '重启 mosdns…'", stages)
        self.assertIn("setInterval(tick, 1000)", stages)
        headline = self.fn("function ruleSaveHeadline", "function startRuleSaveStages")
        self.assertIn("if (res.unchanged) return '规则内容没有变化，未重启';", headline)
        self.assertIn("已保存（校验 ${formatSeconds(timings.validate_ms)}，重启 ${formatSeconds(timings.restart_ms)}）", headline)
        save = self.fn("async function saveRule", "// 规则同步在后端后台线程里进行")
        self.assertIn("clearRuleSaveResult();", save)
        self.assertIn("startRuleSaveStages()", save)
        self.assertIn("同步中 ${job.done || 0}/${job.total || 0}…", save)
        self.assertIn("summarizeSyncJob(job)", save)
        self.assertIn("seq !== ruleSaveSeq", save)
        self.assertIn("同步完成：成功 ${succeeded} / 失败 ${failed.length} / 本机跳过 ${skipped}", self.html)

    def test_sync_result_toast_stays_ten_seconds_others_default(self):
        self.assertIn("const TOAST_DEFAULT_MS = 5200;", self.html)
        self.assertIn("const SYNC_RESULT_TOAST_MS = 10000;", self.html)
        toast_fn = self.fn("function toast(", "function checkPanelVersion")
        self.assertIn("durationMs || TOAST_DEFAULT_MS", toast_fn)
        poll = self.fn("async function pollSyncJob", "async function loadSyncJobSummary")
        self.assertIn("summary.text, SYNC_RESULT_TOAST_MS)", poll)
        self.assertIn("if (job) report(job);", poll)
        self.assertIn("report(null);", poll)
        # 其他调用 toast 的地方不传时长
        other_long = [m for m in re.findall(r"toast\([^;]*SYNC_RESULT_TOAST_MS\)", self.html)]
        self.assertEqual(len(other_long), 2)


if __name__ == "__main__":
    unittest.main()


class RestartReadyUpstreamFailureTest(unittest.TestCase):
    def test_servfail_from_mosdns_counts_as_ready(self):
        source = APP.read_text(encoding="utf-8")
        body = source[source.find("def wait_mosdns_ready"):source.find("def restart_mosdns")]
        self.assertIn('"RCODE=" in detail', body)
        self.assertIn('"没有应答记录" in detail', body)

