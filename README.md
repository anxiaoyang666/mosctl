# Mosctl

[English](README.md) | [中文](README.zh-CN.md)

Mosctl is a mosdns installer, configuration helper, and Web management panel. It installs the [mosdns](https://github.com/IrineSistiana/mosdns) binary, a split-DNS config template, a shell menu, and a Flask panel onto one Linux machine.

The Chinese maintainer guide is [README.zh-CN.md](README.zh-CN.md), with [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [docs/MAINTENANCE.md](docs/MAINTENANCE.md). `remote-root/` is the tree copied onto the target host. Install and panel upgrade replace only that tree's programs and templates. They leave the machine's `.env`, `config.yaml`, and rule files in place.

## Features

- Install the mosdns binary, systemd units, and the Web panel, then print the URL, username, and password.
- Five panel pages: 态势总览 (overview), 解析策略 (policy), 运行维护 (operations), 高级配置 (advanced), 系统控制 (control).
- Edit the domestic upstream, the domestic backup, the foreign upstream, and the cache TTL. Those three upstreams are the lines tagged `TAG_LOCAL`, `TAG_LOCAL_BACKUP`, and `TAG_REMOTE`.
- Edit force-cn, force-nocn, and hosts rules. A save checks the text and starts mosdns in a sandbox before replacing the live file.
- Refresh `geosite_cn.txt` and `geosite_no_cn.txt` from the Loyalsoldier lists, once or on a crontab schedule.
- Push force-cn / force-nocn rules to other mosctl or mihomo panels at `/api/rule-sync`.
- Upgrade the mosdns core and the Mosctl panel. The core is sandbox-tested against the live config and health-checked after replacement; failure restores the previous binary.
- Daily crontab updates for the core and the panel (`--dry-run`, `--only`, `--manual`).
- Webhook notifications for auto-update, Geo update failure and recovery, rule-sync apply failure and recovery, and rollback after a config or rule change.
- Backups of config, rules, and the core binary, plus a directory backup before a panel upgrade.
- Rescue mode: after repeated mosdns start failures, TCP/UDP port 53 is redirected to `223.5.5.5`.
- The `mosctl` menu and the subcommands `rescue`, `sync`, `update`, `flush`, `cache-ttl`, `test`, and `version`.

## Stack

| Piece | What the repo uses |
| --- | --- |
| DNS core | Linux release zips from [IrineSistiana/mosdns](https://github.com/IrineSistiana/mosdns), installed to `/usr/local/bin/mosdns`. This repo does not build mosdns. |
| Web panel | Python 3 and the distro package `python3-flask`. Entry point: `remote-root/etc/mosdns/manager/app.py`. The version constant is `PANEL_VERSION` (currently `0.3.50` in this tree). |
| CLI | Bash: `install.sh` and `remote-root/usr/local/bin/mosctl`. |
| Services | systemd units `mosdns.service`, `mosdns-web.service`, `mosdns-rescue.service`. |
| Schedules | root's crontab, one line for Geo updates and one for auto-update. |
| UI | A single `templates/index.html`. There is no frontend build. |

There is no `requirements.txt`, `pyproject.toml`, Dockerfile, or GitHub Actions workflow. `install.sh` installs Flask from the distro; the repo does not pin it.

## One-Click Install

Run as `root` on Debian/Ubuntu LXC, VM, or server. `install.sh` also accepts `dnf` and `yum`. It exits if none of `apt-get`, `dnf`, or `yum` exists.

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/anxiaoyang666/mosctl/main/install.sh)"
```

China network acceleration:

```bash
bash -c "$(curl -fsSL https://gh-proxy.com/https://raw.githubusercontent.com/anxiaoyang666/mosctl/main/install.sh)"
```

The installer installs `ca-certificates`, `curl`, `wget`, `unzip`, `tar`, `git`, `python3`, `python3-flask`, `iptables`, and `nano`, plus cron (`cron` and `dnsutils` on apt, `cronie` and `bind-utils` on dnf/yum). It clones the configured branch when `remote-root/` is not beside the script, downloads the mosdns zip for `x86_64`/`amd64`, `aarch64`/`arm64`, `armv7*`, `armv6*`, or `armv5*`, and copies `remote-root/` into place. An existing `/etc/mosdns/config.yaml` is kept and copied to `/etc/mosdns/backup/config.<timestamp>.bak`. It then enables and restarts `mosdns` and `mosdns-web`, and runs `mosctl update` once. A failed Geo update is a warning, not a failed install.

The installer prints the Web panel address, username, and generated password when it finishes. The default Web port is `7840`, and the default username is `admin`.

## Custom Install Parameters

These variables are read only while `install.sh` runs. Except `MOSDNS_VERSION` and `MOSCTL_KEEP_ENV`, they are written to `/etc/mosdns/.env`.

```bash
WEB_PORT=7840 WEB_USER=admin WEB_SECRET='your-password' bash -c "$(curl -fsSL https://raw.githubusercontent.com/anxiaoyang666/mosctl/main/install.sh)"
```

Available variables:

- `MOSCTL_REPO_URL`: repository URL, default `https://github.com/anxiaoyang666/mosctl.git`
- `MOSCTL_BRANCH`: install branch, default `main`
- `WEB_PORT`: Web panel port, default `7840`
- `WEB_USER`: Web login username, default `admin`
- `WEB_SECRET`: Web login password, randomly generated when omitted
- `MOSDNS_VERSION`: mosdns core version, default `latest`. Any other value is a release tag such as `v5.3.3`. Not written to `.env`
- `GH_PROXY`: GitHub proxy prefix, default `https://gh-proxy.com/`
- `MOSCTL_KEEP_ENV`: default `1`. Keeps an existing `.env`. Set `0` to rewrite it, which replaces the password and the sync token

Running `bash install.sh` from a checkout uses the local `remote-root/` and does not clone.

## Run locally and test

`app.py` and `mosctl` use absolute paths under `/etc/mosdns` and `/usr/local/bin`. Running `python3 remote-root/etc/mosdns/manager/app.py` on a workstation does not produce a working DNS panel.

On the target machine, start the stack with the installer, then:

```bash
systemctl status mosdns
systemctl status mosdns-web
```

`mosdns-web` runs `python3 /etc/mosdns/manager/app.py` on `0.0.0.0` and the port in `.env`. Open `http://<server-ip>:7840/`.

From a checkout, without root or a mosdns binary:

```bash
python3 -m unittest discover -s tests
```

The tests stub Flask and point `/etc/mosdns` at a temp directory. There is no separate lint or package command.

## Build and deploy

There is no compile step, container image, or CI release workflow. Shipping a change means getting `remote-root/` onto a host from `main`, or from whatever `MOSCTL_BRANCH` the host uses.

1. **New host**: run `install.sh`. It downloads a mosdns release zip and copies this repo's `remote-root/`.
2. **Panel upgrade on an installed host**: 运行维护 → 升级中心, or the daily auto-update. The panel downloads the GitHub branch archive for `MOSCTL_REPO_URL` / `MOSCTL_BRANCH` (a specific commit when the panel minimum age is greater than 0). The archive must contain `remote-root/`, and the new `app.py` must compile. A remote `PANEL_VERSION` that is not newer is refused. Managed files are backed up to `/etc/mosdns/backup/mosctl-panel.<timestamp>/` and restored from `manifest.json` if replacement fails. On success, `systemctl restart mosdns-web` runs about one second later.
3. **Core upgrade**: the upgrade card installs GitHub Releases `latest`. Auto-update picks the newest stable release that is newer than the running binary, old enough, and has this machine's asset. Neither path downgrades. Both sandbox-start the new binary and then health-check for up to 20 seconds.

`mosctl sync` (menu item 2) updates only the config template.

## Configuration File (`/etc/mosdns/.env`)

The installer writes `/etc/mosdns/.env` (mode `600`). The Web panel reads and updates it; `mosctl` only reads `MOSCTL_REPO_URL`, `MOSCTL_BRANCH` and `GH_PROXY` from it. Values are parsed as text, not sourced as shell. Writes reject quotes, backslashes, `$`, backticks, and newlines, and replace the file atomically.

Keys:

- `WEB_SESSION_SECRET`: session signing key, generated at install and rotated whenever the panel password changes
- `WEB_USER`: Web login username
- `WEB_SECRET`: Web login password
- `WEB_PORT`: Web panel port, default `7840`. Read when `mosdns-web` starts; run `systemctl restart mosdns-web` after changing it
- `MOSCTL_REPO_URL`: repository used by the panel upgrade and `mosctl sync`. Online sync and upgrade accept only `https://github.com/<owner>/<repo>` (optional `.git`)
- `MOSCTL_BRANCH`: branch used by the panel upgrade and `mosctl sync`, default `main`
- `GH_PROXY`: GitHub proxy prefix tried only after a direct GitHub download fails; set it to an empty string to never use a proxy. A missing key falls back to `https://gh-proxy.com/`
- `RULE_SYNC_ENABLED`: `true` / `false`, push force-cn / force-nocn rule changes to other panels. Only the string `true` (any case) enables it
- `RULE_SYNC_TOKEN`: shared secret that `/api/rule-sync` checks. The `X-Mosdns-Sync-Token` header wins over a JSON `token`
- `RULE_SYNC_PEERS`: other mosctl / mihomo panel URLs, separated by `|`. The panel also accepts newlines or commas and prefixes `http://` when the scheme is missing
- `BACKUP_KEEP_COUNT`: number of config backups to keep (3–200), default `20`. Kernel backups stay at 3 files, and panel directory backups stay at 3, regardless of this value
- `AUTO_UPDATE_ENABLED`: `true` / `false`, update the mosdns core and the panel automatically every day, default `true`. `false`, `0`, `no`, and `off` disable it
- `AUTO_UPDATE_TIME`: daily auto-update time (server local time, `HH:MM`), default `04:10`
- `AUTO_UPDATE_CORE_MIN_AGE_DAYS`: only install a stable mosdns release once it is at least this many days old (0–365), default `3`
- `AUTO_UPDATE_PANEL_MIN_AGE_DAYS`: only update the panel once the newest commit touching `remote-root/` on the branch is at least this many days old (0–365), default `0`
- `SITE_NAME`: site name shown in notification titles (up to 20 characters); empty shows `mosdns`. `GET /api/site-info` returns this name to other panels and nothing else
- `NOTIFY_ENABLED`: `true` / `false`, send notifications, default `false`
- `NOTIFY_API_URL`: notification webhook (`http://` or `https://`), POSTed JSON `{"title", "content"}` directly without a proxy; the panel only shows its host

## Auto Update

On startup and whenever its settings are saved, the panel keeps one crontab line tagged `# MOSCTL_AUTO_UPDATE` that runs `python3 /etc/mosdns/manager/auto_update.py` at `AUTO_UPDATE_TIME` (`--dry-run` and `--only core|panel` are available; the panel's run-now button adds `--manual`). A disabled schedule still runs for `--dry-run` and `--manual`. Each run updates the core first and the panel last. The core only moves to stable releases old enough, never downgrades, is first started in a sandbox against the live config, and must pass a 20-second health check (service active, reported version, domestic and foreign lookups on 127.0.0.1:53) or the old binary is restored. Results go to `/etc/mosdns/auto_update_state.json` and `/var/log/mosctl-auto-update.log` and show up under 运行维护 → 自动更新. A lock file at `/etc/mosdns/.auto_update.lock` allows one run at a time.

## Notifications

After setting a site name and webhook under 运行维护 → 通知 and enabling it, the panel notifies (same format as the mihomo panel: title `icon site · event`, server time appended to the body; that timestamp is fixed at UTC+8, which can differ from the server timezone used by cron) when auto-update installs, fails or rolls back the core or the panel; when the Geo update fails or recovers; when received rule-sync content cannot be applied or recovers; and when a policy, rule, or `config.yaml` change makes mosdns fail to restart and is rolled back. Recurring failures only notify on state changes, with a reminder at most every 3 days while still failing (state in `/etc/mosdns/notify_state.json`). Sends are logged to `/var/log/mosctl-notify.log` (host only).

`mosctl update` reports Geo results with `python3 /etc/mosdns/manager/notify_cli.py --key geo --event fail|ok ...`. A notification error does not change `mosctl`'s exit code.

## Rule Sync

When rule sync is enabled, saving the force-cn / force-nocn rules pushes them to every URL in `RULE_SYNC_PEERS`. Hosts are not synced. The push runs in a background thread; the page polls `/api/rule-sync-jobs/<id>`. The local panel URL is skipped. A peer that answers "操作进行中" or HTTP 409 is retried 3 times, 3 seconds apart.

Peers default to plain `http://`, and the sync token travels in a request header in clear text, so only use rule sync on a trusted LAN or put the panels behind HTTPS. The panel shows the same warning (同步密钥会以明文发送，建议仅在可信内网使用) whenever a peer is not `https://`.

Connectivity test posts `{"rules": {}}`. A matching token returns success and does not change rules or raise a failure notification.

## System Changes

- `net.ipv4.ip_forward` is no longer enabled permanently. `/etc/sysctl.d/99-mosdns.conf` is empty so an upgrade clears the old permanent setting. Rescue mode (`mosctl rescue enable`) turns it on temporarily, records the previous value in `/etc/mosdns/.rescue-ip-forward`, and `mosctl rescue disable` restores that value and deletes the iptables rules.
- `mosdns.service` enters failed after 5 start failures inside 60 seconds and then starts `mosdns-rescue.service`. About 2 seconds after a successful start, `ExecStartPost` runs `mosctl rescue disable silent` in the background.
- `mosctl sync` (menu item 2) fetches `remote-root/etc/mosdns/templates/default.yaml` from the configured repository and branch, keeps the current upstream DNS and TTL, backs up the old config as `/etc/mosdns/backup/config.<timestamp>.bak`, and rolls back if mosdns fails to start.

## Directory layout

```text
.
├── install.sh
├── README.md
├── README.zh-CN.md
├── docs/
│   ├── ARCHITECTURE.md
│   ├── MAINTENANCE.md
│   └── superpowers/specs/2026-05-28-panel-upgrade-design.md
├── tests/
└── remote-root/
    ├── usr/local/bin/mosctl
    └── etc/
        ├── logrotate.d/mosdns
        ├── sysctl.d/99-mosdns.conf
        ├── systemd/system/
        └── mosdns/
            ├── templates/default.yaml
            ├── rules/
            └── manager/
                ├── app.py
                ├── auto_update.py
                ├── notify_cli.py
                ├── templates/
                └── static/
```

Runtime paths created on the host, not stored in git: `/usr/local/bin/mosdns`, `/etc/mosdns/config.yaml`, `/etc/mosdns/.env`, `/etc/mosdns/cache.dump`, `/etc/mosdns/backup/`, `/var/log/mosdns.log`, `/var/log/mosctl-update.log`, `/var/log/mosctl-auto-update.log`, `/var/log/mosctl-notify.log`.

The geosite files shipped in `remote-root/etc/mosdns/rules/` are the initial copies. A successful `mosctl update` replaces `geosite_cn.txt` and `geosite_no_cn.txt`. `force-cn.txt`, `force-nocn.txt`, and `hosts.txt` are empty in the repo.

## Web panel

`/login` locks a source IP for 60 seconds after 5 failures. The counter lives in process memory. Sessions last 30 days (HttpOnly, `SameSite=Lax`). Changing the password rotates `WEB_SESSION_SECRET`. API writes other than `/api/rule-sync` require `X-Requested-With: XMLHttpRequest`. Posts that change config or restart services share one process lock and return HTTP 409 with `操作进行中，请稍后再试` when it is busy.

## Common Commands

```bash
mosctl
mosctl version
mosctl sync
mosctl update
mosctl flush
mosctl cache-ttl 3600
mosctl test
mosctl rescue enable
mosctl rescue disable
systemctl status mosdns
systemctl status mosdns-web
systemctl restart mosdns-web
python3 /etc/mosdns/manager/auto_update.py --dry-run
python3 /etc/mosdns/manager/auto_update.py --only core
python3 /etc/mosdns/manager/auto_update.py --manual
```

With no arguments, `mosctl` opens the menu: service control, sync, DNS settings, custom rules, Geo update, rescue mode, follow `/var/log/mosdns.log`, a DNS test, and uninstall. Uninstall stops and disables the three units, deletes `/etc/mosdns`, both binaries, the four logs, and the Geo and auto-update crontab lines. A symlink `/etc/resolv.conf` is left alone. A regular file whose `nameserver` is `127.0.0.1` is rewritten to `223.5.5.5`.

## FAQ

**The installer says to run as root.** `install.sh` requires `id -u` to be 0.

**It cannot find apt-get, dnf, or yum.** Those are the only package managers the script calls.

**Unsupported architecture.** `uname -m` is outside the five families listed above. Core upgrade refuses the same machines.

**GitHub downloads fail.** Install, `mosctl`, and the panel try the direct URL first, then `GH_PROXY`. An empty `GH_PROXY` disables the proxy. `mosctl sync` only accepts a GitHub raw URL.

**Reinstall kept the password and config.yaml.** An existing `.env` is kept while `MOSCTL_KEEP_ENV` is `1`. An existing `config.yaml` is backed up and left in place. `MOSCTL_KEEP_ENV=0` rewrites `.env` and replaces the sync token and session secret.

**The panel port did not change.** `WEB_PORT` is read at process start. Restart `mosdns-web`.

**Too many login failures.** Five failures lock that IP for 60 seconds. Wait, or restart `mosdns-web` to clear the in-memory counter.

**操作进行中，请稍后再试.** Another mutating request is running. Manual core or panel upgrade is also refused while auto-update holds its lock.

**Saving the Geo schedule says crontab is missing.** Install `cron` or `cronie` and start `cron` or `crond`. Auto-update reports the same gap.

**Rescue mode or ip_forward stays on.** `mosctl rescue disable` removes the DNAT and MASQUERADE rules aimed at `223.5.5.5` and restores `net.ipv4.ip_forward` from `/etc/mosdns/.rescue-ip-forward`. Without that file it does not change ip_forward.

**Panel upgrade says the current version is newest, or that the remote tree has no version.** Comparison uses `PANEL_VERSION` in `app.py`. A remote version that is not higher is not installed.

**Auto-update did not run.** Check `AUTO_UPDATE_ENABLED`, the crontab line containing `# MOSCTL_AUTO_UPDATE`, and that cron is active. The clock is the server's local timezone. The core also has to be a newer stable build at least `AUTO_UPDATE_CORE_MIN_AGE_DAYS` old.

**Notification time disagrees with the server clock.** Notification bodies use UTC+8. Geo and auto-update schedules use the server's local timezone.

## Further reading

- [Architecture](docs/ARCHITECTURE.md)
- [Maintenance](docs/MAINTENANCE.md)
- [Panel upgrade design note](docs/superpowers/specs/2026-05-28-panel-upgrade-design.md)
