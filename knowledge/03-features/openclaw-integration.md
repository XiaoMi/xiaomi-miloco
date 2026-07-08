# Agent 集成

## 背景与目标

设备控制、感知、规则等能力已经就位，但用户如何与它们交互？用户不会直接调 REST API，他们说"帮我设置一个规则，当爷爷长时间没动静时提醒我"。

Miloco 通过 OpenClaw 插件与 AI Agent 深度集成，形成双向通信闭环：

- **Agent → Miloco**：用户的自然语言请求，由 Agent 选择对应的 Skill，通过 CLI 调用后端 API
- **Miloco → Agent**：感知/规则触发的 DYNAMIC 回调，由后端主动向 Agent 发消息，驱动 Agent 自主执行

---

## 产品面

### 能做什么

- **自然语言控制设备**：对 Agent 描述意图，Skill 负责选择正确的设备和参数，无需知道设备 ID
- **创建持久任务**：Agent 将"记住这件事"类需求装配为任务（rule 条件自动化 / cron 定时提醒 / record 行为统计 自由组合），独立运转
- **主动感知回调**：规则触发、语音指令、陌生设备绑定时，后端主动联系 Agent，Agent 自主决策响应
- **家庭记忆管理**：对话中告知家庭信息，Agent 写入档案，形成长期记忆
- **后台知识整理**：受管 Cron 任务定期从感知日志和对话历史中提炼家庭知识、推荐可建任务

### 典型场景

**场景 1 — 自然语言设置规则**：用户说"帮我记住，每天早上 7 点叫我起床"。Agent 调用 `miloco-create-task` Skill，装配 cron（每天 7 点定时触发），到点通过音箱 TTS 播报叫醒提示。

**场景 2 — DYNAMIC 规则自主决策**：用户创建了 DYNAMIC 规则"感知到猫咪靠近厨房时处理"。感知到猫进厨房，后端投递 DYNAMIC 回调给 Agent，Agent 在 isolated 会话中读取当前时间、厨房灶台状态，决定是否播报提醒，并通过 `miloco-notify` 路由通知用户。整个过程无需用户参与。

**场景 3 — 新设备欢迎**：用户在米家 App 绑定了新的空气净化器，几秒内音箱播报"已为您接入小米空气净化器，您可以直接对我说'打开净化器'"。

### 能力边界

- Agent 运行在 OpenClaw 框架中，Miloco 插件注册 Hook / Webhook / Service / Tool 扩展其能力
- 所有主动通知（感知告警/任务到期/设备欢迎）统一走 `miloco-notify` Skill，不直接调设备 TTS
- DYNAMIC 规则回调在 isolated 会话中运行，文字输出不进对话流，不自动发声
- OpenClaw 框架本身由小米 AI Agent 团队维护，框架问题（agent turn 失败、Cron 不触发）需向该团队反映

---

## 研发面

### 架构概览（数据流图）

#### Agent → Miloco（主动控制）

```
用户对话
  → OpenClaw Agent 选 Skill
  → miloco-cli 调 HTTP API（Authorization: Bearer <token>）
  → MiotService / RuleService / PersonService / TaskService
```

#### Miloco → Agent（主动回调）

```
感知结果 / 规则触发 / 设备绑定
  → AgentDispatcher（dispatch/dispatcher.py）
      同 session_key 单飞 + 同类合并 + 优先级淘汰
  → run_agent_turn → POST /miloco/webhook（OpenClaw）
  → Webhook handler → 触发 Agent subagent turn
  → Agent isolated 会话执行 → miloco-notify 或其他 Skill
```

#### 家庭记忆注入链路

```
HomeProfileService.commit() → profile.md 写盘
  ↓
before_prompt_build Hook（plugins/openclaw/src/hooks/prompt.ts）
  读取 $MILOCO_HOME/home-profile/profile.md（helpers.ts::loadHomeProfile）
  → 拼成家庭档案块
  → 追加到 Agent system prompt
```

### 插件注册点全貌

插件入口：`plugins/openclaw/src/index.ts`。`register(api)` 按以下顺序注册：

1. `registerServices(api)` — 只注册 `backend.ts`（管理 Python 后端生命周期）；`catalog.ts` 不是注册的 Service，而是 `before_prompt_build` Hook 内调用的辅助模块（构建设备目录）
2. `registerHooks(api)` — before_prompt_build + trace 相关 Hook
3. `registerHttpRoutes(api)` — `/miloco/webhook` 路由
4. `registerHomeProfile(api)` — 家庭档案调度和注入，注册 `miloco_habit_suggest` 工具
5. `registerNotifyTool(api)` — 注册 `miloco_im_push` / `miloco_notify_bind` 两个通知工具

各模块详情：

| 扩展类型         | 实现文件                | 职责                                                                                                                                                                                 |
| ---------------- | ----------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **Hook**         | `hooks/prompt.ts`       | 唯一的 `before_prompt_build`：装配系统上下文 + 设备目录（catalog）+ 家庭记忆（profile.md）+ 待回应习惯建议块（后者由 `home-profile/injection.ts::buildPendingSuggestionBlock` 提供） |
| **Hook**         | `hooks/trace.ts`        | 监听 7 个 agent 事件，turn 结束后生成元数据                                                                                                                                          |
| **Webhook**      | `webhooks/agent.ts`     | 接收后端所有事件回调，触发 Agent turn                                                                                                                                                |
| **Webhook**      | `webhooks/get_trace.ts` | 后端反向轮询 agent turn 元数据（runId → done/in_progress/unknown）                                                                                                                   |
| **Service**      | `services/backend.ts`   | 唯一注册的 Service：插件启动时 `miloco-cli service restart`，停止时 `miloco-cli service stop`                                                                                        |
| **辅助模块**     | `services/catalog.ts`   | 非注册 Service；由 `hooks/prompt.ts` 在 `before_prompt_build` 中调 `getCatalog`（`miloco-cli device catalog`，节流防抖）                                                             |
| **Tool**         | `tools/notify.ts`       | 注册 `miloco_im_push`（通知分发）和 `miloco_notify_bind`（通知渠道绑定）两个工具                                                                                                     |
| **Home Profile** | `home-profile/`         | 家庭档案注入 + 受管 Cron 调度 + `miloco_habit_suggest` 工具                                                                                                                          |

所有 Webhook 统一挂在 `/miloco/webhook`，`auth: "gateway"` 鉴权，请求体通过 `action` 字段路由到对应处理器。

### Hook 机制

**before_prompt_build Hook（`hooks/prompt.ts`，唯一一处，按段追加）**

每次 Agent turn 前装配并追加：系统上下文（技能路由表、感知记忆说明、工作方式）、设备目录（catalog）、硬约束（禁止 inline sleep / 行为跟踪必须走任务 / 通知必须走 miloco-notify Skill）、家庭记忆说明 + profile.md 内容（`helpers.ts::loadHomeProfile`）、待回应习惯建议块（`home-profile/injection.ts::buildPendingSuggestionBlock`）。

**trace Hook**：监听 7 个 agent 生命周期事件，turn 结束后计算 meta（LLM 调用次数、工具调用次数、各类耗时、错误统计）；debug 模式下写 JSONL 到 `$MILOCO_HOME/trace/agent/`；在内存中保留 meta 供后端轮询后消费（幂等消费，消费后即清除）。

### Webhook 通信机制

**agent Webhook**（后端 → plugin → Agent）

后端通过 HTTP POST `/miloco/webhook` 发起，payload 包含 `action`、`message`、`sessionKey`、`traceId` 和等待超时。plugin 侧触发 Agent subagent turn，同步等待，返回 `{runId, status, error}`。

**get_trace Webhook**（backend observability 反向轮询）

后端发送 `{action: "get_trace", runId}` → plugin 返回 `{status: "done", ...meta}` 或 `{status: "in_progress"}` 或 `{status: "unknown"}`。状态为 done 时同时从内存清除，保证幂等消费。

### 16 个 Skill 分组与职责

| 功能域        | Skill 名称                      | 核心职责                                                                                    |
| ------------- | ------------------------------- | ------------------------------------------------------------------------------------------- |
| **任务管理**  | `miloco-create-task`            | 任务装配（rule / cron / record / lifecycle 组合）；也是感知引擎语音指令类回调消息的处理入口 |
|               | `miloco-terminate-task`         | 任务终止：清 rule + task（FK 级联清关联），按 agent_pending 清 cron                         |
| **家庭记忆**  | `miloco-home-profile`           | 档案直写：用户提及家庭成员喜好/习惯/规则时写入或更新正式档案                                |
|               | `miloco-home-observe`           | Dreaming Observe 步：从感知/交互记忆提取可沉淀知识写入候选区                                |
|               | `miloco-home-promote`           | Dreaming Promote 步：候选区达到条件的知识晋升为正式档案                                     |
|               | `miloco-home-prune`             | Dreaming Prune 步：统一 subject 绑定、清理过期数据、触发 commit 持久化                      |
|               | `miloco-home-patrol`            | 家庭巡检：结合感知记忆和家庭档案，自动操作设备或发出关怀提醒                                |
|               | `miloco-perception-digest`      | 感知日志压缩：将原始感知日志提炼为结构化感知记忆事件记录                                    |
|               | `miloco-habit-suggest`          | 每日习惯洞察：从家庭档案识别值得建成任务的习惯并主动推荐                                    |
| **MiOT 操作** | `miloco-devices`                | 设备控制与查询：开关灯/调空调/查状态/触发场景/音箱 TTS                                      |
|               | `miloco-miot-scope`             | 感知范围控制：管理 miloco 感知哪些家庭和摄像头                                              |
|               | `miloco-perception`             | 感知统一入口：传感器直读、多模态摄像头感知                                                  |
|               | `miloco-miot-admin`             | 系统运维：连通性检查/设备缓存管理/强制刷新/感知成本统计                                     |
| **身份管理**  | `miloco-miot-identity`          | 家庭成员档案 CRUD（创建/列出/重命名/删除成员）                                              |
|               | `miloco-miot-identity-register` | 身份注册主流程：上传图/视频直接注册 tier_a，或从陌生人池选取升级                            |
| **通知**      | `miloco-notify`                 | 通知分发：选人 → 选通道（TTS/IM/米家推送）→ 生成文案 → 执行                                 |

### Home Profile 调度机制（TS 侧）

`home-profile/scheduler.ts` 管理四个受管 cron 任务（以 `[miloco:home-profile]` 标签标识）：

| 任务名                     | 调度频率           | 会话模式 |
| -------------------------- | ------------------ | -------- |
| `miloco-perception-digest` | 高频（分钟级）     | isolated |
| `miloco-home-patrol`       | 中频（数十分钟级） | isolated |
| `miloco-home-dreaming`     | 每日深夜           | isolated |
| `miloco-habit-suggest`     | 每日               | isolated |

具体调度时间定义在 `scheduler.ts`，插件升级后 reconcile 自动对齐，无需手动管理。

### 关键设计决策

**Catalog 注入机制**：每次 Agent turn 前，`before_prompt_build` Hook 中调 `miloco-cli device catalog`（节流防止短期 spam）。catalog 是 TSV 格式文本，列出最近操作过的高频设备及其 spec，让 Agent 在 system context 中直接看到最相关设备，无需每次调 `device list`。

**任务管理系统**：任务是持久性意图的主体，可装配 rule（感知触发条件）、cron（定时触发）、record（行为统计）三类能力；rule / cron 通过各自的 `task_id` FK CASCADE 挂到 task。cron 分两类：`internal` 由 backend + APScheduler MemoryJobStore 完整管理并触发；`external` 是 v1 遗留数据，backend 只存引用、触发仍走 OpenClaw 老通路，随用户 update / task delete 逐步消亡。删除任务时 backend 单事务写 audit + 删 task（FK CASCADE 一并清 rule / cron / record），残留的 external cron 由 Agent 按 agent_pending 通过自然语言指令让 OpenClaw 侧下线。任务子系统详见 [任务管理](task-management.md)。

**AgentDispatcher 调度保证**：

- **单飞**：同一 session_key 同一时刻只有一个 drain 任务在途
- **同类合并**：同批次内同类型回调合并为一条 message，减少 Agent turn 数
- **优先级淘汰**：队列超长时，按类型级优先级 → 条目级优先级 → 最旧顺序淘汰
- 四类事件（interaction / bind / rule / suggestion）分三条 session 路由：interaction 与 bind 共用主会话（同一 session_key / lane，但属不同合并类型、各自单飞不混入同一 turn），rule、suggestion 各一条；session_key 常量见 `dispatch/dispatcher.py`

### 如果我要添加/修改 Skill

修改步骤、构建命令和 Skill 标准结构见 [开发指南 · 场景三：添加或修改 Skill](../06-dev-guide/dev-guide.md#场景三添加或修改-skill)。Skill 通过 `miloco-cli` 向后端发请求，鉴权通过 `Authorization: Bearer <token>` 头传递。调试日志：`$MILOCO_HOME/log/openclaw-plugin.log`。

### 任务相关 API 路径

主要入口：`POST /api/tasks`（创建任务）、`DELETE /api/tasks/{task_id}`（删除任务，级联清理关联 rule），完整端点见 `task/router.py`。

### 与其他模块的关系

**上游（向 Agent 投递）**：感知流水线的 `speeches` 和 `suggestions` 经 `dispatch_event` 投递。DYNAMIC 规则命中后经 `dispatch_event("rule", ...)` 投递。设备到达时 `DeviceWelcomeService` 经 `dispatch_event("bind", ...)` 投递欢迎消息。

**下游（Agent 操作）**：Agent 通过 `miloco-devices` Skill 执行设备控制。通过 `miloco-create-task` / `miloco-terminate-task` 操作规则和任务。通过 `miloco-home-profile` / `miloco-home-observe` 等 Skill 写入家庭档案。

### 配置共享

三端（backend / CLI / plugin）共用 `$MILOCO_HOME/config.json`：

- `server.token`：backend 独占生成，CLI / 插件只读
- `agent.webhook_url`：backend 调 plugin 的 Webhook 地址（默认值见 `settings.schema.json::agent.webhook_url`）
- `agent.auth_bearer`：由插件启动时框架认证解析写入
