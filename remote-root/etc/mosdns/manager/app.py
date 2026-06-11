from datetime import datetime, timedelta
from functools import wraps
import glob
import os
import re
import secrets
import shutil
import subprocess
import time
from urllib import error, request as urlrequest
import json
import stat
import tempfile
import zipfile
from urllib.parse import quote

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
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
LOG_TIMESTAMP_RE = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:?\d\d))(.*)$")
SYNCABLE_RULE_IDS = {"force-cn", "force-nocn"}
MOSDNS_RELEASE_BASE = "https://github.com/IrineSistiana/mosdns/releases/latest/download"
MOSDNS_RELEASE_API = "https://api.github.com/repos/IrineSistiana/mosdns/releases/latest"
GEO_UPDATE_COMMAND = f"{MOSCTL} update"
GEO_CRON_COMMENT = "# MosDNS Web: Geo update schedule"
DEFAULT_MOSCTL_REPO_URL = "https://github.com/anxiaoyang666/mosctl.git"
DEFAULT_MOSCTL_BRANCH = "main"
PANEL_VERSION = "0.3.14"
PANEL_UPGRADE_EXCLUDES = (ENV_FILE, CONFIG_FILE, f"{MOSDNS_DIR}/rules", "/etc/mosdns/rules")
PANEL_BACKUP_KEEP_COUNT = 3

RULE_FILES = {
    "force-cn": {
        "label": "强制国内",
        "path": f"{MOSDNS_DIR}/rules/force-cn.txt",
        "summary": "命中的域名强制走国内上游 DNS，适合国内站点被误判到国外时使用。",
        "format": "每行一个域名。通常写主域名即可。",
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
        "format": "每行一个域名。通常写主域名即可。",
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
        "format": "每行一个：域名 IP。可以用 # 写注释。",
        "examples": [
            "# nas.lan 固定到 NAS",
            "nas.lan 10.10.30.10",
            "router.lan 10.10.30.1",
        ],
    },
}


app = Flask(__name__)
app.permanent_session_lifetime = timedelta(days=365)


def clean_output(text):
    text = ANSI_RE.sub("", text or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.strip()


def normalize_log_timestamps(text):
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
            local_time = datetime.fromisoformat(timestamp).astimezone()
            lines.append(local_time.strftime("%Y-%m-%d %H:%M:%S") + match.group(2))
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


def write_env(updates):
    os.makedirs(MOSDNS_DIR, exist_ok=True)
    lines = []
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, "r", encoding="utf-8") as file:
            lines = file.readlines()

    seen = set()
    with open(ENV_FILE, "w", encoding="utf-8") as file:
        for line in lines:
            stripped = line.strip()
            if "=" in stripped and not stripped.startswith("#"):
                key = stripped.split("=", 1)[0].strip()
                if key in updates:
                    file.write(f'{key}="{updates[key]}"\n')
                    seen.add(key)
                else:
                    file.write(line)
            else:
                file.write(line)
        for key, value in updates.items():
            if key not in seen:
                file.write(f'{key}="{value}"\n')
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
    app.secret_key = os.environ.get("WEB_SESSION_SECRET") or env["WEB_SESSION_SECRET"]


ensure_env()


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


def parse_config_values():
    text = read_config_text()

    ttl_match = re.search(r"(?m)^\s*lazy_cache_ttl:\s*(\d+)\s*$", text)
    local_match = re.search(r'(?m)^(\s*-\s*addr:\s*)["\']?([^"\'#\n]+)["\']?\s*#\s*TAG_LOCAL\s*$', text)
    remote_match = re.search(r'(?m)^(\s*-\s*addr:\s*)["\']?([^"\'#\n]+)["\']?\s*#\s*TAG_REMOTE\s*$', text)

    local_raw = local_match.group(2).strip() if local_match else ""
    remote_raw = remote_match.group(2).strip() if remote_match else ""

    return {
        "ttl": ttl_match.group(1) if ttl_match else "",
        "local_dns": display_upstream(local_raw, "udp"),
        "remote_dns": display_upstream(remote_raw, None),
        "local_dns_raw": local_raw,
        "remote_dns_raw": remote_raw,
    }


def display_upstream(value, default_scheme=None):
    value = str(value or "").strip()
    if default_scheme and value.startswith(f"{default_scheme}://"):
        value = value[len(default_scheme) + 3 :]
    if "://" not in value and value.endswith(":53"):
        value = value[:-3]
    return value


def looks_like_host_port(value):
    if value.count(":") == 1:
        host, port = value.rsplit(":", 1)
        return bool(host) and port.isdigit()
    return False


def normalize_upstream(value, default_scheme=None, default_port=None):
    value = str(value or "").strip()
    if "://" in value:
        return value
    if default_scheme:
        return f"{default_scheme}://{value}"
    if default_port and ":" not in value and not looks_like_host_port(value):
        return f"{value}:{default_port}"
    return value


def restart_mosdns():
    return run_cmd(["systemctl", "restart", "mosdns"], timeout=30)


def config_starts(path):
    proc = None
    tmpdir = None
    try:
        tmpdir = tempfile.mkdtemp(prefix="mosdns-check-")
        check_path = os.path.join(tmpdir, "config.yaml")
        with open(path, "r", encoding="utf-8") as source:
            content = source.read()
        content = re.sub(
            r'(?m)^(\s*http:\s*)["\']?[^"\'\n]+["\']?\s*$',
            r'\g<1>"127.0.0.1:0"',
            content,
            count=1,
        )
        with open(check_path, "w", encoding="utf-8") as target:
            target.write(content)
        proc = subprocess.Popen(
            [MOSDNS_BIN, "start", "-d", tmpdir],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=2)
            return False, clean_output(stdout + stderr)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
            return True, "配置可以启动"
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


def restore_default_template():
    if not os.path.exists(DEFAULT_TEMPLATE_FILE):
        return False, "内置默认模板不存在"

    current = parse_config_values()
    with open(DEFAULT_TEMPLATE_FILE, "r", encoding="utf-8") as file:
        content = file.read()

    local_dns = current.get("local_dns_raw") or normalize_upstream(current.get("local_dns", ""), default_scheme="udp")
    remote_dns = current.get("remote_dns_raw") or normalize_upstream(current.get("remote_dns", ""), default_port=53)
    ttl = current.get("ttl") or "86400"

    content = re.sub(
        r'(?m)^(\s*-\s*addr:\s*)["\']?[^"\'#\n]+["\']?(\s*#\s*TAG_LOCAL\s*)$',
        rf'\g<1>"{local_dns}"\g<2>',
        content,
        count=1,
    )
    content = re.sub(
        r'(?m)^(\s*-\s*addr:\s*)["\']?[^"\'#\n]+["\']?(\s*#\s*TAG_REMOTE\s*)$',
        rf'\g<1>"{remote_dns}"\g<2>',
        content,
        count=1,
    )
    content = re.sub(
        r"(?m)^(\s*lazy_cache_ttl:\s*)\d+\s*$",
        rf"\g<1>{ttl}",
        content,
        count=1,
    )

    tmp_file = f"{CONFIG_FILE}.defaultcheck"
    with open(tmp_file, "w", encoding="utf-8") as file:
        file.write(content)
    ok, message = config_starts(tmp_file)
    if not ok:
        try:
            os.remove(tmp_file)
        except OSError:
            pass
        return False, "内置默认模板校验失败，未替换当前配置：\n" + message

    backup_file(CONFIG_FILE)
    os.replace(tmp_file, CONFIG_FILE)
    ok, message = restart_mosdns()
    if not ok:
        return False, "默认配置已写入，但 mosdns 重启失败：\n" + message
    return True, "已恢复内置默认配置，并保留当前上游 DNS 与 TTL。"


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


def download_file(urls, target):
    last_error = ""
    for url in urls:
        try:
            req = urlrequest.Request(url, headers={"User-Agent": "mosdns-web-manager"})
            with urlrequest.urlopen(req, timeout=90) as resp, open(target, "wb") as file:
                shutil.copyfileobj(resp, file)
            if os.path.getsize(target) > 0:
                return True, url
        except Exception as exc:
            last_error = str(exc)
    return False, last_error


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


def github_raw_app_url(repo_url, branch):
    parts = github_repo_parts(repo_url)
    if not parts:
        return ""
    owner, name = parts
    return f"https://raw.githubusercontent.com/{owner}/{name}/{quote(branch, safe='/')}/remote-root/etc/mosdns/manager/app.py"


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


def remote_panel_version(settings=None):
    settings = settings or mosctl_repo_settings()
    raw_url = github_raw_app_url(settings["repo_url"], settings["branch"])
    raw_error = ""
    if not raw_url:
        return {
            "success": False,
            "latest_version": "",
            "source": "",
            "message": "仅支持 GitHub 仓库在线检测，请检查 MOSCTL_REPO_URL",
        }
    raw_url = cache_bust_url(raw_url)
    ok, text, source = read_url_text([f"https://gh-proxy.com/{raw_url}", raw_url], timeout=15)
    if ok:
        version = parse_panel_version(text)
        if version:
            return {"success": True, "latest_version": version, "source": source, "message": ""}
        raw_error = "raw 文件没有版本号，已改用 zip 包检测。"
    else:
        raw_error = "raw 文件检测失败，已改用 zip 包检测：\n" + text

    with tempfile.TemporaryDirectory() as tmpdir:
        zip_ok, zip_source, _, zip_settings = download_mosctl_source(tmpdir)
        if zip_ok and zip_settings.get("remote_version"):
            return {
                "success": True,
                "latest_version": zip_settings["remote_version"],
                "source": zip_source,
                "message": raw_error,
            }
        if zip_ok:
            return {
                "success": False,
                "latest_version": "",
                "source": zip_source,
                "message": "远端面板没有版本号，可能是旧版本，已禁止在线升级以避免降级。",
            }
    return {"success": False, "latest_version": "", "source": source, "message": "检测远端面板版本失败：\n" + raw_error}


def panel_upgrade_state():
    settings = mosctl_repo_settings()
    remote = remote_panel_version(settings)
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
        "source": remote.get("source", ""),
        "message": remote.get("message", ""),
    }


def download_mosctl_source(tmpdir):
    settings = mosctl_repo_settings()
    archive_url = github_archive_url(settings["repo_url"], settings["branch"])
    if not archive_url:
        return False, "仅支持 GitHub 仓库在线升级，请检查 MOSCTL_REPO_URL", None, settings
    archive_url = cache_bust_url(archive_url)

    zip_path = os.path.join(tmpdir, "mosctl-panel.zip")
    ok, source = download_file([f"https://gh-proxy.com/{archive_url}", archive_url], zip_path)
    if not ok:
        return False, "下载 Mosctl 面板失败：\n" + source, None, settings

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


def upgrade_mosctl_panel():
    with tempfile.TemporaryDirectory() as tmpdir:
        ok, source, source_root, settings = download_mosctl_source(tmpdir)
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
        "Mosctl 面板升级完成，Web 服务将在 1 秒后重启。\n"
        f"来源：{source}\n"
        f"仓库：{settings['repo_url']}\n"
        f"分支：{settings['branch']}\n"
        f"旧版本：v{PANEL_VERSION}\n"
        f"新版本：v{remote_version}\n"
        f"旧面板备份：{backup_root}\n"
        "请稍等几秒后刷新页面。"
        ,
        True,
    )


def upgrade_mosdns_core():
    asset = mosdns_asset_name()
    if not asset:
        return False, "当前 CPU 架构暂不支持自动升级"

    old_ok, old_version = run_cmd([MOSDNS_BIN, "version"], timeout=10) if os.path.exists(MOSDNS_BIN) else (False, "未知")
    latest = latest_mosdns_release()
    if not latest.get("success"):
        return False, latest.get("message", "获取最新版本失败，已取消升级")
    current_v = version_tuple(old_version)
    latest_v = version_tuple(latest.get("latest"))
    if current_v and latest_v and latest_v <= current_v:
        return False, f"当前版本不低于官方 latest release，已取消升级。\n当前版本：{clean_version(old_version)}\n官方 latest：{clean_version(latest.get('latest'))}"
    if not latest.get("asset_available"):
        return False, f"官方 latest release 未发现当前架构安装包：{asset}"

    direct_url = f"{MOSDNS_RELEASE_BASE}/{asset}"
    urls = [
        direct_url,
        f"https://gh-proxy.com/{direct_url}",
    ]

    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d%H%M%S")
    backup_bin = f"{BACKUP_DIR}/mosdns-bin.{stamp}.bak"

    with tempfile.TemporaryDirectory() as tmpdir:
        zip_path = os.path.join(tmpdir, asset)
        ok, source = download_file(urls, zip_path)
        if not ok:
            return False, "下载 mosdns 内核失败：\n" + source

        try:
            with zipfile.ZipFile(zip_path) as archive:
                archive.extractall(tmpdir)
        except zipfile.BadZipFile:
            return False, "下载文件不是有效 zip，已取消升级"

        candidate = None
        for root, _, files in os.walk(tmpdir):
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
            return False, "压缩包里没有找到可运行的 mosdns 二进制"

        if os.path.exists(MOSDNS_BIN):
            shutil.copy2(MOSDNS_BIN, backup_bin)

        run_cmd(["systemctl", "stop", "mosdns"], timeout=30)
        try:
            shutil.copy2(candidate, MOSDNS_BIN)
            os.chmod(MOSDNS_BIN, 0o755)
            ok, restart_message = restart_mosdns()
        except Exception as exc:
            ok, restart_message = False, str(exc)

        if ok and service_active():
            cleanup_old_backups()
            return True, f"mosdns 内核升级完成。\n来源：{source}\n旧版本：{clean_version(old_version)}\n新版本：{clean_version(new_version)}\n旧内核备份：{backup_bin}"

        if os.path.exists(backup_bin):
            shutil.copy2(backup_bin, MOSDNS_BIN)
            os.chmod(MOSDNS_BIN, 0o755)
            restart_mosdns()
        return False, "新内核启动失败，已回滚旧内核：\n" + restart_message


def safe_sync_config():
    if not os.path.exists(CONFIG_FILE):
        return False, "当前配置文件不存在，已取消同步"

    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d%H%M%S")
    safe_backup = f"{BACKUP_DIR}/config.pre-web-sync.{stamp}.yaml"
    shutil.copy2(CONFIG_FILE, safe_backup)
    cleanup_old_backups()

    ok, output = run_cmd([MOSCTL, "sync"], timeout=180)
    time.sleep(1)
    if service_active():
        return ok, output or "同步完成，mosdns 正在运行"

    shutil.copy2(safe_backup, CONFIG_FILE)
    restart_ok, restart_output = restart_mosdns()
    message = (
        "同步后的配置导致 mosdns 启动失败，已自动恢复同步前配置。\n\n"
        f"同步输出：\n{output}\n\n"
        f"恢复结果：\n{restart_output}"
    )
    return False, message if restart_ok else message + "\n\n恢复后重启仍失败，请手动检查。"


def backup_file(path):
    if not os.path.exists(path):
        return
    os.makedirs(BACKUP_DIR, exist_ok=True)
    name = os.path.basename(path)
    stamp = time.strftime("%Y%m%d%H%M%S")
    shutil.copy2(path, f"{BACKUP_DIR}/{name}.{stamp}.bak")
    cleanup_old_backups()


def backup_candidates():
    paths = []
    patterns = [
        f"{BACKUP_DIR}/*.yaml",
        f"{BACKUP_DIR}/*.bak",
        f"{MOSDNS_DIR}/config.yaml.bak",
        f"{MOSDNS_DIR}/config.yaml.bad-sync.*",
    ]
    for pattern in patterns:
        paths.extend(glob.glob(pattern))

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
                "mtime_text": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
            }
        )
    items.sort(key=lambda item: item["mtime"], reverse=True)
    return items


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

    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d%H%M%S")
    current_backup = f"{BACKUP_DIR}/config.before-restore.{stamp}.yaml"
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
    if not is_safe_text(token, 200) or "\n" in token or "\r" in token:
        return False, "同步密钥不合法"
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
        "username": os.environ.get("WEB_USER") or env.get("WEB_USER", "admin"),
    }


def write_account_settings(data):
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")
    confirm = str(data.get("confirm") or "")
    if not username:
        return False, "用户名不能为空"
    if not is_safe_text(username, 64) or any(char.isspace() for char in username):
        return False, "用户名不能包含空格或换行，最多 64 个字符"
    updates = {"WEB_USER": username}
    if password or confirm:
        if password != confirm:
            return False, "两次输入的新密码不一致"
        if len(password) < 6 or not is_safe_text(password, 200) or "\n" in password or "\r" in password:
            return False, "新密码至少 6 位，且不能包含换行"
        updates["WEB_SECRET"] = password
    write_env(updates)
    os.environ.update(updates)
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
    for peer in peers:
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


def read_crontab_lines():
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        return []
    return result.stdout.splitlines()


def is_geo_update_cron(line):
    stripped = str(line or "").strip()
    return bool(stripped and not stripped.startswith("#") and MOSCTL in stripped and re.search(r"\bupdate\b", stripped))


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
    cron_line = f"{minute} {hour_field} * * {weekday_field} {GEO_UPDATE_COMMAND} > /dev/null 2>&1"
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
    crontab_text = "\n".join(lines).strip()
    if crontab_text:
        crontab_text += "\n"
    result = subprocess.run(["crontab", "-"], input=crontab_text, capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        return False, clean_output(result.stdout + result.stderr) or "写入 crontab 失败"
    return True, describe_geo_schedule(mode, time_value, weekday)


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


def cleanup_old_backups(keep_count=None):
    keep_count = backup_keep_count() if keep_count is None else int(keep_count)
    items = backup_candidates()
    config_items = [item for item in items if not item["id"].startswith("mosdns-bin.")]
    kernel_items = [item for item in items if item["id"].startswith("mosdns-bin.")]
    stale_items = config_items[keep_count:] + kernel_items[KERNEL_BACKUP_KEEP_COUNT:]
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


def save_rule_content(rule_id, content):
    meta = RULE_FILES.get(rule_id)
    if not meta:
        return False, "未知规则文件"
    if not is_safe_text(content):
        return False, "规则内容不合法或过大"

    path = meta["path"]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    backup_file(path)
    with open(path, "w", encoding="utf-8") as file:
        file.write(content)
    return True, "规则已保存"


def broadcast_rule(rule_id, content):
    if rule_id not in SYNCABLE_RULE_IDS:
        return ""
    settings = read_sync_settings()
    if not settings["enabled"]:
        return "规则同步未启用。"
    if not settings["peers"]:
        return "规则同步已启用，但没有配置其他节点。"
    if not settings["token"]:
        return "规则同步已启用，但缺少同步密钥。"

    payload = json.dumps(
        {
            "token": settings["token"],
            "rules": {rule_id: content},
            "source": request.host_url.rstrip("/"),
        }
    ).encode("utf-8")
    results = []
    headers = {
        "Content-Type": "application/json",
        "X-Mosdns-Sync-Token": settings["token"],
    }
    for peer in settings["peers"]:
        url = peer.rstrip("/") + "/api/rule-sync"
        try:
            req = urlrequest.Request(url, data=payload, headers=headers, method="POST")
            with urlrequest.urlopen(req, timeout=15) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            if body.get("success"):
                results.append(f"{peer}: 成功")
            else:
                results.append(f"{peer}: 失败 - {body.get('message', '未知错误')}")
        except Exception as exc:
            results.append(f"{peer}: 失败 - {exc}")
    return "同步结果：\n" + "\n".join(results)


def apply_synced_rules(rules):
    if not isinstance(rules, dict):
        return False, "同步内容不合法"
    applied = []
    for rule_id, content in rules.items():
        if rule_id not in SYNCABLE_RULE_IDS:
            continue
        ok, message = save_rule_content(rule_id, content)
        if not ok:
            return False, message
        applied.append(rule_id)
    if not applied:
        return False, "没有可同步的规则"
    ok, message = restart_mosdns()
    if not ok:
        return False, "规则已写入，但 mosdns 重启失败：\n" + message
    return True, "已同步规则：" + ", ".join(applied)


def update_config_values(local_dns, remote_dns, ttl):
    if not os.path.exists(CONFIG_FILE):
        return False, "配置文件不存在"
    if not re.fullmatch(r"\d{1,7}", str(ttl or "")):
        return False, "TTL 必须是数字"
    for label, value in (("国内 DNS", local_dns), ("国外 DNS", remote_dns)):
        if not is_safe_text(value, 200) or "\n" in value or "\r" in value or not value.strip():
            return False, f"{label} 不合法"
    local_dns = normalize_upstream(local_dns, default_scheme="udp")
    remote_dns = normalize_upstream(remote_dns, default_port=53)

    text = read_config_text()
    new_text, ttl_count = re.subn(
        r"(?m)^(\s*lazy_cache_ttl:\s*)\d+\s*$",
        rf"\g<1>{ttl}",
        text,
        count=1,
    )
    new_text, local_count = re.subn(
        r'(?m)^(\s*-\s*addr:\s*)["\']?[^"\'#\n]+["\']?(\s*#\s*TAG_LOCAL\s*)$',
        rf'\g<1>"{local_dns.strip()}"\g<2>',
        new_text,
        count=1,
    )
    new_text, remote_count = re.subn(
        r'(?m)^(\s*-\s*addr:\s*)["\']?[^"\'#\n]+["\']?(\s*#\s*TAG_REMOTE\s*)$',
        rf'\g<1>"{remote_dns.strip()}"\g<2>',
        new_text,
        count=1,
    )
    if ttl_count != 1 or local_count != 1 or remote_count != 1:
        return False, "没有找到 TAG_LOCAL、TAG_REMOTE 或 lazy_cache_ttl"

    if new_text == text:
        return True, "配置无变化"

    backup_file(CONFIG_FILE)
    tmp_file = f"{CONFIG_FILE}.webtmp"
    with open(tmp_file, "w", encoding="utf-8") as file:
        file.write(new_text)
    os.replace(tmp_file, CONFIG_FILE)
    ok, message = restart_mosdns()
    if not ok:
        return False, "配置已保存，但 mosdns 重启失败：\n" + message
    return True, "配置已保存并重启 mosdns"


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
    urls = [
        f"https://gh-proxy.com/{MOSDNS_RELEASE_API}",
        MOSDNS_RELEASE_API,
    ]
    last_error = ""
    for url in urls:
        try:
            req = urlrequest.Request(url, headers={"User-Agent": "mosdns-web-manager"})
            with urlrequest.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
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
        "last_checked": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        env = read_env()
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        valid_user = os.environ.get("WEB_USER") or env.get("WEB_USER", "admin")
        valid_pass = os.environ.get("WEB_SECRET") or env.get("WEB_SECRET", "")
        if username == valid_user and password == valid_pass:
            session["logged_in"] = True
            session.permanent = True
            return redirect("/")
        return render_template("login.html", error="用户名或密码错误")
    if session.get("logged_in"):
        return redirect("/")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.route("/")
@login_required
def index():
    return render_template("index.html", rule_files=RULE_FILES)


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
    return jsonify(panel_upgrade_state())


@app.route("/api/control", methods=["POST"])
@login_required
def api_control():
    action = (request.json or {}).get("action")
    commands = {
        "start": (["systemctl", "start", "mosdns"], 30),
        "stop": (["systemctl", "stop", "mosdns"], 30),
        "restart": (["systemctl", "restart", "mosdns"], 30),
        "update": ([MOSCTL, "update"], 180),
        "flush": ([MOSCTL, "flush"], 60),
        "test": ([MOSCTL, "test"], 60),
        "rescue_on": ([MOSCTL, "rescue", "enable"], 60),
        "rescue_off": ([MOSCTL, "rescue", "disable"], 60),
    }
    if action == "restore_default":
        ok, message = restore_default_template()
        return jsonify({"success": ok, "message": message})
    if action == "upgrade_core":
        ok, message = upgrade_mosdns_core()
        return jsonify({"success": ok, "message": message})
    if action == "upgrade_panel":
        ok, message, should_reload = upgrade_mosctl_panel()
        return jsonify({"success": ok, "message": message, "reload_after": 5 if should_reload else 0})
    if action not in commands:
        return jsonify({"success": False, "message": "未知操作"})
    ok, message = run_cmd(commands[action][0], timeout=commands[action][1])
    return jsonify({"success": ok, "message": message})


@app.route("/api/settings", methods=["GET", "POST"])
@login_required
def api_settings():
    if request.method == "GET":
        return jsonify(parse_config_values())

    data = request.json or {}
    ok, message = update_config_values(
        data.get("local_dns", ""),
        data.get("remote_dns", ""),
        data.get("ttl", ""),
    )
    return jsonify({"success": ok, "message": message})


@app.route("/api/account-settings", methods=["GET", "POST"])
@login_required
def api_account_settings():
    if request.method == "GET":
        return jsonify(read_account_settings())
    ok, message = write_account_settings(request.json or {})
    return jsonify({"success": ok, "message": message, **read_account_settings()})


@app.route("/api/config", methods=["GET", "POST"])
@login_required
def api_config():
    if request.method == "GET":
        return jsonify({"content": read_config_text()})

    data = request.json or {}
    content = data.get("content", "")
    if not is_safe_text(content, 200000):
        return jsonify({"success": False, "message": "配置内容不合法或过大"})

    backup_file(CONFIG_FILE)
    tmp_file = f"{CONFIG_FILE}.webtmp"
    with open(tmp_file, "w", encoding="utf-8") as file:
        file.write(content)
    os.replace(tmp_file, CONFIG_FILE)
    ok, message = restart_mosdns()
    return jsonify(
        {
            "success": ok,
            "message": "配置已保存并重启 mosdns" if ok else "配置已保存，但 mosdns 重启失败：\n" + message,
        }
    )


@app.route("/api/backups", methods=["GET", "POST"])
@login_required
def api_backups():
    if request.method == "GET":
        return jsonify({"backups": [{k: v for k, v in item.items() if k != "path"} for item in backup_candidates()]})

    backup_id = (request.json or {}).get("id")
    ok, message = restore_backup(backup_id)
    return jsonify({"success": ok, "message": message})


@app.route("/api/backup-settings", methods=["GET", "POST"])
@login_required
def api_backup_settings():
    if request.method == "GET":
        return jsonify(read_backup_settings())
    ok, message = write_backup_settings(request.json or {})
    return jsonify({"success": ok, "message": message, **read_backup_settings()})


@app.route("/api/backups/cleanup", methods=["POST"])
@login_required
def api_backups_cleanup():
    result = cleanup_old_backups()
    message = f"已清理 {result['deleted_count']} 个旧备份，当前剩余 {result['remaining_count']} 个"
    return jsonify({"success": True, "message": message, **result})


@app.route("/api/rule-sync-settings", methods=["GET", "POST"])
@login_required
def api_rule_sync_settings():
    if request.method == "GET":
        return jsonify(read_sync_settings())
    ok, message = write_sync_settings(request.json or {})
    return jsonify({"success": ok, "message": message, **read_sync_settings()})


@app.route("/api/rule-sync-test", methods=["POST"])
@login_required
def api_rule_sync_test():
    ok, message, results = test_sync_peers(request.json or {})
    return jsonify({"success": ok, "message": message, "results": results})


@app.route("/api/geo-schedule", methods=["GET", "POST"])
@login_required
def api_geo_schedule():
    if request.method == "GET":
        return jsonify(read_geo_schedule())
    ok, message = write_geo_schedule(request.json or {})
    return jsonify({"success": ok, "message": message, **read_geo_schedule()})


@app.route("/api/rule-sync", methods=["POST"])
def api_rule_sync():
    env = read_env()
    expected = env.get("RULE_SYNC_TOKEN", "")
    provided = request.headers.get("X-Mosdns-Sync-Token", "")
    data = request.json or {}
    if not provided:
        provided = str(data.get("token") or "")
    if not expected or not secrets.compare_digest(provided, expected):
        return jsonify({"success": False, "message": "同步密钥错误"}), 403
    ok, message = apply_synced_rules(data.get("rules"))
    return jsonify({"success": ok, "message": message})


@app.route("/api/rules/<rule_id>", methods=["GET", "POST"])
@login_required
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

    content = (request.json or {}).get("content", "")
    saved, save_message = save_rule_content(rule_id, content)
    if not saved:
        return jsonify({"success": False, "message": save_message})
    ok, message = restart_mosdns()
    if ok and rule_id in SYNCABLE_RULE_IDS:
        sync_message = broadcast_rule(rule_id, content)
        if sync_message:
            message = "规则已保存并重启 mosdns\n\n" + sync_message
    return jsonify(
        {
            "success": ok,
            "message": message if ok else "规则已保存，但 mosdns 重启失败：\n" + message,
        }
    )


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


if __name__ == "__main__":
    env = read_env()
    try:
        port = int(env.get("WEB_PORT", "7840"))
    except ValueError:
        port = 7840
    app.run(host="0.0.0.0", port=port)
