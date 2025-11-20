# Xiaomi Miloco

**Xiaomi Local Copilot** is a future exploration solution for smart homes. Using Xiaomi Home cameras as the source of visual information and a self-developed LLM as its core, it connects all IoT devices throughout the house. Based on the development paradigm of LLM, it enables users to define various family needs and rules in natural language, achieving broader and more creative smart device integration.

<div align="center">

English | [简体中文](README_zh_Hans.md)

</div>

## News

- [2025-11] Xiaomi Miloco Framework Open Source

## Key Features

1. New Interaction Paradigm: Based on the development paradigm of LLM, rule-setting and complex device command control can be completed through natural language interaction.
2. New Use for Visual Data: Using camera data streams as a source of perceptual information, the LLM is used to analyze various home scene events contained in the visual data to respond to user queries.
3. On-Device LLM: The home scene tasks are split into two stages: planning and visual understanding. It provides Xiaomi's self-developed on-device model to realize on-device video understanding and ensure family privacy and security.
4. Xiaomi Home Ecosystem: It connects with the Xiaomi Home ecosystem, supports the retrieval and execution of Mi Home devices and scenes, and supports sending customized content for Xiao Home notifications.

    <img src="assets/images/ai_center.jpg" width="60%" />

## Quick Start

### System Requirements

- **Hardware Requirements**
```Plain Text
CPU: x64 architecture
Graphics Card: NVIDIA 30 series and above, 8GB VRAM minimum (recommended 12GB and above)
Storage: Recommended 16GB or more available space (for local model storage)
```

- **Software Requirements**
```Plain Text
Operating System:
  - Linux: x64 architecture, recommended Ubuntu 22.04 and above LTS versions
  - Windows: x64 architecture, recommended Windows 10 and above, requires WSL2 support
  - macOS: Not currently supported
Docker: Version 20.10 and above, requires docker compose support
NVIDIA Driver: NVIDIA driver with CUDA support
NVIDIA Container Toolkit: For Docker GPU support
```

### Install

> **Note**: Please ensure your system meets the above hardware and software requirements. Windows systems need to enter the WSL environment.

**Install with Docker**  
One-click installation via command line
```bash
bash -c "$(wget -qO- https://xiaomi-miloco.cnbj1.mi-fds.com/xiaomi-miloco/install.sh)"
```
Or download the source code first, then execute the one-click installation script:
```bash
git clone https://github.com/XiaoMi/xiaomi-miloco.git

bash scripts/install.sh
```
For detailed installation steps, please refer to the [Docker Deployment Documentation](docs/environment-setup.md).

**Install with source code**  
For source code installation steps, please refer to the [Development Guide](docs/development/developer-setup.md).

## Usage Documentation

Please refer to the [Usage Documentation](docs/usage/README.md).

## Contributing

Please refer to the [Contributing Guide](CONTRIBUTING.md).

## License

For license details, please see [LICENSE.md](LICENSE.md).

**Important Notice**: This project is limited to non-commercial use only. Without written authorization from Xiaomi Corporation, this project may not be used for developing applications, web services, or other forms of software.

## Security Issues

If you discover potential security issues in this project, or believe you may have found a security issue, please notify the [Miloco Team](xiaomi-miloco@xiaomi.com) via our vulnerability reporting email. Please do not create public GitHub Issues.

## Contact Us

### Issue Reporting

For issue reporting, please participate through the following methods:
- Submit a [GitHub Issue](https://github.com/XiaoMi/xiaomi-miloco/issues/new/)

### Technical Discussion

- GitHub [Discussions](https://github.com/XiaoMi/xiaomi-miloco/discussions/)
- Project Discussion Group (WeChat):

  <img src="assets/images/miloco_wechat_6.jpg" width="30%" />  <img src="assets/images/miloco_wechat_group_12.jpeg" width="30%" />


### Join Us

The **Xiaomi Miloco** team is hiring. Send your resume to `xiaomi-miloco@xiaomi.com`, and it will be delivered directly to the project lead.

## Acknowledgments

Thank you to the original team members who worked hard for Miloco：zhaoy、yangyongjie、xx、Changyu、yyk、junhui、郭兴宝、47、afei。

Your passion and talent are the fundamental driving force behind Miloco's continuous innovation and progress.

Special thanks to:
- The [llama.cpp](https://github.com/ggml-org/llama.cpp) open source project for providing inference backend capabilities
