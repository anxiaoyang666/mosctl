#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_NAME="Mosctl"
GH_PROXY="${GH_PROXY:-https://gh-proxy.com/}"
REPO_URL="${MOSCTL_REPO_URL:-https://github.com/anxiaoyang666/mosctl.git}"
BRANCH="${MOSCTL_BRANCH:-main}"
MOSDNS_VERSION="${MOSDNS_VERSION:-latest}"
WEB_PORT="${WEB_PORT:-7840}"
INSTALL_DIR="/etc/mosdns"
MANAGER_DIR="$INSTALL_DIR/manager"
TMP_DIR=""

# 状态信息走 stderr，避免被 $(...) 捕获进变量
red() { printf '\033[0;31m%s\033[0m\n' "$*" >&2; }
green() { printf '\033[0;32m%s\033[0m\n' "$*" >&2; }
yellow() { printf '\033[1;33m%s\033[0m\n' "$*" >&2; }
die() { red "ERROR: $*"; exit 1; }

cleanup() {
  if [ -n "${TMP_DIR:-}" ] && [ -d "$TMP_DIR" ]; then
    rm -rf "$TMP_DIR"
  fi
}
trap cleanup EXIT

require_root() {
  if [ "$(id -u)" -ne 0 ]; then
    die "请使用 root 执行安装命令。"
  fi
}

has_cmd() {
  command -v "$1" >/dev/null 2>&1
}

install_packages() {
  if has_cmd apt-get; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y ca-certificates curl wget unzip tar git python3 python3-flask iptables dnsutils nano cron
  elif has_cmd dnf; then
    dnf install -y ca-certificates curl wget unzip tar git python3 python3-flask iptables bind-utils nano cronie
  elif has_cmd yum; then
    yum install -y ca-certificates curl wget unzip tar git python3 python3-flask iptables bind-utils nano cronie
  else
    die "未找到 apt-get/dnf/yum，无法自动安装依赖。"
  fi
}

url_with_proxy() {
  local url="$1"
  if [ -n "$GH_PROXY" ] && [[ "$url" == https://github.com/* || "$url" == https://raw.githubusercontent.com/* ]]; then
    printf '%s%s' "$GH_PROXY" "$url"
  else
    printf '%s' "$url"
  fi
}

git_clone_project() {
  local target="$1"
  local proxy_url
  proxy_url="$(url_with_proxy "$REPO_URL")"

  if git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$target"; then
    return
  fi
  yellow "直接拉取失败，尝试通过 GitHub 代理拉取..."
  rm -rf "$target"
  git clone --depth 1 --branch "$BRANCH" "$proxy_url" "$target"
}

# 结果写入 SOURCE_ROOT 而不是 echo，这样 TMP_DIR 能留在当前 shell 供 cleanup 使用
SOURCE_ROOT=""
source_root() {
  local script_dir=""
  if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  fi
  if [ -n "$script_dir" ] && [ -d "$script_dir/remote-root" ]; then
    SOURCE_ROOT="$script_dir"
    return
  fi

  TMP_DIR="$(mktemp -d)"
  git_clone_project "$TMP_DIR/mosctl"
  [ -d "$TMP_DIR/mosctl/remote-root" ] || die "仓库中没有 remote-root 目录。"
  SOURCE_ROOT="$TMP_DIR/mosctl"
}

detect_arch_asset() {
  local arch
  arch="$(uname -m)"
  case "$arch" in
    x86_64|amd64) echo "mosdns-linux-amd64.zip" ;;
    aarch64|arm64) echo "mosdns-linux-arm64.zip" ;;
    armv7*) echo "mosdns-linux-arm-7.zip" ;;
    armv6*) echo "mosdns-linux-arm-6.zip" ;;
    armv5*) echo "mosdns-linux-arm-5.zip" ;;
    *) die "暂不支持当前架构：$arch" ;;
  esac
}

download_file() {
  local url="$1"
  local output="$2"
  if curl -fL --connect-timeout 20 --retry 2 -o "$output" "$url"; then
    return
  fi
  local proxy_url
  proxy_url="$(url_with_proxy "$url")"
  [ "$proxy_url" != "$url" ] || return 1
  curl -fL --connect-timeout 20 --retry 2 -o "$output" "$proxy_url"
}

install_mosdns_core() {
  local asset version_url tmp_zip tmp_extract
  asset="$(detect_arch_asset)"
  tmp_zip="$(mktemp)"
  tmp_extract="$(mktemp -d)"

  if [ "$MOSDNS_VERSION" = "latest" ]; then
    version_url="https://github.com/IrineSistiana/mosdns/releases/latest/download/$asset"
  else
    version_url="https://github.com/IrineSistiana/mosdns/releases/download/$MOSDNS_VERSION/$asset"
  fi

  yellow "下载 mosdns 内核：$asset"
  download_file "$version_url" "$tmp_zip" || die "mosdns 内核下载失败。"
  unzip -q "$tmp_zip" -d "$tmp_extract"
  local bin
  bin="$(find "$tmp_extract" -type f -name 'mosdns*' | head -n 1)"
  [ -n "$bin" ] || die "压缩包中没有找到 mosdns 二进制。"
  install -m 0755 "$bin" /usr/local/bin/mosdns
  rm -rf "$tmp_zip" "$tmp_extract"
}

copy_payload() {
  local root="$1"
  local payload="$root/remote-root"
  [ -d "$payload" ] || die "安装包缺少 remote-root。"

  mkdir -p "$INSTALL_DIR" "$INSTALL_DIR/rules" "$INSTALL_DIR/templates" "$MANAGER_DIR" /etc/systemd/system /etc/sysctl.d /usr/local/bin

  install -m 0755 "$payload/usr/local/bin/mosctl" /usr/local/bin/mosctl
  cp -a "$payload/etc/mosdns/manager/." "$MANAGER_DIR/"
  rm -rf "$MANAGER_DIR/__pycache__"
  find "$MANAGER_DIR" -type d -name __pycache__ -prune -exec rm -rf {} +

  cp -a "$payload/etc/mosdns/templates/." "$INSTALL_DIR/templates/"
  if [ -f "$payload/etc/mosdns/templates/default.yaml" ]; then
    if [ -f "$INSTALL_DIR/config.yaml" ]; then
      # 重装不覆盖已有配置；留一份快照到面板的备份目录（config.<stamp>.bak 会被面板列出并按保留数清理）
      mkdir -p "$INSTALL_DIR/backup"
      cp -a "$INSTALL_DIR/config.yaml" "$INSTALL_DIR/backup/config.$(date +%Y%m%d%H%M%S).bak"
      yellow "已存在 $INSTALL_DIR/config.yaml，保留不覆盖；如需内置模板可在面板「高级配置」里点击「恢复默认」。"
    else
      cp "$payload/etc/mosdns/templates/default.yaml" "$INSTALL_DIR/config.yaml"
    fi
  fi

  if [ -f "$payload/etc/logrotate.d/mosdns" ]; then
    mkdir -p /etc/logrotate.d
    install -m 0644 "$payload/etc/logrotate.d/mosdns" /etc/logrotate.d/mosdns
  fi
  if [ -d "$payload/etc/mosdns/rules" ]; then
    cp -a "$payload/etc/mosdns/rules/." "$INSTALL_DIR/rules/"
    rm -f "$INSTALL_DIR"/rules/*.save "$INSTALL_DIR"/rules/*.save.*
  fi
  touch "$INSTALL_DIR/rules/force-cn.txt" "$INSTALL_DIR/rules/force-nocn.txt" "$INSTALL_DIR/rules/hosts.txt"

  install -m 0644 "$payload/etc/systemd/system/mosdns.service" /etc/systemd/system/mosdns.service
  install -m 0644 "$payload/etc/systemd/system/mosdns-rescue.service" /etc/systemd/system/mosdns-rescue.service
  install -m 0644 "$payload/etc/systemd/system/mosdns-web.service" /etc/systemd/system/mosdns-web.service
  install -m 0644 "$payload/etc/sysctl.d/99-mosdns.conf" /etc/sysctl.d/99-mosdns.conf
}

rand_secret() {
  if has_cmd openssl; then
    openssl rand -base64 24 | tr -d '\n' | tr '/+' '_-'
  else
    python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(24), end="")
PY
  fi
}

write_env_file() {
  local user pass session_secret sync_token
  user="${WEB_USER:-admin}"
  pass="${WEB_SECRET:-$(rand_secret)}"
  session_secret="$(rand_secret)$(rand_secret)"
  sync_token="$(rand_secret)"

  if [ -f "$INSTALL_DIR/.env" ] && [ "${MOSCTL_KEEP_ENV:-1}" = "1" ]; then
    yellow "保留已有 $INSTALL_DIR/.env"
    # 旧版 .env 没有 GH_PROXY：补上默认值，面板和 mosctl 的下载回退都从这里读代理前缀
    if ! grep -q '^GH_PROXY=' "$INSTALL_DIR/.env"; then
      [ -z "$(tail -c1 "$INSTALL_DIR/.env")" ] || printf '\n' >> "$INSTALL_DIR/.env"
      printf 'GH_PROXY="%s"\n' "$GH_PROXY" >> "$INSTALL_DIR/.env"
    fi
    return
  fi

  cat > "$INSTALL_DIR/.env" <<EOF
WEB_SESSION_SECRET="$session_secret"
WEB_USER="$user"
WEB_SECRET="$pass"
WEB_PORT="$WEB_PORT"
MOSCTL_REPO_URL="$REPO_URL"
MOSCTL_BRANCH="$BRANCH"
GH_PROXY="$GH_PROXY"
RULE_SYNC_TOKEN="$sync_token"
RULE_SYNC_ENABLED="false"
RULE_SYNC_PEERS=""
BACKUP_KEEP_COUNT="20"
AUTO_UPDATE_ENABLED="true"
AUTO_UPDATE_TIME="04:10"
AUTO_UPDATE_CORE_MIN_AGE_DAYS="3"
AUTO_UPDATE_PANEL_MIN_AGE_DAYS="0"
EOF
  chmod 600 "$INSTALL_DIR/.env"
}

enable_services() {
  sysctl --system >/dev/null 2>&1 || true
  systemctl daemon-reload
  systemctl enable mosdns >/dev/null 2>&1 || true
  systemctl enable mosdns-web >/dev/null 2>&1 || true
  /usr/local/bin/mosctl update || yellow "Geo 数据更新失败，已跳过；可进面板维护页重试。"
  systemctl restart mosdns
  systemctl restart mosdns-web
}

env_value() {
  # set -Eeuo pipefail 下 grep 找不到会让整个管道失败，这里显式吞掉，缺键时返回空
  grep "^$1=" "$INSTALL_DIR/.env" 2>/dev/null | tail -n 1 | cut -d= -f2- | tr -d '"' || true
}

print_summary() {
  local ip user pass
  # 有些系统没有 hostname -I（或 hostname 不支持 -I），安装已经成功，这里不能因此退出
  ip="$( (hostname -I 2>/dev/null || true) | awk '{print $1}')"
  [ -n "$ip" ] || ip="服务器IP"
  user="$(env_value WEB_USER)"
  pass="$(env_value WEB_SECRET)"
  [ -n "$user" ] || user="admin"
  [ -n "$pass" ] || pass="(已有 .env 中的密码未改动)"
  green "$PROJECT_NAME 安装完成"
  printf '\n'
  printf 'Web 面板: http://%s:%s/\n' "$ip" "$WEB_PORT"
  printf '用户名: %s\n' "$user"
  printf '密码: %s\n' "$pass"
  printf '\n'
  printf '命令行管理: mosctl\n'
}

main() {
  require_root
  yellow "安装依赖..."
  install_packages
  source_root
  local root="$SOURCE_ROOT"
  yellow "安装 mosdns 内核..."
  install_mosdns_core
  yellow "安装 Mosctl 面板和配置..."
  copy_payload "$root"
  write_env_file
  yellow "启动服务..."
  enable_services
  print_summary
}

main "$@"
