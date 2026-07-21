# 事件反馈打包

## 背景与目标

感知偶尔会出错——把爸爸认成爷爷、把宠物动作认错、规则/建议误触发。用户在日志页看到一条离谱的有价值事件，想告诉团队"这条判错了"，但一条自然语言吐槽对复现和改模型几乎没用：真正能定位问题的是那次 omni 推理的**原始输入与决策记录**（视频/音频、prompt、模型回复）。

事件反馈打包能力把「用户指认错误」和「打包该事件的 omni 复现数据」合成一步：用户在日志页点「反馈」、勾错误类别、写一句补充，系统就把这条事件的 omni_trace + 原始 clip + 元数据脱敏后打成一个本地 tar.gz，用户可一键打开所在文件夹，再通过飞书问卷把包提交给团队。目标是把散落的坏例攒成可复现、可回溯的 bad-case 数据集，喂给模型与规则迭代。

---

## 产品面

### 能做什么

- **一键指认错误**：日志页每条有价值事件下带「反馈」入口，勾选错误类别（人物 / 宠物 / 动作 / 环境设备 / 语音 / 规则/建议误触发 / 其他）+ 可选补充说明
- **打包 omni 复现数据**：把该事件的推理记录（omni_trace）、omni 当时看到的原始音视频片段、事件元数据打成一个本地 tar.gz
- **隐私可控**：默认对打包文本做个人信息脱敏；人员画廊（可能含人脸）默认不含，需用户主动勾选
- **已反馈可见**：反馈过的事件显示「已记录」态，附本地包路径，可点击直接打开所在文件夹
- **引导提交**：打包完成后给出飞书问卷链接，用户把本地包上传给团队

### 典型场景

**场景 — 认错人后反馈**：用户在日志页看到"检测到爷爷进入客厅"，但当时进门的是爸爸。用户点这条事件的「反馈」，勾「人物识别错误」，补一句"是爸爸不是爷爷"，点「打包数据」。几秒后提示"反馈已记录，数据已保存到本地"，点「打开所在文件夹」看到刚生成的 tar.gz，再点飞书问卷链接把它传给团队。

### 能力边界

- **仅本地打包，暂不自动上传**：当前包只落本地磁盘，接口已预留上传字段（`uploaded` / `upload_key`），上传服务就绪前始终为未上传，提交靠飞书问卷手动完成
- **依赖 artifacts 已落盘**：能打进包的内容取决于该事件当时是否落了 omni_trace / clip；缺失的部分在返回的 `components` 里如实标注（found / missing），不因缺件而失败
- **只对有 trace 的事件开放**：前端据事件的 `has_trace` 决定是否显示反馈入口（无推理记录的事件反馈无意义）
- **打开文件夹限桌面端**：`reveal-dir` 走系统文件管理器（macOS `open` / Linux `xdg-open`），且只允许打开打包目录内的路径
- **磁盘占用有上限**：打包目录总大小超限时自动从旧到新清理（阈值见下方指路）

---

## 研发面

### 架构概览（数据流图）

反馈打包本身是「读取已落盘 artifacts + 脱敏 + 打 tar.gz」的薄编排；它复用的原始数据（clip / omni_trace）由感知流水线在推理时旁路收集、落盘（见 [感知流水线](perception-pipeline.md)），本模块不产生这些数据、只消费。

```
日志页某事件 → 点「反馈」（勾错误类别 + 补充 + 是否含画廊）
  → FeedbackSection（web/src/components/ActivityFeed.tsx）
  → submitEventFeedback（web/src/api/index.ts → real.ts::realSubmitEventFeedback）
  → POST /api/admin/events/feedback（admin/router.py::submit_event_feedback）
      解析米家 uid（MiotProxy.get_user_info，失败落 anonymous）
      → asyncio.to_thread 调 build_feedback_pack（打包是同步 IO,不阻塞 event loop）
  → build_feedback_pack（admin/feedback_pack.py）
      meaningful_events_dao.get_by_id 取事件（不存在 → EventNotFoundError → 404）
      读事件目录 snapshots/{event_id}/：omni_trace.json.gz、{device_slug}/clip.*、gallery/
      → PII 脱敏（trace 文本 + 事件 text + 用户补充）
      → 写 metadata.json + omni_trace + clips(+可选 gallery) 到
        $MILOCO_HOME/packs/{时间戳子目录}/feedback-{uid}-{event_id}-{时间戳}.tar.gz
      → 按总大小清理旧包
      → 返回 {path, size_bytes, components}

已反馈态（列表侧,与打包解耦）：
  GET 事件列表 → EventsService.list_events（perception/events_service.py）
      _build_feedback_index 扫 packs 目录,UUID 正则回捞 event_id → (path,size)
      → MeaningfulEvent.has_feedback / feedback_pack_path / feedback_pack_size

打开文件夹：
  已反馈态点「打开所在文件夹」→ revealDir → POST /api/admin/reveal-dir
  （admin/router.py::reveal_dir，路径限制在 packs 根内）
```

### 核心模块

| 类 / 符号                                     | 文件                                                    | 职责                                                                                                              |
| --------------------------------------------- | ------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| `build_feedback_pack`                         | `admin/feedback_pack.py`                                | 打包核心：取事件 → 读事件目录 artifacts → PII 脱敏 → 写 tar.gz → 总大小清理；返回 path/size/components 完整性记录 |
| `submit_event_feedback` / `reveal_dir`        | `admin/router.py`                                       | 反馈提交端点（解析 uid、线程化打包、映射 404/500）；打开文件夹端点（限 packs 根，跨平台 open）                    |
| `EventsService._build_feedback_index`         | `perception/events_service.py`                          | 扫 packs 目录建 `event_id → (path,size)` 索引，把「已反馈态」注入事件列表（同一事件多包取最新）                   |
| `MeaningfulEvent`（反馈相关字段）             | `perception/schema.py`                                  | 事件对外模型新增 `has_feedback` / `feedback_pack_path` / `feedback_pack_size`；`has_trace` 决定前端是否显示入口   |
| `OmniEventArtifacts` / `save_event_artifacts` | `perception/snapshot_context.py` / `snapshot_writer.py` | 推理侧旁路收集 clip/trace/gallery 并落到事件目录——本模块打包的原料源（属感知流水线，非本模块产出）                |
| `FeedbackSection`                             | `web/src/components/ActivityFeed.tsx`                   | 反馈面板 UI：错误类别多选、补充文本、画廊开关、已反馈态、飞书问卷链接、打开文件夹                                 |

### 关键设计决策

**打包只消费、不产生 artifacts**：clip 与 omni_trace 由 omni 推理链路在编码/调用现场「旁路」收集（`OmniEventArtifacts` + ContextVar，见 `perception/snapshot_context.py`），随事件一次性落到 `snapshots/{event_id}/`——字节级即 omni 当时看到的、零重编。反馈打包只是事后按 `event_id` 把这些已落盘文件读出来重新封装，故本模块无需触碰深层 omni 调用栈，也保证包内 clip 与推理输入完全一致。

**已反馈态从文件系统派生，不加 DB 列**：`_build_feedback_index` 每次列事件时扫一遍 packs 目录、用 UUID 正则从包文件名回捞 `event_id`，即时算出 `has_feedback`。事实源就是包文件本身，避免为一个可从磁盘推导的状态做 schema migration，也天然容忍用户手动删包。

**PII 脱敏且失败即弃（fail-closed）**：打包前对 trace 文本、事件 text、用户补充说明统一做个人信息正则替换；trace 脱敏一旦异常，宁可整段丢弃 trace 也不把未脱敏内容打进包（"宁可缺 trace 也不泄 PII"）。脱敏规则与替换目标见 `admin/feedback_pack.py`，属实现细节不在此展开。

**画廊 opt-in**：人员画廊合成图可能含人脸，属最敏感数据，默认不打包，必须用户在面板显式勾选才包含——隐私默认保守。

**本地优先、上传预留**：端点响应已带 `uploaded` / `upload_key` 契约位，但上传服务未就绪前恒为「未上传」；包落在按时间戳分的独立子目录，方便用户在文件管理器里逐个打开、经飞书问卷手动提交。上传通道就绪后从本处补齐即可，前端契约不变。

**reveal-dir 路径收敛**：打开文件夹端点把目标路径 `resolve` 后校验必须落在 packs 根内，拒绝越界路径——避免被诱导打开任意目录。

**打包在线程池执行**：打 tar.gz 是同步阻塞 IO，端点用 `asyncio.to_thread` 卸到线程，不阻塞单进程 event loop。

### 对外契约语义

- **`POST /api/admin/events/feedback`**：入参 `event_id` + 错误类别 + 补充文本 + 是否含画廊；返回 `pack_path` / `pack_size_bytes` / `uploaded`(当前恒 false) / `upload_key`(当前 null) / `components`。`components` 逐项标注 trace 是否找到、哪些 device 的 clip 找到 / 缺失、画廊是否含入——即使部分缺失也成功返回。事件不存在返回 404。
- **`POST /api/admin/reveal-dir`**：入参 `path`（须在 packs 根内），在系统文件管理器打开该目录；越界 403、不存在 404。
- **事件模型的反馈字段**：`has_feedback`（是否已有包）、`feedback_pack_path` / `feedback_pack_size`（最近一次包的本地路径与大小）；`has_trace` 供前端决定是否显示反馈入口。字段定义见 `perception/schema.py::MeaningfulEvent`。

### 如果我要改事件反馈相关功能

| 修改目标                           | 去看哪个文件                                                           |
| ---------------------------------- | ---------------------------------------------------------------------- |
| 打包内容 / 目录结构 / PII 脱敏规则 | `admin/feedback_pack.py`（`build_feedback_pack`）                      |
| 打包目录总大小上限 / 清理策略      | `admin/feedback_pack.py`（`_cleanup_by_total_size`）                   |
| 提交端点入参 / 上传通道接入        | `admin/router.py`（`submit_event_feedback`）                           |
| 打开文件夹的路径限制 / 平台命令    | `admin/router.py`（`reveal_dir`）                                      |
| 已反馈态如何算出 / 多包取哪个      | `perception/events_service.py`（`_build_feedback_index`）              |
| 事件模型的反馈字段                 | `perception/schema.py`（`MeaningfulEvent`）                            |
| clip / trace / 画廊的产生与落盘    | `perception/snapshot_context.py`、`snapshot_writer.py`（属感知流水线） |
| 反馈面板 UI / 错误类别 / 问卷链接  | `web/src/components/ActivityFeed.tsx`（`FeedbackSection`）             |

### 与其他模块的关系

**上游（数据来源）**：[感知流水线](perception-pipeline.md) 在每次 omni 推理后把 clip / omni_trace / gallery 收敛进 `OmniEventArtifacts` 并落到事件目录，是反馈包的原料源；打包只读不写这些文件。

**主体（事件身份）**：以 `meaningful_events` 的 `event_id` 为主键——`build_feedback_pack` 经 `meaningful_events_dao` 取事件元数据，`_build_feedback_index` 用 `event_id` 关联包文件。有价值事件的定义见 [感知流水线](perception-pipeline.md)。

**入口（Web）**：日志页 `ActivityFeed` 是唯一交互入口，反馈面板与视频回放共处一条事件流；已反馈态、打开文件夹、飞书问卷链接均在此渲染，前端 API 封装在 `web/src/api/index.ts` / `real.ts`、类型在 `web/src/lib/types.ts`。

**旁路收集范式**：artifacts 的 ContextVar 旁路收集与 observability 的 `trace_id` 是同一套 task-bound ContextVar 模式，reviewer 可类比理解。
