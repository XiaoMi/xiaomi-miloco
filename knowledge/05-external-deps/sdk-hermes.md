# Hermes Agent 依赖

## L1：它是什么

Hermes Agent 是 [Nous Research](https://nousresearch.com) 的开源 AI Agent（MIT，Python），与小米内部的 OpenClaw 同类——一个自托管、可对接 Telegram/Discord/Slack 等、带记忆与 skill 系统的 agent 运行时。仓库 [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)，文档 [hermes-agent.nousresearch.com/docs](https://hermes-agent.nousresearch.com/docs)。

miloco 通过 `plugins/hermes/` 接入 Hermes，作为 OpenClaw 之外的并列 agent 运行时。

---

## L2：我们怎么用

### 扩展点映射

| miloco 需求                  | Hermes 机制                                                                    | 文件                                                     |
| ---------------------------- | ------------------------------------------------------------------------------ | -------------------------------------------------------- |
| 16 个 skill                  | `~/.hermes/skills/`（agentskills.io 标准，miloco skill 已合规）                | `scripts/sync-skills.py`                                 |
| 注入设备目录/家庭档案/身份块 | adapter.build_system → `<system>` 消息（backend 侧组装，不依赖插件钩子）       | `miloco-plugin/context_injection.py`                     |
| 注册 tool（通知/习惯建议）   | `ctx.register_tool(name, toolset, schema, handler)`                            | `miloco-plugin/tools_*.py`                               |
| 4 个受管 cron                | `cron.jobs.create_job(prompt, schedule, skills=[...])` + reconcile             | `miloco-plugin/cron_setup.py`                            |
| 后端→agent 同步回调          | adapter.send_turn → httpx POST `/v1/chat/completions`（X-Hermes-Session-Id）   | `plugins/hermes/miloco-plugin/hermes_adapter/adapter.py` |

### 关键契约

**插件 ctx API**（`website/docs/guides/build-a-hermes-plugin.md`）：

- `ctx.register_tool(name=, toolset=, schema=, handler=, check_fn=, emoji=, override=)` —— schema 是 OpenAI tool-schema dict；handler `def(args: dict, **kw) -> str`。
- `ctx.register_hook(event, cb)` —— 事件含 `pre_llm_call`/`post_llm_call`/`pre_tool_call`/`post_tool_call`/`on_session_*`/`subagent_stop`/`pre_gateway_dispatch` 等。miloco 的 `pre_llm_call` 钩子注册为 noop——上下文组装已从插件钩子迁移到 backend 侧 `adapter.build_system`，以 `<system>` 消息注入，避免 tool 循环中重复触发。
- `ctx.dispatch_tool(name, arguments)` —— 调任意已注册 tool。

**api_server 同步 chat**（`gateway/platforms/api_server.py`）：

- `POST /v1/chat/completions`（OpenAI 兼容同步端点），body `{"messages":[{"role":"system","content":...},{"role":"user","content":...}]}`，响应 `{"choices":[{"message":{"role":"assistant","content":...}}],"usage":{}}`。
- 会话连续：请求头 `X-Hermes-Session-Id: <id>` —— Hermes 从 state.db 加载该 session 的历史。adapter 用 `miloco:<sessionKey>:<lane>` 作 id。suggest 车道用 `miloco:<sessionKey>:<lane>:<uuid>` 唯一后缀（不复用历史，避免 token 累积）。
- 鉴权：`Authorization: Bearer $API_SERVER_KEY`。`API_SERVER_KEY` 环境变量设置即自动启用 api_server 平台（默认端口 8642）。
- 溢出自愈：adapter 识别溢出关键词后，用无 `X-Hermes-Session-Id` 的全新 turn 重试一次。

**cron**（`cron/jobs.py::create_job`）：

- `create_job(prompt, schedule, name=None, skills=None, deliver=None, ...)`。
- `deliver` 是 **Platform enum 字符串**（`"telegram"/"feishu"/"weixin"/"discord"/...`）；miloco 受管任务统一传 `deliver="local"`（cron 输出静默，由 agent 主动调 `miloco_im_push` 才通知——对齐 OpenClaw 设计）。**不能用字面量** `"all"/"none"` —— `Platform._missing_()` 拒绝未知值，会 fallback 到 `Platform.LOCAL`。
- 无 `description` 字段，故把 `[miloco:home-profile]` 标签塞进 `name` 前缀作 reconcile 识别键。

### 版本兼容约束

- 依赖 Hermes api_server `/v1/chat/completions` 路由（v0.10.0 实测）。
- 插件级配置：Hermes `PluginManifest` 当前不解析 `configSchema`，故运行时配置由插件自管于 `<plugin-dir>/state.json`。
- 需先安装 Hermes（`curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash`）。
- Hermes v0.10.0 的 cron 调度依赖 `croniter` 包（`pip install croniter`）。
- skill frontmatter 的 `date` 字段必须加引号（字符串），`sync-skills.py` 已自动处理。

### 与后端的通信契约

backend dispatcher 构造 `TurnContext`，调 `adapter.send_turn(ctx)`（AgentPlatformAdapter 契约）。Hermes adapter 把 TurnContext 翻译为 Hermes `/v1/chat/completions` 请求，同步返回 `{"status": "ok"|"error"|...}`。对 `deliver=True` 的 turn，adapter 额外从响应提取回复文本，经 `hermes send --to <platform:chat_id>` 投递到车主 IM。参数与返回值定义见 `plugins/hermes/miloco-plugin/hermes_adapter/adapter.py`。

### 出问题找谁

- Hermes 框架本身（agent turn 失败、cron 不触发、api_server 连不通）→ Hermes 仓库 issue。
- miloco 适配层（adapter 翻译、tool 注册）→ miloco 工程侧。
- 排查顺序：先确认 Hermes gateway + api_server 起着、`API_SERVER_KEY` 与 `config.json::agent.auth_bearer` 一致；再看 `miloco-cli service logs`。
