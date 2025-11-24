# Runtime and Development Environment Setup (Windows Edition)

[Back to Previous Level](./environment-setup_zh-Hans.md)  

Language selection: [English](./environment-setup-windows.md) | [Simplified Chinese](./environment-setup-windows_zh-Hans.md)

------

The following tutorial uses Windows 11 25H2 + WSL 2.6.1 as an example.

## Environment Setup

System Requirements: Windows 11 22H2 or later + WSL2

### Enable Windows WSL2 Features

Please refer to the official Microsoft documentation first: [en](https://learn.microsoft.com/zh-cn/windows/wsl/install) | [中文](https://learn.microsoft.com/en-us/windows/wsl/install)  

- Search for and open **Control Panel** in the system, click **Programs > Turn Windows features on or off**, then check **Hyper-V** and **Windows Subsystem for Linux**, click OK, wait for the system to install and update, then restart.
- Install WSL: Search for **Terminal** in the system and open it, then run `wsl --install`. Wait for WSL installation to complete.  
  If already installed, run `wsl --update` to update to the latest version.
- Download a WSL2 Linux distribution:  
  - Open the built-in Microsoft Store, search for **Ubuntu**, then download **Ubuntu 24.04.1 LTS**  
  - Or in Terminal, run `wsl --list --online` to view available distros, then install via `wsl --install -d Ubuntu-24.04`
- Use WSL2:  
  - After downloading from Microsoft Store, click **Open** and follow the prompts to set username and password to initialize  
  - Or run `wsl -d Ubuntu-24.04` in Terminal and follow the prompts to set username and password to initialize

### Network Mode Configuration

Search for **WSL Setting** in the system, click **Network**, then change the network mode to **Mirrored**.

After modification, run `wsl --shutdown` to stop WSL, then run `wsl -d Ubuntu-24.04` to restart.

Check if the network configuration inside WSL matches the host by running `ip a`.

When using **Mirrored** mode, configure the Hyper-V firewall to allow inbound connections.

Run the following commands in a PowerShell window with administrator privileges to set Hyper-V firewall rules to allow inbound traffic:

```powershell
Set-NetFirewallHyperVVMSetting -Name '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' -DefaultInboundAction Allow
# Use the following command to get WSL firewall policy
Get-NetFirewallHyperVVMSetting -PolicyStore ActiveStore -Name '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}'
# Ensure DefaultInboundAction and DefaultOutboundAction are set to Allow:
# Name                  : {40E0AC32-46A5-438A-A0B2-2B479E8F2E90}
# Enabled               : True
# DefaultInboundAction  : Allow
# DefaultOutboundAction : Allow
# LoopbackEnabled       : True
# AllowHostPolicyMerge  : True
```

Related resources:

- [Accessing network applications with WSL](https://learn.microsoft.com/zh-cn/windows/wsl/networking)
- [Configure firewall](https://learn.microsoft.com/zh-cn/windows/security/operating-system-security/network-security/windows-firewall/hyper-v-firewall)

### Install Docker

Use the official installation script (although Docker Desktop is officially recommended for WSL2, you may skip the prompt and install directly with the commands below):

```shell
curl -fsSL https://get.docker.com | bash -s docker
# For users in Mainland China, you can use the Aliyun mirror
curl -fsSL https://get.docker.com | bash -s docker --mirror Aliyun
```

Add the current user to the docker group so you can run Docker without `sudo`:

```shell
sudo usermod -aG docker $USER
```

After adding, you must **log in again** for the group change to take effect.  
Verify installation with `docker --version`.

### Install CUDA Toolkit and NVIDIA Driver

Please refer to the official documentation: [CUDA Toolkit Download for WSL-Ubuntu](https://developer.nvidia.com/cuda-downloads?target_os=Linux&target_arch=x86_64&Distribution=WSL-Ubuntu&target_version=2.0&target_type=deb_network)  
You may also refer to the above **Linux environment setup** process.

### Install NVIDIA Container Toolkit

Refer to the [Linux Edition](./environment-setup-linux.md) setup process

## Environment Verification

Refer to the [Linux Edition](./environment-setup-linux.md) setup process