# 家庭记忆

## 背景与目标

AI 的每次对话默认是无记忆的。用户每次说"我爸爸有高血压"、"我们家有两个孩子在上小学"，下一次对话 AI 又要重新介绍。

家庭记忆（home-profile）让 Miloco 记住家庭成员的喜好、习惯、身体状况、作息、家庭规则等长期知识，并在每次 Agent 对话时注入 system prompt，让 Agent 的回应更贴合这个家庭的实际情况。

---

## 产品面

### 能做什么

- **长期知识注入**：每次 Agent turn 前，将家庭档案注入 system context，Agent 开口就带着家庭背景
- **感知闭环**：档案同时注入 Omni prompt，让 VLM 在识别和描述时具备家庭背景，感知越丰富，档案越精确
- **候选区审核**：新知识先进候选区积累证据，经审核或自动晋升后才生效，防止单次偶然观察污染长期记忆
- **用户直告优先**：用户在对话中直接告知的事实（`source=user_told`）权重最高，不受过期约束
- **被动感知触发**：用户在对话中提及家人喜好/习惯/身体状况/作息/家庭规则时，Agent 无需用户明确要求"记录"，直接静默写入档案

### 知识来源

- **感知日志（Omni 观察）**：感知流水线每次推理的 caption 被周期性摘要（`miloco-perception-digest`），提取有价值的家庭观察
- **对话主动告知**：用户或家庭成员在对话中直接告知 Agent（`source=user_told`），权重最高且不过期
- **家庭记忆 Dreaming（Agent 主动提取）**：`miloco-home-observe` 每日从感知/交互记忆提取候选知识（`miloco-home-patrol` 巡检只做设备自动化与关怀提醒，不写候选区）

### 典型场景

**场景 1 — 健康禁忌记忆**：用户对 Agent 说"我爸有高血压，饮食偏淡"。这条信息通过 `miloco-home-profile` Skill 写入正式档案，`HomeProfileService` 重新渲染 `profile.md`。之后 Agent 在推荐食谱、讨论外卖时，system prompt 中已含这条约束，Agent 自动就此调整建议，用户无需每次提醒。

**场景 2 — 作息规律积累**：感知流水线多次在晚间识别到"客厅无人、卧室有人走动"的场景。家庭记忆 Dreaming 的 Observe 步骤提取出候选知识"家庭通常 22:00 入睡"，经候选区积累证据后晋升为正式档案。此后夜间响铃时 Agent 会主动询问是否需要静音模式。

### 能力边界

- 档案注入受 token 上限约束，超出时按权重截断（权重高的知识优先保留）
- 候选区知识不直接影响 Agent 行为，需晋升到正式档案后才注入
- 档案内容质量取决于感知日志的丰富程度和用户主动告知的频率
- 档案规则/偏好是默认倾向而非硬约束；仅明确标注为底线/安全注意事项的条目优先于用户实时指令

---

## 研发面

### 架构概览（数据流图）

#### 知识写入路径

```
感知日志（Omni caption）
  → Cron: miloco-perception-digest（高频，分钟级）→ 感知记忆摘要
  → Cron: miloco-home-dreaming（每日深夜）
      Observe（miloco-home-observe）→ 从感知/交互记忆提取知识 → 候选区
      Promote（miloco-home-promote）→ 达标候选晋升 → 正式档案
      Prune（miloco-home-prune）→ 统一主体 + 清理过期数据
        → HomeProfileService.commit()
            权重排序 + token 截断 + 归档/激活
            → profile.md 写盘
```

#### 档案消费路径

```
profile.md（$MILOCO_HOME/home-profile/profile.md）
  ├─ before_prompt_build Hook（plugins/openclaw/src/hooks/prompt.ts）
  │    helpers.ts::loadHomeProfile 读取文件 → 拼档案块 → 追加到 Agent system prompt
  │
  └─ home_profile_loader.py（perception/engine/omni/home_profile_loader.py）
       → 注入 Omni prompt 动态层（感知推理时用）
```

### 核心模块

**TypeScript 侧（OpenClaw 插件，`plugins/openclaw/src/home-profile/`）**

- **`scheduler.ts`**：在 `gateway_start` Hook 中调用 OpenClaw Cron 服务的 `reconcile` 流程，以 `[miloco:home-profile]` 标签管理受管 cron 任务。插件重启后自动对齐到代码定义的最新状态，避免孤儿 cron 积累。
- **`helpers.ts`**：同步读取 `profile.md`，不存在时返回占位内容，供注入 Hook 调用。
- **`injection.ts`**：提供待回应习惯建议块（`buildPendingSuggestionBlock`），供注入 Hook 追加。

> 档案注入 Hook 本身在 `plugins/openclaw/src/hooks/prompt.ts`（不在 `home-profile/`）：唯一的 `before_prompt_build` Hook，每次 Agent turn 前读 `profile.md` 拼档案块追加到 system prompt，并注入被动记录触发规则（用户提及家庭信息时 Agent 静默写入档案）。

**HomeProfileService**（`backend/miloco/src/miloco/home_profile/service.py`）

家庭记忆的业务逻辑层，主要职责：

- **读**：`list_entries` 读取候选区和正式档案（含 `ready_to_promote` 列表）
- **写**：`candidate_write` / `profile_write` 批量执行 op（add / merge / update / delete / replace），通过文件锁串行化"读-改-写"
- **commit**：轻量过期清理 → 按权重排序 → token 截断 → 归档/激活 → 重新渲染并写入 `profile.md`
- **成员同步**：commit 时自动同步 `subject_name`（成员改名后档案随之更新）
- **remove_subject**：成员删除时联动清理档案中所有绑定该成员的条目

存储层封装在 `backend/miloco/src/miloco/home_profile/store.py`，数据文件在 `$MILOCO_HOME/home-profile/`（`candidates.json` / `profile.json` / `profile.md`），通过文件锁串行化多写。

### 关键设计决策

**候选区 / 正式档案两层**：防止单次偶然观察直接污染 Agent 的长期记忆。新知识先进候选区积累证据，多次证实后晋升。用户直接告知的知识（`source=user_told`）可直接跳入正式档案并豁免过期清理。

**权重与截断**：权重计算综合三个维度：时间衰减（不同条目类型的衰减速率不同）、来源加成（`user_told` 权重最高）、证据数量（多次观察证实的知识权重更高）。commit 时按权重降序排列再做 token 截断，确保最相关的知识排在前面。

**注入机制双路**：档案渲染为 `profile.md` 后通过两条独立路径注入：① Agent turn 前经 `before_prompt_build` Hook 追加到 system prompt；② 每次感知推理时注入 Omni prompt 动态层。这形成感知→记忆→感知的正反馈闭环：档案越丰富，VLM 识别描述越精准。

**Cron reconcile 意图**：`scheduler.ts` 不直接 add/delete cron，而是先 diff 已有受管任务与代码中 `kCronTasks` 的差异，再增/改/删对齐。插件重启、升级后自动收敛，避免孤儿 cron 积累。

### 如果我要修改家庭记忆相关功能

| 修改目标                | 去看哪个文件                                                                              |
| ----------------------- | ----------------------------------------------------------------------------------------- |
| 修改档案写入/权重逻辑   | `home_profile/service.py`（HomeProfileService）                                           |
| 修改档案存储格式        | `home_profile/store.py`                                                                   |
| 修改档案 Agent 注入方式 | `plugins/openclaw/src/hooks/prompt.ts`（注入 Hook；档案读取见 `home-profile/helpers.ts`） |
| 修改档案 Omni 注入方式  | `perception/engine/omni/home_profile_loader.py`                                           |
| 修改 cron 调度配置      | `plugins/openclaw/src/home-profile/scheduler.ts`（`kCronTasks`）                          |
| 修改家庭档案 API        | `home_profile/router.py`                                                                  |

### 家庭档案相关 API 路径

主要入口：`POST /api/home-profile/commit`（触发提交）、`GET /api/home-profile/entries`（查询档案），完整端点见 `home_profile/router.py`。

### 与其他模块的关系

**上游**：`miloco-perception-digest` 周期从感知日志提取摘要，是家庭记忆的主要知识来源之一。用户对话中 Agent 通过 `miloco-home-profile` Skill 直接写入档案。

**下游**：每次 Agent turn 前，`hooks/prompt.ts` 的 `before_prompt_build` Hook 将档案注入 Agent system context。感知推理时，`home_profile_loader.py` 将档案注入 Omni prompt。

**共享**：成员删除端点（`person/router.py` 的 delete 路由）调 `HomeProfileService.remove_subject`，联动清理档案中绑定该成员的条目。
