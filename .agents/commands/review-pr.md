---
description: Review a GitHub PR or local branch changes on XiaoMi/xiaomi-miloco
version: "1.6"
allowed-tools: Bash(gh pr *), Bash(gh api *), Bash(git checkout *), Bash(git symbolic-ref *), Bash(git rev-parse *), Bash(git branch -D *), Bash(git diff *), Bash(git log *), Bash(git grep *), Bash(git fetch *), Bash(git show *), Bash(md5sum *), Bash(diff *), Read, Glob, Grep
---

# review-pr

审查 XiaoMi/xiaomi-miloco 上的 GitHub PR 或本地分支变更。

## 用法

```
/review-pr              # 列出全部开放 PR
/review-pr <id>         # 审查指定 PR
/review-pr <id> --post  # 审查并把每条问题作为 comment 发回 GitHub
/review-pr <id> --ci    # 审查并更新 PR 上的 review-pr-ci comment（首次执行则新建）
/review-pr --local      # 审查本地分支变更（尚未提交为 PR）
```

---

## PR 模式（提供 PR ID 时）

### Step 1 — 未提供 ID：列出开放 PR

如果未提供 PR ID 且没有 `--local`：

```bash
gh pr list -R XiaoMi/xiaomi-miloco
```

把列表**作为 markdown 表格在回复里渲染**（列：PR ID / 标题 / 分支）——不要依赖 raw bash 输出，部分 UI 会折叠工具输出。然后询问用户要审查哪一个 PR。停止。

### Step 2 — 拉取 PR 元数据、diff 与已有评论

并行运行四条命令：

```bash
gh pr view $PR_ID -R XiaoMi/xiaomi-miloco
gh pr diff $PR_ID -R XiaoMi/xiaomi-miloco
gh api /repos/XiaoMi/xiaomi-miloco/issues/$PR_ID/comments --paginate | jq -r '.[] | "[\(.created_at[:10])] \(.user.login): \(.body)\n---"'
git fetch origin main
```

第三条拉取所有人类评论（GitHub 的 commit / 合并等系统事件落在 timeline events，不在 issue comments 里，无需额外过滤）。**review 开始前**先读这些评论——它们标出了已被讨论过的问题，避免重复发现，把注意力放到新问题或未解决的问题上。

第四条 `git fetch origin main` 是后续所有 diff / log 命令的**前置硬要求**——Step 4 / Step 7 都跟 `origin/main` 比对，本地 `main` ref 可能 stale，不 fetch 会把 main 上 fork 后新合的 commit 反向算成"PR 改动"，复制本轮 phantom deletion 误判。

### Step 3 — Checkout PR 分支

**`--ci` 模式跳过此步**——CI runner 已经把 PR 源分支 checkout 出来了，再 checkout 是多余的（且可能因为浅克隆 / remote 配置差异报错）。

其他模式（默认 / `--post`）：Step 10 cleanup 要用两样东西，**先当文本记下来**（别靠 shell 变量——跨 Bash 工具调用不保留）：① 从 `gh pr view` 输出里记 source 分支名（删本地分支用）；② 当前所在的分支名 / commit——review 完要原样切回去，**别假设你从 main 来**（reviewer 常在自己的 feature 分支上发起 review，硬切 main 会把人留错地方）：

```bash
git symbolic-ref -q --short HEAD || git rev-parse HEAD   # 输出当前分支名；detached 时输出 sha——记下这个值
```

记下输出后再 checkout：

```bash
gh pr checkout $PR_ID -R XiaoMi/xiaomi-miloco
```

确保后续文件读取看到的是**合并后状态**。否则本地文件还是 main 分支的旧代码，分析改动函数的逻辑时会大量误报。

---

## 本地模式（指定 `--local`）

### Step 2（本地）— 取本地 diff

本地模式审查范围包含两部分：相对 origin/main 的已提交变更 + 工作树未提交变更（含 staged/unstaged）。

**先**单跑 fetch（必须等它完成；网络往返通常 1-5s）：

```bash
git fetch origin main
```

`git fetch origin main` 保证 `origin/main` ref 最新（本地 `main` ref 经常 stale，直接用会算错）。

fetch 返回后，**再并行**跑下面三条（都依赖刚刷新的 `origin/main` ref，跟 fetch 并行会读到 stale 值）：

```bash
git log origin/main..HEAD --oneline
git status --short
git diff $(git merge-base origin/main HEAD)
```

- `git log origin/main..HEAD --oneline`：列出已提交但未进 PR 的 commit
- `git status --short`：列出未提交的工作树修改
- `git diff $(git merge-base origin/main HEAD)`：从 merge-base 到工作树的完整 diff，**同时包含**已提交与未提交内容。**不要用 `git diff main` / `git diff origin/main`**——本地分支没 rebase 时 main 上 fork 后新合的 commit 会被反向算成"本地删除"，造成 phantom deletion 误判（同 Step 4 的原理）

只有当 `git log` 与 `git status` **同时**没有输出时，告诉用户「当前分支相对 origin/main 没有任何变更（已提交或未提交）」并停止。

工作树本身就是最新本地变更，无需切分支。

---

## 共享步骤（两种模式都跑）

### Step 4 — 扫 diff stat 找结构性信号

扫一遍 stat 找结构性 alarm pattern（**两种模式用不同形式**）：

- **PR 模式**：`git diff --stat origin/main...HEAD` —— 三点形式，等价于 `merge-base..HEAD`，纯看 PR commit 内容（HEAD 是 `gh pr checkout` 出来的 tip，无 dirty state，三点足够）
- **本地模式**：`git diff --stat $(git merge-base origin/main HEAD)` —— 单 ref 形式（无上界 → 默认对工作树），**包含**已提交 + 未提交。本地模式开发者常有 dirty state，三点会漏；用 merge-base 这个形式跟本地 Step 2 的 diff 命令语义一致

**两种形式都必须使用 `origin/main` 而非本地 `main`**：两点 `git diff main` 会把 main 上 PR fork 之后新合的 commit **反向算成 PR 删除**，导致大段 phantom deletion 误判；用本地 `main` ref 而非 `origin/main` 又有"本地 main 比 origin/main 旧"的二次坑——本地 main 与 origin/main 之间那段 commit 会被算进"PR 改动"。PR 模式在 Step 2 fetch + Step 3 checkout 后即可跑；本地模式在 Step 2 fetch 后即可跑。注意：`gh pr diff` 输出 raw diff 不含 `--stat`，必须另跑。

这是文件清单的**第二遍读**，目标不是再列一次清单，而是找结构性 alarm pattern：

1. **删除文件 / 删除符号 / 责任委托** → "删除"和"委托"在 diff 里看起来可能一模一样（都是 `-` 占主导），但也可能差异巨大（委托可能只是 +1/-1 的一行换一行）。两者语义完全不同，按各自的**触发信号**分流，不要绑死在字符级减号数量上：
   - **真删除**（被删的概念在新代码里彻底消失，没人接盘）→ **触发信号**：`-`-only 或大量 `-`。提取删除符号列表（文件 / 类 / 函数 / 配置项 / 注册名）；C 节「反向引用扫描」必跑
   - **责任委托** → **触发信号**：preamble / 文档 / 注释里出现「**统一交给** X」「**委托** X」「**由 X 处理**」「**走 X 通道**」等措辞，X 是另一个组件 / skill / service / API，**与字符级 `-` 数量无关**（一行 inline 调用换成一行委托调用，+1/-1 也算）。X 的契约文档（SKILL.md / docstring / API spec）**必读**，按原责任清单逐项对账；C 节「委托接收方能力对账」必跑
   - 强信号：preamble / 公共文档里写了「**禁止** inline 做 Y」「**必须**走 X」这种硬约束 —— 约束力度越硬，对接收方 X 的能力对账越严，否则就是给运行时挖坑
2. **同名 / 同语义文件出现在两处**（一处生产、一处 dev/test/scripts/docs）→ 必做内容对比（`md5sum` / `diff -q`）；命中走 C 节「dev / 测试资源 vs 生产资源对齐」
3. **单文件极小改动**（1-2 行 +/- 1-2 行）→ 不能跳过。信息密度极高，往往是反向引用清理 / 边界 case 修补 / 旧符号清扫；打开看具体改了什么；若是清理旧符号 → 回到第 1 条的「真删除」分支；其余照常进 A/B/C
4. **dev / scripts / tools / docs / 知识库目录下新增可执行或可加载内容** → 不要因为"非生产"浅审。这类位置经常引用或复制生产符号 / 资源，会形成"过期引用"或"与生产分裂的副本"；走 C 节「dev / 测试资源 vs 生产资源对齐」
5. **测试 vs 生产改动比例失衡**（生产 +100 测试 +5；或测试改 100 行生产没动）→ 进 A 节「测试 vs 生产改动比例失衡」深审；前者多半漏覆盖，后者多半在测过期接口

### Step 5 — 加载完整上下文

从 diff 里提取改动文件列表。对每个**含逻辑改动**的文件（不含纯配置 / 纯文档），从本地 repo 读取**完整文件**——理解周边的类结构、新变量初始化方式、调用方长什么样、常量定义在哪。

不要跳过这一步。只看 diff 不读完整文件会大量误报。

### Step 6 — 通过读相关文件消除不确定性

读完改动文件后，如果还有任何逻辑不清楚——比如某变量的生命周期取决于调用方、某常量定义在别处、某方法依赖基类行为——**不要猜**。继续读相关文件直到问题解决：

- 改动函数的调用方 → 理解输入怎么来、状态期望是什么
- 基类 / mixin → 理解被覆写的继承行为
- 配置 / 常量文件 → 确认运行时实际值
- 改动模块的测试 → 理解作者考虑过的边界
- 类型 / dataclass 定义 → 确认字段名、默认值、约束

继续扩展上下文，直到每个逻辑顾虑都能用代码证据回答（quality bar 在 Step 7 定义）。

### Step 7 — 审查变更

基于 **Step 4 的 alarm 列表 + Step 5-6 读到的所有文件**分析 diff。

**所有问题都必须有代码证据支撑**（普适 quality bar，两种模式都遵守）：能指出具体文件 + 行号、能用代码里的实际值（常量、初始状态、调用顺序）走一遍场景把 bug 触发出来。

- 严重度（🔴/🟡/🔵）只反映问题严重程度，不是代码证据要求的强弱——🔵 同样要扛住「能不能用代码证据走通」的反问，不能用「反正只是 🔵」给自己开后门
- 扛不住反问就撤回，不要降级到 🔵 保命

**Step 4 alarm 必须在 A/B/C（下面三节）跑完前全部消化**——每条 alarm 都要映射到 A/B/C 某条具体子检查的结论（命中报问题，或用代码证据排除掉），漏处理某条 alarm 等于本轮 review 不完整。

#### Step 7.0 — 建立 ci-bot 基线（仅 PR 模式）

ci-bot 上轮 review（`<!-- review-pr-ci -->` comment）是对同一份 diff 已经做过的一轮判断，处理顺序：

1. **回到 Step 2 已读过的 ci-bot 上轮 review，换 checklist 视角对账**——Step 2 是按时间线通读建立全局印象（含 ci-bot 那条 + 其下作者 / reviewer 的回复线程，看历史讨论氛围），这里换成结构化对账视角，逐条走 ci-bot 提的问题
2. **优先验证 ci-bot 提的问题修没修**——已修复的不重复报，未修复的延续旧严重度
3. 然后再扫新问题（A/B/C 三大维度）——鼓励独立发现，包括 ci-bot 没提的角度，多发现是好事

**ci-bot 没提的发现该怎么处理**：ci-bot 没提一个问题，可能是它漏了角度，也可能是它内化了你没看到的领域约束（比如 plugin/agent ownership 这类项目假设）。**ci-bot 没提不是降级理由**——但是个「再确认一次代码证据是否真的扎实」的触发器。能用代码证据走通的就保留 🟡/🔴。

下面三大类是**正交维度**，每一轮 review 必须都跑一遍：

#### A. 代码逻辑层

常规 code review 维度（off-by-one / null 解引用 / 竞态 / 边界 / 死代码 / 复杂条件）按常识扫一遍。下面只列项目里反复有信号的**非通识检查**：

**重复代码 → 差异点往往就是 bug**
- 变更里出现多处相似逻辑（健康检查循环、错误处理分支），先 diff 它们；不一致的地方多半是漏改
- 同时建议提取共享函数，从根本上消除"两份要同步"的隐性 invariant

**资源 / 错误路径上限对账**
- 等待循环 timeout 与相关 config 必须对齐（典型踩坑：6s 等待循环 vs `stopwaitsecs=30` → 服务还没起来就上报失败）
- 至少构造一个失败场景（crash / reboot / kill -9 / 网络异常），追踪事后磁盘 / 内存残留状态——只测 happy path 等于没测清理

**状态机枚举**：引入新状态机 / 枚举时列出全部合法状态，逐一验证代码处理，不只 happy path

**测试 vs 生产改动比例失衡**（如有测试文件改动）
- 生产 +100 / 测试 +5 → 多半漏覆盖
- 测试动了 100 行而生产文件没动 → 多半在测过期接口（消费方未跟上 schema/契约变更，留了一份 stale 复刻）

#### B. 文档层（描述与代码一致性）

容易被淹没在 bug 检查里，必须显式跑一遍：

- 把 PR 描述 / commit message / PR body 拆成「N 条声明」，逐条在 diff 里找证据：
  - 描述说「做了 X」→ 代码里有没有 X？grep 关键字、读对应函数确认
  - 描述说「不再做 Y」→ 代码里 Y 是不是真的删除了？
- **必须遍历所有段落**，不能只看 Summary：
  - Summary / TL;DR：高层声明（架构原则、优先级、约束等）
  - Key changes / Implementation：每个文件 / 模块的细节描述也是声明
  - Breaking changes：每条变更也要核对
  - 最容易漏的是 Key changes 段的子 bullet——它们看起来像「说明」，实际上每条都是可验证的代码声明
- 严重程度按「修复成本」分级：
  - **commit message 与代码不一致** → 🟡（落入 git 历史后只能 rebase + force-push，且是 `git log` / `blame` / `bisect` 的永久索引）
  - **PR 描述 / PR body 与代码不一致** → 🔵（GitHub 文本框随时可编辑，成本极低；改描述或改代码二选一即可）
- 这个 pass 要带「逐条核对清单」的心态，不是「沿代码逻辑推演」

#### C. 跨层一致性

当变更跨多个层（SDK + 上层 client、driver + service、底层 util + 上层封装）：

- 同名概念（同名字段、同类回调、同种状态）在两层是否走同一套语义？
- 一层刻意「保留 / 不清空 / 不释放」某状态时，另一层是否无脑做了相反操作？
- 一层的注释 / 设计意图（如「故意保留 X 以支持自愈」）是否被上层正确理解？

不一致 → 至少 🟡，明确指出哪一层应该向哪一层对齐。

**特别检查：文档里引用的字段名 vs main 上 schema 实际定义**

PR 分支的 base 可能比 origin/main 老（作者没 rebase），所以**只看 PR 分支的文件看不出 schema 已经在 main 上改过了**。当 PR 含 SKILL.md / docstring / prompt / 配置示例等引用 schema 字段（pydantic model / dataclass / API shape）的内容时：

1. grep 文档里反引号包的字段名（比如 `` `content` ``、`` `category` ``）
2. 直接 `git show origin/main:<schema-file>` 看 main 当前定义（Step 2 已 fetch origin/main，无需再跑；不要切分支，直接读 ref，保持当前 PR 分支视图）
3. 对不上 → 至少 🟡（不是文档 nit，是 agent/调用方运行时拿不到字段，happy path 直接坏）

**最容易漏的窗口**：schema 改动 PR 与消费方 PR 不在同一个 PR——schema 已经先合 main，消费方文档还在引用旧字段名。两个 PR 各自看 ci-bot 都 LGTM，合并后才暴露不一致。review 时不要只看 PR 自身内部一致性。

**删除符号 / 模块的反向引用扫描**

diff 删了一个文件 / 类 / 函数 / 配置项 / 注册名时，**必须** `git grep <删除的符号>` 全仓扫还有谁在引用旧名字。按"反向引用是否进运行时路径"分级，严重度差很大：

1. **运行时反向引用**（被加载 / 解析 / 路由读取）：硬编码字符串、配置文件里的注册名、动态加载列表、被嵌入到 prompt / 模板里再被消费方读取的指针 —— 漏改 → **至少 🟡**（运行时拿到不存在的目标 / 自相矛盾的指引）
2. **逻辑反向引用**（其他模块的"不归我管 → 走 X"这类边界 / 路由说明）：根据 X 是被运行时读还是只供人读，按 1 或 3 处理
3. **结构性反向引用**（README / docs / 目录索引里的清单、目录树）：漏改 → 🔵（文档过时）

**强信号**：作者已经修了**一处**反向引用（diff stat 里某文件 +1/-1）→ 必扫剩下的。一处对齐意味着作者意识到要清，漏掉的就是 review 的责任。

**委托接收方能力对账**

变更里出现「X 责任**统一交给** Y」「**委托** Y 处理」「**走 Y 通道**」这类承诺转嫁时（典型场景：删掉 inline fallback chain 改为调下游 skill / service / 公共组件），必须做正向能力对账：

1. 列出**原方案**的全部责任 / 能力清单（如：旧通知降级链 = 房间 TTS 播报 + IM 推送 + 兜底 log fire_log.md）
2. 打开 Y 的契约文档（SKILL.md / docstring / API spec / 公开 README），逐项找证据：每一条原责任 Y 是否真能兑现？特别留意 Y 自己声明的「v1 简化」「待后续接入」「暂不支持 X」这类**自陈缺口**
3. 任意一条没兑现 → 至少 🟡：preamble / 上层文档给了一张 Y 兑不出来的承诺，运行时 agent / 调用方按上层指引调过去会撞墙

**与「反向引用扫描」的区别**（同一个 diff 信号触发两条不同检查）：
- **反向引用扫描**（朝过去看）：被删的概念在新代码里彻底消失了吗？还有谁在引用旧名字？回答的是「**清理是否干净**」
- **接收方能力对账**（朝未来看）：被删的责任**有人接**，新接盘者能不能兑现？回答的是「**承诺是否兑得出**」

**最容易漏的窗口**：上层 PR 把责任委托给下游 Y，但 Y 的能力补齐在另一个 PR / 后续迭代。上层 PR 单看对齐自洽（preamble 措辞与 create-task SKILL.md 一致），但 Y 的契约文档第一句就写「v1 阶段功能 X 待后续接入」—— 不打开 Y 的 SKILL.md 永远看不见。**只看 PR 改动文件不够，必须主动读未在 diff 里的接收方契约**。

**dev / 测试资源 vs 生产资源对齐**

dev / scripts / 测试目录下出现跟生产**同语义**的资源副本（任何配置 / 模板 / fixture / SOP 文档 / prompt 等）时，**必做内容对比**：

- 内容一致 → 副本应改成对生产文件的引用（symlink / 路径拼接 / 启动时读取），消除"两份要同步"的隐性 invariant
- 内容不一致 → 严重度看 dev tool 用途：若用途是"用同款流程 / 同款配置验证生产行为"，副本漂移直接让验证结论失效 → 🟡；若只是参考样例 → 🔵
- README / 注释自报「**手动同步**」「记得更新 X」「保持与 Y 一致」→ 这本身就是 invariant violation 的自陈，**直接当 🟡**，建议改成单一来源

不要把"dev / 工具不影响生产"当作浅审理由——dev 侧跟生产共享语义时，dev 侧的 staleness 直接降低验证 / debug 的可信度。

### Step 8 — 输出格式

按以下结构呈现 review，**正文与描述全部使用中文**：

```
## [PR #<id> / 本地分支 <branch>]: <title or summary>
**作者**: <author (PR mode) or git user (local mode)>
**范围**:
  - PR 模式 → `<source> → <target>`
  - 本地模式 → `<branch>`，并在括号里标明审查的内容来源：`(领先 origin/main N 个 commit)` / `(仅工作树修改)` / `(领先 origin/main N 个 commit + 工作树修改)`

### 修改方案
<在开始挑问题之前，先讲清楚作者的"修改方案"——既是给读者的 elevator pitch，也是让作者验证 reviewer 是否正确理解的契机。按 PR 体量自适应：>

- **小 PR**（1-2 文件 / 改动 < 50 行）：1-2 句话即可，例如"把 X 函数的 Y 参数从默认 None 改成强制传入，调用方对应更新"
- **中 / 大 PR**：按下面四段式展开（每段省略号位置按需填或省）：
  - **要解决的问题**：1-2 句（原状况 / 痛点 / 触发 reject 的上游错误等）
  - **整体方案**：分 N 条主线列出。主线之间是正交维度（路由分叉 / 反馈环熔断 / token 上报 等），**每条主线下必须用嵌套 bullet 展开实现逻辑**——不是「做了 X」一句话总结，而是把这条主线内部的因果链讲清楚。每个 bullet 应覆盖到：
    - 这一步在做什么（用大白话讲清动作 + 判断依据，详见下面规则 1）
    - 互斥 / 链式 / 分叉关系（两条路二选一 / 三重保险 / A 触发 B / 短路兜底 等）
    - 设计约束的「为什么」（为什么用 ipod muxer 不用 mp4 muxer / 为什么这批感知里任一帧有画面变化就整批按视频处理 / 为什么用 `finally` 兜底而非内联）
    - 与其他主线的交互（某主线的熔断依赖另一主线在 system prompt 里加的规则；某主线只是上游模块加的一行小补丁）

    **规则 1：用大白话讲操作，函数名 / 常量名只作跳转锚点，不作句子主语。** **逐条 bullet 强制执行,不是可选润色**：读者多半没读过这份代码，`_resolve_route`、`extract_samples`、`_AUDIO_ROUTE_HINT` 这种内部符号名对他们是天书——一个坐在主语位的符号名，等于把"解释这步在干嘛"的活甩给读者自己去翻代码。每个 bullet **必须以自然语言开头**，先说清"这步在做什么、依据什么、为什么"，再把对应符号以 `[name](file.py#L)` anchor 挂在句末，供想钻代码的人跳转。**最常见的失效方式**：拿符号名当 bullet 的开头小标题或句子主语（`extract_samples：图片单帧 / 视频均匀采样…`、`computeDefaultPickSet + appendAndPick 默认勾选`）——"读着像能看懂"只是因为你自己读过代码；符号名一旦坐到主语 / 标题位，没读过代码的人就接不住。
    - **反例**："`_resolve_route` 决定走 video 还是 audio，audio 用 input_audio 块"——函数名当了主语，删掉它整句就塌了，读者根本不知道按什么条件分流、分流后各自怎么处理。
    - **正例**："根据这批感知里有没有画面变化，决定是把内容当视频帧送给模型、还是只送音频流（[_resolve_route](backend/.../prompt_builder.py#L473)）；走音频那条路时，把音频数据塞进 `input_audio` 消息块再发"——先把依据和动作讲清楚，符号名退到句末当索引。
    - **写完自检（逐 bullet 跑）：把这条 bullet 里的函数名 / 文件名 / 常量名全部删掉，剩下的文字还能让没读过代码的人看懂"这步在做什么、依据什么"吗？** 删完发现主语没了 / 只剩 `X：…` 的空壳 / 动作讲不清 → 就是符号名在代替解释，回去重写成"自然语言开头 + 符号挂句末"。

    **规则 2：复杂的路由 / 数据流 / 依赖关系，画图或列表，别堆 prose。** **逐条主线强制执行,不是可选润色**：写每条主线正文**之前**，先把它归类到下面三种形状之一（命不中则按"线性叙述"走 prose）——命中前三种就**必须**先外化成图 / 表再落正文。**禁止**用"这段 prose 读着顺不顺"来决定画不画——"只在 prose 写着别扭时才画图"是本规则最常见的失效方式（机会主义执行 = 等于没执行）。主线之间的分叉、链式触发、相互依赖，用纯文字读者得在脑子里自己拼拓扑、读三遍才理顺。命中下列情形就外化成图 / 表（mermaid 或纯 ASCII 均可）：
    - 多分支路由 / 条件决策（走 video 还是 audio、什么条件下整批退化等）→ 决策表（两列：条件 → 走向）或 ASCII 流程图
    - 一条数据从入口流到出口经多步处理 / 并行分叉再汇流（如"一次检测 → 同时喂 tracker 和敏感检测 → 汇成元数据"）→ ASCII 箭头流程图（`输入 → 步骤A → 步骤B → 输出`）
    - 多主线之间的依赖网 → 依赖表（三列：主线 / 依赖谁 / 依赖什么）
    目标是让读者一眼看到拓扑，而不是读三段话在脑子里自己画。**写完自检：凡是"并行 / 分叉 / 汇流 / 链式依赖"的主线还停在纯文字，就是规则没执行——回头改成图 / 表。**

    判断 heuristic（覆盖度）：作者读完每条主线段，**能不能仅靠这段话回放出大致实现路径**？回放不出 → 展开还不够，回到代码里再补一两个关键节点。每条主线一般 4-8 个嵌套 bullet，覆盖入口判定 / 数据透传 / 编码细节 / payload shape / messages 组装 / 降级路径。
  - **关键设计原则**（如有）：总开关 / 回滚兜底 / 向后兼容等保守性设计。**用编号列表**逐条点明（不是堆 bullet 短句），每条带"为什么这么设计"的 trade-off 说明，让读者能区分各条之间的语义层次。
  - **测试覆盖**（如有显著测试改动）：**用 markdown 表格按主线对齐**——三列：主线编号 / `测试文件::TestClass` / 用例摘要（覆盖的 case 简列，多 case 用顿号串）。N 个用例时眼睛扫表格 vs 扫一段 prose 的认知负担差好几倍。

写完读一遍，问"作者读完这段会不会觉得我误读了他的方案"——会就重写。如果觉得方案有明显问题，**先在这一段中性陈述方案再到下面"问题"段批判**，不要在方案段夹带评价。

### 问题

#### 🔴 严重（提交前必须修复）
- `file.py:42` — <一句话标题，扫一眼即知是什么问题>
  - **背景**: <这段代码平时长什么样 / 在什么调用路径里 / 什么用户/客户端在什么场景会走到这里>
  - **问题**: <代码层面具体哪里坏 + 调用方在条件 Y 下会观察到坏行为 Z（带具体输入值与具体输出）>
  - **改进**: <ready-to-paste 代码块；多方案时列多个代码块 + 一句 trade-off>

#### 🟡 重要（应当修复）
- `file.py:88` — <一句话标题>
  - **背景**: <...>
  - **问题**: <...>
  - **改进**: <...>

#### 🔵 建议（可选优化）
- `file.py:10` — <一句话标题>
  - **背景**: <...>
  - **问题**: <...>
  - **改进**: <...>

### 结论
LGTM / 需要修改 — <一句话说明>

---
<sub>由 review-pr skill v<本文件 frontmatter 的 version 字段> 生成</sub>
```

某严重等级下没有问题时，整段省略。

**问题描述写作规则**（所有严重度统一适用）：

- **三段递进，不堆叠** —— 把「背景 / 问题 / 改进」拆成**独立 sub-bullets**（参见上方模板），每段独占一行缩进在文件路径下。**禁止**把多段塞进一段 prose 用加粗字 + `。` 拼接——眼睛要在密文里找加粗字才能定位每层，分层等于没做。读者扫一眼应能直接跳到自己关心的那行
- **问题字段必含可观察的坏行为** —— "哪里坏"不能停在机制层（"少了一行清理"、"删了 X 改 inline"），必须接到"调用方在条件 Y 下看到坏行为 Z"上来，并带具体值：触发输入 `传 2026-05-14T10:00:00`（而非"传 naive ISO"）+ 观察输出 `差 8 小时`（而非"epoch 不一致"）。严重度 emoji 之外，读者靠这一句决定要不要细读
- **背景字段写真实调用场景，非机械调用链** —— 不是「调用方传 ref + 状态满足条件 X」，而是「前端裸 datetime 走 `/perception/logs?after=...`，因服务器 TZ 是 Asia/Shanghai」。涉及非显然代码路径时（"X 在条件 Y 下走到 Z"），**先一两句铺垫"这段路径平时长什么样"**，再让"问题"上诊断——作者确实在代码里，但**不在你的推理状态机里**，他得跟你走一遍才能信你。判断 heuristic：写完读一遍，问"作者本人 30 秒后看到这条评论能不能复现我的推理"，复现不了就补铺垫
- **行话先解码** —— 第一次引入技术缩写时用一两词中文兜底（「naive datetime 即不带时区信息」），不要假定读者立刻能从上下文反推。reviewer-to-reviewer 行话适合互评省口水，不适合首次读 PR 评论的作者
- **多步状态切换外化成表格** —— 当问题诊断涉及多步状态变化（"启动时 X → 创建 Y → gate Z → INSERT 撞错 → exception 吞掉 → 用户视角 ..."），用 markdown 表格逐步呈现，每步独立成行。压进一段 prose 用分号 / 句号串起来，读者要在脑里维护一个状态机一步步推；表格让眼睛扫过去就完成推理，认知负担差好几倍。模板：

  | 启动时状态 | 代码做了什么 |
  |---|---|
  | `existing_tables = {"X"}` | line 91 跳过 X 的创建；line 96 用新 schema 创建 Y |
  | line 104 gate | `"Y" not in existing_tables` 为 False → AND 短路 → 跳过迁移 |
  | 第一次 INSERT | `INSERT ... VALUES (..., new_col, ...)` → "no such column" |
  | 报错被吞 | `try/except` 抓住只打 warning → 统计永久挂掉 |
- **"改进"字段必须是 ready-to-paste 代码块** —— 不是 inline 字符串 + 中文描述，不是"把 X 改成 Y"的 prose。完整 `Field(...)` / `def foo(...):` / patch hunk 展开成多行代码块，缩进、参数、字符串拼接全照原文，作者眼睛不离评论就能验证 fix 形态、IDE 里直接 copy-paste 替换。反面：「与 X 注释口径对齐：`"input/output/cached/audio/video"`」—— 字符串字面量裸塞在中文句子里，作者得自己想怎么填回 `description=...`。正面：直接贴完整的 `usage: dict[str, int] | None = Field(default=None, description=(...))`，原文什么样改后什么样
- **关键证据 inline 贴代码，不只给 file:line 链接** —— 链接是"我已经验证过"的索引，不是"读者也能验证"的证据。当问题诊断要靠跨文件对账（A 处声明 X、B 处实现 Y、X≠Y）时，把两边的关键片段 inline 贴出来，必要时加 `← 漏列 / ← 错值 / ← 这里改了` 这类标注。让读者眼睛不离评论就能完成对账。反面：「`extract_usage` 返回 5 个键」+ 一个 file:line 链接——读者必须切文件确认是不是真 5 个。正面：贴 `return {"input_tokens": ..., "audio_tokens": ..., # ← 漏列 ...}` 字面量。链接保留（看完整上下文用），但**证据本身**要在评论里
- **多个并列影响 enumerate，不要顿号 / 括号串成一句** —— 当一条 bug 对不同读者 / 路径 / 场景的影响是多条独立项时，用 1/2/3 编号每条独立成行点明角色。括号里塞顿号短语会逼读者自己拆。反面：「下游 schema 消费方（OpenAPI 文档、TS 类型生成、人工读 description）会以为没有拆解」——一句话三个独立失败模式。正面：「1. **OpenAPI schema 导出**：进 OpenAPI 文档，前端拿到的定义少 2 key  2. **TS 类型生成**：自动生成的 interface 缺 optional key  3. **人工读 description**：维护期翻字段以为没拆分」

**版本号填充**：页脚 `<version>` 占位符必须替换为本 skill 文件 frontmatter 中 `version` 字段的字面值（不要硬编码，每次跑都从 frontmatter 现读，这样 bump version 后输出自动跟上）。

**LGTM / 需要修改 判定规则：**

- 有任意 🔴 严重 → 必然「需要修改」
- 有任意 🟡 重要 → 默认「需要修改」；只有作者已明确表示「稍后处理」或「这是有意为之」时才能 LGTM
- 只有 🔵 建议 / 无问题 → 必然 LGTM

---

## 后置动作

### Step 9 — Post 评论（仅 PR 模式 + `--post` 或 `--ci`）

若同时给 `--post` 与 `--ci`，优先 `--ci`，跳过 `--post`。本地模式跳过整个 Step 9。

#### `--post` 模式：每条问题一条 comment

把全部 🔴 严重 / 🟡 重要 问题**一次性列给用户，整体询问是否 post**（不要逐条问，避免 N 次确认）。🔵 建议默认不 post，除非用户明确要求。

确认后，对每条问题用 issue comments 端点发一条（GitHub PR 的通用评论即 issue comment，可被回复、emoji 标记；如需精确挂到 diff 某行的可 resolve 线程，需 `/pulls/$PR_ID/comments` 带 `commit_id` + `path` + `line`，机制更重，默认走 issue comment）：

```bash
gh api -X POST \
  "/repos/XiaoMi/xiaomi-miloco/issues/$PR_ID/comments" \
  -f body="$(cat <<'EOF'
[Review] `<file>:<line>` — <issue>

<optional suggestion>
EOF
)" \
  | jq -r '"https://github.com/XiaoMi/xiaomi-miloco/pull/'"$PR_ID"'#issuecomment-\(.id)"'
```

#### `--ci` 模式：写审查结果到文件，由 gate.sh 代发 PR comment

沙箱禁止网络写操作（`gh api -X POST/PATCH` 被拒），改为写文件 `/tmp/review-body.md`，
由调用方 `pr-review-gate.sh` 读取后代发 PR comment。审查内容与发布解耦。

无需用户确认，直接执行。把 **Step 8 完整输出**（不要复制模板，直接复用上面已经生成的内容）前缀上一行 `<!-- review-pr-ci -->` 写入文件。heredoc 用 `'EOF'` 引号防止 shell 解释 review body 里的反引号 / `$`。

```bash
cat > /tmp/review-body.md <<'EOF'
<!-- review-pr-ci -->
<这里粘贴 Step 8 的完整输出，不要重写一遍>
EOF
```

### Step 10 — Cleanup（主动执行，仅默认 / `--post` 模式）

**`--ci` 模式跳过此步**——Step 3 没切分支，Step 10 也无需还原；CI runner 是 ephemeral 的，job 结束自动销毁，再跑 cleanup 的 `git checkout` 还原反而可能因为 detached HEAD / 浅克隆等情况报错。

**本地模式跳过此步**——本地分支是用户自己创建的，不应清理。

默认 / `--post` 模式下，**与 Step 9 的 post 是否发生完全解耦——无条件执行：**

- 默认模式（无 Step 9）→ 直接进 Step 10
- `--post` 模式：用户拒绝 post / 没回应 / 确认 post → 都进 Step 10

还原到 review 前的位置——切回 Step 3 记下的**原始分支 / sha**，**不是无脑 `git checkout main`**（reviewer 可能本就在别的分支上，硬切 main 会丢掉他的工作上下文）；再删掉 `gh pr checkout` 建出来的本地 PR 分支（`<source-branch-name>` 同样是 Step 3 记下的）：

```bash
git checkout <Step 3 记下的原始分支/sha>
git branch -D <source-branch-name> 2>/dev/null || true   # 没有对应本地分支（如用 detached 方式 checkout）时删不到，|| true 跳过
```

如需回到 PR 分支继续验证 review 提到的问题，再 `gh pr checkout $PR_ID -R XiaoMi/xiaomi-miloco` 一次即可——比维护一堆陈旧本地分支成本低。
