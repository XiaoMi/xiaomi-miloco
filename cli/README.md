# miloco-cli

Command-line interface for Xiaomi Miloco — manage the backend service, devices, and configuration.

## Install

miloco-cli 随 Miloco 一起安装，不单独发布到 PyPI。运行官方安装脚本即可（脚本会从 GitHub Release 下载本平台归档并本地安装）：

```bash
curl -LsSf https://github.com/XiaoMi/xiaomi-miloco/releases/latest/download/install.sh | bash
```

## Quick Start

```bash
# Start miloco server
miloco-cli service start

# List connected devices
miloco-cli device list --pretty

# Configure
miloco-cli config set model.omni.api_key sk-xxxxx
miloco-cli config show
```

## Commands

| Command | Description |
|---------|-------------|
| `service start/stop/status/logs` | Manage the backend service |
| `device list/control` | Interact with IoT devices |
| `config show/get/set` | View and modify configuration |
| `account bind` | Bind Xiaomi account via OAuth |

## Configuration

Config file: `$MILOCO_HOME/config.json` (default `~/.openclaw/miloco/config.json`).

Override with environment variables using `MILOCO_` prefix and `__` for nesting:

```bash
MILOCO_SERVER__URL=https://192.168.1.100:1810 miloco-cli service status
```

### IoT Observe sidecar

IoT 事实旁路默认关闭；更新代码不会自动启用。部署新版后，在对应宿主的
`MILOCO_HOME` 下启用：

```bash
miloco-cli config set iot_observation.enabled true
miloco-cli config get iot_observation.enabled
miloco-cli observe batch
```

`config set` 会在后端运行中尝试重启使配置生效；若服务未运行，需要执行
`miloco-cli service start`。使用 `--no-restart` 写入配置时，需自行重启后端。
`observe` 支持 `batch` / `submit`，与 `miloco-home-observe` Skill 和后端批次契约一致。

`batch` 只读取事实，不推进水位，也不触发 Agent 分析。`disabled` 表示旁路未启用；
`empty` 表示当前没有可读的新事件。共性总结与提交由家庭知识 Observe 执行，
不是每轮家庭巡检执行；需要等待或通过宿主触发相应任务。旁路不自动晋升正式档案、
发送通知或控制设备。

## License

For license details, please see [LICENSE.md](https://raw.githubusercontent.com/XiaoMi/xiaomi-miloco/main/LICENSE.md).

**Important Notice**: This project is limited to non-commercial use only. Without written authorization from Xiaomi Corporation, this project may not be used for developing applications, web services, or other forms of software.
