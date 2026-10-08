# Mosctl

[English](README.md) | [中文](README.zh-CN.md)

Mosctl 是一个 mosdns 安装、配置和 Web 管理面板工具。

## 一键安装

推荐在 Debian/Ubuntu LXC、虚拟机或服务器中使用 `root` 执行：

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/anxiaoyang666/mosctl/main/install.sh)"
```

国内网络可以使用加速地址：

```bash
bash -c "$(curl -fsSL https://gh-proxy.com/https://raw.githubusercontent.com/anxiaoyang666/mosctl/main/install.sh)"
```

安装完成后，终端会打印 Web 面板地址、用户名和随机生成的密码。默认 Web 端口是 `7840`，默认用户名是 `admin`。

## 自定义安装参数

```bash
WEB_PORT=7840 WEB_USER=admin WEB_SECRET='your-password' bash -c "$(curl -fsSL https://raw.githubusercontent.com/anxiaoyang666/mosctl/main/install.sh)"
```

可用变量：

- `MOSCTL_REPO_URL`：仓库地址，默认 `https://github.com/anxiaoyang666/mosctl.git`
- `MOSCTL_BRANCH`：安装分支，默认 `main`
- `WEB_PORT`：Web 面板端口，默认 `7840`
- `WEB_USER`：Web 登录用户名，默认 `admin`
- `WEB_SECRET`：Web 登录密码，不填写则随机生成
- `MOSDNS_VERSION`：mosdns 内核版本，默认 `latest`
- `GH_PROXY`：GitHub 代理前缀，默认 `https://gh-proxy.com/`

## 配置文件（`/etc/mosdns/.env`）

安装脚本会生成 `/etc/mosdns/.env`（权限 `600`）。Web 面板会读取并更新它；`mosctl` 只从中读取 `MOSCTL_REPO_URL`、`MOSCTL_BRANCH` 和 `GH_PROXY`。键说明：

- `WEB_SESSION_SECRET`：会话签名密钥，安装时生成，修改面板密码时自动轮换
- `WEB_USER`：Web 登录用户名
- `WEB_SECRET`：Web 登录密码
- `WEB_PORT`：Web 面板端口，默认 `7840`
- `MOSCTL_REPO_URL`：面板升级和 `mosctl sync` 使用的仓库地址
- `MOSCTL_BRANCH`：面板升级和 `mosctl sync` 使用的分支，默认 `main`
- `GH_PROXY`：GitHub 代理前缀，只在直连 GitHub 失败后才使用；设为空字符串表示不走代理
- `RULE_SYNC_ENABLED`：`true` / `false`，把强制国内 / 强制国外规则的改动推送到其他面板
- `RULE_SYNC_TOKEN`：`/api/rule-sync` 校验用的共享密钥
- `RULE_SYNC_PEERS`：其他 mosctl / mihomo 面板地址，用 `|` 分隔
- `BACKUP_KEEP_COUNT`：配置备份保留数量（3–200），默认 `20`

## 规则同步

开启规则同步后，保存强制国内 / 强制国外规则时会推送到 `RULE_SYNC_PEERS` 里的每个地址。节点地址默认是明文 `http://`，同步密钥通过请求头明文传输：同步密钥会以明文发送，建议仅在可信内网使用，或者给面板套上 HTTPS。面板里只要有节点不是 `https://`，也会显示同样的提示。

## 系统改动说明

- `net.ipv4.ip_forward` 不再被永久开启。救援模式（`mosctl rescue enable`）会临时开启，`mosctl rescue disable` 会恢复之前的值。
- `mosctl sync`（菜单第 2 项）会从配置的仓库和分支拉取 `remote-root/etc/mosdns/templates/default.yaml`，保留当前上游 DNS 和 TTL，把旧配置备份为 `/etc/mosdns/backup/config.<时间戳>.bak`，mosdns 启动失败时自动回滚。

## 常用命令

```bash
mosctl
systemctl status mosdns
systemctl status mosdns-web
```
