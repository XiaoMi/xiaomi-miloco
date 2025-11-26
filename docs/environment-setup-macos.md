# Runtime and Development Environment Setup (macOS Edition)

[Back to Previous Level](./environment-setup.md)  

Language selection: [English](./environment-setup-macos.md) | [Simplified Chinese](./environment-setup-macos_zh-Hans.md)

------

On macOS (both M series and Intel series), the service can be run in two ways:

- Clone the code from GitHub and run directly
- Run via Docker inside a virtual machine

The following tutorial takes macOS Tahoe 26.1 as an example and demonstrates how to run this service with Docker. Cloning the code and running the service directly has already been introduced in the development guide, so it will not be elaborated here.

## Environment Setup

>  Docker on macOS runs via a virtual machine for the following reasons:
>
> - Docker containers rely on **Linux kernel features (cgroups, namespaces, overlayfs, etc.)**.
> - The macOS kernel is **XNU**, which does not support the Linux kernel API and cannot directly execute ELF-format Linux binaries.
>
> Therefore, on macOS, a **Linux virtual machine** (VM) must be started first, and Docker containers run inside that VM.  
>
> This tutorial provides two approaches for running the service in Docker:
> - From the command line: using **Multipass + Docker** (steps detailed below)
> - Install VMware Fusion (Free Edition) and configure the network mode to **Bridged**
> 
> Once the VM environment is set up, you can refer to the [Linux Edition](./environment-setup-linux_zh-Hans.md) tutorial to run the service inside VM.

In this tutorial, we will introduce how to run the service using **Multipass + Docker**.

### Basic Setup

On macOS, package management is typically done via **Homebrew (brew)**. Open Terminal and run the following script to install `brew` (if it is already installed, you may skip this step):

```shell
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

During installation, you may encounter the following prompts:

- Whether to install Xcode Command Line Tools (**Required**, follow the prompt)
- Installation path (Intel Mac defaults to `/usr/local`, Apple Silicon defaults to `/opt/homebrew`)
- You will be prompted to add the `brew` path to your `PATH`. For example, on Apple M series:

```shell
echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile
eval "$(/opt/homebrew/bin/brew shellenv)"
```

For users in mainland China, you can configure mirror sources for faster downloads:

```shell
# Replace brew.git repository source
git -C "$(brew --repo)" remote set-url origin https://mirrors.tuna.tsinghua.edu.cn/git/homebrew/brew.git
# Replace core.git repository source
git -C "$(brew --repo homebrew/core)" remote set-url origin https://mirrors.tuna.tsinghua.edu.cn/git/homebrew/homebrew-core.git
# Replace bottles source (cache binary packages for faster downloads)
echo 'export HOMEBREW_BOTTLE_DOMAIN=https://mirrors.tuna.tsinghua.edu.cn/homebrew-bottles' >> ~/.zprofile
source ~/.zprofile
```

Install `multipass` and `qemu`:

```shell
brew install multipass qemu
```

### Virtual Machine Configuration

Docker Desktop's default network mode is NAT (Network Address Translation). Running the service directly in Docker Desktop may result in streaming failures.  
Therefore, this tutorial uses `multipass` to create a virtual machine and bridge its network to the physical network.

First, identify your network interface. It is recommended to use a wired interface; wireless interfaces may cause the process to hang because macOS's Wi-Fi hardware drivers (and most wireless access points) do not allow two different MAC addresses on the same Wi-Fi connection (your Mac and the VM).

Check network interfaces:

```shell
multipass networks
```

On Mac Mini M4, `en0` is the wired Ethernet interface:

```shell
Name   Type       Description
en0    ethernet   Ethernet
en1    wifi       Wi-Fi
en5    ethernet   Ethernet Adapter (en5)
en6    ethernet   Ethernet Adapter (en6)
en7    ethernet   Ethernet Adapter (en7)
```

Create a virtual machine named `bridge-docker` and specify the bridge network (`--network`):

```shell
# Syntax: multipass launch --name [name] --network [your physical NIC] --cpus [CPU cores] --mem [Memory] --disk [Disk space]
multipass launch --name bridge-docker --network en0 --cpus 4 --mem 4G --disk 30G
```

After the VM starts, check its IP address:

```bash
multipass list
```

You should see that the VM's IP address is in the same subnet as your Mac. If it is not, go through the above steps to troubleshoot.

### Install Docker

You can enter the VM with:

```shell
multipass shell bridge-docker
```

At this point, a bridged-network-mode virtual machine is set up on macOS. The next step is to configure the service runtime environment in Linux (ARM64). You can refer to the [Linux Edition](./environment-setup-linux_zh-Hans.md) for detailed steps. We will not repeat them here.

## Environment Verification

1. After entering the macOS VM, ensure that it is in the same subnet as the host. Otherwise, camera streaming will not work properly.
2. Since there is currently no Linux (ARM64) + NVIDIA GPU setup available, GPU passthrough and automatic installation script functionality cannot be verified at this time. Developers are welcome to contribute technical support in this area.