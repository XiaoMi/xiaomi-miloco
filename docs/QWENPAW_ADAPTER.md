# Miloco QwenPaw 适配器

让 Miloco 全屋智能系统运行在 [QwenPaw](https://github.com/qwenpaw) 平台上，替代 OpenClaw 插件层。

## 架构

```
Miloco Python 后端 (:1810)
    │  POST /miloco/webhook { action: "agent"|"get_trace", payload: {...} }
    ▼
QwenPaw Webhook 桥接器 (:18789)          ← scripts/qwenpaw_webhook_bridge.py
    │  qwenpaw agents chat --from-agent miloco-bridge --to-agent miloco
    ▼
QwenPaw Miloco Agent (miloco)
    │  16 个 SKILL.md 技能
    │  miloco-cli 工具命令
    └── 决策 → 返回
```

## 快速开始

### 1. 安装 Miloco（跳过 OpenClaw 插件）

```bash
git clone https://github.com/XiaoMi/xiaomi-miloco.git
cd xiaomi-miloco
bash scripts/install.sh --dev --skip-openclaw --agent-prepare
```

### 2. 创建 QwenPaw Agent

```bash
qwenpaw agents create --name "Miloco" --agent-id miloco
qwenpaw agents create --name "Miloco Bridge" --agent-id miloco-bridge
```

### 3. 部署技能和配置

```bash
# 复制技能（16 个 SKILL.md）
cp -r plugins/skills/* /app/working/workspaces/miloco/skills/

# 注册技能到 skill.json
# 参见仓库中的 skill.json 示例

# 部署 Agent 身份文件
cp docs/miloco-agent/SOUL.md /app/working/workspaces/miloco/
```

### 4. 启动 Webhook 桥接器

```bash
# 开发模式
python3 scripts/qwenpaw_webhook_bridge.py &

# 生产模式（加入 supervisor）
sudo cp scripts/qwenpaw_bridge_supervisor.conf /etc/supervisor/conf.d/
sudo supervisorctl reread && sudo supervisorctl update
```

### 5. 配置并启动

```bash
miloco-cli config set model.omni.api_key <your-api-key>
miloco-cli account bind
miloco-cli service restart
```

## Webhook API 兼容

| Action | 功能 | 状态 |
|--------|------|------|
| `agent` | 投递消息并同步等待 Agent 决策 | ✅ 完全兼容 |
| `get_trace` | 查询 Agent turn 元数据 | ⚠️ 基础兼容 |

响应格式与 OpenClaw `waitForRun` 一致：
```json
{ "code": 0, "data": { "runId": "uuid", "status": "ok" } }
```

## 与 OpenClaw 的差异

| 功能 | OpenClaw | QwenPaw 适配 |
|------|----------|-------------|
| `miloco_im_push` | 原生工具 | `channel_message` skill |
| `miloco_notify_bind` | 原生工具 | session 配置 |
| `miloco_habit_suggest` | 原生工具 | 待适配 |
| Prompt 注入 | hook 动态注入 | SOUL.md 静态注入 |
| Trace | 实时追踪 | 缓存轮询 |
| 会话存储 | `api.session` | memory 文件 |

## Docker 网络说明

Miloco 的摄像头视频流使用 PPCS/P2P 协议直连，需要容器与摄像头在同一个二层网络。

| 部署方式 | 设备控制 | 摄像头画面 | 说明 |
|----------|---------|-----------|------|
| `--net=host` | ✅ | ✅ | 推荐，直接用宿主机网卡 |
| Docker bridge (默认) | ✅ | ❌ | 云端 API 通，P2P 被 NAT 阻断 |
| 宿主机直装 | ✅ | ✅ | 无网络隔离 |

如果必须使用 Docker bridge，可启动 UDP LAN 中继解决设备发现问题（摄像头仍无法出画面）：

```bash
python3 scripts/udp_lan_relay.py &
```

> `udp_lan_relay.py` 是可选组件，仅在非 host 网络环境下需要。

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `QWENPAW_BIN` | `/app/venv/bin/qwenpaw` | CLI 路径 |
| `QWENPAW_BASE_URL` | `http://localhost:8088` | API 地址 |
| `AGENT_TIMEOUT` | `300` | 超时秒数 |

## 故障排查

```bash
# 桥接器日志
tail -f ~/.openclaw/miloco/log/qwenpaw_bridge.log

# 验证桥接器
curl -X POST http://127.0.0.1:18789/miloco/webhook \
  -H "Content-Type: application/json" \
  -d '{"action":"get_trace","payload":{"runId":"test"}}'

# 验证 Agent 互通
qwenpaw agents chat --from-agent miloco-bridge --to-agent miloco --text "ping"

# 检查服务
miloco-cli service status
```
