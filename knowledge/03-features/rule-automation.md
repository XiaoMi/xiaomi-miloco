# 规则自动化

## 背景与目标

智能家居的核心价值是"自动"——用户不需要手动操作，系统感知到情况后自动响应。传统智能家居的规则基于精确传感器（温度达到阈值、门磁打开），无法处理复杂语义场景（"老人摔倒了"、"孩子开始做作业了"）。

规则自动化让用户用自然语言描述"当 X 时，做 Y"。Miloco 感知到 X 后自动执行 Y——不需要写代码，不需要记住设备 API，VLM 负责语义判断。

---

## 产品面

### 能做什么

#### 规则方向

一个 task 可以挂多条规则，每条规则用 `direction` 说明它的条件成立时对 task 意味着什么。

| direction | 条件成立时 | 用在哪                                              |
| --------- | ---------- | --------------------------------------------------- |
| `enter`   | 该进入了   | 单次触发，不关注何时退出                            |
| `exit`    | 该退出了   | 退出条件与进入条件互不相关时（比 V 手势进、挥手出） |
| `session` | 会话进行中 | 进入与退出互为反面时（有人在→保持，人走了→关）      |

- 进入与退出可以由**两条不相关的规则**各自负责，不必互为反面
- 多条 `enter` 挂同一个 task 是 OR：任一条成立就进入，动作只执行一次
- `session` 必须独占一个 task —— 与别的规则混挂会让它永久卡在进行中
- **STATIC**：低延迟、高确定性，直接执行预先写死的设备指令
- **DYNAMIC**：规则只写意图描述，触发时交给 Agent 结合当时上下文决定具体操作
- **动作挂在 task 上**：进入 / 退出 / 达标三个槽，`miloco-cli task set-actions` 配。单方向规则也可以在建规则时直接带动作，由方向决定它落哪个槽
- **生命周期**：permanent（永久存在）和 temporary（Agent 判断终止条件后自删）两种
- **duration 扩展**：任何方向都支持可选的滑动窗口累计触发——设置后条件需在窗口内达到指定比例才触发，而非单帧 True 即触发

### 典型场景

**场景 1 — STATIC session 规则**：用户创建规则"当有人在书房时，保持台灯开启；人离开后关灯"。感知识别到有人进入书房，台灯打开；人离开超过退出防抖时长，台灯关闭。全程无 LLM 调用，延迟极低。

**场景 2 — DYNAMIC enter 规则**：用户创建规则"当感知到孩子开始哭泣时，自动处理"——不指定具体操作。感知到哭泣时，DYNAMIC 规则触发，Agent 在 isolated 会话中读取当前时间和家庭状态，自主决定：白天可能通知家长，深夜可能轻柔播放音乐。

**场景 3 — enter + exit 非互反**：用户想用手势控制专注模式："比 V 手势开始，挥手结束"。两条规则挂同一个 task，一条 `enter` 一条 `exit`，条件互不相关。这种形态用单条规则表达不了——它的退出条件不是进入条件的反面。

**场景 4 — temporary 规则**：Agent 帮用户创建"等快递到了通知我"的临时监控。规则 lifecycle 为 temporary，快递员进门事件被感知后，Agent 播报通知，再自动删除该规则，不留后台垃圾。

**场景 5 — duration 滑窗规则**：用户创建规则"孩子在书房认真学习超过 45 分钟，提醒他休息"。配置 `duration_seconds` 和 `duration_ratio`，窗口内 True 比例达阈值才触发，防止 VLM 单帧误判触发误报。

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
  → 剔除「当期已达标」的 enter 规则（关联 task 的活跃期 record 已达目标，静默不再触发）
     只剔 enter：session 剔了会取消退出动作，exit 剔了会让 task 收不到退出边沿
  → 本轮下发到各摄像头的规则上报 True/False（未下发到某摄像头的规则不参与其状态推退）
  → RuleService.update_state → RuleRunner 帧级状态机（rule/runner.py）
      ① 源层：帧级抗抖，每个条件项算出真 / 假 / 还没喂过数据
      ② 条件层：名下各源 OR 成一个布尔 → duration 滑窗采样（如配置）
      ③ 确认层：与上一拍比出边沿（ENTERED / EXITED / STILL_IN / STILL_OUT），
                 再按 direction 映射成"对 task 是什么意图"→ 进入 / 退出 / 达标槽
      ④ task 层：TaskStateMachine 维护 runtime_state，决定这次边沿该不该真执行
           ├─ STATIC → 执行设备动作 → MiotProxy → 米家设备
           └─ DYNAMIC → AgentDispatcher
                       → run_agent_turn → OpenClaw Webhook
                       → Agent isolated 会话 → Skill 执行

  达标（TARGET_FIRED）走同一条链，只是 ① 层不是画面而是记账：record 源按"还差多少"
  排一个定时器，到点重新读账、够了就喂真。"今天通知过没有"由条件本身的值承载 ——
  已经是真就产不出新边沿，跨日归零把它翻假，第二天达标才是新的假→真。
```

### 核心模块

| 类                   | 文件                          | 职责                                                                                                                                                                                                         |
| -------------------- | ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `RuleService`        | `rule/service.py`             | 规则 CRUD + 一致性校验：执行路径矩阵、lifecycle、query 措辞、idempotent/cooldown 配对、task 名下规则组合的合法性、task 存在性与规则重名，均在 create/update 流程强制校验；达标规则由它按 task 配置代建与撤销 |
| `RuleRunner`         | `rule/runner.py`              | ①②③ 层：per-(rule_id, source_did) 布尔聚合 + 抗抖 + duration 滑窗 + 边沿 diff + 方向映射；STATIC 直调 MiotProxy，DYNAMIC 走 dispatch_event                                                                   |
| `RecordSource`       | `rule/record_source.py`       | 达标这个源：把"当日累计够没够"算成条件项的布尔，自己按剩余量排定时器                                                                                                                                         |
| `TaskStateMachine`   | `task/state_machine.py`       | ④ 层：per-task 的 runtime_state、幂等消费（多条规则进只执行一次）、方向映射的唯一落点 `slot_for_edge`                                                                                                        |
| `TerminateEvaluator` | `rule/terminate_evaluator.py` | temporary 规则的后台评估服务；其到期删除实际由 Agent 经 `miloco-terminate-task` 完成                                                                                                                         |

规则 schema 定义见 `rule/schema.py`（`Rule` / `RuleAction` / `RuleDirection` / `RuleEvent` / `RuleLifecycle`）。`RuleMode` 是被 `RuleDirection` 取代的旧字段，expand-contract 阶段 A 期间仍在表上，读侧一律走 `Rule.resolved_direction`。

### 关键设计决策

#### direction 为什么取代 mode

`mode ∈ {event, state}` 把"怎么判"和"是什么语义"揉在一个字段里，结果一个 task 只能挂一条规则，进入和退出必须互为反面——"比 V 手势进、挥手出"这种需求表达不了。

`direction` 只回答一件事：这条规则的边沿走 task 的哪个动作槽。于是规则退化成纯粹的感知条件、只产边沿，状态和动作归 task，一个 task 想挂几条挂几条。

分层的依据是**输出类型**，每层一种：条件项的三值 → 整条规则的布尔 → 边沿 → 副作用。好处是"我的规则怎么没反应"有确定答案——看哪一层把它吃了（`task get` 的 `last_decision`）。

**帧级抗抖**：主要针对 True→False 的疑似漏识——单帧翻转不立即采信，需连续确认才认定状态改变，避免 VLM 单帧漏识/幻觉导致状态反复抖动。这一机制不同于退出防抖（后者是确认已退出后的延迟执行）。

**duration 滑动窗口**：任何方向都支持可选的 `duration_seconds` + `duration_ratio` 配置。启用后，RuleRunner 维护 per-rule 滑窗，记录窗口内各帧的 True/False 比例，达标才触发。单方向规则触发后清窗口，支持周期 fire；`session` 以达标作为进入的前置门槛，进行期间不重复 fire。`duration_ratio` 未显式设置时由配置段的默认值回填（见 `settings.yaml::rule` / `settings.py` 的 `RuleSettings`）。

**累计达标通知**：给 task 配 `on_target_desc`（`task set-actions`），前提是它挂了 duration 型 record 且设了 target_minutes。达标规则**不由用户建**——task 的达标动作、活跃的时长记账、记账上的阈值三样齐备时服务端自动生成一条 `direction=milestone` 的规则，任一样消失就删掉。它不出现在 `rule list`（`--show-milestone` 可看）和 `task get` 里，用户主动建会被拒。做成派生物是因为装配是分步的，中间态必然有一半不成立，而派生量没有中间态。

**DYNAMIC 规则 isolated 会话**：触发时构造 `RuleTriggerCallback`（含 rule_id / event / prompt_text / room_name / source_device_ids），经 `AgentDispatcher` → OpenClaw Webhook 投递。Agent 在 `session="isolated"` 会话中运行，文字输出不进主对话流、不自动发声，"用户该收到"的内容必须经 `miloco-notify` Skill 落地。

**STATIC 动作两重检查**：执行前做幂等检查（先查当前属性值，已达目标则跳过）和冷却检查（冷却窗口内跳过，适合 TTS 等不宜频繁触发的动作）。`idempotent=false` 的动作必须配 `cooldown_minutes`，service 层在 CRUD 时强制校验。

**STATIC 动作三种形态**：`iid` 决定形态——`prop.<siid>.<piid>` 走属性直控（带 `value`），`action.<siid>.<aiid>` 走 method call（带 `params`，如 TTS），`scene` 触发米家场景（`did` 位置放 scene_id，无 `value`/`params`）。场景读不到当前值，无法做幂等比对，所以强制 `idempotent=false` + `cooldown_minutes`，去重只靠冷却；冷却键是 `(did, iid)`，同一条规则里的多个场景互不干扰。场景执行复用 `MiotService` 的家庭白名单校验和场景台账，与 CLI 手动触发落同一形状，仅 `source` 标成 `rule`、`source_id` 写 rule_id。

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

**共享**：规则通过必填的 `task_id` 字段 FK CASCADE 挂到 task（应用层 + DB NOT NULL 双保险校验 task 存在）；event 规则的「当期达标静默」依赖关联任务的 record 状态。DYNAMIC 规则回调经 `dispatch_event("rule", ...)` 投递，`AgentDispatcher` 保证单飞和批量合并。详见 [任务管理](task-management.md)、[Agent 集成](openclaw-integration.md)。

### 配置

规则相关配置在 `settings.yaml::rule` 段，字段定义见 `settings.py` 的 `RuleSettings`。
