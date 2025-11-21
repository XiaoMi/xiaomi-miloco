# 运行和开发环境配置（macOS 篇）

[返回上一级](./environment-setup_zh-Hans.md)

语言选择：[English](./environment-setup-macos.md) | [简体中文](./environment-setup-macos_zh-Hans.md)

------



下述教程以 macOS Tahoe 26.1 为例，介绍如何在 Docker 下运行本服务。

macOS （M 系列和 Intel 系列）下服务支持两种方式运行：

- GitHub 克隆代码运行
- Docker 运行

## 环境配置

>  macOS的 Docker 是通过虚拟机运行，原因如下：
>
> - Docker 容器使用的是 **Linux 内核特性（cgroups, namespaces, overlayfs 等）**。
> - macOS 内核是 **XNU**，而且不支持 Linux 内核 API，也不支持直接运行 ELF 格式的 Linux 二进制文件。
>
> 因此在 macOS 上，必须先启动一个 **Linux 虚拟机**（VM），然后在 VM 里运行 Docker 容器。

在本教程中，采用 **Multipass + Docker** 的方式运行服务，如果觉得比较复杂，可以尝试直接通过代码运行服务。

### 基础配置

macOS下，包管理通常使用 **Homebrew(brew) **，可打开终端使用下述脚本安装 `brew`，如果已安装可跳过。

```shell
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

在安装过程中，可能会有下述提示：

- 是否安装 Xcode Command Line Tools（必需，按提示装）
- xxxxxxxxxx docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi# 验证完成后，可移除镜像docker rmi nvidia/cuda:12.4.0-base-ubuntu22.04shell
- 结束后会提示你把 `brew` 的路径加到 `PATH` 里。比如 Apple M 系列会提示：

```shell
echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile
eval "$(/opt/homebrew/bin/brew shellenv)"
```

国内用户可继续配置镜像源加速：

```shell
# 替换 brew.git 仓库源
git -C "$(brew --repo)" remote set-url origin https://mirrors.tuna.tsinghua.edu.cn/git/homebrew/brew.git

# 替换 core.git 仓库源
git -C "$(brew --repo homebrew/core)" remote set-url origin https://mirrors.tuna.tsinghua.edu.cn/git/homebrew/homebrew-core.git

# 替换 bottles 源（自动下载二进制包加速）
echo 'export HOMEBREW_BOTTLE_DOMAIN=https://mirrors.tuna.tsinghua.edu.cn/homebrew-bottles' >> ~/.zprofile
source ~/.zprofile
```

使用下述命令安装`multipass`：

```shell
brew install multipass qemu
```

### 网络配置

Docker Desktop 的网络模式默认为 NAT(Network Address Translation) 模式，直接通过 Docker Desktop 运行服务将无法拉流。

确认网络接口（建议使用有线网卡，使用无线网卡可能会卡住），使用下述命令查看：

```shell
multipass networks
```

Mac Mini M4 下，`en0` 为有线网卡（Ethernet），所以使用 `en0`

```shell
Name   Type       Description
en0    ethernet   Ethernet
en1    wifi       Wi-Fi
en5    ethernet   Ethernet Adapter (en5)
en6    ethernet   Ethernet Adapter (en6)
en7    ethernet   Ethernet Adapter (en7)
```

