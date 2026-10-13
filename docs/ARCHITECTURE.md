# 架构

本文描述仓库当前代码的进程划分、DNS 数据路径和面板模块。安装、命令和排障步骤见 [README.zh-CN.md](../README.zh-CN.md) 与 [MAINTENANCE.md](MAINTENANCE.md)。

## 仓库和运行时的关系

`remote-root/` 的目录结构就是安装后的绝对路径：`remote-root/usr/local/bin/mosctl` 对应 `/usr/local/bin/mosctl`，`remote-root/etc/mosdns/manager/app.py` 对应 `/etc/mosdns/manager/app.py`。

`/usr/local/bin/mosdns` 不在仓库里。`install.sh` 和面板的内核升级从 `https://github.com/IrineSistiana/mosdns/releases` 下载 zip。`/etc/mosdns/config.yaml`、`.env`、`backup/`、`cache.dump` 和日志是机器本地状态。

面板版本只有一处：`app.py` 顶部的 `PANEL_VERSION`。`mosctl version` 和菜单标题用 grep 读这个常量，避免脚本和面板各写一个版本号。

## 进程

```text
客户端 :53
    │
    ▼
mosdns.service ── /usr/local/bin/mosdns start -d /etc/mosdns
    │  读 config.yaml、rules/、cache.dump
    │  写 /var/log/mosdns.log
    │  本机 API 127.0.0.1:8080（模板默认）
    │
    │  60 秒内启动失败 5 次
    ▼
mosdns-rescue.service（oneshot）
    └── mosctl rescue enable
            iptables 把 TCP/UDP 53 DNAT 到 223.5.5.5
            临时 net.ipv4.ip_forward=1

mosdns 启动成功约 2 秒后
    └── ExecStartPost：mosctl rescue disable silent

浏览器 :7840（WEB_PORT）
    └── mosdns-web.service
            python3 /etc/mosdns/manager/app.py
            启动时：确认未完成的面板自动更新、按 .env 写自动更新 crontab

cron
    ├── mosctl update >> /var/log/mosctl-update.log
    └── python3 .../auto_update.py >> /var/log/mosctl-auto-update.log  # MOSCTL_AUTO_UPDATE
```

`mosdns-web.service` 不使用 systemd 的 `EnvironmentFile`。`app.py` 自己读 `/etc/mosdns/.env`，避免把密码传给它拉起的子进程。工作目录是 `/etc/mosdns/manager`。

`app.py` 在 `if __name__ == "__main__"` 里才调用 `panel_startup_tasks()` 并 `app.run`。`auto_update.py`、`notify_cli.py` 和单元测试都是 import 这个模块，不会启动 Flask，也不会改 crontab。

## DNS 解析顺序

现行模板是 `remote-root/etc/mosdns/templates/default.yaml`，文件头 `# mosctl-template: 4`。`main_sequence` 按这个顺序执行，命中应答就 `accept`：

1. `hosts`：`/etc/mosdns/rules/hosts.txt`。默认精确匹配写出的域名；`domain:` 前缀连同子域名。
2. 拒绝 qtype 65（HTTPS/SVCB）：`reject 0`，回空的 NOERROR，避免客户端用记录里的地址绕过 fake-ip。
3. `force-cn.txt` 命中则走国内上游 `forward_local`。
4. `force-nocn.txt` 命中则走国外上游。国外路径先去掉 ECS，再 `prefer_ipv4`。
5. `geosite_cn.txt` 命中则先查缓存，没有应答再走国内上游。
6. `geosite_no_cn.txt` 命中则走国外上游。
7. 其余查询走国外上游。

国内上游插件 `forward_local` 的 `concurrent` 为 2：主上游和备用上游同时查，取先成功的结果。模板默认值：

| 标记 | 模板默认 |
| --- | --- |
| `TAG_LOCAL` | `udp://119.29.29.29` |
| `TAG_LOCAL_BACKUP` | `udp://223.5.5.5` |
| `TAG_REMOTE` | `8.8.8.8:53` |
| `lazy_cache_ttl` | `86400` |
| 缓存 | `size: 20480`，每 600 秒写 `/etc/mosdns/cache.dump` |
| API | `127.0.0.1:8080` |
| 监听 | UDP 和 TCP `:53` |

面板和 `mosctl` 改上游时只替换带对应行尾注释的那一行。`TAG_LOCAL` 锚定到行尾，不会改到 `TAG_LOCAL_BACKUP`。国内地址没有 `://` 时补 `udp://`；国外地址不补协议。地址里不允许双引号、反斜杠和 `#`。

「恢复默认」和「迁移到当前模板」用内置 `templates/default.yaml` 重建 `config.yaml`，把当前能解析出来的上游和 TTL 写回去。旧配置没有备用行时保留模板里的备用上游。写入前用沙盒启动；失败则正式配置不动。

`mosctl sync` 做同样的保留，但模板来自 GitHub raw 文件 `remote-root/etc/mosdns/templates/default.yaml`，不是本机那份。

## Geo 列表

`mosctl update`（面板「立即更新」也是这条命令）下载：

| 本机文件 | 来源 | 何时下载 |
| --- | --- | --- |
| `/etc/mosdns/rules/geosite_cn.txt` | `https://raw.githubusercontent.com/Loyalsoldier/v2ray-rules-dat/release/direct-list.txt` | 每次 |
| `/etc/mosdns/rules/geosite_no_cn.txt` | `https://raw.githubusercontent.com/Loyalsoldier/v2ray-rules-dat/release/proxy-list.txt` | 每次 |
| `geoip_cn.txt` | `https://raw.githubusercontent.com/Loyalsoldier/geoip/release/text/cn.txt` | 仅当 `config.yaml` 引用该路径 |
| `geosite_apple.txt` | `https://raw.githubusercontent.com/Loyalsoldier/v2ray-rules-dat/release/apple-cn.txt` | 仅当配置引用 |

若配置不再引用 `geoip_cn.txt`、`geosite_apple.txt`、`user_iot.txt`、`local-ptr.txt`，更新时删除这些旧文件。配置引用了但磁盘上没有的 `/etc/mosdns/rules/<名字>` 会先 `touch` 空文件，避免 mosdns 因缺文件起不来。下载用临时文件，成功后 `mv` 到目标。全部成功后 `systemctl restart mosdns`。

直连失败时，`mosctl` 用 wget（20 秒超时、2 次尝试），面板用 Python 下载。代理前缀只拼到 `github.com`、`raw.githubusercontent.com`、`api.github.com`。

## 面板模块

`app.py` 是一个文件里的 Flask 应用。路由和职责：

| 路由 | 作用 |
| --- | --- |
| `GET/POST /login`、`POST /logout` | 表单登录。用户名和密码与 `.env` 做 `compare_digest` |
| `GET /` | 渲染 `index.html`，把 `PANEL_VERSION` 写进页面 |
| `GET /api/site-info` | 不需要登录。只返回 `SITE_NAME`，并带 `Access-Control-Allow-Origin: *` |
| `GET /api/status` | 运行状态、救援模式、版本、模板版本、上游、健康摘要、mosdns 指标 |
| `GET /api/core-version` | 查 mosdns `releases/latest` |
| `GET /api/panel-upgrade-source` | 远端面板版本。`refresh=true` 跳过一小时缓存 |
| `POST /api/control` | `start` `stop` `restart` `update` `test` `flush` `rescue_on` `rescue_off` `restore_default` `upgrade_core` `upgrade_panel` |
| `GET/POST /api/settings` | 解析策略里的上游和 TTL |
| `GET/POST /api/account-settings` | 用户名和密码。改密码必须提交当前密码 |
| `GET/POST /api/config` | 整份 `config.yaml`。保存前沙盒启动 |
| `POST /api/config/migrate` | 用内置模板重建配置 |
| `GET/POST /api/backups`、`/api/backup-settings`、`POST /api/backups/cleanup` | 列出、恢复、保留数量、清理 |
| `GET/POST /api/rule-sync-settings`、`POST /api/rule-sync-test` | 同步开关、密钥、节点、连通性 |
| `POST /api/rule-sync` | 节点间收规则。不走登录，用同步密钥。空 `rules` 对象是连通性探测 |
| `GET/POST /api/rules/<id>` | `force-cn`、`force-nocn`、`hosts` |
| `GET /api/rule-sync-jobs/<id>` | 后台推送进度，`latest` 表示最近一次 |
| `GET/POST /api/geo-schedule` | 频率 `daily` `weekly` `every_6h` `every_12h` `disabled` |
| `GET/POST /api/auto-update`、`POST /api/auto-update/check`、`POST /api/auto-update/run` | 设置、`--dry-run`、后台 `--manual` |
| `GET/POST /api/notify-settings`、`POST /api/notify-test`、`GET /api/notify-log` | 通知。响应里只有 Webhook 主机名 |
| `GET /api/logs` | `tail` `/var/log/mosdns.log`，可按 `lines` 和 `order=desc` |

`auto_update.py` 只编排：读设置、选内核版本、选面板提交、调用 `app.py` 里的安装函数、写状态和日志。`notify_cli.py` 把 shell 的 Geo 结果转成 `notify_failure` / `notify_recovery`，`--key` 目前只接受 `geo`。

前端没有构建步骤。`index.html` 用 `data-view` 切换五个视图，请求都带 `X-Requested-With: XMLHttpRequest`。页面上的 `mosctl-version` 和 `/api/status` 的 `panel_version` 不一致时，说明这个标签页还是升级前的脚本。

## 写入和回滚

会改正式文件并重启 mosdns 的操作先做沙盒：把 `cache.dump` 指到临时目录，用指定二进制（默认当前内核）执行 `mosdns start`，几秒内进程还在且没有明显的配置错误才算通过。

通过后：

1. 把旧文件复制到 `/etc/mosdns/backup/`。配置是 `config.<14位时间>.bak`，规则是 `rule-<id>.<时间>.bak`，内核是 `mosdns-bin.<时间>`（内核备份文件名不带 `.bak`）。
2. 写入新文件并重启。
3. `restart_mosdns` 在最多 5 秒内确认服务为 active，且 `www.baidu.com` 在 `127.0.0.1:53` 有应答。
4. 失败则把备份拷回去再重启，并在通知开启时发送回滚事件。

备份列表按这三类分开。`BACKUP_KEEP_COUNT` 只限制配置备份。规则备份按每个规则 id 保留同样的个数。内核备份保留 3 个。旧文件名 `config.yaml.<时间>.bak`、`<file>.txt.<时间>.bak`、`mosdns-bin.<时间>.bak` 仍能被认出来；启动清理时会把已知的旧规则备份改名为 `rule-<id>.<时间>.bak`。

面板升级的备份是目录 `/etc/mosdns/backup/mosctl-panel.<时间>/`，里面有 `manifest.json`，保留最近 3 份。它覆盖的目标：

| 目标 | 仓库内相对 `remote-root/` 的路径 |
| --- | --- |
| `/etc/mosdns/manager/` | `etc/mosdns/manager` |
| `/usr/local/bin/mosctl` | `usr/local/bin/mosctl` |
| `/etc/mosdns/templates/default.yaml` | `etc/mosdns/templates/default.yaml` |
| `/etc/systemd/system/mosdns.service` | `etc/systemd/system/mosdns.service` |
| `/etc/systemd/system/mosdns-rescue.service` | `etc/systemd/system/mosdns-rescue.service` |
| `/etc/systemd/system/mosdns-web.service` | `etc/systemd/system/mosdns-web.service` |
| `/etc/sysctl.d/99-mosdns.conf` | `etc/sysctl.d/99-mosdns.conf` |
| `/etc/logrotate.d/mosdns` | `etc/logrotate.d/mosdns` |

不覆盖 `.env`、`config.yaml`、`rules/`、日志、缓存和已有备份。升级后 `systemctl daemon-reload`。Web 进程的重启放在独立的 `sleep 1; systemctl restart mosdns-web` 里，好让当前 HTTP 响应先返回。

自动更新里的「立即更新」优先用 `systemd-run` 把 `auto_update.py --manual` 放进临时 unit（`KillMode=process`）。否则面板升级重启 `mosdns-web` 时，留在同一个 cgroup 里的更新进程会被一起杀掉。

## 规则同步协议

开启同步后，保存 `force-cn` 或 `force-nocn` 会 POST：

```http
POST /api/rule-sync
Content-Type: application/json
X-Mosdns-Sync-Token: <RULE_SYNC_TOKEN>

{"token":"<同一密钥>","rules":{"force-cn":"<文件全文>"},"source":"http://本机面板"}
```

对端用 `compare_digest` 核对密钥，忽略不在 `force-cn` / `force-nocn` 里的键。内容与本地等价时不写文件、不重启。有变化时走上面的沙盒和回滚。应用失败或从失败恢复时发通知，状态键是 `rule-sync`。

连通性测试发送 `"rules": {}`。对端返回「接口可访问，密钥已通过」，不记失败。

## 通知

`post_notification` 向 `NOTIFY_API_URL` POST `{"title", "content"}`，超时 15 秒，不走 `GH_PROXY`。标题形如 `✅ 站点名 · 事件`。正文最多 4 行、每行最多 80 个字符，末尾加 `📅` 和东八区时间。

持续失败用 `/etc/mosdns/notify_state.json` 去重：状态没变不发，仍失败时距离上次至少 3 天再提醒。Geo 的去重键是 `geo`，由 `notify_cli.py` 写入。

## 登录和接口防护

- 密码不进日志。通知日志只记录 Webhook 的主机名。
- 登录失败计数在内存中，按 `request.remote_addr`，超过一小时未失败的条目会被清掉。进程重启后计数消失。
- 会话 Cookie：HttpOnly、`SameSite=Lax`。代码没有设置 `Secure`。面板默认是 HTTP。
- `/api/` 的 POST 除 `/api/rule-sync` 外，没有 `X-Requested-With: XMLHttpRequest` 就返回 403。这是用来挡住浏览器跨站表单，不是通用鉴权。
- `.env` 的值拒绝 `"`、`\`、`$`、反引号和换行，避免面板写入的内容和 `mosctl` 的 grep 取值得不一致。
- 请求体上限 1 MB。直接编辑的 `config.yaml` 文本上限 200000 字符。

## 日志轮转

`/etc/logrotate.d/mosdns` 每天轮转下面四个文件，保留 7 份，`copytruncate`：

- `/var/log/mosdns.log`（mosdns 不会在 SIGHUP 时重新打开文件）
- `/var/log/mosctl-update.log`
- `/var/log/mosctl-auto-update.log`
- `/var/log/mosctl-notify.log`
