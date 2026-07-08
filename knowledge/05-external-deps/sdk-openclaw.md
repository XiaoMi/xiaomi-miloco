# OpenClaw SDK 依赖

## L1：它是什么

OpenClaw 是 Miloco 使用的 AI Agent 运行时框架，提供插件化的 Agent 能力扩展机制。Miloco 通过注册 OpenClaw 插件，将家庭感知、设备控制、家庭记忆等能力接入 Agent 的对话和自动化流程中。

OpenClaw 框架由小米内部 AI 团队维护，以 npm 包（`openclaw`）形式分发。插件通过 Plugin SDK（`openclaw/plugin-sdk`）注册扩展点。

---

## L2：我们怎么用

### 注册点概览

Miloco 的 OpenClaw 插件（`plugins/openclaw/src/index.ts`）注册五类扩展：

| 扩展类型         | 数量 | 职责概述                                                                                                               |
| ---------------- | ---- | ---------------------------------------------------------------------------------------------------------------------- |
| **Services**     | 1    | `backend.ts` 管理 Python 后端进程生命周期（`catalog.ts` 非注册 Service，是 before_prompt_build 内调用的辅助模块）      |
| **Hooks**        | 2    | 唯一的 before_prompt_build（同处装配系统上下文 + 设备目录 + 家庭档案 + 待回应建议）；trace hook（7 个 agent 事件监听） |
| **Webhooks**     | 2    | agent（接收后端事件回调，触发 Agent subagent turn）；get_trace（backend 反向轮询 agent run 元数据）                    |
| **Tools**        | 3    | `miloco_im_push`（通知分发）、`miloco_notify_bind`（通知渠道绑定）、`miloco_habit_suggest`（习惯建议状态）             |
| **Home Profile** | —    | 家庭档案注入 + 4 个受管 Cron 任务调度                                                                                  |

详细注册结构见 [Agent 集成](../03-features/openclaw-integration.md)。

### Skill 格式与构建

Skill 源码在 `plugins/skills/`（`miloco-` 前缀），每个 Skill 一个目录，包含 `SKILL.md`（frontmatter + 指令正文）。frontmatter 遵循 `agentskills.io/specification`，必须包含 `name`、`description`、`metadata`（`author`/`version`/`date`），可选 `openclaw.requires`（声明依赖的 bins 和 built-in tools）。

`pnpm run build` 构建时通过 `scripts/sync-skills.mjs` 把 `plugins/skills/` 整体复制到插件构建产物中。修改 Skill 后须重新 `pnpm run build` + `openclaw plugins install .` 才生效。

### 版本兼容约束

- Miloco 插件依赖 `openclaw/plugin-sdk` 的 `before_prompt_build` Hook 接口
- 插件级配置读写依赖 OpenClaw 运行时提供的配置 API
- 插件安装需先安装 OpenClaw 框架（`openclaw`）
- 后端通过 `run_agent_turn`（`utils/agent_client.py`）调 OpenClaw 的 `/miloco/webhook`

### 与后端的通信契约

后端通过 `run_agent_turn`（`utils/agent_client.py`）向 OpenClaw 的 `/miloco/webhook` 发起调用，传入事件消息和会话路由，返回调用状态（成功 / 超时 / 各类错误）；参数与返回值定义见该文件。

OpenClaw 插件侧通过 `get_trace` Webhook 暴露 agent run 元数据接口，后端的 `AgentMetaPoller`（`observability/agent_meta_poller.py`）异步轮询，取到结果后写入 `observability.db::agent_runs` 表，实现 cycle → agent run 的端到端追踪链路。元数据字段定义见 `observability/agent_meta_poller.py`。

### 配置共享

三端（backend / CLI / plugin）共用 `$MILOCO_HOME/config.json`：

- `server.token`：backend 独占生成，plugin 只读，用于 backend API 鉴权（`Authorization: Bearer <token>`）
- `agent.webhook_url`：backend 调 plugin 的 Webhook 地址（默认值见 `settings.schema.json::agent.webhook_url`）
- `agent.auth_bearer`：plugin 启动时由框架认证解析写入，backend 用于调 Webhook 时的认证
- 插件级私有配置（`debug`、`omni` 模型参数、`notifySessionKey` 等）存储在 `config.json::plugins.entries[miloco].config` 段；字段完整列表见 `plugins/openclaw/src/` 各模块注释

### 出问题找谁

OpenClaw 框架本身（agent turn 失败、Cron 不触发、Webhook 连不通）由小米 AI Agent 团队负责。Miloco 插件侧（Skill 逻辑、Hook 实现、Webhook handler、注册的 Tool）由 Miloco 工程侧负责。排查时先区分"是框架问题"还是"是插件问题"：

- `GET /api/miot/mips_status` 看 MQTT 连接
- `miloco-cli service logs -f` 看 backend 日志
- OpenClaw 插件日志在 `$MILOCO_HOME/log/openclaw-plugin.log`（需插件 debug 配置）
