#!/usr/bin/env python3
"""mosctl 自动更新：cron 每天按 AUTO_UPDATE_TIME 调用一次（服务器本地时间）。

    python3 /etc/mosdns/manager/auto_update.py [--dry-run] [--only core|panel]

一次运行先处理 mosdns 内核，最后处理面板（面板升级会替换 manager 目录并重启 mosdns-web）。
规则：
- 内核只看官方稳定版（排除 prerelease），发布满 AUTO_UPDATE_CORE_MIN_AGE_DAYS 天才装，绝不降级；
  替换前用新二进制在沙盒里跑当前配置，替换后 20 秒内做健康检查，不通过自动回滚旧内核。
- 面板看 MOSCTL_BRANCH 上最近一次改动 remote-root/ 的提交，满 AUTO_UPDATE_PANEL_MIN_AGE_DAYS 天才装；
  需要判断天数但 GitHub 提交接口不可用时跳过，不会"查不到就照装"。
- 结果写入 /etc/mosdns/auto_update_state.json，并追加到 /var/log/mosctl-auto-update.log。
- 开启通知时，已更新 / 失败 / 已回滚各发一条（面板更新成功由新面板启动时发）。
这里只 import app.py 里的函数，不会启动 Flask。
"""
import argparse
import os
import sys
import time
from datetime import datetime


MANAGER_DIR = os.path.dirname(os.path.abspath(__file__))
# app.py 模块：main() 里导入；测试直接把加载好的模块赋给它
core = None


def load_core():
    global core
    if core is None:
        if MANAGER_DIR not in sys.path:
            sys.path.insert(0, MANAGER_DIR)
        import app as module  # noqa: E402  只导入函数，app.run 在 __main__ 保护里

        core = module
    return core


def log(message, tag="auto-update"):
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    lines = str(message).splitlines() or [""]
    text = f"{stamp} [{tag}] {lines[0]}\n" + "".join(f"    {line}\n" for line in lines[1:])
    try:
        with open(core.AUTO_UPDATE_LOG, "a", encoding="utf-8") as file:
            file.write(text)
    except OSError:
        pass
    # cron 下 stdout 已重定向到同一个日志文件，只有在终端里手动跑时才打印，避免重复
    if sys.stdout.isatty():
        sys.stdout.write(text)


def record_check(item, **fields):
    core.update_auto_update_item(item, last_check=int(time.time()), **fields)


def record_result(item, result, message, **fields):
    core.update_auto_update_item(
        item,
        last_result=result,
        last_result_at=int(time.time()),
        message=message,
        **fields,
    )


def notify(level, subject, lines):
    # 通知失败不影响更新流程（notify_event 自己吞异常，这里再兜一层）
    try:
        core.notify_event(level, subject, lines)
    except Exception:
        pass


CORE_SUBJECTS = {"updated": ("success", "mosdns 内核已更新"), "failed": ("failure", "mosdns 内核更新失败"), "rolled_back": ("failure", "mosdns 内核更新失败，已回滚")}


# ---------- mosdns 内核 ----------


def plan_core(settings, now=None):
    now = time.time() if now is None else now
    current_raw = core.get_version()
    current = core.clean_version(current_raw)
    plan = {"item": "core", "current": current, "latest": "", "latest_eligible": "", "release": None}
    asset = core.mosdns_asset_name()
    if not asset:
        return dict(plan, action="skipped", note="当前 CPU 架构没有官方安装包，跳过")
    listing = core.mosdns_stable_releases()
    if not listing.get("success"):
        return dict(plan, action="skipped", note=listing.get("message") or "获取 release 列表失败，跳过")
    selection = core.select_core_release(
        listing.get("releases") or [], current_raw, settings["core_min_age_days"], now=now, asset=asset
    )
    newest = selection.get("newest")
    release = selection.get("release")
    plan.update(
        action=selection["action"],
        note=selection["note"],
        release=release,
        latest=newest["tag"] if newest else "",
        latest_eligible=release["tag"] if release else "",
    )
    return plan


def run_core(plan, dry_run):
    record_check("core", current=plan["current"], latest=plan["latest"], latest_eligible=plan["latest_eligible"], note=plan["note"])
    if dry_run:
        return plan["action"], plan["note"]
    if plan["action"] != "update":
        record_result("core", plan["action"], plan["note"], **{"from": plan["current"], "to": plan["current"]})
        log(f"内核：{plan['action']} — {plan['note']}", "core")
        return plan["action"], plan["note"]
    target = core.clean_version(plan["release"]["tag"])
    log(f"内核：开始更新 {plan['current']} → {target}", "core")
    outcome = core.install_mosdns_core(release=plan["release"])
    result = outcome["result"]
    record_result(
        "core",
        result,
        outcome["message"],
        current=core.clean_version(core.get_version()),
        **{"from": outcome.get("from") or plan["current"], "to": outcome.get("to") or target},
    )
    log(f"内核：{result}\n{outcome['message']}", "core")
    if result in CORE_SUBJECTS:
        level, subject = CORE_SUBJECTS[result]
        versions = f"{outcome.get('from') or plan['current']} → {outcome.get('to') or target}"
        notify(level, subject, [versions] + list(outcome.get("summary") or []))
    return result, outcome["message"]


# ---------- 面板 ----------


def plan_panel(settings, now=None):
    now = time.time() if now is None else now
    current = core.PANEL_VERSION
    plan = {"item": "panel", "current": "v" + current, "latest": "", "latest_eligible": "", "ref": None, "target": ""}
    remote = core.remote_panel_version(force=True)
    if not remote.get("success"):
        return dict(plan, action="skipped", note=(remote.get("message") or "检测远端面板版本失败").strip())
    latest = remote.get("latest_version", "")
    plan["latest"] = "v" + latest
    current_tuple = core.panel_version_tuple(current)
    latest_tuple = core.panel_version_tuple(latest)
    if not latest_tuple or (current_tuple and latest_tuple <= current_tuple):
        return dict(plan, action="up_to_date", note=f"当前 v{current} 已是最新（远端 v{latest}）")
    min_age = settings["panel_min_age_days"]
    if min_age > 0:
        commit = core.latest_panel_commit()
        if not commit.get("success"):
            # 要判断天数却拿不到提交时间：跳过，不能当作"满足条件"
            return dict(plan, action="skipped", note=commit.get("message", "GitHub 提交接口不可用") + "，无法判断提交天数，跳过")
        age_days = (now - commit["committed_at"]) / 86400
        if age_days < min_age:
            return dict(
                plan,
                action="skipped",
                note=f"远端 v{latest} 的最新提交 {commit['sha'][:7]} 距今 {age_days:.1f} 天，未满 {min_age} 天",
            )
        plan["ref"] = commit["sha"]
    plan.update(action="update", target="v" + latest, latest_eligible="v" + latest, note=f"可更新到 v{latest}")
    return plan


def run_panel(plan, dry_run):
    record_check("panel", current=plan["current"], latest=plan["latest"], latest_eligible=plan["latest_eligible"], note=plan["note"])
    if dry_run:
        return plan["action"], plan["note"]
    if plan["action"] != "update":
        record_result("panel", plan["action"], plan["note"], **{"from": plan["current"], "to": plan["current"]})
        log(f"面板：{plan['action']} — {plan['note']}", "panel")
        return plan["action"], plan["note"]

    # 先记"开始 → vX"：面板替换后 mosdns-web 会重启，由新面板启动时把它改成 updated
    record_result("panel", "started", f"开始更新 {plan['current']} → {plan['target']}", **{"from": plan["current"], "to": plan["target"]})
    log(f"面板：开始更新 {plan['current']} → {plan['target']}" + (f"（提交 {plan['ref'][:7]}）" if plan["ref"] else ""), "panel")

    def on_install(version):
        record_result("panel", "started", f"正在安装 v{version}，等待面板重启确认", **{"from": plan["current"], "to": "v" + version})

    ok, message, restarting = core.upgrade_mosctl_panel(ref=plan["ref"], on_install=on_install)
    if ok and restarting:
        log("面板：已安装，mosdns-web 正在重启，新面板启动后确认版本\n" + message, "panel")
        return "started", message
    result = "up_to_date" if ok else "failed"
    record_result("panel", result, message, **{"from": plan["current"], "to": plan["current"]})
    log(f"面板：{result}\n{message}", "panel")
    if result == "failed":
        notify(
            "failure",
            "管理面板更新失败",
            [f"{plan['current']} → {plan['target']}", core.notify_short_line(message) or "安装失败", f"继续运行 {plan['current']}"],
        )
    return result, message


# ---------- 入口 ----------


ITEM_FAILED_SUBJECTS = {"core": "mosdns 内核更新失败", "panel": "管理面板更新失败"}

RESULT_LABELS = {
    "update": "可更新",
    "updated": "已更新",
    "up_to_date": "已是最新",
    "skipped": "跳过",
    "failed": "失败",
    "rolled_back": "已回滚",
    "started": "已安装，等待重启确认",
}


def parse_args(argv):
    parser = argparse.ArgumentParser(description="mosctl 自动更新（mosdns 内核 + 面板）")
    parser.add_argument("--dry-run", action="store_true", help="只检查并报告，不安装")
    parser.add_argument("--only", choices=("core", "panel"), help="只处理一项")
    parser.add_argument("--manual", action="store_true", help="面板里点击“立即更新”触发：自动更新关闭时也执行")
    return parser.parse_args(argv)


def run(args, now=None):
    settings = core.read_auto_update_settings()
    if not settings["enabled"] and not (args.dry_run or args.manual):
        log("自动更新已关闭（AUTO_UPDATE_ENABLED=false），跳过")
        return 0, ["自动更新已关闭，跳过"]

    mode = "检查（不安装）" if args.dry_run else ("手动更新" if args.manual else "定时更新")
    if not args.dry_run:
        log(f"开始{mode}")
    state = core.read_auto_update_state()
    state["last_run"] = {"started_at": int(time.time()), "dry_run": bool(args.dry_run), "manual": bool(args.manual), "finished_at": 0}
    core.write_auto_update_state(state)

    report = []
    failed = False
    steps = [("core", "MosDNS 内核", plan_core, run_core), ("panel", "Mosctl 面板", plan_panel, run_panel)]
    # 顺序固定：内核在前，面板最后（面板升级会重启 mosdns-web，进程可能随后结束）
    for item, label, planner, runner in steps:
        if args.only and args.only != item:
            continue
        try:
            plan = planner(settings, now=now)
            result, message = runner(plan, args.dry_run)
        except Exception as exc:  # 一项出错不影响另一项
            result, message = "failed", f"{type(exc).__name__}: {exc}"
            if not args.dry_run:
                record_result(item, "failed", message)
            log(f"{label}：异常 {message}", item)
            plan = {"current": "", "latest_eligible": ""}
            if not args.dry_run:
                notify("failure", ITEM_FAILED_SUBJECTS[item], ["检查或安装时出错", "详情见自动更新日志"])
        failed = failed or result in ("failed", "rolled_back")
        line = f"{label}：{RESULT_LABELS.get(result, result)}"
        if plan.get("current"):
            line += f"（当前 {plan['current']}"
            if plan.get("latest_eligible"):
                line += f"，可装 {plan['latest_eligible']}"
            line += "）"
        report.append(line + "\n  " + str(message).replace("\n", "\n  "))

    state = core.read_auto_update_state()
    state.setdefault("last_run", {})["finished_at"] = int(time.time())
    core.write_auto_update_state(state)
    if not args.dry_run:
        log(f"{mode}结束" + ("（有失败或回滚）" if failed else ""))
    return (1 if failed else 0), report


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    load_core()
    fd = core.acquire_auto_update_lock()
    if fd is None:
        message = "已有自动更新在运行，本次跳过"
        log(message)
        print(message)
        return 2
    try:
        code, report = run(args)
    finally:
        core.release_auto_update_lock(fd)
    # dry-run 的报告给面板"立即检查"展示；cron 下 stdout 就是日志文件，正式运行时 log() 已经写过
    if args.dry_run or sys.stdout.isatty():
        print("\n".join(report))
    return code


if __name__ == "__main__":
    sys.exit(main())
