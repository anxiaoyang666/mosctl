from datetime import datetime, timedelta, timezone
from functools import wraps
import base64
import fcntl
import glob
import ipaddress
import os
import re
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
from urllib import error, request as urlrequest
import json
import stat
import tempfile
import zipfile
from urllib.parse import quote, urlsplit

from flask import Flask, jsonify, redirect, render_template, request, session


MOSDNS_DIR = "/etc/mosdns"
ENV_FILE = f"{MOSDNS_DIR}/.env"
CONFIG_FILE = f"{MOSDNS_DIR}/config.yaml"
DEFAULT_TEMPLATE_FILE = f"{MOSDNS_DIR}/templates/default.yaml"
BACKUP_DIR = f"{MOSDNS_DIR}/backup"
LOG_FILE = "/var/log/mosdns.log"
MANAGER_DIR = f"{MOSDNS_DIR}/manager"
MOSCTL = "/usr/local/bin/mosctl"
MOSDNS_BIN = "/usr/local/bin/mosdns"
SYSTEMD_DIR = "/etc/systemd/system"
RESCUE_DNS = "223.5.5.5"
DEFAULT_BACKUP_KEEP_COUNT = 20
KERNEL_BACKUP_KEEP_COUNT = 3
# 备份文件按前缀分三类，互不混在同一个列表或保留数里：
#   config.<stamp>.bak            配置备份（旧版叫 config.yaml.<stamp>.bak，仍可识别）
#   rule-<id>.<stamp>.bak         规则文件备份（旧版叫 <file>.txt.<stamp>.bak，启动/清理时自动改名）
#   mosdns-bin.<stamp>            内核备份（旧版叫 mosdns-bin.<stamp>.bak，仍可识别）
CONFIG_BACKUP_PREFIX = "config."
RULE_BACKUP_PREFIX = "rule-"
KERNEL_BACKUP_PREFIX = "mosdns-bin."
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
LOG_TIMESTAMP_RE = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:?\d\d))(.*)$")
SYNCABLE_RULE_IDS = {"force-cn", "force-nocn"}
MOSDNS_RELEASE_BASE = "https://github.com/IrineSistiana/mosdns/releases/latest/download"
MOSDNS_RELEASE_API = "https://api.github.com/repos/IrineSistiana/mosdns/releases/latest"
GEO_UPDATE_COMMAND = f"{MOSCTL} update"
GEO_CRON_COMMENT = "# MosDNS Web: Geo update schedule"
# 定时任务输出写到这里，不然跑没跑、成没成功都看不到
UPDATE_LOG = "/var/log/mosctl-update.log"
DEFAULT_MOSCTL_REPO_URL = "https://github.com/anxiaoyang666/mosctl.git"
DEFAULT_MOSCTL_BRANCH = "main"
# .env 里没有 GH_PROXY 时沿用这个默认值；写成空字符串表示不走代理
DEFAULT_GH_PROXY = "https://gh-proxy.com/"
PANEL_VERSION = "0.3.43"
PANEL_BACKUP_KEEP_COUNT = 3
# 登录态保留 30 天；有登录限速和改密码轮换密钥兜底，不需要一年
SESSION_LIFETIME_DAYS = 30
# 下载上限：内核 zip 约 5 MB、面板源码 zip 约 2 MB，100 MB 足够且能挡住异常响应
DOWNLOAD_MAX_BYTES = 100 * 1024 * 1024
DOWNLOAD_TIME_BUDGET = 300
REMOTE_VERSION_CACHE_TTL = 3600
MOSDNS_RELEASES_API = "https://api.github.com/repos/IrineSistiana/mosdns/releases?per_page=30"
MOSDNS_RELEASE_DOWNLOAD = "https://github.com/IrineSistiana/mosdns/releases/download"
# 自动更新：cron 每天按 AUTO_UPDATE_TIME（服务器本地时间）跑一次 auto_update.py
AUTO_UPDATE_SCRIPT = f"{MANAGER_DIR}/auto_update.py"
AUTO_UPDATE_STATE_FILE = f"{MOSDNS_DIR}/auto_update_state.json"
AUTO_UPDATE_LOCK_FILE = f"{MOSDNS_DIR}/.auto_update.lock"
AUTO_UPDATE_LOG = "/var/log/mosctl-auto-update.log"
AUTO_UPDATE_CRON_MARKER = "# MOSCTL_AUTO_UPDATE"
AUTO_UPDATE_DEFAULTS = {
    "AUTO_UPDATE_ENABLED": "true",
    "AUTO_UPDATE_TIME": "04:10",
    "AUTO_UPDATE_CORE_MIN_AGE_DAYS": "3",
    "AUTO_UPDATE_PANEL_MIN_AGE_DAYS": "0",
}
AUTO_UPDATE_MAX_AGE_DAYS = 365
AUTO_UPDATE_RESULTS = ("updated", "up_to_date", "skipped", "failed", "rolled_back", "started")
AUTO_UPDATE_DRY_RUN_TIMEOUT = 150
# 升级内核后的健康检查：服务 active、版本正确、国内/国外域名在本机 53 端口都有应答
CORE_HEALTH_DOMAINS = ("www.baidu.com", "www.google.com")
CORE_HEALTH_TIMEOUT = 20
CORE_HEALTH_INTERVAL = 1.0
CORE_HEALTH_DNS_SERVER = ("127.0.0.1", 53)
# restart_mosdns 重启后的就绪检查：每 100ms 看一次，直到服务 active 且国内域名能解析，最多等 5 秒。
# mosdns.service 的 ExecStartPost 不再阻塞 2 秒，"重启成功"必须由这里确认，回滚才有依据
RESTART_READY_TIMEOUT = 5.0
RESTART_READY_INTERVAL = 0.1
RESTART_READY_DOMAIN = "www.baidu.com"
LOGIN_MAX_FAILURES = 5
LOGIN_LOCK_SECONDS = 60
# 同一时间只允许一个会改写配置/重启服务的操作；.env 的读改写用单独的锁，
# 避免保存账号设置被一次长达数分钟的 Geo 更新阻塞
OPERATION_LOCK = threading.Lock()
ENV_LOCK = threading.Lock()
REMOTE_VERSION_LOCK = threading.Lock()
REMOTE_VERSION_CACHE = {"key": "", "at": 0.0, "result": None}
LOGIN_FAILURES_LOCK = threading.Lock()
LOGIN_FAILURES = {}
OPERATION_BUSY_MESSAGE = "操作进行中，请稍后再试"

RULE_FILES = {
    "force-cn": {
        "label": "强制国内",
        "path": f"{MOSDNS_DIR}/rules/force-cn.txt",
        "summary": "命中的域名强制走国内上游 DNS，适合国内站点被误判到国外时使用。",
        "format": "每行一个域名，不能有空格；写主域名即可（默认连同子域名一起匹配）。也可以加 full:（只匹配这个域名）、keyword:、regexp: 前缀。",
        "examples": [
            "# 这些域名强制走国内 DNS",
            "example.cn",
            "qq.com",
            "bilibili.com",
        ],
    },
    "force-nocn": {
        "label": "强制国外",
        "path": f"{MOSDNS_DIR}/rules/force-nocn.txt",
        "summary": "命中的域名强制走国外上游 DNS，适合海外服务解析不准或被污染时使用。",
        "format": "每行一个域名，不能有空格；写主域名即可（默认连同子域名一起匹配）。也可以加 full:（只匹配这个域名）、keyword:、regexp: 前缀。",
        "examples": [
            "# 这些域名强制走国外 DNS",
            "openai.com",
            "github.com",
            "google.com",
        ],
    },
    "hosts": {
        "label": "自定义 Hosts",
        "path": f"{MOSDNS_DIR}/rules/hosts.txt",
        "summary": "把指定域名固定解析到指定 IP，适合内网域名、NAS、路由器、服务别名。",
        "format": "每行一个：域名 IP（可跟多个 IP，用空格分开）。默认精确匹配，只命中写出的这个域名本身；要连同子域名一起命中请加 domain: 前缀。可以用 # 写注释。",
        "examples": [
            "# nas.lan 固定到 NAS",
            "nas.lan 10.10.30.10",
            "router.lan 10.10.30.1",
            "# 带 domain: 前缀时 home.lan 和 *.home.lan 都命中",
            "domain:home.lan 10.10.30.1",
        ],
    },
}


app = Flask(__name__)
app.permanent_session_lifetime = timedelta(days=SESSION_LIFETIME_DAYS)
app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"] = True


def json_body():
    # 所有 POST 接口都走这里：非 JSON、非对象的请求体一律当作空对象
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def operation_locked(func):
    # 控制/升级/配置写入互斥；GET 不加锁。非阻塞获取，拿不到锁直接提示稍后再试
    @wraps(func)
    def wrapper(*args, **kwargs):
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return func(*args, **kwargs)
        if not OPERATION_LOCK.acquire(blocking=False):
            return jsonify({"success": False, "message": OPERATION_BUSY_MESSAGE}), 409
        try:
            return func(*args, **kwargs)
        finally:
            OPERATION_LOCK.release()

    return wrapper


def clean_output(text):
    text = ANSI_RE.sub("", text or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.strip()


def normalize_log_timestamps(text):
    # 时间戳统一成带时区偏移的 ISO-8601（去掉小数秒），由浏览器按本地时区显示；
    # 后端不再转成服务器本地时间，否则面板和服务器时区不同时会看错
    lines = []
    for line in (text or "").splitlines():
        match = LOG_TIMESTAMP_RE.match(line)
        if not match:
            lines.append(line)
            continue
        try:
            timestamp = match.group(1).replace("Z", "+00:00")
            if re.search(r"[+-]\d{4}$", timestamp):
                timestamp = timestamp[:-2] + ":" + timestamp[-2:]
            parsed = datetime.fromisoformat(timestamp)
            lines.append(parsed.replace(microsecond=0).isoformat() + match.group(2))
        except ValueError:
            lines.append(line)
    return "\n".join(lines)


def parse_log_payload(value):
    if not value:
        return {}
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except ValueError:
        return {}


def explain_log_line(line):
    parts = (line or "").split("\t")
    entry = {
        "time": "",
        "level": "",
        "component": "",
        "summary": "未知日志",
        "detail": line or "",
        "raw": line or "",
        "kind": "info",
    }
    if len(parts) < 3:
        return entry

    entry["time"] = parts[0]
    entry["level"] = parts[1].upper()
    entry["kind"] = {
        "ERROR": "error",
        "FATAL": "error",
        "WARN": "warn",
        "WARNING": "warn",
        "INFO": "info",
        "DEBUG": "debug",
    }.get(entry["level"], "info")

    if len(parts) >= 5:
        entry["component"] = parts[2]
        message = parts[3]
        payload_text = "\t".join(parts[4:])
    elif len(parts) == 4:
        message = parts[2]
        payload_text = parts[3]
    else:
        message = parts[2]
        payload_text = ""

    payload = parse_log_payload(payload_text)
    tag = payload.get("tag", "")
    addr = payload.get("addr", "")
    entries = payload.get("entries", "")
    error_text = payload.get("error", "")

    if message == "loading plugin":
        entry["summary"] = f"正在加载模块：{tag or entry['component'] or '未命名'}"
    elif message == "closing plugin":
        entry["summary"] = f"正在关闭模块：{tag or entry['component'] or '未命名'}"
    elif message == "all plugins are loaded":
        entry["summary"] = "mosdns 已启动，所有模块加载完成"
    elif message == "all plugins were closed":
        entry["summary"] = "mosdns 已停止，所有模块已关闭"
    elif message == "starting api http server":
        entry["summary"] = f"API 服务已启动：{addr or '地址未知'}"
    elif message in ("udp server started", "tcp server started"):
        protocol = "UDP" if "udp" in message else "TCP"
        entry["summary"] = f"{protocol} DNS 服务已监听：{addr or '地址未知'}"
    elif message == "cache dump loaded":
        entry["summary"] = f"缓存已载入：{entries} 条记录" if entries != "" else "缓存已载入"
    elif message == "cache dumped":
        entry["summary"] = f"缓存已保存：{entries} 条记录" if entries != "" else "缓存已保存"
    elif message == "read err" and "closed network connection" in error_text:
        entry["summary"] = "服务重启或停止时连接被关闭，通常可以忽略"
        entry["kind"] = "notice"
    elif message == "read err":
        entry["summary"] = "读取 DNS 请求时出现异常"
    else:
        entry["summary"] = message or entry["summary"]

    if payload_text:
        entry["detail"] = f"{message} {payload_text}"
    else:
        entry["detail"] = message
    return entry


def parse_log_entries(text):
    return [explain_log_line(line) for line in (text or "").splitlines() if line.strip()]


def run_cmd(args, timeout=60):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return result.returncode == 0, clean_output(result.stdout + result.stderr)
    except Exception as exc:
        return False, str(exc)


def read_env():
    env = {}
    if not os.path.exists(ENV_FILE):
        return env
    with open(ENV_FILE, "r", encoding="utf-8") as file:
        for line in file:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            env[key.strip()] = value.strip().strip('"').strip("'")
    return env


ENV_VALUE_FORBIDDEN = '"\\$`\r\n\x00'


def env_value_error(value):
    # .env 会被 install.sh/mosctl（grep 取值）和 read_env 两种方式解析，
    # 这几个字符在二者之间语义不一致，还可能被当作命令执行，统一拒绝。
    if not isinstance(value, str):
        return "值必须是字符串"
    for char in ENV_VALUE_FORBIDDEN:
        if char in value:
            return '不能包含引号、反斜杠、$、反引号或换行'
    return None


def write_env(updates):
    for key, value in updates.items():
        error = env_value_error(value)
        if error:
            raise ValueError(f"{key}: {error}")
    # 读-改-写在锁内完成，写入先落到临时文件再原子替换，避免并发请求互相覆盖或写出半个文件
    with ENV_LOCK:
        os.makedirs(MOSDNS_DIR, exist_ok=True)
        lines = []
        if os.path.exists(ENV_FILE):
            with open(ENV_FILE, "r", encoding="utf-8") as file:
                lines = file.readlines()

        seen = set()
        output = []
        for line in lines:
            stripped = line.strip()
            if "=" in stripped and not stripped.startswith("#"):
                key = stripped.split("=", 1)[0].strip()
                if key in updates:
                    output.append(f'{key}="{updates[key]}"\n')
                    seen.add(key)
                    continue
            output.append(line)
        # 手工编辑过的 .env 可能没有结尾换行，追加新键前先补上，避免粘到上一行
        if output and not output[-1].endswith("\n"):
            output[-1] += "\n"
        for key, value in updates.items():
            if key not in seen:
                output.append(f'{key}="{value}"\n')

        tmp_file = f"{ENV_FILE}.webtmp"
        fd = os.open(tmp_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write("".join(output))
        os.replace(tmp_file, ENV_FILE)
        try:
            os.chmod(ENV_FILE, 0o600)
        except OSError:
            pass


def ensure_env():
    env = read_env()
    updates = {}
    if not env.get("WEB_SESSION_SECRET"):
        updates["WEB_SESSION_SECRET"] = secrets.token_urlsafe(48)
    if not env.get("WEB_USER"):
        updates["WEB_USER"] = "admin"
    if not env.get("WEB_SECRET"):
        updates["WEB_SECRET"] = secrets.token_urlsafe(18)
    if not env.get("WEB_PORT"):
        updates["WEB_PORT"] = "7840"
    if not env.get("RULE_SYNC_TOKEN"):
        updates["RULE_SYNC_TOKEN"] = secrets.token_urlsafe(24)
    if not env.get("RULE_SYNC_ENABLED"):
        updates["RULE_SYNC_ENABLED"] = "false"
    if "RULE_SYNC_PEERS" not in env:
        updates["RULE_SYNC_PEERS"] = ""
    if updates:
        write_env(updates)
        env.update(updates)
    app.secret_key = env["WEB_SESSION_SECRET"]


# ---------- 通知（Webhook → 微信） ----------
# 和 mihomo 面板同一个 webhook：POST JSON {"title", "content"}，不渲染 Markdown，只认纯文本换行和 emoji。
# 标题 "{图标} {站点名} · {事件}"，正文每行一个事实、最多 4 行，末尾附 "📅 服务器本地时间"。
NOTIFY_LOG = "/var/log/mosctl-notify.log"
NOTIFY_STATE_FILE = f"{MOSDNS_DIR}/notify_state.json"
NOTIFY_STATE_LOCK_FILE = f"{MOSDNS_DIR}/.notify_state.lock"
NOTIFY_TIMEOUT = 15
NOTIFY_MAX_LINES = 4
NOTIFY_LINE_MAX_CHARS = 80
NOTIFY_REMIND_SECONDS = 3 * 86400
NOTIFY_URL_MAX_LEN = 500
SITE_NAME_MAX_LEN = 20
DEFAULT_SITE_NAME = "mosdns"
# 通知正文里的时间是给微信里的人看的服务器本地时间（不经过浏览器格式化），和 mihomo 的 notify.sh 一致
NOTIFY_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
# 通知里的时间统一用北京时间（容器大多跑在 UTC），和 mihomo 面板的 notify.sh 一致
NOTIFY_TZ = timezone(timedelta(hours=8))
NOTIFY_ICONS = {"success": "✅", "warning": "⚠️", "failure": "❌", "info": "🔔"}
NOTIFY_STATE_THREAD_LOCK = threading.Lock()


def site_name_error(value):
    if not isinstance(value, str):
        return "站点名称必须是字符串"
    if len(value) > SITE_NAME_MAX_LEN:
        return f"站点名称最多 {SITE_NAME_MAX_LEN} 个字符"
    if "'" in value or env_value_error(value) or any(ord(char) < 32 for char in value):
        return "站点名称不能包含引号、反斜杠、$、反引号或换行"
    return None


def site_name(env=None):
    env = read_env() if env is None else env
    value = str(env.get("SITE_NAME", "")).strip()
    if not value or site_name_error(value):
        return DEFAULT_SITE_NAME
    return value


def notify_url_error(url):
    if not isinstance(url, str) or not url:
        return "Webhook 地址不能为空"
    if len(url) > NOTIFY_URL_MAX_LEN:
        return f"Webhook 地址最多 {NOTIFY_URL_MAX_LEN} 个字符"
    if any(char.isspace() for char in url) or "'" in url or env_value_error(url):
        return "Webhook 地址不能包含空白、引号、反斜杠、$ 或反引号"
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return "Webhook 地址格式不正确"
    if parts.scheme not in ("http", "https") or not host:
        return "Webhook 地址必须以 http:// 或 https:// 开头并包含主机名"
    return None


def notify_url_host(url):
    """只给日志和页面看主机名（含端口）：路径和查询串里常带 key，一律不外露。"""
    try:
        parts = urlsplit(str(url or ""))
        host = parts.hostname or ""
        port = parts.port
    except ValueError:
        return ""
    if ":" in host:
        host = f"[{host}]"
    return f"{host}:{port}" if host and port else host


def read_notify_settings(env=None):
    env = read_env() if env is None else env
    url = str(env.get("NOTIFY_API_URL", "")).strip()
    url_ok = bool(url) and not notify_url_error(url)
    host = notify_url_host(url) if url_ok else ""
    return {
        "site_name": str(env.get("SITE_NAME", "")).strip(),
        "site_name_effective": site_name(env),
        "enabled": is_true(env.get("NOTIFY_ENABLED")),
        "url_set": url_ok,
        "url_host": host,
        "url_display": f"{host} 已设置" if url_ok else "未设置",
    }


def validate_notify_settings(data, env=None):
    """返回 (要写入 .env 的键值, 错误)。URL 留空表示沿用已保存的；clear_url=true 才清空。"""
    if not isinstance(data, dict):
        return None, "请求格式不正确"
    env = read_env() if env is None else env
    site = data.get("site_name", "")
    site = site.strip() if isinstance(site, str) else site
    error_message = site_name_error(site)
    if error_message:
        return None, error_message
    enabled = data.get("enabled")
    if isinstance(enabled, str) and enabled.lower() in ("true", "false"):
        enabled = enabled.lower() == "true"
    if not isinstance(enabled, bool):
        return None, "通知开关必须是 true 或 false"
    updates = {"SITE_NAME": site, "NOTIFY_ENABLED": "true" if enabled else "false"}
    url = data.get("url", "")
    url = url.strip() if isinstance(url, str) else url
    if data.get("clear_url") in (True, "true"):
        updates["NOTIFY_API_URL"] = ""
    elif url:
        error_message = notify_url_error(url)
        if error_message:
            return None, error_message
        updates["NOTIFY_API_URL"] = url
    final_url = updates.get("NOTIFY_API_URL", str(env.get("NOTIFY_API_URL", "")).strip())
    if enabled and (not final_url or notify_url_error(final_url)):
        return None, "启用通知前请先填写 Webhook 地址"
    return updates, None


def save_notify_settings(data):
    updates, error_message = validate_notify_settings(data)
    if error_message:
        return False, error_message
    write_env(updates)
    return True, "通知设置已保存"


def notify_short_line(text, limit=NOTIFY_LINE_MAX_CHARS):
    """取一段说明的第一行做通知正文：去掉结尾冒号，过长截断。"""
    for line in clean_output(str(text or "")).splitlines():
        line = line.strip().rstrip("：:").strip()
        if line:
            return line if len(line) <= limit else line[: limit - 1] + "…"
    return ""


def build_notification(level, subject, lines, site=None, now=None):
    icon = NOTIFY_ICONS.get(level, NOTIFY_ICONS["info"])
    site = site or DEFAULT_SITE_NAME
    title = f"{icon} {site} · {notify_short_line(subject, 60)}"
    body = []
    for line in lines or []:
        line = notify_short_line(line)
        if line:
            body.append(line)
    body = body[:NOTIFY_MAX_LINES]
    stamp = datetime.fromtimestamp(now if now else time.time(), NOTIFY_TZ).strftime(NOTIFY_TIME_FORMAT)
    content = ("\n".join(body) + "\n\n" if body else "") + f"📅 {stamp}"
    return title, content


def log_notify(message):
    stamp = datetime.now().strftime(NOTIFY_TIME_FORMAT)
    try:
        with open(NOTIFY_LOG, "a", encoding="utf-8") as file:
            file.write(f"[{stamp}] {message}\n")
    except OSError:
        pass


def notify_urlopen(req, timeout):
    # 直连，不走环境变量里的 http(s)_proxy：webhook 通常在内网或国内
    opener = urlrequest.build_opener(urlrequest.ProxyHandler({}))
    return opener.open(req, timeout=timeout)


def scrub_notify_error(text, url):
    text = str(text or "")
    try:
        parts = urlsplit(url)
        secrets_in_url = [url, parts.path, parts.query]
    except ValueError:
        secrets_in_url = [url]
    for secret in secrets_in_url:
        if secret and len(secret) > 1:
            text = text.replace(secret, "***")
    return " ".join(text.split())[:200]


def post_notification(url, title, content, timeout=NOTIFY_TIMEOUT):
    """返回 (ok, http 状态码或 0, 说明)。说明里不含 URL。"""
    payload = json.dumps({"title": title, "content": content}, ensure_ascii=False).encode("utf-8")
    req = urlrequest.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8", "User-Agent": "mosctl-notify"},
        method="POST",
    )
    try:
        with notify_urlopen(req, timeout) as response:
            status = int(getattr(response, "status", 0) or response.getcode() or 0)
    except error.HTTPError as exc:
        try:
            exc.close()
        except Exception:
            pass
        return False, int(exc.code or 0), f"HTTP {exc.code}"
    except error.URLError as exc:
        return False, 0, scrub_notify_error(exc.reason, url) or "连接失败"
    except Exception as exc:
        return False, 0, scrub_notify_error(f"{type(exc).__name__}: {exc}", url)
    if 200 <= status < 300:
        return True, status, f"HTTP {status}"
    return False, status, f"HTTP {status}"


def send_notification(level, subject, lines, url, site, now=None):
    title, content = build_notification(level, subject, lines, site=site, now=now)
    host = notify_url_host(url)
    ok, status, detail = post_notification(url, title, content)
    if ok:
        log_notify(f"sent host={host} {detail} title={title}")
        return {"success": True, "status": status, "message": f"已发送（{detail}）", "title": title, "content": content}
    log_notify(f"failed host={host} error={detail} title={title}")
    return {"success": False, "status": status, "message": f"发送失败：{detail}", "title": title, "content": content}


def notify_event(level, subject, lines=None, background=False, env=None):
    """发一条通知。未启用或没配地址直接跳过；任何异常都吞掉，不影响调用方。

    background=True 时在后台线程里发（Web 请求里用，避免 webhook 慢拖住页面）。
    """
    if background:
        try:
            threading.Thread(target=notify_event, args=(level, subject, lines), kwargs={"env": env}, daemon=True).start()
        except Exception:
            pass
        return None
    try:
        env = read_env() if env is None else env
        if not is_true(env.get("NOTIFY_ENABLED")):
            return {"success": False, "skipped": True, "message": "通知未启用"}
        url = str(env.get("NOTIFY_API_URL", "")).strip()
        if notify_url_error(url):
            return {"success": False, "skipped": True, "message": "Webhook 地址未设置"}
        return send_notification(level, subject, lines, url, site_name(env))
    except Exception as exc:
        try:
            log_notify(f"error {type(exc).__name__} subject={subject}")
        except Exception:
            pass
        return {"success": False, "message": "发送通知时出错"}


def send_test_notification(data):
    """“发送测试通知”：表单里填了地址/站点名就用表单的（不必先保存），否则用已保存的。不看启用开关。"""
    data = data if isinstance(data, dict) else {}
    env = read_env()
    url = str(data.get("url") or "").strip() or str(env.get("NOTIFY_API_URL", "")).strip()
    error_message = notify_url_error(url)
    if error_message:
        return {"success": False, "message": "请先填写 Webhook 地址" if not url else error_message}
    site = data.get("site_name")
    site = site.strip() if isinstance(site, str) else ""
    if site and site_name_error(site):
        return {"success": False, "message": site_name_error(site)}
    try:
        result = send_notification("info", "通知测试", ["通知通道正常"], url, site or site_name(env))
    except Exception:
        return {"success": False, "message": "发送通知时出错"}
    return {"success": result["success"], "message": result["message"], "status": result["status"], "host": notify_url_host(url)}


def read_notify_state():
    try:
        with open(NOTIFY_STATE_FILE, "r", encoding="utf-8") as file:
            state = json.load(file)
    except (OSError, ValueError):
        state = {}
    return state if isinstance(state, dict) else {}


def write_notify_state(state):
    os.makedirs(os.path.dirname(NOTIFY_STATE_FILE), exist_ok=True)
    tmp_file = f"{NOTIFY_STATE_FILE}.{os.getpid()}.tmp"
    with open(tmp_file, "w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)
    os.replace(tmp_file, NOTIFY_STATE_FILE)


def update_notify_state(key, decide):
    """在锁内读 notify_state.json → decide(旧条目) 返回 (新条目, 动作) → 写回。CLI 与面板共用这个文件，用 flock 串行。"""
    with NOTIFY_STATE_THREAD_LOCK:
        os.makedirs(os.path.dirname(NOTIFY_STATE_LOCK_FILE), exist_ok=True)
        fd = os.open(NOTIFY_STATE_LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            state = read_notify_state()
            entry = state.get(key) if isinstance(state.get(key), dict) else {}
            new_entry, action = decide(dict(entry))
            if new_entry != entry:
                state[key] = new_entry
                write_notify_state(state)
            return action
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


def notify_failure(key, level, subject, lines, now=None, background=False):
    """反复出现的失败（Geo 更新、收到的规则同步）：状态变为失败时发一次，之后仍失败最多每 3 天提醒一次。

    返回动作：notified / reminded / suppressed / disabled / error。
    """
    if background:
        threading.Thread(target=notify_failure, args=(key, level, subject, lines), kwargs={"now": now}, daemon=True).start()
        return None
    try:
        env = read_env()
        if not is_true(env.get("NOTIFY_ENABLED")) or notify_url_error(str(env.get("NOTIFY_API_URL", "")).strip()):
            return "disabled"
        now = int(time.time() if now is None else now)
        lines = list(lines or [])

        def decide(entry):
            if not entry.get("failing"):
                result = notify_event(level, subject, lines, env=env)
                sent = bool(result and result.get("success"))
                # 没发出去 last_notified 记 0，下次失败按“持续失败”补发
                return {"failing": True, "since": now, "last_notified": now if sent else 0, "subject": subject}, "notified"
            if now - int(entry.get("last_notified") or 0) < NOTIFY_REMIND_SECONDS:
                return entry, "suppressed"
            since = int(entry.get("since") or now)
            day = (now - since) // 86400 + 1
            reminder = lines[: NOTIFY_MAX_LINES - 1] + [f"持续失败第 {day} 天"]
            result = notify_event(level, subject, reminder, env=env)
            if result and result.get("success"):
                entry["last_notified"] = now
            return entry, "reminded"

        return update_notify_state(key, decide)
    except Exception as exc:
        log_notify(f"error {type(exc).__name__} key={key}")
        return "error"


def notify_recovery(key, subject, lines, now=None, background=False):
    """之前记为失败的事项恢复了：发一条 ✅，清掉失败状态。之前没失败就什么都不做。返回 recovered / none / disabled / error。"""
    if background:
        threading.Thread(target=notify_recovery, args=(key, subject, lines), kwargs={"now": now}, daemon=True).start()
        return None
    try:
        env = read_env()
        enabled = is_true(env.get("NOTIFY_ENABLED")) and not notify_url_error(str(env.get("NOTIFY_API_URL", "")).strip())
        now = int(time.time() if now is None else now)

        def decide(entry):
            if not entry.get("failing"):
                return entry, "none"
            if enabled:
                notify_event("success", subject, lines, env=env)
            return {"failing": False, "recovered_at": now}, ("recovered" if enabled else "disabled")

        return update_notify_state(key, decide)
    except Exception as exc:
        log_notify(f"error {type(exc).__name__} key={key}")
        return "error"


def gh_proxy_prefix():
    # 代理前缀只从 .env 读：键不存在用默认值，键存在但为空表示不走代理
    env = read_env()
    if "GH_PROXY" not in env:
        return DEFAULT_GH_PROXY
    prefix = env.get("GH_PROXY", "").strip()
    if not prefix:
        return ""
    if not prefix.startswith(("http://", "https://")):
        return ""
    return prefix if prefix.endswith("/") else prefix + "/"


def github_url_candidates(url):
    # 先直连 GitHub，失败再退到代理；代理只用于 GitHub 域名
    urls = [url]
    prefix = gh_proxy_prefix()
    if prefix and url.startswith(("https://github.com/", "https://raw.githubusercontent.com/", "https://api.github.com/")):
        urls.append(prefix + url)
    return urls


ensure_env()


def migrate_cron_logging():
    """老版本写的 `mosctl update > /dev/null 2>&1` 改成写日志；没有需要改的就不碰 crontab。"""
    try:
        lines = read_crontab_lines()
    except Exception:
        return False
    changed = False
    updated = []
    for line in lines:
        if is_geo_update_cron(line) and "> /dev/null" in line:
            line = re.sub(r"\s*>\s*/dev/null\s+2>&1\s*$", f" >> {UPDATE_LOG} 2>&1", line)
            changed = True
        updated.append(line)
    if not changed:
        return False
    text = "\n".join(updated).strip() + "\n"
    try:
        subprocess.run(["crontab", "-"], input=text, capture_output=True, text=True, timeout=10)
    except Exception:
        return False
    return True



@app.before_request
def require_ajax_header_for_api_writes():
    # 浏览器表单/跨站请求带不上自定义头，用它挡住 CSRF。
    # /api/rule-sync 是节点间调用、用同步密钥鉴权，不需要这个头
    if not request.path.startswith("/api/") or request.path == "/api/rule-sync":
        return None
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    if request.headers.get("X-Requested-With", "") != "XMLHttpRequest":
        return jsonify({"success": False, "message": "缺少 X-Requested-With 请求头"}), 403
    return None


def client_address():
    return request.remote_addr or "unknown"


def login_locked_seconds(address):
    with LOGIN_FAILURES_LOCK:
        count, lock_until, _ = LOGIN_FAILURES.get(address, (0, 0.0, 0.0))
    remaining = int(lock_until - time.time())
    return remaining if count >= LOGIN_MAX_FAILURES and remaining > 0 else 0


def record_login_failure(address):
    # 内存里按来源 IP 记 (失败次数, 锁定截止, 最后失败时间)；超过 5 次锁 60 秒
    now = time.time()
    with LOGIN_FAILURES_LOCK:
        # 顺手清掉一小时没动静的条目，避免字典无限增长
        for key in [key for key, (_, _, last_at) in LOGIN_FAILURES.items() if now - last_at > 3600]:
            LOGIN_FAILURES.pop(key, None)
        count, lock_until, _ = LOGIN_FAILURES.get(address, (0, 0.0, 0.0))
        if lock_until and lock_until < now and count >= LOGIN_MAX_FAILURES:
            count = 0
        count += 1
        lock_until = now + LOGIN_LOCK_SECONDS if count >= LOGIN_MAX_FAILURES else 0.0
        LOGIN_FAILURES[address] = (count, lock_until, now)


def clear_login_failures(address):
    with LOGIN_FAILURES_LOCK:
        LOGIN_FAILURES.pop(address, None)


def login_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in"):
            if request.path.startswith("/api"):
                return jsonify({"error": "Unauthorized"}), 401
            return redirect("/login")
        return func(*args, **kwargs)

    return wrapper


def is_safe_text(value, max_len=50000):
    return isinstance(value, str) and "\x00" not in value and len(value) <= max_len


def is_true(value):
    return str(value).lower() == "true"


def read_config_text():
    if not os.path.exists(CONFIG_FILE):
        return ""
    with open(CONFIG_FILE, "r", encoding="utf-8") as file:
        return file.read()


# 模板结构版本：templates/default.yaml 第一行的 "# mosctl-template: N"。
# config.yaml 只在安装时从模板复制，面板升级不会动它；没有标记的旧配置按 v0 处理
TEMPLATE_VERSION_RE = re.compile(r"^\s*#\s*mosctl-template\s*:\s*(\d+)\s*$")


def template_version(text):
    # 只看前 5 行，容忍多余空白；找不到标记返回 0
    for line in str(text or "").splitlines()[:5]:
        match = TEMPLATE_VERSION_RE.match(line)
        if match:
            return int(match.group(1))
    return 0


def template_version_info():
    latest = 0
    if os.path.exists(DEFAULT_TEMPLATE_FILE):
        with open(DEFAULT_TEMPLATE_FILE, "r", encoding="utf-8") as file:
            latest = template_version(file.read())
    current = template_version(read_config_text())
    return {
        "config_template_version": current,
        "latest_template_version": latest,
        "config_outdated": current < latest,
    }


def tag_addr_pattern(tag):
    # "- addr: xxx # <TAG>" 行；TAG 后面只允许空白到行尾，
    # 所以 TAG_LOCAL 不会误中 TAG_LOCAL_BACKUP。group(1)=前缀，group(2)=" # TAG" 尾巴
    return re.compile(r'(?m)^(\s*-\s*addr:\s*)["\']?[^"\'#\n]+["\']?(\s*#\s*' + re.escape(tag) + r'\s*)$')


def tag_addr_value(text, tag):
    match = re.search(r'(?m)^\s*-\s*addr:\s*["\']?([^"\'#\n]+)["\']?\s*#\s*' + re.escape(tag) + r'\s*$', text or "")
    return match.group(1).strip() if match else ""


def replace_tag_addr(text, tag, value):
    # 返回 (新文本, 替换次数)
    return tag_addr_pattern(tag).subn(quoted_replacer(value.strip()), text, count=1)


def parse_config_values():
    text = read_config_text()

    ttl_match = re.search(r"(?m)^\s*lazy_cache_ttl:\s*(\d+)\s*$", text)
    local_raw = tag_addr_value(text, "TAG_LOCAL")
    backup_raw = tag_addr_value(text, "TAG_LOCAL_BACKUP")
    remote_raw = tag_addr_value(text, "TAG_REMOTE")

    return {
        "ttl": ttl_match.group(1) if ttl_match else "",
        "local_dns": display_upstream(local_raw, "udp"),
        "local_dns_backup": display_upstream(backup_raw, "udp"),
        "remote_dns": display_upstream(remote_raw, None),
        "local_dns_raw": local_raw,
        "local_dns_backup_raw": backup_raw,
        "remote_dns_raw": remote_raw,
    }


def line_indent(line):
    return len(line) - len(line.lstrip(" "))


def item_end(lines, start):
    # start 是 "- addr:" 行；返回这个列表项结束后的下一行下标（缩进更深的续行都算本项）
    dash_indent = line_indent(lines[start])
    end = start + 1
    while end < len(lines) and lines[end].strip() and line_indent(lines[end]) > dash_indent:
        end += 1
    return end


def set_local_backup(text, backup):
    """在 TAG_LOCAL 所在的 forward 插件里写入/删除 TAG_LOCAL_BACKUP 备用上游，返回 (新文本, 错误)。

    backup 为空：删掉备用行；该插件的 concurrent 大于剩余上游数时降到剩余上游数
    （mosdns 不会报错，但会按 us[(r+i)%len(us)] 轮转，对同一个上游重复发查询）。
    backup 非空：有备用行就替换地址；没有（旧配置）就插到 TAG_LOCAL 这一项之后，
    缩进相同，并保证该插件 args 里有 concurrent: 2。
    """
    lines = text.splitlines(keepends=True)
    local_pattern = tag_addr_pattern("TAG_LOCAL")
    backup_pattern = tag_addr_pattern("TAG_LOCAL_BACKUP")
    local_idx = next((i for i, line in enumerate(lines) if local_pattern.match(line.rstrip("\n"))), None)
    if local_idx is None:
        return text, "没有找到 TAG_LOCAL"
    dash_indent = line_indent(lines[local_idx])

    # 往上找 upstreams: 键，它的缩进就是 args 下各字段的缩进
    upstreams_idx = None
    for i in range(local_idx - 1, -1, -1):
        if not lines[i].strip():
            continue
        if line_indent(lines[i]) < dash_indent and re.match(r"^\s*upstreams:\s*$", lines[i]):
            upstreams_idx = i
            break
        if line_indent(lines[i]) < dash_indent:
            break
    if upstreams_idx is None:
        return text, "没有找到 TAG_LOCAL 所在的 upstreams"
    args_indent = line_indent(lines[upstreams_idx])

    def args_range():
        # 返回 (起, 止)：插件 args 下缩进 >= args_indent 的连续区域
        start = upstreams_idx
        while start - 1 >= 0 and (not lines[start - 1].strip() or line_indent(lines[start - 1]) >= args_indent):
            start -= 1
        end = upstreams_idx + 1
        while end < len(lines) and (not lines[end].strip() or line_indent(lines[end]) >= args_indent):
            end += 1
        return start, end

    def find_backup():
        start, end = args_range()
        return next((i for i in range(start, end) if backup_pattern.match(lines[i].rstrip("\n"))), None)

    def set_concurrent(value, raise_only=False, lower_only=False, insert=True):
        # raise_only：已有值 >= value 就不动；lower_only：已有值 <= value 就不动
        start, end = args_range()
        for i in range(start, end):
            match = re.match(r"^(\s*concurrent:\s*)(\d+)(\s*)$", lines[i].rstrip("\n"))
            if match and line_indent(lines[i]) == args_indent:
                current = int(match.group(2))
                if (raise_only and current >= value) or (lower_only and current <= value):
                    return
                lines[i] = f"{match.group(1)}{value}{match.group(3)}\n"
                return
        if insert:
            lines.insert(upstreams_idx, " " * args_indent + f"concurrent: {value}\n")

    backup_idx = find_backup()
    if not str(backup or "").strip():
        if backup_idx is None:
            return text, None
        del lines[backup_idx:item_end(lines, backup_idx)]
        start, end = args_range()
        remaining = sum(1 for i in range(start, end) if re.match(r"^\s*-\s*addr:", lines[i]))
        set_concurrent(max(1, remaining), lower_only=True, insert=False)
        return "".join(lines), None

    if backup_idx is not None:
        lines[backup_idx] = backup_pattern.sub(quoted_replacer(backup.strip()), lines[backup_idx].rstrip("\n"), count=1) + "\n"
    else:
        if not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        insert_at = item_end(lines, local_idx)
        lines.insert(insert_at, " " * dash_indent + f'- addr: "{backup.strip()}" # TAG_LOCAL_BACKUP\n')
    set_concurrent(2, raise_only=True)
    return "".join(lines), None


def display_upstream(value, default_scheme=None):
    value = str(value or "").strip()
    if default_scheme and value.startswith(f"{default_scheme}://"):
        value = value[len(default_scheme) + 3 :]
    if "://" not in value and value.endswith(":53"):
        value = value[:-3]
    return value


# mosdns forward 插件支持的上游协议；不在列表里的（http://、doh:// 之类）直接拒绝
UPSTREAM_SCHEMES = ("udp", "tcp", "tls", "https", "h3", "quic", "doq", "tcp+pipeline", "tls+pipeline")


def bracket_bare_ipv6(value):
    # "2400:3200::1" 这种没加方括号的 IPv6 会被当成 host:port 解析，这里补上方括号。
    # value 是去掉 scheme:// 之后的部分；带路径（https://host/path）时只看第一段
    netloc, slash, rest = value.partition("/")
    if netloc.startswith("[") or netloc.count(":") < 2:
        return value
    try:
        ipaddress.IPv6Address(netloc)
    except ValueError:
        return value
    return f"[{netloc}]{slash}{rest}"


def normalize_upstream(value, default_scheme=None, default_port=None):
    """校验并规范化上游地址，返回 (地址, 错误信息)；错误时地址为空串。

    default_scheme：没写协议时补上（国内上游补 udp://）；
    default_port：没写协议也没写端口时补上端口（国外上游保持 host:port 形式，和模板一致）。
    """
    value = str(value or "").strip()
    if not value:
        return "", "不能为空"
    if any(char in value for char in '"\\#'):
        return "", "不能包含双引号、反斜杠或 #"
    if any(char.isspace() for char in value):
        return "", "不能包含空格"
    scheme, sep, rest = value.partition("://")
    if not sep:
        scheme, rest = (default_scheme or ""), value
    rest = bracket_bare_ipv6(rest)
    probe_scheme = scheme or "udp"
    if probe_scheme.lower() not in UPSTREAM_SCHEMES:
        return "", f"不支持的协议 {probe_scheme}://，可用：" + "、".join(UPSTREAM_SCHEMES)
    try:
        parts = urlsplit(f"{probe_scheme}://{rest}")
        hostname = parts.hostname
        port = parts.port
    except ValueError as exc:
        return "", f"地址格式不正确：{exc}"
    if not hostname:
        return "", "缺少主机名或 IP"
    if scheme:
        return f"{scheme}://{rest}", None
    if default_port and port is None and "/" not in rest:
        return f"{rest}:{default_port}", None
    return rest, None


def wait_mosdns_ready(timeout=None, interval=None):
    """重启后轮询：服务 active 且向 127.0.0.1:53 查国内域名有应答才算就绪。返回 (ok, 说明)。"""
    timeout = RESTART_READY_TIMEOUT if timeout is None else timeout
    interval = RESTART_READY_INTERVAL if interval is None else interval
    deadline = time.monotonic() + timeout
    while True:
        if service_active():
            # 单次查询最多等 0.5 秒：端口还没监听时 UDP 收不到拒绝，只能靠超时，太长会拖慢就绪判断
            remaining = deadline - time.monotonic()
            ok, detail = dns_query(RESTART_READY_DOMAIN, timeout=min(0.5, max(0.2, remaining)))
            # 只要 mosdns 回了一个 DNS 响应（哪怕上游失败返回 SERVFAIL 或空应答）就算启动成功：
            # 这里判断的是“本机 mosdns 起来了”，不能因为国内上游一时故障就把正常的保存误判失败并回滚。
            # 内核自动更新的健康检查仍要求真实解析成功（core_health_check）。
            if ok or "RCODE=" in detail or "没有应答记录" in detail:
                return True, detail
            problem = "DNS 查询失败：" + detail
        else:
            problem = "mosdns 服务不是 active"
        if time.monotonic() >= deadline:
            return False, problem
        time.sleep(interval)


def restart_mosdns():
    """重启 mosdns 并等它真正能解析。返回 (ok, 说明)，成功时说明里带实测耗时。"""
    # 先清掉 start-limit 计数：连续几次启动失败后 systemd 会拒绝再启动，回滚也会被挡住
    run_cmd(["systemctl", "reset-failed", "mosdns"], timeout=10)
    started = time.monotonic()
    ok, output = run_cmd(["systemctl", "restart", "mosdns"], timeout=30)
    if not ok:
        return False, output
    ready, detail = wait_mosdns_ready()
    elapsed = time.monotonic() - started
    prefix = output.strip() + "\n" if output and output.strip() else ""
    if not ready:
        return False, f"{prefix}mosdns 重启后 {RESTART_READY_TIMEOUT:g} 秒内未就绪：{detail}"
    return True, f"{prefix}mosdns 已重启并能正常解析（耗时 {elapsed:.1f}s）"


def tail_lines(text, count=15):
    lines = (text or "").strip().splitlines()
    return "\n".join(lines[-count:])


def free_local_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def port_open(port, timeout=0.3):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def sandbox_config_text(content, tmpdir):
    # 校验用的副本不能碰正在运行的 mosdns：所有 listen 改到回环随机端口、
    # 日志和缓存 dump 指到临时目录、api 监听在一个可探测的本地端口
    api_port = free_local_port()

    def replace_listen(match):
        return f'{match.group(1)}"127.0.0.1:{free_local_port()}"'

    content = re.sub(r'(?m)^(\s*listen:\s*)["\']?[^"\'#\n]*["\']?\s*$', replace_listen, content)
    content = re.sub(
        r'(?m)^(\s*http:\s*)["\']?[^"\'#\n]+["\']?\s*$',
        lambda match: f'{match.group(1)}"127.0.0.1:{api_port}"',
        content,
        count=1,
    )
    log_path = os.path.join(tmpdir, "mosdns.log")
    dump_path = os.path.join(tmpdir, "cache.dump")
    content = re.sub(
        r'(?m)^(\s*file:\s*)["\']?[^"\'#\n]+["\']?\s*$',
        lambda match: f'{match.group(1)}"{log_path}"',
        content,
    )
    content = re.sub(
        r'(?m)^(\s*dump_file:\s*)["\']?[^"\'#\n]+["\']?\s*$',
        lambda match: f'{match.group(1)}"{dump_path}"',
        content,
    )
    return content, api_port


def config_starts(path, wait_seconds=3.0, binary=None):
    # binary 默认是正在用的内核；内核升级时传入新下载的二进制，先确认它能跑当前配置
    binary = binary or MOSDNS_BIN
    proc = None
    tmpdir = None
    try:
        tmpdir = tempfile.mkdtemp(prefix="mosdns-check-")
        check_path = os.path.join(tmpdir, "config.yaml")
        with open(path, "r", encoding="utf-8") as source:
            content = source.read()
        content, api_port = sandbox_config_text(content, tmpdir)
        with open(check_path, "w", encoding="utf-8") as target:
            target.write(content)
        proc = subprocess.Popen(
            [binary, "start", "-d", tmpdir],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        # 进程提前退出视为失败；api 端口能连上视为成功；
        # 否则沿用"存活超过几秒就算能启动"的判断（配置里可能没有 api 段）
        deadline = time.time() + wait_seconds
        api_ready = False
        while time.time() < deadline:
            if proc.poll() is not None:
                stdout, stderr = proc.communicate()
                return False, tail_lines(clean_output(stdout + stderr)) or "mosdns 校验进程异常退出"
            if port_open(api_port):
                api_ready = True
                break
            time.sleep(0.1)
        if proc.poll() is not None:
            stdout, stderr = proc.communicate()
            return False, tail_lines(clean_output(stdout + stderr)) or "mosdns 校验进程异常退出"
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
        return True, "配置可以启动" + ("（API 端口已响应）" if api_ready else "")
    except Exception as exc:
        if proc:
            try:
                proc.kill()
            except Exception:
                pass
        return False, str(exc)
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


def upstream_value_error(value, default_scheme=None):
    # 上游地址会被拼进 YAML 的双引号字符串和 # TAG 注释行，字符/协议/主机名都在 normalize_upstream 里查
    return normalize_upstream(value, default_scheme=default_scheme)[1]


def config_text_starts(content):
    # config_starts 只接受文件路径：把候选内容写到临时文件再校验，不碰 config.yaml
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".yaml", prefix="mosdns-candidate-", delete=False) as file:
        file.write(content)
        path = file.name
    try:
        return config_starts(path)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def rule_content_starts(rule_id, content):
    # 规则文件的校验：把当前 config.yaml 里指向 rules/ 的路径改到临时目录，
    # 临时目录里放候选规则文件，其他规则文件用符号链接指回原文件，再起一个沙箱 mosdns
    meta = RULE_FILES.get(rule_id)
    if not meta:
        return False, "未知规则文件"
    config_text = read_config_text()
    if not config_text.strip():
        return False, "配置文件不存在，无法校验规则"
    rules_dir = os.path.dirname(meta["path"])
    tmpdir = tempfile.mkdtemp(prefix="mosdns-rules-")
    try:
        tmp_rules = os.path.join(tmpdir, "rules")
        os.makedirs(tmp_rules)
        candidate_name = os.path.basename(meta["path"])
        with open(os.path.join(tmp_rules, candidate_name), "w", encoding="utf-8") as file:
            file.write(content)
        if os.path.isdir(rules_dir):
            for name in os.listdir(rules_dir):
                source = os.path.join(rules_dir, name)
                if name == candidate_name or not os.path.isfile(source):
                    continue
                target = os.path.join(tmp_rules, name)
                try:
                    os.symlink(source, target)
                except OSError:
                    shutil.copy2(source, target)
        rewritten = config_text.replace(rules_dir.rstrip("/") + "/", tmp_rules + "/")
        return config_text_starts(rewritten)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def quoted_replacer(value):
    # 用函数而不是 f-string 模板做替换，值里的 \g<1>、\1 之类不会被当作反向引用
    return lambda match: f'{match.group(1)}"{value}"{match.group(2)}'


def write_config_text(new_text):
    tmp_file = f"{CONFIG_FILE}.webtmp"
    with open(tmp_file, "w", encoding="utf-8") as file:
        file.write(new_text)
    os.replace(tmp_file, CONFIG_FILE)


def restore_rollbacks(rollbacks):
    # rollbacks: [(备份路径或 None, 目标文件)]。None 表示写入前文件不存在，回滚即删除
    restored = False
    for backup_path, target in rollbacks or []:
        try:
            if backup_path and os.path.exists(backup_path):
                shutil.copy2(backup_path, target)
                restored = True
            elif backup_path is None and os.path.exists(target):
                os.remove(target)
                restored = True
        except OSError:
            pass
    return restored


def restart_or_rollback(rollbacks, success_message, failure_prefix):
    # 重启失败就把备份拷回去再重启一次，和 restore_backup 的处理方式一致
    ok, message = restart_mosdns()
    if ok:
        return True, success_message
    if not restore_rollbacks(rollbacks):
        return False, f"{failure_prefix}，mosdns 重启失败，且没有可回滚的备份：\n{message}"
    restart_ok, restart_message = restart_mosdns()
    text = f"{failure_prefix}，但 mosdns 重启失败，已回滚到修改前的文件：\n{message}"
    if not restart_ok:
        text += "\n\n回滚后重启仍失败，请手动检查：\n" + restart_message
    return False, text


def notify_if_rolled_back(subject, result):
    """包住 restart_or_rollback 的结果：失败（= 改动后 mosdns 重启失败）时发 ❌ 通知，结果原样返回。

    解析策略里的手动修改也要通知：回滚前服务已经中断过，回滚后重启也可能仍失败。
    """
    ok, message = result
    if not ok:
        text = str(message or "")
        lines = ["改动后 mosdns 重启失败"]
        if "没有可回滚的备份" in text:
            lines.append("没有可回滚的备份，请立即检查")
        elif "回滚后重启仍失败" in text:
            lines += ["已恢复修改前的文件，但重启仍失败", "请立即检查，必要时启用救援模式"]
        else:
            lines.append("已恢复修改前的文件，服务已重启")
        notify_event("failure", subject, lines, background=True)
    return ok, message


def restore_default_template():
    ok, message, _details = restore_default_template_details()
    return ok, message


def migrate_to_current_template():
    """把旧模板站点迁到当前内置模板（不挂路由，供 python 直接调用）。

    等同 restore_default_template()，但返回 dict：carried 是带过去的
    local / local_backup / remote / ttl（local_backup 为空表示旧配置没有，用模板默认值），
    sandbox_ok 表示沙箱校验是否通过；校验不通过时什么都不改。
    """
    ok, message, details = restore_default_template_details()
    return {"success": ok, "message": message, **details}


def restore_default_template_details():
    details = {"sandbox_ok": False, "carried": {}}
    if not os.path.exists(DEFAULT_TEMPLATE_FILE):
        return False, "内置默认模板不存在", details

    current = parse_config_values()
    with open(DEFAULT_TEMPLATE_FILE, "r", encoding="utf-8") as file:
        content = file.read()

    local_dns = current.get("local_dns_raw") or normalize_upstream(current.get("local_dns", ""), default_scheme="udp")[0]
    local_backup = current.get("local_dns_backup_raw", "")
    remote_dns = current.get("remote_dns_raw") or normalize_upstream(current.get("remote_dns", ""), default_port=53)[0]
    ttl = current.get("ttl") or "86400"
    if not re.fullmatch(r"\d{1,7}", ttl):
        ttl = "86400"

    # 当前配置里解析出的上游如果含非法字符，就保留模板默认值而不是拼进去
    carried = {"local": "", "local_backup": "", "remote": "", "ttl": ttl}
    if local_dns.strip() and not upstream_value_error(local_dns):
        content = replace_tag_addr(content, "TAG_LOCAL", local_dns)[0]
        carried["local"] = local_dns.strip()
    # 旧配置没有备用行时保留模板默认的备用上游
    if local_backup.strip() and not upstream_value_error(local_backup):
        content = replace_tag_addr(content, "TAG_LOCAL_BACKUP", local_backup)[0]
        carried["local_backup"] = local_backup.strip()
    if remote_dns.strip() and not upstream_value_error(remote_dns):
        content = replace_tag_addr(content, "TAG_REMOTE", remote_dns)[0]
        carried["remote"] = remote_dns.strip()
    content = re.sub(
        r"(?m)^(\s*lazy_cache_ttl:\s*)\d+\s*$",
        lambda match: match.group(1) + ttl,
        content,
        count=1,
    )

    details["carried"] = carried

    tmp_file = f"{CONFIG_FILE}.defaultcheck"
    with open(tmp_file, "w", encoding="utf-8") as file:
        file.write(content)
    ok, message = config_starts(tmp_file)
    if not ok:
        try:
            os.remove(tmp_file)
        except OSError:
            pass
        return False, "内置默认模板校验失败，未替换当前配置：\n" + message, details
    details["sandbox_ok"] = True

    backup = backup_file(CONFIG_FILE, "config")
    os.replace(tmp_file, CONFIG_FILE)
    ok, message = restart_or_rollback(
        [(backup, CONFIG_FILE)],
        "已恢复内置默认配置，并保留当前上游 DNS（含备用国内 DNS）与 TTL。",
        "默认配置已写入",
    )
    ok, message = notify_if_rolled_back("恢复默认配置已回滚", (ok, message))
    return ok, message, details


def mosdns_asset_name():
    ok, arch = run_cmd(["uname", "-m"], timeout=10)
    arch = arch.strip()
    if arch in ("x86_64", "amd64"):
        return "mosdns-linux-amd64.zip"
    if arch in ("aarch64", "arm64"):
        return "mosdns-linux-arm64.zip"
    if arch.startswith("armv7"):
        return "mosdns-linux-arm-7.zip"
    if arch.startswith("armv6"):
        return "mosdns-linux-arm-6.zip"
    if arch.startswith("armv5"):
        return "mosdns-linux-arm-5.zip"
    return None


def version_tuple(value):
    match = re.search(r"v?(\d+)\.(\d+)\.(\d+)", str(value or ""))
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def clean_version(value):
    match = re.search(r"v?(\d+\.\d+\.\d+)", str(value or ""))
    if not match:
        return str(value or "未知")
    return "v" + match.group(1)


def download_file(urls, target, max_bytes=DOWNLOAD_MAX_BYTES, time_budget=DOWNLOAD_TIME_BUDGET):
    # 分块下载：超过大小上限或总耗时预算就中止，不让一个异常的源把磁盘或请求拖死
    last_error = ""
    started = time.time()
    for url in urls:
        remaining = time_budget - (time.time() - started)
        if remaining <= 0:
            last_error = "下载总耗时超过预算，已中止"
            break
        try:
            req = urlrequest.Request(url, headers={"User-Agent": "mosdns-web-manager"})
            written = 0
            with urlrequest.urlopen(req, timeout=min(90, max(5, remaining))) as resp, open(target, "wb") as file:
                declared = resp.headers.get("Content-Length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    raise ValueError(f"文件过大（{int(declared)} 字节），超过 {max_bytes} 字节上限")
                while True:
                    chunk = resp.read(256 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > max_bytes:
                        raise ValueError(f"文件超过 {max_bytes} 字节上限")
                    if time.time() - started > time_budget:
                        raise ValueError("下载总耗时超过预算，已中止")
                    file.write(chunk)
            if written > 0:
                return True, url
            last_error = "下载内容为空"
        except Exception as exc:
            last_error = str(exc)
            try:
                os.remove(target)
            except OSError:
                pass
    return False, last_error


def verify_zip_file(path):
    # IrineSistiana/mosdns 的 release 只提供各平台 zip，没有 checksum 文件，
    # 这里只能做 zip 完整性校验（CRC），等价于 unzip -t
    try:
        with zipfile.ZipFile(path) as archive:
            bad = archive.testzip()
        if bad:
            return False, f"zip 内文件校验失败：{bad}"
        return True, ""
    except zipfile.BadZipFile:
        return False, "下载文件不是有效 zip"
    except Exception as exc:
        return False, str(exc)


def mosctl_repo_settings():
    env = read_env()
    return {
        "repo_url": env.get("MOSCTL_REPO_URL") or DEFAULT_MOSCTL_REPO_URL,
        "branch": env.get("MOSCTL_BRANCH") or DEFAULT_MOSCTL_BRANCH,
    }


def github_repo_parts(repo_url):
    repo = str(repo_url or "").strip()
    repo = repo[:-4] if repo.endswith(".git") else repo
    match = re.match(r"^https://github\.com/([^/\s]+)/([^/\s]+)$", repo)
    if not match:
        return None
    return match.groups()


def github_archive_url(repo_url, branch):
    parts = github_repo_parts(repo_url)
    if not parts:
        return ""
    owner, name = parts
    return f"https://github.com/{owner}/{name}/archive/refs/heads/{quote(branch, safe='/')}.zip"


COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def github_commit_archive_url(repo_url, sha):
    # 按提交下载：自动更新判定"最新提交满 N 天"后装的就是那一个提交，不会被期间新推的提交顶掉
    parts = github_repo_parts(repo_url)
    if not parts or not COMMIT_SHA_RE.match(str(sha or "")):
        return ""
    owner, name = parts
    return f"https://github.com/{owner}/{name}/archive/{sha}.zip"


def github_commits_api_url(repo_url, branch, path="remote-root"):
    parts = github_repo_parts(repo_url)
    if not parts:
        return ""
    owner, name = parts
    return (
        f"https://api.github.com/repos/{owner}/{name}/commits"
        f"?path={quote(path, safe='/')}&sha={quote(branch, safe='')}&per_page=1"
    )


def parse_github_time(value):
    # GitHub 时间都是 2026-10-01T12:34:56Z；解析失败返回 None
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def latest_panel_commit(settings=None):
    """MOSCTL_BRANCH 上最近一次改动 remote-root/ 的提交；先直连 GitHub API，失败再走 GH_PROXY。"""
    settings = settings or mosctl_repo_settings()
    url = github_commits_api_url(settings["repo_url"], settings["branch"])
    if not url:
        return {"success": False, "message": "仅支持 GitHub 仓库，请检查 MOSCTL_REPO_URL"}
    ok, text, source = read_url_text(github_url_candidates(url), timeout=15)
    if not ok:
        return {"success": False, "message": "GitHub 提交接口不可用：" + text}
    try:
        data = json.loads(text)
        item = data[0]
        sha = str(item["sha"])
        date = str(item["commit"]["committer"]["date"])
    except (ValueError, TypeError, KeyError, IndexError):
        return {"success": False, "message": "GitHub 提交接口返回了无法识别的数据"}
    committed_at = parse_github_time(date)
    if not COMMIT_SHA_RE.match(sha) or committed_at is None:
        return {"success": False, "message": "GitHub 提交接口返回了无法识别的数据"}
    return {"success": True, "sha": sha, "date": date, "committed_at": committed_at, "source": source}


def github_raw_app_url(repo_url, branch):
    parts = github_repo_parts(repo_url)
    if not parts:
        return ""
    owner, name = parts
    return f"https://raw.githubusercontent.com/{owner}/{name}/{quote(branch, safe='/')}/remote-root/etc/mosdns/manager/app.py"


def github_contents_app_url(repo_url, branch):
    parts = github_repo_parts(repo_url)
    if not parts:
        return ""
    owner, name = parts
    path = "remote-root/etc/mosdns/manager/app.py"
    return f"https://api.github.com/repos/{owner}/{name}/contents/{path}?ref={quote(branch, safe='/')}"


def parse_github_contents_text(text):
    try:
        data = json.loads(text or "{}")
    except ValueError:
        return ""
    if not isinstance(data, dict):
        return ""
    if data.get("encoding") != "base64" or not data.get("content"):
        return ""
    payload = str(data.get("content") or "").replace("\n", "")
    try:
        return base64.b64decode(payload, validate=False).decode("utf-8", "replace")
    except (ValueError, TypeError):
        return ""


def cache_bust_url(url):
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}_mosctl_ts={int(time.time())}"


def read_url_text(urls, timeout=15):
    last_error = ""
    for url in urls:
        try:
            req = urlrequest.Request(url, headers={"User-Agent": "mosdns-web-manager"})
            with urlrequest.urlopen(req, timeout=timeout) as resp:
                return True, resp.read().decode("utf-8", "replace"), url
        except Exception as exc:
            last_error = str(exc)
    return False, last_error, ""


def panel_version_tuple(value):
    match = re.search(r"v?(\d+)\.(\d+)\.(\d+)", str(value or ""))
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def parse_panel_version(text):
    match = re.search(r'(?m)^PANEL_VERSION\s*=\s*["\']([^"\']+)["\']\s*$', text or "")
    return match.group(1).strip() if match else ""


def fetch_remote_panel_version(settings):
    raw_url = github_raw_app_url(settings["repo_url"], settings["branch"])
    contents_url = github_contents_app_url(settings["repo_url"], settings["branch"])
    if not raw_url or not contents_url:
        return {
            "success": False,
            "latest_version": "",
            "source": "",
            "message": "仅支持 GitHub 仓库在线检测，请检查 MOSCTL_REPO_URL",
        }
    raw_url = cache_bust_url(raw_url)
    # 顺序：GitHub API 直连 → raw 直连 → 代理 raw。都失败就报"未知"，
    # 不再为了读一个版本号去下载整个仓库 zip
    urls = [contents_url, raw_url] + github_url_candidates(raw_url)[1:]
    ok, text, source = read_url_text(urls, timeout=15)
    if not ok:
        return {"success": False, "latest_version": "", "source": "", "message": "检测远端面板版本失败（未知）：\n" + text}
    if source == contents_url:
        text = parse_github_contents_text(text)
    version = parse_panel_version(text)
    if version:
        return {"success": True, "latest_version": version, "source": source, "message": ""}
    return {
        "success": False,
        "latest_version": "",
        "source": source,
        "message": "远端面板没有版本号，可能是旧版本，已禁止在线升级以避免降级。",
    }


def remote_panel_version(settings=None, force=False):
    # 结果在内存里缓存 1 小时：每次打开页面都会检测一次，不能每次都打 GitHub
    settings = settings or mosctl_repo_settings()
    key = f"{settings['repo_url']}@{settings['branch']}"
    now = time.time()
    with REMOTE_VERSION_LOCK:
        cached = REMOTE_VERSION_CACHE
        if (
            not force
            and cached["result"] is not None
            and cached["key"] == key
            and now - cached["at"] < REMOTE_VERSION_CACHE_TTL
        ):
            return dict(cached["result"], cached=True)
        result = fetch_remote_panel_version(settings)
        REMOTE_VERSION_CACHE.update({"key": key, "at": now, "result": result})
        return dict(result, cached=False)


def panel_upgrade_state(force=False):
    settings = mosctl_repo_settings()
    remote = remote_panel_version(settings, force=force)
    current_tuple = panel_version_tuple(PANEL_VERSION)
    latest_tuple = panel_version_tuple(remote.get("latest_version"))
    update_available = bool(remote.get("success") and current_tuple and latest_tuple and latest_tuple > current_tuple)
    return {
        **settings,
        "archive_url": github_archive_url(settings["repo_url"], settings["branch"]),
        "supported": bool(github_archive_url(settings["repo_url"], settings["branch"])),
        "current_version": PANEL_VERSION,
        "latest_version": remote.get("latest_version", ""),
        "update_available": update_available,
        "check_success": remote.get("success", False),
        "cached": bool(remote.get("cached")),
        "source": remote.get("source", ""),
        "message": remote.get("message", ""),
    }


def download_mosctl_source(tmpdir, ref=None):
    settings = mosctl_repo_settings()
    if ref:
        archive_url = github_commit_archive_url(settings["repo_url"], ref)
    else:
        archive_url = github_archive_url(settings["repo_url"], settings["branch"])
    if not archive_url:
        return False, "仅支持 GitHub 仓库在线升级，请检查 MOSCTL_REPO_URL", None, settings
    archive_url = cache_bust_url(archive_url)

    zip_path = os.path.join(tmpdir, "mosctl-panel.zip")
    ok, source = download_file(github_url_candidates(archive_url), zip_path)
    if not ok:
        return False, "下载 Mosctl 面板失败：\n" + source, None, settings

    zip_ok, zip_message = verify_zip_file(zip_path)
    if not zip_ok:
        return False, f"{zip_message}，已取消升级", None, settings
    try:
        with zipfile.ZipFile(zip_path) as archive:
            archive.extractall(tmpdir)
    except zipfile.BadZipFile:
        return False, "下载文件不是有效 zip，已取消升级", None, settings

    for root, dirs, _ in os.walk(tmpdir):
        if "remote-root" in dirs:
            source_root = os.path.join(root, "remote-root")
            app_path = os.path.join(source_root, "etc/mosdns/manager/app.py")
            cli_path = os.path.join(source_root, "usr/local/bin/mosctl")
            if not os.path.exists(app_path) or not os.path.exists(cli_path):
                continue
            ok, message = run_cmd(["python3", "-m", "py_compile", app_path], timeout=20)
            if not ok:
                return False, "新面板 app.py 校验失败，已取消升级：\n" + message, None, settings
            with open(app_path, "r", encoding="utf-8") as file:
                remote_version = parse_panel_version(file.read())
            settings["remote_version"] = remote_version
            return True, source, source_root, settings
    return False, "安装包中没有找到有效的 remote-root，已取消升级", None, settings


def panel_managed_targets():
    return [
        (MANAGER_DIR, "etc/mosdns/manager", "dir", 0o755),
        (MOSCTL, "usr/local/bin/mosctl", "file", 0o755),
        (DEFAULT_TEMPLATE_FILE, "etc/mosdns/templates/default.yaml", "file", 0o644),
        (f"{SYSTEMD_DIR}/mosdns.service", "etc/systemd/system/mosdns.service", "file", 0o644),
        (f"{SYSTEMD_DIR}/mosdns-rescue.service", "etc/systemd/system/mosdns-rescue.service", "file", 0o644),
        (f"{SYSTEMD_DIR}/mosdns-web.service", "etc/systemd/system/mosdns-web.service", "file", 0o644),
        ("/etc/sysctl.d/99-mosdns.conf", "etc/sysctl.d/99-mosdns.conf", "file", 0o644),
        ("/etc/logrotate.d/mosdns", "etc/logrotate.d/mosdns", "file", 0o644),
    ]


def backup_panel_targets():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d%H%M%S")
    backup_root = f"{BACKUP_DIR}/mosctl-panel.{stamp}"
    os.makedirs(backup_root, exist_ok=True)
    manifest = []
    for target, _, kind, _ in panel_managed_targets():
        backup_path = os.path.join(backup_root, target.lstrip("/"))
        existed = os.path.exists(target)
        manifest.append({"target": target, "kind": kind, "existed": existed})
        if not existed:
            continue
        os.makedirs(os.path.dirname(backup_path), exist_ok=True)
        if os.path.isdir(target):
            shutil.copytree(target, backup_path)
        else:
            shutil.copy2(target, backup_path)
    with open(os.path.join(backup_root, "manifest.json"), "w", encoding="utf-8") as file:
        json.dump(manifest, file)
    return backup_root


def restore_panel_backup(backup_root):
    manifest_path = os.path.join(backup_root, "manifest.json")
    if not os.path.exists(manifest_path):
        return
    with open(manifest_path, "r", encoding="utf-8") as file:
        manifest = json.load(file)
    for item in manifest:
        target = item["target"]
        backup_path = os.path.join(backup_root, target.lstrip("/"))
        if os.path.isdir(target):
            shutil.rmtree(target, ignore_errors=True)
        elif os.path.exists(target):
            os.remove(target)
        if not item.get("existed"):
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if item.get("kind") == "dir":
            shutil.copytree(backup_path, target)
        else:
            shutil.copy2(backup_path, target)


def cleanup_panel_backups():
    backups = [path for path in glob.glob(f"{BACKUP_DIR}/mosctl-panel.*") if os.path.isdir(path)]
    backups.sort(key=lambda path: os.path.getmtime(path), reverse=True)
    for path in backups[PANEL_BACKUP_KEEP_COUNT:]:
        shutil.rmtree(path, ignore_errors=True)


def install_panel_payload(source_root):
    for target, relative, kind, mode in panel_managed_targets():
        source = os.path.join(source_root, relative)
        if not os.path.exists(source):
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if kind == "dir":
            if os.path.isdir(target):
                shutil.rmtree(target)
            shutil.copytree(source, target)
        else:
            shutil.copy2(source, target)
            os.chmod(target, mode)
    run_cmd(["systemctl", "daemon-reload"], timeout=20)


def schedule_web_restart():
    subprocess.Popen(
        ["sh", "-c", "sleep 1; systemctl restart mosdns-web"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )


def upgrade_mosctl_panel(ref=None, on_install=None):
    # ref：指定提交（自动更新用）；on_install(新版本号)：确认要装、替换文件之前回调，用于记录"开始 → vX"
    with tempfile.TemporaryDirectory() as tmpdir:
        ok, source, source_root, settings = download_mosctl_source(tmpdir, ref=ref)
        if not ok:
            return False, source, False

        remote_version = settings.get("remote_version", "")
        current_tuple = panel_version_tuple(PANEL_VERSION)
        remote_tuple = panel_version_tuple(remote_version)
        if not remote_tuple:
            return False, "远端面板没有版本号，可能是旧版本，已取消升级以避免降级。", False
        if current_tuple and remote_tuple <= current_tuple:
            return (
                True,
                "当前已是最新版本，无需更新。\n"
                f"当前版本：v{PANEL_VERSION}\n"
                f"远端版本：v{remote_version}",
                False,
            )

        if on_install:
            on_install(remote_version)
        backup_root = backup_panel_targets()
        try:
            install_panel_payload(source_root)
            cleanup_panel_backups()
        except Exception as exc:
            restore_panel_backup(backup_root)
            run_cmd(["systemctl", "daemon-reload"], timeout=20)
            return False, "Mosctl 面板升级失败，已回滚旧文件：\n" + str(exc), False

    schedule_web_restart()
    return (
        True,
        "Mosctl 面板升级完成，Web 服务将在 1 秒后重启，页面会在服务恢复后自动刷新。\n"
        f"来源：{source}\n"
        f"仓库：{settings['repo_url']}\n"
        f"分支：{settings['branch']}\n"
        f"旧版本：v{PANEL_VERSION}\n"
        f"新版本：v{remote_version}\n"
        f"旧面板备份：{backup_root}",
        True,
    )


def build_dns_query(name, query_id, qtype=1):
    header = struct.pack("!HHHHHH", query_id, 0x0100, 1, 0, 0, 0)
    labels = [label for label in str(name).strip(".").split(".") if label]
    qname = b"".join(bytes([len(label)]) + label.encode("idna") for label in labels) + b"\x00"
    return header + qname + struct.pack("!HH", qtype, 1)


def parse_dns_answer_count(data, query_id):
    # 只看报文头：ID 对得上、是响应、RCODE=0、至少一条应答记录
    if len(data) < 12:
        return False, "响应过短"
    rid, flags, _, ancount, _, _ = struct.unpack("!HHHHHH", data[:12])
    if rid != query_id or not flags & 0x8000:
        return False, "响应不匹配"
    rcode = flags & 0x000F
    if rcode:
        return False, f"RCODE={rcode}"
    if not ancount:
        return False, "没有应答记录"
    return True, f"{ancount} 条应答"


def dns_query(name, server=None, timeout=2.0):
    """向本机 mosdns 发一个真实的 UDP A 记录查询（不依赖 dig）。返回 (ok, 说明)。"""
    server = server or CORE_HEALTH_DNS_SERVER
    query_id = secrets.randbelow(65536)
    try:
        packet = build_dns_query(name, query_id)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.sendto(packet, server)
            deadline = time.time() + timeout
            while True:
                data, _ = sock.recvfrom(4096)
                ok, detail = parse_dns_answer_count(data, query_id)
                if ok or detail != "响应不匹配" or time.time() >= deadline:
                    return ok, f"{name}：{detail}"
    except Exception as exc:
        return False, f"{name}：{exc or '查询超时'}"


def core_health_check(expected_version=None, timeout=None):
    """内核换完后的健康检查：timeout 秒内反复检查，直到服务 active、版本对、国内外域名都有应答。"""
    timeout = CORE_HEALTH_TIMEOUT if timeout is None else timeout
    deadline = time.time() + timeout
    expected = version_tuple(expected_version) if expected_version else None
    while True:
        problems = []
        if not service_active():
            problems.append("mosdns 服务不是 active")
        else:
            if expected:
                reported = get_version()
                if version_tuple(reported) != expected:
                    problems.append(f"mosdns version 报告 {clean_version(reported)}，预期 {clean_version(expected_version)}")
            for name in CORE_HEALTH_DOMAINS:
                ok, detail = dns_query(name)
                if not ok:
                    problems.append("DNS 查询失败：" + detail)
        if not problems:
            return True, "健康检查通过：服务运行中、版本正确、国内外域名均有应答"
        if time.time() >= deadline:
            return False, f"健康检查失败（{timeout} 秒内未恢复）：\n" + "\n".join(problems)
        time.sleep(CORE_HEALTH_INTERVAL)


def health_failure_summary(message):
    """把 core_health_check 的失败说明归纳成一句给通知用的话。"""
    labels = []
    text = str(message or "")
    if "服务不是 active" in text:
        labels.append("服务")
    if "version 报告" in text:
        labels.append("版本")
    for domain, label in zip(CORE_HEALTH_DOMAINS, ("国内解析", "国外解析")):
        if f"DNS 查询失败：{domain}" in text:
            labels.append(label)
    return "健康检查未通过：" + "、".join(labels) if labels else "新内核启动失败"


def replace_binary(source, target):
    # 先拷到同目录临时文件再 rename：不会出现半个二进制，也不怕 "Text file busy"
    tmp_path = f"{target}.mosctl-new"
    shutil.copy2(source, tmp_path)
    os.chmod(tmp_path, 0o755)
    os.replace(tmp_path, target)


def mosdns_release_asset_url(tag, asset):
    return f"{MOSDNS_RELEASE_DOWNLOAD}/{quote(str(tag), safe='')}/{asset}"


def mosdns_stable_releases():
    """官方 release 列表里的稳定版（排除 draft / prerelease），带发布时间和安装包名。"""
    ok, text, source = read_url_text(github_url_candidates(MOSDNS_RELEASES_API), timeout=15)
    if not ok:
        return {"success": False, "releases": [], "message": "获取 mosdns release 列表失败：" + text}
    try:
        data = json.loads(text)
    except ValueError:
        return {"success": False, "releases": [], "message": "mosdns release 接口返回了非 JSON 数据"}
    if not isinstance(data, list):
        return {"success": False, "releases": [], "message": "mosdns release 接口返回了非列表数据"}
    releases = []
    for item in data:
        if not isinstance(item, dict) or item.get("draft") or item.get("prerelease"):
            continue
        tag = str(item.get("tag_name") or "")
        version = version_tuple(tag)
        if not version:
            continue
        assets = [asset.get("name", "") for asset in item.get("assets") or [] if isinstance(asset, dict)]
        releases.append(
            {
                "tag": tag,
                "version": version,
                "published_at": parse_github_time(item.get("published_at")),
                "assets": assets,
                "url": item.get("html_url", ""),
            }
        )
    return {"success": True, "releases": releases, "source": source}


def select_core_release(releases, current_version, min_age_days, now=None, asset=None):
    """从稳定版里挑"比当前新、发布满 min_age_days 天、有本机安装包"的最高版本；绝不降级。

    返回 {action: update|up_to_date|skipped, release, newest, note}。
    """
    now = time.time() if now is None else now
    current = version_tuple(current_version)
    if not current:
        return {"action": "skipped", "release": None, "newest": None, "note": "无法识别当前内核版本，跳过自动更新"}
    newest = max(releases, key=lambda item: item["version"]) if releases else None
    newer = [item for item in releases if item["version"] > current]
    if not newer:
        return {"action": "up_to_date", "release": None, "newest": newest, "note": f"当前 {clean_version(current_version)} 已是最新稳定版"}
    min_age = max(0, int(min_age_days)) * 86400
    eligible = []
    notes = []
    for item in sorted(newer, key=lambda entry: entry["version"], reverse=True):
        published = item.get("published_at")
        if published is None:
            notes.append(f"{item['tag']} 没有发布时间，跳过")
            continue
        age = now - published
        if age < min_age:
            notes.append(f"{item['tag']} 发布 {age / 86400:.1f} 天，未满 {min_age_days} 天")
            continue
        if asset and asset not in item.get("assets", []):
            notes.append(f"{item['tag']} 没有本机安装包 {asset}")
            continue
        eligible.append(item)
    if not eligible:
        return {"action": "skipped", "release": None, "newest": newest, "note": "；".join(notes) or "没有符合条件的新版本"}
    best = eligible[0]
    note = f"可更新到 {best['tag']}"
    if notes:
        note += "（" + "；".join(notes) + "）"
    return {"action": "update", "release": best, "newest": newest, "note": note}


def install_mosdns_core(release=None):
    """下载并替换 mosdns 内核。release 为 None 时用官方 latest（面板手动升级）。

    步骤：不降级 → 下载 + zip 校验 → 新二进制版本必须等于目标 → 用新二进制在沙盒里跑当前配置
    → 备份旧内核 → 停服务、替换、重启 → 20 秒健康检查 → 不通过就恢复旧内核并复查。
    返回 {result: updated|up_to_date|failed|rolled_back, message, from, to}。
    """
    # summary：给通知用的几行大白话（不含日志原文）；替换前就失败的统一补一句“现有内核未改动”
    outcome = {"result": "failed", "message": "", "from": "", "to": "", "summary": []}

    def done(result, message, summary=None, untouched=True):
        lines = list(summary or [])
        if result == "failed" and untouched:
            lines.append("现有内核未改动")
        outcome.update(result=result, message=message, summary=lines)
        return outcome

    asset = mosdns_asset_name()
    if not asset:
        return done("failed", "当前 CPU 架构暂不支持自动升级", ["当前 CPU 架构没有官方安装包"])

    old_ok, old_version = run_cmd([MOSDNS_BIN, "version"], timeout=10) if os.path.exists(MOSDNS_BIN) else (False, "未知")
    old_version = old_version.splitlines()[0] if old_ok and old_version else old_version
    outcome["from"] = clean_version(old_version) if old_ok else "未知"
    if release is None:
        latest = latest_mosdns_release()
        if not latest.get("success"):
            return done("failed", latest.get("message", "获取最新版本失败，已取消升级"), ["获取最新版本失败"])
        tag = latest.get("latest")
        asset_available = latest.get("asset_available")
        direct_url = f"{MOSDNS_RELEASE_BASE}/{asset}"
    else:
        tag = release.get("tag")
        asset_available = asset in (release.get("assets") or [])
        direct_url = mosdns_release_asset_url(tag, asset)
    current_v = version_tuple(old_version) if old_ok else None
    latest_v = version_tuple(tag)
    outcome["to"] = clean_version(tag)
    if not latest_v:
        return done("failed", f"无法识别目标版本号：{tag or '空'}，已取消升级", ["无法识别目标版本号"])
    if current_v and latest_v <= current_v:
        return done(
            "up_to_date",
            f"当前版本不低于目标版本，已取消升级（不降级）。\n当前版本：{clean_version(old_version)}\n目标版本：{clean_version(tag)}",
        )
    if not asset_available:
        return done("failed", f"{clean_version(tag)} 未发现当前架构安装包：{asset}", ["目标版本没有当前架构的安装包"])
    if not os.path.exists(CONFIG_FILE):
        return done("failed", f"找不到 {CONFIG_FILE}，无法用新内核做沙盒校验，已取消升级", ["找不到当前配置，无法校验新内核"])

    urls = github_url_candidates(direct_url)
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d%H%M%S")
    backup_bin = f"{BACKUP_DIR}/{KERNEL_BACKUP_PREFIX}{stamp}"

    with tempfile.TemporaryDirectory() as tmpdir:
        zip_path = os.path.join(tmpdir, asset)
        ok, source = download_file(urls, zip_path)
        if not ok:
            return done("failed", "下载 mosdns 内核失败：\n" + source, ["下载新内核失败"])

        # 官方 release 没有 checksum 文件，只能做 zip 完整性校验
        zip_ok, zip_message = verify_zip_file(zip_path)
        if not zip_ok:
            return done("failed", f"{zip_message}，已取消升级", ["下载的安装包已损坏"])
        extract_dir = os.path.join(tmpdir, "extract")
        try:
            with zipfile.ZipFile(zip_path) as archive:
                archive.extractall(extract_dir)
        except zipfile.BadZipFile:
            return done("failed", "下载文件不是有效 zip，已取消升级", ["下载的安装包已损坏"])

        candidate = None
        new_version = ""
        for root, _, files in os.walk(extract_dir):
            for name in files:
                path = os.path.join(root, name)
                if name == "mosdns" or name.startswith("mosdns"):
                    try:
                        mode = os.stat(path).st_mode
                        os.chmod(path, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                        test_ok, version_output = run_cmd([path, "version"], timeout=10)
                    except Exception:
                        test_ok, version_output = False, ""
                    if test_ok:
                        candidate = path
                        new_version = version_output.splitlines()[0] if version_output else "未知版本"
                        break
            if candidate:
                break
        if not candidate:
            return done("failed", "压缩包里没有找到可运行的 mosdns 二进制", ["安装包里没有可运行的内核"])
        if version_tuple(new_version) != latest_v:
            return done(
                "failed",
                f"压缩包里的内核报告版本 {clean_version(new_version)}，与目标 {clean_version(tag)} 不符，已取消升级",
                ["安装包里的版本与目标不符"],
            )

        # 先用新内核在沙盒里跑一遍当前配置：跑不起来就放弃，正在用的内核一点不动
        sandbox_ok, sandbox_message = config_starts(CONFIG_FILE, binary=candidate)
        if not sandbox_ok:
            return done(
                "failed",
                "新内核无法用当前配置启动（沙盒校验失败），已放弃升级，现有内核未改动：\n" + sandbox_message,
                ["新内核无法用当前配置启动"],
            )

        if os.path.exists(MOSDNS_BIN):
            shutil.copy2(MOSDNS_BIN, backup_bin)

        stop_ok, stop_message = run_cmd(["systemctl", "stop", "mosdns"], timeout=30)
        if not stop_ok:
            return done("failed", "停止 mosdns 失败，未替换内核：\n" + stop_message, ["停止 mosdns 失败"])
        try:
            replace_binary(candidate, MOSDNS_BIN)
            ok, restart_message = restart_mosdns()
        except Exception as exc:
            ok, restart_message = False, str(exc)

        if ok:
            ok, restart_message = core_health_check(tag)
        if ok:
            cleanup_old_backups()
            return done(
                "updated",
                f"mosdns 内核升级完成。\n来源：{source}\n旧版本：{clean_version(old_version)}\n新版本：{clean_version(new_version)}\n"
                f"{restart_message}\n旧内核备份：{backup_bin}",
                ["健康检查通过：服务、国内解析、国外解析"],
            )

        # 回滚本身也可能失败（磁盘满、权限等），要把原因说清楚而不是抛 500
        problem = health_failure_summary(restart_message)
        if not os.path.exists(backup_bin):
            return done(
                "failed",
                "新内核未通过启动/健康检查，且没有旧内核备份可回滚，请手动处理：\n" + restart_message,
                [problem, "没有旧内核备份，请立即检查"],
                untouched=False,
            )
        try:
            replace_binary(backup_bin, MOSDNS_BIN)
        except Exception as exc:
            return done(
                "failed",
                f"新内核未通过启动/健康检查，回滚旧内核也失败（{exc}），请手动把 {backup_bin} 复制回 {MOSDNS_BIN}：\n{restart_message}",
                [problem, "恢复旧版本失败，请立即检查"],
                untouched=False,
            )
        rollback_ok, rollback_message = restart_mosdns()
        text = "新内核未通过启动/健康检查，已回滚旧内核：\n" + restart_message
        summary = [problem, "已恢复旧版本"]
        if not rollback_ok:
            text += "\n\n回滚后重启仍失败，请手动检查（必要时启用救援模式）：\n" + rollback_message
            summary = [problem, "已恢复旧版本，但服务仍未启动，请立即检查"]
        else:
            recheck_ok, recheck_message = core_health_check(old_version if old_ok else None)
            text += "\n\n回滚后复查：" + recheck_message
            if not recheck_ok:
                text += "\n请尽快检查，必要时启用救援模式。"
                summary = [problem, "已恢复旧版本，但复查未通过，请尽快检查"]
        return done("rolled_back", text, summary)


def upgrade_mosdns_core():
    # 面板"升级"按钮：升到官方 latest，同样走沙盒校验、健康检查和自动回滚
    outcome = install_mosdns_core()
    return outcome["result"] == "updated", outcome["message"]


def backup_file(path, prefix):
    # 返回备份文件路径，不存在原文件时返回 None（调用方据此决定如何回滚）
    if not os.path.exists(path):
        return None
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d%H%M%S")
    backup_path = f"{BACKUP_DIR}/{prefix}.{stamp}.bak"
    shutil.copy2(path, backup_path)
    cleanup_old_backups()
    return backup_path


def rule_backup_prefix(rule_id):
    return f"{RULE_BACKUP_PREFIX}{rule_id}"


def list_backup_files(paths):
    items = []
    seen = set()
    for path in paths:
        real_path = os.path.realpath(path)
        if real_path in seen or not os.path.isfile(real_path):
            continue
        seen.add(real_path)
        stat = os.stat(real_path)
        items.append(
            {
                "id": os.path.basename(real_path),
                "path": real_path,
                "size": stat.st_size,
                "mtime": int(stat.st_mtime),
            }
        )
    items.sort(key=lambda item: item["mtime"], reverse=True)
    return items


def backup_candidates():
    # 只列配置备份：backup/ 下以 config. 开头的文件，加上旧版放在 /etc/mosdns 根目录的几种
    patterns = [
        f"{BACKUP_DIR}/{CONFIG_BACKUP_PREFIX}*",
        f"{MOSDNS_DIR}/config.yaml.bak",
        f"{MOSDNS_DIR}/config.yaml.bak.*",
        f"{MOSDNS_DIR}/config.yaml.bad-sync.*",
    ]
    paths = []
    for pattern in patterns:
        paths.extend(glob.glob(pattern))
    return [
        item
        for item in list_backup_files(paths)
        if item["id"].startswith(CONFIG_BACKUP_PREFIX)
        and not item["id"].endswith((".webtmp", ".defaultcheck"))
    ]


def kernel_backup_candidates():
    return list_backup_files(glob.glob(f"{BACKUP_DIR}/{KERNEL_BACKUP_PREFIX}*"))


def rule_backup_candidates():
    # 按规则 id 分组，返回 {prefix: [items...]}
    groups = {}
    for item in list_backup_files(glob.glob(f"{BACKUP_DIR}/{RULE_BACKUP_PREFIX}*")):
        prefix = item["id"].rsplit(".", 2)[0]
        groups.setdefault(prefix, []).append(item)
    return groups


def resolve_backup(backup_id):
    if not re.fullmatch(r"[A-Za-z0-9._-]+", str(backup_id or "")):
        return None
    for item in backup_candidates():
        if item["id"] == backup_id:
            return item["path"]
    return None


def restore_backup(backup_id):
    source = resolve_backup(backup_id)
    if not source:
        return False, "未找到这个备份文件"
    ok, message = config_starts(source)
    if not ok:
        return False, "这个备份无法启动 mosdns，未恢复：\n" + message

    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d%H%M%S")
    current_backup = f"{BACKUP_DIR}/{CONFIG_BACKUP_PREFIX}before-restore.{stamp}.yaml"
    if os.path.exists(CONFIG_FILE):
        shutil.copy2(CONFIG_FILE, current_backup)
        cleanup_old_backups()

    shutil.copy2(source, CONFIG_FILE)
    ok, message = restart_mosdns()
    if ok:
        return True, f"已恢复备份 {os.path.basename(source)}，mosdns 已重启"

    if os.path.exists(current_backup):
        shutil.copy2(current_backup, CONFIG_FILE)
        restart_mosdns()
    return False, "恢复的备份导致 mosdns 启动失败，已回滚到恢复前配置：\n" + message


def parse_peers(value):
    if isinstance(value, list):
        parts = value
    else:
        parts = re.split(r"[\n,|]+", str(value or ""))
    peers = []
    for item in parts:
        peer = str(item or "").strip().rstrip("/")
        if not peer:
            continue
        if not peer.startswith(("http://", "https://")):
            peer = "http://" + peer
        peers.append(peer)
    return list(dict.fromkeys(peers))


def read_sync_settings():
    env = read_env()
    peers = parse_peers(env.get("RULE_SYNC_PEERS", ""))
    return {
        "enabled": is_true(env.get("RULE_SYNC_ENABLED")),
        "token": env.get("RULE_SYNC_TOKEN", ""),
        "peers": peers,
        "peers_text": "\n".join(peers),
        "syncable_rules": sorted(SYNCABLE_RULE_IDS),
    }


def write_sync_settings(data):
    peers = parse_peers(data.get("peers_text") or data.get("peers") or "")
    token = str(data.get("token") or "").strip()
    if not token:
        token = secrets.token_urlsafe(24)
    if not is_safe_text(token, 200) or env_value_error(token):
        return False, "同步密钥不合法：" + (env_value_error(token) or "过长")
    for peer in peers:
        if env_value_error(peer):
            return False, f"同步节点地址不合法：{peer}"
    write_env(
        {
            "RULE_SYNC_ENABLED": str(is_true(data.get("enabled"))).lower(),
            "RULE_SYNC_TOKEN": token,
            "RULE_SYNC_PEERS": "|".join(peers),
        }
    )
    return True, "规则同步设置已保存"


def read_account_settings():
    env = read_env()
    return {
        "username": env.get("WEB_USER", "admin"),
    }


def write_account_settings(data):
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")
    confirm = str(data.get("confirm") or "")
    if not username:
        return False, "用户名不能为空"
    if not is_safe_text(username, 64) or any(char.isspace() for char in username) or env_value_error(username):
        return False, "用户名不能包含空格、引号或特殊符号，最多 64 个字符"
    updates = {"WEB_USER": username}
    if password or confirm:
        if password != confirm:
            return False, "两次输入的新密码不一致"
        if len(password) < 6 or not is_safe_text(password, 200) or env_value_error(password):
            return False, "新密码至少 6 位，且" + (env_value_error(password) or "不能过长")
        updates["WEB_SECRET"] = password
        # 改密码时轮换会话密钥，其他浏览器上已登录的会话全部失效；
        # 当前请求的会话会在响应时用新密钥重新签名，所以本窗口不用重新登录
        updates["WEB_SESSION_SECRET"] = secrets.token_urlsafe(48)
    write_env(updates)
    if "WEB_SESSION_SECRET" in updates:
        app.secret_key = updates["WEB_SESSION_SECRET"]
        return True, "面板登录信息已保存，其他设备上的登录状态已失效"
    return True, "面板登录信息已保存"


def test_sync_peers(data):
    peers = parse_peers(data.get("peers_text") or data.get("peers") or "")
    token = str(data.get("token") or "").strip()
    if not peers:
        return False, "请先填写其他 mosdns 面板地址", []
    if not token:
        return False, "请先填写同步密钥", []

    payload = json.dumps({"token": token, "rules": {}, "source": request.host_url.rstrip("/")}).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-Mosdns-Sync-Token": token,
    }
    results = []
    own_port = read_env().get("WEB_PORT", "7840")
    for peer in peers:
        if is_self_peer(peer, own_port):
            results.append({"peer": peer, "success": True, "message": "本机（跳过）"})
            continue
        url = peer.rstrip("/") + "/api/rule-sync"
        try:
            req = urlrequest.Request(url, data=payload, headers=headers, method="POST")
            with urlrequest.urlopen(req, timeout=8) as resp:
                body_text = resp.read().decode("utf-8", "replace")
            try:
                body = json.loads(body_text)
            except json.JSONDecodeError:
                body = {}
            message = body.get("message") or ""
            if message == "没有可同步的规则":
                message = "接口可访问，密钥已通过"
            results.append(
                {
                    "peer": peer,
                    "success": True,
                    "message": message or "接口可访问，密钥已通过",
                }
            )
        except error.HTTPError as exc:
            message = "同步密钥错误" if exc.code == 403 else f"HTTP {exc.code}"
            results.append({"peer": peer, "success": False, "message": message})
        except Exception as exc:
            results.append({"peer": peer, "success": False, "message": str(exc)})
    ok = all(item["success"] for item in results)
    message = "所有节点连通性正常" if ok else "部分节点连通性异常"
    return ok, message, results


def read_crontab_state(strict=False):
    # 返回 (行列表, crontab 命令是否存在)；没装 cron 的系统上 crontab 二进制不存在。
    # strict=True 给无人值守的写入用：除了"还没有 crontab"以外的读取失败都抛错，
    # 免得把读失败当成空表、写回时把 Geo 计划等其他行冲掉
    try:
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=10)
    except FileNotFoundError:
        return [], False
    if result.returncode != 0:
        detail = clean_output((result.stdout or "") + (result.stderr or ""))
        if strict and "no crontab" not in detail.lower():
            raise RuntimeError("读取 crontab 失败：" + (detail or f"退出码 {result.returncode}"))
        return [], True
    return result.stdout.splitlines(), True


def read_crontab_lines():
    return read_crontab_state()[0]


def cron_service_active():
    # Debian 叫 cron，RHEL 系叫 crond；任一在跑即可
    return any(run_cmd(["systemctl", "is-active", "--quiet", name], timeout=10)[0] for name in ("cron", "crond"))


GEO_LOG_HEADER_RE = re.compile(r"^===== (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) 更新 Geo 规则 =====")
GEO_LOG_RESULT_RE = re.compile(r"^===== 结果: (成功|失败) =====")


def read_tail_text(path, max_bytes=64 * 1024):
    try:
        with open(path, "rb") as file:
            file.seek(0, os.SEEK_END)
            size = file.tell()
            file.seek(max(0, size - max_bytes))
            return file.read().decode("utf-8", "replace")
    except OSError:
        return ""


def parse_geo_update_log(text, summary_lines=8):
    """取日志里最后一个「===== <时间> 更新 Geo 规则 =====」块，返回 {at, ok, summary}；没有块返回 None。

    ok 由块里的「===== 结果: 成功/失败 =====」决定；老版本 CLI 没有这行时为 None。
    """
    lines = clean_output(text).splitlines()
    start = None
    for index in range(len(lines) - 1, -1, -1):
        if GEO_LOG_HEADER_RE.match(lines[index]):
            start = index
            break
    if start is None:
        return None
    header = GEO_LOG_HEADER_RE.match(lines[start])
    try:
        at = int(time.mktime(time.strptime(header.group(1), "%Y-%m-%d %H:%M:%S")))
    except (ValueError, OverflowError):
        at = 0
    block = lines[start + 1 :]
    ok = None
    for line in block:
        result = GEO_LOG_RESULT_RE.match(line)
        if result:
            ok = result.group(1) == "成功"
    body = [line for line in block if line.strip() and not GEO_LOG_RESULT_RE.match(line)]
    return {"at": at, "ok": ok, "summary": body[-summary_lines:]}


def geo_rule_files():
    items = []
    for name in ("geosite_cn.txt", "geosite_no_cn.txt"):
        path = f"{MOSDNS_DIR}/rules/{name}"
        try:
            info = os.stat(path)
            items.append({"name": name, "mtime": int(info.st_mtime), "size": info.st_size})
        except OSError:
            items.append({"name": name, "mtime": 0, "size": 0})
    return items


def geo_update_status():
    return {
        "last_run": parse_geo_update_log(read_tail_text(UPDATE_LOG)),
        "files": geo_rule_files(),
        "cron_available": read_crontab_state()[1],
        "cron_service_active": cron_service_active(),
    }


def is_geo_update_cron(line):
    stripped = str(line or "").strip()
    if is_auto_update_cron(stripped):
        # 自动更新那一行归 write_auto_update_cron 管，Geo 计划的增删不能碰它
        return False
    return bool(stripped and not stripped.startswith("#") and MOSCTL in stripped and re.search(r"\bupdate\b", stripped))


def is_auto_update_cron(line):
    stripped = str(line or "").strip()
    return bool(stripped) and (AUTO_UPDATE_CRON_MARKER in stripped or "auto_update.py" in stripped)


def parse_time_fields(minute, hour):
    if not re.fullmatch(r"\d{1,2}", minute or ""):
        return None
    minute_int = int(minute)
    if minute_int < 0 or minute_int > 59:
        return None
    if re.fullmatch(r"\d{1,2}", hour or ""):
        hour_int = int(hour)
        if 0 <= hour_int <= 23:
            return f"{hour_int:02d}:{minute_int:02d}"
    if re.fullmatch(r"\d{1,2}(,\d{1,2})+", hour or ""):
        hours = [int(item) for item in hour.split(",")]
        if all(0 <= item <= 23 for item in hours):
            return f"{min(hours):02d}:{minute_int:02d}"
    return None


def describe_geo_schedule(mode, time_value, weekday):
    if mode == "disabled":
        return "已关闭自动更新"
    if mode == "every_6h":
        return f"每 6 小时更新一次，从 {time_value} 所在小时开始"
    if mode == "every_12h":
        return f"每 12 小时更新一次，从 {time_value} 所在小时开始"
    if mode == "weekly":
        weekday_names = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"]
        return f"每{weekday_names[int(weekday)]} {time_value} 更新"
    return f"每天 {time_value} 更新"


def parse_geo_schedule_line(line):
    parts = str(line or "").split()
    if len(parts) < 6:
        return None
    minute, hour, day, month, weekday = parts[:5]
    if day != "*" or month != "*":
        return None
    time_value = parse_time_fields(minute, hour)
    if not time_value:
        return None
    if weekday != "*":
        if re.fullmatch(r"[0-6]", weekday):
            return {"mode": "weekly", "time": time_value, "weekday": weekday}
        return None
    if re.fullmatch(r"\d{1,2}(,\d{1,2})+", hour):
        hours = sorted(int(item) for item in hour.split(","))
        deltas = sorted({(hours[(idx + 1) % len(hours)] - hours[idx]) % 24 for idx in range(len(hours))})
        if deltas == [6]:
            return {"mode": "every_6h", "time": time_value, "weekday": "1"}
        if deltas == [12]:
            return {"mode": "every_12h", "time": time_value, "weekday": "1"}
    if re.fullmatch(r"\d{1,2}", hour):
        return {"mode": "daily", "time": time_value, "weekday": "1"}
    return None


def read_geo_schedule():
    lines = read_crontab_lines()
    cron_line = next((line for line in lines if is_geo_update_cron(line)), "")
    parsed = parse_geo_schedule_line(cron_line)
    if parsed:
        parsed["enabled"] = parsed["mode"] != "disabled"
        parsed["cron"] = cron_line
        parsed["summary"] = describe_geo_schedule(parsed["mode"], parsed["time"], parsed["weekday"])
        return parsed
    if cron_line:
        return {
            "enabled": True,
            "mode": "custom",
            "time": "02:00",
            "weekday": "1",
            "cron": cron_line,
            "summary": "检测到自定义 crontab：" + cron_line,
        }
    return {
        "enabled": False,
        "mode": "disabled",
        "time": "02:00",
        "weekday": "1",
        "cron": "",
        "summary": "已关闭自动更新",
    }


def normalize_schedule_time(value):
    if not re.fullmatch(r"\d{2}:\d{2}", str(value or "")):
        return None
    hour, minute = [int(item) for item in value.split(":", 1)]
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour, minute
    return None


def build_geo_cron_line(data):
    mode = str(data.get("mode") or "daily")
    if mode == "disabled":
        return None, "disabled", "02:00", "1"
    parsed_time = normalize_schedule_time(data.get("time") or "02:00")
    if not parsed_time:
        raise ValueError("更新时间格式不合法")
    hour, minute = parsed_time
    weekday = str(data.get("weekday") or "1")
    if not re.fullmatch(r"[0-6]", weekday):
        raise ValueError("星期选择不合法")
    if mode == "daily":
        hour_field = str(hour)
        weekday_field = "*"
    elif mode == "weekly":
        hour_field = str(hour)
        weekday_field = weekday
    elif mode in ("every_6h", "every_12h"):
        interval = 6 if mode == "every_6h" else 12
        hour_field = ",".join(str(item) for item in sorted({(hour + offset) % 24 for offset in range(0, 24, interval)}))
        weekday_field = "*"
    else:
        raise ValueError("更新频率不合法")
    cron_line = f"{minute} {hour_field} * * {weekday_field} {GEO_UPDATE_COMMAND} >> {UPDATE_LOG} 2>&1"
    return cron_line, mode, f"{hour:02d}:{minute:02d}", weekday


def write_geo_schedule(data):
    try:
        cron_line, mode, time_value, weekday = build_geo_cron_line(data)
    except ValueError as exc:
        return False, str(exc)
    lines = [
        line
        for line in read_crontab_lines()
        if not is_geo_update_cron(line) and line.strip() != GEO_CRON_COMMENT
    ]
    if cron_line:
        lines.extend([GEO_CRON_COMMENT, cron_line])
    ok, message = write_crontab_lines(lines)
    if not ok:
        return False, message
    return True, describe_geo_schedule(mode, time_value, weekday)


CRONTAB_MISSING_MESSAGE = "系统没有 crontab 命令，请先安装 cron（Debian: apt install cron；RHEL: dnf install cronie）"


def write_crontab_lines(lines):
    crontab_text = "\n".join(lines).strip()
    if crontab_text:
        crontab_text += "\n"
    try:
        result = subprocess.run(["crontab", "-"], input=crontab_text, capture_output=True, text=True, timeout=10)
    except FileNotFoundError:
        return False, CRONTAB_MISSING_MESSAGE
    if result.returncode != 0:
        return False, clean_output(result.stdout + result.stderr) or "写入 crontab 失败"
    return True, ""


# ---------- 自动更新（设置 / cron / 状态 / 锁） ----------


def parse_age_days(value):
    if isinstance(value, bool):
        return None
    text = str(value).strip() if value is not None else ""
    if not re.fullmatch(r"\d{1,3}", text):
        return None
    days = int(text)
    return days if 0 <= days <= AUTO_UPDATE_MAX_AGE_DAYS else None


def read_auto_update_settings(env=None):
    # .env 里没有或写坏了就用默认值：默认开启、04:10、内核满 3 天、面板不等待
    env = read_env() if env is None else env

    def value(key):
        raw = str(env.get(key, "")).strip()
        return raw or AUTO_UPDATE_DEFAULTS[key]

    enabled_raw = value("AUTO_UPDATE_ENABLED").lower()
    time_value = value("AUTO_UPDATE_TIME")
    if not normalize_schedule_time(time_value):
        time_value = AUTO_UPDATE_DEFAULTS["AUTO_UPDATE_TIME"]
    core_age = parse_age_days(value("AUTO_UPDATE_CORE_MIN_AGE_DAYS"))
    panel_age = parse_age_days(value("AUTO_UPDATE_PANEL_MIN_AGE_DAYS"))
    return {
        "enabled": enabled_raw not in ("false", "0", "no", "off"),
        "time": time_value,
        "core_min_age_days": int(AUTO_UPDATE_DEFAULTS["AUTO_UPDATE_CORE_MIN_AGE_DAYS"]) if core_age is None else core_age,
        "panel_min_age_days": int(AUTO_UPDATE_DEFAULTS["AUTO_UPDATE_PANEL_MIN_AGE_DAYS"]) if panel_age is None else panel_age,
    }


def validate_auto_update_settings(data):
    """校验页面提交的设置，返回 (要写入 .env 的键值, 错误信息)。"""
    if not isinstance(data, dict):
        return None, "请求格式不正确"
    enabled = data.get("enabled")
    if isinstance(enabled, str) and enabled.lower() in ("true", "false"):
        enabled = enabled.lower() == "true"
    if not isinstance(enabled, bool):
        return None, "启用开关必须是 true 或 false"
    time_value = str(data.get("time") or "").strip()
    if not normalize_schedule_time(time_value):
        return None, "更新时间格式不合法，应为 HH:MM（00:00–23:59）"
    core_age = parse_age_days(data.get("core_min_age_days"))
    if core_age is None:
        return None, f"内核最少发布天数必须是 0–{AUTO_UPDATE_MAX_AGE_DAYS} 的整数"
    panel_age = parse_age_days(data.get("panel_min_age_days"))
    if panel_age is None:
        return None, f"面板最少提交天数必须是 0–{AUTO_UPDATE_MAX_AGE_DAYS} 的整数"
    return {
        "AUTO_UPDATE_ENABLED": "true" if enabled else "false",
        "AUTO_UPDATE_TIME": time_value,
        "AUTO_UPDATE_CORE_MIN_AGE_DAYS": str(core_age),
        "AUTO_UPDATE_PANEL_MIN_AGE_DAYS": str(panel_age),
    }, None


def build_auto_update_cron_line(settings):
    if not settings.get("enabled"):
        return None
    hour, minute = normalize_schedule_time(settings.get("time")) or (4, 10)
    return f"{minute} {hour} * * * python3 {AUTO_UPDATE_SCRIPT} >> {AUTO_UPDATE_LOG} 2>&1 {AUTO_UPDATE_CRON_MARKER}"


def write_auto_update_cron(settings=None):
    """按设置增删 `# MOSCTL_AUTO_UPDATE` 那一行；其他行（Geo 计划等）原样保留，没变化就不写。"""
    settings = settings or read_auto_update_settings()
    try:
        lines, available = read_crontab_state(strict=True)
    except Exception as exc:
        return False, str(exc)
    if not available:
        return False, CRONTAB_MISSING_MESSAGE
    desired = build_auto_update_cron_line(settings)
    kept = [line for line in lines if not is_auto_update_cron(line)]
    updated = kept + ([desired] if desired else [])
    if updated == lines:
        return True, "crontab 无需改动"
    ok, message = write_crontab_lines(updated)
    if not ok:
        return False, message
    return True, ("已写入 crontab：每天 " + settings["time"] + " 自动更新") if desired else "已从 crontab 移除自动更新"


def save_auto_update_settings(data):
    updates, error_message = validate_auto_update_settings(data)
    if error_message:
        return False, error_message
    write_env(updates)
    ok, message = write_auto_update_cron(read_auto_update_settings())
    if not ok:
        return False, "设置已保存，但更新 crontab 失败：" + message
    return True, "自动更新设置已保存。" + message


def read_auto_update_state():
    try:
        with open(AUTO_UPDATE_STATE_FILE, "r", encoding="utf-8") as file:
            state = json.load(file)
    except (OSError, ValueError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    for item in ("core", "panel"):
        if not isinstance(state.get(item), dict):
            state[item] = {}
    return state


def write_auto_update_state(state):
    os.makedirs(os.path.dirname(AUTO_UPDATE_STATE_FILE), exist_ok=True)
    tmp_file = f"{AUTO_UPDATE_STATE_FILE}.tmp"
    with open(tmp_file, "w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)
    os.replace(tmp_file, AUTO_UPDATE_STATE_FILE)


def update_auto_update_item(item, **fields):
    state = read_auto_update_state()
    state[item].update(fields)
    write_auto_update_state(state)
    return state


def acquire_auto_update_lock():
    """单实例锁（fcntl）：拿到返回 fd，已有实例在跑返回 None。进程退出时内核自动释放。"""
    os.makedirs(os.path.dirname(AUTO_UPDATE_LOCK_FILE), exist_ok=True)
    fd = os.open(AUTO_UPDATE_LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def release_auto_update_lock(fd):
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def auto_update_running():
    try:
        fd = acquire_auto_update_lock()
    except OSError:
        return False
    if fd is None:
        return True
    release_auto_update_lock(fd)
    return False


def finish_panel_auto_update():
    """面板启动时确认自动更新是否装上了：状态里是"started → vX"，现在跑的版本 ≥ vX 就记为 updated。"""
    state = read_auto_update_state()
    panel = state["panel"]
    if panel.get("last_result") != "started":
        return False
    target = panel_version_tuple(panel.get("to"))
    current = panel_version_tuple(PANEL_VERSION)
    now = int(time.time())
    previous = panel.get("from") or "旧版本"
    if target and current and current >= target:
        update_auto_update_item(
            "panel",
            last_result="updated",
            last_result_at=now,
            current=PANEL_VERSION,
            message=f"面板已重启，当前运行 v{PANEL_VERSION}",
        )
        notify_event("success", "管理面板已更新", [f"{previous} → v{PANEL_VERSION}", "面板已重启并运行新版本"], background=True)
        return True
    if auto_update_running():
        # 还在下载/安装中，面板因为别的原因重启了：交给自动更新进程自己收尾
        return False
    update_auto_update_item(
        "panel",
        last_result="failed",
        last_result_at=now,
        current=PANEL_VERSION,
        message=f"面板重启后仍是 v{PANEL_VERSION}，预期 {panel.get('to') or '新版本'}",
    )
    notify_event(
        "failure",
        "管理面板更新失败",
        [f"{previous} → {panel.get('to') or '新版本'}", f"重启后仍是 v{PANEL_VERSION}", "继续运行当前版本"],
        background=True,
    )
    return True


def panel_startup_tasks():
    # 只在 mosdns-web 真正启动时调用（__main__），import（auto_update.py、测试）时不碰 crontab
    for task in (finish_panel_auto_update, write_auto_update_cron):
        try:
            task()
        except Exception as exc:
            print(f"启动任务 {task.__name__} 失败：{exc}", file=sys.stderr)


def auto_update_overview():
    settings = read_auto_update_settings()
    lines, cron_available = read_crontab_state()
    cron_line = next((line for line in lines if is_auto_update_cron(line) and not line.strip().startswith("#")), "")
    return {
        "settings": settings,
        "state": read_auto_update_state(),
        "running": auto_update_running(),
        "cron_line": cron_line,
        "cron_available": cron_available,
        "cron_service_active": cron_service_active(),
        "core_current": clean_version(get_version()),
        "panel_current": PANEL_VERSION,
        "log_tail": tail_lines(read_tail_text(AUTO_UPDATE_LOG, 16 * 1024), 30),
        **server_time_info(),
    }


def run_auto_update_dry_run():
    if auto_update_running():
        return False, "自动更新正在运行，请稍后再检查"
    if not os.path.exists(AUTO_UPDATE_SCRIPT):
        return False, f"缺少 {AUTO_UPDATE_SCRIPT}，请先升级面板"
    try:
        result = subprocess.run(
            [sys.executable or "python3", AUTO_UPDATE_SCRIPT, "--dry-run"],
            capture_output=True,
            text=True,
            timeout=AUTO_UPDATE_DRY_RUN_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return False, f"检查超过 {AUTO_UPDATE_DRY_RUN_TIMEOUT} 秒未完成，已中止"
    except Exception as exc:
        return False, str(exc)
    output = clean_output((result.stdout or "") + (result.stderr or "")).strip()
    return result.returncode in (0, 1), output or "检查完成，没有输出"


def start_auto_update_detached():
    """"立即更新"：在后台单独起一个进程跑 auto_update.py，页面轮询状态。

    优先用 systemd-run 放进独立的临时 unit：面板升级会重启 mosdns-web，
    留在 mosdns-web 的 cgroup 里会被一起杀掉。KillMode=process：主进程退出后不杀它留下的
    "sleep 1; systemctl restart mosdns-web"，否则面板文件换了 Web 却没重启。
    """
    if auto_update_running():
        return False, "自动更新正在运行"
    if not os.path.exists(AUTO_UPDATE_SCRIPT):
        return False, f"缺少 {AUTO_UPDATE_SCRIPT}，请先升级面板"
    command = [sys.executable or "python3", AUTO_UPDATE_SCRIPT, "--manual"]
    if shutil.which("systemd-run"):
        unit = f"mosctl-auto-update-{int(time.time())}"
        ok, message = run_cmd(["systemd-run", "--quiet", "--collect", "--unit", unit, "--property=KillMode=process", *command], timeout=20)
        if ok:
            return True, "已在后台开始更新，可在本卡片查看进度"
    try:
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
    except Exception as exc:
        return False, "启动后台更新失败：" + str(exc)
    return True, "已在后台开始更新，可在本卡片查看进度"


def backup_keep_count():
    env = read_env()
    value = env.get("BACKUP_KEEP_COUNT", str(DEFAULT_BACKUP_KEEP_COUNT))
    if not re.fullmatch(r"\d{1,3}", str(value or "")):
        return DEFAULT_BACKUP_KEEP_COUNT
    return max(3, min(200, int(value)))


def read_backup_settings():
    items = backup_candidates()
    return {
        "keep_count": backup_keep_count(),
        "count": len(items),
        "total_size": sum(item["size"] for item in items),
    }


def write_backup_settings(data):
    value = str(data.get("keep_count") or "").strip()
    if not re.fullmatch(r"\d{1,3}", value):
        return False, "保留数量必须是数字"
    keep_count = int(value)
    if keep_count < 3 or keep_count > 200:
        return False, "保留数量必须在 3 到 200 之间"
    write_env({"BACKUP_KEEP_COUNT": str(keep_count)})
    return True, "备份保留策略已保存"


LEGACY_RULE_BACKUP_RE = re.compile(r"^(?P<base>[^/]+\.txt)\.(?P<stamp>[0-9]+)\.bak$")


def migrate_legacy_rule_backups():
    # 旧版规则备份叫 <file>.txt.<stamp>.bak（例如 force-cn.txt.20261008090000.bak），
    # 新版只列出并清理 rule-<id>.<stamp>.bak；这里一次性改名，让旧备份也能被看到和按保留数清理。
    # 只处理 RULE_FILES 里已知的文件名，目标已存在则保留原文件不动。
    renamed = []
    if not os.path.isdir(BACKUP_DIR):
        return renamed
    rule_id_by_basename = {os.path.basename(meta["path"]): rule_id for rule_id, meta in RULE_FILES.items()}
    for name in sorted(os.listdir(BACKUP_DIR)):
        match = LEGACY_RULE_BACKUP_RE.match(name)
        if not match:
            continue
        rule_id = rule_id_by_basename.get(match.group("base"))
        if not rule_id:
            continue
        source = os.path.join(BACKUP_DIR, name)
        target_name = f"{rule_backup_prefix(rule_id)}.{match.group('stamp')}.bak"
        target = os.path.join(BACKUP_DIR, target_name)
        if not os.path.isfile(source) or os.path.exists(target):
            continue
        try:
            os.replace(source, target)
            renamed.append((name, target_name))
        except OSError:
            pass
    return renamed


def cleanup_old_backups(keep_count=None):
    # 配置备份按 keep_count 保留；内核备份固定保留 KERNEL_BACKUP_KEEP_COUNT 个；
    # 规则备份每个规则各自保留 keep_count 个，三者互不占用名额
    migrate_legacy_rule_backups()
    keep_count = backup_keep_count() if keep_count is None else int(keep_count)
    stale_items = backup_candidates()[keep_count:] + kernel_backup_candidates()[KERNEL_BACKUP_KEEP_COUNT:]
    for items in rule_backup_candidates().values():
        stale_items.extend(items[keep_count:])
    deleted = []
    for item in stale_items:
        try:
            os.remove(item["path"])
            deleted.append(item["id"])
        except OSError:
            pass
    return {
        "deleted": deleted,
        "deleted_count": len(deleted),
        "remaining_count": len(backup_candidates()),
        "keep_count": keep_count,
    }


# 启动时顺手把旧命名的规则备份改成新命名；失败不影响面板启动
try:
    migrate_legacy_rule_backups()
except Exception:
    pass


def is_ip_literal(value):
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def rule_lines(content):
    # 去掉行内 # 注释和空行，返回 (行号, 内容)
    for number, line in enumerate(str(content or "").splitlines(), 1):
        body = line.split("#", 1)[0].strip()
        if body:
            yield number, body


def hosts_rule_error(content):
    # mosdns hosts 插件：每行「匹配模式 IP [IP...]」；最常见的错误是把 IP 写在前面
    for number, body in rule_lines(content):
        fields = body.split()
        if is_ip_literal(fields[0]):
            return f"格式是「域名 IP」，不是「IP 域名」（第 {number} 行）"
        if len(fields) < 2:
            return f"第 {number} 行缺少 IP：格式是「域名 IP」"
        for field in fields[1:]:
            if not is_ip_literal(field):
                return f"第 {number} 行的「{field}」不是合法 IP"
    return None


DOMAIN_RULE_PREFIXES = ("domain:", "full:", "keyword:", "regexp:")


def domain_rule_error(content):
    # domain_set 插件：每行一个匹配项，可带 domain:/full:/keyword:/regexp: 前缀，不能有空格
    for number, body in rule_lines(content):
        if len(body.split()) != 1:
            return f"第 {number} 行只能写一个域名，不能有空格（注释请用 # 开头）"
        value = body
        for prefix in DOMAIN_RULE_PREFIXES:
            if body.lower().startswith(prefix):
                value = body[len(prefix):]
                break
        if not value:
            return f"第 {number} 行前缀后面没有内容"
    return None


def rule_content_error(rule_id, content):
    if rule_id == "hosts":
        return hosts_rule_error(content)
    if rule_id in ("force-cn", "force-nocn"):
        return domain_rule_error(content)
    return None


RULES_UNCHANGED_MESSAGE = "规则内容没有变化，未重启"


def rule_entry_set(content):
    # 比较规则是否变化用：去掉注释/空行、去重、不计顺序；域名不分大小写，domain: 前缀等同于不写
    entries = set()
    for _number, body in rule_lines(content):
        if body.lower().startswith("domain:"):
            body = body[len("domain:"):]
        if not body.lower().startswith("regexp:"):
            body = body.lower()
        entries.add(body)
    return entries


def rule_content_unchanged(rule_id, content):
    """同步规则（强制国内/国外）的内容和当前文件等价时返回 True；文件不存在算有变化。"""
    meta = RULE_FILES.get(rule_id)
    if rule_id not in SYNCABLE_RULE_IDS or not meta or not is_safe_text(content):
        return False
    try:
        with open(meta["path"], "r", encoding="utf-8") as file:
            current = file.read()
    except OSError:
        return False
    return rule_entry_set(current) == rule_entry_set(content)


def elapsed_ms(started):
    return max(0, int(round((time.monotonic() - started) * 1000)))


def save_rule_content(rule_id, content):
    # 返回 (ok, message, (备份路径, 规则路径))，第三项给 restart_or_rollback 用。
    # 先查格式、再用沙箱 mosdns 校验，都通过才写文件；线上服务在这之前不会被碰
    meta = RULE_FILES.get(rule_id)
    if not meta:
        return False, "未知规则文件", None
    if not is_safe_text(content):
        return False, "规则内容不合法或过大", None
    format_error = rule_content_error(rule_id, content)
    if format_error:
        return False, format_error, None
    ok, message = rule_content_starts(rule_id, content)
    if not ok:
        return False, "规则校验失败，未保存：\n" + message, None

    path = meta["path"]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    backup = backup_file(path, rule_backup_prefix(rule_id))
    tmp_file = f"{path}.webtmp"
    with open(tmp_file, "w", encoding="utf-8") as file:
        file.write(content)
    os.replace(tmp_file, path)
    return True, "规则已保存", (backup, path)



def local_ipv4_addresses():
    """本机所有 IPv4 地址（含回环），用来识别同步节点里的“自己”。"""
    addrs = {"127.0.0.1", "localhost"}
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr"], capture_output=True, text=True, timeout=5).stdout
        addrs.update(re.findall(r"inet (\d+\.\d+\.\d+\.\d+)/", out))
    except Exception:
        pass
    return addrs

def is_self_peer(peer, own_port):
    """同步节点列表在各处是同一份，会包含本机。推送给自己会撞上本机正持有的锁（409），只会显示成失败。"""
    try:
        parts = urlsplit(peer)
        host, port = parts.hostname or "", parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        return False
    try:
        own_port = int(own_port)
    except (TypeError, ValueError):
        return False
    return port == own_port and host in local_ipv4_addresses()

# 规则同步在后台线程里推送：保存规则的请求不再等各节点返回。
# 节点里可能有本机上游的 mihomo，它重启时会断掉浏览器经由它的连接，同步等在请求里就会让页面误报“请求中断”。
SYNC_JOBS = []
SYNC_JOBS_LOCK = threading.Lock()
SYNC_JOBS_KEEP = 5
SYNC_PEER_TIMEOUT = 15
SYNC_BUSY_RETRIES = 3
SYNC_BUSY_DELAY = 3
SYNC_SELF_MESSAGE = "本机（跳过）"
# 对端忙时的提示：mosctl 返回 409“操作进行中，请稍后再试”，mihomo 返回 200 + “另一个操作正在进行中，请稍后再试。”
SYNC_BUSY_HINTS = ("稍后再试", "操作进行中", "正在进行中")


def sync_peer_busy(message):
    return any(hint in str(message or "") for hint in SYNC_BUSY_HINTS)


def push_rule_to_peer(peer, payload, headers):
    """推给一个节点，返回 (成功, 说明)。对端忙（409 或忙碌提示）时隔 SYNC_BUSY_DELAY 秒重试，最多 SYNC_BUSY_RETRIES 次。"""
    url = peer.rstrip("/") + "/api/rule-sync"
    attempt = 0
    while True:
        busy = False
        try:
            req = urlrequest.Request(url, data=payload, headers=headers, method="POST")
            with urlrequest.urlopen(req, timeout=SYNC_PEER_TIMEOUT) as resp:
                body = json.loads(resp.read().decode("utf-8", "replace"))
            if not isinstance(body, dict):
                body = {}
            if body.get("success"):
                return True, "成功"
            message = str(body.get("message") or "未知错误")
            busy = sync_peer_busy(message)
        except error.HTTPError as exc:
            message = str(exc)
            busy = exc.code == 409
            exc.close()
        except Exception as exc:
            message = str(exc)
        if not busy or attempt >= SYNC_BUSY_RETRIES:
            if busy and attempt:
                message += f"（已重试 {attempt} 次）"
            return False, message
        attempt += 1
        time.sleep(SYNC_BUSY_DELAY)


def sync_job_snapshot(job):
    with SYNC_JOBS_LOCK:
        return json.loads(json.dumps(job))


def find_sync_job(job_id):
    with SYNC_JOBS_LOCK:
        if job_id == "latest":
            job = SYNC_JOBS[-1] if SYNC_JOBS else None
        else:
            job = next((item for item in SYNC_JOBS if item["id"] == job_id), None)
    return sync_job_snapshot(job) if job else None


def run_broadcast_job(job, peers, payload, headers):
    for peer in peers:
        try:
            ok, message = push_rule_to_peer(peer, payload, headers)
        except Exception as exc:  # 线程里不能把异常抛丢，任务要能结束
            ok, message = False, str(exc)
        with SYNC_JOBS_LOCK:
            job["results"].append({"peer": peer, "success": ok, "message": message})
            job["done"] += 1
    with SYNC_JOBS_LOCK:
        job["finished_at"] = int(time.time())


def start_broadcast(rule_id, content):
    """在后台推送规则，返回 (任务 id 或 None, 给用户的说明)。

    依赖请求的东西（source、设置、本机端口、本机识别）都在这里取好再起线程；线程不持有操作锁。
    """
    if rule_id not in SYNCABLE_RULE_IDS:
        return None, ""
    settings = read_sync_settings()
    if not settings["enabled"]:
        return None, ""
    if not settings["peers"]:
        return None, "规则同步已启用，但没有配置其他节点。"
    if not settings["token"]:
        return None, "规则同步已启用，但缺少同步密钥。"

    payload = json.dumps(
        {
            "token": settings["token"],
            "rules": {rule_id: content},
            "source": request.host_url.rstrip("/"),
        }
    ).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-Mosdns-Sync-Token": settings["token"],
    }
    own_port = read_env().get("WEB_PORT", "7840")
    remote_peers = []
    results = []
    for peer in settings["peers"]:
        if is_self_peer(peer, own_port):
            results.append({"peer": peer, "success": True, "message": SYNC_SELF_MESSAGE})
        else:
            remote_peers.append(peer)
    job = {
        "id": secrets.token_hex(4),
        "rule_id": rule_id,
        "started_at": int(time.time()),
        "finished_at": None,
        "total": len(settings["peers"]),
        "done": len(results),
        "results": results,
    }
    with SYNC_JOBS_LOCK:
        SYNC_JOBS.append(job)
        del SYNC_JOBS[:-SYNC_JOBS_KEEP]
    threading.Thread(
        target=run_broadcast_job,
        args=(job, remote_peers, payload, headers),
        name="rule-sync-" + job["id"],
        daemon=True,
    ).start()
    return job["id"], f"正在后台同步到 {len(remote_peers)} 个节点…"


SYNC_PING_MESSAGE = "接口可访问，密钥已通过"


def apply_synced_rules(rules):
    if not isinstance(rules, dict):
        return False, "同步内容不合法"
    applied = []
    rollbacks = []
    unchanged = []
    for rule_id, content in rules.items():
        if rule_id not in SYNCABLE_RULE_IDS:
            continue
        if rule_content_unchanged(rule_id, content):
            unchanged.append(rule_id)
            continue
        ok, message, rollback = save_rule_content(rule_id, content)
        if not ok:
            # 之前已写入的规则先还原，不留下半套同步结果
            restore_rollbacks(rollbacks)
            return False, message
        rollbacks.append(rollback)
        applied.append(rule_id)
    if not applied:
        if unchanged:
            return True, RULES_UNCHANGED_MESSAGE
        return False, "没有可同步的规则"
    return restart_or_rollback(rollbacks, "已同步规则：" + ", ".join(applied), "规则已写入")


def notify_rule_sync_receive(ok, message, source=""):
    """收到其他面板推来的规则：应用失败发 ⚠️（去重，最多 3 天提醒一次），之后成功一次发“已恢复”。"""
    if ok:
        notify_recovery("rule_sync", "规则同步已恢复", ["其他面板推送的规则已正常应用"], background=True)
        return
    lines = []
    if source:
        lines.append(f"来源：{source}")
    lines += [notify_short_line(message) or "规则未通过校验", "本机规则保持不变"]
    notify_failure("rule_sync", "warning", "收到的同步规则未能应用", lines, background=True)


def update_config_values(local_dns, remote_dns, ttl, local_backup=None):
    # local_backup=None 表示不动备用国内 DNS；空串表示删掉备用行
    if not os.path.exists(CONFIG_FILE):
        return False, "配置文件不存在"
    if not re.fullmatch(r"\d{1,7}", str(ttl or "")):
        return False, "TTL 必须是数字"
    for label, value in (("国内 DNS", local_dns), ("国外 DNS", remote_dns), ("备用国内 DNS", local_backup or "")):
        if not is_safe_text(value, 200):
            return False, f"{label} 不合法"
    local_dns, local_error = normalize_upstream(local_dns, default_scheme="udp")
    if local_error:
        return False, f"国内 DNS {local_error}"
    if local_backup is not None and str(local_backup).strip():
        local_backup, backup_error = normalize_upstream(local_backup, default_scheme="udp")
        if backup_error:
            return False, f"备用国内 DNS {backup_error}"
    remote_dns, remote_error = normalize_upstream(remote_dns, default_port=53)
    if remote_error:
        return False, f"国外 DNS {remote_error}"

    text = read_config_text()
    new_text, ttl_count = re.subn(
        r"(?m)^(\s*lazy_cache_ttl:\s*)\d+\s*$",
        lambda match: match.group(1) + str(ttl),
        text,
        count=1,
    )
    new_text, local_count = replace_tag_addr(new_text, "TAG_LOCAL", local_dns)
    new_text, remote_count = replace_tag_addr(new_text, "TAG_REMOTE", remote_dns)
    if ttl_count != 1 or local_count != 1 or remote_count != 1:
        return False, "没有找到 TAG_LOCAL、TAG_REMOTE 或 lazy_cache_ttl"
    if local_backup is not None:
        new_text, backup_error = set_local_backup(new_text, local_backup)
        if backup_error:
            return False, f"备用国内 DNS 写入失败：{backup_error}"

    if new_text == text:
        return True, "配置无变化"

    ok, message = config_text_starts(new_text)
    if not ok:
        return False, "配置校验失败，未保存：\n" + message
    backup = backup_file(CONFIG_FILE, "config")
    write_config_text(new_text)
    result = restart_or_rollback([(backup, CONFIG_FILE)], "配置已保存并重启 mosdns", "配置已保存")
    return notify_if_rolled_back("DNS 参数修改已回滚", result)


def config_api_address():
    # 取 config.yaml 里 api.http 的监听地址，转成本机可访问的 host:port；没有 api 段返回空
    match = re.search(r'(?m)^\s*http:\s*["\']?([^"\'#\n]+?)["\']?\s*$', read_config_text())
    if not match:
        return ""
    listen = match.group(1).strip()
    if listen.startswith(":"):
        return "127.0.0.1" + listen
    for wildcard in ("0.0.0.0:", "[::]:"):
        if listen.startswith(wildcard):
            return "127.0.0.1:" + listen[len(wildcard):]
    return listen


def config_cache_tag():
    match = re.search(r'(?m)^\s*-\s*tag:\s*["\']?([A-Za-z0-9_.-]+)["\']?\s*\n\s*type:\s*["\']?cache["\']?\s*$', read_config_text())
    return match.group(1) if match else ""


def config_dump_file():
    match = re.search(r'(?m)^\s*dump_file:\s*["\']?([^"\'#\n]+?)["\']?\s*$', read_config_text())
    return match.group(1).strip() if match else f"{MOSDNS_DIR}/cache.dump"


def flush_cache_via_api(timeout=5):
    # mosdns 的 cache 插件提供 GET /plugins/<tag>/flush，清缓存不用重启
    address = config_api_address()
    tag = config_cache_tag()
    if not address or not tag:
        return False, "配置里没有 api.http 监听或 cache 插件"
    url = f"http://{address}/plugins/{tag}/flush"
    try:
        req = urlrequest.Request(url, headers={"User-Agent": "mosdns-web-manager"})
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            if 200 <= resp.status < 300:
                return True, f"已通过 API 清空缓存（{url}），无需重启"
            return False, f"API 返回 HTTP {resp.status}"
    except Exception as exc:
        return False, str(exc)


def flush_cache():
    ok, message = flush_cache_via_api()
    if ok:
        return True, message
    # API 不可用（服务没起来、配置里没有 api 段等）时退回老办法：删 dump 文件再重启
    try:
        os.remove(config_dump_file())
    except FileNotFoundError:
        pass
    except OSError as exc:
        return False, f"删除缓存文件失败：{exc}"
    restart_ok, restart_message = restart_mosdns()
    if restart_ok:
        return True, f"API 清缓存不可用（{message}），已删除缓存文件并重启 mosdns"
    return False, f"API 清缓存不可用（{message}），删除缓存文件后重启 mosdns 失败：\n{restart_message}"


def rescue_enabled():
    ok, _ = run_cmd(
        [
            "iptables",
            "-t",
            "nat",
            "-C",
            "PREROUTING",
            "-p",
            "udp",
            "--dport",
            "53",
            "-j",
            "DNAT",
            "--to-destination",
            RESCUE_DNS,
        ],
        timeout=10,
    )
    return ok


def service_active():
    ok, _ = run_cmd(["systemctl", "is-active", "--quiet", "mosdns"], timeout=10)
    return ok


def service_enabled():
    ok, _ = run_cmd(["systemctl", "is-enabled", "--quiet", "mosdns"], timeout=10)
    return ok


def get_version():
    if not os.path.exists(MOSDNS_BIN):
        return "未知"
    ok, output = run_cmd([MOSDNS_BIN, "version"], timeout=10)
    if not ok:
        return "未知"
    return output.splitlines()[0] if output else "未知"


def latest_mosdns_release():
    # 先直连 GitHub API，失败再走 .env 里配置的代理
    urls = github_url_candidates(MOSDNS_RELEASE_API)
    last_error = ""
    for url in urls:
        try:
            req = urlrequest.Request(url, headers={"User-Agent": "mosdns-web-manager"})
            with urlrequest.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read(DOWNLOAD_MAX_BYTES).decode("utf-8", "replace"))
            if not isinstance(data, dict):
                raise ValueError("release 接口返回了非对象数据")
            tag = data.get("tag_name") or data.get("name") or ""
            assets = data.get("assets") or []
            asset_names = [asset.get("name", "") for asset in assets if isinstance(asset, dict)]
            wanted_asset = mosdns_asset_name()
            return {
                "success": True,
                "current": get_version(),
                "current_clean": clean_version(get_version()),
                "latest": tag,
                "latest_clean": clean_version(tag),
                "release_url": data.get("html_url", ""),
                "asset": wanted_asset or "",
                "asset_available": bool(wanted_asset and wanted_asset in asset_names),
                "assets": asset_names,
                "source": url,
            }
        except Exception as exc:
            last_error = str(exc)
    return {
        "success": False,
        "current": get_version(),
        "current_clean": clean_version(get_version()),
        "latest": "",
        "latest_clean": "",
        "release_url": "",
        "asset": mosdns_asset_name() or "",
        "asset_available": False,
        "assets": [],
        "source": "",
        "message": "获取最新版本失败：" + last_error,
    }


def server_time_info():
    # cron 按服务器本地时间跑；把时区名、偏移和当前时间一起给前端，前端按偏移算出服务器此刻的钟点
    now = datetime.now().astimezone()
    offset = now.utcoffset() or timedelta(0)
    total = int(offset.total_seconds())
    sign = "+" if total >= 0 else "-"
    hours, minutes = divmod(abs(total) // 60, 60)
    zone = now.tzname() or ""
    label = f"{zone} (UTC{sign}{hours:02d}:{minutes:02d})" if zone else f"UTC{sign}{hours:02d}:{minutes:02d}"
    return {"server_tz": label, "server_time": int(now.timestamp()), "server_utc_offset": total}


def service_health_summary(running, enabled, rescue, values):
    issues = []
    tone = "ok"
    state = "healthy"
    title = "解析服务正常"

    if not running:
        issues.append("mosdns 当前未运行，客户端 DNS 解析可能不可用")
        tone = "error"
        state = "down"
        title = "解析服务已停止"
    elif rescue:
        issues.append(f"救援模式已启用，UDP 53 会被转发到 {RESCUE_DNS}")
        tone = "warn"
        state = "rescue"
        title = "救援模式接管中"
    elif not enabled:
        issues.append("mosdns 未设置开机自启，重启系统后需要手动启动")
        tone = "warn"
        state = "attention"
        title = "服务需要关注"

    if not values.get("local_dns"):
        issues.append("国内上游 DNS 未从配置中识别")
        if tone == "ok":
            tone = "warn"
            state = "attention"
            title = "配置需要检查"
    if not values.get("remote_dns"):
        issues.append("国外上游 DNS 未从配置中识别")
        if tone == "ok":
            tone = "warn"
            state = "attention"
            title = "配置需要检查"
    if not values.get("ttl"):
        issues.append("缓存 TTL 未从配置中识别")
        if tone == "ok":
            tone = "warn"
            state = "attention"
            title = "配置需要检查"

    if not issues:
        issues.append("服务运行、开机自启、上游 DNS 与缓存 TTL 均已识别")

    return {
        "state": state,
        "tone": tone,
        "title": title,
        "issues": issues,
        "last_checked": int(time.time()),
    }


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        address = client_address()
        locked = login_locked_seconds(address)
        if locked:
            return render_template("login.html", error=f"失败次数过多，请 {locked} 秒后再试"), 429
        env = read_env()
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        valid_user = env.get("WEB_USER", "admin")
        valid_pass = env.get("WEB_SECRET", "")
        user_ok = secrets.compare_digest(username.encode("utf-8"), valid_user.encode("utf-8"))
        pass_ok = bool(valid_pass) and secrets.compare_digest(password.encode("utf-8"), valid_pass.encode("utf-8"))
        if user_ok and pass_ok:
            clear_login_failures(address)
            session.clear()
            session["logged_in"] = True
            session.permanent = True
            return redirect("/")
        record_login_failure(address)
        return render_template("login.html", error="用户名或密码错误"), 401
    if session.get("logged_in"):
        return redirect("/")
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect("/login")


@app.route("/")
@login_required
def index():
    # 页面里嵌入自己的版本号：面板升级后旧标签页靠 /api/status 的 panel_version 发现自己过期
    return render_template("index.html", rule_files=RULE_FILES, panel_version=PANEL_VERSION)


@app.route("/api/status")
@login_required
def api_status():
    values = parse_config_values()
    env = read_env()
    running = service_active()
    enabled = service_enabled()
    rescue = rescue_enabled()
    version = get_version()
    return jsonify(
        {
            "running": running,
            "enabled": enabled,
            "rescue": rescue,
            "version": version,
            "version_clean": clean_version(version),
            "panel_version": PANEL_VERSION,
            "panel_version_text": f"Mosctl v{PANEL_VERSION}",
            "web_port": env.get("WEB_PORT", "7840"),
            "health": service_health_summary(running, enabled, rescue, values),
            **server_time_info(),
            **template_version_info(),
            **values,
        }
    )


@app.route("/api/core-version")
@login_required
def api_core_version():
    return jsonify(latest_mosdns_release())


@app.route("/api/panel-upgrade-source")
@login_required
def api_panel_upgrade_source():
    # 页面加载时走缓存；"检测/重新检测"按钮带 refresh=1 强制重新请求 GitHub
    return jsonify(panel_upgrade_state(force=is_true(request.args.get("refresh"))))


@app.route("/api/control", methods=["POST"])
@login_required
@operation_locked
def api_control():
    action = json_body().get("action")
    commands = {
        "start": (["systemctl", "start", "mosdns"], 30),
        "stop": (["systemctl", "stop", "mosdns"], 30),
        "update": ([MOSCTL, "update"], 180),
        "test": ([MOSCTL, "test"], 60),
        "rescue_on": ([MOSCTL, "rescue", "enable"], 60),
        "rescue_off": ([MOSCTL, "rescue", "disable"], 60),
    }
    if action == "restart":
        ok, message = restart_mosdns()
        return jsonify({"success": ok, "message": message})
    if action == "flush":
        ok, message = flush_cache()
        return jsonify({"success": ok, "message": message})
    if action == "restore_default":
        ok, message = restore_default_template()
        return jsonify({"success": ok, "message": message})
    if action in ("upgrade_core", "upgrade_panel") and auto_update_running():
        return jsonify({"success": False, "message": "自动更新正在运行，请等它结束后再手动升级"})
    if action == "upgrade_core":
        ok, message = upgrade_mosdns_core()
        return jsonify({"success": ok, "message": message})
    if action == "upgrade_panel":
        ok, message, should_reload = upgrade_mosctl_panel()
        return jsonify({"success": ok, "message": message, "reload_after": 60 if should_reload else 0})
    if action not in commands:
        return jsonify({"success": False, "message": "未知操作"})
    ok, message = run_cmd(commands[action][0], timeout=commands[action][1])
    return jsonify({"success": ok, "message": message})


@app.route("/api/settings", methods=["GET", "POST"])
@login_required
@operation_locked
def api_settings():
    if request.method == "GET":
        return jsonify(parse_config_values())

    data = json_body()
    ok, message = update_config_values(
        data.get("local_dns", ""),
        data.get("remote_dns", ""),
        data.get("ttl", ""),
        data.get("local_dns_backup") if "local_dns_backup" in data else None,
    )
    return jsonify({"success": ok, "message": message})


@app.route("/api/account-settings", methods=["GET", "POST"])
@login_required
def api_account_settings():
    if request.method == "GET":
        return jsonify(read_account_settings())
    ok, message = write_account_settings(json_body())
    return jsonify({"success": ok, "message": message, **read_account_settings()})


@app.route("/api/config", methods=["GET", "POST"])
@login_required
@operation_locked
def api_config():
    if request.method == "GET":
        return jsonify({"content": read_config_text()})

    data = json_body()
    content = data.get("content", "")
    if not is_safe_text(content, 200000):
        return jsonify({"success": False, "message": "配置内容不合法或过大"})
    ok, message = config_text_starts(content)
    if not ok:
        return jsonify({"success": False, "message": "配置校验失败，未保存：\n" + message})

    backup = backup_file(CONFIG_FILE, "config")
    write_config_text(content)
    result = restart_or_rollback([(backup, CONFIG_FILE)], "配置已保存并重启 mosdns", "配置已保存")
    ok, message = notify_if_rolled_back("config.yaml 修改已回滚", result)
    return jsonify({"success": ok, "message": message})


@app.route("/api/config/migrate", methods=["POST"])
@login_required
@operation_locked
def api_config_migrate():
    # 用当前内置模板重建 config.yaml，保留上游与 TTL；沙箱校验失败则不做任何修改
    result = migrate_to_current_template()
    return jsonify({**result, **template_version_info()})


@app.route("/api/backups", methods=["GET", "POST"])
@login_required
@operation_locked
def api_backups():
    if request.method == "GET":
        return jsonify({"backups": [{k: v for k, v in item.items() if k != "path"} for item in backup_candidates()]})

    backup_id = json_body().get("id")
    ok, message = restore_backup(backup_id)
    return jsonify({"success": ok, "message": message})


@app.route("/api/backup-settings", methods=["GET", "POST"])
@login_required
def api_backup_settings():
    if request.method == "GET":
        return jsonify(read_backup_settings())
    ok, message = write_backup_settings(json_body())
    return jsonify({"success": ok, "message": message, **read_backup_settings()})


@app.route("/api/backups/cleanup", methods=["POST"])
@login_required
@operation_locked
def api_backups_cleanup():
    result = cleanup_old_backups()
    message = f"已清理 {result['deleted_count']} 个旧备份，当前剩余 {result['remaining_count']} 个"
    return jsonify({"success": True, "message": message, **result})


@app.route("/api/rule-sync-settings", methods=["GET", "POST"])
@login_required
def api_rule_sync_settings():
    if request.method == "GET":
        return jsonify(read_sync_settings())
    ok, message = write_sync_settings(json_body())
    return jsonify({"success": ok, "message": message, **read_sync_settings()})


@app.route("/api/rule-sync-test", methods=["POST"])
@login_required
def api_rule_sync_test():
    ok, message, results = test_sync_peers(json_body())
    return jsonify({"success": ok, "message": message, "results": results})


@app.route("/api/geo-schedule", methods=["GET", "POST"])
@login_required
def api_geo_schedule():
    if request.method == "GET":
        return jsonify({**read_geo_schedule(), **geo_update_status()})
    ok, message = write_geo_schedule(json_body())
    return jsonify({"success": ok, "message": message, **read_geo_schedule(), **geo_update_status()})


@app.route("/api/auto-update", methods=["GET", "POST"])
@login_required
@operation_locked
def api_auto_update():
    if request.method == "GET":
        return jsonify(auto_update_overview())
    ok, message = save_auto_update_settings(json_body())
    return jsonify({"success": ok, "message": message, **auto_update_overview()})


@app.route("/api/auto-update/check", methods=["POST"])
@login_required
@operation_locked
def api_auto_update_check():
    ok, report = run_auto_update_dry_run()
    return jsonify({"success": ok, "message": report, **auto_update_overview()})


@app.route("/api/auto-update/run", methods=["POST"])
@login_required
@operation_locked
def api_auto_update_run():
    ok, message = start_auto_update_detached()
    return jsonify({"success": ok, "message": message, **auto_update_overview()})


@app.route("/api/notify-settings", methods=["GET", "POST"])
@login_required
def api_notify_settings():
    # 响应里永远不带完整 Webhook 地址，只给主机名
    if request.method == "GET":
        return jsonify(read_notify_settings())
    ok, message = save_notify_settings(json_body())
    return jsonify({"success": ok, "message": message, **read_notify_settings()})


@app.route("/api/notify-test", methods=["POST"])
@login_required
def api_notify_test():
    return jsonify(send_test_notification(json_body()))


def sync_token_matches(provided, expected):
    if not expected or not isinstance(provided, str):
        return False
    return secrets.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


@app.route("/api/rule-sync", methods=["POST"])
@operation_locked
def api_rule_sync():
    env = read_env()
    expected = env.get("RULE_SYNC_TOKEN", "")
    # 优先用请求头里的密钥，这样密钥错误时根本不用解析请求体
    provided = request.headers.get("X-Mosdns-Sync-Token", "")
    data = None
    if not provided:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"success": False, "message": "请求体必须是 JSON 对象"}), 400
        provided = str(data.get("token") or "")
    if not sync_token_matches(provided, expected):
        return jsonify({"success": False, "message": "同步密钥错误"}), 403
    if data is None:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"success": False, "message": "请求体必须是 JSON 对象"}), 400
    if data.get("rules") == {}:
        # 对端面板的「测试连通性」只发空规则：密钥已通过就算成功，不当作同步失败去告警
        return jsonify({"success": True, "message": SYNC_PING_MESSAGE})
    ok, message = apply_synced_rules(data.get("rules"))
    notify_rule_sync_receive(ok, message, client_address())
    return jsonify({"success": ok, "message": message})


@app.route("/api/rules/<rule_id>", methods=["GET", "POST"])
@login_required
@operation_locked
def api_rules(rule_id):
    meta = RULE_FILES.get(rule_id)
    if not meta:
        return jsonify({"success": False, "message": "未知规则文件"}), 404

    path = meta["path"]
    if request.method == "GET":
        content = ""
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as file:
                content = file.read()
        return jsonify(
            {
                "id": rule_id,
                "label": meta["label"],
                "summary": meta.get("summary", ""),
                "format": meta.get("format", ""),
                "examples": meta.get("examples", []),
                "content": content,
            }
        )

    content = json_body().get("content", "")
    # 仍是一个同步请求；timings 让前端显示"校验 x.xs，重启 y.ys"（校验含格式检查、沙箱启动和写文件）
    timings = {"validate_ms": 0, "restart_ms": 0}
    unchanged = rule_content_unchanged(rule_id, content)
    if unchanged:
        # 内容等价：不写文件、不备份、不跑沙箱、不重启；但仍推送给其他节点（它们可能还是旧的，已一致的会自己跳过）
        ok, message = True, RULES_UNCHANGED_MESSAGE
    else:
        started = time.monotonic()
        saved, save_message, rollback = save_rule_content(rule_id, content)
        timings["validate_ms"] = elapsed_ms(started)
        if not saved:
            return jsonify({"success": False, "message": save_message, "sync_job": None, "unchanged": False, "timings": timings})
        started = time.monotonic()
        result = restart_or_rollback([rollback], "规则已保存并重启 mosdns", "规则已保存")
        timings["restart_ms"] = elapsed_ms(started)
        ok, message = notify_if_rolled_back(f"{meta['label']}规则修改已回滚", result)
    sync_job = None
    if ok and rule_id in SYNCABLE_RULE_IDS:
        # 同步在后台线程里进行，不占用本请求的操作锁；前端拿 sync_job 轮询结果
        sync_job, sync_message = start_broadcast(rule_id, content)
        if sync_job:
            message = message + "；" + sync_message
        elif sync_message:
            message = message + "\n\n" + sync_message
    return jsonify({"success": ok, "message": message, "sync_job": sync_job, "unchanged": unchanged, "timings": timings})


@app.route("/api/rule-sync-jobs/<job_id>")
@login_required
def api_rule_sync_job(job_id):
    # job_id 为 latest 时返回最近一次同步任务；还没有任务时 job 为 null
    job = find_sync_job(job_id)
    if job is None and job_id != "latest":
        return jsonify({"success": False, "message": "同步任务不存在或已过期"}), 404
    return jsonify({"success": True, "job": job})


@app.route("/api/logs")
@login_required
def api_logs():
    lines = request.args.get("lines", "160")
    if not re.fullmatch(r"\d{1,4}", lines):
        lines = "160"
    if not os.path.exists(LOG_FILE):
        return jsonify({"logs": "日志文件不存在"})
    ok, output = run_cmd(["tail", "-n", lines, LOG_FILE], timeout=10)
    if not ok:
        return jsonify({"logs": "读取日志失败：\n" + output})
    logs = normalize_log_timestamps(output)
    if request.args.get("order", "desc") == "desc":
        logs = "\n".join(reversed(logs.splitlines()))
    return jsonify({"logs": logs, "entries": parse_log_entries(logs)})


# 放在所有函数定义之后：它用到 read_crontab_lines / is_geo_update_cron，放前面会 NameError
try:
    migrate_cron_logging()
except Exception:
    pass


if __name__ == "__main__":
    panel_startup_tasks()
    env = read_env()
    try:
        port = int(env.get("WEB_PORT", "7840"))
    except ValueError:
        port = 7840
    app.run(host="0.0.0.0", port=port)
