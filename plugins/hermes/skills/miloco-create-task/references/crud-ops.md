# miloco-create-task · CRUD 操作（非 create 路径）

> 任务的查询、日志、启停、修改都从任务视角进入，**不直接操作底层 rule / cron / record**。所有反查走 `miloco-cli task get/list`，record 改字段走 `task record update`，不再扫 `rule list | jq` 过滤前缀 / `cron list` 过滤前缀。
>
> miloco-create-task 主线只做 op 路由:消息进来判 op,命中本文件四个章节之一就跳过来执行;命中 create 走主线 SKILL.md。delete 不在本 skill 内 → miloco-terminate-task。

## 通用前置

任何 CRUD 操作开始前先确认 task 存在:

1. 解析用户消息里的 task 标识(task_id 或自然语言名称)
2. 跑 `miloco-cli task list --pretty` 拿全集
3. 名称模糊匹配:按返回的 `description` 字段过滤;多个候选 → 让用户选
4. 找不到:答「未找到 X 任务」+ 列现有任务清单,**不硬建空对象**

## CRUD.1 · list（任务列表）

**输入**:「我有哪些任务」「查看任务列表」「最近的任务」。

**步骤**:

```bash
miloco-cli task list --pretty
```

返回每个 task 的 task_id / description / status / rule_briefs / links。

**回复**:≤30 字简述 + Markdown 表(task_id / 概要 / 状态)。

## CRUD.2 · logs（触发日志）

**输入**:「X 任务今天触发过几次」「看 X 的触发记录」「最近 1 小时 X 触发」。

**步骤**:

1. 解析 task_id(通用前置)
2. 拿 rule_id 列表:`miloco-cli task get <task_id>` → `data.links` 过滤 `kind=="rule"` 取 `ref`
3. 对每个 rule_id:`miloco-cli rule logs --rule <rule_id> --since <window>`(默认 24h)
4. 汇总条数、kind 分布、时间分布

**回复**:「X 任务 [窗口] 触发 N 次(成功 a / 失败 b);[时间分布]」≤80 字。

## CRUD.3 · disable / enable（启停）

**输入**:「暂停喝水任务」「启用 X 任务」「先停一下房间监控」。

**步骤**:

```bash
miloco-cli task disable <task_id>   # 暂停
# 或
miloco-cli task enable <task_id>    # 启用
```

返回结构:

```jsonc
{
  "task_id": "...",
  "status": "paused" | "active",
  "backend_synced": {
    "meta_status": "ok" | "noop",
    "rules": [{"rule_id": "...", "result": "ok"}]
  },
  "agent_pending": [
    {"kind": "cron", "ref": "...", "action": "disable"}
    // 只可能是 cron kind
  ]
}
```

**跑 agent_pending**:

- 顺序跑(不并行)
- 任一项失败**不中止后续**——继续跑剩余项
- 全部跑完后组装 `partial_failures[]` 一次性回话告知用户

**回复**:

- 全部成功:「X 任务已暂停 / 启用」(≤20 字)
- 有失败:补一句「还有 N 步未完成需人工核对」

`backend_synced.meta_status="noop"` 表示 task 已经是目标状态(重复 disable 已 paused 的 task),跑 agent_pending 时把已 disabled 的 cron 当幂等处理。

## CRUD.4 · update（修改）

**输入**:「把喝水改成 10 杯」「条件改成阳台有人」「触发时间改到早上 8 点」。

**步骤**:

1. 解析 task_id 和改动语义
2. **改底层载体(必跑)** —— 按改动维度逐一改 rule / cron / memory:

| 改什么 | 改哪 | 怎么做 |
|---|---|---|
| rule 条件 / 动作 / 防抖 | rule | `miloco-cli rule update <rule_id> ...`（`rule_id` 从 `task get` 的 links[] 拿） |
| 持续时长门槛 | rule | `miloco-cli rule update <rule_id> --duration-seconds <N>`（单位换算见 SKILL.md §Rule.duration_seconds）；desc 含字面分钟/小时数时同步改 |
| 触发时间 / cron 表达式 | schedule | cron remove + cron add（cron 无 update API）；jobId 从 `task get` 的 links[] 拿；新 jobId 紧跟 `task link --kind cron --ref <new_jobId>` 重新挂 |
| 目标值 / 单位 / window / recurring_pattern / expires_at | record | `miloco-cli task record update <task_id> --patch '{...}'`（白名单按 kind：progress=target/unit/window/recurring_pattern/expires_at；duration=target_minutes/recurring_pattern/expires_at；event=recurring_pattern/expires_at）|

跨件套修改时**按 record → schedule → rule 顺序**逐一更新；任一步失败则停止后续。

3. **附加同步 `task.description`(可能跑)** —— 步骤 2 完成后,**额外**判断是否要调:

```bash
miloco-cli task update <task_id> --description "<新>"
```

`task.description` 是 task 整体语义快照(dedupe 主信号 + list 显示用),跟底层载体是两份独立写入。以下任一变化即视为 task 整体意图变更,**必须**额外跑 task update:

- 任何数字 / 阈值变化("喝水 8 杯改 10 杯"、"久坐 30 分改 50 分")
- 时间点变化("早 7 点改早 8 点")
- 触发条件主体变化("客厅改卧室"、"任意人改具体姓名")
- 动作类型变化("通知改成开灯")

仅当所有变化都是"内部参数微调不改用户视角效果"时跳过 task update(罕见)。

> 例：「喝水 8 杯改 10 杯」→ 步骤 2 调 `miloco-cli task record update drink_water --patch '{"target":10}'`，步骤 3 调 `task update --description "每天喝 10 杯水"`。两步**都要跑**，不是二选一。

**回复**:「X 任务已更新:[改动摘要]」≤40 字。

## CRUD 失败处理

| 异常 | 处理 |
|---|---|
| 找不到 task_id | 列现有任务清单让用户选;不创建 |
| `task get` 404 | 同上 |
| update 跨件套中途失败 | 已成功的部分**不回滚**;追加 audit `partial` 提示用户人工核对 |
| disable agent_pending 部分失败 | 组装 `partial_failures[]` 回话标 partial |
