---
name: miloco-devices
description: 查询与控制米家智能家居设备。查询能力包括设备开关状态、运行状态、电量、设定温度、当前温湿度、PM2.5 等环境与设备数据；控制能力包括开关灯、调节空调温度/模式/风速、控制窗帘开合、启动或停止扫地机器人、开关摄像头等设备操作；场景能力包括触发已有米家场景，如回家、离家、睡眠等智能场景；以及刷新设备列表缓存。
metadata:
  author: miloco
  version: "1.7"
  date: "2026-06-14"
  openclaw:
    requires:
      bins: ["miloco-cli"]
---

# miloco-devices

处理米家智能家居设备交互，通过 `miloco-cli` 查询、控制、米家设备；触发米家场景。

## 何时激活

| 意图              | 用户说了类似…                                       |
| ----------------- | --------------------------------------------------- |
| **control**       | "打开灯" "空调调到26度" "把窗帘拉上" "扫地"         |
| **query**         | "灯开着吗" "空调设定的温度" "客厅多少度" "湿度多少" |
| **scene_trigger** | "执行回家模式" "触发离家场景"                       |
| **refresh**       | "刷新设备列表" "更新设备信息" "重新拉一遍"           |

## 核心工作流

> **命令拆分 → 确定设备列表 → 确定 spec → 生成指令 → 安全分流 → 下发和回复**
> 触发场景、刷新设备走文末「旁路操作」。

### 步骤 1 · 命令拆分

按用户**自身的表述**，拆成它点到的**每一处设备指代**，各成一条独立命令——各判各的，别把并列的几样揉成一团：

- "打开卧室空调和灯" → `打开卧室空调`、`打开卧室灯`
- "关闭客厅和卧室的灯" → `关闭客厅的灯`、`关闭卧室的灯`
- "台灯和落地灯开了么" → `台灯开了么`、`落地灯开了么`
- "开空调" → `开空调`

### 步骤 2 · 确定设备列表

> **设备目录注入**：system context 注入了 `## 设备目录` 段（行式记录的高频子集，≤50 台 + 生成时刻快照，每台 spec 块也只收高频属性）。字段含义、spec_name 形态看该段目录头部的注释。**catalog 找不到 ≠ 不存在。**

每个命令分别确定它落到哪几台设备。先判这条命令是**单台**还是**复数**（泛指、集合、多台、数量不定，如"空调全关了""灯都关""所有窗帘"）——**靠语义理解判断，而非匹配字面措辞**；**拿不准就按复数处理**。再据此定位 did：

- **2.1 单台 → 从 catalog 搜 did**：卧室仅一台空调时的"卧室空调"、"客厅扫地机"。
- **2.2 复数 / catalog 里没找到 → `device list | grep -E '<中文名>|<英文category>'` 拉全量再找**：grep 把**中文名和英文 category 一起**写进 alternation（`灯|light`），避免漏；`device list` 有时效性，会随时变动，**每轮重查禁复用上一轮**，**绝不拿 catalog 子集当"全部"**。
- **2.3 多候选处理**：命中多台时，若用户明确表达“全部”（如“所有灯”“灯都关”“全屋空调”），选择全部候选；否则反问“哪个房间 / 哪台”，不默认选一台，也不擅自全做。

**注意事项**
- **感知事件触发 → 在事件「来自：房间」里找对应设备**：按该房间缩小范围。⚠️ 事件里 `did=` 是来源设备（摄像头/传感器），不是要控制的目标。
- 仍找不到 → `miloco-cli device refresh` 重试一次 → 回"没找到"，**禁止编造 did**。
- `device list` 很轻量、返回快，有需求直接调用；输出都是**行式记录**（每行一条完整记录、`|` 分隔），可以用 grep 过滤。

### 步骤 3 · 确定 spec

**每台设备都需要找到它自己的 spec 内容**，依据 `spec_name` 和注释定位用户想控制/查询的那一项，读出 **spec_name + access + format/range**：

- **3.1 从 catalog 的 spec 块找**：从每台设备自己下方的预注入spec信息找。
- **3.2 catalog 里没有 / 不全 / 拿不准的设备 → `device spec <did1> <did2> …` 一次拉全**：把所有未确定 spec 的设备一并查（多 did 批量）。catalog 里有但语义不吻合 / 拿不准是不是最贴合的那一项时也拉完整 spec，挑最符合用户意图的调用，别将就模糊匹配。

**注意事项**
- **禁止猜、禁止"同类/同名设备就套同一份 spec 或同一个 spec_name"**——同叫"灯"也可能一个有 `on`、另一个是只读传感器（无可写属性）；spec_name、枚举、范围都因设备而异。批量控制多台时尤其要每台核对，不能拿一台的 spec_name 发给全部。
- **access 列含义**：`w` 可写（控制）、`r` 可读（查询）、`x` 动作（一行可同时含多个，如 `wr`）。
- **spec_name** 就是 spec 行首列（`on`/`brightness`/`target-temperature`/`play-text`/`on@空调` 等）；多键/多模块带 `@` 后缀消歧（`on@油烟机` / `on@照明灯`）。

### 步骤 4 · 生成指令

**4.1 按 access 列生成命令**

| access | 用途 | 命令 |
| --------- | ---- | ---- |
| `w` | 控制属性 | 单属性 `device control <did> <spec_name> <value>`；多属性 `device control <did> --set <spec_name> <v> --set <spec_name> <v>` |
| `r` | 查询属性 | `device props <did> [spec_name ...]` |
| `x` | 执行动作 | `device action <did> <spec_name> [<值1> <值2>…]`（**只传值，不传参数名**） |

**4.2 参数检查**

- **枚举/档位值必须按该设备 spec 的实际枚举映射，禁止套默认档位**：fan-level 可能是 `Level1=1..Level3=3`、也可能 `Auto=0..Level8=8`，不在 catalog spec 块就先 `device spec` 查真实枚举，别脑补"高=3"。
- **范围参数**：严格按 `[min,max;step]` 校验（不越界、且符合 step 步进）。
- **action 只传值**，多入参按 spec 逗号顺序传位置值：

**示例**

| spec 行 | ✅ 正确 | ❌ 错误 |
| ------- | ------ | ------ |
| `start-charge\|x`（无参） | `device action <did> start-charge` | `device control <did> start-charge` |
| `play-text\|x\|text-content:string` | `device action <did> play-text "文本"` | `… play-text text-content:"文本"` |
| `execute-text-directive\|x\|text-content:string,silent-execution:bool` | `… execute-text-directive "你好" false` | `… text-content:"你好"` |

**4.3 强制补 on（控制非 on 属性时）**

控制**非 on 属性**时（temperature/brightness/mode/fan-level/volume…），把开关属性并入同一条 `--set` 一起开机——零成本且幂等，**无需查 on 当前状态、无需分轮**。但补的是 spec 里**实际的开关 spec_name，不是字面 `on`**（先确认存在、再选对 spec_name）：

- spec **没有**开关属性（部分窗帘/传感器）→ **不补**。
- 开关 spec_name 带 `@` 后缀（空调 `on@空调`、油烟机 `on@油烟机`）→ 补**带 `@` 的完整 spec_name**，别补字面 `on`。
- 设备有**多个 on**（油烟机 `on@油烟机` + `on@灯`；插座 `on@开关` + `on@指示灯`）→ 只补**与本次控制功能对应的主开关**，别补到照明/指示灯那个。
- **厨房电器（微波炉 / 烤箱 / 热水器…）→ 不补 `on`**：设 `on` 不会启动工作，须先设参数再调 `start-cook` 类 action，见文末「设备控制指导」。
- **设 on 本身（开/关）、action、查询不补**。

```bash
# "空调调到26度" → 属性 + 该设备实际开关 spec_name（空调是 on@空调，非字面 on）一条到位
miloco-cli device control 4962 --set target-temperature 26 --set on@空调 true
```

**4.4 相对调节（"调高一点"）→ 先查后改**

先 `device props` 取当前值 → 步进（亮度±10/色温±500/温度±1）→ 确保在 `[min,max;step]` 范围内。这是**先查后改**，必须**单独一轮**（要先拿到当前值，不能串进同一条）。

### 步骤 5 · 安全分流

本步只**分流、不下发**：把步骤 4 生成的命令按是否安全分成**普通批 / 危险批**，交给步骤 6 的下发回合。

- **危险批**：**门锁 / 摄像头 / 燃气阀 / 烟雾报警器** 等安全设备的控制 / 动作（断电、开关机、开锁、关阀）——需二次确认。
- **普通批**：其余设备的 control / action，以及**所有设备的 props 查询**。
- 没有危险指令 → 全部归普通批，下发回合只跑第 1 轮。

### 步骤 6 · 下发和回复

一个「回合」=**下发（6.1）→ 回复（6.2）**。按步骤 5 的分流结果，最多跑两轮：

1. **第 1 轮 · 普通批**：下发后**回复时附上所有危险指令的二次确认**，让用户确认。
2. **第 2 轮 · 危险批**：仅把用户同意的危险指令再下发一遍；未同意的跳过。无危险指令则只有第 1 轮。

> "关客厅灯，顺便关摄像头" → 先把客厅灯关掉、回复"灯已关闭，确定要关闭摄像头吗？"（第 1 轮：普通批下发 + 危险确认）；用户确认后才 `device control` 关摄像头（第 2 轮）。

**6.1 下发**

同一轮 **≥2 条互不依赖**的命令（跨设备批量、多设备查询）→ 用 `;` 串成**一行**一次下发：

```bash
miloco-cli device control 4912 --set brightness 30 --set on true ; miloco-cli device control 4945 --set brightness 30 --set on true
```

- **用 `;` 不用 `&&`**：`&&` 短路（一台失败后面全不执行）；`;` 不短路、每条都跑、输出有序。❌ 也别发一条、等结果、再发下一条。
- **输出归属**：`control` / `props` 返回体均含 `data.did`，多条按顺序输出、按 did 对号。
- **单次 ≤10 个设备**，超出自动拆分多轮。**离线设备照常下命令**，由 CLI 返回离线错误，agent 层不预拒。
- **主用 `;` 串联**；仅当环境不支持 `;`（沙箱限制等）→ 退化为在同一条消息里一次性发出全部工具调用。

**6.2 回复**

- 控制确认 → 生成简洁清晰的回复（"空调已调到26度"）。
- 查询 / 集合类 → 按需完整（温度值、灯的数量 + 房间分布）。
- 部分失败 → 报失败设备 + 确认成功的；全部失败 → 给原因 + 建议。

## 设备控制指导

> 设备专属控制知识库：部分设备的正确控制方式与通用流程不同，命中下列设备时以此处为准。

### 智能音箱：`play-text` vs `execute-text-directive`

两个 action 语义完全不同，按用户意图选对：

| 命令 | 语义 |
| ---- | ---- |
| `play-text` | TTS 文字转语音，音箱**逐字念出**传入的文字 |
| `execute-text-directive` | 小爱同学指令，等同于对音箱说"嘿小爱，xxx"，**小爱理解语义后自己执行** |

- 用户要音箱**念出/播报一段话**（"让音箱说'晚安'"）→ `play-text`。
- 用户要**借音箱下达小爱指令**（"让音箱查下天气/关灯"、"让音箱放首歌"）→ `execute-text-directive`。

### 厨房电器（微波炉 / 烤箱 / 热水器 等）：先设参数，再 `start-cook` 启动

这类设备**不能只设温度、也不能 `set on true` 就开始工作**：必须**先设好温度、时间等参数**，再调用启动类 action（如 `start-cook`）才会真正运转。启动 action 的确切 spec_name 以该设备 `device spec` 为准。

## 旁路操作

- **scene_trigger**：用户说的是场景**名称** → 先 `scene list` 拿 名称→scene_id 映射 → `scene trigger <id>`；名称匹配到多个 → 列出追问。
- **refresh**：用户要"刷新 / 更新设备列表" → 直接 `miloco-cli device refresh`，无需走命令拆分 → spec → 下发；返回最新设备数后回复"已刷新，共 N 个设备"。

## 异常处理

| 异常 | 处理 | 回复 |
| ---- | ---- | ---- |
| 设备不在目录 | `device list \| grep` 拉全量再搜（别省） | （静默） |
| 设备未找到 | `device refresh` → 重试一次 | "没找到，正在刷新…" |
| 设备离线 | **照常下命令**，CLI 返回体 `results[]`/`result` 的 `code_msg` 会标"设备离线" | "{设备名}离线了" |
| 设备侧执行失败 | 看返回体 `results[]`/`result` 的 `code_msg` 中文原因（属性不可写 / 属性不存在 / 属性值不正确等）→ 据此回复或改对重发 | "{设备名}该属性不可写" |
| 参数越界 | CLI 报 `out of range [min,max;step] <unit>` → 按范围改对 | "亮度范围1-100" |
| 枚举值非法 | CLI 报 `not a valid enum; allowed: …` → 从列出的可选值里挑对的重发 | "风速可选 自动/1-8 档" |
| 多设备同名 | 列候选追问 | "找到2个，哪个？" |
| spec_name 多匹配 | CLI 报 "matches N iids" → 按建议重发 | （自动处理） |
| 用 control 调 action | CLI 报 "is an action… 请改用 device action" → 切 `device action` | （自动处理） |
| CLI 超时 | 3 秒后重试一次 | "超时，重试中…" |

## 关键规则

1. **`spec_name` / action `iid` 不硬编码**——以 catalog / `device spec` 为准（`@` 后缀、`play-text` 等因设备而异）。
2. **安全设备控制必须二次确认**——门锁/摄像头/燃气阀/烟雾报警器（步骤5）。
3. **控制非 on 属性强制补该设备实际开关 spec_name**（并入同一条 `--set`，可能 `on@空调`，非字面 `on`）——不查不分轮（步骤4）。
4. **离线设备照常下命令**——由 CLI 兜底。

## 边界

- ❌ 不支持非米家生态设备 / 第三方 API / 绕过安全规范的指令
- ❌ 禁止编造 did / spec_name / model
- ⚠️ 单次 ≤10 个设备，超出自动拆分
- ✅ 支持 `scene trigger` 触发已有场景、`device refresh` 刷新缓存

## 示例

| 用户说 | 命令 |
| ------ | ---- |
| "空调调到26度"（控制非 on 属性 → 补该设备实际开关 spec_name） | `device control 4962 --set target-temperature 26 --set on@空调 true` |
| "关客厅灯"（设 on 本身，灯的开关就是 `on`） | `device control 4912 on false` |
| "空调设定的温度"（查询） | `device props 4962 target-temperature` → 26 |
| "客厅多少度"（环境数据，同样走 props） | `device props ht01 temperature` → 24.5 |
| "家里有几盏灯"（集合 → grep 每轮重查） | `device list \| grep -E '灯\|light'` |
| "扫地机回去充电"（access=x → action 只传值） | `device action 4981 start-charge` |

**多设备顺序下发** — "卧室灯都调到30%"：grep 筛出 4912/4945/4653，每盏补 on，`;` 串成一行：

```bash
miloco-cli device control 4912 --set brightness 30 --set on true ; miloco-cli device control 4945 --set brightness 30 --set on true ; miloco-cli device control 4653 --set brightness 30 --set on true
```

**catalog 找不到 → list 兜底** — "打开阳台灯"：catalog 无 → `device list | grep -E '灯|light'` 命中 `5001 阳台灯带` → `device control 5001 on true`。

**多候选必追问** — "把灯调暗一点"：全屋多盏灯分散在各房间，用户没指明哪台、也没说"全部" → grep 枚举后**反问"哪个房间的灯？"**，既不默认挑一台、也不擅自全做（对照上面"灯**都**调30%"是明确全体 → 才全做）。

**安全设备** — "关客厅灯，顺便关摄像头"：🔒 先关灯下发（普通）；回复"灯已关闭，确定要关闭摄像头吗？"（普通结果 + 危险确认）；用户确认后才 `device control cam_001 on false`。
