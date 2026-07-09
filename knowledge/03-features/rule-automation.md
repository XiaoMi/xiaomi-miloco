# 规则自动化

## 背景与目标

智能家居的核心价值是"自动"——用户不需要手动操作，系统感知到情况后自动响应。传统智能家居的规则基于精确传感器（温度达到阈值、门磁打开），无法处理复杂语义场景（"老人摔倒了"、"孩子开始做作业了"）。

规则自动化让用户用自然语言描述"当 X 时，做 Y"。Miloco 感知到 X 后自动执行 Y——不需要写代码，不需要记住设备 API，VLM 负责语义判断。

---

## 产品面

### 能做什么

#### 四象限规则矩阵

|             | event 模式                                      | state 模式                            |
| ----------- | ----------------------------------------------- | ------------------------------------- |
| **STATIC**  | 单次触发，直接执行预设设备指令                  | 进入/退出状态时，各自执行预设设备指令 |
| **DYNAMIC** | 单次触发，交给 Agent 在 isolated 会话中自主决策 | 进入/退出状态时，各自交给 Agent 决策  |

- **event 模式**：条件由 False 变 True 时触发一次，不关注持续时长和何时退出
- **state 模式**：条件进入（ENTERED）触发 on_enter 动作，持续检测，条件退出（EXITED，带去抖延迟）触发 on_exit 动作；两个方向可独立配置 STATIC 或 DYNAMIC
- **STATIC**：低延迟、高确定性，直接执行预先写死的设备指令
- **DYNAMIC**：规则只写意图描述，触发时交给 Agent 结合当时上下文决定具体操作
- **生命周期**：permanent（永久存在）和 temporary（Agent 判断终止条件后自删）两种
- **duration 扩展**：event 和 state 模式均支持可选的滑动窗口累计触发——设置后条件需在窗口内达到指定比例才触发，而非单帧 True 即触发

### 典型场景

**场景 1 — STATIC state 规则**：用户创建规则"当有人在书房时，保持台灯开启；人离开后关灯"。感知识别到有人进入书房（ENTERED），台灯打开；人离开超过退出防抖时长（EXITED），台灯关闭。全程无 LLM 调用，延迟极低。

**场景 2 — DYNAMIC event 规则**：用户创建规则"当感知到孩子开始哭泣时，自动处理"——不指定具体操作。感知到哭泣时，DYNAMIC 规则触发，Agent 在 isolated 会话中读取当前时间和家庭状态，自主决定：白天可能通知家长，深夜可能轻柔播放音乐。

**场景 3 — temporary 规则**：Agent 帮用户创建"等快递到了通知我"的临时监控。规则 lifecycle 为 temporary，快递员进门事件被感知后，Agent 播报通知，再自动删除该规则，不留后台垃圾。

**场景 4 — duration 滑窗规则**：用户创建规则"孩子在书房认真学习超过 45 分钟，提醒他休息"。配置 `duration_seconds` 和 `duration_ratio`，窗口内 True 比例达阈值才触发，防止 VLM 单帧误判触发误报。

### 能力边界

- 规则条件以自然语言描述，由 Omni VLM 在每个感知窗口评估，结果非确定性
- 规则执行依赖感知流水线持续运行，感知引擎停止时规则不会触发
- 不支持基于精确传感器数值的条件（如"温度高于 28 度"），需通过 VLM 语义推理
- DYNAMIC 规则的 Agent isolated 会话的文字输出不进主对话流，不自动发声；需通过 `miloco-notify` Skill 路由才能让用户感知到
- 规则名不可重复，创建/更新遇重名冲突失败（`ConflictException`）
- condition.query 不能以"检测到/识别到/感知到"等断言性词汇开头（会导致 VLM 将条件视为已发生事实而连续误触发）

---

## 研发面

### 架构概览（数据流图）

```
感知推理完成（OmniOutput.matched_rules）
  → PerceptionEngineProxy（perception/client.py）结果后处理
  → 剔除「当期已达标」的 event 规则（关联 task 的活跃期 record 已达目标，静默不再触发）
  → 本轮下发到各摄像头的规则上报 True/False（未下发到某摄像头的规则不参与其状态推退）
  → RuleService.update_state → RuleRunner 帧级状态机（rule/runner.py）
      帧级抗抖 → 多 source OR 聚合 → duration 滑窗采样（如配置）
      → 状态机 diff（ENTERED / EXITED / STILL_IN / STILL_OUT）
      → ENTERED → 触发分发
           ├─ STATIC → 执行设备动作 → MiotProxy → 米家设备
           └─ DYNAMIC → AgentDispatcher
                       → run_agent_turn → OpenClaw Webhook
                       → Agent isolated 会话 → Skill 执行
         EXITED（state 模式）→ 去抖延迟任务 → fire on_exit slot

  另有 TARGET_FIRED 事件（不走 condition diff）：state 规则关联 duration record 时，
  RuleRunner 内部 timer 在当日累计时长达 target_minutes 瞬间 fire on_target slot（Agent 回调）
```

### 核心模块

| 类                   | 文件                          | 职责                                                                                                                                                                                  |
| -------------------- | ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `RuleService`        | `rule/service.py`             | 规则 CRUD + V3 一致性校验：mode×执行路径矩阵、lifecycle、query 措辞、idempotent/cooldown 配对、on_target×duration record 兼容、task 存在性与规则重名，均在 create/update 流程强制校验 |
| `RuleRunner`         | `rule/runner.py`              | 帧级状态机：per-(rule_id, source_did) 布尔聚合 + 抗抖 + duration 滑窗 + diff + event dispatch，附 on_target 累计达标 timer；STATIC 直调 MiotProxy，DYNAMIC 走 dispatch_event          |
| `TerminateEvaluator` | `rule/terminate_evaluator.py` | temporary 规则的后台评估服务；其到期删除实际由 Agent 经 `miloco-terminate-task` 完成                                                                                                  |

规则 schema 定义见 `rule/schema.py`（`Rule` / `RuleAction` / `RuleMode` / `RuleEvent` / `RuleLifecycle`）。

### 关键设计决策

#### event vs state 为什么分开

event 模式只监听 False→True 的翻转，适合"检测到 X 这件事发生了，马上做 Y"。state 模式监听进入和退出两个边界，适合"当 X 持续存在时保持 Y"（如有人看书时灯保持开着，人走了灯才关）。state 模式的退出防抖应对 LLM 短暂漏识——人没有真正离开，只是某帧没被识别到，不应立即触发 on_exit。

**帧级抗抖**：主要针对 True→False 的疑似漏识——单帧翻转不立即采信，需连续确认才认定状态改变，避免 VLM 单帧漏识/幻觉导致状态反复抖动。这一机制不同于 state 模式的退出防抖（后者是确认已退出后的延迟执行）。

**duration 滑动窗口**：event 和 state 模式都支持可选的 `duration_seconds` + `duration_ratio` 配置。启用后，RuleRunner 维护 per-rule 滑窗，记录窗口内各帧的 True/False 比例，达标才触发。event 模式触发后清窗口，支持周期 fire；state 模式以达标作为 ENTERED 前置门槛，STILL_IN 期间不重复 fire。`duration_ratio` 未显式设置时由配置段的默认值回填（见 `settings.yaml::rule` / `settings.py` 的 `RuleSettings`）。

**on_target 累计达标通知**：state 规则可选配 `on_target_desc`，前提是关联的 task 挂了 duration 型 record 且设了 target_minutes（service 层 CRUD 强制校验此兼容性）。与依赖 condition 翻转的 ENTERED/EXITED 不同，TARGET_FIRED 由 RuleRunner 内部 timer 驱动（不走画面 diff），每个 record 周期内至多回调一次、跨日随 record rollover 重置。设计意图是把"目标达成"这类与画面进出无关的时机也纳入规则回调，用户无需盯着累计进度。

**DYNAMIC 规则 isolated 会话**：触发时构造 `RuleTriggerCallback`（含 rule_id / event / prompt_text / room_name / source_device_ids），经 `AgentDispatcher` → OpenClaw Webhook 投递。Agent 在 `session="isolated"` 会话中运行，文字输出不进主对话流、不自动发声，"用户该收到"的内容必须经 `miloco-notify` Skill 落地。

**STATIC 动作两重检查**：执行前做幂等检查（先查当前属性值，已达目标则跳过）和冷却检查（冷却窗口内跳过，适合 TTS 等不宜频繁触发的动作）。`idempotent=false` 的动作必须配 `cooldown_minutes`，service 层在 CRUD 时强制校验。

**query 措辞校验**：`RuleService` 在创建和更新规则时拒绝以"检测到"/"识别到"/"感知到"等断言性词汇开头的 query。这类措辞被注入 Omni prompt 后，VLM 会把 query 当成已发生事实而非待判断条件，导致连续误触发。query 应改写为进行时状态描述（如"有人坐在书房桌前"而非"检测到有人进入书房"）。

### 如果我要修改规则相关功能

| 修改目标                                          | 去看哪个文件                         |
| ------------------------------------------------- | ------------------------------------ |
| 修改规则状态机逻辑（触发条件/抗抖/duration 窗口） | `rule/runner.py`（`RuleRunner`）     |
| 修改规则 CRUD 校验逻辑                            | `rule/service.py`（`RuleService`）   |
| 修改 STATIC 规则执行逻辑                          | `rule/runner.py`（设备动作执行部分） |
| 修改 DYNAMIC 规则 prompt 组装                     | `rule/runner.py`（prompt 组装部分）  |
| 修改规则数据结构                                  | `rule/schema.py`                     |
| 修改规则 API 端点                                 | `rule/router.py`                     |

### 规则相关 API 路径

主要入口：`POST /api/rules`（创建规则）、`GET /api/rules`（查询规则列表），完整端点见 `rule/router.py`。

### 与其他模块的关系

**上游**：`PerceptionEngineProxy`（`perception/client.py`）每次推理后把本轮实际下发到各摄像头的规则的 True/False 经 `RuleService.update_state` 上报（驱动 `RuleRunner` 状态机；未下发到某摄像头的规则不参与其状态推退）。详见 [感知流水线](perception-pipeline.md)。

**下游**：STATIC 规则直接调 `MiotProxy`（`miot/client.py`）；DYNAMIC 规则经 `AgentDispatcher` 投给 OpenClaw Agent，Agent 调 `miloco-devices` Skill 执行。详见 [设备控制](device-control.md)。

**共享**：规则通过必填的 `task_id` 字段关联到 task——`RuleService.create_rule` 先校验目标 task 存在，再由 `RuleRepo.create` 在一笔事务里同时写入 rule 与 `task_link(kind='rule')`（崩在中间整笔回滚不留孤儿）；event 规则的「当期达标静默」依赖关联任务的 record 状态。DYNAMIC 规则回调经 `dispatch_event("rule", ...)` 投递，`AgentDispatcher` 保证单飞和批量合并。详见 [任务管理](task-management.md)、[Agent 集成](openclaw-integration.md)。

### 配置

规则相关配置在 `settings.yaml::rule` 段，字段定义见 `settings.py` 的 `RuleSettings`。
