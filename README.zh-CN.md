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

## 常用命令

```bash
mosctl
systemctl status mosdns
systemctl status mosdns-web
```
