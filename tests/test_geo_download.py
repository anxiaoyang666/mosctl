"""Geo 规则下载：镜像依次回退、jsDelivr 地址、失败后重试一次、旧规则还在时通知降级。"""
from pathlib import Path
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "remote-root" / "usr" / "local" / "bin" / "mosctl"

DIRECT = "https://raw.githubusercontent.com/Loyalsoldier/v2ray-rules-dat/release/direct-list.txt"
PROXY_LIST = "https://raw.githubusercontent.com/Loyalsoldier/v2ray-rules-dat/release/proxy-list.txt"
GEOIP = "https://raw.githubusercontent.com/Loyalsoldier/geoip/release/text/cn.txt"
APPLE = "https://raw.githubusercontent.com/Loyalsoldier/v2ray-rules-dat/release/apple-cn.txt"
JSDELIVR_DIRECT = "https://cdn.jsdelivr.net/gh/Loyalsoldier/v2ray-rules-dat@release/direct-list.txt"


def cli_functions(*names):
    source = CLI.read_text(encoding="utf-8")
    chunks = []
    for name in names:
        match = re.search(rf"(?ms)^{re.escape(name)}\(\) \{{\n.*?^\}}\n", source)
        if not match:
            raise AssertionError(name)
        chunks.append(match.group(0))
    return "\n".join(chunks)


FETCH_FUNCS = (
    "env_get", "env_has",
    "normalize_proxy_prefix", "apply_github_proxy", "geo_url", "jsdelivr_url",
    "github_mirror_prefixes", "wget_fetch", "fetch_url",
)
UPDATE_FUNCS = (
    "env_get", "env_has", "config_uses_rule",
    "geo_failure_is_download", "geo_should_retry", "geo_retry_delay_seconds", "geo_retry_sleep",
    "geo_kept_old_rules", "notify_cli", "update_geo_rules",
)


def bash(script, env):
    started = time.monotonic()
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)
    result.elapsed = time.monotonic() - started
    return result


@unittest.skipUnless(shutil.which("bash"), "需要 bash")
class FetchUrlTest(unittest.TestCase):
    def run_fetch(self, source, allow=(), gh_proxy="", gh_mirrors=None, env_file=None):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bindir = tmp / "bin"
            bindir.mkdir()
            log = tmp / "urls.txt"
            allow_file = tmp / "allow.txt"
            allow_file.write_text("\n".join(allow) + ("\n" if allow else ""), encoding="utf-8")
            output = tmp / "out.txt"
            wget = bindir / "wget"
            wget.write_text(
                "#!/bin/bash\n"
                "out=\"\"\nprev=\"\"\nurl=\"\"\n"
                "for arg in \"$@\"; do\n"
                "  if [ \"$prev\" = \"-O\" ]; then out=\"$arg\"; fi\n"
                "  prev=\"$arg\"\n"
                "  url=\"$arg\"\n"
                "done\n"
                f'printf "%s\\n" "$url" >> "{log}"\n'
                f'if grep -Fxq "$url" "{allow_file}"; then printf "rule-data\\n" > "$out"; exit 0; fi\n'
                "exit 1\n",
                encoding="utf-8",
            )
            wget.chmod(0o755)
            dotenv = env_file if env_file is not None else tmp / "no.env"
            script = (
                'WGET_TIMEOUT_OPTS=(--timeout=20 --tries=2)\n'
                'YELLOW=""; PLAIN=""; RED=""; GREEN=""\n'
                f'ENV_FILE="{dotenv}"\n'
                f'GH_PROXY="{gh_proxy}"\n'
                + ("unset GH_MIRRORS\n" if gh_mirrors is None else f'GH_MIRRORS="{gh_mirrors}"\n')
                + cli_functions(*FETCH_FUNCS)
                + f'\nfetch_url "{output}" "{source}"\necho rc=$?\n'
            )
            env = {"PATH": f"{bindir}:{os.environ.get('PATH', '/usr/bin')}", "HOME": str(tmp), "LANG": "C"}
            result = bash(script, env)
            urls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
            body = output.read_text(encoding="utf-8") if output.exists() else ""
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertLess(result.elapsed, 5, "镜像回退不能真的等到 wget 超时")
            return urls, body, result.stdout

    def test_mirrors_are_tried_in_order_then_jsdelivr(self):
        urls, body, out = self.run_fetch(DIRECT, gh_proxy="https://gh-proxy.com/")
        self.assertEqual(
            urls,
            [
                DIRECT,
                "https://gh-proxy.com/" + DIRECT,
                "https://ghfast.top/" + DIRECT,
                JSDELIVR_DIRECT,
            ],
        )
        self.assertEqual(body, "")
        self.assertIn("rc=1", out)
        self.assertEqual(urls.count("https://gh-proxy.com/" + DIRECT), 1, "和用户 GH_PROXY 重复的镜像只试一次")

    def test_custom_gh_proxy_is_first_and_not_duplicated(self):
        urls, _, _ = self.run_fetch(DIRECT, gh_proxy="https://mirror.example")
        self.assertEqual(
            urls,
            [
                DIRECT,
                "https://mirror.example/" + DIRECT,
                "https://gh-proxy.com/" + DIRECT,
                "https://ghfast.top/" + DIRECT,
                JSDELIVR_DIRECT,
            ],
        )

    def test_gh_mirrors_replaces_the_builtin_list(self):
        urls, _, _ = self.run_fetch(
            DIRECT,
            gh_proxy="https://custom.example/",
            gh_mirrors="https://only.example/,https://ghfast.top/",
        )
        self.assertEqual(
            urls,
            [
                DIRECT,
                "https://custom.example/" + DIRECT,
                "https://only.example/" + DIRECT,
                "https://ghfast.top/" + DIRECT,
                JSDELIVR_DIRECT,
            ],
        )
        self.assertNotIn("https://gh-proxy.com/" + DIRECT, urls)

    def test_success_stops_before_later_mirrors(self):
        urls, body, out = self.run_fetch(
            DIRECT,
            allow=("https://ghfast.top/" + DIRECT,),
            gh_proxy="https://gh-proxy.com/",
        )
        self.assertEqual(
            urls,
            [DIRECT, "https://gh-proxy.com/" + DIRECT, "https://ghfast.top/" + DIRECT],
        )
        self.assertNotIn(JSDELIVR_DIRECT, urls)
        self.assertEqual(body, "rule-data\n")
        self.assertIn("rc=0", out)

    def test_empty_mirror_list_still_falls_back_to_jsdelivr(self):
        urls, body, out = self.run_fetch(DIRECT, allow=(JSDELIVR_DIRECT,), gh_proxy="", gh_mirrors="")
        self.assertEqual(urls, [DIRECT, JSDELIVR_DIRECT])
        self.assertEqual(body, "rule-data\n")
        self.assertIn("rc=0", out)

    def test_env_file_mirror_list_is_used_when_the_variable_is_unset(self):
        with tempfile.TemporaryDirectory() as tmp:
            dotenv = Path(tmp) / "env"
            dotenv.write_text('GH_MIRRORS="https://from-dotenv.example/"\n', encoding="utf-8")
            urls, _, _ = self.run_fetch(DIRECT, gh_proxy="https://custom.example/", env_file=dotenv)
        self.assertEqual(
            urls,
            [
                DIRECT,
                "https://custom.example/" + DIRECT,
                "https://from-dotenv.example/" + DIRECT,
                JSDELIVR_DIRECT,
            ],
        )

    def test_environment_mirror_list_overrides_env_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            dotenv = Path(tmp) / "env"
            dotenv.write_text('GH_MIRRORS="https://from-dotenv.example/"\n', encoding="utf-8")
            urls, _, _ = self.run_fetch(
                DIRECT,
                gh_proxy="",
                gh_mirrors="https://from-environ.example/",
                env_file=dotenv,
            )
        self.assertEqual(urls, [DIRECT, "https://from-environ.example/" + DIRECT, JSDELIVR_DIRECT])

    def test_non_github_url_is_not_rewritten(self):
        urls, _, out = self.run_fetch("https://example.com/list.txt", gh_proxy="https://gh-proxy.com/")
        self.assertEqual(urls, ["https://example.com/list.txt"])
        self.assertIn("rc=1", out)

    def test_jsdelivr_urls(self):
        cases = {
            DIRECT: JSDELIVR_DIRECT,
            PROXY_LIST: "https://cdn.jsdelivr.net/gh/Loyalsoldier/v2ray-rules-dat@release/proxy-list.txt",
            APPLE: "https://cdn.jsdelivr.net/gh/Loyalsoldier/v2ray-rules-dat@release/apple-cn.txt",
            GEOIP: "https://cdn.jsdelivr.net/gh/Loyalsoldier/geoip@release/text/cn.txt",
        }
        script = cli_functions("jsdelivr_url") + "\n"
        for source, expected in cases.items():
            script += f'jsdelivr_url "{source}"; echo\n'
        script += 'if jsdelivr_url "https://github.com/Loyalsoldier/v2ray-rules-dat"; then echo BAD; else echo NO; fi\n'
        result = bash(script, {"PATH": os.environ.get("PATH", "/usr/bin"), "LANG": "C"})
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(lines[:4], list(cases.values()))
        self.assertEqual(lines[4], "NO")


@unittest.skipUnless(shutil.which("bash"), "需要 bash")
class GeoRetryNotifyTest(unittest.TestCase):
    def run_update(self, body, delay=None, files=(), config="", dotenv=""):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bindir = tmp / "bin"
            bindir.mkdir()
            rules = tmp / "rules"
            rules.mkdir()
            for name, text in files:
                (rules / name).write_text(text, encoding="utf-8")
            config_file = tmp / "config.yaml"
            config_file.write_text(config, encoding="utf-8")
            env_file = tmp / "mos.env"
            env_file.write_text(dotenv, encoding="utf-8")
            record = tmp / "calls.txt"
            sleeps = tmp / "sleeps.txt"
            attempts = tmp / "attempts.txt"
            fake = bindir / "python3"
            fake.write_text(
                "#!/bin/bash\n"
                f'for a in "$@"; do printf "%s|" "$a"; done >> "{record}"\n'
                f'echo >> "{record}"\n'
                "echo noisy\n",
                encoding="utf-8",
            )
            fake.chmod(0o755)
            notify = tmp / "notify_cli.py"
            notify.write_text("", encoding="utf-8")
            delay_line = "unset GEO_UPDATE_RETRY_DELAY\n" if delay is None else f'GEO_UPDATE_RETRY_DELAY="{delay}"\n'
            script = (
                'RED=""; YELLOW=""; PLAIN=""\n'
                f'sleep() {{ printf "%s\\n" "$1" >> "{sleeps}"; }}\n'
                f'NOTIFY_CLI="{notify}"\n'
                f'GEO_RULES_DIR="{rules}"\n'
                f'CONFIG_FILE="{config_file}"\n'
                f'ENV_FILE="{env_file}"\n'
                + delay_line
                + cli_functions(*UPDATE_FUNCS)
                + f'\ndo_update_geo_rules() {{\nprintf x >> "{attempts}"\n{body}\n}}\n'
                + "update_geo_rules\necho rc=$?\n"
            )
            env = {"PATH": f"{bindir}:{os.environ.get('PATH', '/usr/bin')}", "HOME": str(tmp), "LANG": "C"}
            result = bash(script, env)
            calls = record.read_text(encoding="utf-8").splitlines() if record.exists() else []
            slept = sleeps.read_text(encoding="utf-8").splitlines() if sleeps.exists() else []
            tried = attempts.read_text(encoding="utf-8") if attempts.exists() else ""
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertLess(result.elapsed, 5, "重试等待必须能跳过")
            self.assertNotIn("noisy", result.stdout)
            return result.stdout, calls, slept, len(tried)

    def test_download_failure_retries_once_with_configured_delay(self):
        out, calls, slept, tried = self.run_update(
            'GEO_FAIL_REASON="geosite_cn.txt 下载失败"; return 1',
            delay="180",
        )
        self.assertEqual(tried, 2)
        self.assertEqual(slept, ["180"])
        self.assertEqual(len(calls), 1, "重试结束前不发通知")
        self.assertIn("180 秒后自动重试一次", out)
        self.assertIn("===== 结果: 失败 =====", out)
        self.assertTrue(out.rstrip().endswith("rc=1"))

    def test_zero_delay_does_not_call_sleep(self):
        out, calls, slept, tried = self.run_update(
            'GEO_FAIL_REASON="geosite_cn.txt 下载失败"; return 1',
            delay="0",
        )
        self.assertEqual(tried, 2)
        self.assertEqual(slept, [])
        self.assertIn("立即重试一次", out)
        self.assertEqual(len(calls), 1)

    def test_default_delay_is_three_minutes_and_dotenv_can_override_it(self):
        _, _, slept, _ = self.run_update('GEO_FAIL_REASON="geosite_cn.txt 下载失败"; return 1')
        self.assertEqual(slept, ["180"])
        _, _, slept, _ = self.run_update(
            'GEO_FAIL_REASON="geosite_cn.txt 下载失败"; return 1',
            dotenv='GEO_UPDATE_RETRY_DELAY="30"\n',
        )
        self.assertEqual(slept, ["30"])
        _, _, slept, _ = self.run_update(
            'GEO_FAIL_REASON="geosite_cn.txt 下载失败"; return 1',
            delay="0",
            dotenv='GEO_UPDATE_RETRY_DELAY="30"\n',
        )
        self.assertEqual(slept, [], "环境变量优先于 .env，0 表示不睡")
        _, _, slept, _ = self.run_update(
            'GEO_FAIL_REASON="geosite_cn.txt 下载失败"; return 1',
            delay="99999",
        )
        self.assertEqual(slept, ["1800"])

    def test_second_attempt_success_notifies_recovery_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "n"
            body = (
                f'if [ ! -f "{marker}" ]; then echo 1 > "{marker}"; '
                'GEO_FAIL_REASON="geosite_cn.txt 下载失败"; return 1; fi\n'
                "return 0"
            )
            out, calls, slept, tried = self.run_update(body, delay="180")
        self.assertEqual(tried, 2)
        self.assertEqual(slept, ["180"])
        self.assertEqual(len(calls), 1)
        args = calls[0].split("|")
        self.assertEqual(args[1:6], ["--key", "geo", "--event", "ok", "Geo 规则更新已恢复"])
        self.assertIn("===== 结果: 成功 =====", out)
        self.assertTrue(out.rstrip().endswith("rc=0"))
        self.assertNotIn("规则未更新", out)

    def test_old_rules_downgrade_the_notice(self):
        out, calls, _, tried = self.run_update(
            'GEO_FAIL_REASON="geosite_cn.txt 下载失败"; return 1',
            delay="0",
            files=(("geosite_cn.txt", "cn\n"), ("geosite_no_cn.txt", "nocn\n")),
        )
        self.assertEqual(tried, 2)
        self.assertEqual(len(calls), 1)
        args = calls[0].split("|")
        self.assertEqual(args[1:7], ["--key", "geo", "--event", "warn", "规则未更新，继续使用旧规则", "geosite_cn.txt 下载失败"])
        self.assertIn("规则未更新，继续使用旧规则", out)
        self.assertNotIn("Geo 规则更新失败", out)
        self.assertIn("===== 结果: 失败 =====", out)
        self.assertTrue(out.rstrip().endswith("rc=1"))

    def test_missing_or_empty_rule_file_stays_a_failure(self):
        for files in ((), (("geosite_cn.txt", "cn\n"),), (("geosite_cn.txt", ""), ("geosite_no_cn.txt", "nocn\n"))):
            _, calls, _, _ = self.run_update(
                'GEO_FAIL_REASON="geosite_cn.txt 下载失败"; return 1',
                delay="0",
                files=files,
            )
            args = calls[0].split("|")
            self.assertEqual(args[1:6], ["--key", "geo", "--event", "fail", "Geo 规则更新失败"], files)

    def test_referenced_optional_list_must_still_exist(self):
        config = 'files:\n  - "/etc/mosdns/rules/geoip_cn.txt"\n'
        _, calls, _, _ = self.run_update(
            'GEO_FAIL_REASON="geoip_cn.txt 下载失败"; return 1',
            delay="0",
            files=(("geosite_cn.txt", "cn\n"), ("geosite_no_cn.txt", "nocn\n")),
            config=config,
        )
        self.assertEqual(calls[0].split("|")[4], "fail")
        _, calls, _, _ = self.run_update(
            'GEO_FAIL_REASON="geoip_cn.txt 下载失败"; return 1',
            delay="0",
            files=(("geosite_cn.txt", "cn\n"), ("geosite_no_cn.txt", "nocn\n"), ("geoip_cn.txt", "1.0.0.0\n")),
            config=config,
        )
        self.assertEqual(calls[0].split("|")[4], "warn")

    def test_restart_failure_does_not_retry_or_downgrade(self):
        out, calls, slept, tried = self.run_update(
            'GEO_FAIL_REASON="规则已下载，但 mosdns 重启失败"; return 1',
            delay="180",
            files=(("geosite_cn.txt", "cn\n"), ("geosite_no_cn.txt", "nocn\n")),
        )
        self.assertEqual(tried, 1)
        self.assertEqual(slept, [])
        args = calls[0].split("|")
        self.assertEqual(args[1:6], ["--key", "geo", "--event", "fail", "Geo 规则更新失败"])
        self.assertEqual(args[6], "规则已下载，但 mosdns 重启失败")
        self.assertNotIn("重试", out)


if __name__ == "__main__":
    unittest.main()
