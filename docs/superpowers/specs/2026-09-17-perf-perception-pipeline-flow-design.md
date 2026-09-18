# Perf 页运行时感知流水线图

日期：2026-09-17

## 1. 目标

在现有 Perf 页面增加一张只读的运行时感知流水线图，用于回答：

- 当前窗口经过了哪些处理阶段；
- 每个阶段输入和输出的视频尺寸、帧率与帧数；
- 缓冲区是否发生积压或丢弃；
- Gate 是否放行；
- 尺寸或帧率具体在哪个阶段发生变化；
- 当前失败或跳过发生在哪个阶段。

后端负责把生产实现映射为稳定的 Graph JSON；前端只按协议布局和展示，不根据业务节点名称推导流程。

## 2. 范围与约束

- 只展示 realtime Pipeline，不接入 on-demand。
- 新增诊断只保存在当前进程内存，不写 SQLite、事件或 trace 文件。
- 进程重启后运行时快照清空。
- `perf.enabled=false` 时不创建快照 store、不采集专用诊断；接口照常注册，store 缺失时返回 503。
- 图内容统一为英文；Perf 卡片标题、说明、按钮和状态提示使用现有前端 i18n。
- 前端为只读展示页，不提供节点详情框、边详情框、点击跳转或流程编辑能力。
- 页面不提供“全部设备”选项，设备列表存在时默认选择第一台设备。
- 图一次完整展示，不使用横向拖动条；每个 group 独占一行，每行均从左向右。
- 所有尺寸来自实际帧、当前模型 session 或本次媒体变换结果，不使用固定显示常量。

## 3. 生产流程事实

实时主路径以 `run_batch_pipeline()` 为准：

```text
Decoded Camera Frames
  → Sync Window
  → Ready Queue
  → Pipeline Sampling
  → Visual Gate
  → Detection, Tracking, ReID
  → Omni Sampling
  → Media Transform
  → Media Encoding
  → Omni Request
  → Result Complete
```

### 3.1 最早可观测边界

当前诊断从解码帧进入感知回调后开始。系统没有在同一链路中观测相机编码码流和解码器输入，因此图不创建虚假的 `Camera Source` 或 `Media Decode` 节点。

`media.decoded` 表示当前可观测到的最早视频边界：已经解码的 BGR 帧。

### 3.2 Sampling、Gate 与 Tracking

`downsample_snapshot()` 在 Pipeline 入口先把窗口抽样到 `input.fps`。因此真实关系是串行关系：

```text
Ready Queue
  → Pipeline Sampling
  → Visual Gate
  → Detection, Tracking, ReID
```

Gate 和 Tracking 都消费 Pipeline Sampling 产生的同一批源尺寸帧，但 Tracking 只有在 Gate 放行后才执行，不与 Gate 并列。

Visual Gate 内部会为帧差检查生成缩放后的检查图。该检查图尺寸只作为 `check_frame_size` metric 展示，不能作为 Gate 的媒体输出尺寸。Gate 放行时透传的仍是抽样后的源尺寸帧。

Gate 未放行时：

- `Pipeline Sampling → Visual Gate` 仍为 active；
- `Visual Gate → Detection, Tracking, ReID` 为 inactive；
- 该边标签为 `Gate blocked`，不携带媒体数据；
- 后续节点显示 skipped 或 inactive 状态。

### 3.3 Detection、Tracking 与 ReID

生产代码中 Detection 不是主视频流上的独立 packet：Tracking Service 逐帧调用 tracker，tracker 内部再调用 detector。Detection 的检测框映射回源帧坐标，Tracking 输出 packet 仍保留原始抽样帧。

因此图使用一个语义节点：

```text
identity.track: Detection, Tracking, ReID
```

该节点的主媒体输入和输出保持抽样源帧尺寸。以下尺寸作为节点内部 metric 展示：

- `detector_input_size`：从当前 Detector ONNX session 输入 shape 读取；
- `reid_input_size`：从当前 ReID ONNX session 输入 shape 读取。

这可以明确表达“模型内部可能放大或缩小输入，但主视频流分辨率没有在 Detection → Tracking 之间改变”。

### 3.4 Omni 与媒体编码

Tracking 后的 `IdentityPacket.all_frames` 先在 `omni.sample` 下采到 `omni_fps`，分辨率仍为源尺寸。

`media.transform` 表示编码前的实际 resize 或 Smart Crop：

- 输入尺寸来自 Omni 抽样帧；
- 输出尺寸在实际 resize 完成时记录；
- `transform_mode` 表示 `panorama` 或 `smart_crop`；
- 配置的 `video_short_edge` 只作为 configured metric，不冒充实际输出尺寸。

`media.encode` 的输入等于本次 Transform 输出；编码成功后的输出尺寸、FPS 和帧数来自实际 `LocalMediaInfo`。

`omni.request → result.complete` 传递的是推理结果，不是视频媒体，因此该边不携带 `GraphMedia`。

音频-only 路径不执行视频 Transform：`Media Transform` 为 `inactive`，`Omni Sampling → Media Encoding` 增加 `Audio bypass` 旁路边。此时 Encoding 展示实际音频容器与采样率；音频存在性进协议、不上卡片；视频尺寸、FPS 和帧数保持未知。

前端对同一行内跨越其它节点的旁路边使用卡片下方的正交路径，避免连线穿过 `Media Transform` 等中间节点。

## 4. 默认语义图

节点按 group 分成四行：

```text
[Source]
Decoded Camera Frames

[Buffer]
Sync Window → Ready Queue

[Processing]
Pipeline Sampling → Visual Gate → Detection, Tracking, ReID

[Output]
Omni Sampling → Media Transform → Media Encoding → Omni Request → Result Complete
```

`Ready Queue` 优先展示最近一次 drain 前的运行时队列深度和累计丢弃窗口数，再展示容量、满队列动作等配置项。

设备摘要包含当前活跃但尚未产生首个运行时快照的设备。此类设备的房间、trace 和观测时间为 `null`，freshness 与 status 为 `unknown`，以便前端仍可默认选择设备并展示空状态图。

跨行连接保持真实方向：

```text
Decoded Camera Frames → Sync Window
Ready Queue → Pipeline Sampling
Detection, Tracking, ReID → Omni Sampling
```

每行都从左向右，不使用反向蛇形。跨行边使用正交折线，从上一行节点底部进入行间空隙，水平连接到下一行起点上方，再垂直进入目标节点顶部；`Source` 和 `Buffer` 是两个独立 group，不合并成同一行。

## 5. Graph JSON

接口：

```text
GET /api/perf/perception-flow?device_id=<device-id>
```

无 `device_id` 的请求仅用于获取当前设备列表和空态信息；前端收到列表后立即选择第一台设备，并只展示单设备图。接口不接收 Perf 历史时间窗口参数。

顶层结构：

```json
{
  "schema_version": 1,
  "generated_at": 0,
  "process_started_at": 0,
  "stale_after_sec": 30,
  "scope": {
    "device_id": "camera-1",
    "room_name": "Living Room"
  },
  "graph": {
    "id": "perception-flow",
    "label": "Perception Flow",
    "direction": "LR",
    "layout": {
      "rank_separation": 48,
      "node_separation": 24
    },
    "groups": [],
    "nodes": [],
    "edges": []
  },
  "summary": {
    "latest_trace_id": null,
    "latest_device_trace_id": null,
    "observed_at": null,
    "freshness": "unknown",
    "devices": []
  }
}
```

### 5.1 GraphMedia

```json
{
  "width": {"value": 1920, "source": "observed"},
  "height": {"value": 1080, "source": "observed"},
  "fps": {"value": 3.0, "source": "observed"},
  "frame_count": {"value": 12, "source": "observed"},
  "duration_ms": {"value": 4000, "source": "observed"}
}
```

缺少实际观测时对应 datum 的 `value` 为 `null` 且 `source` 为 `unknown`。配置值与观测值不能互相回退。

### 5.2 节点

节点包含稳定 ID、英文 label、group、rank/order、状态、freshness、input/output media、metrics、warnings 和 evidence。

节点卡片展示：

- 名称；
- kind 与当前状态；
- `In`、`Out` 或 `In/Out` 媒体摘要；
- 最多两个最重要的 metric。

当 input/output 完全相同时合并显示为 `In/Out`，避免重复文字。最早可观测的 `Decoded Camera Frames` 节点也将实际解码媒体作为 `input` 和 `output` 展示，明确首个可观测边界的尺寸和帧率。Tracking 节点由此显示源尺寸媒体，同时用 metric 显示 Detector/ReID 模型尺寸。Gate 节点将 `check_frame_size` 以 `Diff Image Size` 作为优先展示 metric，说明实际用于帧差检查的图像尺寸。

配置类 metric 的显示 label 使用简短名称，例如 `Window Size`、`Max Windows`、`Config FPS` 和 `Config Short Edge`；协议中的 `source=configured` 与 `role=config` 保留不变。

### 5.3 边

边包含 source、target、active、状态、freshness 和可选 media。媒体数据保留在协议中，用于校验节点边界和后续通用消费；当前 SVG 不在普通连线上重复渲染媒体摘要，避免五节点行的标签互相覆盖。尺寸、FPS 和帧数统一展示在节点 `In/Out` 区域。

语义标签只用于非普通边：

- `Gate blocked`
- `Audio bypass`
- `Inactive video path`

普通 `Frames` 标签不重复显示，避免与媒体摘要重叠；Gate 放行后的普通流和 `Omni Request → Result Complete` 均使用该普通语义，不在连线上显示额外文字。

### 5.4 状态与 freshness

状态枚举：

```text
ok | warning | skipped | backpressure | error | inactive | unknown
```

freshness 枚举：

```text
fresh | stale | unknown
```

stale 外观优先于旧的 error/backpressure 外观，避免把历史故障显示为当前故障；`last_observed_status` 保留最后状态。

## 6. 运行时诊断

每次 realtime cycle 为参与设备构造 `PerDeviceFlowDiagnostics`。诊断对象随 Pipeline 结果返回到 processor，但从序列化模型中排除。

processor 将本轮结果原子合并到 `PerceptionFlowSnapshotStore`：

- 每台设备只保留最新快照；
- 设备删除、会话重置或服务停止时清理对应快照；
- store 只绑定在当前进程；
- Graph Adapter 读取副本，避免接口读取中间写入状态。

媒体 transform 和 encode 在 task-local `ContextVar` scope 中记录；没有 active scope 时 recorder 为 no-op，不影响 on-demand 或其它调用路径。

## 7. 数据来源

| 数据 | 来源 |
| --- | --- |
| 解码帧尺寸 | 当前窗口首个实际 frame shape |
| 源帧率 | 窗口帧数 / 实际窗口时长 |
| Pipeline 输出帧率 | 抽样后帧数 / 实际窗口时长 |
| Gate 检查图尺寸 | `run_gate()` 返回的本次实际检查图 shape |
| Detector 输入尺寸 | 当前 Detector ONNX session input shape |
| ReID 输入尺寸 | 当前 ReID ONNX session input shape |
| Omni 帧率与帧数 | `_downsample_for_omni()` 结果 |
| Transform 输出尺寸 | 实际 resize 使用的目标尺寸和输出帧数 |
| Encode 输出 | 实际 `LocalMediaInfo` |
| 缓冲深度与丢弃 | `StreamBuffer` 在锁内采样的数据 |

Detector/ReID session 声明的固定输入尺寸使用 `source=model_fixed`，它表示模型内部输入，不表示视频主链媒体尺寸。禁止用默认模型尺寸、固定相机尺寸、配置 FPS 或 CSS 常量填补未知运行时数据。

## 8. 前端布局

`PerfPipelineFlow` 负责：

- 独立刷新运行时接口；
- 缓存设备列表；
- 默认选择第一台设备；
- 处理 loading、error、protocol warning 和 freshness 文案；
- 将完整 Graph JSON 传给 `GenericGraphViewer`；
- 在 Perf 页面中位于实时率卡片之后。

`GenericGraphViewer` 负责：

- 根据 `group/rank/order/direction` 布局；
- group 作为原子单元换行；
- 每行最多五个节点；
- 所有行从左向右；
- 节点卡片使用较宽的固定 SVG 布局宽度以容纳多行标题和 metric，外层通过 `viewBox` 缩放到卡片宽度；
- 节点高度根据标题、媒体摘要和 metric 的实际换行行数计算，全图所有节点统一使用其中的最大高度；
- 整张 SVG 使用 `viewBox` 自适应卡片宽度；
- 标题、边标签和 metric 使用 SVG `tspan` 完整多行显示，不使用省略号截断；
- 同行边从卡片侧面水平连接；跨行边使用行间正交折线，箭头颜色继承对应连线颜色；
- 不渲染详情面板，不绑定点击或键盘动作。

布局不能根据 `gate.visual`、`identity.track` 等业务 ID 写分支。

## 9. 验收条件

1. 默认展示第一台设备，不出现“全部设备”选项。
2. 图完整显示且没有横向滚动条。
3. 默认拓扑为四行，`Source`、`Buffer`、`Processing`、`Output` group 不被拆开，每行从左向右。
4. Ready Queue 到 Sampling 的边显示抽样前窗口媒体；Sampling 到 Gate 显示抽样后媒体。
5. Gate 检查图尺寸只显示为内部 metric，Gate 输出保持源尺寸。
6. Detection、Tracking、ReID 合并为一个节点；主媒体保持源尺寸，模型尺寸作为 metric。
7. Gate skip 时后续边 inactive 且不显示伪造媒体。
8. Media Transform 节点清楚显示实际输入和输出尺寸，Media Encoding 输入与其输出一致。
9. Omni Request 到 Result Complete 不携带视频媒体。
10. 未观测尺寸或 FPS 显示 unknown，不使用固定值填充。
11. 首个 `Decoded Camera Frames` 节点展示 `In/Out` 媒体摘要；Gate 卡片展示实际 diff 图尺寸；Gate 放行边和 `Omni Request → Result Complete` 不显示额外连线文字。
12. 卡片外壳 i18n，Graph 内容保持英文。
13. 新增诊断不持久化，进程重启后清空。
14. 感知流水线卡片位于实时率卡片之后。
15. 所有节点卡片使用全图内容计算出的最大高度，卡片高度统一且同一行箭头保持水平流向。
16. 每行末节点到下一行首节点的连线不斜切目标卡片或 group 标题，使用清晰的正交跨行路径。
