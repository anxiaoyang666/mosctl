# Mosctl

[English](README.md) | [中文](README.zh-CN.md)

Mosctl 是一个 mosdns 安装、配置和 Web 管理面板工具。它把 [mosdns](https://github.com/IrineSistiana/mosdns) 内核、一份分流配置模板、命令行菜单和 Flask Web 面板装到同一台 Linux 机器上，用来做国内 / 国外 DNS 分流。

仓库里的 `remote-root/` 是安装到目标机上的文件树。安装脚本和面板升级都只覆盖这份树里的程序与模板，不改本机的 `.env`、`config.yaml` 和规则文件。更细的模块边界见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)，日常操作见 [docs/MAINTENANCE.md](docs/MAINTENANCE.md)。

## 功能

- 一键安装 mosdns 内核、systemd 服务和 Web 面板，并打印访问地址、用户名和密码。
- Web 面板五个页面：态势总览、解析策略、运行维护、高级配置、系统控制。
- 维护国内主上游、备用国内上游、国外上游和缓存 TTL。配置里用行尾注释 `TAG_LOCAL`、`TAG_LOCAL_BACKUP`、`TAG_REMOTE` 标记这三项。
- 编辑三类本机规则：强制国内（`force-cn.txt`）、强制国外（`force-nocn.txt`）、自定义 Hosts（`hosts.txt`）。保存前会做格式检查，并用 mosdns 沙盒试启动，失败则不写入。
- 从 Loyalsoldier 规则列表更新 `geosite_cn.txt`、`geosite_no_cn.txt`，可手动执行，也可写入 crontab。
- 把强制国内 / 强制国外规则推送到其他 mosctl 或 mihomo 面板的 `/api/rule-sync`。
- 升级 mosdns 内核和 Mosctl 面板。内核替换前用新二进制在沙盒里跑当前配置，替换后做健康检查，不通过则回滚。
- 每天按 crontab 自动更新内核和面板（可用 `--dry-run`、`--only`、`--manual`）。
- Webhook 通知：自动更新成功、失败或回滚；Geo 更新失败与恢复；收到的同步规则未能应用与恢复；解析策略或配置改动导致 mosdns 重启失败并回滚。
- 配置、规则、内核的备份与按数量清理；面板文件升级前另有目录备份。
- 救援模式：mosdns 连续启动失败时，把 53 端口的 TCP/UDP 临时转到 `223.5.5.5`。
- 命令行菜单 `mosctl`，以及 `rescue`、`sync`、`update`、`flush`、`cache-ttl`、`test`、`version` 子命令。

## 技术栈

| 部分 | 实现 |
| --- | --- |
| DNS 内核 | 上游 [IrineSistiana/mosdns](https://github.com/IrineSistiana/mosdns) 的 Linux 发行包，安装到 `/usr/local/bin/mosdns`。本仓库不编译内核。 |
| Web 面板 | Python 3 标准库 + 发行版软件包 `python3-flask`。入口是 `remote-root/etc/mosdns/manager/app.py`，版本号常量 `PANEL_VERSION`（当前仓库为 `0.3.50`）。 |
| 命令行 | Bash：`install.sh` 与 `remote-root/usr/local/bin/mosctl`。 |
| 服务 | systemd：`mosdns.service`、`mosdns-web.service`、`mosdns-rescue.service`。 |
| 定时任务 | 当前用户的 crontab（安装时是 root）。Geo 更新和自动更新各占一行。 |
| 前端 | 单页 `templates/index.html`，无单独的前端构建。 |

仓库没有 `requirements.txt`、`pyproject.toml`、Dockerfile 或 GitHub Actions。面板依赖由 `install.sh` 用系统包管理器安装，Flask 版本随发行版走，仓库不锁版本。

## 一键安装

推荐在 Debian/Ubuntu LXC、虚拟机或服务器中使用 `root` 执行。`install.sh` 也接受 `dnf` 或 `yum` 的系统；找不到 `apt-get`、`dnf`、`yum` 三者时会退出。必须以 root 运行。

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/anxiaoyang666/mosctl/main/install.sh)"
```

国内网络可以使用加速地址：

```bash
bash -c "$(curl -fsSL https://gh-proxy.com/https://raw.githubusercontent.com/anxiaoyang666/mosctl/main/install.sh)"
```

安装脚本会：

1. 安装 `ca-certificates`、`curl`、`wget`、`unzip`、`tar`、`git`、`python3`、`python3-flask`、`iptables`、`nano`，以及 cron（Debian 系是 `cron` 和 `dnsutils`，RHEL 系是 `cronie` 和 `bind-utils`）。
2. 若当前目录旁没有 `remote-root/`，则 `git clone --depth 1` 指定仓库和分支；直连失败后再给仓库地址加上 `GH_PROXY`。
3. 按 `uname -m` 下载 mosdns zip：`x86_64`/`amd64`、`aarch64`/`arm64`、`armv7*`、`armv6*`、`armv5*`。其他架构会退出。
4. 把 `remote-root/` 拷到 `/etc/mosdns`、`/usr/local/bin/mosctl` 和 systemd 单元。已有 `/etc/mosdns/config.yaml` 时保留，并复制一份到 `/etc/mosdns/backup/config.<时间戳>.bak`。
5. 写入 `/etc/mosdns/.env`（权限 `600`）。文件已存在且 `MOSCTL_KEEP_ENV` 不是 `0` 时保留原文件；若其中没有 `GH_PROXY=`，则追加当前的 `GH_PROXY`。
6. `systemctl enable` 并重启 `mosdns`、`mosdns-web`，然后执行一次 `/usr/local/bin/mosctl update`。Geo 更新失败只打印警告，不中断安装。

安装完成后，终端会打印 Web 面板地址、用户名和随机生成的密码。默认 Web 端口是 `7840`，默认用户名是 `admin`。打不开 `hostname -I` 时，地址里的主机名会显示为「服务器IP」。

支持的 CPU 架构与安装包文件名：

| `uname -m` | 安装包 |
| --- | --- |
| `x86_64`、`amd64` | `mosdns-linux-amd64.zip` |
| `aarch64`、`arm64` | `mosdns-linux-arm64.zip` |
| `armv7*` | `mosdns-linux-arm-7.zip` |
| `armv6*` | `mosdns-linux-arm-6.zip` |
| `armv5*` | `mosdns-linux-arm-5.zip` |

## 自定义安装参数

这些变量只在执行 `install.sh` 时读取，除下面表格注明的以外，会写进 `/etc/mosdns/.env`。

```bash
WEB_PORT=7840 WEB_USER=admin WEB_SECRET='your-password' bash -c "$(curl -fsSL https://raw.githubusercontent.com/anxiaoyang666/mosctl/main/install.sh)"
```

| 变量 | 默认 | 作用 |
| --- | --- | --- |
| `MOSCTL_REPO_URL` | `https://github.com/anxiaoyang666/mosctl.git` | 没有本地 `remote-root/` 时克隆的仓库，并写入 `.env` |
| `MOSCTL_BRANCH` | `main` | 克隆和以后面板升级、`mosctl sync` 使用的分支 |
| `WEB_PORT` | `7840` | 面板监听端口，写入 `.env` 的 `WEB_PORT` |
| `WEB_USER` | `admin` | 面板用户名 |
| `WEB_SECRET` | 随机生成 | 面板密码。不填写则随机生成 |
| `MOSDNS_VERSION` | `latest` | `latest` 下载 GitHub Releases 的 latest 包；其他值当作 tag，例如 `v5.3.3`。只在安装时使用，不写入 `.env` |
| `GH_PROXY` | `https://gh-proxy.com/` | GitHub 下载失败后拼接在原 URL 前面的前缀 |
| `MOSCTL_KEEP_ENV` | `1` | 为 `1` 时保留已有 `.env`。设为 `0` 会按本次变量重写 `.env`，原密码和同步密钥会被替换 |

在已经克隆好的仓库里，以 root 执行 `bash install.sh` 时，脚本发现旁边有 `remote-root/` 就不再克隆。

## 本地启动与测试

面板和 `mosctl` 的路径写死在 `/etc/mosdns`、`/usr/local/bin/mosdns` 和 systemd 上。在开发机上执行 `python3 remote-root/etc/mosdns/manager/app.py` 不会变成一套可用的 DNS 面板：导入时就会按这些绝对路径准备 `.env`，监听端口也来自那份 `.env`。

在目标 Linux 上的启动方式就是上一节的安装。装好之后：

```bash
systemctl status mosdns
systemctl status mosdns-web
```

面板进程是 `python3 /etc/mosdns/manager/app.py`，监听 `0.0.0.0` 和 `.env` 里的 `WEB_PORT`。浏览器打开 `http://<服务器IP>:7840/`。

改仓库后、不安装服务也能跑的检查是单元测试。测试用桩替换 Flask，并把 `/etc/mosdns` 指到临时目录，不需要 root、mosdns 二进制或网络：

```bash
python3 -m unittest discover -s tests
```

仓库没有单独的 lint 或打包命令。

## 构建与部署

没有编译、镜像构建或 CI 发布流程。部署就是把 `main`（或 `.env` 里的 `MOSCTL_BRANCH`）上的 `remote-root/` 装到机器上。三条路径：

1. **新机器**：运行 `install.sh`。它下载 mosdns 发行包，并复制本仓库的 `remote-root/`。
2. **已安装的机器升级面板**：Web「运行维护 → 升级中心」或自动更新。面板按 `MOSCTL_REPO_URL` 和 `MOSCTL_BRANCH` 下载 GitHub 分支压缩包（自动更新在设置了等待天数时改为下载那一次提交）。压缩包里必须有 `remote-root/`，且新的 `app.py` 能通过 `py_compile`。版本号不高于当前 `PANEL_VERSION` 时不安装。替换前把面板文件备份到 `/etc/mosdns/backup/mosctl-panel.<时间戳>/`，失败则按 `manifest.json` 还原，成功后约 1 秒执行 `systemctl restart mosdns-web`。
3. **已安装的机器升级内核**：升级中心安装 GitHub Releases 的 latest；自动更新则在稳定版里挑选比当前新、发布满指定天数、且带本机安装包的最高版本。两条路径都不降级，都先沙盒启动再做最多 20 秒的健康检查。

面板升级会替换的路径见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。它不覆盖 `/etc/mosdns/.env`、`/etc/mosdns/config.yaml`、`/etc/mosdns/rules/`、日志、缓存和已有备份。

`mosctl sync`（菜单第 2 项）只更新配置模板，不更新面板代码。

## 配置文件（`/etc/mosdns/.env`）

安装脚本会生成 `/etc/mosdns/.env`（权限 `600`）。Web 面板会读取并更新它；`mosctl` 只从中读取 `MOSCTL_REPO_URL`、`MOSCTL_BRANCH` 和 `GH_PROXY`。面板用逐行 `key=value` 解析，不把文件交给 shell 执行。写入时拒绝引号、反斜杠、`$`、反引号、回车、换行和空字节，先写临时文件再替换，并保持权限 `600`。

键说明：

- `WEB_SESSION_SECRET`：会话签名密钥，安装时生成，修改面板密码时自动轮换
- `WEB_USER`：Web 登录用户名
- `WEB_SECRET`：Web 登录密码
- `WEB_PORT`：Web 面板端口，默认 `7840`。只在 `mosdns-web` 启动时读取，改完后执行 `systemctl restart mosdns-web`
- `MOSCTL_REPO_URL`：面板升级和 `mosctl sync` 使用的仓库地址。在线同步和升级只接受 `https://github.com/<owner>/<repo>`（可带 `.git`）
- `MOSCTL_BRANCH`：面板升级和 `mosctl sync` 使用的分支，默认 `main`
- `GH_PROXY`：GitHub 代理前缀，只在直连 GitHub 失败后才使用；设为空字符串表示不走代理。键不存在时面板和 `mosctl` 使用 `https://gh-proxy.com/`
- `RULE_SYNC_ENABLED`：`true` / `false`，把强制国内 / 强制国外规则的改动推送到其他面板。只有字符串 `true`（忽略大小写）算开启
- `RULE_SYNC_TOKEN`：`/api/rule-sync` 校验用的共享密钥。请求头 `X-Mosdns-Sync-Token` 优先，其次才是 JSON 里的 `token`
- `RULE_SYNC_PEERS`：其他 mosctl / mihomo 面板地址，用 `|` 分隔。面板保存时也接受换行或逗号，没有协议时补 `http://`
- `BACKUP_KEEP_COUNT`：配置备份保留数量（3–200），默认 `20`。内核备份固定保留 3 个，面板目录备份固定保留 3 份，都不受这个数控制
- `AUTO_UPDATE_ENABLED`：`true` / `false`，每天自动更新 mosdns 内核和面板。`false`、`0`、`no`、`off` 为关闭，缺省按开启处理，默认 `true`
- `AUTO_UPDATE_TIME`：自动更新时间（服务器本地时间，`HH:MM`），默认 `04:10`
- `AUTO_UPDATE_CORE_MIN_AGE_DAYS`：mosdns 稳定版发布满几天才自动更新（0–365），默认 `3`
- `AUTO_UPDATE_PANEL_MIN_AGE_DAYS`：面板分支上改动 `remote-root/` 的最新提交满几天才自动更新（0–365），默认 `0`
- `SITE_NAME`：通知标题里的站点名（最多 20 个字符），留空显示 `mosdns`。`/api/site-info` 会公开这个名字，供其他面板做站点切换，不含其他字段
- `NOTIFY_ENABLED`：`true` / `false`，是否发送通知，默认 `false`
- `NOTIFY_API_URL`：通知 Webhook 地址（`http://` 或 `https://`），以 JSON `{"title", "content"}` POST，直连不走代理；页面上只显示主机名

面板启动时如果缺少会话密钥、用户名、密码、端口或规则同步三项，会补上默认值。自动更新四项缺省时用上表的默认值，不必事先写在文件里。

## 自动更新

面板启动和保存设置时会在 crontab 写入一行带 `# MOSCTL_AUTO_UPDATE` 标记的任务，按 `AUTO_UPDATE_TIME` 运行 `python3 /etc/mosdns/manager/auto_update.py`（可加 `--dry-run`、`--only core|panel`，面板上的「立即更新」还会加 `--manual`）。`AUTO_UPDATE_ENABLED` 关闭时，定时运行直接退出；`--dry-run` 和 `--manual` 仍会执行。

每次先更新内核、最后更新面板：内核只装发布满指定天数的稳定版（排除 draft 和 prerelease）、绝不降级，替换前用新内核在沙盒里跑当前配置，替换后 20 秒内检查服务状态、版本以及 `www.baidu.com` / `www.google.com` 在 `127.0.0.1:53` 的解析，不通过就自动回滚。结果写入 `/etc/mosdns/auto_update_state.json` 和 `/var/log/mosctl-auto-update.log`，在「运行维护 → 自动更新」查看。同一时刻只能有一个自动更新进程，锁文件是 `/etc/mosdns/.auto_update.lock`。

## 通知

在「运行维护 → 通知」填写站点名和 Webhook 地址并开启后，以下事件会发送通知（与 mihomo 面板格式一致，标题为 `图标 站点名 · 事件`，正文末尾附服务器时间；这条时间固定用东八区，和 crontab 使用的服务器本地时区不是同一套）：自动更新的内核 / 面板已更新、失败或回滚；Geo 规则更新失败与恢复；收到的同步规则未能应用与恢复；解析策略、规则或 `config.yaml` 修改后 mosdns 重启失败并回滚。反复出现的失败只在状态变化时通知，仍未恢复时最多每 3 天提醒一次（状态在 `/etc/mosdns/notify_state.json`）。发送记录写入 `/var/log/mosctl-notify.log`（只记主机名）。

`mosctl update` 通过 `python3 /etc/mosdns/manager/notify_cli.py --key geo --event fail|ok ...` 上报 Geo 结果。通知失败不会改变 `mosctl` 的退出码。

## 规则同步

开启规则同步后，保存强制国内 / 强制国外规则时会推送到 `RULE_SYNC_PEERS` 里的每个地址。Hosts 不同步。推送在后台线程里进行，页面用返回的任务 id 轮询 `/api/rule-sync-jobs/<id>`。本机地址会被跳过。对端返回「操作进行中」或 HTTP 409 时，间隔 3 秒重试，最多 3 次。

节点地址默认是明文 `http://`，同步密钥通过请求头明文传输：同步密钥会以明文发送，建议仅在可信内网使用，或者给面板套上 HTTPS。面板里只要有节点不是 `https://`，也会显示同样的提示。

「测试连通」向对端 POST `{"rules": {}}`。对端密钥通过后返回「接口可访问，密钥已通过」，不会改规则，也不会当成同步失败去告警。

## 系统改动说明

- `net.ipv4.ip_forward` 不再被永久开启。`/etc/sysctl.d/99-mosdns.conf` 是空文件，用来在升级时清掉旧版本写入的永久设置。救援模式（`mosctl rescue enable`）会临时开启转发，并把开启前的值记到 `/etc/mosdns/.rescue-ip-forward`；`mosctl rescue disable` 会删掉 iptables 规则并恢复之前的值。
- `mosdns.service` 在 60 秒内启动失败达到 5 次后进入 failed，并触发 `mosdns-rescue.service`（一次性执行 `mosctl rescue enable`）。服务启动成功约 2 秒后，`ExecStartPost` 会在后台执行 `mosctl rescue disable silent`。
- `mosctl sync`（菜单第 2 项）会从配置的仓库和分支拉取 `remote-root/etc/mosdns/templates/default.yaml`，保留当前上游 DNS 和 TTL，把旧配置备份为 `/etc/mosdns/backup/config.<时间戳>.bak`，mosdns 启动失败时自动回滚。

## 目录结构

仓库：

```text
.
├── install.sh                 # root 安装脚本
├── README.md                  # 英文说明（测试要求默认 README 为英文）
├── README.zh-CN.md            # 本文件
├── docs/
│   ├── ARCHITECTURE.md
│   ├── MAINTENANCE.md
│   └── superpowers/specs/2026-05-28-panel-upgrade-design.md
├── tests/                     # unittest，多数用源码契约或桩加载 app.py
└── remote-root/               # 安装到目标机的文件树
    ├── usr/local/bin/mosctl
    └── etc/
        ├── logrotate.d/mosdns
        ├── sysctl.d/99-mosdns.conf
        ├── systemd/system/    # mosdns、mosdns-web、mosdns-rescue
        └── mosdns/
            ├── templates/default.yaml
            ├── rules/         # 空的 force-cn / force-nocn / hosts，以及随仓库附带的 geosite 列表
            └── manager/
                ├── app.py
                ├── auto_update.py
                ├── notify_cli.py
                ├── templates/ # index.html、login.html
                └── static/    # 图标
```

安装后的运行时路径（不在 git 里）：

| 路径 | 内容 |
| --- | --- |
| `/usr/local/bin/mosdns` | 从上游 release 下载的内核 |
| `/usr/local/bin/mosctl` | 命令行 |
| `/etc/mosdns/config.yaml` | 当前配置。首次安装从模板复制 |
| `/etc/mosdns/.env` | 面板和少量 CLI 配置，权限 `600` |
| `/etc/mosdns/rules/` | 本机规则和 Geo 列表 |
| `/etc/mosdns/cache.dump` | mosdns 缓存转储 |
| `/etc/mosdns/backup/` | 配置、规则、内核和面板备份 |
| `/etc/mosdns/templates/default.yaml` | 面板「恢复默认」和迁移使用的内置模板 |
| `/etc/mosdns/manager/` | 面板代码 |
| `/var/log/mosdns.log` | mosdns 自己的日志（模板里 `log.file`） |
| `/var/log/mosctl-update.log` | `mosctl update` 的输出 |
| `/var/log/mosctl-auto-update.log` | 自动更新日志 |
| `/var/log/mosctl-notify.log` | 通知发送记录 |

`remote-root/etc/mosdns/rules/geosite_cn.txt` 和 `geosite_no_cn.txt` 是仓库里的初始列表。安装结束时会跑 `mosctl update`，成功后这两份文件被 Loyalsoldier 的列表替换。`force-cn.txt`、`force-nocn.txt`、`hosts.txt` 在仓库里是空文件。

## Web 面板

登录页 `/login`。连续失败 5 次后，该来源 IP 锁定 60 秒（记在进程内存里，重启 `mosdns-web` 会清掉）。会话保留 30 天，Cookie 为 HttpOnly、`SameSite=Lax`。修改密码会轮换 `WEB_SESSION_SECRET`，其他浏览器上的会话失效。除 `/api/rule-sync` 外，写接口还要求请求头 `X-Requested-With: XMLHttpRequest`。会改配置或重启服务的 POST 共用一把进程内锁，拿不到锁时返回 HTTP 409，正文为「操作进行中，请稍后再试」。

| 页面 | 能做的事 |
| --- | --- |
| 态势总览 | 服务是否运行、是否开机自启、是否处于救援模式、内核版本、解析链路、日志事件 |
| 解析策略 | 国内 / 备用国内 / 国外上游、TTL、三类规则；配置模板版本落后时提供迁移 |
| 运行维护 | Geo 计划、规则同步、备份、内核与面板升级、自动更新、通知 |
| 高级配置 | 修改用户名和密码（需当前密码，新密码至少 6 位）、恢复内置默认模板、直接编辑 `config.yaml` |
| 系统控制 | 启动、停止、重启、清缓存、救援模式开关 |

直接保存 `config.yaml` 或规则前，面板会把缓存文件改到临时目录，用 mosdns 试启动。试启动失败则不替换正式文件。

## 常用命令

```bash
mosctl                              # 交互菜单
mosctl version                      # 读 app.py 里的 PANEL_VERSION
mosctl sync                         # 拉取仓库默认模板，保留上游和 TTL
mosctl update                       # 更新 Geo 列表并重启 mosdns
mosctl flush                        # 优先调 mosdns API 清缓存，失败再删 cache.dump 并重启
mosctl cache-ttl 3600               # 改 lazy_cache_ttl 并重启
mosctl test                         # 对本机 127.0.0.1 做 nslookup
mosctl rescue enable
mosctl rescue disable

systemctl status mosdns
systemctl status mosdns-web
systemctl restart mosdns-web        # 改 WEB_PORT 或手工换面板文件后

python3 /etc/mosdns/manager/auto_update.py --dry-run
python3 /etc/mosdns/manager/auto_update.py --only core
python3 /etc/mosdns/manager/auto_update.py --manual
```

`mosctl` 没有子命令时进入菜单：服务管理、同步配置、DNS 参数、自定义规则、更新 Geo、救援模式、跟踪 `/var/log/mosdns.log`、解析测试、卸载。卸载会停掉并禁用三个 systemd 单元，删除 `/etc/mosdns`、两个二进制、四份日志和相关 crontab 行。`/etc/resolv.conf` 是符号链接时不改；它是普通文件且 `nameserver` 指向 `127.0.0.1` 时改成 `223.5.5.5`。

## 常见问题

**安装立刻退出，提示请使用 root。** `install.sh` 检查 `id -u`。用 root 或 root 的非交互会话执行。

**提示未找到 apt-get/dnf/yum。** 脚本不会用其他包管理器。需要先自行装好 python3、Flask、git、curl、wget、unzip 和 cron，或换到它支持的系统后再运行。

**提示暂不支持当前架构。** `uname -m` 不在上一节的五类之中。面板升级内核时也会因为没有对应 zip 而取消。

**GitHub 下载失败。** 安装、`mosctl` 和面板都是先直连，失败后再用 `GH_PROXY`。把 `GH_PROXY` 设成空字符串会禁止代理。`mosctl sync` 只接受 GitHub 仓库的 raw 地址。

**重装后密码和 config.yaml 没变。** 这是默认行为：已有 `.env` 在 `MOSCTL_KEEP_ENV=1` 时保留；已有 `config.yaml` 只备份、不覆盖。要重写 `.env` 时执行 `MOSCTL_KEEP_ENV=0`，同时会换掉同步密钥和会话密钥。

**改了端口但页面仍是旧端口。** `WEB_PORT` 只在进程启动时读取。改完 `.env` 后 `systemctl restart mosdns-web`。

**登录提示失败次数过多。** 同一 IP 5 次失败锁定 60 秒。等 60 秒，或重启 `mosdns-web` 清掉内存里的计数。

**保存时提示操作进行中。** 另一个会重启 mosdns 或升级的请求还在跑。等它结束再试。自动更新运行时，手动升级内核或面板会被拒绝。

**Geo 计划保存失败，提示没有 crontab。** 安装 `cron`（Debian/Ubuntu）或 `cronie`（RHEL），并确认 `cron` 或 `crond` 处于 active。自动更新写 crontab 失败时也是这条提示。

**救援模式关不掉，或转发被打开。** `mosctl rescue disable` 按固定规则删除转到 `223.5.5.5` 的 DNAT/MASQUERADE，并恢复 `.rescue-ip-forward` 里的 `net.ipv4.ip_forward`。没有这个状态文件时，关闭救援不会改 ip_forward。

**面板升级说当前已是最新，或提示远端没有版本号。** 比较的是 `app.py` 里的 `PANEL_VERSION`。远端版本号不高于本机时不安装，避免降级。

**自动更新到点没跑。** 看 `AUTO_UPDATE_ENABLED`、crontab 里带 `# MOSCTL_AUTO_UPDATE` 的那一行，以及 cron 服务是否在跑。时间是服务器本地时区。内核还要满足「稳定版、比当前新、发布满 `AUTO_UPDATE_CORE_MIN_AGE_DAYS` 天」。

**通知时间和机器时区对不上。** 通知正文里的时间固定写成东八区。Geo 计划和自动更新的钟点按服务器本地时区。

## 延伸阅读

- [架构说明](docs/ARCHITECTURE.md)：进程、解析顺序、面板模块、升级覆盖范围
- [维护手册](docs/MAINTENANCE.md)：日志、备份、升级、同步、改仓库后的验证
- [面板在线升级设计摘记](docs/superpowers/specs/2026-05-28-panel-upgrade-design.md)：该功能的原始范围，现行行为以代码和上面两份文档为准
