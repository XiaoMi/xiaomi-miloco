# Runtime and Development Environment Setup (Linux Edition)

[Back to Previous Level](./environment-setup.md)

Language selection: [English](./environment-setup-linux.md) | [Simplified Chinese](./environment-setup-linux_zh-Hans.md)

------

The following tutorial uses Ubuntu 24.04 LTS as an example. For other Linux distributions, please adjust the commands accordingly.

## Environment Setup

### Install Docker

Use the official installation script:

```shell
curl -fsSL https://get.docker.com | bash -s docker
# For users in Mainland China, you can specify the Aliyun mirror for installation
curl -fsSL https://get.docker.com | bash -s docker --mirror Aliyun
```

You can add the current user to the docker group, allowing you to run docker commands directly:

```shell
sudo usermod -aG docker $USER
```

After adding, you need to **log in again** for the group changes to take effect.  
Verify the installation by running `docker --version`.

### Install CUDA Toolkit and NVIDIA Driver

Refer to the official documentation [CUDA Toolkit Downloads](https://developer.nvidia.com/cuda-downloads). Select the corresponding version based on your system, then follow the instructions to install:

```shell
# Updated on 25-11-1
# Install CUDA Toolkit
wget https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/cuda-keyring_1.1-1_all.deb
sudo dpkg -i cuda-keyring_1.1-1_all.deb
sudo apt-get update
# Optional installation for compilation
sudo apt-get -y install cuda-toolkit-13-0
# Install NVIDIA Driver, choose one; cuda-drivers is recommended
sudo apt-get -y install nvidia-open
sudo apt-get -y install cuda-drivers
```

When installing the CUDA Toolkit using the above method, CUDA environment variables may not be automatically added. You can append the following lines to `~/.bashrc` or `~/.zshrc` (depending on your shell):

```shell
export PATH="/usr/local/cuda/bin:${PATH:-}"
export LD_LIBRARY_PATH="/usr/local/cuda/lib64:${LD_LIBRARY_PATH:-}"
```

### Install NVIDIA Container Toolkit

Refer to the official documentation [NVIDIA Container Toolkit Installation](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html#next-steps). Select the corresponding version according to your system, then follow the instructions to install:

```shell
# Updated on 25-11-1
# Configure the download source
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

## Environment Verification

### Verify Docker

Use the `hello-world` image to verify if Docker has been installed successfully. If you see `Hello from Docker!`, it means the installation was successful.

```shell
docker run hello-world
# After verification, you may remove the image
docker rmi hello-world
```

### Verify NVIDIA GPU Driver

Run the `nvidia-smi` command to verify if the NVIDIA driver has been installed successfully. If it displays GPU driver and CUDA Toolkit information, the installation is successful.

Run the `nvcc --version` command to verify if the NVIDIA CUDA Toolkit is installed successfully. If installed, the version information will be displayed.

### Verify NVIDIA Container Toolkit

Run the following command to verify if the NVIDIA Container Toolkit has been installed successfully. If it displays GPU driver and CUDA Toolkit information, the installation is successful.

```shell
docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi
# After verification, you may remove the image
docker rmi nvidia/cuda:12.4.0-base-ubuntu22.04
```