/**
 * 前端类型定义。
 *
 * 与 backend Pydantic schema 对齐但解耦——backend 返回什么字段，这里就声明什么。
 * 后续接通 openapi-typescript 自动生成时，这一份会被替换或退化成 superset。
 *
 * 所有面向 UI 的"语义类型"（HomeStatus / Person / Device / ActivityEvent）都包装过
 * 一层人话翻译——见 real.ts 里的 cleanRoom / cleanDeviceName / zhLabel / zhEnumValue
 * 等本地辅助函数，不让 UI 直接接触 backend 的工程语义。
 */

// ── 状态条聚合（home_status 聚合接口） ─────────────────────────────
export interface HomeStatus {
  // 米家
  miot: {
    bound: boolean;
    accountName?: string;
    /** 小米账号头像 CDN URL（来自 backend miot.user_info.icon），未绑时缺省 */
    userIcon?: string;
    /** 小米账号 uid（住户 AccountMenu 展示用），未绑时缺省 */
    userUid?: string;
    devicesCount: number;
    roomsCount: number;
  };
  // 感知（"它正在替你看家" / "它在休息" / "还没准备好"）
  perception: {
    running: boolean;
    /** engine 是否真就绪（running=true 但 ready=false 时通常是模型缺失等阻塞） */
    ready: boolean;
    /** engine.message 的人话（仅 ready=false 时有意义；engine.status 字段当前未透出） */
    engineMessage?: string;
  };
  /** 最多投喂给 miloco 的摄像头数（后端 MAX_ENABLED_CAMERAS，唯一来源）。 */
  maxEnabledCameras: number;
}

// ── 家人 ────────────────────────────────────────────────────
// TODO(miloco): 等 backend 暴露 person presence 接口后再加 last_seen / current_area
//   等 backend 接通后替换
export interface Person {
  id: string;
  name: string;
  role?: string; // 家庭角色（爸爸/妈妈等，可空）
  faceEnrolled: boolean;
  voiceEnrolled: boolean;
  // 头像底色（personPalette 选）
  avatarHue: number; // 0..5
}

// ── 设备 ────────────────────────────────────────────────────
export type DeviceCategory =
  | "light"
  | "aircond"
  | "purifier"
  | "fan"
  | "curtain"
  | "lock"
  | "tv"
  | "camera"
  | "other";

export interface DeviceProperty {
  iid: string; // 内部用，UI 不展示
  label: string; // 翻译后的中文标签（"温度" / "档位"）
  type: "switch" | "number" | "enum" | "readonly";
  value: number | string | boolean;
  // 控件参数
  unit?: string; // °C / %
  min?: number;
  max?: number;
  step?: number;
  options?: { label: string; value: string | number }[]; // for enum
}

export interface Device {
  did: string;
  name: string;
  category: DeviceCategory;
  room: string;
  online: boolean;
  // 概览状态文本（"开着" / "26°C 制冷" / "睡眠档"）
  statusText: string;
  // 是否危险设备（门锁/燃气/烟雾），需要二次确认
  dangerous: boolean;
  // 主开关 prop（单按钮直控；为 null 时只能从 sheet 进）
  mainSwitch?: { iid: string; current: boolean };
  // 完整属性（QuickSheet 用）
  props: DeviceProperty[];
}

export interface Scene {
  id: string;
  name: string;
}

// ── 活动事件(meaningful_events)─────────────────────────────
// 数据源:GET /api/events(perception/events_router).
// 一次感知推理 = 一行 event;同窗口 N 摄像头合并 1 行,device_ids 记录参与摄像头.
// `text` 字段是 agent webhook 收到的同一段聚合文本(单源真值,B2 约束).
//
// has_rule_hit / has_suggestion / has_asr 只用于诊断,**UI 不渲染 badge**(B14:
// 三类信息已在 text 里以章节形式呈现 — "[感知引擎]语音提醒：" / "[感知引擎]事件提醒：" / "[感知引擎]规则提醒：").
export interface ActivityEvent {
  id: string; // = event_id;保留为 React key 兼容
  timestamp: number; // Unix ms UTC(注意:与 backend events_router 一致,不再是 ISO)
  text: string; // 聚合 agent 视图文本
  has_rule_hit?: boolean;
  has_suggestion?: boolean;
  has_asr?: boolean;
  /** 成功落 clip 的 device 数(0 ~ len(device_ids);每 device 1 个 mp4/m4a 文件);
   *  字段名沿用历史,语义现是 device 数而非帧数.0 表示 metadata-only(磁盘满 / 落盘失败) */
  snapshot_count: number;
  /** 参与本次推理的 device_id 列表;clip URL 拼接用(eventClipUrl) */
  device_ids: string[];
  /** rule_id → rule_name 映射;UI 渲染规则提醒时把 [rule_id] 替换成 rule_name */
  rule_names?: Record<string, string>;
  /** clip 容器类型,服务端 stat 落盘文件后缀计算:
   *   "mp4" = 视频路径(H264+AAC,UI 显 🎬)
   *   "m4a" = audio-only 路径(纯 AAC,UI 显 🎤 音频)
   *   undefined / null = 未落盘(老 event / metadata-only / 已被 cleanup 清掉) */
  clip_kind?: "mp4" | "m4a" | null;
  /** omni_trace.json.gz 是否存在;前端据此决定是否显示反馈按钮 */
  has_trace?: boolean;
  /** 该事件是否已有反馈打包 */
  has_feedback?: boolean;
  feedback_pack_path?: string | null;
  feedback_pack_size?: number | null;
}

// ── 家庭档案（home_profile：候选区 / 正式区记忆）─────────────────
// 与 backend miloco/home_profile/schema.py::Entry 对齐。member_* 是与人绑定的
// 个人记忆（按 subject_id 归到成员），family/space/device 是与人无关的家庭信息。
export type HomeEntryType =
  | "member_persona"
  | "member_health"
  | "member_routine"
  | "member_entertain"
  | "member_preference"
  | "family"
  | "space"
  | "device";

export type HomeEntrySource = "observed" | "user_told";

export interface HomeEntry {
  id: string;
  type: HomeEntryType;
  // person_id（成员记忆）/ null（与人无关）。subject_name 是兜底展示名。
  subjectId?: string | null;
  subjectName?: string | null;
  content: string;
  confidence: number;
  evidenceCount: number;
  firstSeen: string;
  lastSeen: string;
  source: HomeEntrySource;
  // 观察证据原文（详情卡展开看「它据什么记下的」）；user_told 条目通常为空。
  evidenceLog?: string[];
  // 仅正式区条目有 archived（被 commit 归档则不渲染进 md，但仍保留）
  archived?: boolean;
}

export interface HomeEntries {
  profile: HomeEntry[];
  candidates: HomeEntry[];
  // 候选区里「已达提升门槛」的条目 id 集合，UI 给个「可提升」提示
  readyToPromote: string[];
}

// 它对家的了解
//   v3 删了 habits / hints / recognizedObjects——这些都是 miloco 没立项的产品概念
//   （习惯归纳 / anomaly suggestion / 物体识别去重），等真有了再加

// ── 摄像头（用于实时画面区块；watch.html 自身有完整列表） ───
export interface PerceptionCamera {
  did: string;
  name: string;
  channel: number;
  roomName?: string;
}

// ── 米家 scope 摄像头（含禁用 / 离线，控件配置用） ─────────────
// 来源：GET /api/miot/scope/cameras（in_use=false 即停用该摄像头的感知）。
// PerceptionCamera 是「当前 perception 在订阅」的子集（含 channel 用于播放），
// ScopeCamera 是「米家账号下全集」（含已禁用 / 离线，用于显示开关）。
// 渲染卡片时 ScopeCamera 是主列表，channel 通过 did 从 PerceptionCamera 字典查。
export interface ScopeCamera {
  did: string;
  name: string;
  // 米家分配的房间名（"客厅" / "卧室" / ...）。多摄像头家庭里 name 常是
  // "小米智能摄像机 2 代"等泛称，靠 roomName 才能区分。米家未分房间时为空。
  roomName?: string;
  // 三个正交可用性指标（替代旧的一把揉 isOnline）：
  cloudOnline: boolean; // 米家云端在线
  lanReachable: boolean; // 局域网可达（拉流前提）
  // 镜头开关（camera-control:on）。true=镜头开启 / false=镜头关闭(隐私·遮挡) /
  // null=该机型无开关属性或读取失败（未知）。
  awake: boolean | null;
  // 当下真正开启（后端 = 在活跃集里：默认开·未拉黑 + 三态满足 + 上限≤4）。离线/
  // 不可达/镜头关的相机 inUse=false，不显示为开；超上限的也不算开。
  inUse: boolean;
  // 拾音「存储偏好」（PUT /api/miot/scope/cameras/voice）。false = mic-off：该相机
  // 声音完全不被处理（引擎入口剥离音频，不转写、不上云）。与 inUse 正交：
  // 生效态 = inUse && voiceInUse（关掉相机感知时拾音自动失效，但偏好保留、不落库）。
  voiceInUse: boolean;
  connected: boolean;
}

// 相机是否满足「开启感知」的全部条件：云端在线 && 局域网可达 && 镜头未关。
// awake=null(机型无开关/未知) 不算不可用（与后端放行口径一致）。
export function cameraAvailable(c: ScopeCamera): boolean {
  return c.cloudOnline && c.lanReachable && c.awake !== false;
}

// ── 米家家庭接入范围(scope.homes)─────────────────────────────────
// PUT /api/miot/scope/homes 切 in_use 标记后,GET 同一接口拿到的全集仍是这个 shape。
export interface ScopeHome {
  homeId: string;
  homeName: string;
  inUse: boolean;
}

// ── 多家庭 ─────────────────────────────────────────────────
// HomeId 仅作 useAsync 缓存 key 占位用("primary"),真 home_id 走 ScopeHome.homeId
// (已接通 PUT/GET /api/miot/scope/homes,backend 多家庭接入范围已上线)。
export type HomeId = string;

// ── 用量统计（📊 用量 tab）─────────────────────────────────────
// 数据来自 backend admin token-usage 接口（仅 omni/MiMo 调用有计费）：
//   today      → GET /api/admin/token-usage/buckets  （服务端按桶聚合）
//   week/month → GET /api/admin/token-usage/daily     （按 date/model/type 聚合）
// 客户端在 src/api/real.ts::realGetUsageStats 里折算成下面的结构。
// 注意：video/audio/cache 都是 input 的子集（不叠加）；总量 = input + output。

export type UsagePeriod = "today" | "week" | "month";

/** 调用类型：realtime=感知循环驱动，on_demand=用户主动发起。 */
export type UsageCallType = "realtime" | "on_demand";

/** token 拆分。cache/video/audio 均为 input 的子集。 */
export interface TokenBreakdown {
  /** 总 prompt（含全部模态）。 */
  input: number;
  /** completion 输出。 */
  output: number;
  /** 命中缓存的 prompt token（⊆ input）。 */
  cache: number;
  /** 视频 token（⊆ input）。 */
  video: number;
  /** 音频 token（⊆ input）。 */
  audio: number;
}

/** 按某维度（调用类型 / 模型）的一行聚合。 */
export interface UsageGroup {
  /** 维度取值：调用类型名 或 模型名。 */
  key: string;
  calls: number;
  /** input + output。 */
  tokens: number;
  breakdown: TokenBreakdown;
}

/** 明细表的一行：model × type 组合。 */
export interface UsageRow {
  model: string;
  type: UsageCallType;
  calls: number;
  /** input + output。 */
  tokens: number;
  breakdown: TokenBreakdown;
}

export interface UsageStats {
  period: UsagePeriod;
  /** input + output 之和。 */
  total_tokens: number;
  /** 调用次数。 */
  calls: number;
  /** 全周期 token 拆分汇总。 */
  totals: TokenBreakdown;
  /** 按调用类型聚合（realtime / on_demand），按 tokens 降序。 */
  by_type: UsageGroup[];
  /** model × type 明细行，按 tokens 降序。 */
  rows: UsageRow[];
  /**
   * 时间序列。today 桶数随 bin 粒度变化（10分=144 / 1时=24 / 3小时=8，默认 1 时）且铺满整天；
   * week=7 天，month=30 天。ts 是 ISO 8601。
   */
  timeline: { ts: string; tokens: number }[];
}

// ── omni 模型配置（在「模型」页内读/写，支持多档案切换） ──────────────
/** 一套 omni 配置；api_key 仅给打码值（前3…后4），永不回全文。 */
export interface OmniModelConfig {
  /** 档案显示名（可选）；为空时前端回退为 model · 域名。 */
  label: string;
  model: string;
  base_url: string;
  /** 打码后的 api_key，如 "sk-…79a8"；无 key 时为空串。 */
  api_key_masked: string;
  /** 是否已配置 api_key。 */
  has_key: boolean;
}

/** 已保存的配置档案（label 为唯一标识），active 标记是否为当前生效。 */
export interface OmniProfile extends OmniModelConfig {
  active: boolean;
}

/** GET /omni-config 返回：当前生效 active + 已存档案 profiles。 */
export interface OmniConfigState {
  active: OmniModelConfig;
  profiles: OmniProfile[];
}

/** 定位一套档案(档案名 = 唯一 id)。 */
export interface OmniProfileRef {
  label: string;
}

/** 拉取模型列表结果。 */
export interface OmniModelsResult {
  ok: boolean;
  /** 失败时的机器码(no_key/unreachable/bad_key/http_error);缺省回退 message。 */
  code?: string;
  models: string[];
  message?: string;
}

/** 提交给后端的 omni 配置(保存/测试)；档案名 label = 唯一 id。 */
export interface OmniConfigUpdate {
  /** 档案名(唯一 id，非空)。 */
  label: string;
  model: string;
  base_url: string;
  /** 省略 / 留空 = 沿用该档案原 key（不被打码值覆盖）。 */
  api_key?: string;
  /** 正在编辑的档案原名（支持改名/定位）；省略=新增。 */
  original_label?: string;
  /** 是否同时设为当前生效；省略=后端默认 true。「保存」传 false（激活走列表的「启用」）。 */
  activate?: boolean;
}

/** 测试连接结果：ok=true 即连通；否则 message 给出原因（Key 无效 / 不可达 / 模型不存在等）。 */
export interface OmniTestResult {
  ok: boolean;
  /** 机器码,前端按它本地化(ok/bad_key/not_found/rejected_authed/unreachable/no_key/http_error);缺省回退 message。 */
  code?: string;
  status?: number;
  latency_ms?: number;
  message: string;
}

// ── 性能 tab（observability） ────────────────────────────────────
// 直接对接 backend /api/stats 和 /api/traces，无 Normal 包装。

/** 时间窗口选项。映射到 since/until ms timestamp。 */
export type PerfWindow = "1h" | "6h" | "24h" | "3d";

/** 时间桶粒度，对应 backend stats 的 bucket 参数。 */
export type PerfBucket = "1m" | "5m" | "1h" | "1d";

/** /api/stats?metric=summary 返回。 */
export interface PerfSummary {
  cycle_count: number;
  /** 窗口内被 buffer clear 丢的窗口数。cycle_count + dropped_count = 应处理总数。 */
  dropped_count: number;
  skip_rate: number;
  drop_rate: number;
  omni_error_rate: number;
  p95_rtf_e2e: number;
  /** P95 of rtf_omni 仅 omni 成功 cycle(omni_error_count=0)。看 omni 单段实时性,
   *  跟 p95_rtf_e2e(端到端含等待)对比反映等待时间占比。 */
  p95_rtf_omni: number;
  agent_call_count: number;
  window: { since: number; until: number };
}

/** /api/stats?metric=drop_series 单个点。绝对值,不是比率。 */
export interface PerfDropPoint {
  ts: number;
  dropped: number;
  overflow_count: number;
  cycle_count: number;
}

/** /api/stats?metric=gate_pass_rate 单个点。返回的是通过率(0-1),前端转过滤率。
 *  字段可为 null:densifyByBucket 给空 bucket 填的 null,折线在该点断开。 */
export interface PerfGatePoint {
  ts: number;
  overall: number | null;
  video: number | null;
  audio: number | null;
}

/** /api/stats?metric=gate_score_percentiles 单 device 一行。
 *
 * Gate 真实评估的打分(visual_change_score / audio_energy_level)按 device 聚合
 * 算 P50/P75/P90/P99,用来对照配置阈值看实际打分分布。NULL 行(on-demand
 * bypass / cycle 异常 fallback)在后端已过滤,这里看不到。
 *
 * 单 device 全无数据时 count=0、各 percentile 为 null。
 */
export interface PerfGateScorePcts {
  p50: number | null;
  p75: number | null;
  p90: number | null;
  p99: number | null;
  count: number;
}
export interface PerfGateScoreRow {
  device_id: string;
  room_name: string | null;
  video: PerfGateScorePcts;
  audio: PerfGateScorePcts;
}

/** /api/stats?metric=latency_percentiles 单个点。ts 是 bucket 起点 ms timestamp。
 *  字段可为 null:densifyByBucket 给空 bucket 填的 null,折线在该点断开。 */
export interface PerfLatencyPoint {
  ts: number;
  p50: number | null;
  p75: number | null;
  p95: number | null;
  p99: number | null;
}

/** /api/stats?metric=rtf_series 单个点。ts 是 ms timestamp。 */
export interface PerfRtfPoint {
  ts: number;
  rtf: number | null;
  rtf_e2e: number | null;
  rtf_stream_e2e: number | null;
  rtf_pipeline: number | null;
  rtf_omni: number | null;
  /** 仅成功 cycle (omni_error_count=0) 的 rtf_e2e 均值;跟 rtf_e2e 差值反映失败拖累。 */
  rtf_e2e_ok: number | null;
  /** 仅成功 cycle (omni_error_count=0) 的 rtf_omni 均值;跟 rtf_omni 差值反映失败拖累。 */
  rtf_omni_ok: number | null;
}

/** /api/stats?metric=omni_error_series 单个点。三类堆叠用。 */
export interface PerfOmniErrorPoint {
  ts: number;
  rate_limit: number;
  timeout: number;
  other: number;
}

/** /api/stats?metric=stage_percentiles 单阶段。 */
export interface PerfStageStat {
  avg: number;
  p50: number;
  p75: number;
  p95: number;
  p99: number;
  sample_size: number;
}

export type PerfStageKey =
  | "decode_ms"
  | "collect_ms"
  | "convert_ms"
  | "gate_ms"
  | "identity_ms"
  | "omni_ms"
  | "log_ms";

export type PerfStagePercentiles = Record<PerfStageKey, PerfStageStat>;

/** /api/traces 列表项。字段对齐 traces_v 视图。仅保留 UI 用到的列。 */
export interface PerfTraceRow {
  trace_id: string;
  timestamp: number;
  device_count: number | null;
  skipped: number;
  /** 1=video gate 通过(有有效视频),0=被过滤掉。 */
  gate_video_pass: number;
  /** 1=audio gate 通过,0=被过滤掉。 */
  gate_audio_pass: number;
  /** 1=hold 滞回拉起本窗 packet(visual 未变化但距上次通过 ≤ hold_duration_sec);
   *  与 gate_video_pass 互斥,可与 gate_audio_pass 共存,omni 路由仍走 video。 */
  gate_hold_pass: number;
  cycle_total_ms: number | null;
  pipeline_total_ms: number | null;
  window_duration_ms: number | null;
  decode_ms: number | null;
  collect_ms: number | null;
  convert_ms: number | null;
  gate_ms: number | null;
  identity_ms: number | null;
  omni_ms: number | null;
  log_ms: number | null;
  omni_call_count: number | null;
  omni_error_count: number;
  /** 非 omni 异常 cycle 的错误摘要(类型 + 首行 + 截断 160 字符);null 表示正常 cycle。 */
  cycle_error_msg: string | null;
  /** traces_v 视图派生:EXISTS(SELECT 1 FROM agent_runs WHERE trace_id=t.trace_id)。 */
  has_agent_turn: number;
  rtf: number | null;
  rtf_pipeline: number | null;
  rtf_e2e: number | null;
  rtf_stream_e2e: number | null;
  rtf_omni: number | null;
  dropped_windows_total: number;
  overflow_count_total: number;
}

/** /api/agent_runs 列表项。每次 agent turn 一行。 */
export interface PerfAgentRun {
  run_id: string;
  trace_id: string;
  timestamp: number;
  /** 调用来源:rule | interaction | suggestion。 */
  source: string;
  query: string | null;
  webhook_rtt_ms: number | null;
  duration_ms: number | null;
  llm_call_count: number | null;
  tool_call_count: number | null;
  llm_total_ms: number | null;
  tool_total_ms: number | null;
  tool_max_ms: number | null;
  slowest_tool_name: string | null;
  /** 1=成功 0=失败 null=未拿到 meta(timeout)。 */
  success: number | null;
  error_count: number | null;
  error_msg: string | null;
  jsonl_path: string | null;
}

// === Memory monitor (/monitor/memory + /monitor/memory/series) ===

export interface MemoryCategory {
  name: string;
  rss_kb: number;
  count: number;
}

export interface PyTypeStats {
  qualname: string;
  count: number;
  size_kb: number;
}

export interface PyHeapStats {
  total_objects: number;
  total_size_kb: number;
  types: PyTypeStats[];
  other_size_kb: number;
  other_count: number;
}

export interface MemorySnapshot {
  ts: number;
  // smaps 段：smaps 不可用时整段字段全部缺失
  total_rss_kb?: number;
  categories?: MemoryCategory[];
  other_rss_kb?: number;
  other_count?: number;
  // py_heap 段：首次采集前缺失
  python_heap?: PyHeapStats;
}

export interface MemoryPoint {
  ts: number;
  rss_kb: number;
  py_objects: number;
  py_size_kb: number;
}

export interface MemorySeries {
  ts_start: number | null;
  ts_end: number | null;
  interval_s: number;
  points: MemoryPoint[];
}

// === Monitor meta (/monitor/) ===

export interface MonitorMeta {
  node_count: number;
  uptime_s?: number;
  uname?: string;
  resources?: Record<string, unknown>;
}

// ── 任务（task / task_record 摘要视图，GET /api/tasks/summary?window=day）──
export type TaskStatus = "active" | "paused";
export type TaskRecordKind = "progress" | "duration" | "event";

export interface TaskRecordSummary {
  kind: TaskRecordKind;
  completed: boolean;
  // duration 当前计时段（非 duration 恒 null）
  activeSession: { startedAt: string; elapsedMinutes: number } | null;
  // 按 kind 形态不同的派生量，原样透传 backend snake_case：
  //   progress: target / current / unit / remaining / progress_pct
  //   duration: target_minutes / accumulated_minutes_today / remaining_minutes
  //   event:    count_total / count_today / last_at
  derived: Record<string, unknown>;
}

export interface Task {
  taskId: string;
  description: string;
  status: TaskStatus;
  createdAt: string;
  record: TaskRecordSummary | null;
}
