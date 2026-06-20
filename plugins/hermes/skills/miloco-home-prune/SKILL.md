---
name: miloco-home-prune
description: home-dreaming 定时任务的 Prune 步骤，仅由该任务调用。
metadata:
  author: miloco
  version: "2.1"
  date: 2026-06-16
  openclaw:
    requires:
      bins: ["miloco-cli"]
---

# Prune（剪枝瘦身）

统一主体(Subject)绑定与命名——既把成员的不同称呼绑定到同一 person，也把空间/设备等非成员的不同名称收敛为规范名；清理过期或超窗旧数据，并最终 Commit 持久化。

> 仅在 miloco-home-dreaming cron 流程中激活，不单独使用。

## 执行步骤

1. 执行 `miloco-cli home-profile list --target profile` 获取所有条目
2. 审视 subject 列表，识别指向同一实体的不同名称/绑定
   - 如"爸爸"/"父亲"/"老王"/"王刚"指同一人（应归并到同一 person_id）
   - 如"客厅"/"大厅"/"起居室"指同一空间
3. 如有不一致，执行 `miloco-cli home-profile reassign` 归并到目标主体
4. 执行 `miloco-cli home-profile commit` 持久化（自动执行过期清理、超窗归档、md 渲染）

## 主体归并（reassign）

reassign 有两层用途，不只是"绑人"：

1. **绑定统一 person**：把成员的不同称呼/旧 person_id 归并到同一身份库 person_id（member_* 类型，person_id 从 `miloco-cli identity member list` 查得）。
2. **统一任意 subject/分组名称**：把指向同一实体的不同名称收敛为规范名——空间（客厅/大厅/起居室）、设备等。这类无需 person_id，`to_subject_id` 留空、只统一 `to_subject_name`。

成员归并到 person_id：

```bash
miloco-cli home-profile reassign --mappings '[
  {
    "from_subject_names": ["父亲", "老王", "王刚"],
    "to_subject_id": "<person_id>",
    "to_subject_name": "王刚"
  }
]'
```

统一空间/设备等非成员名称（不绑 person，仅收敛名称）：

```bash
miloco-cli home-profile reassign --mappings '[
  {"from_subject_names": ["大厅", "起居室"], "to_subject_name": "客厅"}
]'
```

也可用 `from_subject_ids` 指定要归并的旧 person_id 列表。一次 reassign 会同时改写正式区与候选区中匹配的条目。

## 主体命名原则

- 优先选择最常用、最自然的称呼作为规范名
- 家庭场景中，亲属称谓（爸爸/妈妈）优先于姓名
- member_* 条目尽量绑定唯一 person_id，名称仅作展示/兜底
- 不确定是否为同一实体时，不合并
- 保留特殊 subject 不做合并："shared"（多主体共同）、"general"（通用信息）

## Commit 自动维护

执行 `miloco-cli home-profile commit` 时自动执行：
- 过期候选清理：超过 30 天且证据不足 3 条的候选条目被移除
- Token 预算归档：正式区超出 2000 tokens 上限时，低权重条目被归档
- 渲染持久化：将正式区内容渲染为 profile.md 供后端读取

## 注意

- 仅检查正式区的 subject 一致性
- 如果所有 subject 已统一，直接执行 commit 即可
