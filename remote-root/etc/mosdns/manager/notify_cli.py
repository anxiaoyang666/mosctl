#!/usr/bin/env python3
"""给 shell 脚本（mosctl update 等）用的通知入口，复用 app.py 里的通知和去重逻辑。

    python3 /etc/mosdns/manager/notify_cli.py --key geo --event fail "Geo 规则更新失败" "geosite_cn.txt 下载失败"
    python3 /etc/mosdns/manager/notify_cli.py --key geo --event ok "Geo 规则更新已恢复" "规则文件已更新，mosdns 已重启"

--event fail：状态变为失败时发一次 ❌，仍在失败时最多每 3 天提醒一次（“持续失败第 N 天”）。
--event ok：之前失败过才发 ✅，否则什么都不发。
未开启通知时什么都不做；任何情况下都以 0 退出，不影响调用方。
"""
import argparse
import os
import sys


MANAGER_DIR = os.path.dirname(os.path.abspath(__file__))
core = None


def load_core():
    global core
    if core is None:
        if MANAGER_DIR not in sys.path:
            sys.path.insert(0, MANAGER_DIR)
        import app as module  # noqa: E402  只导入函数，app.run 在 __main__ 保护里

        core = module
    return core


def parse_args(argv):
    parser = argparse.ArgumentParser(description="mosctl 通知（带去重）")
    parser.add_argument("--key", required=True, choices=("geo",), help="去重用的事项名")
    parser.add_argument("--event", required=True, choices=("fail", "ok"))
    parser.add_argument("subject", help="标题里的事件，例如：Geo 规则更新失败")
    parser.add_argument("lines", nargs="*", help="正文，每个参数一行")
    return parser.parse_args(argv)


def run(args):
    if args.event == "fail":
        return core.notify_failure(args.key, "failure", args.subject, args.lines)
    return core.notify_recovery(args.key, args.subject, args.lines)


def main(argv=None):
    try:
        args = parse_args(sys.argv[1:] if argv is None else argv)
    except SystemExit:
        return 0
    try:
        load_core()
        action = run(args)
    except Exception as exc:  # 通知是附带功能，绝不让调用方失败
        action = f"error {type(exc).__name__}"
    if sys.stdout.isatty():
        print(action)
    return 0


if __name__ == "__main__":
    sys.exit(main())
