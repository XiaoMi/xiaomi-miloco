# Runtime and Development Environment Setup

[English](./environment-setup.md) | [简体中文](./environment-setup_zh-Hans.md)

Runtime Environment Requirements:
- System Requirements:
  - **Linux**: x64 architecture, Ubuntu 22.04 LTS or higher is recommended
  - **Windows**: x64 architecture, Windows 11 22H2 or higher is recommended; WSL2 support is required
  - **macOS**: ARM architecture, currently not supported
- GPU Requirements (The project's AI Engine requires GPU support)
  - **NVIDIA**: GeForce RTX 30 series or newer recommended, with at least 8 GB VRAM
  - **AMD**: Currently not supported
  - **Intel**: Currently not supported
  - **MThreads**: Currently not supported
- Software Requirements:
  - **Docker**: Version 20.10 or higher, with `docker compose` support required

## Environment Setup

> NOTICE:
>
> - If running via Docker, follow the steps below to install the environment. If the environment is already installed and passes verification, you may skip the setup steps; otherwise, the program may fail to run.
> - The camera only allows streaming within the local area network. On Windows, you need to set the WSL2 network mode to **Mirrored**.
> - After setting the WSL2 network to **Mirrored** mode, make sure to configure the Hyper-V firewall to allow inbound connections. Refresh the camera list; if it still shows as offline, you can try disabling the Windows firewall.

### Linux
The following instructions use **Ubuntu 24.04 LTS** as an example. For other Linux distributions, change the commands accordingly.
#### Environment Setup
##### Install Docker
Use the official installation script:
```shell
curl -fsSL https://get.docker.com | bash -s docker
# For users in mainland China, you can specify the Aliyun mirror
curl -fsSL https://get.docker.com | bash -s docker --mirror Aliyun
```
Add the current user to the `docker` group so you can use Docker commands without `sudo`:
```shell
sudo usermod -aG docker $USER
```
After adding, you need to **log out and log back in** for the group change to take effect.  
Run `docker --version` to verify successful installation.

##### Install CUDA Toolkit and NVIDIA Driver
Refer first to the official documentation: [CUDA Toolkit Downloads](https://developer.nvidia.com/cuda-downloads). Select the version matching your system and install accordingly:
```shell
# Updated on 25-11-1
# Install CUDA Toolkit
wget https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/cuda-keyring_1.1-1_all.deb
sudo dpkg -i cuda-keyring_1.1-1_all.deb
sudo apt-get update
# Optional installation, used for compilation
sudo apt-get -y install cuda-toolkit-13-0
# Install NVIDIA Driver, choose any; cuda-drivers is recommended
sudo apt-get -y install nvidia-open
sudo apt-get -y install cuda-drivers
```
When installing the CUDA Toolkit this way, CUDA environment variables may not be added automatically.  
Append the following to `~/.bashrc` or `~/.zshrc` (depending on your shell):
```shell
export PATH="/usr/local/cuda/bin:${PATH:-}"
export LD_LIBRARY_PATH="/usr/local/cuda/lib64:${LD_LIBRARY_PATH:-}"
```
##### Install NVIDIA Container Toolkit
Refer first to the official documentation: [NVIDIA Container Toolkit Installation](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html#next-steps). Select the matching version for your system and install:
```shell
# Updated on 25-11-1
# Configure download source
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
#### Environment Verification
##### Verify Docker
Use the `hello-world` image to check if Docker is properly installed. If it shows `Hello from Docker!`, installation was successful.
```shell
docker run hello-world
# You may remove the image after verification
docker rmi hello-world
```
##### Verify NVIDIA GPU Driver
Run `nvidia-smi` to verify the NVIDIA driver installation. If GPU and CUDA toolkit info is displayed, installation was successful.  
Run `nvcc --version` to check CUDA Toolkit installation; version info should be displayed if successful.
##### Verify NVIDIA Container Toolkit
Run the following command to check installation. If GPU and CUDA toolkit info is displayed, installation was successful:
```shell
docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi
# You may remove the image after verification
docker rmi nvidia/cuda:12.4.0-base-ubuntu22.04
```
### Windows
The following guide uses **Windows 11 25H2 + WSL 2.6.1** as an example.
#### Environment Setup
System requirements: Windows 11 22H2 or above + WSL2
##### Enable WSL2
Refer to Microsoft’s official guide: [English](https://learn.microsoft.com/zh-cn/windows/wsl/install) | [Chinese](https://learn.microsoft.com/en-us/windows/wsl/install)
- Search for and open Control Panel → Programs → Turn Windows features on or off → check **Hyper-V** and **Windows Subsystem for Linux**, click OK, wait for installation, then restart.
- Install WSL: search for "Terminal", open it, run `wsl --install` and wait for installation; if already installed, run `wsl --update` to update to the latest version.
- Download a WSL2 Linux distribution:
  - Open Microsoft Store, search "Ubuntu", install **Ubuntu 24.04.1 LTS**
  - Or check available distributions with `wsl --list --online`, then run `wsl --install -d Ubuntu-24.04`
- Using WSL2:
  - After installation from Store, click **Open** and follow the prompts to set username/password.
  - Or run `wsl -d Ubuntu-24.04` in Terminal and follow prompts to set username/password.
##### Network Mode Configuration
Search for **WSL Setting**, open **Network**, change network mode to **Mirrored**. After changing, run `wsl --shutdown` to stop WSL, then restart with `wsl -d Ubuntu-24.04`.  
Run `ip a` to check whether the subnet matches the host machine.

After setting to **Mirrored** mode, you need to configure the Hyper-V firewall to allow inbound connections.  

In a PowerShell window run the following commands with administrator privileges to configure the Hyper-V firewall settings so that inbound connections are allowed:  
```powershell
Set-NetFirewallHyperVVMSetting -Name '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' -DefaultInboundAction Allow
# Use the following command to get the WSL firewall policy
Get-NetFirewallHyperVVMSetting -PolicyStore ActiveStore -Name '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}'
# DefaultInboundAction and DefaultOutboundAction should both be set to Allow:
# Name                  : {40E0AC32-46A5-438A-A0B2-2B479E8F2E90}
# Enabled               : True
# DefaultInboundAction  : Allow
# DefaultOutboundAction : Allow
# LoopbackEnabled       : True
# AllowHostPolicyMerge  : True
```
References:  
- [Accessing network applications with WSL](https://learn.microsoft.com/en-us/windows/wsl/networking)
- [Configure the firewall](https://learn.microsoft.com/en-us/windows/security/operating-system-security/network-security/windows-firewall/hyper-v-firewall)

##### Install Docker
Use the official script (WSL official recommendation is Docker Desktop, but you can ignore the prompt and install directly):
```shell
curl -fsSL https://get.docker.com | bash -s docker
# For users in mainland China, you can specify the Aliyun mirror
curl -fsSL https://get.docker.com | bash -s docker --mirror Aliyun
```
Add the current user to the `docker` group:
```shell
sudo usermod -aG docker $USER
```
After adding, **log out and log back in** to apply changes.  
Run `docker --version` to verify installation.
##### Install CUDA Toolkit and NVIDIA Driver
Refer to the official guide: [CUDA Toolkit Download for WSL-Ubuntu](https://developer.nvidia.com/cuda-downloads?target_os=Linux&target_arch=x86_64&Distribution=WSL-Ubuntu&target_version=2.0&target_type=deb_network)  
You can also follow the Linux installation steps above.
##### Install NVIDIA Container Toolkit
Same process as Linux.
#### Environment Verification
Refer to the Linux verification steps above.
### macOS (M Series & Intel Series)
Currently not supported.
## Model Downloads
All steps below are to be performed in the `models` folder.
### Xiaomi MiMo-VL-Miloco-7B
Xiaomi’s self-developed multimodal model for local image inference.  
Download links:
- `huggingface`:
- - Quantized: https://huggingface.co/xiaomi-open-source/Xiaomi-MiMo-VL-Miloco-7B-GGUF
- - Non-quantized: https://huggingface.co/xiaomi-open-source/Xiaomi-MiMo-VL-Miloco-7B
- `modelscope`:
- - Quantized: https://modelscope.cn/models/xiaomi-open-source/Xiaomi-MiMo-VL-Miloco-7B-GGUF
- - Non-quantized: https://modelscope.cn/models/xiaomi-open-source/Xiaomi-MiMo-VL-Miloco-7B
In the `models` folder, create a new directory named `MiMo-VL-Miloco-7B`, then open the `modelspace` quantized model download link:
- Download `MiMo-VL-Miloco-7B_Q4_0.gguf` and place it into the `MiMo-VL-Miloco-7B` directory
- Download `mmproj-MiMo-VL-Miloco-7B_BF16.gguf` and place it into the `MiMo-VL-Miloco-7B` directory
### Qwen3-8B
If your GPU memory is sufficient, you can also download a local planning model, such as `Qwen-8B`. You can modify the config file to use other models as well.  
Download links:
- `huggingface`: https://huggingface.co/Qwen/Qwen3-8B
- `modelscope`: https://modelscope.cn/models/Qwen/Qwen3-8B-GGUF/files
Create a folder `Qwen3-8B` under `models`, open the above links, and download the Q4 quantized version:
- Download `Qwen3-8B-Q4_K_M.gguf` into the `Qwen3-8B` folder
## Run
Use `docker compose` to run the program. Copy `.env.example` to `.env`, modify ports as needed.  
Run the program:
```shell
# Pull images
docker compose pull
# Stop and remove containers
docker compose down
# Start
docker compose up -d
```
## Access Service
Access via `https://<your ip>:8000`.  
If accessing on localhost, use IP `127.0.0.1`.
> NOTICE:
>
> - Use **https** instead of **http**
> - Under WSL2, you can try directly accessing the WSL IP from Windows, e.g. `https://<wsl ip>:8000`
