---
name: miloco-create-task
description: 创建/管理"持续运转的家庭任务"—— 由 rule（条件触发自动化）/ schedule（定时提醒）/ record（行为累积统计）/ lifecycle（长期/限时）组合装配；也是 [感知引擎] 语音指令类系统消息的统一入口。覆盖：定时/累积提醒、"X 时 Y" 自动化、行为统计、持续状态阈值等。
metadata:
  author: miloco
  version: "3.0"
  date: "2026-06-10"
  openclaw:
    requires:
      bins: ["miloco-cli"]
---

# miloco-create-task

收到任务相关消息 → 第一步判 **op**，六条路径之一：

| op | 触发来源 | 例子 | 走哪条 |
|---|---|---|---|
| **create** | 用户消息 / `[感知引擎]` 语音指令系统消息 | "每天喝 8 杯水" / "房间有人提醒我" / "明天 X" / "没人就 Y" | 本 skill 后续段（前置检查 → 两层维度判 → 装配映射） |
| **list** | 用户消息 | "我有哪些任务" / "查看任务列表" | [references/crud-ops.md](references/crud-ops.md) |
| **logs** | 用户消息 | "X 任务今天触发几次" / "看 X 触发记录" | [references/crud-ops.md](references/crud-ops.md) |
| **disable / enable** | 用户消息 | "暂停喝水任务" / "启用 X 任务" | [references/crud-ops.md](references/crud-ops.md) |
| **update** | 用户消息 | "把喝水改成 10 杯" / "条件改成阳台有人" | [references/crud-ops.md](references/crud-ops.md) |
| **delete** | 用户消息 | "删除喝水任务" / "把 X 任务去掉" | miloco-terminate-task |

## 输出协议

user-facing text 仅在两个时机出现：
- 终态：tool 链跑完发一段结果
- 反问：等用户答复发一句；**只问本次需要的字段，不夹带已装配信息 / 进度同步 / 任何其他内容**

其余时刻过程描述放 thinking 块，user-facing 静默。

**person 引用规则**：user-facing 文本（终态 + 反问）引用 person 时 role 优先——role 非空只写 role，不带 name 括号标注；role 为空才写 name。

### 终态必含项

内容反映本轮实际 CLI 装配参数，不增不减。格式不限，必含：

1. **任务标识**：task id + 简述
2. **触发场景**：什么时候启动
3. **响应动作**：触发后做什么 + 记录什么
4. **生命周期**：长期常驻 / 限时
5. **装配提示**：按 §装配提示元规则 逐条独立列出

以下内容仅覆盖 **create 路径**。判据走**语义判断**，不照字面词表死扣。下文给的词只作类别示例，不是穷举枚举。

## 装配后自检

工具链跑完到终态输出之间，**必须 echo 下列格式**做自检（任一 N → 修正后重过；echo 内容不输出到 user-facing）：

```
[装配后自检]
1. 含 on-target-desc 时 task → record → rule 顺序完成装配：Y / N / NA
2. 终态 §响应动作 同时含「触发后做什么」+「记录什么」：Y / N
3. 每条装配提示「怎么改」字段给出用户可直接复述的短语：Y / N
```

## 装配提示元规则

凡装配过程中 agent 自主产生的对用户的提示性信息，统称「装配提示」。覆盖：默认猜测、偏离推荐、超阈值范围、需用户验证的常识默认等。

每条装配提示**必产出**，在终态**独立成句**出现，措辞自由。

每条必含 3 要素：

- 实际取值（用户可理解措辞，不暴露内部字段）
- 一句话原因
- 怎么改 / 怎么确认

子维度判据里只标 "→ 触发装配提示"。

**多提示装配**：N 条提示 → N 句，独立成句。

## 输入硬约束

- 多个独立 task → 拆成单 task 列表，各自走分类
- AND 型规则（同一 task 内多触发必须同时成立）→ 拒建

## 前置检查（早于第一层判定）

> [输出协议 reload]：本章节及后续所有装配工具调用期间，过程描述走 thinking，user-facing 仅在终态与反问输出。

### 感知设备清单

调 `miloco-cli scope camera list`，取 `in_use=true` 的子集记为 N。结果给 §Rule.感知设备 装配阶段消费。与 §重复检查 / §person 清单 并行调用。

### person 清单

调 `miloco-cli person list` 拿 role/name 列表，结果给 §Rule.condition.query 主语决策消费。与 §感知设备清单 / §重复检查 并行调用。

### task_id 规范

agent 自己起，snake_case `[a-z0-9_]{1,32}`，需含语义（`drink_8_today` / `fall_alert` / `phone_time_daily`），不用纯数字 / 简短缩写。

### 重复检查

调 `miloco-cli task list --pretty` 拿活跃 task 列表，按 task_id + description + rule_briefs 比对：

| 类型 | 判据 | 处理 |
|---|---|---|
| A 名字冲突 | 待建 task_id 命中现有活跃 task | 让用户换 task_id 或先 terminate 旧 |
| B 语义重复 | description 表意一致 / rule_briefs 触发+动作组合本质相同 | 反问三选一：替换 / 强制新建 / 取消 |
| C 触发冲突 | 同 source + 同 condition + 动作矛盾或参数改写 | 反问三选一：替换并删旧（旧历史不可恢复）/ 暂停旧建新（旧历史保留，可恢复）/ 取消 |

**跳过条件**：
- 命中 task_id 是 paused → 让用户先 enable 或 delete，不算冲突
- 作用对象不同（不同摄像头/房间/设备）→ 不算冲突
- 同 source + 同 condition 但 piid 不同（亮度 vs 色温）→ 不算冲突，并存

**禁止主动 update / delete**：A / B / C 类命中后等用户答复，不私自 update / delete 已有 rule / cron / record / task。

---

# 第一层 · task 维度判定

> [输出协议 reload]：本章节及后续所有装配工具调用期间，过程描述走 thinking，user-facing 仅在终态与反问输出。

task 是聚合根，含：

- 0~N 个 rule（**Rule** 判存在性）
- 0~N 个 cron（**Schedule** 判存在性）
- 0~1 个 record（**Record** 判存在性）
- 1 个 lifecycle 属性（**Lifecycle** 判取值）

Rule/Schedule/Record 是子组件存在性（Y/N）；Lifecycle 是 task 整体属性（permanent/temporary）。四者独立判，不互依赖。

## Rule?（Y/N）

**Y** = 用户描述需要系统**持续观察现实世界**才能触发的场景，含以下任一语义：

- **环境状态变化**：人在/不在、设备开关状态、温度湿度烟雾等环境量异常
- **人体可观测动作或行为**：任何能被摄像头或麦克风识别的人体动作或姿态（瞬时如吃药/咳嗽/按门铃，持续如看书/写作业/玩手机）
- **人身安全异常**：摔倒、入侵、求救、火灾相关
- **计数/累计数字目标**：N 次/杯/个 或 累计 N 小时/分钟

**N** = 纯时间触发或纯查询，不依赖现实事件。

**Rule 跟 Schedule 完全独立**：句中含任何现实事件信号（动作/环境/状态变化/计数目标）就 Rule=Y，**即使同句有定时提醒**。

## Schedule?（Y/N）

**核心判据**：触发是否依赖**时钟锚点**（在某个未来时间点 / 周期性时间点上由系统主动发起）。

- **时钟锚点触发**：用户期望系统按时钟自动触发，无需现实事件配合 → **Y**
- **现实事件触发**：触发由感知事件或时长持续状态发起，不依赖时钟 → **N**

**Y 的语义**（含以下任一）：

- 单一未来**具体时点**（明天 12 点 / N 分钟后 / 一会儿 / 后天 等明确到小时/分钟/相对偏移）
- 周期性**具体时点**（每天早上 8 点 / 每周三 9 点 / 工作日 7 点 等明确含时段或时点的周期）
- 显式定时措辞（定时提醒 / 到点提醒 / 每 N 小时催一次 等含具体时间间隔的措辞）
- **周期+计数无具体时点 + 无显式定时措辞**（每天/每周 N 杯/次/个 等周期目标） → 默认装周期定时提醒（按 §Schedule.频率默认 表取默认时点） → **触发装配提示**

**"每天累计/合计/总共/加起来 N 小时/分钟"** → Schedule=N。区别于"每天 N 杯/N 次"离散计数 → Schedule=Y 定时提醒。

**不归 Schedule**：

- 时长阈值（"持续/连续 N 分钟"）
- 限时窗口（"今天/这周/这个月"）
- 语气词（一声/一下）

## Record?（Y/N）

**核心判据**：用户语义里**是否含"累计/计数"信号**。

- **含**累计/计数 → **Y**
- **不含** → **N**

**累计/计数信号**（含以下任一即 Y）：

- **计数达标**：够 N / 超过 N / 目标 N / 喝 N 杯 / 走 N 步 / 吃 N 次药 / 做 N 个
- **计数统计**："几杯/多少次/几个"等问句计数
- **时长累计**：累计/总共/合计/加起来 N 小时/分钟
- **量化反查**：算/统计/看/问 X 多久 / X 了多久 / 算 X 几次 / 看 X 用了多少时间
- **历史记录意图**：记录/统计/跟踪/数 / 记下每次 / 看历史
- **周期重置**：每天 N 杯 / 每周 N 次

**N 的边界 case**：

- **时长阈值 + 提醒（不含显式跨次词、不含历史记录词）** → 默认按**单次连续**装（rule duration_seconds 表达，不建 record）→ **触发装配提示**

**Record=Y → Rule=Y（强约束）**：record 各 kind 必须由 rule 触发写入 —— progress = `--action-desc`「计数加一」/ duration = `--on-enter-desc`「开始计时」+ `--on-exit-desc`「结束计时」/ event = `--action-desc`「事件追加」。具体动词短语变体见 §Rule.action 自检表。

## Lifecycle（permanent / temporary）

**核心判据**：用户的规则是**长期常驻**还是**只在某个有限时间段内有效**。

- **长期常驻**：用户期望规则不限时一直生效，每次满足触发条件都响应 → **permanent**
- **限时有效**：用户期望规则只在某个有限时间段内有效，过期或满足完成条件后失效 → **temporary**

按以下信号**顺序判**，命中第一个即停：

1. **周期信号**（每天/每周/工作日/周末/每月/以后/一直/总是/每次/都）→ permanent
2. **时间窗信号**（用户原话明确含时间段截止：今天/今晚/这周/本月/本月底前）→ temporary
3. **达标完成信号**（"做够 N / 达到 N 个 / 满足 N 次"作为整个规则的终止条件）→ temporary
4. **绝对一次性时刻信号**（"明天 X 点 / N 分钟后 / 后天 / 一会儿"作为唯一触发点）→ temporary
5. **以上都无** → permanent

**不归 temporary**：

- 含具体地点（阳台/卧室）但无时间窗信号
- 含时长阈值（"持续/连续 N 分钟"）
- 含瞬时事件触发词（进入某场景、状态切换发生时）
- 语气词（一声/一下/就行）
- 活动名（看书/做饭/洗澡）

---

# 第二层 · 按取值展开子维度

## Rule=Y 时填 Rule

### Rule.mode（event / state）

**核心判据**：是否需要对"进入条件"和"离开条件"**双向响应**？
- 是 → `state`
- 否 → `event`（触发条件含持续时长用 `duration_seconds` 修饰，不改 mode）

按顺序判，命中即停：

1. **Record.kind=duration（跨次累计 record）** → `state`（on_enter 写 duration-start + on_exit 写 duration-end）
2. **人身安全/紧急异常** → `event`
3. **显式 state 信号** → `state`
   - **双向状态切换**（用户明示进入做 X，退出做 Y）
   - **激活/启动持续设备状态**（开灯/开空调/拉帘/播放音乐 等"开 X"动作）→ 默认补 `on_exit` 复位
   - **开始/进入持续行为态**（"开始 X" / "进入 X" 语义，仅响应入态）→ `duration_seconds` 取稳定窗（按推荐表）+ 长 `exit_debounce_seconds`（按推荐表）+ `on_exit` 留空
4. **到达/进入类瞬时事件**（到家/到门/进门/进 X / 回家 等含强常识默认） → `state` + 长 `exit_debounce_seconds`（按推荐表）+ `on_exit` 留空
5. **通知/播报类一次性动作 + 陌生主体瞬时存在态触发**（陌生人 / 任何人 来了 / 出现 等无强常识默认）**+ 无明示触发频率** → **反问 A/B**（详见 §事件触发频率反问）
   - A 每次发生即响 → `event`
   - B 间隔一段时间才算 → `state` + 长 `exit_debounce_seconds`（按推荐表）+ `on_exit` 留空
6. **其他一次性判断** → `event`
   - **瞬时动作**（< 10s 完成的可观测动作 / 计数型 N 个 N 次离散动作 / 喝水 / 咳嗽 / 按门铃 / 仰卧起坐）
   - **关设备**（关灯 / 关空调 / 关窗帘 等单向"关 X"动作；含"X 无人后关 X"持续条件触发）
   - **持续行为 + 一次性通知**（写作业的时候告诉我 / 久坐 N 分钟提醒 / 看电视 N 分钟提醒 等；行为持续但响应是一次性 desc 通知）
   - **触发条件含持续时长** → 用 `duration_seconds` 表达，不改 mode

### Rule.condition.query

**condition.query 是「判定 X 在发生」的视觉命题**——主语 + 谓语，主语类型由命题语义决定（person / object / scene）。

- **只用视觉常识写命题，不读 family profile 习惯描述**；profile 仅用于「主语是具体人物时」的 role/name 取值（见下）
- 必须是当下视觉信号的具体描述，禁止抽象动作词、禁止时间语义
- 不含字面 AND/OR 信号词
- 不含断言式措辞（"检测到 / 识别到 / 已..." 等）
- 不含时长修饰（持续/累计 N 分钟 → 走 duration_seconds 或独立时长字段）
- 不含 SKILL 内部判据词
- 瞬时进入事件按摄像头视角填：视角覆盖关键动作 → 含动作的命题；视角不覆盖 → 退化为存在态命题（详见感知视角反问段）

**主语取值规则**（按命题语义分支）：

| 命题语义 | 主语取值 |
|---|---|
| 人在做某事（具体姓名 / 家庭关系称谓 / 类别词如宝宝/老人 / 第一人称）| 跑 `miloco-cli person list` 解析（见下）|
| 多个具体人物明示列举（"妈妈和爸爸" 等）| 拆 N 条 rule（同 task），每条主语 = 一位 role/name |
| 抽象集合（家人 / 家庭成员 / 家里人 / 所有家人 / 全家）| `已注册成员` + 触发装配提示。**禁展开**为当前 person list 的具体成员 |
| 有人 / 任何人 / 谁 | `任何人` + 触发装配提示 |
| 陌生人 / 外人 / 非家人 | `陌生人`（命题反例排除必含"不含家庭成员"）|
| 物体异常（烟雾 / 火焰 / 积水 / ...）| 物体名词，取自用户原话 |
| 场景状态（无人 / 明暗 / ...）| `画面` 或具体房间名 |
| 句中无明示主体（"起床开窗帘" / "久坐提醒" 等）| `用户` + 触发装配提示 |

**person list 解析**（主语是具体人物 / 第一人称 共用，使用 §前置检查 已拿的列表）：
- 0 命中 → 反问 A. 注册再建 / B. 退化兜底（按用户原话语义从主语取值规则挑合适的）
- 1 命中 → role 非空填 role；role 为空填 name（第一人称额外触发装配提示）
- ≥2 命中 → 反问 A. 选哪位（第一人称表述为"选哪位是自己"）/ B. 退化兜底

**动作类命题**（含动作姿态的 query）必含三段。装 `--condition` 前先按三段拆解（`- 动作姿态：...` / `- 关键物体：...` / `- 反例排除：...`），对每个具象词标注来源「用户原话 / profile 习惯描述 / 视觉常识」，profile 习惯描述 → 删词。再按结构模板机械串接三段为 query 字符串装入 `--condition`：`<主语><动作姿态><关键物体>；不含<反例1>，不含<反例2>[...]`。**串接规则**：三段所有具象词必须 1:1 保留到 query 字符串，禁止简化、抽象化或省略下位词；串接后 query 字符串必须包含拆解出的所有具象词字面字符串。query 字符串必含分号 + ≥ 2 个「不含」前缀，缺任一视为未完成；关键物体段使用近邻易混结构时缺下位词或上位类别视为未完成：
**颗粒度总则**：每段提供视觉模型独立判别正例的最低必要信息，避免过粗（单动词 / 抽象集合）和过细（精确角度 / 品牌 / 型号）。

- **动作姿态**：必含两类视觉特征——①身体部位与物体/环境的接触点关系，②动作完成时的姿态/状态变化描述。两类都要写，缺一不可；单动词（仅写抽象动作名）不算动作姿态描述
- **关键物体**：动作涉及的物体（动作无固定物体的命题，如徒手运动，跳过本段）。类别单一明确（如"手机"/"电脑"）直接写下位词；类别有近邻易混（如饮品容器、健身器械）用「<下位>/<下位>(/...)等<上位类别>」结构（**≥ 2 个下位 + 上位类别**）；**不含**从 profile 习惯描述里读到的个体偏好
- **反例排除**：≥ 2 组视觉极易混淆的相似情况，**用具体物体/动作名词替代抽象否定**。反例必须**独立于正例成立**——禁止以正例的反向、否定、缺失形式作为反例。每条反例必须与正例**视觉判别互斥**——同一帧只能是其中之一。装入前答一问：「这条反例和正例放在同一帧里，能不能并存？」能并存 → 属穿插行为，删该反例

### Rule.action

每方向单独判（state mode 下 ENTERED / EXITED 独立选）：

| 动作性质 | 装配 | fire 时谁执行 |
|---|---|---|
| 能 100% 写死（开关固定设备、播固定文本） | action JSON | rule engine 直接执行 |
| 需按运行时状态/上下文决定 | desc 文案（业务意图） | fire-agent 独立 turn 推理执行 |

按顺序命中即停：

1. 涉及 Record 读写 → 响应方向 desc 首句必含 record 写操作动词短语：
   - progress 用「计数加一」/「+1」/「<次数>加一」
   - event 用「事件追加：<事件描述>」
   - duration on_enter 用「开始计时」/「记录起点」/「记录计时起点」；on_exit 用「结束计时」/「记录终点」
2. 激活型动作的 on_exit + 用户没明确说退出动作 → desc（默认）
3. 动作内容要按上下文决策 → desc
4. 动作是固定调一个 handler（开灯/调温/拉帘 等设备控制类）→ action JSON

通知类场景（通知用户/播报/提醒家人/告知监护人）统一 desc。达标触发的通知按 §达标通知机制 三选一定位置。

**desc 写法**：业务语义，不贴 CLI 命令字面。文案以**客观陈述事件**为准，不含「检测到/识别到/感知到/察觉到」等系统视角词。通知类 desc 句式以「使用<通道>通知：<内容>」开头，<通道>取 §通道反问 A/B/C 三选项之一。desc 内禁用 `{...}` / `${...}` / `%s` 等占位符语法。

### Record 回写 desc 自检

涉及 Record 读写的 desc 首句必含动词短语：

| record kind | 响应方向 | 首句动词短语 |
|---|---|---|
| progress | action | 「计数加一」/「+1」/「<次数>加一」 |
| event | action | 「事件追加：<事件描述>」 |
| duration | on_enter | 「开始计时」/「记录起点」/「记录计时起点」 |
| duration | on_exit | 「结束计时」/「记录终点」 |
| duration | on_target | 业务通知文案 |

desc 结构：`<首句动词短语>[；<业务通知文案>]`。

on_exit 留空条件（不填任何 flag）：

- 用户明确说"离开不动它"
- 进入动作是一次性 desc（TTS 播报 / 一次性通知 / 欢迎语）

### 达标通知机制

按业务语义对照：

| 业务语义 | 触发字段 | 通知装配位置 |
|---|---|---|
| 连续观测达标 | `rule.duration_seconds` | `action-desc`（event）/ `on-enter-desc`（state） |
| 跨次累计达标 | `record.target_minutes`（duration kind） | `on-target-desc`（文案为抽象业务语义） |
| 计数达标 | `record.target`（progress kind） | `action-desc` 末段含达标判断 |
| 跨次累计达标 + 退出复提醒 | 用户原话明示每次退出复提醒语义 | `on-exit-desc` 附加条件通知 |

state + duration record 三 desc 分工：
- `on-enter-desc` 仅放计时起点动词短语
- `on-exit-desc` 仅放计时终点动词短语；用户原话明示每次退出复提醒语义时附加条件通知（按 §Rule.action 通知 desc 句式）
- `on-target-desc` 仅放业务通知文案

`on-target-desc` 非空 → 必同时配 duration record + `target_minutes`，且必按 task → record → rule 顺序装配。

### Rule.duration_seconds

**含义**：触发条件需要持续 N 秒才算成立。mode 无关修饰符（event / state 都可配）。

**单位**：CLI `--duration-seconds` 收**秒整数**。用户原话 N 分钟 → 装 `N×60`；N 小时 → 装 `N×3600`。同任务内 `record.target_minutes` 字段按分钟传，两者不混用。

**单次时长跟踪场景**：duration_seconds 直接表达用户业务时长（CLI 上限 86400 = 24h）。**业务时长 > 24h 拒建**，回话告知用户改用跨次累计 record（Record.kind=duration）。装 > 12h（43200s）→ **触发装配提示**（内容："本 rule 跟踪时长较长，若 Miloco 服务期间重启，计时窗口会清零重新累计"）。

**跨次累计场景**（Record.duration）：duration_seconds 退化为 rule 层姿态稳定窗（推荐值见下方推荐表），业务时长由 `record.target_minutes` 表达。

何时配：

1. 人身安全/紧急 → 禁配
2. 瞬时存在态 / 瞬时动作（< 10s，如喝水 / 咳嗽 / 按门铃 / 仰卧起坐 / 计数型离散动作）→ **不配**
3. 触发条件含"持续/连续 N 分钟"或语义上需要持续观测才能稳定判断 → 必配

推荐值（按动作类别取，不是字面词表）：

| 动作类别 | 推荐 |
|---|---|
| 一般简单姿态动作 | 30 |
| 视线类前置确认 | 45 |
| 久坐/久站/久躺类前置确认 | 60 |
| 屏幕/读写沉浸类前置确认 | 90 |
| 运动/健身/锻炼类前置确认 | 180 |
| 睡眠/专注做事类前置确认 | 180 |
| 拿不准 | 60 |

**取值流程**：

1. 先按上述「何时配」判定：命中 1 / 2 条 → 不传 `--duration-seconds`，跳过此项；命中第 3 条 → 继续取值
2. 用户原话明示具体值 → 按用户值装；偏离推荐值较多 → **触发装配提示**（中性陈述，不阻装、不劝改）
3. 用户未明示 → 按上表推荐值装 → **触发装配提示**

### Rule.exit_debounce_seconds

state mode 防边沿抖动 / 防重复触发；event mode 一般不配（duration_seconds 已提供抖动保护）。

**何时必配**（state mode）：

- on_exit 装了动作（desc 或 action JSON）
- 瞬时进入事件（到达/进入/到家 等）

| 场景 | 推荐 |
|---|---|
| state 双向边沿去重（默认） | 60 |
| state + duration record · 离散使用型（玩手机/看电视/看书 等） | 60 |
| state + duration record · 间断持续型（健身/做饭/打游戏/学习 等） | 180 |
| 瞬时事件防重复 / 持续行为入态 · 长窗（默认） | 1800 |
| 瞬时事件防重复 / 持续行为入态 · 长窗（用户指定 N 分钟） | N × 60（封顶 3600） |

**取值流程**：

- 用户原话明示具体值 → 按用户值装；偏离推荐值较多 → **触发装配提示**（中性陈述，不阻装、不劝改）
- 用户未明示 → 按上表推荐值装 → **触发装配提示**

## Schedule=Y 时填 Schedule

**Schedule.message**：写业务意图，fire-agent fire 时独立 turn 推理执行；周期触发的**唤醒次数 ≠ 提醒次数**。

### Schedule.触发器

| 类型 | 信号 |
|---|---|
| `at` | 单一未来时刻 |
| `every` | 固定间隔（"每 N 分钟" / "每 N 小时" 等明示间隔但无具体时点的措辞）|
| `cron` | 时钟模式 / 周期重复（具体时点 / 周中某天 / 工作日 / 周末）|

### Schedule.频率默认

按顺序判，命中即停：

1. **用户明示时点/频率** → 照用
2. **Record=Y(progress) 且 target > 0** → 多时点提醒：
   - 时点数：`N = min(target_per_day, 6)` 取整；跨周期先归一 `target_per_day = target / 周期天数（day=1, week=7, month=30）`
   - 时段：限清醒时段 06:00-22:00
   - 时点分布按任务自然执行场景定（服药对齐三餐、晨练放早晨、课后活动放放学时段等）；无强场景信号 → N 个时点均分 10:00-20:00
   - 周期：window=day → `* * *`；用户明示工作日/周末 → `* * 1-5` / `* * 0,6`
   - 装配：N 个时点合并装单 cron `0 H1,H2,...,Hn * * *`，单 jobId 单 task_link
3. **其他**（无 target / Record=event / Record=duration / 无 Record） → 每周期单时点：day → `0 9 * * *`；week → `0 9 * * 1`；month → `0 9 1 * *`

命中默认路径 → **触发装配提示**（按 §装配提示元规则）

## Record=Y 时填 Record

### Record.kind

| kind | 用户语义 |
|---|---|
| `progress` | 计数达标 + 明确目标正整数 |
| `duration`（含阈值） | 累计/连续超过 N 小时/分钟，含明确时长阈值 |
| `duration`（无阈值） | 记录/统计/追踪 X 时长，无目标数字 |
| `event` | 累积事件流，记录/统计/跟踪/数 + 行为，无目标数字 |

歧义裁决（按顺序判）：

1. 记录对象是**时长/多久**（"记录每次时长 / 看了多久 / 健身用了多久 / 记下时长" 等）→ `duration`（含阈值时填 `target_minutes`；无阈值省略字段，**禁写 0/1 占位**）
2. **持续行为态** + "记录每次/记下来/统计"（看电视/玩游戏/写作业/久坐/看屏幕 等 ≥ 分钟级活动，无论是否含时长阈值） → `duration` → **触发装配提示**
3. 记录对象是**瞬时动作（< 10s）的次数/事件流** → `event`
4. 含明确正整数目标（喝 8 杯 / 走 10000 步 / 吃 3 次药 等）→ `progress`
5. 含计数但 target 缺失（"几杯/多少次"问句）→ 强制 `event`，禁 `target=0/1`

### Record.task_type

| task_type | 判据 |
|---|---|
| `recurring` | 含周期/习惯化语义 |
| `oneshot` | kind=progress/duration + 含限时窗口语义 |
| `longterm` | kind=event + 任意时间语义或无时间语义 |

特例（时长门槛模板）：

- 单次连续追踪 → `longterm`
- 跨 session 累计追踪 → `recurring`

## Rule=Y + 动作含通知类语义 时填通道

**触发条件**：用户用了**含糊动作词且未明示通道**。

含糊动作词分两类：

- **通知类**：提醒 / 告诉 / 通知 / 告知 / 喊我 / 让我知道 等
- **对人输出语音/文本类**：夸 / 鼓励 / 表扬 / 安慰 / 欢迎 / 招呼 / 说一句 等

**未明示通道** = 用户没说"音箱/喇叭/播报/朗读/手机/推送/消息"等任何通道线索。

必反问 A/B/C，等用户答复后装 desc：

- **A. 使用音箱播报通知**
- **B. 使用手机推送通知**
- **C. 使用 AI 对话通知**

跳过条件：user_intent 已明示通道 → desc 按用户指定通道写，不反问。

## Rule=Y 时填触发歧义反问

用户描述的感知触发存在以下不明确 → 先反问再装配，不默认猜。

### 事件触发频率（短窗 / 长窗）

由 Rule.mode 判据第 4 条触发。按用户原话信号判：

- 明示**即时**（"每次 / 一 X 就"等强调每发生一次都响）→ 短窗 60s，不反问
- 明示**场景窗口**（暗示间隔一段时间后才算，如"下班/外出/出门后"）→ 长 `exit_debounce_seconds`（按推荐表），不反问
- 无修饰 → 反问 A/B 二选一

反问模板：

> 你说的"<原词>"，我想确认下：
> - **A. 每次发生即触发**（短时间内重复发生也响）
> - **B. 间隔一段时间后才算一次**（默认 30 分钟以上）

映射：A → `event`；B → `state` + 长 `exit_debounce_seconds`（按推荐表）+ `on_exit` 留空。


### 感知视角

**触发条件**：触发依赖主体进入或离开摄像头视野（vs 主体已在视野内的姿态/状态变化）。

**跳过本反问**：
- 持续行为态触发（久坐/写作业/看电视/玩手机 等单帧可判姿态）
- 音频/纯听感触发（咳嗽 / 哭 / 呼救 / 按门铃 等）
- 关设备 / 复位类一次性动作

按以下顺序判，命中即停：

1. 用户原话**明示视角**（描述了摄像头位置或视角能力）→ 按字面装，不反问不提示
2. 用户原话有房间名 + 房间名提供**强常识默认**（某类房间位置的摄像头按常识应当覆盖该视角）→ **按常识默认装** + **触发装配提示**（让用户验证）
3. 其他（无房间名 / 房间名无强常识默认）+ 触发依赖关键视角 → **反问 A/B 二选一**

反问模板：

> "<房间名>" 那个摄像头能不能 <关键视角描述>？
> - **A. 看得到**
> - **B. 看不到**

`<关键视角描述>` 指向主体可见性切换发生的**具体局部位置**（出入口/家具点位/区域边界），不是房间整体。

按答复（或常识默认）选 condition.query 形式：视角覆盖 → 含触发动作的命题；视角不覆盖 → 退化为存在态命题。`<X>` 按命题主语决策替换。

路径 2 走常识默认 → **触发装配提示**。

## Rule=Y + 任一方向 action JSON 时填设备

### 感知设备（`--source`，可选）

消费 §前置检查 §感知设备清单 拿到的 N：

1. 已锁 `source_did[]`（非空）→ 直接当 `--source`
2. N=0 → 反问 A. 开启摄像头感知再建 / B. 取消
3. N≥1 → 按用户原话二分：
   - 未指定房间 / 摄像头（含"家里" / "全屋"）→ 不传 `--source` + 触发装配提示（mode 无关，state 持续姿态触发不例外）
   - 指定房间 / 摄像头名 → 在 N 内按 `name` / `room_name` 模糊匹配：
     - 命中 1 → 用该 DID
     - 命中 ≥2 → 全部传（`--source <did1> --source <did2> ...`）+ 触发装配提示
     - 命中 0 → 反问 A. 改名字 / B. fallback 全屋

### 动作设备（`--action` JSON 的 did）

优先看 system context `## 设备目录` 段。缺失或未覆盖 → `device list --room` + `device spec <did>` 拿 iid。

**目标设备消歧**（TTS/语音播报/单点提示音/警报音/局部光效）+ 候选 ≥ 2：

1. 用户原话**含房间词** → 按房间词匹配，不反问
2. **无房间词** → 默认装**第一候选** + **触发装配提示**（告知用户实际装到哪台，想换告诉我）

---

# 第三层 · 装配映射

> [输出协议 reload]：本章节及后续所有装配工具调用期间，过程描述走 thinking，user-facing 仅在终态与反问输出。

按维度取值映射到 CLI 命令。多命令按顺序执行。

`--name` 必带 `[<task_id>]` 前缀（适用 `rule create` / cron job；task_id 规范见 §前置检查）。

## 装配执行规则

**单任务串行装配**：多 task 装配按 task 维度串行——上一个 task 装完所有相关命令、CLI 返回成功后，才能启动下一个 task。**禁止同 model turn 中并发多 task 的 CLI 命令**。

**CLI 报错后重审全命令**：任何 CLI 报错后，重装该命令前必须对所有参数逐项重新检查，不允许仅针对报错字段补丁式重发。

## CLI 命令

| 维度 | CLI |
|---|---|
| 创建 task | `miloco-cli task create --task-id <id> --description "<描述>"` |
| Rule=Y | `miloco-cli rule create --task-id <id> <rule-flags>` |
| Schedule=Y | 调 OpenClaw cron tool 创建 cron job（`name` 必带 `[<id>]` 前缀，`message` 写业务意图）→ 拿 `jobId`；调 `miloco-cli task link --task <id> --kind cron --ref <jobId>` 挂 |
| Record=Y | `miloco-cli task record init <id> --kind <progress/duration/event> --content '<JSON>'` |
| Lifecycle=temporary | 调 OpenClaw cron tool 建 termination at job（at=`<expires_at>`，message=`到期销毁 task <id>`），expires_at 用 `miloco-cli time-compute --anchor <kind>` 算 |

## Rule flag 映射

| 维度取值 | flag |
|---|---|
| name（必填） | `--name "[<task_id>] <场景描述>"` |
| `mode=event` | `--mode event` |
| `mode=state` | `--mode state` |
| `condition.query` | `--condition "<query>"` |
| event + action JSON | `--action '<JSON>'` |
| event + desc | `--action-desc "<desc>"` |
| state + on_enter action JSON | `--on-enter-action '<JSON>'` |
| state + on_enter desc | `--on-enter-desc "<desc>"` |
| state + on_exit action JSON | `--on-exit-action '<JSON>'` |
| state + on_exit desc | `--on-exit-desc "<desc>"` |
| on_exit 留空 | 不传 on_exit flag |
| `duration_seconds=N` | `--duration-seconds N` |
| `exit_debounce_seconds=N` | `--exit-debounce-seconds N` |
| 感知设备=`<DID>` | `--source <DID>` |
| 感知设备=广播 | 不传 `--source` |

## Record content JSON

| kind | content |
|---|---|
| progress（temporary）| `{"target":N,"unit":"<次/杯/步>","window":"<day/week>","expires_at":"<ISO>"}` |
| progress（recurring）| `{"target":N,"unit":"<次/杯/步>","window":"<day/week>","recurring_pattern":{"window":"<day/week>"}}` |
| duration 含阈值（temporary）| `{"target_minutes":N,"expires_at":"<ISO>"}` |
| duration 含阈值（recurring）| `{"target_minutes":N,"recurring_pattern":{"window":"<day/week>"}}` |
| duration 无阈值（longterm）| `{"recurring_pattern":{"window":"longterm"}}` |
| duration 无阈值（recurring）| `{"recurring_pattern":{"window":"<day/week>"}}` |
| event | `{}` |

**装填规则**：

- progress 顶层 `window` 必填，决定 period 边界
- recurring 任务必填 `recurring_pattern`

## 装配失败回滚

多步执行（task create → record init → cron+link → rule create）任一步失败 → 调 `miloco-cli task delete <task_id> --reason abandoned` 回滚。

- backend 一笔事务同步清 task / rule / record / task_link，agent 不重复清
- 跑响应里的 `agent_pending`（仅含 cron kind），按顺序逐条 `cron remove`
- 本 turn 已建但**未挂 task_link** 的 cron jobId → 按本 turn 已知的 jobId 单独 `cron remove`
- task create 本身失败 → 无需调 delete

## 装配示例

> example 展示判据→取值的推导过程，具体取值仅适用其原话场景。每个用户原话独立按 §第一层 / §第二层 判据推。

cron 由 OpenClaw cron tool 创建，拿到 `jobId` 后用 `miloco-cli task link --task <id> --kind cron --ref <jobId>` 挂关联。

### 例 1

用户："家里有人摔倒就报警"

推理：「摔倒」人身安全异常 → §Rule?=Y · §Rule.mode(人身安全)=event；无累计/计数 → §Record?=N；现实事件触发 → §Schedule?=N；无信号兜底 → §Lifecycle(无信号兜底)=permanent；「报警」通知类 → §Rule.action=desc · §通道反问 触发(必反问 A/B/C)；§Rule.感知设备 N≥1 + 「家里」未指定房间 → 不传 `--source` + 触发装配提示

```
Rule?=Y · Schedule?=N · Record?=N · Lifecycle=permanent
Rule.mode=event · action=desc
通道反问 → A 音箱
```

```bash
miloco-cli task create --task-id fall_alert --description "家里有人摔倒报警"
miloco-cli rule create --task-id fall_alert \
  --name "[fall_alert] 摔倒报警" \
  --mode event \
  --condition "任何人身体突然失去平衡倒地，呈仰面/侧卧/俯卧姿态躺在地面；不含主动卧倒、躺床睡觉等休息姿态，不含做仰卧起坐、瑜伽下犬式/平板支撑等贴地运动" \
  --action-desc "使用音箱播报通知：有人摔倒了，立即报警"
```

### 例 2

用户："起床后自动开窗帘"

推理：「起床」人体可观测动作 → §Rule?=Y；「开窗帘」"开 X" 动作 → §Rule.mode(激活持续设备)=state · 默认补 on_exit 复位；现实事件触发 → §Schedule?=N；无累计/计数 → §Record?=N；无信号兜底 → §Lifecycle(无信号兜底)=permanent；state 默认 → §Rule.exit_debounce_seconds=60；§Rule.感知设备 N≥1 + 未指定房间 → 不传 `--source` + 触发装配提示

```
Rule?=Y · Schedule?=N · Record?=N · Lifecycle=permanent
Rule.mode=state · 默认补 on_exit 复位 · exit_debounce_seconds=60
```

```bash
miloco-cli task create --task-id wakeup_curtain --description "起床开窗帘"
miloco-cli rule create --task-id wakeup_curtain \
  --name "[wakeup_curtain] 起床开窗帘" \
  --mode state \
  --condition "用户从卧床躺姿切换到坐起或离床站立姿态；不含翻身、伸懒腰等床上小动作，不含坐起喝水后躺回等短暂起身动作" \
  --exit-debounce-seconds 60 \
  --on-enter-action '{"did":"<窗帘 DID>","iid":"prop.X","value":true,"idempotent":true}' \
  --on-exit-desc "检查开窗帘是否仍有效，若已结束则复位"
```

### 例 3

用户："明天 9 点提醒吃药"

推理：「明天 9 点」单一未来具体时点 → §Schedule?=Y(at)；绝对一次性时刻信号 → §Lifecycle(绝对一次性时刻)=temporary；纯时间触发 → §Rule?=N · §Record?=N

```
Rule?=N · Schedule?=Y(at) · Record?=N · Lifecycle=temporary
```

```bash
AT_TIME=$(miloco-cli time-compute --anchor '{"kind":"tomorrow_at","time":"09:00:00"}')
miloco-cli task create --task-id med_tomorrow_9am --description "明天 9 点提醒吃药"

# 调 OpenClaw cron tool 建 at job：name="[med_tomorrow_9am] 吃药提醒"，at=$AT_TIME，message="提醒用户吃药"
# → 拿 jobId
miloco-cli task link --task med_tomorrow_9am --kind cron --ref <jobId>
```

### 例 4

用户："今天喝够 8 杯水提醒"

推理：「喝水」人体可观测动作 → §Rule?=Y；「8 杯」计数达标 → §Record?=Y · §Record.kind=progress · target=8 · window=day；「今天」时间窗信号 → §Lifecycle(时间窗信号)=temporary · expires_at=今日 24:00；瞬时动作 + 计数型 → §Rule.mode(瞬时动作)=event；progress + target>0 → §Schedule?=Y(cron) · §Schedule.频率默认(progress+target>0 多时点) · N=min(8,6)=6 时点 · 无强场景均分 10-20；「提醒」通知类 → §Rule.action=desc · §通道反问 触发(必反问 A/B/C)；句中无主体 → 主语=`用户` + 触发装配提示；§Rule.感知设备 N≥1 + 未指定房间 → 不传 `--source` + 触发装配提示

```
Rule?=Y · Schedule?=Y(cron×6 默认+装配提示) · Record?=Y · Lifecycle=temporary
Rule.mode=event · Record.kind=progress · target=8 · window=day
通道反问 → B 手机
```

```bash
EXPIRES_AT=$(miloco-cli time-compute --anchor '{"kind":"end_of_day"}')
miloco-cli task create --task-id drink_8_today --description "今天喝够 8 杯水"

miloco-cli rule create --task-id drink_8_today \
  --name "[drink_8_today] 喝水计数" \
  --mode event \
  --condition "用户手持水杯/水瓶/茶杯/保温杯等饮品容器，杯口贴近嘴边并伴随仰头吞咽动作；不含手持牙刷/麦克风/纸盒/食物/餐盒等非饮品物品，不含举杯凑近鼻子闻、吹凉、展示等动作" \
  --action-desc "喝水次数加一；首次达标时使用手机推送通知：恭喜达标"

miloco-cli task record init drink_8_today \
  --kind progress \
  --content "{\"target\":8,\"unit\":\"杯\",\"window\":\"day\",\"expires_at\":\"$EXPIRES_AT\"}"

# 调 OpenClaw cron 建提醒 cron（喝水无强场景 → 均分 10-20，N=min(8,6)=6 时点）：name="[drink_8_today] 喝水提醒"，cron="0 10,12,14,16,18,20 * * *"，message="调 miloco-cli task record get drink_8_today，按 derived.remaining 决定是否催"
miloco-cli task link --task drink_8_today --kind cron --ref <jobId_remind>

# 调 OpenClaw cron 建到期销毁 at：name="[drink_8_today] 到期销毁"，at=$EXPIRES_AT，message="到期销毁 task drink_8_today"
miloco-cli task link --task drink_8_today --kind cron --ref <jobId_termination>
```

### 例 5

用户："每天累计玩手机超 1 小时提醒"

推理：「玩手机」人体可观测行为 → §Rule?=Y；「累计 1 小时」时长累计 → §Record?=Y · §Record.kind=duration · target_minutes=60；「每天累计 N 小时」→ §Schedule?=N；「每天」周期信号 → §Lifecycle(周期信号)=permanent · §Record.task_type=recurring · recurring_pattern={"window":"day"}；跨次累计 record(duration kind) → §Rule.mode(跨次累计 record)=state；看屏幕姿态稳定窗 → §Rule.duration_seconds=90；state + duration record 离散使用型 → §Rule.exit_debounce_seconds=60；「提醒」通知类 → §Rule.action=desc · §通道反问 触发(必反问 A/B/C) · 跨次累计达标 → §达标通知机制 → on-target-desc；§Rule.感知设备 N≥1 + 未指定房间 → 不传 `--source` + 触发装配提示

```
Rule?=Y · Schedule?=N · Record?=Y · Lifecycle=permanent
Rule.mode=state · duration_seconds=90 · exit_debounce_seconds=60
通道反问 → B 手机
Record.kind=duration · target_minutes=60 · task_type=recurring · window=day
```

```bash
miloco-cli task create --task-id phone_time_daily --description "每天累计玩手机超 1 小时提醒"
miloco-cli task record init phone_time_daily \
  --kind duration \
  --content '{"target_minutes":60,"recurring_pattern":{"window":"day"}}'
miloco-cli rule create --task-id phone_time_daily \
  --name "[phone_time_daily] 玩手机累计时长" \
  --mode state \
  --condition "用户手持手机，屏幕亮起朝向脸部，目光低头注视屏幕；不含手持平板/书本/遥控器，不含手机贴耳通话" \
  --duration-seconds 90 \
  --exit-debounce-seconds 60 \
  --on-enter-desc "记录计时起点" \
  --on-exit-desc "结束计时" \
  --on-target-desc "使用手机推送通知：今日累计玩手机已达目标时长，休息一下"
```

### 例 6

用户："客厅没人就关灯"

推理：「客厅没人」环境状态变化 → §Rule?=Y；无累计/计数 → §Record?=N；现实事件触发 → §Schedule?=N；无信号兜底 → §Lifecycle(无信号兜底)=permanent；"关 X" 动作 → §Rule.mode(关 X 动作)=event；持续条件「没人」需稳定观测 → §Rule.duration_seconds=300；「客厅」指定房间 → §Rule.感知设备 N 内按 room_name 匹配命中 1 台 → `--source <客厅摄像头 DID>`；「关灯」固定 handler → §Rule.action=action JSON

```
Rule?=Y · Schedule?=N · Record?=N · Lifecycle=permanent
Rule.mode=event · duration_seconds=300 · action=action JSON
```

```bash
miloco-cli task create --task-id living_room_off --description "客厅没人关灯"
miloco-cli rule create --task-id living_room_off \
  --name "[living_room_off] 客厅无人关灯" \
  --mode event \
  --condition "画面中无人" \
  --source <客厅摄像头 DID> \
  --duration-seconds 300 \
  --action '{"did":"<客厅灯 DID>","iid":"prop.on","value":false,"idempotent":true}'
```

### 例 7

用户："久坐 30 分钟提醒一下"

推理：「坐」人体可观测姿态 → §Rule?=Y；「30 分钟」时长阈值 + 无跨次词 → §Record?=N · 单次连续 + 触发装配提示；现实事件触发 → §Schedule?=N；无信号兜底 → §Lifecycle(无信号兜底)=permanent；持续行为 + 一次性通知 → §Rule.mode(持续行为+一次性通知)=event；30 分钟 → §Rule.duration_seconds=1800；「提醒」通知类 → §Rule.action=desc · §通道反问 触发(必反问 A/B/C)；句中无主体 → 主语=`用户` + 触发装配提示；§Rule.感知设备 N≥1 + 未指定房间 → 不传 `--source` + 触发装配提示

```
Rule?=Y(触发装配提示) · Schedule?=N · Record?=N · Lifecycle=permanent
Rule.mode=event · duration_seconds=1800 · action=desc
通道反问 → B 手机
```

```bash
miloco-cli task create --task-id sit_30min --description "久坐 30 分钟提醒"
miloco-cli rule create --task-id sit_30min \
  --name "[sit_30min] 久坐 30 分钟提醒" \
  --mode event \
  --condition "用户臀部接触沙发/座椅，腰背靠近椅背或半弯曲，保持坐姿；不含蹲在地面双膝弯曲但臀部未接触座面，不含半靠扶手/桌沿臀部悬空的站姿，不含跪地或跪坐臀部触脚跟未接触座面" \
  --duration-seconds 1800 \
  --action-desc "使用手机推送通知：已久坐 30 分钟，建议起身活动"
```

### 例 8

用户："妈妈回家就播放欢迎曲"

推理：「妈妈」具体人物 → person list 1 命中 → role/name；「回家」到达/进入类瞬时事件 → §Rule?=Y · §Rule.mode(到达/进入瞬时事件)=state · §Rule.exit_debounce_seconds=1800 · on_exit 留空；「欢迎曲」一次性 TTS → §Record?=N · §Schedule?=N；无信号兜底 → §Lifecycle(无信号兜底)=permanent；玄关无明示视角 → §感知视角 常识默认 + 装配提示；「玄关」隐含房间 → §Rule.感知设备 N 内按 room_name 匹配命中 1 台 → `--source <玄关摄像头 DID>`

```
Rule?=Y · Schedule?=N · Record?=N · Lifecycle=permanent
Rule.mode=state（到达事件）· on_exit 留空 · exit_debounce_seconds=1800
命题主语="妈妈"（跑 person list 唯一命中）
感知视角：玄关/门口走 agent 常识默认 + 装配提示
```

```bash
miloco-cli task create --task-id mom_arrival --description "妈妈回家欢迎播报"
miloco-cli rule create --task-id mom_arrival \
  --name "[mom_arrival] 妈妈回家欢迎" \
  --mode state \
  --condition "妈妈伴随开门动作从户外走入玄关画面，正面或侧面对镜头；不含路过门口但未开门走入的短暂停留，不含其他家庭成员或来访客人进门" \
  --source <玄关摄像头 DID> \
  --exit-debounce-seconds 1800 \
  --on-enter-desc "使用音箱播报通知：向妈妈说一段欢迎回家的话"
```

### 例 9

用户："提醒我每天喝 8 杯水"

推理：「我」第一人称 → person list 1 命中 → role 非空填 role · 触发装配提示；「喝水」人体可观测动作 → §Rule?=Y；「8 杯」计数达标 → §Record?=Y · §Record.kind=progress · target=8 · window=day；「每天」周期信号 → §Lifecycle(周期信号)=permanent · recurring_pattern={"window":"day"}；瞬时动作 + 计数型 → §Rule.mode(瞬时动作)=event；progress+target>0 → §Schedule?=Y(cron) · §Schedule.频率默认(progress+target>0 多时点) · N=min(8,6)=6 时点 · 无强场景均分 10-20；「提醒」通知类 → §Rule.action=desc · §通道反问 触发(必反问 A/B/C)；§Rule.感知设备 N≥1 + 未指定房间 → 不传 `--source` + 触发装配提示

```
Rule?=Y · Schedule?=Y(cron×6 默认+装配提示) · Record?=Y · Lifecycle=permanent
Rule.mode=event · 命题主语=<当前用户 role>（"我" → 跑 person list 1 命中 → role + 装配提示；此例假设命中 role=妈妈）· Record.kind=progress · target=8 · window=day · recurring_pattern={"window":"day"}
通道反问 → B 手机
```

```bash
miloco-cli task create --task-id drink_8_daily --description "妈妈每天喝 8 杯水"

miloco-cli rule create --task-id drink_8_daily \
  --name "[drink_8_daily] 喝水计数" \
  --mode event \
  --condition "妈妈手持水杯/水瓶/茶杯/保温杯等饮品容器，杯口贴近嘴边并伴随仰头吞咽动作；不含手持牙刷/麦克风/纸盒/食物/餐盒等非饮品物品，不含举杯凑近鼻子闻、吹凉、展示等动作" \
  --action-desc "喝水次数加一；首次达标时使用手机推送通知：恭喜达标"

miloco-cli task record init drink_8_daily \
  --kind progress \
  --content '{"target":8,"unit":"杯","window":"day","recurring_pattern":{"window":"day"}}'

# 调 OpenClaw cron 建提醒 cron（喝水无强场景 → 均分 10-20，N=min(8,6)=6 时点）：name="[drink_8_daily] 喝水提醒"，cron="0 10,12,14,16,18,20 * * *"，message="调 miloco-cli task record get drink_8_daily，按 derived.remaining 决定是否催"
miloco-cli task link --task drink_8_daily --kind cron --ref <jobId_remind>
```

