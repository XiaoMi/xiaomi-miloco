# Miloco MCP Server — Hermes Agent 部署说明

## 简介

将小米 Miloco 全屋智能后端的 REST API 包装为 MCP (Model Context Protocol) 工具，供 Hermes Agent 直接调用，实现语音/文字控制米家设备、摄像头感知、家庭任务管理等功能。

## 架构

```
Hermes Agent ──MCP(stdio)──▶ miloco-mcp-server ──REST──▶ miloco backend (:1810)
```

## 前置条件

1. **Miloco Backend 已部署并运行**（端口 1810）
   - 安装: `curl -LsSf https://github.com/XiaoMi/xiaomi-miloco/releases/latest/download/install.sh | bash`
   - 或源码: `bash scripts/install.sh --dev`
   - 确认: `miloco-cli service status` 显示 running

2. **Python >= 3.10**

3. **Hermes Agent 已安装**

## 安装

```bash
cd plugins/hermes-mcp
pip install -e .
```

或使用 venv:

```bash
cd plugins/hermes-mcp
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## 配置

### 方式一：自动读取（推荐）

MCP Server 会自动从 `~/.openclaw/miloco/config.json` 读取 token，无需额外配置。

### 方式二：环境变量

复制 `.env.example` 为 `.env` 并编辑:

```bash
cp .env.example .env
```

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `MILOCO_BASE_URL` | `http://127.0.0.1:1810` | Miloco 后端地址 |
| `MILOCO_TOKEN` | (自动读取) | Bearer Token |
| `MILOCO_TIMEOUT` | `30` | 请求超时（秒） |
| `MILOCO_TLS_VERIFY` | `false` | TLS 证书验证 |

### 方式三：Hermes config.yaml 直接传参

## Hermes 注册

在 `~/.hermes/config.yaml` 的 `mcp_servers` 下添加:

```yaml
mcp_servers:
  miloco:
    command: python3
    args:
      - "-m"
      - "miloco_mcp.server"
    cwd: "/path/to/xiaomi-miloco/plugins/hermes-mcp"
    env:
      MILOCO_BASE_URL: "http://127.0.0.1:1810"
    timeout: 60
    connect_timeout: 30
```

注册后重启 Hermes 或执行 `/mcp reload` 使其生效。

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

## 设备控制 API 格式

MCP Server 转发控制请求到 Miloco Backend，格式如下:

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

在 Hermes 中直接对话:

```
帮我把客厅灯打开
看看门口有没有人
把空调调到26度制冷
关闭卧室所有灯
每天提醒我喝水
查看家里现在什么情况
```

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
├── pyproject.toml        # Python 项目配置
├── README.md             # 项目说明
├── DEPLOY.md             # 本文档
└── src/miloco_mcp/
    ├── __init__.py
    ├── config.py         # 配置加载（自动读取 miloco config.json）
    ├── client.py         # HTTP 客户端（封装 REST 调用）
    ├── server.py         # MCP Server 入口
    └── tools/
        ├── __init__.py   # 工具注册
        ├── devices.py    # 设备查询与控制
        ├── home.py       # 家庭档案、成员管理
        ├── perception.py # 摄像头感知
        └── tasks.py      # 家庭任务管理
```

## 许可证

MIT
