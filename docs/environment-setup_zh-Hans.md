# 运行和开发环境配置

[English](./environment-setup.md) | [简体中文](./environment-setup_zh-Hans.md)

运行环境要求：
- 系统要求：
- - **Linux**: x64 架构，建议 Ubuntu 22.04 及以上 LTS 版本
- - **Windows**: x64 架构，建议 Windows11 22H2 及以上版本，要求支持WSL2
- - **macOS**:  Arm 架构，暂不支持
- 显卡要求（项目AI Engine需要显卡支持）
- - **NVIDIA**：建议**30系及以上显卡，显存8G及以上**
- - **AMD**：暂不支持
- - **Intel**：暂不支持
- - **MThreads**：暂不支持
- 软件要求
- - **Docker**: 20.10 及以上版本，要求支持 `docker compose`

## 环境配置

> NOTICE:
>
> - 采用 Docker 方法运行，请按照下述步骤安装环境，如果环境已安装且验证无问题，可跳过环境配置步骤，否则可能导致程序无法运行
> - 摄像头只允许局域网拉流，Windows 下需要将 WSL2 的网络模式设置为 **Mirrored**
> - WSL2网络设置为 **Mirrored** 模式后，注意配置Hyper-V防火墙允许入站连接；重新刷新摄像头列表，如果还是离线状态，可以尝试关闭Windows防火墙

### Linux
下述教程以 Ubuntu 24.04 LTS 为例，其它 Linux 发行版请自行修改命令。

#### 环境配置

##### 安装 Docker

使用官方脚本安装：
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


##### 安装 CUDA Toolkit 和 NVIDIA Driver

请优先参考官方文档 [CUDA Toolkit Downloads](https://developer.nvidia.com/cuda-downloads) ，根据当前系统选择对应的版本后，按照步骤安装：
```shell
# 25-11-1更新
# 安装CUDA Toolkit
wget https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/cuda-keyring_1.1-1_all.deb
sudo dpkg -i cuda-keyring_1.1-1_all.deb
sudo apt-get update
# 可选安装，编译时使用
sudo apt-get -y install cuda-toolkit-13-0
# 安装NVIDIA Driver，任选一个，推荐安装cuda-drivers
sudo apt-get -y install nvidia-open
sudo apt-get -y install cuda-drivers
```

采用上述方式安装 CUDA Toolkit ，CUDA 环境变量可能未添加，可在`~/.bashrc`或者`~/.zshrc`（按照系统实际shell版本）后追加：
```shell
export PATH="/usr/local/cuda/bin:${PATH:-}"
export LD_LIBRARY_PATH="/usr/local/cuda/lib64:${LD_LIBRARY_PATH:-}"
```

##### 安装 NVIDIA Container Toolkit

请优先参考官方文档 [NVIDIA Container Toolkit Installation](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html#next-steps) ，根据系统版本选择对应的版本后，按照步骤安装：
```shell
# 25-11-1更新
# 配置下载源
sudo apt-get update && sudo apt-get install -y --no-install-recommends curl gnupg2
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg \
  && curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
    sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
    sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo sed -i -e '/experimental/ s/^#//g' /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update
export NVIDIA_CONTAINER_TOOLKIT_VERSION=1.18.0-1
  sudo apt-get install -y \
      nvidia-container-toolkit=${NVIDIA_CONTAINER_TOOLKIT_VERSION} \
      nvidia-container-toolkit-base=${NVIDIA_CONTAINER_TOOLKIT_VERSION} \
      libnvidia-container-tools=${NVIDIA_CONTAINER_TOOLKIT_VERSION} \
      libnvidia-container1=${NVIDIA_CONTAINER_TOOLKIT_VERSION}
```

#### 环境验证

##### 验证 Docker

使用`hello-world`镜像验证 Docker 是否安装成功，如果显示`Hello from Docker!`则表示安装成功。
```shell
docker run hello-world
# 验证完成后，可移除镜像
docker rmi hello-world
```
##### 验证 NVIDIA 显卡驱动

使用命令`nvidia-smi`验证 NVIDIA Driver 是否安装成功，如果显示显卡驱动和 CUDA 工具包信息，则表示安装成功。

使用命令`nvcc --version`验证 NVIDIA CUDA Toolkit 是否安装成功，如果安装成功，会显示版本信息。

##### 验证 NVIDIA Container Toolkit

使用下述命令验证 NVIDIA Container Toolkit 是否安装成功，如果显示显卡驱动和 CUDA 工具包信息，则表示安装成功。
```shell
docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi
# 验证完成后，可移除镜像
docker rmi nvidia/cuda:12.4.0-base-ubuntu22.04
```
### Windows

下述教程以 Windows11 25H2 + WSL2.6.1 为例。

#### 环境配置

系统要求： Windows11 22H2 及以上版本 + WSL2

##### 开启 Windows WSL2 功能

请优先参考微软官方教程： [en](https://learn.microsoft.com/zh-cn/windows/wsl/install) | [中文](https://learn.microsoft.com/en-us/windows/wsl/install)

- 在系统中搜索然后打开控制面板，点击程序>启动或关闭 Windows 功能，然后勾选 Hyper-V 和适用于 Linux 的 Windows 子系统，点击确定，等待系统安装更新后重启
- 安装 WSL ，在系统中搜素终端然后打开，输入`wsl --install`，等待 WSL 安装完成；如果已经安装，可以使用`wsl --update`更新到最新版本
- 下载 WSL2 Linux 发行版
  - 打开 Windows 自带的应用商店，搜索 Ubuntu ，然后下载 Ubuntu24.04.1 LTS
  - 在 Windows 终端可使用`wsl --list --online`查看在线的发行版，然后输入`wsl --install -d Ubuntu-24.04`安装
- 使用 WSL2
  - 在应用商店下载完成后，可以点击**打开**按钮，然后按照提示输入用户名和密码，完成初始化
  - 在终端输入`wsl -d Ubuntu-24.04`，然后按照提示输入用户名和密码，完成初始化

##### 网络模式配置

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

##### 安装 Docker

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

##### 安装 CUDA Toolkit 和 NVIDIA Driver

请优先参考官方教程文档： [CUDA Toolkit Download for WSL-Ubuntu](https://developer.nvidia.com/cuda-downloads?target_os=Linux&target_arch=x86_64&Distribution=WSL-Ubuntu&target_version=2.0&target_type=deb_network) ，也可参考上述 Linux 环境配置流程。

##### 安装 NVIDIA Container Toolkit

参考上述Linux环境配置流程

#### 环境验证

参考上述 Linux 环境验证流程

### MAC（M 系列和 Intel 系列）

暂不支持

## 下载模型

下述所有操作都在`models`文件下进行。

### Xiaomi MiMo-VL-Miloco-7B

小米自研的多模态模型，用于图像的本地推理。

模型下载地址：

- `huggingface`:
- - 量化: https://huggingface.co/xiaomi-open-source/Xiaomi-MiMo-VL-Miloco-7B-GGUF
- - 未量化: https://huggingface.co/xiaomi-open-source/Xiaomi-MiMo-VL-Miloco-7B

- `modelscope`:
- - 量化: https://modelscope.cn/models/xiaomi-open-source/Xiaomi-MiMo-VL-Miloco-7B-GGUF
- - 未量化: https://modelscope.cn/models/xiaomi-open-source/Xiaomi-MiMo-VL-Miloco-7B

在`models`文件夹下，新建目录`MiMo-VL-Miloco-7B`，然后打开`modelspace`量化模型下载链接：

- 下载`MiMo-VL-Miloco-7B_Q4_0.gguf`放到`MiMo-VL-Miloco-7B`目录下
- 下载`mmproj-MiMo-VL-Miloco-7B_BF16.gguf`放到`MiMo-VL-Miloco-7B`目录下

### Qwen3-8B

如果机器显存够，也可以继续下载本地的规划模型，规划模型可以使用`Qwen-8B`模型，通过修改配置文件，也可以使用其它模型。

模型下载地址：

- `huggingface`：https://huggingface.co/Qwen/Qwen3-8B
- `modelscope`: https://modelscope.cn/models/Qwen/Qwen3-8B-GGUF/files

在`models`文件夹下，新建`Qwen3-8B`目录，然后打开上述下载链接，下载 Q4 量化版本即可：

- 下载`Qwen3-8B-Q4_K_M.gguf`放到`Qwen3-8B`目录下

## 运行

使用`docker compose`运行程序，复制`.env.example`，命名为`.env`，端口根据实际环境修改。

运行程序：

```Shell
# Pull 镜像
docker compose pull
# 卸载
docker compose down
# 启动
docker compose up -d
```

## 访问服务

通过`https://<your ip>:8000`访问服务，如果是本机访问， IP 为`127.0.0.1`；

> NOTICE:
>
> - 请使用 **https** 访问，而不是 **http**
> - WSL2 下，在 Windows 中可以尝试直接访问 WSL 的 IP 地址，如 `https://<wsl ip>:8000`
