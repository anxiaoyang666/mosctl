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

## Common Commands

```bash
mosctl
systemctl status mosdns
systemctl status mosdns-web
```
