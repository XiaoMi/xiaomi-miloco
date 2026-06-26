# Hermes Agent 集成

## 背景与目标

miloco 原本只通过 OpenClaw 插件（`plugins/openclaw/`）接入小米内部的 OpenClaw agent 运行时。为支持开源生态，fork 新增 `plugins/hermes/`，把同样的双向集成移植到 [Hermes Agent](https://github.com/NousResearch/hermes-agent)（Nous Research 的开源 Python agent，与 OpenClaw 同类，内置 `hermes claw migrate` 迁移路径）。

两条集成路径并列、互不影响，用户按自己装的 agent 运行时二选一。

---

## 产品面

能力与 OpenClaw 版一致：

- **自然语言控制设备**：对 Hermes 说意图，miloco-\* skill 经 `miloco-cli` 调后端 API。
- **创建持久任务**：rule / cron / record 组合。
- **主动感知回调**：规则触发、设备绑定、感知告警时，后端经适配进程投递 DYNAMIC 回调给 Hermes，agent 在对应会话自主决策。
- **家庭记忆管理**：对话写入档案，`pre_llm_call` 钩子把档案注入上下文。
- **后台知识整理**：4 个受管 cron（perception-digest / home-patrol / home-dreaming / habit-suggest）。

---

## 研发面

### 架构（数据流）

#### Agent → Miloco（出站）

```
用户对话
  → Hermes 选 miloco-* skill（/skills 或自然语言）
  → miloco-cli 调 HTTP API（Authorization: Bearer <token>）
  → MiotService / RuleService / PersonService / TaskService
```

#### Miloco → Agent（入站回调）

```
感知结果 / 规则触发 / 设备绑定
  → AgentDispatcher（dispatch/dispatcher.py，单飞+合并+优先级淘汰）
  → run_agent_turn → POST {action:"agent",payload} → 适配进程(adapter/)
  → adapter 调 Hermes api_server POST /v1/chat/completions（同步，X-Hermes-Session-Id 头做会话连续）
  → agent 跑 miloco-notify 或其它 skill
```

### 插件注册点（`plugins/hermes/miloco-plugin/`）

`register(ctx)` 注册：

1. **`pre_llm_call` 钩子**（`context_injection.py::inject_context`）—— 按 profile 分级装配上下文，返回 `{"context": text}` 注入 user message。profile 判定：`platform=="cron"` 或 session_id 含 `:cron:` → minimal；含 `miloco-rule` → rule；含 `miloco-suggest` → suggestion；其余 → full。
2. **3 个 tool**：`miloco_im_push`（通知分发，两段式）、`miloco_notify_bind`（绑定通知目标）、`miloco_habit_suggest`（习惯建议状态机）。
3. **受管 cron reconcile**（`cron_setup.py::reconcile_cron_jobs`）—— 启动时按 `[miloco:home-profile]` 标签对齐 4 个任务。

### 入站适配进程（`plugins/hermes/adapter/`）

独立 Python 进程（aiohttp），因为 **Hermes 插件 ctx API 不能注册任意 HTTP 路由**（只能注册 tool/hook/command/skill/...），而 miloco 后端要求一个 `{action, payload} → {code, data:{runId,status}}` 的同步 webhook 端点——契约也与 Hermes 的 webhook（异步 deliver）/ api_server（`{run_id, status}`）都不匹配，故需独立适配层翻译。

- `POST /miloco/webhook`，Bearer 鉴权。
- `action:"agent"` → `hermes_client.run_turn` → 调 `POST /v1/chat/completions`（OpenAI 兼容同步端点），用 `X-Hermes-Session-Id: miloco:<sessionKey>:<lane>` 头维持跨回合会话连续；HTTP 超时 = `timeoutMs/1000 + 15`，返回 `{code:0, data:{runId, status, error?, recovered?}}`。
- `action:"get_trace"` → `{code:0, data:{status:"done"}}`（observability 降级）。
- 幂等：`idempotencyKey` 命中内存缓存（TTL 1h）直接回缓存。
- 上下文溢出 best-effort 自愈：识别溢出错误文案后，丢弃会话上下文用无 `X-Hermes-Session-Id` 的全新 turn 重试一次（api_server 无 session 删除/重置路由）。
- session 映射：`miloco:<sessionKey>:<lane>`（确定性，`session_map.py`）。

### 与 OpenClaw 集成的关键差异

| 维度                     | OpenClaw 版                                       | Hermes 版                                                                       |
| ------------------------ | ------------------------------------------------- | ------------------------------------------------------------------------------- |
| 插件语言                 | TypeScript                                        | Python                                                                          |
| 上下文注入               | `before_prompt_build` → system prompt             | `pre_llm_call` → user message                                                   |
| 入站回调                 | 插件内 `api.registerHttpRoute("/miloco/webhook")` | 独立适配进程（Hermes 插件不能注册 HTTP 路由）                                   |
| 同步等 turn              | `api.runtime.subagent.run` + `waitForRun`         | Hermes api_server `/v1/chat/completions` 同步（`X-Hermes-Session-Id` 会话连续） |
| get_trace                | 内存 trace buffer，后端反向轮询取 meta            | 直接回 done，不回传 meta（降级）                                                |
| 溢出自愈                 | `deleteSession({deleteTranscript:true})` + 重跑   | 丢弃会话上下文、无 session 头全新 turn 重试一次                                 |
| 通知投递                 | `subagent.run({deliver:true})`                    | `ctx.dispatch_tool("send_message", ...)`                                        |
| backend 生命周期 Service | 有（`miloco-cli service restart/stop`）           | 无（backend 独立运行）                                                          |

### 配置共享

三端（backend / CLI / 插件）仍共用 `$MILOCO_HOME/config.json`：

- `server.token`：backend 独占生成，CLI/插件只读。
- `agent.webhook_url`：Hermes 版指向适配进程（`http://127.0.0.1:18789/miloco/webhook`）。
- `agent.auth_bearer`：与适配进程启动时的 `ADAPTER_AUTH_BEARER` 一致。

### 如果我要添加/修改 Skill

skill 源在 `plugins/skills/miloco-*`（OpenClaw/Hermes 共用源），改完跑 `plugins/hermes/scripts/sync-skills.py` 重新生成 `plugins/hermes/skills/` 并复制到 `~/.hermes/skills/`。skill 通过 `miloco-cli` 调后端，与 agent 平台无关。

### 出问题排查

- `GET /api/miot/mips_status` 看 MQTT 连接。
- `miloco-cli service logs -f` 看后端日志。
- 适配进程日志看 stdout/stderr（建议常驻 + 重定向）。
- 入站回调不通：先 `curl http://127.0.0.1:18789/health` 探 adapter，再确认 miloco `config.json::agent.webhook_url` 与 `auth_bearer`、Hermes `API_SERVER_KEY`、`HERMES_API_KEY` 三者一致。
- 出站 skill 不触发：`hermes -z "/miloco-devices 帮我列出设备"` 确认 skill 已装入 `~/.hermes/skills/`。
