# 运行和开发环境配置（Windows 篇）

[返回上一级](./environment-setup_zh-Hans.md)

选择语言：[English](./environment-setup-windows.md) | [简体中文](./environment-setup-windows_zh-Hans.md)

------



下述教程以 Windows11 25H2 + WSL2.6.1 为例。

## 环境配置

系统要求： Windows11 22H2 及以上版本 + WSL2

### 开启 Windows WSL2 功能

请优先参考微软官方教程： [en](https://learn.microsoft.com/zh-cn/windows/wsl/install) | [中文](https://learn.microsoft.com/en-us/windows/wsl/install)

- 在系统中搜索然后打开控制面板，点击程序>启动或关闭 Windows 功能，然后勾选 Hyper-V 和适用于 Linux 的 Windows 子系统，点击确定，等待系统安装更新后重启
- 安装 WSL ，在系统中搜素终端然后打开，输入`wsl --install`，等待 WSL 安装完成；如果已经安装，可以使用`wsl --update`更新到最新版本
- 下载 WSL2 Linux 发行版
  - 打开 Windows 自带的应用商店，搜索 Ubuntu ，然后下载 Ubuntu24.04.1 LTS
  - 在 Windows 终端可使用`wsl --list --online`查看在线的发行版，然后输入`wsl --install -d Ubuntu-24.04`安装
- 使用 WSL2
  - 在应用商店下载完成后，可以点击**打开**按钮，然后按照提示输入用户名和密码，完成初始化
  - 在终端输入`wsl -d Ubuntu-24.04`，然后按照提示输入用户名和密码，完成初始化

### 网络模式配置

在系统中搜索 WSL Setting ，点击网络，然后将网络模式修改为 **Mirrored** ，修改完成后，需要使用`wsl --shutdown`停止子系统，然后重新运行`wsl -d Ubuntu-24.04`进入子系统，输入`ip a`查看子系统网络配置是否和宿主机器一致。

设置为 **Mirrored** 模式后，需要配置 Hyper-V 防火墙，允许入站连接。

在 PowerShell 窗口中以管理员权限运行以下命令，以配置 Hyper-V 防火墙设置，使其允许入站连接：
```powershell
Set-NetFirewallHyperVVMSetting -Name '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' -DefaultInboundAction Allow
# 使用下述命令获取WSL防火墙策略
Get-NetFirewallHyperVVMSetting -PolicyStore ActiveStore -Name '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}'
# DefaultInboundAction和DefaultOutboundAction为Allow即可:
# Name                  : {40E0AC32-46A5-438A-A0B2-2B479E8F2E90}
# Enabled               : True
# DefaultInboundAction  : Allow
# DefaultOutboundAction : Allow
# LoopbackEnabled       : True
# AllowHostPolicyMerge  : True
```

相关资料：
- [使用 WSL 访问网络应用程序](https://learn.microsoft.com/zh-cn/windows/wsl/networking)
- [配置防火墙](https://learn.microsoft.com/zh-cn/windows/security/operating-system-security/network-security/windows-firewall/hyper-v-firewall)

### 安装 Docker

使用官方脚本安装（ WSL2 中官方推荐 Docker Desktop 安装，可以忽略提示，采用下述命令直接安装）
```shell
curl -fsSL https://get.docker.com | bash -s docker
# 中国国内用户可以指定Aliyun源安装
curl -fsSL https://get.docker.com | bash -s docker --mirror Aliyun
```
可将当前用户加入 docker 组，从而可以直接使用 docker 命令：
```shell
sudo usermod -aG docker $USER
```
添加完成后，需要**重新登录**，以使用户组更改生效。
使用命令`docker --version`验证是否安装成功。

### 安装 CUDA Toolkit 和 NVIDIA Driver

请优先参考官方教程文档： [CUDA Toolkit Download for WSL-Ubuntu](https://developer.nvidia.com/cuda-downloads?target_os=Linux&target_arch=x86_64&Distribution=WSL-Ubuntu&target_version=2.0&target_type=deb_network) ，也可参考上述 Linux 环境配置流程。

### 安装 NVIDIA Container Toolkit

参考 [Linux 篇](./environment-setup-linux_zh-Hans.md) 配置流程

## 环境验证

参考 [Linux 篇](./environment-setup-linux_zh-Hans.md) 配置流程
