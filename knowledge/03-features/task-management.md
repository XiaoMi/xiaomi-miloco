# 任务管理

## 背景与目标

智能家居里很多需求不是"开关一次"，而是"持续运转 + 跨时间累积"：每天喝够 8 杯水、孩子每天练琴别超 2 小时、记录老人每次起夜。这类需求需要一个长期存在的"意图主体"，挂载自动化、定时提醒、行为统计，并能按周期归零、回看历史。

任务管理（task / task_record）就是这个主体：把"持续运转的家庭任务"从一次性自动化升级为**带累积统计与生命周期的可追踪任务**。

---

## 产品面

### 能做什么

- **持久意图主体**：一个任务（`task_id` + 描述）是长期存在的身份，可处于启用 / 暂停态，挂载规则、定时、记录三类能力，独立运转
- **行为统计（任务记录）**：记录"做了多少 / 做了多久 / 做了几次"，按周期窗口（日 / 周 / 月 / 长期）自动归档滚动
- **三种统计形态**：
  - **进度型（progress）**：有明确目标值与单位的累积，如"每天喝 8 杯水"，达标即完成
  - **时长型（duration）**：累积时长，支持开始 / 结束计时段，如"今天练琴 60 分钟""孩子每天屏幕时间不超 2 小时"
  - **事件型（event）**：只记发生次数与时间线，无目标、长期不归零，如"记录每次老人起夜"
- **周期归档**：周期型任务跨周期时自动归档上一期成绩、开启新一期，历史可回看
- **终止审计**：任务终止时保留终止原因和最终成绩快照，供事后回看

### 典型场景

**场景 1 — 健康习惯打卡**：用户说"每天喝 8 杯水"。Agent 创建一个 progress 型日周期任务，每喝一杯累加一次；跨日时自动归档昨日成绩并归零，开启新一天。

**场景 2 — 时长管控**：用户说"孩子每天练琴别超过 2 小时"。Agent 创建 duration 型日周期任务，练琴开始 / 结束各打一次计时段，累计今日分钟数，供规则判断是否超限。

**场景 3 — 行为留痕**：用户说"记录每次家里有陌生人"。Agent 创建 event 型长期任务，每次感知到陌生人就追加一条事件，长期沉淀供回看。

### 能力边界

- 任务记录只做统计与归档，不做自动化触发（那是规则）、不做定时提醒（那是 cron）
- 任务被暂停后，记录累积类操作（进度累加 / 事件追加 / 计时段起止）被静默跳过（返回 noop 而非报错，不计数），恢复启用后才继续累积
- 一个任务同时只能有一条活跃记录；记录形态（kind）在创建时定死，不可中途切换
- 事件型记录长期累积、不参与周期归档
- 任务被删除后，关联的规则与所有记录一并清理，仅保留独立的终止审计快照

---

## 研发面

### 架构概览（数据流图）

任务管理由两个模块协作：`task/`（任务生命周期主体）与 `task_record/`（任务的行为统计载体）。

```
创建任务（Agent: miloco-create-task 分步装配）
  → POST /api/tasks → TaskService（task/service.py）仅建 task 占位行
  → 再分步挂载（rule 置于最后，故称倒序）：POST /api/tasks/{id}/record 初始化记录（FK 直连 task，不进 task_link）；
      POST /api/tasks/{id}/link 挂 cron（记入 task_link）；
      rule create endpoint 内部一笔事务写 rule + task_link（依赖 record 先就位）

累积统计（CLI / Agent）
  → POST /api/tasks/{id}/record/...（init / progress-inc / event-append / session-start|end）
  → TaskRecordService（task_record/service.py）
      业务规则校验 → Repo 写主/子表 → 返回派生量（remaining / 今日累计 / 总次数等）

周期归档（后台 daemon）
  → main.py lifespan 起 _rollover_daily_loop（每日凌晨定时）
  → rollover_daily_job（task_record/rollover.py）
      扫到点的周期型活跃记录 → 单事务归档旧行 + 开新活跃行
      → 归档前 snapshot 旧成绩 → 回调 RuleService.notify_record_rollover（跨日兜底）

终止任务（Agent: miloco-terminate-task）
  → DELETE /api/tasks/{id} → TaskService.delete_task（单事务）
      写 task_terminate_log 审计快照 → 删 rule → 删 task
      （FK CASCADE 连带清 task_link 与全部 task_record_* 表）
```

### 核心模块

**TaskService**（`task/service.py`）

任务生命周期主体的业务层：创建（仅建占位行）/ 启停 / 更新 / 删除任务，维护 task↔rule/cron 关联（记入 `task_link`）。启停时联动改写关联规则的 `enabled`，并把 cron 侧操作汇总成 `agent_pending` 交由 Agent 落地——后端不直接操作 OpenClaw Cron。`delete_task` 单事务编排终止——写审计快照 + 删规则 + 删任务（FK CASCADE 清理关联与记录）。另提供 summary 聚合视图：以 task 为主表左连接各任务的活跃记录摘要（没绑记录的 task 也返，不丢行），一次性出全部任务的完整状态（基础 + 规则摘要 + 关联 + 记录摘要），供前端任务面板拉取；记录侧摘要由 `TaskRecordService.list_active_summaries` 拼装。

**TaskRecordService**（`task_record/service.py`）

任务记录的业务层，跨表事务编排 + 派生量计算 + 字段校验：

- **init**：按 kind 校验内容后插入活跃记录（前提任务已存在，FK 直连 task）
- **累积**：进度累加 / 事件追加 / 计时段开始与结束，写表后返回派生量
- **调整**：按 kind 白名单 PATCH 记录的可配字段（目标 / 单位 / 周期窗口 / 过期时间等），kind 本身不在任何白名单、永不可改
- **读取**：取活跃记录 + 子表 + 派生量；支持当前 / 单日历史 / 区间聚合三套互斥查询，另有归档列表读取按日回看历史成绩
- **派生量**：进度型出剩余量与百分比，时长型出今日累计 / 剩余分钟 / 活跃计时段，事件型出总次数 / 今日次数 / 最近时间

四个 Repo（`ProgressRepo` / `DurationRepo` / `EventRepo` / `TerminateLogRepo`，`task_record/repo.py`）是薄包装，接 caller 传入的 cursor、不持有连接、不做业务判断——把事务边界统一交给 service。

**rollover_daily_job**（`task_record/rollover.py`）

周期归档调度入口，由 `main.py` 的 lifespan 起一个每日凌晨触发的 asyncio daemon。具备自愈：启动即跑一次，跨日重启漏滚由"上次归档时间早于上一周期结束"判据兜底，重复跑靠唯一索引无副作用。

### 关键设计决策

**task 与 task_record 分离**：task 管"是谁、什么生命周期、由哪些能力组成"，task_record 管"做了多少"。两者状态字段同字面（task 的 active/paused，record 的 active/completed）但语义独立，模型层隔离。

**为何分三种 kind**：达目标 / 累时长 / 计次数三类行为数据形态根本不同，强行塞一张表会让大量字段互斥为空。按 kind 拆主表 + 子表（时长型的计时段、事件型的事件条目），每种语义干净。kind 创建时定死不可切换。

**Repo 接 cursor 而非自持连接**：记录的计时段结束 / 归档 / 终止天然是多语句跨表事务，需 service 统一管事务边界——这与 `task_repo` 每方法自持连接的风格不同，是刻意区分。

**独立终止审计表**：删任务会 CASCADE 抹掉所有记录，但用户 / 运营需回看"任务因何终止、终止时最终成绩如何"。故删除前把最终快照写入独立的 `task_terminate_log`（与生命周期表解耦，按窗口滚动清理）。

**归档跨日兜底规则引擎**：时长型归档会清掉旧累计，若规则依赖"昨天是否达标"会丢判据。故归档前 snapshot 旧成绩并回调 `RuleService.notify_record_rollover`，让规则引擎拿到归档前状态。

### 涉及的数据表

- **task 子系统**：任务生命周期主体表 + task↔rule/cron 关联表（`task_link`）
- **task_record 子系统**：按 kind 拆的主表（进度 / 时长 / 事件）+ 时长与事件各自的子表 + 一张终止审计表

记录不进 `task_link`，FK 直连 task。表名与 schema 见 `database/connector.py`。

### 任务相关 API 路径

主要入口：`POST /api/tasks`（创建）、`DELETE /api/tasks/{task_id}`（终止，级联清理）、`/api/tasks/{task_id}/record/...`（记录初始化 / 累积 / 查询 / 归档），完整端点见 `task/router.py` 和 `task_record/router.py`。

### 与其他模块的关系

**上游**：Agent 通过 `miloco-create-task` Skill 创建任务并装配 rule / cron / record，通过 `miloco-terminate-task` Skill 终止任务。`miloco-habit-suggest` Skill 从家庭习惯洞察中推荐可建任务。

**下游 / 共享**：任务装配的规则进入规则引擎（见 [规则自动化](rule-automation.md)），定时进入 OpenClaw Cron。记录归档时回调规则引擎做跨日兜底。任务的 Agent 侧编排见 [Agent 集成](openclaw-integration.md)。
