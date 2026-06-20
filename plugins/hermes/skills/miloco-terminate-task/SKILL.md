---
name: miloco-terminate-task
description: 任务终止 —— 用户主动取消或任务到期时触发，调 `miloco-cli task delete --reason X` 一笔走完 backend（写 task_terminate_log + 删 rule + 删 task，FK CASCADE 清 task_link 与 task_record_* 主表/子表），顺序跑返回的 agent_pending 清 cron。
metadata:
  author: miloco
  version: "3.0"
  date: "2026-06-10"
  openclaw:
    requires:
      bins: ["miloco-cli"]
---

# miloco-terminate-task

任务终止 skill。整个流程 3 步：

0. **（仅 user_cancel）反问用户确认永久删除**——执行 `task delete` 前先确认；自动触发场景（`termination_schedule` / `archive_maintenance` / `completed`）跳过此步直接执行。
1. 调 `miloco-cli task delete <task_id> --reason {completed|expired|abandoned}`——backend 一笔事务写 `task_terminate_log`（30 天滚动审计）+ 按 task_link 反查删 rule + 删 task（FK CASCADE 同步清 task_link / task_record_* 主表/子表 / event 子表 / duration_session 子表）。
2. 按响应的 `agent_pending` 顺序跑 OpenClaw cron tool `action=remove`（**仅 `kind:'cron'` 一种**，无其他类型）。

## 何时激活

三种触发场景，prompt 中均含 `调用 skill: miloco-terminate-task` 和 task_id：

| 触发源 | 表现 |
|---|---|
| 用户主动取消 | 用户说"不喝了 / 不记药了 / 取消那个监控" → Layer 1 路由 → 调 skill |
| 重复任务的 termination at-schedule 到点 | recurring 任务带 `valid_until` 时建的一次性 schedule 自动调 |
| archive_maintenance 兜底 | 每天 3 点扫到 `valid_until` 已过期但任务仍存活时补一次 terminate |

prompt 应含 `trigger_source` 字段（user_cancel / termination_schedule / archive_maintenance），决定 `--reason` 入参。

## 第零步 · 用户主动取消时反问确认（仅 user_cancel）

`trigger_source` 为 `user_cancel` 时，**不直接 `task delete`**——先调
`miloco-cli task get <task_id>` 拿当前 description（**禁止凭印象拼任务名**），
然后反问用户：

```
确认永久删除「<description 原文>」吗？删除后所有历史记录（每天的喝水次数 /
累计时长 / 触发日志）都会一并清空且无法恢复。

如果只是暂时不想接收提醒，回「暂停」我帮你停用任务、历史记录保留；想彻底
删除回「确定」。
```

按用户答复分支（不要硬解析，按语义路由）：

| 用户答复语义 | 动作 |
|---|---|
| "确定 / 确认 / 删了 / 嗯 / 好 / 是的" | 继续第一步 `task delete` |
| "暂停 / 先停一下 / 不要提醒了" | 调 `miloco-cli task disable <task_id>` 改 status=paused；历史记录保留，rule disabled / cron disable pending；不删，`status=disabled_instead` 输出报告 |
| "算了 / 再想想 / 取消" | 不动任何东西，`status=user_canceled` 输出报告，回话"好的，保留任务" |

**其他 trigger_source（termination_schedule / archive_maintenance / completed）跳过本步**——系统自动触发不需要用户介入。

> **反例**（禁踩）：
> - ❌ 反问时贴 `task_id` 给用户看（`drink_water`）——按 SKILL §术语黑名单 task_id / 内部 id 禁外漏
> - ❌ 反问时列 "rule_id / cron jobId 都会清" 等技术细节——用户视角只关心"历史记录会消失"
> - ❌ user_cancel 不反问直接 delete——历史数据是有价值的，误删不可恢复

## 第一步 · 决定 reason

按触发源映射 `--reason`：

| trigger_source | reason |
|---|---|
| `user_cancel`（用户主动取消，第零步已确认）| `abandoned` |
| `termination_schedule`（到期触发）| `expired` |
| `archive_maintenance`（兜底触发）| `expired` |
| 任务自然达标后调（completed）| `completed` |

## 第二步 · 调 `miloco-cli task delete`

```bash
miloco-cli task delete <task_id> --reason <abandoned|expired|completed>
```

backend 一笔事务：

1. 写 `task_terminate_log`（kind / reason / description / final_snapshot / terminated_at）
2. 滚动清 30 天外的 `task_terminate_log` 行
3. 按 task_link 反查删 rule
4. `DELETE FROM task WHERE task_id=?` —— FK CASCADE 同步清 task_link 全部行 + `task_record_*` 主表 + duration_session / event_entry 子表

返回结构：

```jsonc
{
  "task_id": "...",
  "backend_synced": {
    "rules_deleted": ["..."],
    "task_link_rows_deleted": <N>
  },
  "agent_pending": [
    { "kind": "cron", "ref": "<jobId>", "action": "remove" }
    // 只可能是 cron kind
  ]
}
```

**404 处理**：返回 `code=2001 task_not_found` 视同 noop（任务已删），跳过第三步。

## 第三步 · 跑 agent_pending

逐项执行 `agent_pending[]`，每条都是 `{kind: "cron", ref: "<jobId>", action: "remove"}`：

- 调 OpenClaw cron `action=remove`，jobId = `ref`
- 顺序跑、不并行
- 任一项失败**不中止后续**——继续跑剩余项
- 全部跑完后组装 `operations[]` + `errors[]` 一次性回话

## 输出格式

```jsonc
{
  "skill": "miloco-terminate-task",
  "task_id": "drink_water",
  "trigger_source": "user_cancel | termination_schedule | archive_maintenance",
  "status": "completed | partial | failed | disabled_instead | user_canceled",
  "final_reason": "abandoned | expired | completed | null",
  "operations": [
    { "type": "task.delete", "rules_deleted": ["rule_xyz"], "task_link_rows_deleted": 2, "agent_pending_count": 2, "ok": true },
    { "type": "cron.remove", "jobId": "job_aaa", "ok": true },
    { "type": "cron.remove", "jobId": "job_bbb", "ok": true }
  ],
  "errors": []
}
```

`status` 含义：

- `completed`：所有步骤成功
- `partial`：典型场景——`task delete` 成功但第三步某条 cron remove 失败（jobId 已不存在等）
- `failed`：`task delete` 调用本身失败（5xx 等）
- `disabled_instead`：用户在第零步选了"暂停"，走 `task disable`，未 delete；operations 只有 `task.disable` 一项
- `user_canceled`：用户在第零步选了"算了"，无任何 backend 调用

## 验收标准

- `task delete` 返回 `code=0`（backend 部分已 commit）或 `code=2001`（task_not_found，视同已删 noop）
- agent_pending 列表逐项执行，失败的单独记 `errors[]` 不中止后续
- 输出报告 `status=completed`

## 失败处理

| 异常 | 处理 |
|---|---|
| `task delete` 返回 404 / `code=2001` | 任务不存在，视同已删 noop，跳过第三步，`status=completed`、`operations=[]` |
| `task delete` 5xx | `status=failed`；记错误，不继续 |
| 第三步单条 cron remove 失败 | 记到 `errors[]`，**继续后续**，不回滚 |
| 报告 `status=partial / failed` | 调 `system.failureAlert` 让 archive_maintenance 第二天兜底再扫 |

## 关键约定

1. `task delete` 单笔事务覆盖审计写入 + rule 删 + task 删 + record / task_link CASCADE 清，agent 不要拆步骤手工做
2. 不要扫 cron list / rule list 按前缀过滤反查——`agent_pending` 已是权威清单
3. `agent_pending` 仅 `kind:'cron'`，没有其他 kind；若遇其他 kind 视作 backend 异常 `failed`

## 示例 · 用户说「不喝水了」

输入（Layer 1 路由后）：

```
调用 skill: miloco-terminate-task

trigger_source=user_cancel
task_id=drink_water
```

执行（用户答"确定"分支）：

1. **第零步反问**：调 `miloco-cli task get drink_water` 拿 description="每天喝 8 杯水"，回话：
   > 确认永久删除「每天喝 8 杯水」吗？删除后历史记录（每天的喝水次数）会一并清空且无法恢复。如果只是暂时不想接收提醒，回「暂停」；想彻底删除回「确定」。
2. 等用户答复（**本 turn 结束**）。下个 turn 用户回 "确定" → 继续第一/二/三步。
3. trigger_source=user_cancel → `--reason abandoned`
4. 调 `miloco-cli task delete drink_water --reason abandoned`：
   ```jsonc
   {
     "task_id": "drink_water",
     "backend_synced": {
       "rules_deleted": ["rule_xyz"],
       "task_link_rows_deleted": 2
     },
     "agent_pending": [
       { "kind": "cron", "ref": "job_aaa", "action": "remove" },
       { "kind": "cron", "ref": "job_bbb", "action": "remove" }
     ]
   }
   ```
5. 顺序跑 `agent_pending`：对 `job_aaa` / `job_bbb` 调 cron `action=remove`
6. 输出 `status=completed` 报告

### 分支：用户答"暂停"

第零步反问后用户答"暂停" / "先停一下"：

1. 调 `miloco-cli task disable drink_water` → backend 把 task.status 改 paused，rule disabled，返回 `agent_pending` 让 agent 跑 cron disable
2. 顺序跑 `agent_pending` 的 cron `action=disable`（**不是 remove**）
3. 输出 `status=disabled_instead`、`operations=[{type:"task.disable", ...}, {type:"cron.disable", ...}]`
4. 回话"好的，已经暂停喝水任务，历史记录都还在。想恢复说一声。"

### 分支：用户答"算了"

第零步反问后用户答"算了" / "再想想"：

1. 不调任何 backend
2. 输出 `status=user_canceled`、`operations=[]`、`final_reason=null`
3. 回话"好的，保留任务。"
