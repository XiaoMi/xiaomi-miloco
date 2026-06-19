# Miloco MCP Server (Hermes Agent)

将小米 Miloco 全屋智能后端的 REST API 包装为 MCP 工具，供 Hermes Agent 调用，实现设备控制、摄像头感知、任务管理等功能。

---

## Hermes 一键安装

> **前置条件：已安装 [OpenClaw](https://openclaw.ai)（含 Hermes Agent），版本 >= 2026.5.2。**
> 将以下提示词逐条复制粘贴给 Hermes，即可自主完成全部安装。

### 提示词 1 — 确认项目位置

```text
请帮我确认 xiaomi-miloco 项目的文件位置：
1. 检查当前目录下是否存在 plugins/hermes-mcp 目录
2. 如果不存在，在 home 目录下搜索 xiaomi-miloco 仓库
3. 找到后告诉我完整路径，后续步骤需要用到
```

### 提示词 2 — 安装 Miloco 本体

```text
帮我安装 Miloco 插件：https://raw.githubusercontent.com/XiaoMi/xiaomi-miloco/main/scripts/install-guide.md
```

> 安装脚本会自动完成：环境检查、依赖安装、服务启动、账号绑定、模型配置。
> 安装完成后执行 `miloco-cli service status` 确认服务已 running。

### 提示词 3 — 安装 MCP 插件并注册到 Hermes

```text
请完成 Miloco MCP 插件的安装与注册：
1. 进入 xiaomi-miloco/plugins/hermes-mcp 目录，执行 pip install -e .
2. 确认 miloco-mcp-server 可用：python3 -c "import miloco_mcp"
3. 将以下 MCP Server 配置写入 ~/.hermes/config.yaml 的 mcp_servers 下（cwd 替换为实际的 plugins/hermes-mcp 绝对路径）：

mcp_servers:
  miloco:
    command: python3
    args: ["-m", "miloco_mcp.server"]
    cwd: "{实际路径}/plugins/hermes-mcp"
    env:
      MILOCO_BASE_URL: "http://127.0.0.1:1810"
    timeout: 60
    connect_timeout: 30

4. 执行 /mcp reload 使配置生效
5. 完成后用 device_list 工具测试是否能正常列出设备
```

> Token 会自动从 `~/.openclaw/miloco/config.json` 读取，无需手动配置。

---

## 架构

```
Hermes Agent ──MCP(stdio)──▶ miloco-mcp-server ──REST──▶ miloco backend (:1810)
```

Hermes Agent 通过 stdin/stdout（stdio 模式）与 MCP Server 通信，Server 将工具调用转发为 HTTP 请求到 Miloco 后端。

## 新增依赖

本项目在 Miloco 主项目之外，新增以下 Python 依赖（定义在 `pyproject.toml`）：

| 包名 | 版本要求 | 用途 |
|------|---------|------|
| `fastmcp` | >= 2.0 | MCP Server 框架，提供工具注册、stdio/HTTP 传输 |
| `httpx` | >= 0.27 | 异步 HTTP 客户端，调用 Miloco REST API |
| `pydantic` | >= 2.8 | 数据校验与序列化 |
| `pydantic-settings` | >= 2.0 | 从环境变量 / `.env` 文件加载配置 |
| `python-dotenv` | >= 1.0 | `.env` 文件解析 |

开发依赖（可选）：`pytest>=8.0`、`pytest-asyncio>=0.23`

## 手动安装

如不使用 Hermes 自动安装，可手动执行：

```bash
cd plugins/hermes-mcp
pip install -e .
```

或使用 venv 隔离环境：

```bash
cd plugins/hermes-mcp
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## 配置

### 方式一：自动读取（推荐）

MCP Server 启动时会自动从 `~/.openclaw/miloco/config.json` 读取 `server.token`，无需额外配置。

代码逻辑见 `config.py`：

```python
cfg_path = Path.home() / ".openclaw" / "miloco" / "config.json"
data = json.loads(cfg_path.read_text())
token = data["server"]["token"]
```

如果设置了 `MILOCO_HOME` 环境变量，则从 `$MILOCO_HOME/config.json` 读取。

### 方式二：环境变量 / .env 文件

复制 `.env.example` 为 `.env` 并编辑：

```bash
cp .env.example .env
```

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `MILOCO_BASE_URL` | `http://127.0.0.1:1810` | Miloco 后端地址 |
| `MILOCO_TOKEN` | (自动读取) | Bearer Token，留空则从 miloco config 自动加载 |
| `MILOCO_TIMEOUT` | `30` | 请求超时（秒） |
| `MILOCO_TLS_VERIFY` | `false` | TLS 证书验证（Miloco 默认 HTTP，保持 false） |

## 运行模式

### Stdio 模式（Hermes 默认）

```bash
python3 -m miloco_mcp.server
```

Hermes 通过 stdin/stdout 与 MCP Server 通信，无需额外端口。

### HTTP 模式（调试用）

```bash
python3 -m miloco_mcp.server --http --port 8001
```

## Agent 工作原理

### 配置文件读取链路

```
config.py
  ├─ 读取 $MILOCO_HOME/config.json  （如设置了 MILOCO_HOME）
  └─ 否则读取 ~/.openclaw/miloco/config.json
       └─ 提取 server.token 作为 API Bearer Token
```

优先级：`MILOCO_TOKEN` 环境变量 > `.env` 文件 > miloco config.json 自动读取。

### 工具注册链路

```
server.py  create_server()
  └─ tools/__init__.py  register_all_tools(server)
       ├─ tools/devices.py      设备查询与控制
       ├─ tools/perception.py   摄像头感知
       ├─ tools/tasks.py        任务与规则管理
       └─ tools/home.py         家庭档案与成员
```

每个 tool 模块创建一个 `FastMCP` 子实例，通过 `server.mount()` 挂载到主 server。Hermes Agent 调用工具时，MCP Server 将请求转发到 Miloco 后端 REST API。

### 设备控制 API 格式

```json
POST /api/miot/devices/{did}/control
{
  "type": "set_properties",
  "properties": [
    {"iid": "prop.2.1", "value": false}
  ]
}
```

- `iid` 格式: `prop.{siid}.{piid}`，如 `prop.2.1` 表示开关
- `type` 取值: `set_properties`（设置属性）/ `call_action`（调用动作）

## 使用示例

在 Hermes 中直接对话：

```
帮我把客厅灯打开
看看门口有没有人
把空调调到26度制冷
关闭卧室所有灯
每天提醒我喝水
查看家里现在什么情况
```

## MCP 工具一览

### 设备控制
| 工具 | 说明 |
|------|------|
| `device_list` | 列出所有米家设备 |
| `device_spec` | 查看设备能力定义 |
| `device_status` | 读取设备属性 |
| `device_control` | 控制设备（设置属性 / 调用动作） |
| `scene_list` | 列出手动场景 |
| `scene_trigger` | 触发场景 |
| `home_info` | 家庭概况 |
| `user_info` | 当前小米账号信息 |

### 摄像头感知
| 工具 | 说明 |
|------|------|
| `perceive` | 单次视觉/音频感知 |
| `perception_engine_status/start/stop` | 管理持续感知引擎 |
| `perception_logs` | 历史感知日志 |
| `perception_cameras` | 列出摄像头及感知状态 |
| `camera_perception_enable/disable` | 开关单个摄像头感知 |

### 任务与规则
| 工具 | 说明 |
|------|------|
| `task_summary` | 任务列表摘要 |
| `task_get` | 任务详情 |
| `task_update` | 更新任务属性 |
| `task_enable/disable` | 启用/禁用任务 |
| `task_delete` | 删除任务 |
| `rule_logs` | 规则引擎触发日志 |
| `rule_trigger` | 手动触发规则 |
| `task_record_get` | 获取任务累加记录 |
| `task_record_increment` | 递增任务计数器 |
| `task_record_event_append` | 追加事件到任务记录 |

### 家庭档案与成员
| 工具 | 说明 |
|------|------|
| `person_list/create/update/delete` | 家庭成员增删改查 |
| `person_samples` | 查看已注册的人脸/人体样本 |
| `home_profile_list/rendered` | 查看家庭档案 |
| `home_profile_write/commit` | 写入并提交档案条目 |

### 系统
| 工具 | 说明 |
|------|------|
| `miloco_status` | 系统状态 |
| `token_usage_summary/daily` | LLM Token 用量 |

## 故障排查

### 连接不上 Miloco Backend

```bash
# 检查后端是否运行
miloco-cli service status

# 检查端口
ss -tlnp | grep 1810

# 测试 API
curl http://127.0.0.1:1810/
```

### 设备控制返回 422

确保请求格式为 `set_properties` + `iid` 格式，而非旧版 `set_property` + `siid/piid`。

### Token 错误

```bash
# 查看 token
miloco-cli config get server.token

# 或直接看配置
cat ~/.openclaw/miloco/config.json
```

## 文件结构

```
plugins/hermes-mcp/
├── .env.example          # 环境变量模板
├── pyproject.toml        # Python 项目配置（依赖定义）
├── README.md             # 本文档
└── src/miloco_mcp/
    ├── __init__.py
    ├── config.py         # 配置加载（自动读取 miloco config.json）
    ├── client.py         # HTTP 客户端（封装 REST 调用）
    ├── server.py         # MCP Server 入口（stdio / HTTP 模式）
    └── tools/
        ├── __init__.py   # 工具注册（mount 各子模块）
        ├── devices.py    # 设备查询与控制
        ├── home.py       # 家庭档案、成员管理
        ├── perception.py # 摄像头感知
        └── tasks.py      # 家庭任务管理
```

## 许可证

MIT
