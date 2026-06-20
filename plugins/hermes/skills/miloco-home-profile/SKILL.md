---
name: miloco-home-profile
description: 家庭档案管理 — 当用户提到自己或家人的喜好、兴趣、习惯、身体状况、作息规律、家庭规则时激活，无论用户是否明确要求"记录"。也在用户修正已有记录、要求删除、或查询家庭档案时激活。
metadata:
  author: miloco
  version: "5.1"
  date: 2026-06-12
  openclaw:
    requires:
      bins: ["miloco-cli"]
---

# 家庭档案管理

记录家庭档案的目的不是"存档"，而是让 miloco 越来越懂这家人——后续控制设备、给建议、写通知都会参考档案，贴合成员的偏好与习惯。所以**记得准、记得全，比记得多更重要**：脏数据（重复、过期、张冠李戴）会让后续操作变笨。

## 何时激活

**主动场景：**
- 用户明确告知家庭信息："我们家 3 室 1 厅"、"爸爸不喜欢灯太亮"、"晚上 10 点后不要播报"
- 用户查询家庭档案："查看家庭档案"、"我爸的作息是什么"
- 用户要求修改/删除记录

**被动场景（对话中自然提及，无需用户说"记录"）：**
- 用户提到自己的喜好/兴趣："我喜欢喝咖啡"、"我不喜欢太甜的"、"我最近在看《三体》"
- 用户提到家人的信息："我妈对花粉过敏"、"我爸每天早起跑步"、"弟弟喜欢打篮球"
- 用户提到生活习惯/规律："我一般 11 点睡"、"周末我们全家会一起吃早餐"

被动场景下，先完成用户当前请求的主要回答，然后静默写入档案（无需向用户确认）。

## 命令使用（必须遵守）

操作家庭档案使用 `miloco-cli home-profile` 命令，不要直接编辑 profile.md 文件：
- `miloco-cli home-profile list --target profile` — 查看当前档案
- `miloco-cli home-profile profile-write --ops '...' --user-edit` — 写入/更新/删除条目
- `miloco-cli home-profile commit` — 提交保存

## 工作流

1. **先拉全量档案（任何增删查改前必做）**：执行 `miloco-cli home-profile list --target profile`。
   > 系统提示词里注入的家庭档案是**摘要片段**——不含条目 id、且可能因篇幅截断，**不能据它操作**：写入前不看全量会写重复 / 改错条目（merge / replace / delete 都要 id），查询时只看它会漏答。每次都重新拉、不要复用上一轮输出（档案随时可能被其它 session 更新）。
2. **判断意图**：
   - 查询 → 基于全量档案返回相关内容
   - 更新（含被动记录）→ 进入写入流程
3. **执行操作**（执行 `miloco-cli home-profile profile-write` 并带 `--user-edit`）：
   - 全新信息 → op `add`，传入 entry
   - 补充确认已有 → op `merge`，传入 id
   - 修正/更新已有 → op `replace`，传入 id + entry
   - 要求删除 → op `delete`，传入 id
4. **提交**：执行 `miloco-cli home-profile commit` 保存

```bash
miloco-cli home-profile profile-write --user-edit --ops '[
  {"op": "add", "entry": {"type": "member_preference", "subject_id": "<person_id 或留空>", "subject_name": "爸爸", "content": "喜欢 24°C 制冷", "evidence_log": ["2026-05-29 20:10: 用户告知爸爸偏好24度"]}}
]'
```

## 条目格式

entry 字段：
- type: 分类（类型枚举见下方）
- subject_id: member_* 类型优先绑定身份库 person_id（从 `miloco-cli identity member list` 查得）；其它类型留空
- subject_name: 关联主体显示名/兜底
  - member_* → 成员名（如"爸爸"），多成员共同适用时 "shared"
  - family → 固定 "shared"
  - space/device → 空间名/设备名（如"主卧"、"小米空调"），通用信息 "general"
- content: 一句话描述
- confidence: 1.0（用户直接告知，带 `--user-edit` 时自动设定）
- source: "user_told"（带 `--user-edit` 时自动设定）
- evidence_log: ["YYYY-MM-DD HH:mm: 用户告知 <原话摘要>"]

### type 分类

| type | 含义 | 示例 |
|------|------|------|
| member_persona | 成员画像（家庭角色、身份、外貌） | "爸爸是家里的主厨" |
| member_health | 体质健康（过敏、禁忌、慢性病） | "妈妈对花粉过敏" |
| member_routine | 日常习惯（作息、出行规律） | "爸爸通常 7:30 出门上班" |
| member_entertain | 娱乐习惯（观影、游戏、音乐） | "妈妈睡前听白噪音" |
| member_preference | 个人偏好（温度、光线、饮食） | "爸爸喜欢 24°C 制冷" |
| family | 全家共同遵守的规则/约定（**仅规则**，非家庭构成信息） | "22:00 后全屋静音"、"访客来访自动开走廊灯" |
| space | 空间环境（户型、朝向、动线） | "主卧空调出风口对床头" |
| device | 设备信息（常用功能、操作习惯） | "客厅空调制冷需 5 分钟达温" |

### subject 命名规则

- **member_* 类型**：`subject_id` = 身份库 person_id（优先）；`subject_name` = 成员名（如"爸爸"），多成员共同适用时 `subject_name` = `"shared"` 且 `subject_id` 留空
- **family 类型**：`subject_name` 固定为 `"shared"`（家庭规则天然多主体）
- **space/device 类型**：`subject_name` = 空间名或设备名（如"主卧"、"小米空调"），通用信息 `subject_name` = `"general"`

### 宠物与家庭构成归类（避免误入 family）

- `family` 仅指"全家共同遵守的规则/约定"，**不是**任何家庭相关信息的兜底类。
- 宠物视为一个非人成员主体：相关信息按维度归入对应 `member_*` 类型，`subject_name` = 宠物名（如"旺财"），`subject_id` 留空（宠物不在身份库）。
  - "养了一只小狗旺财" → `member_persona`，subject_name="旺财"
  - "旺财每天傍晚要遛" → `member_routine`，subject_name="旺财"
- 家庭构成/成员关系（家里几口人、谁是谁的什么人）→ `member_persona`，subject_name 为对应成员；全家整体构成事实可用 subject_name="shared"。

## 写入原则

- **只写持久知识**：习惯、偏好、规则、空间特征
- **不写临时状态**："爸爸今天还没回来"不写，"爸爸通常 18:00 回家"写
- **精简表达**：每条 content 简洁明了，不加冗余修饰
- **安全过滤**：不记录密码、证件号、银行卡、API Key 等敏感信息