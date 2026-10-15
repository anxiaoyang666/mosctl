# 维护手册

面向已经装好 Mosctl 的机器，以及要改这个仓库的人。命令和路径以当前仓库为准。安装步骤见 [README.zh-CN.md](../README.zh-CN.md)，模块边界见 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 日常检查

```bash
systemctl status mosdns mosdns-web
mosctl version
/usr/local/bin/mosdns version
mosctl test
```

`mosctl test` 对 `127.0.0.1` 做 `nslookup`，域名是 `www.baidu.com` 和 `www.google.com`。面板态势总览读的是 systemd 状态、救援模式 iptables 规则，以及 mosdns 在 `127.0.0.1:8080` 上的指标（模板里的 `api.http`）。API 不可达时指标为空，DNS 服务本身仍可能正常。

看定时任务：

```bash
crontab -l
systemctl is-active cron || systemctl is-active crond
```

应能看到两类行（Geo 计划关闭时没有第一类）：

- 含 `/usr/local/bin/mosctl update`，输出追加到 `/var/log/mosctl-update.log`。面板写入的上一行注释是 `# MosDNS Web: Geo update schedule`。
- 含 `python3 /etc/mosdns/manager/auto_update.py`，行尾注释 `# MOSCTL_AUTO_UPDATE`，输出追加到 `/var/log/mosctl-auto-update.log`。

## 日志

| 文件 | 谁写入 | 看什么 |
| --- | --- | --- |
| `/var/log/mosdns.log` | mosdns，路径来自 `config.yaml` 的 `log.file` | 启动失败、上游超时。面板「态势总览」和 `GET /api/logs` 读它 |
| `/var/log/mosctl-update.log` | `mosctl update` | 以 `===== <时间> 更新 Geo 规则 =====` 开头，以 `===== 结果: 成功 =====` 或 `===== 结果: 失败 =====` 结束。面板用这两行判断上次 Geo 更新 |
| `/var/log/mosctl-auto-update.log` | `auto_update.py`，以及 cron 的 stdout | 内核和面板各自的 `updated` / `up_to_date` / `skipped` / `failed` / `rolled_back` |
| `/var/log/mosctl-notify.log` | 面板 | `sent` 或 `failed`、Webhook 主机名、标题。没有完整 URL |

跟踪 mosdns 日志：`mosctl` 菜单第 7 项，即 `tail -n 50 -f /var/log/mosdns.log`。

logrotate 每天 `copytruncate`，保留 7 份。改轮转策略时改仓库里的 `remote-root/etc/logrotate.d/mosdns`，再通过安装或面板升级覆盖到 `/etc/logrotate.d/mosdns`。只改已安装机器上的那份，下次面板升级会被仓库版本盖掉。

## 改上游、TTL 和规则

优先用面板「解析策略」或 `mosctl` 菜单第 3、4 项。这两处会改带 TAG 的行并重启 mosdns。

命令行直接改 TTL：

```bash
mosctl cache-ttl 3600
```

规则文件：

| 面板 id | 路径 | 格式 |
| --- | --- | --- |
| `force-cn` | `/etc/mosdns/rules/force-cn.txt` | 每行一个域名，可加 `full:`、`keyword:`、`regexp:`。同步到 mihomo 时 `keyword:` 和 `regexp:` 不生效，`full:` 会连同子域名一起匹配 |
| `force-nocn` | `/etc/mosdns/rules/force-nocn.txt` | 同上 |
| `hosts` | `/etc/mosdns/rules/hosts.txt` | `域名 IP`，可写多个 IP。`domain:` 前缀才匹配子域名。不同步 |

`force-cn` 和 `force-nocn` 在去掉注释、空行、重复项和顺序，并忽略大小写（`regexp:` 除外）以及可有可无的 `domain:` 前缀之后，若与当前文件相同，则不写盘、不重启，但仍会按同步设置推送。`hosts` 不走这套比较。格式不合法或沙盒启动失败时，正式文件不变。

直接改 `/etc/mosdns/config.yaml` 后执行 `systemctl restart mosdns`。面板里的编辑器会先沙盒再重启，手改文件没有这一步。改坏到连续启动失败时，`mosdns-rescue.service` 会打开救援模式。

配置模板版本落后时，菜单会提示，面板「解析策略」出现迁移卡。迁移调用内置模板，保留上游和 TTL。也可以在「高级配置」点恢复默认，效果相同。

清缓存：

```bash
mosctl flush
```

配置里有 `api.http` 且存在 `type: cache` 插件时，请求 `GET http://<api>/plugins/<tag>/flush`，不重启。否则删除 `/etc/mosdns/cache.dump` 并重启。

## Geo 更新

立刻更新：

```bash
mosctl update
```

退出码 0 为成功，非 0 为失败。面板上的频率写进 crontab，可选每天、每周、每 6 小时、每 12 小时或关闭。时间是服务器本地时间。默认未设置时，`read_geo_schedule` 的展示时间是 `02:00`、星期一，但 crontab 里没有行，也就是关闭。安装脚本只在结束时跑一次 `mosctl update`，不写入周期任务。

失败时先看 `/var/log/mosctl-update.log` 里最后一个块。常见原因是 GitHub raw 下载失败，或下载成功但 `systemctl restart mosdns` 失败。下载失败不会用半截文件替换原列表。

每个地址先直连，再试 `GH_PROXY`，然后试 `GH_MIRRORS`（没写这个键时是 `https://gh-proxy.com/` 和 `https://ghfast.top/`），最后试 jsDelivr（`https://cdn.jsdelivr.net/gh/<owner>/<repo>@<ref>/<path>`）。同名环境变量优先于 `.env`。想只保留自己的代理前缀时，把 `GH_MIRRORS` 写成空字符串；jsDelivr 仍会作为 raw 文件的最后一跳。

下载全部失败后会等 `GEO_UPDATE_RETRY_DELAY` 秒（默认 180）再自动试一次，然后才发通知。测试或手工排查可以 `GEO_UPDATE_RETRY_DELAY=0 mosctl update`，立即重试、不必等几分钟。旧的 `geosite_cn.txt` / `geosite_no_cn.txt` 还在且非空时，通知是警告「规则未更新，继续使用旧规则」，mosdns 继续用磁盘上的旧列表；日志里的 `===== 结果: 失败 =====` 和退出码 1 不变。规则文件缺失，或重启失败，仍按严重故障发通知。面板上的「立即更新」最多等 1200 秒，用来盖住这次重试。

## 自动更新

查看而不安装：

```bash
python3 /etc/mosdns/manager/auto_update.py --dry-run
python3 /etc/mosdns/manager/auto_update.py --dry-run --only core
python3 /etc/mosdns/manager/auto_update.py --dry-run --only panel
```

只处理一项并真正安装：`--only core` 或 `--only panel`。面板按钮「立即更新」等价于 `--manual`，在 `AUTO_UPDATE_ENABLED=false` 时也会跑。

状态文件 `/etc/mosdns/auto_update_state.json` 里 `core` 和 `panel` 各有 `last_result`。面板升级会重启 Web，所以结果先写成 `started`，新进程启动时若 `PANEL_VERSION` 已经不低于目标，再改成 `updated` 并发通知。

手动升级和自动更新不要叠在一起。自动更新锁还在时，面板拒绝 `upgrade_core` 和 `upgrade_panel`。

内核回滚后的二进制在 `/etc/mosdns/backup/mosdns-bin.<时间>`。健康检查没过且回滚也失败时，日志会写明把该文件复制回 `/usr/local/bin/mosdns`。需要临时恢复解析时：

```bash
mosctl rescue enable
```

确认 mosdns 正常后：

```bash
mosctl rescue disable
```

## 备份

备份目录 `/etc/mosdns/backup/`。

- 配置：`config.<时间>.bak`，保留个数是 `.env` 的 `BACKUP_KEEP_COUNT`（3 到 200，默认 20）。
- 规则：`rule-force-cn.<时间>.bak` 这类名字，每个规则 id 按同一个保留数清理。
- 内核：`mosdns-bin.<时间>`，固定留 3 个。
- 面板：目录 `mosctl-panel.<时间>/`，固定留 3 个。

面板「清理旧备份」调用 `cleanup_old_backups()`，只删超出保留数的那部分。恢复配置或规则会再走沙盒和重启；恢复失败会把恢复前的文件拷回去。

## 升级面板和内核

升级中心：

- 内核：安装 GitHub `releases/latest` 里本机架构的 zip。不降级。沙盒和 20 秒健康检查与自动更新相同。
- 面板：下载 `.env` 里 `MOSCTL_REPO_URL` / `MOSCTL_BRANCH` 的分支压缩包。远端 `PANEL_VERSION` 必须更高。

改完仓库并推到该分支后，已安装的机器要等自动更新，或在升级中心点检测再升级。检测按钮请求 `GET /api/panel-upgrade-source?refresh=true`，不用一小时缓存。 |

只想更新配置模板、不换程序：

```bash
mosctl sync
```

`MOSCTL_REPO_URL` 为空，或不是 `https://github.com/<owner>/<repo>` 这种单层仓库地址时，sync 直接失败。

## 规则同步

在每台面板的「运行维护 → 规则同步」填写相同的 `RULE_SYNC_TOKEN`，并把其他面板的基址写进节点列表，然后打开开关。先点「测试连通」。密钥错误是 HTTP 403。

节点之间要能访问对方的 `WEB_PORT`。地址写成 `http://10.0.0.2:7840` 这种基址，不要带 `/api/rule-sync`，程序会自己拼。列表里指向自己的地址会显示「本机（跳过）」。

同步密钥出现在请求头里。节点不是 `https://` 时，页面固定提示：同步密钥会以明文发送，建议仅在可信内网使用。

## 通知

「运行维护 → 通知」保存站点名和 Webhook。留空 Webhook 再保存会沿用已保存的地址。页面和日志都不显示完整 URL。

用「发送测试通知」确认对端。正式事件见 README 的通知一节。关闭通知后，`notify_cli.py` 和 `notify_event` 直接返回，不影响 Geo 更新或配置保存的结果。

## 账号、端口和重装

改用户名或密码用面板「高级配置」。新密码至少 6 位。改密码会使其他浏览器的登录失效，当前窗口保持登录。

改端口：编辑 `/etc/mosdns/.env` 的 `WEB_PORT`，然后：

```bash
systemctl restart mosdns-web
```

重装同一台机器：再执行 `install.sh`。已有 `config.yaml` 会保留并多一份备份。已有 `.env` 默认保留；只有缺少 `GH_PROXY=` 时追加。要连密码一起重生成：

```bash
MOSCTL_KEEP_ENV=0 bash install.sh
```

这会换掉 `WEB_SECRET`、`WEB_SESSION_SECRET` 和 `RULE_SYNC_TOKEN`。

## 卸载

`mosctl` 菜单第 9 项，确认输入 `y`。它会：

- 停止并禁用 `mosdns`、`mosdns-rescue`、`mosdns-web`
- 删除三个 unit、`/etc/sysctl.d/99-mosdns.conf`、`/etc/logrotate.d/mosdns`，然后 `daemon-reload`
- 删除 `/etc/mosdns`、`/usr/local/bin/mosdns`、`/usr/local/bin/mosdns-web`（若存在）、`/usr/local/bin/mosctl`
- 删除四份日志
- 从 crontab 去掉含 `mosctl update`、`MOSCTL_AUTO_UPDATE`、`auto_update.py` 的行

不会删除 iptables 里由救援模式加上、且卸载前没有执行 `rescue disable` 的规则。卸载前先 `mosctl rescue disable`。

`/etc/resolv.conf`：符号链接不改。普通文件且某一行是 `nameserver 127.0.0.1` 时，整个文件被写成 `nameserver 223.5.5.5`。

## 在仓库里改代码

程序改动放在 `remote-root/` 下对应的安装路径，不要只改已经装到 `/etc/mosdns` 的副本。面板升级会用仓库里的 `remote-root/` 覆盖那份副本。

发一版面板时改 `app.py` 的 `PANEL_VERSION`。字符串要能被 `panel_version_tuple` 解析成 `主.次.修`，否则升级逻辑认为远端没有版本号并取消安装。`mosctl` 不用另改版本号。

改默认分流行为时改 `remote-root/etc/mosdns/templates/default.yaml`，并提高文件头 `# mosctl-template: N`。已安装的机器在面板升级后得到新模板文件，但 `config.yaml` 要等用户在面板迁移，或执行 `mosctl sync`，才会换结构。迁移和 sync 都保留 TAG 行上的上游和 `lazy_cache_ttl`。

改 systemd、logrotate 或 `99-mosdns.conf` 后，已安装机器要经过面板升级（会 `daemon-reload`）或重新安装才会换上。`99-mosdns.conf` 保持为空：救援模式自己用 `sysctl -w` 临时打开转发。

`.env` 若增加新键，安装脚本的 `write_env_file` 和面板的读取默认值要一起改。面板 `write_env` 拒绝的字符不要出现在默认值里。

## 测试

在仓库根目录：

```bash
python3 -m unittest discover -s tests
```

不需要安装 mosdns、Flask 或 root。`tests/test_app_logic.py`、`tests/test_auto_update.py`、`tests/test_notifications.py` 等会把 `app.py` 载入临时目录。其余多数测试对照源码，防止路由、模板顺序、升级覆盖范围和 README 里的环境变量条目被改丢。

`tests/test_readme_contract.py` 要求：

- `README.md` 保留英文的一键安装标题、安装后会打印面板地址这句话，以及两条 `curl` 安装命令
- `README.md` 和 `README.zh-CN.md` 都用列表项写出规定的 `.env` 键，并提到 `ip_forward`
- `README.zh-CN.md` 保留「一键安装」和「默认 Web 端口是 `7840`」

仓库没有 GitHub Actions。把上述 unittest 在本地跑过，是这个仓库里对应 CI 的检查。

## 发布

没有标签脚本、镜像仓库或 Actions workflow。已安装机器的更新来源是 `.env` 的 `MOSCTL_REPO_URL` 和 `MOSCTL_BRANCH`（默认 `https://github.com/anxiaoyang666/mosctl.git` 的 `main`）。

把 `remote-root/` 的改动合并到该分支后：

1. 新机器执行 README 里的 `install.sh`，克隆的就是这个分支。
2. 旧机器在面板升级中心检测并升级，或等到自动更新。面板等待天数大于 0 时，自动更新安装的是「当时判定已满天数的那一次 `remote-root/` 提交」，不会在下载前被更新的提交换掉。

`MOSDNS_VERSION` 只影响新安装时下载哪个内核 tag，不影响以后的自动更新。
