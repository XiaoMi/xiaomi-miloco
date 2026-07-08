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
- **全新安装引导**：首次接入且家庭成员与档案均为空时，后端主动发起 onboarding 访谈邀请（直达用户 IM 会话），引导用户一次性建起家庭成员与家庭档案

### 典型场景

**场景 1 — 自然语言设置规则**：用户说"帮我记住，每天早上 7 点叫我起床"。Agent 调用 `miloco-create-task` Skill，装配 cron（每天 7 点定时触发），到点通过音箱 TTS 播报叫醒提示。

**场景 2 — DYNAMIC 规则自主决策**：用户创建了 DYNAMIC 规则"感知到猫咪靠近厨房时处理"。感知到猫进厨房，后端投递 DYNAMIC 回调给 Agent，Agent 在 isolated 会话中读取当前时间、厨房灶台状态，决定是否播报提醒，并通过 `miloco-notify` 路由通知用户。整个过程无需用户参与。

**场景 3 — 新设备欢迎**：用户在米家 App 绑定了新的空气净化器，几秒内音箱播报"已为您接入小米空气净化器，您可以直接对我说'打开净化器'"。

**场景 4 — 全新安装主动引导**：用户刚装好并完成米家授权，此时家庭成员与档案都还是空的。后端检测到"全新安装"，主动在用户的 IM 会话里开口邀请其登记家庭信息，Agent 用 `miloco-onboarding` Skill 分环节访谈，把家庭成员与家庭档案一次性建起来（终身只主动邀请一次）。

### 能力边界

- Agent 运行在 OpenClaw 框架中，Miloco 插件注册 Hook / Webhook / Service / Tool 扩展其能力
- 所有主动通知（感知告警/任务到期/设备欢迎）统一走 `miloco-notify` Skill，不直接调设备 TTS
- DYNAMIC 规则回调在 isolated 会话中运行，文字输出不进对话流，不自动发声
- OpenClaw 框架本身由小米 AI Agent 团队维护，框架问题（agent turn 失败、Cron 不触发）需向该团队反映；框架能力边界与版本兼容见 [OpenClaw SDK](../05-external-deps/sdk-openclaw.md)

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
  读取 $MILOCO_HOME/home-profile/profile.md（home-profile/helpers.ts::loadHomeProfile）
  → 拼成家庭档案块
  → 追加到 Agent system prompt
```

### 插件注册点全貌

插件入口：`plugins/openclaw/src/index.ts`。`register(api)` 必须先调 `loadSharedConfig(api)`（`miloco/config.ts`）再注册其余扩展——它把 gateway 当前认证凭据解析后写入 `config.json::agent.auth_bearer`；若晚于 backend 拉起，backend 读到空 bearer、回调 `/miloco/webhook` 会 401。各扩展点的实现文件与职责如下：

| 扩展类型         | 实现文件                     | 职责                                                                                                                                                                                                   |
| ---------------- | ---------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **Hook**         | `hooks/prompt.ts`            | 唯一的 `before_prompt_build`：装配系统上下文 + 设备目录（catalog）+ 家庭记忆（profile.md）+ 待回应习惯建议块（后者由 `home-profile/injection.ts::buildPendingSuggestionBlock` 提供）                   |
| **Hook**         | `hooks/trace.ts`             | 监听 7 个 agent 事件，turn 结束后生成元数据                                                                                                                                                            |
| **Webhook**      | `webhooks/agent.ts`          | 接收后端所有事件回调，触发 Agent turn                                                                                                                                                                  |
| **Webhook**      | `webhooks/get_trace.ts`      | 后端反向轮询 agent turn 元数据（runId → done/in_progress/unknown）                                                                                                                                     |
| **Webhook**      | `webhooks/reset_sessions.ts` | 后端切换家庭时批量重置（删除）指定 miloco session，清掉旧家庭遗留上下文；逐个 deleteSession，单个失败不影响其余，返回 reset / failed 清单（切换家庭语义见 [设备控制 · Scope 变更](device-control.md)） |
| **Service**      | `services/backend.ts`        | 唯一注册的 Service：插件启动时 `miloco-cli service restart`，停止时 `miloco-cli service stop`                                                                                                          |
| **辅助模块**     | `services/catalog.ts`        | 非注册 Service；由 `hooks/prompt.ts` 在 `before_prompt_build` 中调 `getCatalog`（`miloco-cli device catalog`，节流防抖）                                                                               |
| **Tool**         | `tools/notify.ts`            | 注册 `miloco_im_push`（通知分发）和 `miloco_notify_bind`（通知渠道绑定）两个工具                                                                                                                       |
| **Home Profile** | `home-profile/`              | 家庭档案注入 + 受管 Cron 调度 + `miloco_habit_suggest` 工具                                                                                                                                            |

所有 Webhook 统一挂在 `/miloco/webhook`，`auth: "gateway"` 鉴权，请求体通过 `action` 字段路由到对应处理器。

### Hook 机制

**before_prompt_build Hook（`hooks/prompt.ts`，唯一一处）**

每次 Agent turn 前按会话 profile（`resolveProfile` 分 full / rule / suggestion / minimal）装配系统上下文，分 prepend 指令块（人设 / 能力 / 感知格式 / 通知与输出约定）与 append 数据块（家庭档案、待回应习惯建议、设备目录 catalog）两部分。关键设计决策：isolated cron 走 minimal——剥掉家庭记忆等块与全部 append 数据，避免定时任务继承主 agent 人格。各块装配见 `hooks/prompt.ts`。

**trace Hook**：监听 7 个 agent 生命周期事件，turn 结束后计算 meta（LLM 调用次数、工具调用次数、各类耗时、错误统计）；debug 模式下写 JSONL 到 `$MILOCO_HOME/trace/agent/`；在内存中保留 meta 供后端轮询后消费（幂等消费，消费后即清除）。

### Webhook 通信机制

**agent Webhook**（后端 → plugin → Agent）

后端通过 HTTP POST `/miloco/webhook` 发起，payload 包含 `action`、`message`、`sessionKey`、`traceId`、等待超时，以及可选的投递控制 `deliver` / `resolveTarget`。plugin 侧触发 Agent subagent turn，同步等待，返回 `{runId, status, error}`。

默认 turn 跑在后台会话、结果不投递给用户（`deliver:false`）。当 `resolveTarget:"owner-channel"`（onboarding 邀请用）时，plugin 忽略 payload 的 sessionKey，复用 `tools/notify.ts` 的主人会话解析（单一事实源），整个 turn 直接跑在主人 IM channel 会话里且回复对用户可见——交互式访谈必须让用户的回复与访谈状态落在同一会话。主人从未私聊过 bot、解析不到任何 channel 时，返回结构化 `status:"no-channel"`（`runId:null`，HTTP 正常返回而非传输失败），后端据此按"未送达且不重试传输"处理。

**上下文溢出自愈**：agent turn 因上下文溢出失败时，plugin 无法在原地 reset/clear 会话，只能删除该会话再以原 sessionKey 重跑一次（恒一次、不成环；重建后仍溢出即判定为系统提示自身超预算，停手交后端记录原因）。owner-channel 会话是例外——删除会连用户真实 IM 历史一并删掉，代价远大于一次投递失败，故只记日志、不自愈，交由后端按未送达重试。

**get_trace Webhook**（backend observability 反向轮询）

后端发送 `{action: "get_trace", runId}` → plugin 返回 `{status: "done", ...meta}` 或 `{status: "in_progress"}` 或 `{status: "unknown"}`。状态为 done 时同时从内存清除，保证幂等消费。

### Skill 分组与职责

| 功能域        | Skill 名称                      | 核心职责                                                                                                                                                                                                                   |
| ------------- | ------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **任务管理**  | `miloco-create-task`            | 任务装配（rule / cron / record / lifecycle 组合）与查询 / 启停 / 修改（删除转 `miloco-terminate-task`）；也是感知引擎语音指令类回调消息的处理入口                                                                          |
|               | `miloco-terminate-task`         | 任务终止：清 rule + task（FK 级联清关联），按 agent_pending 清 cron                                                                                                                                                        |
| **家庭记忆**  | `miloco-home-profile`           | 正式档案直编：用户提及家庭成员喜好/习惯/健康/作息/规则时以 `user_told` 直写 / 更新（区别于 observe 走候选区），也处理修正 / 删除与查询                                                                                     |
|               | `miloco-home-observe`           | Dreaming Observe 步：从感知/交互记忆提取可沉淀知识写入候选区                                                                                                                                                               |
|               | `miloco-home-promote`           | Dreaming Promote 步：候选区达到条件的知识晋升为正式档案                                                                                                                                                                    |
|               | `miloco-home-prune`             | Dreaming Prune 步：统一 subject 绑定（reassign 归并成员/空间等分散称呼）后触发 commit（commit 内做过期清理、归档、持久化）                                                                                                 |
|               | `miloco-home-patrol`            | 家庭巡检：结合感知记忆和家庭档案，自动操作设备或发出关怀提醒                                                                                                                                                               |
|               | `miloco-perception-digest`      | 感知日志压缩：将原始感知日志提炼为结构化感知记忆事件记录                                                                                                                                                                   |
|               | `miloco-habit-suggest`          | 每日习惯洞察：从家庭档案识别值得建成任务的习惯并主动推荐                                                                                                                                                                   |
|               | `miloco-onboarding`             | 全新安装首次初始化：分环节访谈把家庭成员写入身份库、成员画像/家规/空间设备注解写入档案                                                                                                                                     |
| **MiOT 操作** | `miloco-devices`                | 设备控制与查询：开关灯/调空调/查状态/触发场景/音箱 TTS/刷新设备缓存                                                                                                                                                        |
|               | `miloco-miot-scope`             | 感知范围控制：管理 miloco 感知哪些家庭和摄像头                                                                                                                                                                             |
|               | `miloco-perception`             | 多模态摄像头感知统一入口：实时视听查询（`perceive query` 调 Omni，有成本）与感知日志回顾（`perceive logs`，零成本优先），也可用画面核实设备开关 / 亮灭状态（尤其设备上报状态拿不到时）；只答画面可见信息，读不出传感器数值 |
|               | `miloco-miot-admin`             | 系统运维：连通性检查（MiOT / SQLite / 感知 / 规则引擎）、家庭信息摘要（设备 / 区域 / 场景 / 成员数量，只读）、感知成本统计（当前未实现）；不含设备缓存刷新（→ miloco-devices）                                             |
| **身份管理**  | `miloco-miot-identity`          | 家庭成员档案 CRUD（创建/列出/重命名/删除成员）                                                                                                                                                                             |
|               | `miloco-miot-identity-register` | 身份注册主流程：上传图/视频直接注册 tier_a，或从陌生人池选取升级                                                                                                                                                           |
| **通知**      | `miloco-notify`                 | 通知分发：选人 → 选通道（TTS/IM/米家推送）→ 生成文案 → 执行；也是通知渠道（IM channel）绑定的入口                                                                                                                          |

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

**任务管理系统**：任务是持久性意图的主体，可装配 rule（感知触发条件）、cron（定时触发）、record（行为统计）三类能力；task↔rule/cron 关联记入 `task_link`（record 不进 link，FK 直连 task）。cron 制品存活在 OpenClaw 侧，backend 不直接操作——维持松耦合。删除任务时，backend 单事务清理 task 记录与关联 rule（FK 级联清 link 与 record），cron 由 Agent 按 agent_pending 清理。任务子系统详见 [任务管理](task-management.md)。

**AgentDispatcher 调度保证**：

- **单飞**：同一 session_key 同一时刻只有一个 drain 任务在途
- **同类合并**：同批次内同类型回调合并为一条 message，减少 Agent turn 数
- **优先级淘汰**：队列超长时，按类型级优先级 → 条目级优先级 → 最旧顺序淘汰
- **投递回执**：`dispatch_event` 返回值仅表示「入队被接纳」、不等于送达；需区分二者的 producer（如 onboarding 终身一次性标记）可传入投递结果 future，由 dispatcher 在每条丢弃 / 送达路径上如实 resolve（平台侧仍在途的超时按已送达计，是唯一反直觉的边界）——onboarding 据此「以真送达为准」标记，杜绝入队即标记却从未送达导致的终身漏发；各路径的判定见 `dispatch/dispatcher.py`
- 五类事件（interaction / bind / onboarding / rule / suggestion）分三条 session 路由：interaction、bind、onboarding 共用主会话（同一 session_key / lane，但属不同合并类型、各自单飞不混入同一 turn），rule、suggestion 各一条；session_key 常量与类型级投递参数（如 onboarding 带 owner-channel 直达投递）见 `dispatch/dispatcher.py`

**通知去重兜底**：通知发送路径加一层相同文案短窗去重——agent 陷入循环或串行重发时，同一条文案在窗口内只投递一次，不被 1:1 放大成一串重复推送。米家推送（后端 `MiotService.send_notify`）与 `miloco_im_push`（插件 `notifyOwner`）是两条相互独立的发送路径，各自维护去重，且仅在成功投递后记账（失败可立即重试）。这是串行循环的兜底、不为真正的并发双发加锁；窗口在 `config.json` 的 `notify.dedup_window_sec` 配置（后端与插件共读，`<=0` 关闭）。

**通知渠道绑定兜底（不丢首条主动通知）**：`miloco_im_push` 面对「用户尚未绑定通知渠道」的冷启动时不直接失败，而走两段式 `needsBind` 契约——先回传绑定引导让 agent 补一句提示并带 `bindHint` 原文重发，重发时才把通知兜底投递到最近活跃会话，避免首条主动通知因「还没绑定」而丢失。用户显式绑定走 `miloco_notify_bind`，把当前会话记为固定通知渠道（`notifySessionKey`）。两段式重发与 `bindHint` 模板的完整契约语义见 skill 的 `references/channel-config.md`，与 `tools/notify.ts` 单一事实源同步。

**习惯建议防骚扰状态机**：每日习惯洞察由两个不共享上下文的 agent 衔接——扫描 agent（isolated cron）从家庭档案识别可建成任务的习惯并主动 IM 推荐（路径 A），回应 agent（用户主会话）在用户回应后加载 `miloco-create-task` 落地（路径 B）；二者通过 `miloco_habit_suggest` 工具的持久候选库（`home-profile/suggestions.ts`）衔接。设计核心是让工具而非 agent 充当防骚扰权威：候选在 `pending → asked →（accepted → created）| rejected | expired` 状态机内流转，防骚扰闸门（待回应位与每日新推上限、拒绝抑制、过期重推等）由工具裁定并拒绝越界写入，具体闸门与节奏常量见 `suggestions.ts`。一个关键不变量：`asked` 严格等价「已确认送达」——须 `miloco_im_push` 成功后才能 `mark_asked`，杜绝通知未送达却翻状态导致的静默死锁或次日重复打扰。待回应建议向 system prompt 的注入见「Hook 机制」，习惯建成任务后从家庭档案渲染中剔除见 [家庭记忆](home-profile.md)。

**家庭巡检去重（隔离会话状态接力）**：`miloco-home-patrol` 每轮做两件事——依家庭档案偏好/规则自动控制设备（经 `miloco-devices`）、对值得关心的事件发关怀提醒（经 `miloco-notify`）。它由受管 cron 每轮拉起一个隔离会话、无跨轮记忆，不预注入任何数据（所需数据全部自取）；若不加约束会把同一事件/操作每轮重复处理。设计上靠一份按天归档的**巡检日志（已处理台账）**在隔离会话间接力「已做过什么」——每轮先读台账、只做没做过的、处理后把新动作写回台账，靠语义去重（同一主体 + 同一类问题即同一件事）静默重复项。回看采用固定时间窗而非精确时间游标：感知记忆由 `miloco-perception-digest` 异步追加、按天归档且事件行只带时刻，用游标「只处理某时刻之后」会漏掉迟到写入的事件；固定回看 + 台账去重则既不漏也不重复。缺席型安全信号（如老人长时间无活动、成员远超回家时间未归）以「事件缺席」为判据，不受该窗限制，另按主体上次活动时刻回溯历史评估。

### 如果我要添加/修改 Skill

修改步骤、构建命令和 Skill 标准结构见 [开发指南 · 场景三：添加或修改 Skill](../06-dev-guide/dev-guide.md#场景三添加或修改-skill)。Skill 通过 `miloco-cli` 向后端发请求，鉴权通过 `Authorization: Bearer <token>` 头传递。调试日志：`$MILOCO_HOME/log/openclaw-plugin.log`。

### 任务相关 API 路径

主要入口：`POST /api/tasks`（创建任务）、`DELETE /api/tasks/{task_id}`（删除任务，级联清理关联 rule），完整端点见 `task/router.py`。

### 与其他模块的关系

**上游（向 Agent 投递）**：感知流水线的 `speeches` 和 `suggestions` 经 `dispatch_event` 投递。DYNAMIC 规则命中后经 `dispatch_event("rule", ...)` 投递。设备到达时 `DeviceWelcomeService` 经 `dispatch_event("bind", ...)` 投递欢迎消息。全新安装且家庭信息为空时，`OnboardingTriggerService`（`home_profile/onboarding_trigger.py`）经 `dispatch_event("onboarding", ...)` 主动发起家庭信息初始化邀请（终身一次，以真送达为准）。

**下游（Agent 操作）**：Agent 通过 `miloco-devices` Skill 执行设备控制。通过 `miloco-create-task` / `miloco-terminate-task` 操作规则和任务。通过 `miloco-home-profile` / `miloco-home-observe` 等 Skill 写入家庭档案。

### 配置共享

三端（backend / CLI / plugin）共用 `$MILOCO_HOME/config.json` 完成鉴权与 Webhook 寻址（`server.token` / `agent.webhook_url` / `agent.auth_bearer`）；各键的归属、读写方与默认值指路见 [OpenClaw SDK · 配置共享](../05-external-deps/sdk-openclaw.md)。
