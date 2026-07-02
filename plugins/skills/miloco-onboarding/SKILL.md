---
name: miloco-onboarding
description: 家庭信息首次初始化 / onboarding —— 用户说"初始化家庭""配置家庭成员""首次设置""帮我建家庭档案""onboarding"时激活；也在身份库成员为空 + 家庭档案为空、用户刚接入 miloco 时，由 agent 主动提议引导。通过一问一答访谈，把家庭成员与家庭档案一次性建起来。
metadata:
  author: miloco
  version: "1.0"
  date: "2026-07-02"
  openclaw:
    requires:
      bins: ["miloco-cli"]
---

# miloco-onboarding

首次初始化家庭信息：用**一问一答的访谈**把家庭成员（写入身份库 person）和家庭档案
（member_* / family / space / device 条目）一次性建起来。这些数据会被注入 agent 系统
提示词和感知引擎——**开局就录准，miloco 才能从第一天起就懂这家人**。后续控制设备、给建议、
写通知都参考它们，所以本流程的目标是"**准、且经用户确认**"，不是"多"。

本 skill 只负责**首次引导 + 批量写入**；单条家庭信息的日常增改走
[miloco-home-profile](../miloco-home-profile/SKILL.md)，成员的日常 CRUD 走
[miloco-miot-identity](../miloco-miot-identity/SKILL.md)，录人脸/身形样本走
[miloco-miot-identity-register](../miloco-miot-identity-register/SKILL.md)。

## 何时激活

**主动请求：** 用户说"初始化家庭""配置家庭成员""首次设置""帮我建家庭档案""onboarding"
"刚装好，帮我设置一下"等。

**被动提议：** 当一次对话里发现身份库无成员 **且** 家庭档案为空（用户像是刚接入 miloco），
可以主动问一句："要不要我花几分钟带你把家里的成员和一些基本情况登记一下？以后我控制设备、
提醒你事情会更贴合。" 用户答应才进入访谈；拒绝就正常继续，不反复劝。

判断是否为空（进入前先查一次，两条都跑）：

```bash
miloco-cli person list --pretty
miloco-cli home-profile list --target profile --pretty
```

- 两者都空 → 首次初始化，走完整访谈。
- 已有数据 → 这是**重跑**，进入访谈前先读现状，按「幂等重跑」小节处理（复核补充，不重复建）。

## 总原则（必须遵守）

1. **一次只问一个问题。** 访谈是聊天不是表单。发一个问题 → 等用户回 → 再问下一个。
   **绝不**把多个问题堆进一条消息、也不要一次性把整张问卷甩给用户。
   > 反例（禁止）："请告诉我：①家里几口人分别是谁 ②各自作息 ③有什么家庭规则 ④设备别名"
   > 正例：先只问"家里都有谁呀？把名字和身份（爸爸/妈妈/孩子/老人…）告诉我就行。"
2. **随时可跳过。** 每个可选环节都提示"不想答可以跳过 / 直接说'跳过'"。用户跳过就跳过，
   不追问、不补问。
3. **写入前必须确认。** 访谈结束 → 汇总成一段清单给用户看 → 用户确认后才动写命令。
4. **绝不编造。** 只登记用户**明确说过**的事实。用户没说的**不要脑补**（例如别因为有孩子就
   假设"晚上要安静"）。如果你想记一条推断出来的信息，先问用户确认——确认了它就变成用户明示，
   没确认就不写。
5. **只碰能建的东西。** 家庭成员（person）和家庭档案（home-profile 条目）是本流程能创建的。
   **家、房间、设备本身是从米家云同步来的只读数据，onboarding 绝不去新建/改名它们**——只对
   设备/空间做"别名、习惯、格局"这类**注解**（存成 device/space 档案条目）。

## 访谈流程

按 a→e 顺序推进，**每步一问一答**。可选环节用户可跳过。

### a. 家庭成员（必做）

先问成员：**名字 + 身份**。一次问全家、让用户一口气说完即可（这是"让用户一次说完"，不是
"你一次问多项"）：

> "家里都有谁呀？把名字和身份（爸爸/妈妈/孩子/老人…）说给我就行，比如'我是张伟爸爸，我爱人李娜妈妈，还有 7 岁的儿子张小乐'。"

- **真名必填，身份（家庭角色）可选**：抽不到真名就追问（"这位叫什么名字？"）；抽不到角色就
  留空，不追问。
- **宠物**：宠物**不进身份库 person 表**，按家庭档案的宠物规则记成 `member_persona` 等条目
  （`subject_name` = 宠物名，`subject_id` 留空）。所以问成员时可顺带问"有没有养宠物？"
- 记下每个人的名字/角色，稍后写入时用。

### b. 每位成员的画像（可选 · 快速过一遍）

对已登记的每位成员，快速问一两句就够，别逐维盘问。用户可整段跳过：

> "想让我更懂大家的话，可以简单说说每个人——比如作息、有什么偏好、要留意的健康情况、平时的
> 娱乐习惯。不想说可以跳过。"

按用户回答归类到档案条目类型（详见 [miloco-home-profile](../miloco-home-profile/SKILL.md) 的 type 表）：

| 用户说的 | 条目 type |
|---|---|
| 作息、出行规律（"爸爸 7:30 出门"） | `member_routine` |
| 偏好（温度、光线、饮食："妈妈空调爱开 24 度"） | `member_preference` |
| 健康（过敏、禁忌、慢病："妈妈对花粉过敏"） | `member_health` |
| 娱乐（观影、音乐、游戏："睡前听白噪音"） | `member_entertain` |
| 身份/画像（"爸爸是主厨"） | `member_persona` |

### c. 家庭规则（可选）

问全家共同遵守的**规则/约定**（→ `family` 条目，`subject_name` 固定 `"shared"`）：

> "家里有没有什么规矩想让我记住？比如几点以后要安静、有没有安全上的提醒（像'小孩单独进厨房
> 要提醒大人'）、来客人时怎么处理。"

常见：静音时段、安全规则、访客策略。注意 `family` **只装规则**，不装"家里几口人"这类构成信息
（构成走 `member_persona`）。

### d. 空间与设备注解（可选 · 高价值）

这一步专门消除以后语音控制的歧义，尤其是**设备别名**。**先拉一次设备目录**看看家里有哪些设备、
哪些容易混淆（同一房间多盏灯 / 多台同类设备）：

```bash
miloco-cli device catalog
```

然后**针对性地**问模糊点，别泛泛问。例如目录里客厅有主灯和灯带两盏：

> "客厅我看到有'主灯'和'灯带'两个灯。你平时说'客厅灯'一般指哪个？"

- 用户澄清的别名/默认指代 → `device` 条目（`subject_name` = 设备名或别名）。
- 空间格局/朝向/动线（"主卧朝南，空调出风口对床头"）→ `space` 条目（`subject_name` = 空间名）。
- 通用而非绑定某台设备/空间的信息，`subject_name` 用 `"general"`。
- 设备控制细节（枚举、spec）不在这里问，那是 [miloco-devices](../miloco-devices/SKILL.md) 运行时的事。

### e. 人脸/身形登记（可选 · 指路，不在此实现）

想让摄像头认得出家人，需要照片/视频样本——**那是另一个流程**。这里只**告知并指路**，不处理任何
媒体：

> "如果想让摄像头以后能认出家人，可以给每个人录点照片或一段视频。要现在弄的话，跟我说'给张伟
> 登记样本'并发张照片就行。"

用户要做 → 转 [miloco-miot-identity-register](../miloco-miot-identity-register/SKILL.md)。不做就跳过。

## 写回流程

访谈拿到信息、**用户看过汇总并确认后**，按顺序执行：

### 1. 先查现状（幂等前提）

```bash
miloco-cli person list --pretty
```

拿到现有成员的 `id / name / role`。**按真名去重**：名字已存在的**不要再建**（`person add`
撞名会 409 报错）。

### 2. 建/更新成员（先做，因为档案条目要用 person_id）

对每位成员：

- **新成员** → `miloco-cli person add --name "<真名>" [--role "<角色>"] --pretty`
  返回体 `data.person_id` 就是该成员的 id，**记下来**，第 4 步 member_* 条目要用它。
- **已存在、只是角色变了** → `miloco-cli person update <person_id> --role "<新角色>"`（复用已有 id）。
- 宠物**不建 person**（见访谈 a）。

```bash
# 例：新建三名成员，逐条记下返回的 person_id
miloco-cli person add --name "张伟" --role "爸爸" --pretty      # → data.person_id = <爸爸id>
miloco-cli person add --name "李娜" --role "妈妈" --pretty      # → data.person_id = <妈妈id>
miloco-cli person add --name "张小乐" --role "孩子" --pretty    # → data.person_id = <孩子id>
```

### 3. 组装档案 ops 数组

把访谈里所有要写的档案信息组装成**一个** ops 数组，全部用 `op: "add"`（首次初始化都是新条目）。
字段规则见下方「ops 字段速查」。**member_* 条目的 `subject_id` 必须填第 2 步记下的真实
person_id**；`family` 用 `subject_name:"shared"`；`space/device` 用空间/设备名（通用信息用
`"general"`）；宠物条目 `subject_id` 留空、`subject_name` 填宠物名。

下面是一份完整、可直接照搬结构的示例（对应上面这家人；真实执行时把 `subject_id` 换成第 2 步
拿到的真实 id）：

<!-- onboarding-ops-example -->
```json
[
  {"op": "add", "entry": {"type": "member_persona", "subject_id": "3f2a9c14-8b7e-4d21-9f6a-1c2d3e4f5a6b", "subject_name": "爸爸", "content": "爸爸张伟，家里的主厨", "evidence_log": ["2026-07-02: 初始化时用户告知——爸爸张伟，负责做饭"]}},
  {"op": "add", "entry": {"type": "member_routine", "subject_id": "3f2a9c14-8b7e-4d21-9f6a-1c2d3e4f5a6b", "subject_name": "爸爸", "content": "工作日通常 7:30 出门、19:00 回家", "evidence_log": ["2026-07-02: 初始化时用户告知爸爸作息"]}},
  {"op": "add", "entry": {"type": "member_health", "subject_id": "6b1e0d52-2c4a-4f8b-8a3d-7e9f0a1b2c3d", "subject_name": "妈妈", "content": "对花粉过敏", "evidence_log": ["2026-07-02: 初始化时用户告知妈妈过敏史"]}},
  {"op": "add", "entry": {"type": "member_preference", "subject_id": "6b1e0d52-2c4a-4f8b-8a3d-7e9f0a1b2c3d", "subject_name": "妈妈", "content": "空调偏好 24°C 制冷", "evidence_log": ["2026-07-02: 初始化时用户告知妈妈偏好"]}},
  {"op": "add", "entry": {"type": "member_entertain", "subject_id": "6b1e0d52-2c4a-4f8b-8a3d-7e9f0a1b2c3d", "subject_name": "妈妈", "content": "睡前习惯听白噪音", "evidence_log": ["2026-07-02: 初始化时用户告知妈妈娱乐习惯"]}},
  {"op": "add", "entry": {"type": "member_persona", "subject_id": "9c7d1a83-4e5f-4a90-b1c2-3d4e5f60718a", "subject_name": "孩子", "content": "孩子张小乐，7 岁", "evidence_log": ["2026-07-02: 初始化时用户告知家里有 7 岁孩子张小乐"]}},
  {"op": "add", "entry": {"type": "member_persona", "subject_name": "旺财", "content": "养了一只金毛犬旺财（宠物，不在身份库）", "evidence_log": ["2026-07-02: 初始化时用户告知养了狗旺财"]}},
  {"op": "add", "entry": {"type": "family", "subject_name": "shared", "content": "22:00 后全屋静音，不做语音播报", "evidence_log": ["2026-07-02: 初始化时用户设定的家庭规则"]}},
  {"op": "add", "entry": {"type": "family", "subject_name": "shared", "content": "孩子单独进厨房时提醒大人", "evidence_log": ["2026-07-02: 初始化时用户设定的安全规则"]}},
  {"op": "add", "entry": {"type": "device", "subject_name": "客厅灯", "content": "「客厅灯」默认指客厅主灯（吸顶灯）；灯带请说「客厅灯带」", "evidence_log": ["2026-07-02: 初始化时用户澄清客厅灯别名"]}},
  {"op": "add", "entry": {"type": "space", "subject_name": "主卧", "content": "主卧朝南，空调出风口正对床头", "evidence_log": ["2026-07-02: 初始化时用户告知主卧格局"]}}
]
```

### 4. 一次性写入 + 提交

档案条目含中文，**用文件形式传 `--ops-file` 避免 shell 转义出错**（把第 3 步的数组写进临时
文件），带 `--user-edit`（自动置 `source=user_told`、`confidence=1.0`），然后 commit 渲染：

```bash
# 把第 3 步组装好的 ops 数组写入临时文件（内容就是那个 JSON 数组）
#   （也可直接 --ops '[...]'，但中文多、易踩 shell 转义，推荐文件形式）
miloco-cli home-profile profile-write --user-edit --ops-file /tmp/onboarding_ops.json --pretty
miloco-cli home-profile commit --pretty
```

### 5. 读回并给用户一个摘要

```bash
miloco-cli home-profile show
```

把渲染出的档案精简成一段人话回给用户："已经登记好啦——成员：爸爸张伟、妈妈李娜、孩子张小乐，
还有狗狗旺财；规则：22 点后静音、小孩进厨房会提醒；也记了'客厅灯'默认指主灯。以后想改或补充随时
跟我说。" 顺带提一句可选的下一步（录样本走 register skill）。

## ops 字段速查

`profile-write` 的每个 op：`{"op": "add", "entry": {...}}`。`entry` 字段（完整规则以
[miloco-home-profile](../miloco-home-profile/SKILL.md) 的「条目格式」为准）：

- `type`：8 选 1 —— `member_persona / member_health / member_routine / member_entertain /
  member_preference / family / space / device`。
- `subject_id`：仅 `member_*` 填**真实 person_id**（第 2 步拿到）；`family` / `space` / `device`
  / 宠物条目留空（不写或写 `null`）。
- `subject_name`：member_* 填成员名/角色（如"爸爸"）；`family` 固定 `"shared"`；`space/device`
  填空间/设备名，通用信息填 `"general"`；宠物填宠物名。
- `content`：一句话事实，精简。
- `evidence_log`：`["YYYY-MM-DD: 初始化时用户告知 <原话摘要>"]`。
- `confidence` / `source` **不用写**——带 `--user-edit` 时 service 统一置 `1.0` / `user_told`。

> 保留值 `"shared"`（member_*/family 全家共享）和 `"general"`（space/device 通用）会让 service
> 自动清空 `subject_id`，不要再给它们绑 id。

## 幂等重跑

再次运行 onboarding **不能重复建数据**：

1. 进入前已按「何时激活」查过 `person list` + `home-profile list`。
2. **成员**：真名已存在 → 不 `person add`；只在角色有变时 `person update`。
3. **档案**：先 `miloco-cli home-profile list --target profile --pretty` 看全量（含条目 id）。
   - 已有等价条目 → 跳过，别再 `add` 出重复。
   - 用户明确要改某条 → 用 `op:"replace"`（带该条 id）而不是再 add；要删 → `op:"delete"`（带 id）。
   - 只有真正新增的信息才 `add`。
4. 重跑时把访谈重点放在"缺什么、要改什么"，而不是从头再问一遍已答过的。

> 注意：从**系统提示词里注入的档案摘要**看不到条目 id、还可能被截断，**不能据它做增改**——每次
> 增改前都重新 `home-profile list` 拉全量（同 home-profile skill 的约定）。

## 安全红线

- **确认后再写**：没给用户看汇总、没拿到确认，不执行任何 `person add` / `profile-write`。
- **不编造**：用户没明说的不写；想记推断先问、确认后才算用户明示。
- **敏感信息不记**：密码、证件号、银行卡、API Key 等一律不写入档案。
- **不碰只读数据**：不新建/改名 家 / 房间 / 设备本体（只对它们写注解条目）。
- **person_id 要真**：member_* 条目的 `subject_id` 必须是 `person add` / `person list` 返回的
  真实 id，别编 id、也别把角色名当 id。

## 边界

- ❌ 不录人脸/身形样本（→ [miloco-miot-identity-register](../miloco-miot-identity-register/SKILL.md)）。
- ❌ 不新建/编辑 家 / 房间 / 设备本体（只读，来自米家云同步）。
- ❌ 不控制设备、不做实时感知。
- ✅ 只建 person 行 + 写 home-profile 条目，且都在用户确认后。
- ✅ 可重跑：复核 + 增量更新，不产生重复数据。
