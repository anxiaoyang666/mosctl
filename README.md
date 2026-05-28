# Mosctl

Mosctl 是一个 mosdns 安装、配置和 Web 管理面板工具。

## 上传到你自己的仓库

先在 GitHub 创建一个你自己的仓库，例如：

```text
https://github.com/你的用户名/mosctl
```

然后把本项目目录上传到你的仓库。上传后，把下面命令里的 `你的用户名` 替换成你的 GitHub 用户名。

## 一键安装

推荐在 Debian/Ubuntu LXC、虚拟机或服务器上用 `root` 执行：

```bash
MOSCTL_REPO_URL=https://github.com/你的用户名/mosctl.git bash -c "$(curl -fsSL https://raw.githubusercontent.com/你的用户名/mosctl/main/install.sh)"
```

国内网络可以用代理地址：

```bash
MOSCTL_REPO_URL=https://github.com/你的用户名/mosctl.git bash -c "$(curl -fsSL https://gh-proxy.com/https://raw.githubusercontent.com/你的用户名/mosctl/main/install.sh)"
```

安装完成后终端会打印：

- Web 面板地址
- 登录用户名
- 随机生成的登录密码

默认 Web 端口是 `7840`，默认用户名是 `admin`。

## 自定义安装参数

```bash
MOSCTL_REPO_URL=https://github.com/你的用户名/mosctl.git WEB_PORT=7840 WEB_USER=admin WEB_SECRET='your-password' bash install.sh
```

可用变量：

- `MOSCTL_REPO_URL`: 你的仓库地址，curl 安装时必填
- `MOSCTL_BRANCH`: 安装分支，默认 `main`
- `WEB_PORT`: Web 面板端口，默认 `7840`
- `WEB_USER`: Web 登录用户名，默认 `admin`
- `WEB_SECRET`: Web 登录密码，不填则随机生成
- `MOSDNS_VERSION`: mosdns 内核版本，默认 `latest`
- `GH_PROXY`: GitHub 代理前缀，默认 `https://gh-proxy.com/`

## 常用命令

```bash
mosctl
systemctl status mosdns
systemctl status mosdns-web
```
