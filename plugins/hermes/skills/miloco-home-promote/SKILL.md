---
name: miloco-home-promote
description: home-dreaming 定时任务的 Promote 步骤，仅由该任务调用。
metadata:
  author: miloco
  version: "2.1"
  date: 2026-06-16
  openclaw:
    requires:
      bins: ["miloco-cli"]
---

# Promote（提升策展）

处理候选区中达到提升条件的知识，决定它们如何进入正式区。

> 仅在 miloco-home-dreaming cron 流程中激活，不单独使用。

## 执行步骤

1. 执行 `miloco-cli home-profile list --target both`，关注 ready_to_promote
2. 对每个 ready 条目，结合其 evidence_log 分析与正式区的关系
3. 执行 `miloco-cli home-profile profile-write` 操作

## 提升决策

| 关系 | 操作 |
|------|------|
| **全新**（无匹配） | op `add`，带 `"from": "<candidate_id>"` |
| **相似**（结论一致） | op `merge`，带 `id` + `"from": "<candidate_id>"` |
| **矛盾，新胜** | op `replace`，带 `id` + `"from": "<candidate_id>"` |
| **矛盾，旧胜** | 不操作（继续观察） |

传入 `from=<candidate_id>` 后，工具自动删除该候选条目。

```bash
miloco-cli home-profile profile-write --ops '[
  {"op": "add", "from": "<candidate_id>", "entry": {"type": "member_routine", "subject_name": "爸爸", "content": "通常 7:30 出门上班", "confidence": 0.9}}
]'
```

## 矛盾裁决参考

- evidence_count：谁被观察更多次
- last_seen：谁更近期（习惯可能变了）
- source：user_told > observed
- evidence_log：证据质量和一致性

无法判断时保守处理——让候选继续积累。

## 注意

- ready_to_promote 为空时，直接退出提升流程
- 如有多个条目需要操作，优先在单次 `--ops` 中批量提交，提高效率
