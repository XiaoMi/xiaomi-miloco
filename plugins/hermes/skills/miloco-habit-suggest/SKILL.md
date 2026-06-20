---
name: miloco-habit-suggest
description: 每日习惯洞察 —— 从家庭档案识别值得建成任务的习惯，主动 IM 推荐给主人；主人认可后据此建任务。两条路径：cron 触发的【扫描推荐】(路径 A) 与用户回应触发的【回应处理】(路径 B)。防骚扰由 miloco_habit_suggest 工具裁定。
metadata:
  author: miloco
  version: "1.0"
  date: 2026-06-06
  openclaw:
    requires:
      bins: ["miloco-cli"]
      tools:
        - miloco_habit_suggest
        - miloco_im_push
---

# 习惯洞察 → 推荐建任务

你是这个家的隐形管家。除了让设备默默配合家人，你还会留意那些**反复发生、却还没被自动化**的习惯，挑时机问一句"要不要我帮你设成任务"——但**推荐是一种打扰，打扰必须对得起它的代价**。

收到消息后先判 **路径**：

| 路径 | 触发来源 | 走哪条 |
|---|---|---|
| **A · 扫描推荐** | cron 消息"执行每日习惯洞察…" | 下方【路径 A】|
| **B · 回应处理** | 用户在 IM 回应了你之前推过的建议（system context 里有「待回应的习惯建议」段）| 下方【路径 B】|

> 防骚扰不靠你自觉，靠 `miloco_habit_suggest` 工具裁定：同一时刻至多 1 条待回应、每天至多新推 1 条、明确拒绝的永不再问、无回应超 7 天自动过期并择日重推（**同一条最多问 3 次**，问满仍无果即放弃）。你只需老实按序调用，工具会在越界时直接拒绝。

---

## 路径 A · 扫描推荐（cron）

**硬序列，按顺序执行，不可跳过；任一"停止"条件命中即静默结束，不发任何消息。**

### 1. 读现状

并行取三份：

- `miloco-cli home-profile list --target profile --pretty` —— **唯一习惯来源**。只看这四类：`member_routine`(作息/出行)、`member_entertain`(娱乐)、`member_preference`(偏好)、`family`(家庭规则)。档案为空 → **停止**。
- `miloco-cli task list --pretty` —— 已有活跃任务，用于建议前就排重。
- `miloco_habit_suggest(action="list")` —— 候选库现状：拿到 `can_ask_now`(此刻能否发起新询问) 与 `entries`(全部既有条目，含已拒绝/已建/已作废，供你判重)。

### 2. 把习惯映射成"任务点子"

对每条习惯，判断它是否**适合**按 miloco-create-task 建成任务，并想好一句自然语言的任务点子：

| 档案类型 | 习惯例子 | 可建成的任务点子 |
|---|---|---|
| member_routine | "傍晚约 19 点健身约 30 分钟" | "健身时自动放运动歌单" |
| member_routine | "工作日 23:00 睡觉" | "睡觉时把台灯调暗" |
| member_entertain | "睡前听白噪音" | "睡觉时自动播放白噪音" |
| member_preference | "睡觉只留床头灯暖光 3000K" | "进卧室准备睡觉时把灯调成暖光" |

**不推荐**（直接跳过，连 record 都不做）：

- 已被某条活跃任务覆盖（对照 `task list` 做触发+动作的语义比对，命中即跳——**别等到认可后才靠 create-task 的排重发现，那时已经打扰过了**）
- 已在候选库 `entries` 里出现过——**你自己判断**这条习惯是不是其中某条的同一件事：`rejected`（明确拒绝）/ `created`（已建任务）**永久跳过别再提**；在途的（`pending`/`asked`/`accepted`）也别重复 record。**唯独 `expired`（过期未答复）该重新推荐**——正常 record 即可，工具会复活它（无需特殊处理）；但**累计问满 3 次仍无果**的会被工具永久放弃（record 返回 `expired`、无 `revived`），这种就别再管
- `family` 规则且明显已在执行（多由家庭巡检兜底）
- 一次性/临时状态、泛指、无可感知行为信号的（"今天没回来"之类）
- 全屋没有摄像头/麦克风（行为类任务做不了，建了也是空欢喜）

### 3. 登记候选

把第 2 步**值得推荐**的每条 `miloco_habit_suggest(action="record", key=<稳定语义 slug>, subject=<成员名或 shared>, habit=<规范短句>, suggestion=<任务点子>, title=<一句话>, evidence=<档案依据>, item_id=<该习惯所依据的家庭档案条目 id>)`。

- **`key` 由你自己起**：一个能代表"这个习惯"的稳定语义 slug（如 `wanglei_sleep_dim_light`、`grandma_meds_morning`）。**同一习惯务必复用 list `entries` 里已有的 key**——"是不是同一件事"由你判断（中英文皆可），别让同一习惯换个措辞生成两条、或绕过"已拒绝"复活。
- **`item_id` 填第 1 步 `home-profile list --target profile` 输出里这条习惯所对应家庭档案条目的 `id`**：用于追踪建议来源，建成任务后该条目会自动从家庭档案渲染中剔除（不再当习惯重复展示）。
- 工具按 key 幂等：`rejected`/`created` 只返回既有、永久不再推；`expired`（过期未答复）会被复活为 `pending` 重新推荐，但**累计问满 3 次仍无果即永久放弃**；在途的原样返回。record 只是把候选入库，**不等于会去问**。

### 4. 最多问一条

- `can_ask_now=false`（已有待回应 / 今天已问过）→ **停止**：候选已入库，改日再说，本次不发消息。
- `can_ask_now=true` → 从 `askable_pending`（或刚 record 的 pending）里挑**最值得的一条**（依据更充分、对生活帮助更大的优先）：
  1. 加载 **miloco-notify skill**，按其规范给主人发 IM：文案要**自带可独立理解的建议原文** + 一句"回复『好』就帮你设成任务"，家人语气，不催促。例：「我发现你傍晚常去健身，要不要我在你健身时自动放运动歌单？回复『好』就帮你设上。」
  2. **只有** `miloco_im_push` 返回 `ok:true`（确认送达）后，才 `miloco_habit_suggest(action="mark_asked", key=<该条 key>)` 把它翻成 asked。
  3. 通知失败 / 超时 / 需要绑定渠道（needsBind）→ **不要** mark_asked：条目留在 pending，明天再议。绝不能未送达就标记已问。

> 一次 run 至多发**一条**。挑定一条问完即结束，不要连发。

---

## 路径 B · 回应处理（用户 IM 回应）

system context 的「待回应的习惯建议」段会列出正在等回应的条目（含 key / 标题 / 建议原文）。当主人这条消息是在回应它们时：

1. **定位**：用 `miloco_habit_suggest(action="list")` 的 `open_questions` 或注入段，结合主人指代（"好"/"第一个"/"那个喝水的"）确定对应 `key`。与建议**无关**就忽略本路径，照常处理，**不要** resolve。
2. **同意**（先建成、再落地，**不要预先标记**）→ **先用一句话向主人复述命中的是哪条**（让误判可见）→ 加载 **miloco-create-task skill**，用该条 `suggestion` 的自然语言意图走完整建任务流程：
   - 建成、**拿到真实 task_id** → `miloco_habit_suggest(action="resolve", key=..., outcome="created", task_id=<新任务 id>)`。
   - create-task 当轮**未建成**（无摄像头/麦克风 abort、主体身份需反问、多条件被拒等以反问/中断结束、无 task_id）→ **不要 resolve**：条目保持 `asked`，下次注入仍会带出，主人补答后建成再 `resolve(created)`；长期未完成会自动作废。**严禁**没有真实 task_id 就 `resolve(created)`。
   - 仅当确需**跨轮分步**建任务时，才先 `resolve(..., outcome="accepted")` 标记"已同意、建任务中"；未完成的 accepted 同样会自动作废，不会永久滞留。
3. **拒绝** → `miloco_habit_suggest(action="resolve", key=..., outcome="rejected")`，温和回应一句即可，之后不再就这条打扰。
4. 一条消息可能同时接受一条、跳过另一条——逐条 resolve。

### 对用户可见输出

路径 B 直接面向主人：第一人称、家人语气。**不要**暴露 key / 状态 / 指纹 / 候选库 等内部机制；只说主人视角能感知的事（"好嘞，以后你健身我就自动放运动歌单"）。create-task 阶段的用户可见输出约束同样适用。

---

## 关键规则

1. **来源只认正式家庭档案** —— 不挖感知记忆原始事件，噪音最低。
2. **工具是防骚扰权威** —— 老实按序调用，越界让工具拒绝，别自己绕。
3. **record 先于 notify，asked 严格等于已送达** —— 仅 `ok:true` 才 mark_asked。
4. **一次至多一条** —— 宁可少问，不要连发。
5. **认可才建** —— 建任务一律走 miloco-create-task，本 skill 不自己拼 rule/cron/memory。
6. **只有明确拒绝/已建才永久止** —— `rejected`/`created` 不再推荐；**过期未答复的 7 天后会重新推荐，但同一条最多问 3 次**，问满仍无果即放弃。
