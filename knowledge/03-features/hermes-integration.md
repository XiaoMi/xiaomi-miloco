# Hermes Agent 集成

## 背景与目标

miloco 原本只通过 OpenClaw 插件（`plugins/openclaw/`）接入小米内部的 OpenClaw agent 运行时。为支持开源生态，fork 新增 `plugins/hermes/`，把同样的双向集成移植到 [Hermes Agent](https://github.com/NousResearch/hermes-agent)（Nous Research 的开源 Python agent）。

两条集成路径并列、互不影响，用户按自己装的 agent 运行时二选一。

---

## 产品面

能力与 OpenClaw 版一致：

- **自然语言控制设备**：对 Hermes 说意图，miloco-* skill 经 `miloco-cli` 调后端 API。
- **创建持久任务**：rule / cron / record 组合。
- **主动感知回调**：规则触发、感知等事件经 backend dispatcher → adapter.send_turn() → Hermes /v1/chat/completions 推送；对需投递到 IM 的事件（如 onboarding），adapter 从响应提取回复后调 `hermes send` 送达车主。
- **家庭记忆管理**：`build_system` 按 profile 组装 `<system>` 消息注入上下文（含家庭档案、设备目录、感知记忆路径等）。
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
  → adapter.send_turn(TurnContext) → POST Hermes /v1/chat/completions（直调，X-Hermes-Session-Id 头做会话连续）
  → agent 跑 miloco-notify 或其它 skill
  → 若 turn 标记 deliver=True，adapter 从响应提取回复 → hermes send --to <platform:chat_id> 投递到 IM
```

与 OpenClaw 版的关键差异：Hermes 的 `/v1/chat/completions` 是纯拉取端点，不会主动把回复推回 IM。因此需要 `_deliver_response` 显式投递——这是 OpenClaw webhook（自动推）所不需要的步骤。

### 插件注册点（`plugins/hermes/miloco-plugin/`）

`register(ctx)` 注册：

1. **`pre_llm_call` 钩子**（注册为 noop——上下文组装改由 backend 侧 adapter.build_system 在 send_turn 内完成，以 `<system>` 消息注入，缓存友好）。
2. **3 个 tool**：`miloco_im_push`（通知分发，两段式）、`miloco_notify_bind`（绑定通知目标）、`miloco_habit_suggest`（习惯建议状态机）。
3. **受管 cron reconcile**（`cron_setup.py::reconcile_cron_jobs`）—— 启动时按 `[miloco:home-profile]` 标签对齐 4 个任务，deliver="local"（cron 输出静默，agent 主动调 `miloco_im_push` 才通知）。

### Adapter（`plugins/hermes/miloco-plugin/hermes_adapter/adapter.py`）

**非独立进程**。backend 启动时由 `agent_platform/loader.py` 按 `agent.platform=hermes` 动态加载 adapter adapter.py，存放在 `$MILOCO_HOME/agent_platform/hermes/` 目录下（连同 context_injection/catalog/paths/tools_habit 等依赖文件，loader 以 `submodule_search_locations` 按目录加载）。

对外暴露三个方法（满足 AgentPlatformAdapter duck-typed 契约）：

- `send_turn(ctx)` — 调 Hermes `/v1/chat/completions`（OpenAI 兼容端点），用 `X-Hermes-Session-Id: miloco:<sessionKey>:<lane>` 头维持会话连续；`build_system` 丢线程池执行（避免 catalog CLI 阻塞事件循环）。
- `read_trace_meta(run_id)` — 读 `$MILOCO_HOME/trace/<run_id>.meta.json`（trace.py 常写，adapter 只读）。
- `reset_sessions(routes)` — 切家庭时清理 Hermes 会话（调 `hermes sessions delete` CLI）。

上下文溢出 best-effort 自愈：识别溢出关键词后，丢弃会话上下文用无 session 头的全新 turn 重试一次。

### 与 OpenClaw 集成的关键差异

| 维度                     | OpenClaw 版                                       | Hermes 版                                                                       |
| ------------------------ | ------------------------------------------------- | ------------------------------------------------------------------------------- |
| 插件语言                 | TypeScript                                        | Python                                                                          |
| 上下文注入               | `before_prompt_build` → system prompt             | adapter.build_system → `<system>` 消息（丢线程池，不阻塞事件循环）               |
| 入站回调                 | 插件内 `api.registerHttpRoute("/miloco/webhook")` | backend adapter 直调 Hermes `/v1/chat/completions`                              |
| 同步等 turn              | `api.runtime.subagent.run` + `waitForRun`         | adapter.send_turn 内 httpx POST `/v1/chat/completions`（sync，X-Hermes-Session-Id） |
| get_trace                | 内存 trace buffer，后端反向轮询取 meta            | 文件 IPC：trace.py 写 `$MILOCO_HOME/trace/*.meta.json`，adapter 读盘              |
| 溢出自愈                 | `deleteSession({deleteTranscript:true})` + 重跑   | 丢弃会话上下文、无 session 头全新 turn 重试一次                                 |
| 通知投递                 | subagent run with deliver:true                    | adapter._deliver_response → `hermes send --json --to <platform:chat_id>`       |
| backend 生命周期         | 有（`miloco-cli service restart/stop`）           | 有（supervisord 管理）                                                           |

### 配置共享

三端（backend / CLI / 插件）共用 `$MILOCO_HOME/config.json`：

- `server.token`：backend 独占生成，CLI/插件只读。
- `agent.platform`：设 `"hermes"` 触发 loader 加载 Hermes adapter。
- `agent.auth_bearer`：adapter 与 Hermes 网关间的鉴权，写入 `~/.hermes/.env` 的 `API_SERVER_KEY`。

### 如果我要添加/修改 Skill

skill 源在 `plugins/skills/miloco-*`（OpenClaw/Hermes 共用源），改完跑 `plugins/hermes/scripts/sync-skills.py` 重新生成 `plugins/hermes/skills/` 并复制到 `~/.hermes/skills/`。skill 通过 `miloco-cli` 调后端，与 agent 平台无关。

### 出问题排查

- `GET /api/miot/mips_status` 看 MQTT 连接。
- `miloco-cli service logs -f` 看后端日志。
- Hermes gateway 日志看 stdout/stderr。
- 入站回调不通：确认 `config.json::agent.platform` 为 `"hermes"`、`auth_bearer` 与 `~/.hermes/.env` 中 `API_SERVER_KEY` 一致。
- 出站 skill 不触发：`hermes -z "/miloco-devices 帮我列出设备"` 确认 skill 已装入 `~/.hermes/skills/`。
