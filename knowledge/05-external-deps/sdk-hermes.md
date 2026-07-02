# Hermes Agent 依赖

## L1：它是什么

Hermes Agent 是 [Nous Research](https://nousresearch.com) 的开源 AI Agent（MIT，Python），与小米内部的 OpenClaw 同类——一个自托管、可对接 Telegram/Discord/Slack 等、带记忆与 skill 系统的 agent 运行时。仓库 [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)，文档 [hermes-agent.nousresearch.com/docs](https://hermes-agent.nousresearch.com/docs)。

miloco 通过 `plugins/hermes/` 接入 Hermes，作为 OpenClaw 之外的并列 agent 运行时。

---

## L2：我们怎么用

### 扩展点映射

| miloco 需求                  | Hermes 机制                                                                    | 文件                                 |
| ---------------------------- | ------------------------------------------------------------------------------ | ------------------------------------ |
| 16 个 skill                  | `~/.hermes/skills/`（agentskills.io 标准，miloco skill 已合规）                | `scripts/sync-skills.py`             |
| 注入设备目录/家庭档案/身份块 | `pre_llm_call` 插件钩子，返回 `{"context": text}`                              | `miloco-plugin/context_injection.py` |
| 注册 tool（通知/习惯建议）   | `ctx.register_tool(name, toolset, schema, handler)`                            | `miloco-plugin/tools_*.py`           |
| 4 个受管 cron                | `cron.jobs.create_job(prompt, schedule, skills=[...])` + reconcile             | `miloco-plugin/cron_setup.py`        |
| 后端→agent 同步回调          | api_server `POST /v1/chat/completions`（同步，`X-Hermes-Session-Id` 会话连续） | `adapter/hermes_client.py`           |

### 关键契约

**`pre_llm_call` 钩子**（`website/docs/user-guide/features/hooks.md`）：

- 回调签名 `(session_id, user_message, conversation_history, is_first_turn, model, platform, **kwargs)`。
- 返回 `{"context": text}` 或纯字符串 → 注入到**当前 turn 的 user message**（非 system prompt，为保 prompt cache）。
- 每个 `run_conversation()` 调用触发一次，在 context 压缩后、tool 循环前。
- 多插件返回的 context 按 plugin 发现序用 `\n\n` 拼接。

**插件 ctx API**（`website/docs/guides/build-a-hermes-plugin.md`）：

- `ctx.register_tool(name=, toolset=, schema=, handler=, check_fn=, emoji=, override=)` —— schema 是 OpenAI tool-schema dict；handler `def(args: dict, **kw) -> str`。
- `ctx.register_hook(event, cb)` —— 事件含 `pre_llm_call`/`post_llm_call`/`pre_tool_call`/`post_tool_call`/`on_session_*`/`subagent_stop`/`pre_gateway_dispatch` 等。
- `ctx.dispatch_tool(name, arguments)` —— 调任意已注册 tool（含内置 `send_message`），继承父 agent 的审批/凭据上下文。
- **不能注册任意 HTTP 路由**——这是入站适配层必须独立成进程的根因。

**api_server 同步 chat**（`gateway/platforms/api_server.py`，v0.10.0 实测路由）：

- `POST /v1/chat/completions`（OpenAI 兼容同步端点），body `{"messages":[{"role":"system","content":...},{"role":"user","content":...}]}`（system 可选），响应 `{"choices":[{"message":{"role":"assistant","content":...}}],"usage":{}}`。
- 会话连续：请求头 `X-Hermes-Session-Id: <id>` —— Hermes 从 state.db 加载该 session 的历史拼入上下文。适配器用 `miloco:<sessionKey>:<lane>` 作 id，使同一 miloco 会话多次回调落到同一 Hermes 会话。无需预建 session。
- 鉴权：`Authorization: Bearer $API_SERVER_KEY`。`API_SERVER_KEY` 环境变量设置即自动启用 api_server 平台（默认端口 8642）。
- 异步变体 `POST /v1/runs`（返 `202 {run_id, status:"started"}`，`GET /v1/runs/{run_id}/events` SSE 取结果）——首版用同步 `/v1/chat/completions`，因 miloco 后端要求同步回包。
- 溢出自愈：api_server 无 session 删除/重置路由，适配器识别溢出错误文案后用无 `X-Hermes-Session-Id` 的全新 turn 重试一次。

**cron**（`cron/jobs.py::create_job`）：

- `create_job(prompt, schedule, name=None, skills=None, deliver=None, ...)`。
- `deliver` 是字符串（`"origin"/"local"/"none"/...`）；miloco 受管任务用 `"none"`。
- 无 `description` 字段，故把 `[miloco:home-profile]` 标签塞进 `name` 前缀作 reconcile 识别键。

### 版本兼容约束

- 依赖 Hermes `pre_llm_call` 钩子接口与 api_server `/v1/chat/completions` 路由（v0.10.0 实测；main 分支的 `/api/sessions/{id}/chat` 在 v0.10.0 不存在）。
- 插件级配置：Hermes `PluginManifest` 当前不解析 `configSchema`，故运行时配置由插件自管于 `<plugin-dir>/state.json`；`omni_*` 实际由 `$MILOCO_HOME/config.json`（后端管）提供。
- 需先安装 Hermes（`curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash`）。
- Hermes v0.10.0 的 cron 调度依赖 `croniter` 包（`pip install croniter`），否则 4 个受管任务创建失败。
- skill frontmatter 的 `date` 字段必须加引号（字符串），否则 YAML 解析成 `datetime.date` 导致 Hermes `skill_view` 的 `json.dumps` 失败、cron 加载 skill 报错——`sync-skills.py` 已自动处理。

### 与后端的通信契约

后端 `run_agent_turn`（`utils/agent_client.py`）向适配进程 `POST {action, payload}`，适配进程翻译为 Hermes `/chat`，同步返回 `{code, message, data}`，`data = {runId, status, error?, recovered?}`，`status ∈ {ok, error, timeout}`。参数与返回值定义见 `adapter/hermes_client.py` 与 `plugins/openclaw/src/webhooks/agent.ts`（OpenClaw 版的契约蓝本）。

### 出问题找谁

- Hermes 框架本身（agent turn 失败、cron 不触发、api_server 连不通）→ Hermes 仓库 issue。
- miloco 适配层（适配进程契约翻译、pre_llm_call 注入、tool 注册）→ miloco 工程侧。
- 排查顺序：先确认 Hermes gateway + api_server 起着、`API_SERVER_KEY` 设了；再 `curl adapter /health`；最后看 miloco 后端 `agent.webhook_url`/`auth_bearer` 与 adapter 的 `ADAPTER_AUTH_BEARER`/`HERMES_API_KEY` 是否一致。
