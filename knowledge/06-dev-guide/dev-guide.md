# 开发指南

## 环境准备

- **Python** >= 3.11（推荐 3.12+），由 `uv` 管理虚拟环境
- **Node.js**（LTS），由 pnpm 管理 TypeScript 包
- **uv**：Python 包和 workspace 管理，缺失时 `install.sh` 自动安装
- **pnpm**：TypeScript 包管理（插件 + 前端）
- **openclaw**：Agent 框架，用于运行插件和 Skill

---

## 一键安装（推荐）

```bash
# 开发者：--dev 从源码构建后本地安装
bash scripts/install.sh --dev

# 终端用户：从 GitHub Release 下载对应平台归档后本地安装
bash scripts/install.sh [--lang zh] [--omni-api-key <key>]

# Agent 非交互模式（CI / 自动化）
bash scripts/install.sh --agent-prepare   # 输出 JSON，供 Agent 解析后分步调用
```

`install.sh` 先确保 uv + Python >= 3.11 就位，再依次完成：环境检查 → 包安装 → 服务预热 → 米家账号绑定引导 → Omni 模型配置 → 感知模型下载 → OpenClaw 插件安装。

---

## 手动安装（逐步）

```bash
# Server（Python）
cd backend && uv sync --all-packages
miloco-cli config set model.omni.api_key <key>

# CLI
cd cli && uv sync && uv tool install . --force --reinstall

# OpenClaw 插件（TypeScript）
cd plugins/openclaw && pnpm install && pnpm run build && openclaw plugins install .
```

---

## 启动

```bash
# CLI 管理服务（推荐，后台守护进程）
miloco-cli service start
miloco-cli service status
miloco-cli service logs -f
miloco-cli service stop

# 开发模式（前台直跑，进程异常立即可见）
cd backend && uv run task dev
```

服务启动后监听 `127.0.0.1:1810`（默认）。健康检查：`curl http://127.0.0.1:1810/health` 应返回 `{"status":"ok"}`。

**单进程约束**：Server 不支持多 worker（`workers != 1` 直接抛 `NotImplementedError`）。感知引擎、watchdog、resource monitor 均为单实例 daemon，多 worker 会导致资源锁竞争和监控状态分裂。需要横向扩展请在反代层做（nginx 多上游 / haproxy）。

---

## 配置体系

### 配置来源与优先级

配置统一由 `MilocoSettings`（`backend/miloco/src/miloco/config/settings.py`）管理，加载优先级从高到低：

1. **环境变量**（`MILOCO_*`，嵌套用 `__` 分隔，如 `MILOCO_SERVER__PORT`）
2. **用户配置文件**（`$MILOCO_HOME/config.json`）— 三端（backend / CLI / 插件）共享，用户日常调整的入口
3. **后端默认 YAML**（`backend/miloco/src/miloco/config/settings.yaml`）— 后端打包时的默认值
4. **代码默认值**

`settings.schema.json`（同目录）是 `config.json` 面向用户的 JSON Schema 契约，覆盖核心可配置段（`debug` / `server` / `agent` / `model`）的类型与语义描述。其余段（`perception` / `rule` / `camera` / `directories` 等）的完整字段定义以 `settings.yaml`（含注释）与 `settings.py` 的 pydantic 模型为准。

### 配置分段与用途

| 配置段        | 控制什么                                                                                                                                            |
| ------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| `server`      | 后端监听 host/port、访问 Bearer token、启动用 Python 路径、日志级别                                                                                 |
| `agent`       | OpenClaw webhook 地址和认证凭据                                                                                                                     |
| `model.omni`  | 多模态模型的 API Key、Base URL、模型标识；**感知必填项**                                                                                            |
| `directories` | 工作目录（`storage`）、ONNX 模型目录；派生路径（log_dir / snapshot_dir 等）由此计算                                                                 |
| `database`    | SQLite 连接参数                                                                                                                                     |
| `miot`        | 小米云区域（`cn/de/i2/ru/sg/us`）                                                                                                                   |
| `camera`      | 摄像头采集帧间隔和缓冲大小                                                                                                                          |
| `rule`        | 规则日志保留天数；duration 窗口触发比例默认值                                                                                                       |
| `perception`  | 感知日志 TTL、事件截图 TTL + 磁盘配额；子段：`perception.collect`（采集窗口）、`perception.engine`（识别 / VLM 等引擎子参数）、tier_u dump 调试开关 |
| `perf`        | 可观测性总开关（`enabled`）、各表/文件保留天数；关闭后 observability.db 不建                                                                        |
| `dispatcher`  | Agent 事件队列上限、单 turn 等待超时                                                                                                                |

### 用户最常修改的配置项

- `model.omni.api_key`：感知启动前必填，通过 `miloco-cli config set model.omni.api_key <key>` 设置
- `server.host`：默认 `127.0.0.1`，仅本机可达；开放局域网访问改为 `0.0.0.0`（需自行评估网络安全）
- `server.port`：服务监听端口，默认 `1810`（定义在 `settings.yaml::server` / `settings.py` 的 `ServerSettings`，不进 schema.json）；与其他服务端口冲突时修改此项
- `miot.cloud_server`：使用海外账号时改为对应区域
- `perception.snapshot_max_disk_mb`：事件截图磁盘配额，磁盘紧张时调低
- `perf.enabled`：关闭后不采集 observability 数据，节省磁盘

### 配置修改后是否需要重启

大多数配置在服务启动时一次性读取，修改后需重启才能生效。包括：`server.*`（host/port/log_level）、`model.omni.*`、`directories.*`、`perception.engine.*` 等。

`miloco-cli config set` 写入 `config.json` 后，下次 `get_settings()` 调用即读到新值——但如果对应的 Service/Runner 在初始化时已缓存了该配置，运行期不会自动重读，仍需重启服务。

### 验证配置生效

```bash
# 查看所有当前生效配置（含派生路径）
miloco-cli config show

# 查看具体字段
miloco-cli config get model.omni.api_key

# 重启后确认服务正常
miloco-cli service stop && miloco-cli service start
miloco-cli admin status
```

---

## 常用操作

```bash
# 绑定小米账号
miloco-cli account bind       # 交互式：等待粘贴 base64 授权码
miloco-cli account status     # 查看绑定状态

# 查看已接入设备
miloco-cli device list [--online]

# 查看服务状态和节点健康
miloco-cli admin status
```

配置文件路径：`$MILOCO_HOME/config.json`（默认 `~/.openclaw/miloco/config.json`），字段定义以 `settings.yaml` + `settings.py` 为准（核心段另见 `settings.schema.json`）。

---

## 开发工作流

```bash
# Server 开发
cd backend
uv sync --all-groups           # 安装包含 dev 依赖
uv run task dev                # 前台直跑开发服务器（进程异常立即可见）
uv run task test               # 运行测试
uv run task lint               # lint + format 检查
uv run task check              # 类型检查

# CLI 修改后需重装才生效
cd cli
uv run miloco-cli device list  # 直接用 uv run 跑本地版本（不需重装）

# OpenClaw 插件
cd plugins/openclaw
pnpm run build                 # 构建（prebuild 钩子把 plugins/skills/ 复制进来）
pnpm test                      # 运行测试
openclaw plugins install .     # 安装到 OpenClaw（build 后需重装）

# 家庭面板前端（web/）
cd web
pnpm install                   # 安装依赖
pnpm build                     # 构建到 web/dist/（生产包）
pnpm dev                       # Vite dev server，自动代理 /api 到 backend
pnpm test                      # 单元测试（vitest）
pnpm typecheck                 # 类型检查
```

**Skill 修改**：`plugins/skills/` 是唯一源，修改 `SKILL.md` 后须重新 `pnpm run build` + `openclaw plugins install`。

**家庭面板修改**：修改 `web/src/` 后 `pnpm build` 产出到 `web/dist/`，再手动复制到 `$MILOCO_HOME/static/`，或重跑 `install.sh` 自动同步。开发期用 `pnpm dev` + Vite proxy 直接对接 backend，无需额外部署步骤。

---

## 常见开发场景

### 场景一：修改感知参数

感知流水线参数集中在 `$MILOCO_HOME/config.json` 的 `perception` 段（覆盖 `settings.yaml::perception`），字段定义见 `settings.py` 的 `PerceptionSettings`。

```bash
# 查看当前感知配置
miloco-cli config show | grep perception

# 修改某项参数后重启服务使配置生效
miloco-cli service stop && miloco-cli service start

# 确认引擎状态
curl -H "Authorization: Bearer $(miloco-cli config get server.token)" \
  http://127.0.0.1:1810/api/perception/engine/status
```

相关代码入口：

- 感知调度：`perception/runner.py`（PerceptionRunner）
- Gate 逻辑：`perception/engine/gate/gate.py`
- 身份识别逻辑：`perception/engine/identity/engine.py`
- Omni prompt：`perception/engine/omni/omni.py`

### 场景二：添加/调试规则

```bash
# 查看当前规则列表
miloco-cli rule list --pretty

# 查看规则触发日志（最近 1 小时）
miloco-cli rule logs --since 1h --pretty

# 手动触发规则测试（debug 用）
miloco-cli rule trigger <rule_id>
```

规则 schema 定义在 `backend/miloco/src/miloco/rule/schema.py`，规则执行逻辑在 `rule/runner.py`（RuleRunner），规则设计原理见 [规则自动化](../03-features/rule-automation.md)。

**注意**：condition.query 不能以"检测到/识别到/感知到"等断言性词汇开头，否则创建时会返回 `422`。

### 场景三：添加或修改 Skill

所有 Skill 源码在 `plugins/skills/` 目录下，每个 Skill 一个子目录，包含 `SKILL.md`（frontmatter + 指令描述）。

```bash
# 修改 Skill 后重新构建并安装
cd plugins/openclaw
pnpm run build
openclaw plugins install .

# 验证 Skill 已安装
openclaw skills list | grep miloco
```

#### Skill 标准结构

每个 Skill 目录下有一个 `SKILL.md`，frontmatter 遵循 `agentskills.io/specification`：

```yaml
---
name: miloco-<skill-name> # 小写字母+连字符，必须以 miloco- 开头
description: 一句话描述 # 何时激活，Agent 依据此选择 Skill
metadata:
  author: miloco
  version: "1.0"
  date: YYYY-MM-DD # 取 git 最后提交日期
  openclaw:
    requires:
      bins: ["miloco-cli"] # 依赖的命令行工具
      tools: # 依赖的 OpenClaw built-in tools（可选）
        - miloco_im_push
---
# Skill 正文（Markdown）
```

frontmatter 之后是 Skill 的自然语言指令正文，Agent 加载 Skill 时读取此内容决定如何行动。

#### Skill 如何调用后端 API

Skill 通过 `miloco-cli` 调用后端：

```bash
# 设备控制
miloco-cli device control <did> <key> <value>
miloco-cli device control <did> --set <key> <val> --set <key> <val>  # 多属性合并
miloco-cli device props <did> [key ...]                                # 查询属性
miloco-cli device action <did> <iid> [param ...]                       # 调用动作
miloco-cli device list [--room <房间>] [--category <品类>]             # 查设备列表
miloco-cli device spec <did>                                           # 查设备 spec

# 规则管理
miloco-cli rule create --task-id <id> --name "规则描述" --mode event --condition "..."
miloco-cli rule list --pretty
miloco-cli rule logs --since 1h

# 任务管理（task：生命周期；record：行为统计）
miloco-cli task create --task-id <id> --description "<desc>"
miloco-cli task link --task <task_id> --kind cron --ref <jobId>       # 挂 cron（rule 由 rule create 自动 link）
miloco-cli task delete <task_id> --reason completed|expired|abandoned  # 终止（写审计快照）
miloco-cli task record init <task_id> --kind progress|duration|event  # 初始化记录
miloco-cli task record progress-inc <task_id> [--delta N]             # 进度累加
miloco-cli task record event-append <task_id> --description "<描述>"   # 事件追加
miloco-cli task record session-start|session-end <task_id>            # 时长计时段
miloco-cli task record get|compute <task_id>                          # 读取 / 聚合

# 身份管理
miloco-cli identity member list
miloco-cli identity pool fetch --cam <cam_id>
miloco-cli identity register from-cluster --cluster-id <id> [--member-id <id> | --name <名>]
```

CLI 会读取 `$MILOCO_HOME/config.json` 中的 `server.url`（后端 HTTP Base URL）和 `server.token`，向后端发 HTTP 请求。鉴权通过 `Authorization: Bearer <token>` 头传递。

#### 开发新 Skill 的快速入门

1. 在 `plugins/skills/` 下新建目录，命名为 `miloco-<name>`
2. 创建 `SKILL.md`，填写 frontmatter 和指令正文
3. 构建并安装：`cd plugins/openclaw && pnpm run build && openclaw plugins install .`
4. 验证：`openclaw skills list | grep miloco-<name>`
5. 在 Agent 对话中触发，观察日志 `$MILOCO_HOME/log/openclaw-plugin.log`

Skill 正文中可以通过 `Bash` tool 调用 `miloco-cli` 任意子命令（含 `task record` 行为统计），或通过插件注册的 built-in tools（`miloco_im_push` 发通知、`miloco_notify_bind` 绑通知渠道、`miloco_habit_suggest` 习惯建议状态）操作。

### 场景四：直接调用 API 进行调试

```bash
# 获取 token
TOKEN=$(miloco-cli config get server.token)

# 查询设备列表
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:1810/api/miot/device_list

# 控制设备
curl -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"type": "set_property", "iid": "prop.<siid>.<piid>", "value": true}' \
  http://127.0.0.1:1810/api/miot/devices/<did>/control

# 查询感知引擎状态
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:1810/api/perception/engine/status

# 触发主动查询
curl -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"sources": ["<device_did>"], "query": "客厅里现在有几个人？"}' \
  http://127.0.0.1:1810/api/perception/perceive

# 查询规则列表
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:1810/api/rules

# 查询家庭档案
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:1810/api/home-profile/entries

# 查询节点健康状态
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:1810/api/monitor/nodes
```

---

## 感知模型管理

感知流水线依赖多个 ONNX 模型，存放在 `$MILOCO_HOME/models/`（包内 `perception/models/` 作兜底）：必需的 `det_4C.onnx`（检测）/ `human_body_reid_v2.onnx`（ReID），以及可选的 VAD、语义去重模型（清单见 `perception/engine/resource_validator.py`）。必需模型缺失才会导致引擎降级。

```bash
# 检查感知引擎状态
miloco-cli admin status  # 看 engine 节点 lifecycle

# 感知引擎未就绪时：查询详细状态
curl -H "Authorization: Bearer $(miloco-cli config get server.token)" \
  http://127.0.0.1:1810/api/perception/engine/status

# 模型缺失时重跑 install.sh 补全
bash scripts/install.sh --dev
```

---

## 数据落盘约定

| 路径                              | 用途                                                                                                       |
| --------------------------------- | ---------------------------------------------------------------------------------------------------------- |
| `~/.openclaw/miloco/`             | `$MILOCO_HOME` 默认根                                                                                      |
| `config.json`                     | 三端共享配置                                                                                               |
| `miloco.db`                       | SQLite 业务数据库                                                                                          |
| `observability.db`                | 性能追踪数据库（`perf.enabled=true` 时建）                                                                 |
| `data/identity_lib/persons/<id>/` | 身份库（tier_a / tier_c / meta.json）                                                                      |
| `models/`                         | ONNX 模型（必需 det_4C / human_body_reid_v2，另有可选模型；清单见 `resource_validator.py`）                |
| `home-profile/`                   | 家庭档案（candidates.json / profile.json / profile.md）                                                    |
| `static/`                         | 家庭面板前端静态资源（由 `install.sh` 从 `web/dist/` 同步）                                                |
| `log/`                            | 各组件日志（`miloco-backend.log` / `supervisord.log` 等）                                                  |
| `supervisord.*`                   | supervisor 配置和 socket（service start 首次生成）                                                         |
| `memory/`                         | Agent 工作区记忆（如 `_system/dynamic_failures.md`；任务行为统计已迁至 `miloco.db` 的 `task_record_*` 表） |
| `trace/agent/`                    | DYNAMIC rule trace jsonl（`debug_observability` flag 开时写）                                              |
| `trace/omni/`                     | Omni 推理 trace jsonl（同上）                                                                              |
| `packs/`                          | 日志打包产物（LRU 保留最新几个）                                                                           |

---

## 日志位置速查

日志位置速查见 [故障排查 · 日志位置](troubleshooting.md#日志位置)。

---

## 代码规范

- **Python**：Ruff（lint + format），配置在 `backend/pyproject.toml`
- **TypeScript**：Biome，配置在 `plugins/openclaw/`
- **测试**：pytest / vitest
- **Commit**：遵循项目 git log 中的提交风格
