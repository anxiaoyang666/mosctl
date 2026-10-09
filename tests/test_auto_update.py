"""服务端自动更新：auto_update.py + app.py 里的设置 / cron / 状态 / 内核安装与回滚。

exec app.py（Flask 用桩、MOSDNS_DIR 指到临时目录），再把 auto_update.py 的 core 指向它。
GitHub、systemctl、DNS 查询、沙盒校验都换成假的；mosdns 二进制用会打印版本号的小脚本代替。
"""
from pathlib import Path
import argparse
import contextlib
import io
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]
MANAGER = ROOT / "remote-root" / "etc" / "mosdns" / "manager"
APP = MANAGER / "app.py"
AUTO = MANAGER / "auto_update.py"
INDEX = MANAGER / "templates" / "index.html"
CLI = ROOT / "remote-root" / "usr" / "local" / "bin" / "mosctl"
LOGROTATE = ROOT / "remote-root" / "etc" / "logrotate.d" / "mosdns"
INSTALL = ROOT / "install.sh"
DAY = 86400
NOW = 1_800_000_000


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
    module = types.ModuleType("mosctl_auto_update_app")
    exec(compile(source, str(APP), "exec"), module.__dict__)
    module.AUTO_UPDATE_LOG = os.path.join(tmp_dir, "auto-update.log")
    return module


def load_auto(app):
    module = types.ModuleType("mosctl_auto_update_cli")
    module.__dict__["__file__"] = str(AUTO)
    exec(compile(AUTO.read_text(encoding="utf-8"), str(AUTO), "exec"), module.__dict__)
    module.core = app
    return module


def write_fake_mosdns(path, version):
    Path(path).write_text(f'#!/bin/sh\necho "{version}"\n', encoding="utf-8")
    os.chmod(path, 0o755)


def release(tag, age_days, prerelease=False, draft=False, assets=("mosdns-linux-amd64.zip",)):
    published = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - age_days * DAY))
    return {
        "tag_name": tag,
        "prerelease": prerelease,
        "draft": draft,
        "published_at": published,
        "assets": [{"name": name} for name in assets],
        "html_url": f"https://github.com/IrineSistiana/mosdns/releases/tag/{tag}",
    }


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = load_app(self.tmp.name)
        self.auto = load_auto(self.app)

    def tearDown(self):
        self.tmp.cleanup()


class ReleaseSelectionTest(Base):
    def fake_releases(self, items):
        self.app.read_url_text = lambda urls, timeout=15: (True, json.dumps(items), urls[0])
        return self.app.mosdns_stable_releases()

    def test_prerelease_and_draft_are_skipped(self):
        listing = self.fake_releases([release("v5.4.0", 30, prerelease=True), release("v5.3.9", 30, draft=True), release("v5.3.4", 30)])
        self.assertTrue(listing["success"])
        self.assertEqual([item["tag"] for item in listing["releases"]], ["v5.3.4"])

    def test_age_filtering_picks_newest_old_enough(self):
        listing = self.fake_releases([release("v5.3.5", 1), release("v5.3.4", 5), release("v5.3.3", 40)])
        plan = self.app.select_core_release(listing["releases"], "v5.3.3", 3, now=NOW, asset="mosdns-linux-amd64.zip")
        self.assertEqual(plan["action"], "update")
        self.assertEqual(plan["release"]["tag"], "v5.3.4")
        self.assertEqual(plan["newest"]["tag"], "v5.3.5")
        self.assertIn("未满 3 天", plan["note"])

    def test_too_young_release_is_skipped(self):
        listing = self.fake_releases([release("v5.3.4", 1), release("v5.3.3", 40)])
        plan = self.app.select_core_release(listing["releases"], "v5.3.3", 3, now=NOW, asset="mosdns-linux-amd64.zip")
        self.assertEqual(plan["action"], "skipped")
        self.assertIsNone(plan["release"])

    def test_never_downgrade(self):
        listing = self.fake_releases([release("v5.3.3", 40), release("v5.2.0", 400)])
        plan = self.app.select_core_release(listing["releases"], "mosdns v5.3.4", 0, now=NOW, asset="mosdns-linux-amd64.zip")
        self.assertEqual(plan["action"], "up_to_date")
        self.assertIsNone(plan["release"])

    def test_missing_asset_and_unknown_current_are_skipped(self):
        listing = self.fake_releases([release("v5.3.4", 10, assets=("mosdns-linux-arm64.zip",))])
        plan = self.app.select_core_release(listing["releases"], "v5.3.3", 3, now=NOW, asset="mosdns-linux-amd64.zip")
        self.assertEqual(plan["action"], "skipped")
        self.assertEqual(self.app.select_core_release(listing["releases"], "未知", 3, now=NOW)["action"], "skipped")

    def test_release_api_unavailable_skips_core(self):
        self.app.read_url_text = lambda urls, timeout=15: (False, "timed out", "")
        self.app.get_version = lambda: "v5.3.3"
        self.app.mosdns_asset_name = lambda: "mosdns-linux-amd64.zip"
        plan = self.auto.plan_core(self.app.read_auto_update_settings(), now=NOW)
        self.assertEqual(plan["action"], "skipped")

    def test_plan_core_records_newest_publish_time(self):
        self.fake_releases([release("v5.3.5", 1), release("v5.3.4", 10)])
        self.app.get_version = lambda: "v5.3.4"
        self.app.mosdns_asset_name = lambda: "mosdns-linux-amd64.zip"
        plan = self.auto.plan_core(self.app.read_auto_update_settings(), now=NOW)
        self.assertEqual((plan["latest"], plan["latest_eligible"]), ("v5.3.5", ""))
        self.assertEqual(plan["latest_published_at"], NOW - DAY)

    def test_release_api_goes_direct_before_proxy(self):
        urls_seen = []
        self.app.read_url_text = lambda urls, timeout=15: urls_seen.extend(urls) or (False, "x", "")
        self.app.mosdns_stable_releases()
        self.assertTrue(urls_seen[0].startswith("https://api.github.com/repos/IrineSistiana/mosdns/releases"))
        self.assertTrue(urls_seen[1].endswith(urls_seen[0]) and urls_seen[1] != urls_seen[0])


class CoreInstallTest(Base):
    def setUp(self):
        super().setUp()
        app = self.app
        self.bin = os.path.join(self.tmp.name, "mosdns-bin")
        write_fake_mosdns(self.bin, "v5.3.3")
        app.MOSDNS_BIN = self.bin
        Path(app.CONFIG_FILE).write_text("log:\n  level: info\n", encoding="utf-8")
        app.mosdns_asset_name = lambda: "mosdns-linux-amd64.zip"
        zip_source = os.path.join(self.tmp.name, "release.zip")
        with zipfile.ZipFile(zip_source, "w") as archive:
            info = zipfile.ZipInfo("mosdns")
            info.external_attr = 0o755 << 16
            archive.writestr(info, '#!/bin/sh\necho "v5.3.4"\n')
        self.downloads = []

        def fake_download(urls, target, **kwargs):
            self.downloads.append(urls)
            with open(zip_source, "rb") as src, open(target, "wb") as dst:
                dst.write(src.read())
            return True, urls[0]

        app.download_file = fake_download
        self.systemctl = []
        real_run_cmd = app.run_cmd

        def fake_run_cmd(args, timeout=60):
            if args[0] == "systemctl":
                self.systemctl.append(args[1:])
                return True, ""
            return real_run_cmd(args, timeout=timeout)

        app.run_cmd = fake_run_cmd
        app.service_active = lambda: True
        app.CORE_HEALTH_TIMEOUT = 0
        app.CORE_HEALTH_INTERVAL = 0
        app.RESTART_READY_TIMEOUT = 0
        app.RESTART_READY_INTERVAL = 0
        app.cleanup_old_backups = lambda *a, **k: None
        self.sandbox_calls = []
        app.config_starts = lambda path, wait_seconds=3.0, binary=None: self.sandbox_calls.append(binary) or (True, "ok")
        self.release = {"tag": "v5.3.4", "assets": ["mosdns-linux-amd64.zip"]}

    def bin_text(self):
        return Path(self.bin).read_text(encoding="utf-8")

    def test_sandbox_failure_aborts_and_leaves_binary_untouched(self):
        self.app.config_starts = lambda path, wait_seconds=3.0, binary=None: (False, "plugin forward: bad")
        outcome = self.app.install_mosdns_core(release=self.release)
        self.assertEqual(outcome["result"], "failed")
        self.assertIn("沙盒校验失败", outcome["message"])
        self.assertIn("v5.3.3", self.bin_text())
        self.assertEqual(self.systemctl, [], "沙盒失败时不能停服务或重启")
        self.assertEqual(os.listdir(self.app.BACKUP_DIR), [])

    def test_sandbox_runs_new_binary_and_download_uses_tag_url(self):
        self.app.dns_query = lambda name, server=None, timeout=2.0: (True, name)
        outcome = self.app.install_mosdns_core(release=self.release)
        self.assertEqual(outcome["result"], "updated", outcome["message"])
        self.assertTrue(self.sandbox_calls[0] and self.sandbox_calls[0] != self.bin)
        self.assertIn("/releases/download/v5.3.4/mosdns-linux-amd64.zip", self.downloads[0][0])
        self.assertIn("v5.3.4", self.bin_text())
        self.assertIn(["reset-failed", "mosdns"], self.systemctl)
        self.assertEqual((outcome["from"], outcome["to"]), ("v5.3.3", "v5.3.4"))

    def test_health_failure_rolls_back(self):
        queried = []

        def fake_dns(name, server=None, timeout=2.0):
            queried.append(name)
            return name != "www.google.com", name

        self.app.dns_query = fake_dns
        outcome = self.app.install_mosdns_core(release=self.release)
        self.assertEqual(outcome["result"], "rolled_back", outcome["message"])
        self.assertIn("v5.3.3", self.bin_text(), "旧内核必须被恢复")
        self.assertIn("www.baidu.com", queried)
        self.assertIn("www.google.com", queried)
        restarts = [call for call in self.systemctl if call[0] == "restart"]
        self.assertEqual(len(restarts), 2, "换新后重启一次，回滚后再重启一次")
        self.assertIn("回滚后复查", outcome["message"])

    def test_wrong_version_reported_rolls_back(self):
        self.app.dns_query = lambda name, server=None, timeout=2.0: (True, name)
        self.app.get_version = lambda: "v5.3.3"
        outcome = self.app.install_mosdns_core(release=self.release)
        self.assertEqual(outcome["result"], "rolled_back")
        self.assertIn("预期 v5.3.4", outcome["message"])

    def test_service_inactive_rolls_back(self):
        self.app.service_active = lambda: False
        outcome = self.app.install_mosdns_core(release=self.release)
        self.assertEqual(outcome["result"], "rolled_back")
        self.assertIn("v5.3.3", self.bin_text())

    def test_never_downgrade_in_install(self):
        outcome = self.app.install_mosdns_core(release={"tag": "v5.3.3", "assets": ["mosdns-linux-amd64.zip"]})
        self.assertEqual(outcome["result"], "up_to_date")
        self.assertEqual(self.downloads, [])

    def test_manual_upgrade_wrapper_keeps_tuple_contract(self):
        self.app.latest_mosdns_release = lambda: {"success": True, "latest": "v5.3.4", "asset_available": True}
        self.app.dns_query = lambda name, server=None, timeout=2.0: (True, name)
        ok, message = self.app.upgrade_mosdns_core()
        self.assertTrue(ok, message)
        self.assertIn("/releases/latest/download/", self.downloads[0][0])

    def test_notification_summaries(self):
        # 成功：健康检查通过一行
        self.app.dns_query = lambda name, server=None, timeout=2.0: (True, name)
        outcome = self.app.install_mosdns_core(release=self.release)
        self.assertEqual(outcome["summary"], ["健康检查通过：服务、国内解析、国外解析"])

    def test_rollback_summary_names_failed_check(self):
        write_fake_mosdns(self.bin, "v5.3.3")
        calls = {"google": 0}

        def fake_dns(name, server=None, timeout=2.0):
            if name == "www.google.com":
                calls["google"] += 1
                return calls["google"] > 1, name  # 新内核失败，回滚后复查通过
            return True, name

        self.app.dns_query = fake_dns
        outcome = self.app.install_mosdns_core(release=self.release)
        self.assertEqual(outcome["result"], "rolled_back")
        self.assertEqual(outcome["summary"], ["健康检查未通过：国外解析", "已恢复旧版本"])

    def test_sandbox_failure_summary(self):
        self.app.config_starts = lambda path, wait_seconds=3.0, binary=None: (False, "plugin forward: bad")
        outcome = self.app.install_mosdns_core(release=self.release)
        self.assertEqual(outcome["summary"], ["新内核无法用当前配置启动", "现有内核未改动"])

    def test_health_failure_summary_labels(self):
        summary = self.app.health_failure_summary
        self.assertEqual(summary("健康检查失败：\nmosdns 服务不是 active"), "健康检查未通过：服务")
        self.assertEqual(
            summary("x\nDNS 查询失败：www.baidu.com：超时\nDNS 查询失败：www.google.com：超时"),
            "健康检查未通过：国内解析、国外解析",
        )
        self.assertEqual(summary("mosdns version 报告 v5.3.3，预期 v5.3.4"), "健康检查未通过：版本")
        self.assertEqual(summary("Job for mosdns.service failed"), "新内核启动失败")


class DnsQueryTest(Base):
    def test_real_udp_query_against_local_responder(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        server.bind(("127.0.0.1", 0))
        port = server.getsockname()[1]
        packets = []

        def respond():
            data, addr = server.recvfrom(4096)
            packets.append(data)
            query_id = struct.unpack("!H", data[:2])[0]
            header = struct.pack("!HHHHHH", query_id, 0x8180, 1, 1, 0, 0)
            server.sendto(header + data[12:], addr)

        thread = threading.Thread(target=respond)
        thread.start()
        ok, detail = self.app.dns_query("www.baidu.com", server=("127.0.0.1", port), timeout=2)
        thread.join(3)
        server.close()
        self.assertTrue(ok, detail)
        self.assertIn(b"\x03www\x05baidu\x03com\x00", packets[0])

    def test_answer_parsing(self):
        parse = self.app.parse_dns_answer_count
        self.assertFalse(parse(struct.pack("!HHHHHH", 7, 0x8183, 1, 0, 0, 0), 7)[0], "NXDOMAIN")
        self.assertFalse(parse(struct.pack("!HHHHHH", 7, 0x8180, 1, 0, 0, 0), 7)[0], "没有应答")
        self.assertFalse(parse(struct.pack("!HHHHHH", 8, 0x8180, 1, 1, 0, 0), 7)[0], "ID 不符")
        self.assertTrue(parse(struct.pack("!HHHHHH", 7, 0x8180, 1, 2, 0, 0), 7)[0])

    def test_no_server_fails_quickly(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        ok, _ = self.app.dns_query("www.google.com", server=("127.0.0.1", port), timeout=0.3)
        sock.close()
        self.assertFalse(ok)


class LockTest(Base):
    def test_single_instance_lock(self):
        first = self.app.acquire_auto_update_lock()
        self.assertIsNotNone(first)
        try:
            self.assertIsNone(self.app.acquire_auto_update_lock())
            self.assertTrue(self.app.auto_update_running())
            calls = []
            self.auto.run = lambda args, now=None: calls.append(args) or (0, [])
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(self.auto.main([]), 2)
            self.assertEqual(calls, [], "已有实例时不能再跑")
        finally:
            self.app.release_auto_update_lock(first)
        self.assertFalse(self.app.auto_update_running())


class CronTest(Base):
    GEO = "0 2 * * * /usr/local/bin/mosctl update >> /var/log/mosctl-update.log 2>&1"

    def install_fake_crontab(self, initial):
        self.crontab = list(initial)
        self.writes = 0
        real_run = self.app.subprocess.run

        def fake_run(args, **kwargs):
            if args == ["crontab", "-l"]:
                return types.SimpleNamespace(returncode=0, stdout="\n".join(self.crontab) + "\n", stderr="")
            if args == ["crontab", "-"]:
                self.writes += 1
                self.crontab = kwargs["input"].splitlines()
                return types.SimpleNamespace(returncode=0, stdout="", stderr="")
            return real_run(args, **kwargs)

        self.app.subprocess.run = fake_run
        self.addCleanup(setattr, self.app.subprocess, "run", real_run)

    def test_add_and_remove_without_touching_geo_line(self):
        self.install_fake_crontab([self.app.GEO_CRON_COMMENT, self.GEO])
        ok, _ = self.app.write_auto_update_cron({"enabled": True, "time": "03:25"})
        self.assertTrue(ok)
        auto_lines = [line for line in self.crontab if "MOSCTL_AUTO_UPDATE" in line]
        self.assertEqual(len(auto_lines), 1)
        self.assertTrue(auto_lines[0].startswith("25 3 * * * python3 "))
        self.assertTrue(auto_lines[0].endswith(f"auto_update.py >> {self.app.AUTO_UPDATE_LOG} 2>&1 # MOSCTL_AUTO_UPDATE"))
        self.assertIn(self.GEO, self.crontab)

        writes = self.writes
        self.assertTrue(self.app.write_auto_update_cron({"enabled": True, "time": "03:25"})[0])
        self.assertEqual(self.writes, writes, "没有变化时不写 crontab")

        self.app.write_auto_update_cron({"enabled": True, "time": "05:00"})
        self.assertEqual(len([line for line in self.crontab if "MOSCTL_AUTO_UPDATE" in line]), 1)

        self.app.write_auto_update_cron({"enabled": False, "time": "05:00"})
        self.assertEqual(self.crontab, [self.app.GEO_CRON_COMMENT, self.GEO])

    def test_geo_schedule_save_keeps_auto_update_line(self):
        self.install_fake_crontab([self.GEO])
        self.app.write_auto_update_cron({"enabled": True, "time": "04:10"})
        ok, _ = self.app.write_geo_schedule({"mode": "daily", "time": "06:30"})
        self.assertTrue(ok)
        self.assertEqual(len([line for line in self.crontab if "MOSCTL_AUTO_UPDATE" in line]), 1)
        self.assertIn("30 6 * * * /usr/local/bin/mosctl update >> /var/log/mosctl-update.log 2>&1", self.crontab)
        self.assertFalse(self.app.is_geo_update_cron(next(line for line in self.crontab if "MOSCTL_AUTO_UPDATE" in line)))

    def test_unreadable_crontab_is_not_overwritten(self):
        self.writes = 0
        real_run = self.app.subprocess.run

        def fake_run(args, **kwargs):
            if args == ["crontab", "-l"]:
                return types.SimpleNamespace(returncode=1, stdout="", stderr="crontab: permission denied")
            self.writes += 1
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        self.app.subprocess.run = fake_run
        self.addCleanup(setattr, self.app.subprocess, "run", real_run)
        ok, message = self.app.write_auto_update_cron({"enabled": True, "time": "04:10"})
        self.assertFalse(ok)
        self.assertEqual(self.writes, 0)

    def test_empty_crontab_gets_line_and_startup_uses_env(self):
        self.install_fake_crontab([])
        self.crontab = []
        real_run = self.app.subprocess.run

        def fake_run(args, **kwargs):
            if args == ["crontab", "-l"] and not self.crontab:
                return types.SimpleNamespace(returncode=1, stdout="", stderr="no crontab for root")
            return real_run(args, **kwargs)

        self.app.subprocess.run = fake_run
        self.app.write_env({"AUTO_UPDATE_TIME": "01:05"})
        self.app.panel_startup_tasks()
        self.assertTrue(any(line.startswith("5 1 * * * ") and "MOSCTL_AUTO_UPDATE" in line for line in self.crontab), self.crontab)

    def test_saving_settings_writes_env_and_cron(self):
        self.install_fake_crontab([self.GEO])
        ok, message = self.app.save_auto_update_settings(
            {"enabled": False, "time": "02:40", "core_min_age_days": "7", "panel_min_age_days": 2}
        )
        self.assertTrue(ok, message)
        env = self.app.read_env()
        self.assertEqual(env["AUTO_UPDATE_ENABLED"], "false")
        self.assertEqual(env["AUTO_UPDATE_CORE_MIN_AGE_DAYS"], "7")
        self.assertEqual(self.crontab, [self.GEO])


class SettingsTest(Base):
    def test_defaults(self):
        settings = self.app.read_auto_update_settings({})
        self.assertEqual(settings, {"enabled": True, "time": "04:10", "core_min_age_days": 3, "panel_min_age_days": 0})

    def test_bad_env_values_fall_back_to_defaults(self):
        settings = self.app.read_auto_update_settings(
            {"AUTO_UPDATE_TIME": "25:00", "AUTO_UPDATE_CORE_MIN_AGE_DAYS": "-1", "AUTO_UPDATE_PANEL_MIN_AGE_DAYS": "abc", "AUTO_UPDATE_ENABLED": "false"}
        )
        self.assertEqual(settings, {"enabled": False, "time": "04:10", "core_min_age_days": 3, "panel_min_age_days": 0})

    def test_validation(self):
        good = {"enabled": True, "time": "23:59", "core_min_age_days": "0", "panel_min_age_days": 365}
        updates, error = self.app.validate_auto_update_settings(good)
        self.assertIsNone(error)
        self.assertEqual(updates["AUTO_UPDATE_PANEL_MIN_AGE_DAYS"], "365")
        for bad in (
            dict(good, enabled="yes"),
            dict(good, time="4:10"),
            dict(good, time="24:00"),
            dict(good, time=""),
            dict(good, core_min_age_days="-1"),
            dict(good, core_min_age_days="1.5"),
            dict(good, core_min_age_days=True),
            dict(good, panel_min_age_days="366"),
            dict(good, panel_min_age_days='1"; rm -rf /'),
        ):
            updates, error = self.app.validate_auto_update_settings(bad)
            self.assertIsNone(updates, bad)
            self.assertTrue(error)
        ok, message = self.app.save_auto_update_settings(dict(good, time="99:99"))
        self.assertFalse(ok)
        self.assertNotIn("AUTO_UPDATE_TIME", self.app.read_env())


class PanelPlanTest(Base):
    def setUp(self):
        super().setUp()
        self.app.PANEL_VERSION = "0.3.38"
        self.app.remote_panel_version = lambda settings=None, force=False: {"success": True, "latest_version": "0.3.40"}

    def settings(self, panel_age):
        return dict(self.app.read_auto_update_settings({}), panel_min_age_days=panel_age)

    def test_commits_api_unavailable_skips_panel(self):
        self.app.read_url_text = lambda urls, timeout=15: (False, "HTTP Error 403: rate limit", "")
        plan = self.auto.plan_panel(self.settings(2), now=NOW)
        self.assertEqual(plan["action"], "skipped")
        self.assertIn("GitHub 提交接口不可用", plan["note"])

    def test_commits_api_url_and_age_gate(self):
        seen = []
        sha = "a" * 40

        def fake_read(urls, timeout=15):
            seen.extend(urls)
            date = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - 1 * DAY))
            return True, json.dumps([{"sha": sha, "commit": {"committer": {"date": date}}}]), urls[0]

        self.app.read_url_text = fake_read
        plan = self.auto.plan_panel(self.settings(2), now=NOW)
        self.assertEqual(plan["action"], "skipped")
        self.assertIn("未满 2 天", plan["note"])
        self.assertEqual(
            seen[0],
            "https://api.github.com/repos/anxiaoyang666/mosctl/commits?path=remote-root&sha=main&per_page=1",
        )
        self.assertTrue(seen[1].endswith(seen[0]) and seen[1] != seen[0], "直连失败后才走 GH_PROXY")

        plan = self.auto.plan_panel(self.settings(1), now=NOW + 60)
        self.assertEqual(plan["action"], "update")
        self.assertEqual(plan["ref"], sha)
        self.assertEqual(plan["target"], "v0.3.40")

    def test_zero_age_does_not_need_commits_api(self):
        self.app.latest_panel_commit = lambda settings=None: self.fail("0 天不需要查提交")
        plan = self.auto.plan_panel(self.settings(0), now=NOW)
        self.assertEqual(plan["action"], "update")
        self.assertIsNone(plan["ref"])

    def test_up_to_date_and_remote_unknown(self):
        self.app.remote_panel_version = lambda settings=None, force=False: {"success": True, "latest_version": "0.3.38"}
        self.assertEqual(self.auto.plan_panel(self.settings(0), now=NOW)["action"], "up_to_date")
        self.app.remote_panel_version = lambda settings=None, force=False: {"success": True, "latest_version": "0.3.10"}
        self.assertEqual(self.auto.plan_panel(self.settings(0), now=NOW)["action"], "up_to_date", "绝不降级")
        self.app.remote_panel_version = lambda settings=None, force=False: {"success": False, "message": "检测失败"}
        self.assertEqual(self.auto.plan_panel(self.settings(0), now=NOW)["action"], "skipped")

    def test_commit_archive_url(self):
        sha = "b" * 40
        self.assertEqual(
            self.app.github_commit_archive_url("https://github.com/anxiaoyang666/mosctl.git", sha),
            f"https://github.com/anxiaoyang666/mosctl/archive/{sha}.zip",
        )
        self.assertEqual(self.app.github_commit_archive_url("https://github.com/a/b.git", "main"), "")

    def test_started_is_recorded_before_upgrade_and_finished_on_startup(self):
        states_during_upgrade = []

        def fake_upgrade(ref=None, on_install=None):
            states_during_upgrade.append(self.app.read_auto_update_state()["panel"].get("last_result"))
            on_install("0.3.40")
            return True, "升级完成", True

        self.app.upgrade_mosctl_panel = fake_upgrade
        plan = self.auto.plan_panel(self.settings(0), now=NOW)
        result, _ = self.auto.run_panel(plan, dry_run=False)
        self.assertEqual(result, "started")
        self.assertEqual(states_during_upgrade, ["started"])
        panel = self.app.read_auto_update_state()["panel"]
        self.assertEqual((panel["last_result"], panel["to"]), ("started", "v0.3.40"))

        # 新面板启动
        self.app.PANEL_VERSION = "0.3.40"
        self.assertTrue(self.app.finish_panel_auto_update())
        panel = self.app.read_auto_update_state()["panel"]
        self.assertEqual(panel["last_result"], "updated")

    def test_startup_marks_failed_when_version_did_not_change(self):
        self.app.update_auto_update_item("panel", last_result="started", to="v0.3.40")
        self.assertTrue(self.app.finish_panel_auto_update())
        self.assertEqual(self.app.read_auto_update_state()["panel"]["last_result"], "failed")

    def test_startup_notifies_panel_result(self):
        sent = []
        self.app.notify_event = lambda level, subject, lines=None, background=False, env=None: sent.append((level, subject, lines))
        self.app.update_auto_update_item("panel", last_result="started", to="v0.3.40", **{"from": "v0.3.38"})
        self.app.PANEL_VERSION = "0.3.40"
        self.app.finish_panel_auto_update()
        self.assertEqual(sent, [("success", "管理面板已更新", ["v0.3.38 → v0.3.40", "面板已重启并运行新版本"])])
        sent.clear()
        self.app.PANEL_VERSION = "0.3.38"
        self.app.update_auto_update_item("panel", last_result="started", to="v0.3.40", **{"from": "v0.3.38"})
        self.app.finish_panel_auto_update()
        self.assertEqual(sent, [("failure", "管理面板更新失败", ["v0.3.38 → v0.3.40", "重启后仍是 v0.3.38", "继续运行当前版本"])])

    def test_upgrade_failure_is_recorded(self):
        self.app.upgrade_mosctl_panel = lambda ref=None, on_install=None: (False, "下载失败", False)
        plan = self.auto.plan_panel(self.settings(0), now=NOW)
        self.assertEqual(self.auto.run_panel(plan, dry_run=False)[0], "failed")
        self.assertEqual(self.app.read_auto_update_state()["panel"]["last_result"], "failed")


class RunOrderTest(Base):
    def args(self, **kwargs):
        return argparse.Namespace(dry_run=kwargs.get("dry_run", False), only=kwargs.get("only"), manual=kwargs.get("manual", False))

    def install_fakes(self):
        calls = []
        self.auto.plan_core = lambda settings, now=None: calls.append("plan_core") or {
            "item": "core", "action": "update", "current": "v5.3.3", "latest": "v5.3.4", "latest_eligible": "v5.3.4",
            "release": {"tag": "v5.3.4", "assets": []}, "note": "可更新",
        }
        self.auto.plan_panel = lambda settings, now=None: calls.append("plan_panel") or {
            "item": "panel", "action": "update", "current": "v0.3.38", "latest": "v0.3.39", "latest_eligible": "v0.3.39",
            "ref": None, "target": "v0.3.39", "note": "可更新",
        }
        self.app.install_mosdns_core = lambda release=None: calls.append("install_core") or {
            "result": "updated", "message": "ok", "from": "v5.3.3", "to": "v5.3.4"
        }
        self.app.upgrade_mosctl_panel = lambda ref=None, on_install=None: calls.append("upgrade_panel") or (True, "ok", True)
        self.app.get_version = lambda: "v5.3.4"
        return calls

    def test_core_first_panel_last(self):
        calls = self.install_fakes()
        code, report = self.auto.run(self.args())
        self.assertEqual(code, 0)
        self.assertEqual(calls, ["plan_core", "install_core", "plan_panel", "upgrade_panel"])
        state = self.app.read_auto_update_state()
        self.assertEqual(state["core"]["last_result"], "updated")
        self.assertTrue(state["last_run"]["finished_at"])
        self.assertIn("开始定时更新", Path(self.app.AUTO_UPDATE_LOG).read_text(encoding="utf-8"))

    def test_dry_run_installs_nothing(self):
        calls = self.install_fakes()
        code, report = self.auto.run(self.args(dry_run=True))
        self.assertEqual(calls, ["plan_core", "plan_panel"])
        state = self.app.read_auto_update_state()
        self.assertNotIn("last_result", state["core"])
        self.assertEqual(state["core"]["latest_eligible"], "v5.3.4")
        self.assertIn("MosDNS 内核：可更新", report[0])

    def test_only_and_disabled(self):
        calls = self.install_fakes()
        self.auto.run(self.args(only="panel"))
        self.assertEqual(calls, ["plan_panel", "upgrade_panel"])
        calls.clear()
        self.app.write_env({"AUTO_UPDATE_ENABLED": "false"})
        self.auto.run(self.args())
        self.assertEqual(calls, [], "关闭后定时任务不执行")
        self.auto.run(self.args(manual=True, only="core"))
        self.assertEqual(calls, ["plan_core", "install_core"], "面板里手动点“立即更新”仍执行")

    def capture_notifications(self):
        sent = []
        self.app.notify_event = lambda level, subject, lines=None, background=False, env=None: sent.append((level, subject, lines))
        return sent

    def test_notifications_per_item(self):
        self.install_fakes()
        sent = self.capture_notifications()
        self.app.install_mosdns_core = lambda release=None: {
            "result": "updated", "message": "ok", "from": "v5.3.3", "to": "v5.3.4",
            "summary": ["健康检查通过：服务、国内解析、国外解析"],
        }
        self.auto.run(self.args())
        # 面板 started 不发，等新面板启动后发
        self.assertEqual(sent, [("success", "mosdns 内核已更新", ["v5.3.3 → v5.3.4", "健康检查通过：服务、国内解析、国外解析"])])

        sent.clear()
        self.app.install_mosdns_core = lambda release=None: {
            "result": "rolled_back", "message": "x", "from": "v5.3.3", "to": "v5.3.4",
            "summary": ["健康检查未通过：国外解析", "已恢复旧版本"],
        }
        self.app.upgrade_mosctl_panel = lambda ref=None, on_install=None: (False, "下载面板源码失败：\nconnection refused", False)
        self.auto.run(self.args())
        self.assertEqual(sent, [
            ("failure", "mosdns 内核更新失败，已回滚", ["v5.3.3 → v5.3.4", "健康检查未通过：国外解析", "已恢复旧版本"]),
            ("failure", "管理面板更新失败", ["v0.3.38 → v0.3.39", "下载面板源码失败", "继续运行 v0.3.38"]),
        ])

    def test_no_notifications_for_dry_run_or_up_to_date(self):
        self.install_fakes()
        sent = self.capture_notifications()
        self.auto.run(self.args(dry_run=True))
        self.app.install_mosdns_core = lambda release=None: {"result": "up_to_date", "message": "x", "from": "v5.3.3", "to": "v5.3.3"}
        self.app.upgrade_mosctl_panel = lambda ref=None, on_install=None: (True, "已是最新", False)
        self.auto.run(self.args())
        self.assertEqual(sent, [])

    def test_exception_notifies_without_internal_text(self):
        self.install_fakes()
        sent = self.capture_notifications()

        def boom(settings, now=None):
            raise RuntimeError("Traceback secret")

        self.auto.plan_core = boom
        self.auto.run(self.args(only="core"))
        self.assertEqual(sent, [("failure", "mosdns 内核更新失败", ["检查或安装时出错", "详情见自动更新日志"])])

    def test_rollback_sets_exit_code_and_core_error_does_not_block_panel(self):
        calls = self.install_fakes()
        self.app.install_mosdns_core = lambda release=None: {"result": "rolled_back", "message": "健康检查失败", "from": "v5.3.3", "to": "v5.3.4"}
        code, _ = self.auto.run(self.args())
        self.assertEqual(code, 1)
        self.assertEqual(self.app.read_auto_update_state()["core"]["last_result"], "rolled_back")

        def boom(settings, now=None):
            raise RuntimeError("boom")

        self.auto.plan_core = boom
        calls.clear()
        code, _ = self.auto.run(self.args())
        self.assertEqual(code, 1)
        self.assertIn("upgrade_panel", calls)


class ContractTest(unittest.TestCase):
    def test_entry_point_does_not_start_flask(self):
        source = AUTO.read_text(encoding="utf-8")
        self.assertIn("import app as module", source)
        self.assertNotIn("app.run(", source)
        self.assertIn('"--dry-run"', source)
        self.assertIn('choices=("core", "panel")', source)
        self.assertIn("acquire_auto_update_lock", source)
        app_source = APP.read_text(encoding="utf-8")
        self.assertRegex(app_source, r'if __name__ == "__main__":\n    panel_startup_tasks\(\)')
        self.assertRegex(app_source, r"import fcntl")

    def test_routes_are_behind_login_and_operation_lock(self):
        source = APP.read_text(encoding="utf-8")
        for route in ('"/api/auto-update", methods=["GET", "POST"]', '"/api/auto-update/check", methods=["POST"]', '"/api/auto-update/run", methods=["POST"]'):
            self.assertRegex(source, re.escape(f"@app.route({route})") + r"\n@login_required\n@operation_locked\n")

    def test_ui_card(self):
        index = INDEX.read_text(encoding="utf-8")
        view = index[index.index('id="view-operations"'):index.index('id="view-advanced"')]
        for marker in (
            "<h3>自动更新</h3>", 'id="autoUpdateEnabled"', 'id="autoUpdateTime"', 'id="autoUpdateCoreMinAge"',
            'id="autoUpdatePanelMinAge"', 'id="autoUpdateTimeNote"', "立即检查（不安装）", "confirmRunAutoUpdate()",
            'id="autoUpdateCoreResult"', 'id="autoUpdatePanelResult"',
        ):
            self.assertIn(marker, view)
        self.assertIn("'/api/auto-update/check'", index)
        self.assertIn("'/api/auto-update/run'", index)
        self.assertIn("DNS 会中断 1–2 秒", index)
        self.assertIn("function serverTimeToBrowserLocal", index)
        self.assertIn("startAutoUpdatePolling()", index)
        self.assertIn("loadAutoUpdate();", index)

    def auto_update_card(self):
        index = INDEX.read_text(encoding="utf-8")
        start = index.index('<section class="panel" id="autoUpdateCard">')
        return index[start:index.index("</section>", start)]

    def test_ui_card_table_uses_theme_variables(self):
        card = self.auto_update_card()
        self.assertIn('class="update-table"', card)
        self.assertIn("<th>项目</th><th>版本</th><th>上次检查</th>", card)
        css = INDEX.read_text(encoding="utf-8")
        rules = "\n".join(line for line in css.splitlines() if ".update-table" in line)
        self.assertIn("var(--surface-2)", rules)
        self.assertIn("var(--line)", rules)
        for text in (card, rules):
            self.assertNotRegex(text.lower(), r"background[^;\"]*(#fff\b|#ffffff|white)")

    def test_ui_card_labels_are_plain(self):
        card = self.auto_update_card()
        self.assertIn("内核发布满几天才更新", card)
        self.assertIn("面板发布满几天才更新", card)
        self.assertIn("新版面板发布后等几天再更新，0 表示有新版就更新", card)
        for jargon in ("remote-root", "提交", "分支"):
            self.assertNotIn(jargon, card)

    def run_describe_cases(self, cases):
        node = shutil.which("node")
        if not node:
            self.skipTest("node 不可用")
        index = INDEX.read_text(encoding="utf-8")
        pieces = []
        for pattern in (
            r"(?ms)^    const AUTO_UPDATE_RESULT_LABELS = \{.*?^    \};\n",
            r"(?ms)^    function compareAutoUpdateVersions\(.*?^    \}\n",
            r"(?ms)^    function describeAutoUpdateItem\(.*?^    \}\n",
        ):
            match = re.search(pattern, index)
            self.assertIsNotNone(match, pattern)
            pieces.append(match.group(0))
        script = "\n".join(pieces) + (
            "\nconst cases = JSON.parse(process.argv[1]);"
            "\nprocess.stdout.write(JSON.stringify(cases.map(c => describeAutoUpdateItem(c[0], c[1], c[2], c[3]))));\n"
        )
        out = subprocess.run([node, "-e", script, json.dumps(cases)], capture_output=True, text=True, check=True).stdout
        return json.loads(out)

    def test_describe_auto_update_item_cases(self):
        now = 1_800_000_000
        core_state = {"current": "v5.3.4", "from": "v5.3.4", "to": "v5.3.4", "latest": "v5.3.4", "latest_eligible": "",
                      "last_result": "up_to_date", "last_result_at": now - 60, "last_check": now - 60,
                      "message": "当前 v5.3.4 已是最新稳定版", "note": "当前 v5.3.4 已是最新稳定版"}
        panel_state = {"current": "v0.3.38", "from": "v0.3.38", "to": "v0.3.38", "latest": "v0.3.38", "latest_eligible": "",
                       "last_result": "up_to_date", "last_result_at": now - 60, "last_check": now - 60,
                       "message": "当前 v0.3.38 已是最新（远端 v0.3.38）", "note": "当前 v0.3.38 已是最新（远端 v0.3.38）"}
        eligible = dict(core_state, current="v5.3.3", latest="v5.3.5", latest_eligible="v5.3.5", last_result="skipped",
                        message="x", note="x")
        waiting = dict(core_state, latest="v5.3.5", latest_published_at=now - 86400, last_result="skipped",
                       message="v5.3.5 发布 1.0 天，未满 3 天", note="v5.3.5 发布 1.0 天，未满 3 天")
        failed = dict(core_state, current="v5.3.3", latest="v5.3.4", latest_eligible="v5.3.4", last_result="failed",
                      **{"from": "v5.3.3", "to": "v5.3.4"}, message="下载失败", note="可更新到 v5.3.4")
        views = self.run_describe_cases([
            [core_state, "v5.3.4", 3, now],
            [panel_state, "0.3.40", 0, now],
            [eligible, "v5.3.3", 3, now],
            [waiting, "v5.3.4", 3, now],
            [dict(waiting, latest_published_at=None), "v5.3.4", 3, now],
            [failed, "v5.3.3", 3, now],
            [{}, "v5.3.4", 3, now],
        ])
        # 已是最新：message 与 note 相同，不重复显示
        self.assertEqual((views[0]["current"], views[0]["status"], views[0]["warn"], views[0]["detail"]), ("v5.3.4", "已是最新", False, ""))
        self.assertEqual(views[0]["resultLabel"], "已是最新")
        # 检查后手动升级：显示实际版本，标注手动升级，不再显示过时的"当前 v0.3.38 / 远端 v0.3.38"
        self.assertEqual(views[1]["current"], "v0.3.40")
        self.assertEqual(views[1]["status"], "已是最新（上次检查后已手动升级）")
        self.assertEqual(views[1]["detail"], "")
        self.assertNotIn("v0.3.38", json.dumps(views[1], ensure_ascii=False))
        # 有可用新版：警告样式
        self.assertEqual((views[2]["status"], views[2]["warn"]), ("可更新到 v5.3.5", True))
        # 新版已发布但未满天数：给出剩余天数；没有发布时间时不写剩余天数
        self.assertEqual(views[3]["status"], "v5.3.5 已发布，满 3 天后自动更新（还差 2 天）")
        self.assertEqual(views[3]["detail"], "")
        self.assertEqual(views[4]["status"], "v5.3.5 已发布，满 3 天后自动更新")
        # 失败：结果带版本变化，message 与 note 不同才显示
        self.assertEqual(views[5]["resultLabel"], "失败 v5.3.3 → v5.3.4")
        self.assertEqual(views[5]["detail"], "下载失败")
        # 没有任何记录
        self.assertEqual((views[6]["current"], views[6]["status"], views[6]["resultLabel"]), ("v5.3.4", "", "还没有记录"))

    def test_uninstall_logrotate_and_installer(self):
        cli = CLI.read_text(encoding="utf-8")
        self.assertIn('grep -v "MOSCTL_AUTO_UPDATE"', cli)
        self.assertIn("/var/log/mosctl-auto-update.log", cli)
        self.assertIn("/var/log/mosctl-auto-update.log", LOGROTATE.read_text(encoding="utf-8"))
        install = INSTALL.read_text(encoding="utf-8")
        for key in ("AUTO_UPDATE_ENABLED", "AUTO_UPDATE_TIME", "AUTO_UPDATE_CORE_MIN_AGE_DAYS", "AUTO_UPDATE_PANEL_MIN_AGE_DAYS"):
            self.assertIn(key, install)

    def test_panel_version(self):
        self.assertIn('PANEL_VERSION = "0.3.50"', APP.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
