# Runtime and Development Environment Setup

Language selection: [English](./environment-setup.md) | [Simplified Chinese](./environment-setup_zh-Hans.md)

------

The service consists of two parts: the main service and the AI engine. The runtime environment requirements are as follows:

Main Service:
- System Requirements:
- - **Linux**: x86_64 + ARM64 architecture; Ubuntu 22.04 LTS or later is recommended
- - **Windows**: x86_64 + ARM64 architecture under **WSL2**; Windows 11 22H2 or later is recommended
- - **macOS**: x86_64 + ARM64 architecture

AI Engine:
- System Requirements:
- - **Linux**: x86_64 architecture; Ubuntu 22.04 LTS or later is recommended
- - **Windows**: x86_64 architecture under **WSL2**; Windows 11 22H2 or later is recommended
- - **macOS**: Not supported
- GPU Requirements:
- - **NVIDIA**: 30-series or above is recommended; 8GB or more VRAM; driver version 527.41 or above; CUDA version 12.5.1 or above
- - **AMD**: Not supported
- - **Intel**: Not supported
- - **MThreads**: Not supported

Software Requirements:
- **Python**: Python 3.10 or above
- **Docker**: Version 20.10 or above, with `docker compose` support
## Environment Setup
> 📄**NOTICE:**
>
> - When running via Docker, please follow the steps below to install the environment. If the environment is already installed and passes verification, you may skip this step; otherwise, the program may fail to run.
>
> - Notes for Windows:
>   - The camera only supports LAN streaming. Under Windows, WSL2's network mode should be set to **Mirrored**.
>   - After setting WSL2 to **Mirrored** mode, configure the Hyper-V firewall to allow inbound connections. Refresh the camera list; if the camera remains offline, try disabling the Windows firewall.
> - Notes for macOS:
>   - When running this service via Docker on macOS, you must configure the virtual machine network to **bridged mode** (refer to tutorial below), otherwise streaming will fail.
>   - By default, Docker Desktop uses NAT mode. Running the service in this mode will fail to stream. You may switch to bridged mode manually or follow the tutorial below.
>   - It is recommended to use bridged mode with a wired network adapter because macOS Wi-Fi drivers (and most wireless access points) do not allow two different MAC addresses on the same Wi-Fi link (one from your Mac, one from the VM).

### Linux
For Linux environment setup, see: [English](./environment-setup-linux.md) | [Simplified Chinese](./environment-setup-linux_zh-Hans.md)

### Windows
For Windows environment setup, see: [English](./environment-setup-windows.md) | [Simplified Chinese](./environment-setup-windows_zh-Hans.md)

### macOS (M series and Intel series)
For macOS environment setup, see: [English](./environment-setup-macos.md) | [Simplified Chinese](./environment-setup-macos_zh-Hans.md)

## Download Models
All the following operations are performed under the `models` directory.

### Xiaomi MiMo-VL-Miloco-7B
Xiaomi's self-developed multimodal model for local image inference.
Model download links:
- `huggingface`:
- - Quantized: https://huggingface.co/xiaomi-open-source/Xiaomi-MiMo-VL-Miloco-7B-GGUF
- - Non-quantized: https://huggingface.co/xiaomi-open-source/Xiaomi-MiMo-VL-Miloco-7B
- `modelscope`:
- - Quantized: https://modelscope.cn/models/xiaomi-open-source/Xiaomi-MiMo-VL-Miloco-7B-GGUF
- - Non-quantized: https://modelscope.cn/models/xiaomi-open-source/Xiaomi-MiMo-VL-Miloco-7B

Under the `models` directory, create a new folder `MiMo-VL-Miloco-7B`, then open the `modelscope` quantized model link:
- Download `MiMo-VL-Miloco-7B_Q4_0.gguf` and place it under the `MiMo-VL-Miloco-7B` folder
- Download `mmproj-MiMo-VL-Miloco-7B_BF16.gguf` and place it under the `MiMo-VL-Miloco-7B` folder

### Qwen3-8B

If your GPU memory is sufficient, you may also download the local planning model. The planning model can use the `Qwen-8B` model; other models can be used by modifying the configuration file.

Model download links:
- `huggingface`: https://huggingface.co/Qwen/Qwen3-8B
- `modelscope`: https://modelscope.cn/models/Qwen/Qwen3-8B-GGUF/files

Under the `models` directory, create a `Qwen3-8B` folder, then open the above download link and download the Q4 quantized version:
- Download `Qwen3-8B-Q4_K_M.gguf` and place it under the `Qwen3-8B` folder

## Run
Run the program using `docker compose`. Copy `.env.example` to `.env` and adjust the ports according to your environment.

Run the program:

```Shell
# Pull image
docker compose pull
# Remove containers
docker compose down
# Start containers
docker compose up -d
```
## Access the Service

Access the service via `https://<your ip>:8000`. For local access, IP is `127.0.0.1`;

> 📄NOTICE:
>
> - Please use **https** instead of **http**
> - On Windows, you can try accessing WSL's IP directly, e.g., `https://<wsl ip>:8000`
> - On macOS, if the network mode is bridged, please use the IP address of the VM running Docker.
