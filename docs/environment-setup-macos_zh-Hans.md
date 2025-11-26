# 运行和开发环境配置（macOS 篇）

[返回上一级](./environment-setup_zh-Hans.md)

语言选择：[English](./environment-setup-macos.md) | [简体中文](./environment-setup-macos_zh-Hans.md)

------


macOS （M 系列和 Intel 系列）下服务支持两种方式运行：

- GitHub 克隆代码直接运行
- 虚拟机下 Docker 运行

下述教程以 macOS Tahoe 26.1 为例，介绍如何在 Docker 下运行本服务，克隆代码直接运行服务在开发指南中已经介绍，在此不做赘述。

## 环境配置

>  macOS的 Docker 是通过虚拟机运行，原因如下：
>
> - Docker 容器使用的是 **Linux 内核特性（cgroups, namespaces, overlayfs 等）**。
> - macOS 内核是 **XNU**，而且不支持 Linux 内核 API，也不支持直接运行 ELF 格式的 Linux 二进制文件。
>
> 因此在 macOS 上，必须先启动一个 **Linux 虚拟机**（VM），然后在 VM 里运行 Docker 容器。
> 
> 本教程提供两条思路在 Docker 下运行服务：
> - 命令行下，采用 **Multipass + Docker** 的方式运行服务，下述教程有介绍运行步骤
> - 安装 VMware Fusion 免费版，配置网络模式为桥接（Bridge）。
> 
> 虚拟机环境配置完成后，可参考 [Linux 篇](./environment-setup-linux_zh-Hans.md) 的教程在 VM 中运行服务。

在本教程中，介绍 **Multipass + Docker** 的方式运行服务。

### 基础配置

macOS下，包管理通常使用 **Homebrew(brew)**，可打开终端使用下述脚本安装 `brew`，如果已安装可跳过。

```shell
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

在安装过程中，可能会有下述提示：

- 是否安装 Xcode Command Line Tools（必需，按提示装）
- 安装路径（Intel Mac 默认 /usr/local，Apple Silicon 默认 /opt/homebrew）
- 结束后会提示你把 `brew` 的路径加到 `PATH` 里。比如 Apple M 系列会提示：

```shell
echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile
eval "$(/opt/homebrew/bin/brew shellenv)"
```

中国大陆用户可继续配置镜像源加速：

```shell
# 替换 brew.git 仓库源
git -C "$(brew --repo)" remote set-url origin https://mirrors.tuna.tsinghua.edu.cn/git/homebrew/brew.git

# 替换 core.git 仓库源
git -C "$(brew --repo homebrew/core)" remote set-url origin https://mirrors.tuna.tsinghua.edu.cn/git/homebrew/homebrew-core.git

# 替换 bottles 源（自动下载二进制包加速）
echo 'export HOMEBREW_BOTTLE_DOMAIN=https://mirrors.tuna.tsinghua.edu.cn/homebrew-bottles' >> ~/.zprofile
source ~/.zprofile
```

使用下述命令安装`multipass` + `qemu`：

```shell
brew install multipass qemu
```

### 虚拟机配置

Docker Desktop 的网络模式默认为 NAT(Network Address Translation)  模式，直接通过 Docker Desktop 运行服务可能无法拉流。所以本教程使用 `multipass` 创建一个虚拟机，并将虚拟机网络桥接到物理网络。

先确认网络接口；建议使用有线网卡，使用无线网卡可能会卡住，原因是 macOS 的 Wi-Fi 硬件驱动（以及大多数无线接入点）可能不允许同一个 Wi-Fi 链接上有两个不同的 MAC 地址（一个是你的 Mac，一个是虚拟机）；

使用下述命令查看网络接口：

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

创建一个名为 `bridge-docker` 的虚拟机，并指定网络为桥接（`--network`）：

```shell
# 语法：multipass launch --name [名字] --network [你的物理网卡] --cpus [核数] --mem [内存] --disk [硬盘]
multipass launch --name bridge-docker --network en0 --cpus 4 --mem 4G --disk 30G
```

虚拟机启动后，查看它的 IP：
```bash
multipass list
```

可以看到当前虚拟机的 IP 和你 Mac 在同一网段，如果未再同一网段，可按照上述步骤重新排查。


### 安装 Docker

可使用下述命令进入虚拟机：

```shell
multipass shell bridge-docker
```

至此，macOS下桥接网络模式的虚拟机已配置完成，后续步骤就是在 Linux(ARM64) 下配置服务运行环境，具体步骤可参考[Linux 篇](./environment-setup-linux_zh-Hans.md), 这里不再赘述。

## 环境验证

1. 进入macOS虚拟机后，确保和宿主主机处于同一网段，否则摄像头无法正常拉流。
2. 由于暂时没有 Linux(ARM64) + NVIDIA GPU 的环境，所以无法验证显卡直通和一键安装脚本是否能够正常配置运行环境，欢迎开发者提供这方面的技术支持。
