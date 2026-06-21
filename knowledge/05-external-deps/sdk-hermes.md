# Hermes Agent SDK 依赖

## L1：它是什么

Hermes Agent 是小米内部维护的 AI Agent 运行时框架，提供插件化的 Agent 能力扩展机制，定位与 OpenClaw 平行——两者都是 Agent 框架，Miloco 在其上注册插件。

Miloco 的 Hermes 插件（`plugins/hermes/`）是 OpenClaw 插件（`plugins/openclaw/`）的平行实现，复用同一套 Python 后端（`backend/`）与 `miloco-cli`（`cli/`）。设计目标：**后端零改动**——webhook 协议、`config.json` schema、Skill 文档与 OpenClaw 完全一致，切换框架只改 config 中的 webhook 地址。

详细集成设计见 [Hermes Agent 集成](../03-features/hermes-plugin-integration.md)。

---

## L2：我们怎么用

### 注册点概览

Miloco 的 Hermes 插件（`plugins/hermes/__init__.py`）注册以下扩展：

| 扩展类型     | 数量 | 职责概述                                                                                                     |
| ------------ | ---- | ------------------------------------------------------------------------------------------------------------ |
| **配置**     | —    | `$MILOCO_HOME` 路径解析（基于 `get_hermes_home()` 派生）、插件配置读取                              |
| **Bridge**   | 1    | 自建 aiohttp HTTP 服务（默认 `:18789`），接收后端 `{ action, payload }` POST，实现与 OpenClaw 一致的固定契约 |
| **Hooks**    | 2    | `pre_llm_call`（Profile 分级 + 上下文注入到 user message）；trace hooks（多个 agent 事件监听）               |
| **Tools**    | 2    | `miloco_im_push`（通知推送）、`miloco_habit_suggest`（防骚扰状态机）——与 OpenClaw 同名同语义           |
| **Cron**     | —    | 4 个受管 cron job 的一次性 reconcile + `hermes miloco` CLI 命令                                              |
| **Skills**   | 16   | 逐个 `ctx.register_skill()` 注册，命名空间 `miloco:<skill-name>`                                             |

详细注册结构与设计见 [Hermes Agent 集成](../03-features/hermes-plugin-integration.md)。

### 版本兼容约束

- Hermes 插件依赖 `pre_llm_call` Hook 接口——注入位置是 user message（与 OpenClaw 的 `before_prompt_build` 注入 system prompt 不同）
- 直接 import `run_agent.AIAgent.run_conversation()` 作为同步 turn 入口，这是 Hermes 所有前端（CLI / gateway / cron / delegate_task）共用的稳定 API；cron 的 `run_job()` 是参考样例
- `$MILOCO_HOME` 默认从 `get_hermes_home()` 派生（`~/.hermes/miloco`），不硬编码路径；后端 / CLI 已支持 `$MILOCO_HOME` 环境变量覆盖
- 后端通过 `run_agent_turn`（`utils/agent_client.py`）调 Hermes bridge 的 `/miloco/webhook`，调用方式与调 OpenClaw 完全一致

### 与后端的通信契约

后端的 `call_agent_webhook()`（`utils/agent_client.py`）对 OpenClaw 与 Hermes **完全无感**：请求格式 `{ action, payload }`、响应格式 `{ code, message, data }` 是**硬编码的固定契约**，不可通过配置改变。bridge 实现方必须逐字段兼容。

`agent` action 的 `data` 字段（`runId` / `status` / `error` / `recovered`）与 `get_trace` action 的 `data` 字段（`status` 及 done 时的 trace meta）语义与 OpenClaw 一致，字段定义见 `utils/agent_client.py`——知识库不复制字段表。配置项 `agent.webhook_url`、`agent.auth_bearer` 存于 `$MILOCO_HOME/config.json`，默认值见 [Hermes Agent 集成 · 配置共享](../03-features/hermes-plugin-integration.md#配置共享)。

### 配置共享

三端（backend / CLI / plugin）共用 `$MILOCO_HOME/config.json`，schema 与 OpenClaw 完全一致，差异仅在默认值（webhook_url 指向 bridge 的 `:18789`、auth_bearer 默认空）。插件级私有配置（`bridge_host` / `bridge_port` / `bridge_auth_token`、`bin_path`、`deliver` / `deliver_extra` 等）从 Hermes config.yaml 的 `plugins.entries.miloco` 段读取，合并默认值，字段完整列表见 `plugins/hermes/__init__.py`。

### Skills 安装机制差异

OpenClaw 与 Hermes 安装 skills 的时机不同，这是两个框架安装流程结构性差异决定的：

| 项                 | OpenClaw                                       | Hermes                                                         |
| ------------------ | ---------------------------------------------- | -------------------------------------------------------------- |
| 安装流程           | `npm install`（已构建的 npm 包，含 skills）    | `git clone` + `shutil.move`（无构建步骤，无 post-install 钩子） |
| skills 复制时机     | `prebuild`（构建前，`sync-skills.mjs`）        | 安装时（`scripts/install.sh --agent hermes`）                    |
| skills 是否进仓库   | 否（`.gitignore` 排除，发布到 npm 包）        | 否（`.gitignore` 排除，安装时从仓库同步）                       |
| 访问命名空间       | `<skill-name>`（无命名空间）                  | `miloco:<skill-name>`（插件命名空间，只读，不在系统提示索引）  |

具体复制脚本与阈值不写，分别指向 `plugins/openclaw/scripts/sync-skills.mjs` 与 `scripts/install.sh`。

### 出问题找谁

Hermes 框架本身（agent turn 失败、cron 不触发、plugin 加载失败、`AIAgent` 接口变更）由小米 AI Agent 团队负责。Miloco 插件侧（Skill 逻辑、Hook 实现、bridge handler、注册的 Tool）由 Miloco 工程侧负责。排查时先区分"是框架问题"还是"是插件问题"：

- bridge 日志：进程级日志（`miloco-bridge` 后台线程），指向 Hermes 进程日志
- 后端日志：`miloco-cli service logs -f`
- trace 落盘：`$MILOCO_HOME/trace/agent/`（debug 模式，哨兵文件 `$MILOCO_HOME/.debug_observability`）
