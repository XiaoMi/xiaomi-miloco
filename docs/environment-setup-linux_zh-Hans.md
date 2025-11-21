# 运行和开发环境配置（Linux 篇）

[返回上一级](./environment-setup_zh-Hans.md)

选择语言：[English](./environment-setup-linux.md) | [简体中文](./environment-setup-linux_zh-Hans.md)

------



下述教程以 Ubuntu 24.04 LTS 为例，其它 Linux 发行版请自行修改命令。

## 环境配置

### 安装 Docker

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

### 安装 CUDA Toolkit 和 NVIDIA Driver

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

### 安装 NVIDIA Container Toolkit

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

## 环境验证

### 验证 Docker

使用`hello-world`镜像验证 Docker 是否安装成功，如果显示`Hello from Docker!`则表示安装成功。
```shell
docker run hello-world
# 验证完成后，可移除镜像
docker rmi hello-world
```
### 验证 NVIDIA 显卡驱动

使用命令`nvidia-smi`验证 NVIDIA Driver 是否安装成功，如果显示显卡驱动和 CUDA 工具包信息，则表示安装成功。

使用命令`nvcc --version`验证 NVIDIA CUDA Toolkit 是否安装成功，如果安装成功，会显示版本信息。

### 验证 NVIDIA Container Toolkit

使用下述命令验证 NVIDIA Container Toolkit 是否安装成功，如果显示显卡驱动和 CUDA 工具包信息，则表示安装成功。
```shell
docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi
# 验证完成后，可移除镜像
docker rmi nvidia/cuda:12.4.0-base-ubuntu22.04
```
