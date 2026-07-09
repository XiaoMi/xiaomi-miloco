# 感知流水线

## 背景与目标

传统智能家居靠传感器（门磁、人体感应）只能回答"有没有人"，无法回答"谁在做什么"或"有没有值得关注的事"。

感知流水线解决这个问题：持续从家庭摄像头采集音视频，自动分析场景变化，将结果驱动规则自动化和 Agent 主动介入。用户无需手动查看摄像头，系统自动"看"——识别出谁在场、在做什么、说了什么。

---

## 产品面

### 能做什么

- **场景描述（caption）**：用自然语言输出当前画面发生了什么；识别出的家庭成员以成员名出现，而非匿名编号（"爷爷坐在书房看书"而非"unknown_1 坐着"）
- **人物识别（person_id）**：识别家庭成员身份；对陌生人分配唯一编号（`unknown_<n>`），同一人跨摄像头/跨帧保持同一编号
- **语音指令（speeches）**：识别家人对 Miloco 发出的语音指令，触发 Agent 响应；无需额外麦克风，摄像头内置音频即可
- **规则命中（matched_rules）**：将场景语义对照用户配置的规则，输出命中列表驱动自动化
- **主动建议（suggestions）**：感知到值得提醒的事项（如老人长时间未移动），经去重后推送给 Agent
- **主动查询（on-demand）**：Agent 可随时触发"现在客厅里有几个人"，跳过 Gate 直接推理
- **有价值事件（meaningful_events）**：规则命中、语音指令、建议三类事件沉淀到数据库，附带视频片段，供 Agent 和用户回溯

### 典型场景

**场景 1 — 语音触发 Agent**：家人对着摄像头说"Miloco，把客厅灯调暗"。感知流水线提取到语音指令，经 `speeches` 字段投递给 Agent，Agent 调用 `miloco-devices` Skill 执行控制。

**场景 2 — 规则自动响应**：用户预设规则"当爷爷在书房坐超过 30 分钟时，提醒他起来活动"。感知流水线识别出书房摄像头中爷爷持续出现，规则条件满足后 Agent 通过音箱播报提醒。

**场景 3 — 陌生人告警**：家中无主人时，摄像头出现陌生人。感知流水线识别为 `unknown_1`，命中 DYNAMIC 规则"有陌生人在家时通知我"，后端向 Agent 投递回调，Agent 通过手机通知联系主人。

### 能力边界

- 感知以时间窗口为单位触发；每台摄像头独立流水线，身份库全局共享
- 感知质量取决于摄像头画质、光线条件和 VLM（MiMo）的理解能力，存在误判率
- 规则条件由 VLM 自然语言推理，非精确传感器；不适合需要精确数值判定的场景（"温度超过 28 度"应走传感器，不走感知流水线）
- 仅处理 RGB 彩色画面，不处理纯红外/热成像摄像头
- 感知范围可管理但有约束：账号含多个家庭时同一时刻只感知一个（切换即换，其余自动停用）；家庭内摄像头默认全部接入、可按需停用，且同时启用的摄像头数有上限。范围经 `miloco-miot-scope` Skill / `/api/miot/scope/*` 端点管理，改动热同步生效无需重启
- 感知引擎 PREREQ_MISSING（Omni API Key 未配置或 ONNX 模型缺失）时推理跳过，设备控制等其他功能不受影响；主动查询接口返回 `503`，状态/日志等查询接口不受影响
- 不支持跨局域网推流（摄像头和 Miloco 服务须在同一网络）

---

## 研发面

### 架构概览（数据流图）

```
CameraDeviceAdapter（per-camera）
  → MultiTrackSyncBuffer（音视频时间窗口对齐）
  → PerceptionRunner 触发（窗口就绪 或 采集间隔超时）
  ↓
MultimodalCollector（perception/collect/collector.py）
  ↓
PipelineProcessor（perception/processor.py）
  ↓
PerceptionEngineProxy（perception/client.py）
  ↓  per-device 并发（asyncio.gather）
  Gate → 过滤静止窗口（None 则跳过后续）
    ↓ 通过
  Identity → {track_id → person_id} 映射
    ↓
  Omni（VLM）→ OmniOutput
    ├─ caption / speeches / env_sounds
    ├─ matched_rules（命中规则列表）
    └─ suggestions
  ↓
PerceptionEngineProxy 结果后处理
  ├─ PerceptionLogRepo（写感知日志）
  ├─ 本轮下发到各摄像头的规则上报 True/False → RuleService.update_state
  ├─ speeches / suggestions → AgentDispatcher
  └─ meaningful_events 写入 + 事件 artifacts 落盘（clip + omni trace）
```

`PerceptionEngine`（`perception/engine/api.py`）是顶层入口，内部通过 `perception/engine/pipeline.py` 按房间分组后并发执行：room 之间、以及同 room 内多设备均以 `asyncio.gather` 并发（各设备 Gate→Identity→Omni 独立成协程），omni 墙钟从 Σ 收敛到 ≈max。`PerceptionEngineProxy`（`perception/client.py`）只是它对外的包装 + 结果后处理入口，推理编排本身在 `PerceptionEngine`。

### 核心模块

#### Input — 采集与缓冲

`MultimodalCollector`（`perception/collect/collector.py`）管理多设备适配器。`CameraDeviceAdapter`（`perception/collect/camera_adapter.py`）负责单摄像头接入，内部用 `MultiTrackSyncBuffer`（`perception/collect/stream_buffer.py`）将音视频帧按时间窗口对齐。

`PerceptionRunner`（`perception/runner.py`）是后台调度器，双触发机制：窗口就绪事件和采集间隔超时。推理在专用单线程 `ThreadPoolExecutor`（`perception-infer`）中执行，不阻塞主事件循环。

#### Gate — 变化门控

Gate 层（`perception/engine/gate/gate.py`）对每个窗口做双模态判定：视觉帧差分（`gate/visual_gate.py`）和音频峰值能量（`gate/audio_gate.py`）。任一触发即通过，输出 `GatePacket`；两路均无变化且不在 hold 窗内时返回 `None`，下游整个跳过。

**Hold 滞回**：视觉刚通过后的一段时间内，即使本窗视觉/音频都无变化也继续放行并在 `GateTrigger.hold` 打标，让下游保持 video 路由——避免人短暂静止时 route 在 video / audio 间来回抖动；on-demand 单次调用不触发 hold（时长配置见 `settings.yaml::perception`）。

音频过能量门后再跑一道语音活动检测（VAD，`gate/speech_vad.py`，silero 模型）：判定本窗音频是否含真人声，无人声则从下游 schema 剥掉 `speeches` 字段——这是对输出字段的子门控，不改变窗口整体是否放行。

#### Identity — 跟踪与身份识别

Identity 层编排器（`perception/engine/identity/identity.py`）编排两条子链路：

- **跟踪侧**：`DeepSortTrackingService`（`engine/identity/tracking_service.py`）封装 `DeepSortTracker`（`engine/identity/deep_sort.py`，IoU + 卡尔曼 + ReID 关联级联），执行检测 + ReID 特征提取 + 卡尔曼滤波，输出 active tracks
- **识别侧**：`IdentityEngine`（`engine/identity/engine.py`）维护每个 track 的识别状态机，决定何时派发识别请求，回流结果后返回 `{track_id → person_id}` 映射

映射写回 `IdentityPacket.targets[].person_id` 后，Omni prompt 中以成员名渲染对应 track，VLM 输出的 caption 中出现成员名而非匿名编号。

识别对每个待识别 track 给出三类结论之一：命中 gallery 成员、确有人但认不出（`unknown`）、框内根本不是人（`no_person`，非人物体被误检成人体框）。判为 `no_person` 的 track 停止重复识别、身份不对外导出、也不进陌生人聚类（非永久判定，画面条件变化后会重新识别）——避免静态误检框被 VLM 脑补成"有陌生人在做某事"（现场无人却报告有人）。

每个摄像头持有独立的 `TrackingService` 和 `IdentityEngine` 实例（按 `device_id` 懒加载创建）。`IdentityLibrary`（`engine/identity/library.py`）全局共享——所有 per-camera 实例共用同一份样本库。

#### Omni — VLM 场景推理

Omni 层（`engine/omni/omni.py`）调用视觉语言模型（MiMo API，OpenAI 兼容协议），输入 `IdentityPacket`，输出结构化的 `OmniOutput`。

两类调用入口：实时感知（含 fused 模式，将身份识别合并到主调用，当前默认）和主动查询（非流式，跳过 Gate 直接推理）。核心编排在 `engine/omni/omni.py`。

**两种 route 语义**：Omni 层根据当前窗口是否有视觉变化选择 video 或 audio 路由。audio route 仅发送音频（无视频），省去视觉相关输出字段，降低 token 消耗。

**Prompt 架构**：system prompt 由 `prompt_builder.py` 按场景动态装配，核心组件是 `field_registry.py` 中的 `FieldSpec` 和 `SceneDescriptor`——`FieldSpec` 是所有输出字段 schema 与说明的唯一来源，`SceneDescriptor` 描述本次调用的场景维度，由此派生完整 system prompt，杜绝多处散落导致的 schema 漂移。

#### PipelineProcessor — 编排层

`PipelineProcessor`（`perception/processor.py`）将 collect → 推理 → 日志 → 后处理连接成完整管线，分别提供实时感知和主动查询两个入口。`PerceptionEngineProxy`（`perception/client.py`）包装推理引擎，提供结果后处理入口 `handle_realtime_perception_result`。

### 关键设计决策

**Gate 的作用**：Omni VLM 调用有明显成本（延迟 + API 费用）。家庭场景大多数时间静止，Gate 过滤这些窗口，是整条链路的核心降本设计。

**为什么用 DeepSORT**：纯 IoU 跟踪在多人遮挡、检测抖动时 track 容易死亡重建，导致同一人被分配多个 track_id，身份状态频繁重置。DeepSORT 引入 ReID 特征关联，跨帧关联更鲁棒，减少 IdentityEngine 反复发送重复识别请求。

**per-device Omni 设计**：每个摄像头独立调一次 Omni，而非多摄像头合并。合并调用时，prompt 只携带首个摄像头的视频，其他摄像头的 track 无对应视觉信息，识别准确率低。

**Fused 模式**：将身份识别合并到主 Omni 调用中，省掉额外的识别请求。gallery 采用"全或无"语义：任一候选成员的图像合成失败，整 gallery 放弃，避免少一个人时 Omni 错认。

**主动查询路径（on-demand）**：主动查询入口从实时流缓冲 peek 数据后跳过 Gate 直接走 Identity + Omni，不影响实时流水线。

**Suggestion 去重**：`PerceptionEngine` 对建议做去重抑制，同类建议短期内只报一次，避免 Agent 被重复触发。去重用句向量语义相似度（`EventEmbedder`，`engine/omni/dedup_embedder.py`，bge-small-zh）而非精确文本匹配——措辞略有差异的同类建议也能识别为重复；embedder 初始化失败时降级为精确文本匹配。

**Prompt 人名护栏**：VLM 只能给本轮 Identity 真正识别出的成员安姓名；对未识别（`unknown`）的人、或名册中本轮画面未真正出现的陌生人，一律不从 gallery / 家庭档案取成员名安到画面人物上，防止"注入了家庭档案就凭空点名"的幻觉。约束集中在各字段的 `FieldSpec`（`field_registry.py`）。

**omni prompt「当前时间」锚定部署时区**：注入 omni prompt 的当前时刻走 `deploy_timezone()`（`perception/engine/api.py` 的时钟格式化）而非裸主机时钟——VLM 会据此把画面标注成「凌晨 / 早上…」，宿主时区 ≠ 部署（家庭真实所在）时区时裸时钟会让模型编造出错误的时段。部署时区的定义、解析优先级与配置方式见 [开发指南 · 时区](../06-dev-guide/dev-guide.md#时区)。

**有价值事件沉淀**：每次推理后，规则命中/语音指令/建议至少一项为真时，写入 `meaningful_events` 表并异步落盘事件级 artifacts。per-device 视频片段（字节从编码现场旁路到事件写入侧，无需重新编码）与本次 omni 调用 trace 一并收敛到 `OmniEventArtifacts` 容器（`perception/snapshot_context.py`），由 `snapshot_writer.py` 一次性落到事件目录，trace 供事后复盘 LLM 决策。

**引擎降级与自愈**：Omni API Key 未配置或 ONNX 模型缺失时，引擎进入 `PREREQ_MISSING` 状态，感知推理跳过，设备控制等功能不受影响，`/health` 返回 200。前置条件补齐后无需重启——`PerceptionRunner` 每个 tick 自愈一次，下个推理周期自动拉起引擎（廉价的"等外部条件"态才放行；引擎初始化真失败不在此重试，需手动重启感知）。从 web 删除或停用当前生效的模型配置时，引擎回到"未配模型"态并软停，仅关引擎实例、保留采集与自愈循环，重新配好后自动恢复；软停与在飞推理经 `PerceptionEngineProxy` 的引擎锁互斥，避免推理途中被 teardown 拔掉引擎而崩溃。

**用户「休息」暂停的持久化**：用户在 web 上「让它休息 / 唤醒」的意图会落盘到 KV（`perception/engine_state.py`，缺省视为开启，老部署 / 新装行为不变）。系统自动拉起的两条路径——开机 init 与重新授权后 restart——在 start 前先查该意图，被暂停则跳过、不自动拉起。为什么要持久化：早先暂停仅为内存态，后台一旦重启（崩溃自愈 / 手动重启 / 重新授权）就无条件把引擎拉起、继续调云端 VLM 烧 token，用户额度被静默耗光；落盘 + 开机门控根治这一静默复位。持久化只挂在「唤醒 / 让它休息」两个用户 endpoint 上，机制层的引擎启停底层方法、软停、优雅关停、切家一律不碰，显式唤醒仍必然 start。落盘失败时 endpoint fail-loud（返回 `500`、不执行启停），让用户如实感知可重试，而非静默返回成功却在重启后复位。

### 如果我要修改感知相关功能

| 修改目标                                | 去看哪个文件                                                                                 |
| --------------------------------------- | -------------------------------------------------------------------------------------------- |
| 修改 Gate 触发阈值                      | `perception/engine/gate/visual_gate.py`（视觉）、`gate/audio_gate.py`（音频）                |
| 修改 VLM 输出字段定义（schema/说明）    | `perception/engine/omni/field_registry.py`（`FieldSpec` 单一来源）                           |
| 修改 VLM prompt 组装逻辑                | `perception/engine/omni/prompt_builder.py`                                                   |
| 修改家庭档案注入 Omni 的方式            | `perception/engine/omni/home_profile_loader.py`                                              |
| 修改身份识别逻辑                        | `perception/engine/identity/engine.py`（识别状态机）、`tracking_service.py`（DeepSORT 跟踪） |
| 修改感知结果后处理（规则上报/事件投递） | `perception/client.py`（`PerceptionEngineProxy`，`handle_realtime_perception_result`）       |
| 修改感知调度/触发频率                   | `perception/runner.py`；配置在 `settings.yaml::perception.collect`                           |
| 修改感知 API 端点                       | `perception/router.py`                                                                       |

### 感知相关 API 路径

主要入口：`POST /api/perception/perceive`（主动查询）和 `/api/perception/engine/start|stop|status`（引擎生命周期管理），完整端点见 `perception/router.py`。

### 与其他模块的关系

**上游**：`CameraDeviceAdapter` 通过 MiOT SDK 解码层订阅摄像头帧，与直播观看共享同一次解码（避免重复推流）。scope 变更后 `MiotService` 触发 adapter 同步。

**下游**：规则按各自配置的感知设备范围（空=广播到全部感知设备）精确下发到对应摄像头；每次推理后，把本轮实际下发的每个 (规则, 摄像头) 的 True/False 上报给规则引擎（`RuleService.update_state`）驱动状态机——未下发到某摄像头的规则不参与该摄像头的状态推退。`speeches` 和 `suggestions` 经 `AgentDispatcher` 投递给 Agent。

**共享**：`home_profile_loader.py` 将 `profile.md` 注入 Omni 动态层，形成感知→记忆→感知的正反馈闭环。

### 配置

感知相关参数集中在 `settings.yaml::perception` 段，字段定义见 `settings.py` 的 `PerceptionSettings`。

### 可观测性

每个感知 cycle 完成后，`MetricsClient`（`observability/metrics_client.py`）将 cycle 级汇总和 per-device 细节异步写入 `observability.db`。追踪内容涵盖各阶段耗时、Gate 通过率、Omni 调用次数和错误类型。通过 web 面板 URL hash `#perf` 进入的独立调试视图可查看这些指标的时序图和分布。
