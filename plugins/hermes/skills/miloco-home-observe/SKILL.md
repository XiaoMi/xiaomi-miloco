---
name: miloco-home-observe
description: home-dreaming 定时任务的 Observe 步骤，仅由该任务调用。
metadata:
  author: miloco
  version: "2.2"
  date: 2026-06-16
  openclaw:
    requires:
      bins: ["miloco-cli"]
---

# Observe（观察提取）

从感知记忆和交互记忆中提取**值得沉淀**的家庭知识（成员的习惯、偏好、健康、画像，以及家居环境），累积到家庭档案。

> 仅在 miloco-home-dreaming cron 流程中激活，不单独使用。

读到一条观察后，按这条决策链处理：**① 值不值得记 → ② 归哪一类 → ③ 和已有知识什么关系 → 落命令**。下面依次展开。

## 执行步骤

1. 尝试读取昨日和今日的感知记忆 `memory/YYYY-MM-DD-miloco-perception.md`
2. 尝试读取昨日和今日的交互记忆 `memory/YYYY-MM-DD.md`
3. 执行 `miloco-cli home-profile list --target both` 查看已有知识
4. 从记忆中提取新知识，结合 `evidence_log`（完整证据列表）判断与已有条目的关系
5. 执行 `miloco-cli home-profile candidate-write` 或 `miloco-cli home-profile profile-write` 操作

> **注意**：步骤 1、2 中的记忆文件在当天没有相关记录时不会生成。读取失败（文件不存在）时直接跳过该文件，继续处理剩余可用的记忆源。不要重复尝试读取同一个不存在的文件。只要至少有一个记忆源有内容即可继续后续步骤；若所有文件均不存在，则本次 Observe 无输入可处理，直接结束。

## ① 值不值得记（提取原则）

家庭档案只有一个用途：**让 agent 借它读懂每个成员的生活习惯，从而把控设备、调环境、提醒关怀做得更精准、更合各人心意**。所有提取都服务于此，唯一标尺是——

> **核心判断：知道这条，未来能让某次「控设备 / 调环境 / 提醒关怀」更准、更贴某个具体成员的心意吗？** 能 → 记；只是"识别到了"却不会改变任何未来动作 → 丢。

感知识别结果天然嘈杂，**大多数没有沉淀价值**，宁缺毋滥——记一条无用信息，未来只会干扰判断。

**值得记**（每类都能落到具体行为上）：

| 信息 | 未来用它做什么 | 例子 |
|------|------|------|
| 偏好 | 调环境到成员舒适态 | 爸爸喜欢 24°C 制冷、奶奶要暖光 |
| 作息 / 习惯 | 把提醒、自动化卡在对的时机 | 工作日 7:30 出门、每晚 9 点吃药 |
| 健康 / 禁忌 | 关怀与安全的硬约束 | 花粉过敏、高血压、行动不便 |
| 画像 / 构成 | 弄清"家里有谁、谁要被照顾" | 谁是主厨、有个小孩豆豆、养了狗旺财 |
| 空间 / 设备 | 让控制落到对的设备、给对的预期 | 主卧出风口对床头、客厅空调达温慢 |

**不值得记**（记了也用不上，直接丢）：

- **与控制 / 关怀无关的琐碎细节**：用橙色手机、桌上有水杯、今天穿了什么
- **纯一次性临时状态**：今天还没回来、客厅现在有人、桌上有个快递——除非反复出现、沉淀成规律
- **等于常识、无个性化增量**：人晚上会睡觉、开灯要用电
- **推不出任何成员习惯的"裸识别"**：仅"识别到一个人 / 一只猫"，不带可指导未来动作的信息

**置信度怎么给**（已判定值得记的，按性质决定单次能否入候选——候选区是"证据累积区"，低置信不等于丢弃）：

- **稳定属性**（外貌/身份/体质/空间格局/设备固有特征）：属性不靠重复确认，单次观察即可入候选，confidence 取决于证据强度而非次数（如"戴黑框眼镜"）。
- **行为/偏好模式**（作息/习惯/偏好）：单次以低 confidence（0.3~0.5）入候选，content 标注"疑似"，靠后续重复出现累积证据再 promote；不要因"只有一次"就丢弃可能的规律（如"工作日上午在办公室用电脑"）。

## ② 归哪一类（type 与 subject）

**type 分类：**

| type | 含义 | 示例 |
|------|------|------|
| member_persona | 成员画像（家庭角色、身份、外貌） | "爸爸是家里的主厨" |
| member_health | 体质健康（过敏、禁忌、慢性病） | "妈妈对花粉过敏" |
| member_routine | 日常习惯（作息、出行规律） | "爸爸通常 7:30 出门上班" |
| member_entertain | 娱乐习惯（观影、游戏、音乐） | "妈妈睡前听白噪音" |
| member_preference | 个人偏好（温度、光线、饮食） | "爸爸喜欢 24°C 制冷" |
| family | 全家共同遵守的规则/约定（**仅规则**，非家庭构成信息） | "22:00 后全屋静音"、"访客来访自动开走廊灯" |
| space | 空间环境（户型、朝向、动线） | "主卧空调出风口对床头" |
| device | 设备经验（设备使用经验） | "客厅空调制冷需 5 分钟达温" |

**subject 命名规则：**

- member_* 类型：`subject_id` **优先**绑定身份库 person_id（从 `miloco-cli identity member list` 查得，保证映射唯一）；无法确定 person_id 时退回 `subject_name` = 成员名（如"爸爸"）。多成员共同适用时 `subject_name` = "shared"，`subject_id` 留空。
- family 类型：`subject_name` 固定为 "shared"（家庭规则天然多主体）。
- space/device 类型：`subject_name` = 空间名或设备名（如"主卧"、"小米空调"），通用信息 `subject_name` = "general"。

**宠物与家庭构成归类（避免误入 family）：**

- `family` 仅指"全家共同遵守的规则/约定"，**不是**任何家庭相关信息的兜底类。
- 宠物视为一个非人成员主体：相关信息按维度归入对应 `member_*` 类型，`subject_name` = 宠物名（如"旺财"），`subject_id` 留空（宠物不在身份库）。
  - "养了一只小狗旺财" → `member_persona`，subject_name="旺财"
  - "旺财每天傍晚要遛" → `member_routine`，subject_name="旺财"
- 家庭构成/成员关系（家里几口人、谁是谁的什么人）→ `member_persona`，subject_name 为对应成员；全家整体构成事实可用 subject_name="shared"。

## ③ 和已有知识什么关系（写入规则）

先和 `list` 拿到的已有条目比对，决定怎么写：

| 情况 | 操作 |
|------|------|
| 全新知识 | `candidate-write` op `add` |
| 与候选区已有相同 | `candidate-write` op `merge(id)` |
| 与正式区已有相同 | `profile-write` op `merge(id)`（仅+证据） |
| 与已有矛盾 | `candidate-write` op `add`（独立竞争） |

- **不修改正式区内容**：对正式区只做 merge（+证据），不做 replace/add/delete。
- **"相同"基于语义**：同主体 + 同维度 + 相似结论 = 相同 → merge；结论冲突 = 矛盾 → 建独立候选条目，各自积累证据竞争，不要强行覆盖。
- **证据去重（重要）**：本流程会同时读取昨日与今日记忆，昨日数据上一轮可能已处理过。merge 前先看 `list` 返回的该条 `evidence_log`——同一观察事件（同日期 + 同来源/现象）若已在其中，**不要重复 add/merge**，否则会虚增 evidence_count / confidence。只有日志里没有的新观察才 add/merge。

## 命令格式

批量候选写入（一次提交多条，提高效率）。每个 op **必须**带 `op`（`add` / `merge`）和 `date`（观察日期 `YYYY-MM-DD`，必填，传入对应记忆的日期）：

```bash
miloco-cli home-profile candidate-write --ops '[
  {
    "op": "add",
    "date": "2026-05-29",
    "entry": {
      "type": "member_routine",
      "subject_id": "<person_id 或留空>",
      "subject_name": "爸爸",
      "content": "通常 7:30 出门上班",
      "confidence": 0.8,
      "source": "observed",
      "evidence_log": ["2026-05-29 07:31: 玄关相机识别到爸爸出门"]
    }
  }
]'
```

merge 到已有候选（仅 +证据）：`{"op": "merge", "id": "<candidate_id>", "date": "2026-05-29", "evidence_log": "2026-05-29 07:35: 玄关相机再次识别到爸爸出门", "confidence_delta": 0.1}`
merge 到正式区（仅 +证据）：`{"op":"merge","id":"<profile_id>","date":"2026-05-29","evidence_log":"2026-05-29 07:35: 玄关相机再次识别到爸爸出门"}`

> 多个条目需要操作时，优先在单次 `--ops` 中批量提交，提高效率。
