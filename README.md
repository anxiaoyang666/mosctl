# Mosctl

[English](README.md) | [中文](README.zh-CN.md)

Mosctl is a mosdns installer, configuration helper, and Web management panel.

## One-Click Install

Run as `root` on Debian/Ubuntu LXC, VM, or server:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/anxiaoyang666/mosctl/main/install.sh)"
```

China network acceleration:

```bash
bash -c "$(curl -fsSL https://gh-proxy.com/https://raw.githubusercontent.com/anxiaoyang666/mosctl/main/install.sh)"
```

The installer prints the Web panel address, username, and generated password when it finishes. The default Web port is `7840`, and the default username is `admin`.

## Custom Install Parameters

```bash
WEB_PORT=7840 WEB_USER=admin WEB_SECRET='your-password' bash -c "$(curl -fsSL https://raw.githubusercontent.com/anxiaoyang666/mosctl/main/install.sh)"
```

Available variables:

- `MOSCTL_REPO_URL`: repository URL, default `https://github.com/anxiaoyang666/mosctl.git`
- `MOSCTL_BRANCH`: install branch, default `main`
- `WEB_PORT`: Web panel port, default `7840`
- `WEB_USER`: Web login username, default `admin`
- `WEB_SECRET`: Web login password, randomly generated when omitted
- `MOSDNS_VERSION`: mosdns core version, default `latest`
- `GH_PROXY`: GitHub proxy prefix, default `https://gh-proxy.com/`

## Configuration File (`/etc/mosdns/.env`)

The installer writes `/etc/mosdns/.env` (mode `600`). The Web panel reads and updates it; `mosctl` only reads `MOSCTL_REPO_URL`, `MOSCTL_BRANCH` and `GH_PROXY` from it. Keys:

- `WEB_SESSION_SECRET`: session signing key, generated at install and rotated whenever the panel password changes
- `WEB_USER`: Web login username
- `WEB_SECRET`: Web login password
- `WEB_PORT`: Web panel port, default `7840`
- `MOSCTL_REPO_URL`: repository used by the panel upgrade and `mosctl sync`
- `MOSCTL_BRANCH`: branch used by the panel upgrade and `mosctl sync`, default `main`
- `GH_PROXY`: GitHub proxy prefix tried only after a direct GitHub download fails; set it to an empty string to never use a proxy
- `RULE_SYNC_ENABLED`: `true` / `false`, push force-cn / force-nocn rule changes to other panels
- `RULE_SYNC_TOKEN`: shared secret that `/api/rule-sync` checks
- `RULE_SYNC_PEERS`: other mosctl / mihomo panel URLs, separated by `|`
- `BACKUP_KEEP_COUNT`: number of config backups to keep (3–200), default `20`
- `AUTO_UPDATE_ENABLED`: `true` / `false`, update the mosdns core and the panel automatically every day, default `true`
- `AUTO_UPDATE_TIME`: daily auto-update time (server local time, `HH:MM`), default `04:10`
- `AUTO_UPDATE_CORE_MIN_AGE_DAYS`: only install a stable mosdns release once it is at least this many days old (0–365), default `3`
- `AUTO_UPDATE_PANEL_MIN_AGE_DAYS`: only update the panel once the newest commit touching `remote-root/` on the branch is at least this many days old (0–365), default `0`

## Auto Update

On startup and whenever its settings are saved, the panel keeps one crontab line tagged `# MOSCTL_AUTO_UPDATE` that runs `python3 /etc/mosdns/manager/auto_update.py` at `AUTO_UPDATE_TIME` (`--dry-run` and `--only core|panel` are available). Each run updates the core first and the panel last. The core only moves to stable releases old enough, never downgrades, is first started in a sandbox against the live config, and must pass a 20-second health check (service active, reported version, domestic and foreign lookups on 127.0.0.1:53) or the old binary is restored. Results go to `/etc/mosdns/auto_update_state.json` and `/var/log/mosctl-auto-update.log` and show up under 运行维护 → 自动更新.

## Rule Sync

When rule sync is enabled, saving the force-cn / force-nocn rules pushes them to every URL in `RULE_SYNC_PEERS`. Peers default to plain `http://`, and the sync token travels in a request header in clear text, so only use rule sync on a trusted LAN or put the panels behind HTTPS. The panel shows the same warning (同步密钥会以明文发送，建议仅在可信内网使用) whenever a peer is not `https://`.

## System Changes

- `net.ipv4.ip_forward` is no longer enabled permanently. Rescue mode (`mosctl rescue enable`) turns it on temporarily and `mosctl rescue disable` restores the previous value.
- `mosctl sync` (menu item 2) fetches `remote-root/etc/mosdns/templates/default.yaml` from the configured repository and branch, keeps the current upstream DNS and TTL, backs up the old config as `/etc/mosdns/backup/config.<timestamp>.bak`, and rolls back if mosdns fails to start.

## Common Commands

```bash
mosctl
systemctl status mosdns
systemctl status mosdns-web
```
