/**
 * API 出口——直连 backend。
 *
 * 注:旧 `_mock/register.ts` + `vite.config.ts::mockAutoInject` mock 注入通道
 * 已彻底删除(包含 vite plugin 函数)。如需 sessionStorage 假数据走 UI 调试,
 * 从 git 历史拉回 plugin + 重起 vite serve。
 */

import * as realImpl from "./real";
import { apiFetch } from "./client";
import type {
  ActivityEvent,
  Device,
  HomeEntries,
  HomeEntryType,
  HomeId,
  HomeStatus,
  MemorySeries,
  MemorySnapshot,
  MonitorMeta,
  PerceptionCamera,
  PerfBucket,
  PerfDropPoint,
  PerfGatePoint,
  PerfGateScoreRow,
  PerfLatencyPoint,
  PerfAgentRun,
  PerfOmniErrorPoint,
  PerfRtfPoint,
  PerfStagePercentiles,
  PerfSummary,
  PerfTraceRow,
  PerfWindow,
  Person,
  Scene,
  ScopeCamera,
  ScopeHome,
  Task,
  UsagePeriod,
  UsageStats,
  OmniConfigState,
  OmniConfigUpdate,
  OmniProfileRef,
  OmniTestResult,
  OmniModelsResult,
} from "@/lib/types";
export type { ScopeHome };

const impl: typeof realImpl = realImpl;

// 当前 backend 多家庭未上线,前端 homeId 永远 "primary"。isPrimary 永真,
// 但保留兜底分支让未来 backend 接通多家庭时直接挂 listScopeHomes 路径。
const PRIMARY: HomeId = "primary";
const isPrimary = (homeId: HomeId | undefined): boolean =>
  !homeId || homeId === PRIMARY;

// ── 米家账号绑定（OAuth 三步：bind → 用户打开 oauth_url → authorize 提 code/state） ─
export async function bindMiot(): Promise<{ oauthUrl: string }> {
  return impl.realBindMiot();
}

export async function authorizeMiot(code: string, state: string): Promise<void> {
  return impl.realAuthorizeMiot(code, state);
}

export async function unbindMiot(): Promise<void> {
  return impl.realUnbindMiot();
}

// ── 状态条 ────────────────────────────────────────────────
export async function getHomeStatus(homeId?: HomeId): Promise<HomeStatus> {
  if (!isPrimary(homeId)) {
    return {
      miot: { bound: false, devicesCount: 0, roomsCount: 0 },
      perception: { running: false, ready: false },
      maxEnabledCameras: 4,
    };
  }
  return impl.realHomeStatus();
}

// ── 家人 ──────────────────────────────────────────────────
export async function listPersons(homeId?: HomeId): Promise<Person[]> {
  if (!isPrimary(homeId)) return [];
  return impl.realListPersons();
}

export async function createPerson(payload: {
  name: string;
  role?: string;
}): Promise<Person> {
  return impl.realCreatePerson(payload);
}

export async function updatePerson(
  id: string,
  payload: { name?: string; role?: string },
): Promise<void> {
  return impl.realUpdatePerson(id, payload);
}

export async function deletePerson(id: string): Promise<void> {
  return impl.realDeletePerson(id);
}

export async function enrollPersonSample(
  personId: string,
  imageBase64: string,
): Promise<void> {
  return impl.realEnrollPersonSample(personId, imageBase64);
}

// ── 家庭档案（home_profile）────────────────────────────────
// UI 只调这组语义函数；snake_case 的 op 构造全收在 real.ts，组件不碰。
function today(): string {
  return new Date().toISOString().slice(0, 10);
}

export async function listHomeEntries(
  homeId?: HomeId,
  target: "profile" | "candidates" | "both" = "both",
): Promise<HomeEntries> {
  if (!isPrimary(homeId)) {
    return { profile: [], candidates: [], readyToPromote: [] };
  }
  return impl.realListHomeEntries(target);
}

// 住户手动新增一条正式记忆（user_told / confidence 满格由 user_edit 保证）。
export async function addHomeEntry(input: {
  type: HomeEntryType;
  content: string;
  subjectId?: string | null;
  subjectName?: string | null;
}): Promise<void> {
  return impl.realProfileWrite([
    {
      op: "add",
      entry: {
        type: input.type,
        content: input.content,
        subject_id: input.subjectId ?? null,
        subject_name: input.subjectName ?? null,
        source: "user_told",
        confidence: 1.0,
      },
    },
  ]);
}

// 住户直编正式记忆（仅覆盖显式提供的字段）。
// subjectId/subjectName 用于把「未关联成员」的记忆手动归到某个家人。
export async function updateHomeEntry(
  id: string,
  patch: {
    type?: HomeEntryType;
    content?: string;
    subjectId?: string | null;
    subjectName?: string | null;
  },
): Promise<void> {
  return impl.realProfileWrite([
    {
      op: "update",
      id,
      edit: {
        ...(patch.type !== undefined && { type: patch.type }),
        ...(patch.content !== undefined && { content: patch.content }),
        ...(patch.subjectId !== undefined && { subject_id: patch.subjectId }),
        ...(patch.subjectName !== undefined && {
          subject_name: patch.subjectName,
        }),
      },
    },
  ]);
}

export async function deleteHomeEntry(id: string): Promise<void> {
  return impl.realProfileWrite([{ op: "delete", id }]);
}

// 确认候选 → 提升为正式（backend 自动从候选区移除该条）。
export async function confirmCandidate(candidateId: string): Promise<void> {
  return impl.realProfileWrite([{ op: "add", from: candidateId }]);
}

// 忽略候选 → 直接从候选区删除。
export async function ignoreCandidate(candidateId: string): Promise<void> {
  return impl.realCandidateWrite([
    { op: "delete", id: candidateId, date: today() },
  ]);
}

export async function commitHomeProfile(): Promise<void> {
  return impl.realCommitHomeProfile();
}

// ── 任务（miloco 任务管理）──────────────────────────────────
export async function listTasks(homeId?: HomeId): Promise<Task[]> {
  if (!isPrimary(homeId)) return [];
  return impl.realListTasks();
}

export async function setTaskEnabled(
  taskId: string,
  enabled: boolean,
): Promise<void> {
  return impl.realSetTaskEnabled(taskId, enabled);
}

export async function deleteTask(taskId: string): Promise<void> {
  return impl.realDeleteTask(taskId);
}

// ── 设备 ──────────────────────────────────────────────────
export async function listDevices(homeId?: HomeId): Promise<Device[]> {
  if (!isPrimary(homeId)) return [];
  return impl.realListDevices();
}

export async function controlDeviceProp(
  did: string,
  iid: string,
  value: number | string | boolean,
): Promise<void> {
  return impl.realControlDeviceProp(did, iid, value);
}

// ── 场景 ──────────────────────────────────────────────────
export async function listScenes(homeId?: HomeId): Promise<Scene[]> {
  if (!isPrimary(homeId)) return [];
  return impl.realListScenes();
}

export async function triggerScene(id: string): Promise<void> {
  return impl.realTriggerScene(id);
}

// ── 活动 ──────────────────────────────────────────────────
export async function listActivity(
  homeId?: HomeId,
  opts?: { since?: number; before?: number; limit?: number; offset?: number },
): Promise<ActivityEvent[]> {
  if (!isPrimary(homeId)) return [];
  return impl.realListActivity(opts);
}

/** 事件 clip mp4 URL,含 ?token=... query 鉴权(<video> 无法设 Authorization). */
export function eventClipUrl(event_id: string, device_id: string): string {
  return impl.realEventClipUrl(event_id, device_id);
}

/** 订阅 /api/events/stream SSE;返回 unsubscribe. onOpen 重连成功时触发(可选). */
export function subscribeEvents(
  onEvent: (e: ActivityEvent) => void,
  onOpen?: () => void,
): () => void {
  return impl.realSubscribeEvents(onEvent, onOpen);
}

// ── 摄像头 ────────────────────────────────────────────────
// ── 米家多家庭 ────────────────────────────────────────────
export async function listScopeHomes(homeId?: HomeId): Promise<ScopeHome[]> {
  if (!isPrimary(homeId)) return [];
  return impl.realListScopeHomes();
}

export async function switchScopeHome(homeId: string): Promise<void> {
  return impl.realSwitchScopeHome(homeId);
}

export async function listScopeCameras(homeId?: HomeId): Promise<ScopeCamera[]> {
  if (!isPrimary(homeId)) return [];
  return impl.realListScopeCameras();
}

// 轻量触发 backend 刷新相机云端 online 状态(节流见 impl,不扰流)。「此刻」页加载
// 相机前调,让"已离线/在线"判断不读陈旧缓存。非主家庭(mock)直接 no-op。
export async function refreshCameraOnline(homeId?: HomeId): Promise<void> {
  if (!isPrimary(homeId)) return;
  return impl.realRefreshCameraOnline();
}

export async function toggleScopeCamera(
  dids: string[],
  inUse: boolean,
): Promise<void> {
  return impl.realToggleScopeCamera(dids, inUse);
}

export async function listCameras(homeId?: HomeId): Promise<PerceptionCamera[]> {
  if (!isPrimary(homeId)) {
    return [];
  }
  return impl.realListCameras();
}

// ── 事件反馈 ────────────────────────────────────────────────
export async function submitEventFeedback(
  eventId: string,
  errorTypes: string[],
  feedbackText: string,
  includeGallery: boolean,
): Promise<{ uploaded: boolean; upload_key?: string; pack_path: string; pack_size_bytes: number }> {
  return impl.realSubmitEventFeedback(eventId, errorTypes, feedbackText, includeGallery);
}

export async function revealFile(path: string): Promise<void> {
  return impl.realRevealFile(path);
}

// ── 让它休息 / 唤醒 ────────────────────────────────────────
// backend 当前只有 stop/start 两态，永久暂停直到手动唤醒，不支持定时恢复。
// 返回值 {resumesAt: null} 给 UI 留住"以后接定时恢复"的形状，但当前永远 null。
// 若未来 backend 加 duration 支持，签名改成 pausePerception(opts?: {until?: Date})
// 让调用方显式传入"暂停到某点"——比 number 形参更清楚语义。
export async function pausePerception(): Promise<{ resumesAt: string | null }> {
  await impl.realPausePerception();
  return { resumesAt: null };
}

export async function resumePerception(): Promise<void> {
  return impl.realResumePerception();
}

// ── 摄像头抓帧（占位 — 等 miloco 提供 snapshot 接口后实现）────
// NOTE: 当前无调用方，保留接口形状供后续接入。不导出，避免误用。
// export async function snapshotCamera(did: string): Promise<{
//   jpegBase64: string; timestamp: string;
// }> { ... }

// ── Token 用量统计（用量 tab）─────────────────────────
export async function getUsageStats(
  period: UsagePeriod = "today",
  binMinutes?: number,
): Promise<UsageStats> {
  return impl.realGetUsageStats(period, binMinutes);
}

// 清空全部用量数据（实时表 + 日聚合，不可恢复）
export async function clearUsageData(): Promise<void> {
  return impl.realClearUsageData();
}

// ── omni 模型配置（「模型」页内读/写，多档案切换）────────────────
export async function getOmniConfig(): Promise<OmniConfigState> {
  return impl.realGetOmniConfig();
}

export async function updateOmniConfig(
  input: OmniConfigUpdate,
): Promise<OmniConfigState> {
  return impl.realUpdateOmniConfig(input);
}

export async function activateOmniConfig(
  ref: OmniProfileRef,
): Promise<OmniConfigState> {
  return impl.realActivateOmniConfig(ref);
}

export async function deleteOmniConfig(
  ref: OmniProfileRef,
): Promise<OmniConfigState> {
  return impl.realDeleteOmniConfig(ref);
}

export async function deactivateOmniConfig(
  ref: OmniProfileRef,
): Promise<OmniConfigState> {
  return impl.realDeactivateOmniConfig(ref);
}

export async function listOmniModels(input: {
  base_url: string;
  api_key?: string;
  label?: string;
}): Promise<OmniModelsResult> {
  return impl.realListOmniModels(input);
}

export async function testOmniConfig(
  input: OmniConfigUpdate,
): Promise<OmniTestResult> {
  return impl.realTestOmniConfig(input);
}

// ── 性能 tab（observability）────────────────────────────
// backend observability/router.py 不走 Normal 包装,直接返回原始 JSON。

const PERF_WINDOW_MS: Record<PerfWindow, number> = {
  "1h": 60 * 60_000,
  "6h": 6 * 60 * 60_000,
  "24h": 24 * 60 * 60_000,
  "3d": 3 * 24 * 60 * 60_000,
};

function windowToSince(w: PerfWindow): number {
  return Date.now() - PERF_WINDOW_MS[w];
}

export async function getPerfSummary(w: PerfWindow): Promise<PerfSummary> {
  const since = windowToSince(w);
  return apiFetch<PerfSummary>(
    `/api/stats?metric=summary&since=${since}`,
  );
}

export async function getPerfRtfSeries(
  w: PerfWindow,
  bucket: PerfBucket,
): Promise<PerfRtfPoint[]> {
  const since = windowToSince(w);
  return apiFetch<PerfRtfPoint[]>(
    `/api/stats?metric=rtf_series&bucket=${bucket}&since=${since}`,
  );
}

export async function getPerfLatencyPercentiles(
  w: PerfWindow,
  bucket: PerfBucket,
): Promise<PerfLatencyPoint[]> {
  const since = windowToSince(w);
  return apiFetch<PerfLatencyPoint[]>(
    `/api/stats?metric=latency_percentiles&bucket=${bucket}&since=${since}`,
  );
}

export async function getPerfGatePassRate(
  w: PerfWindow,
  bucket: PerfBucket,
): Promise<PerfGatePoint[]> {
  const since = windowToSince(w);
  return apiFetch<PerfGatePoint[]>(
    `/api/stats?metric=gate_pass_rate&bucket=${bucket}&since=${since}`,
  );
}

export async function getPerfGateScorePercentiles(
  w: PerfWindow,
): Promise<PerfGateScoreRow[]> {
  const since = windowToSince(w);
  return apiFetch<PerfGateScoreRow[]>(
    `/api/stats?metric=gate_score_percentiles&since=${since}`,
  );
}

export async function getPerfDropSeries(
  w: PerfWindow,
  bucket: PerfBucket,
): Promise<PerfDropPoint[]> {
  const since = windowToSince(w);
  return apiFetch<PerfDropPoint[]>(
    `/api/stats?metric=drop_series&bucket=${bucket}&since=${since}`,
  );
}

export async function getPerfOmniErrorSeries(
  w: PerfWindow,
  bucket: PerfBucket,
): Promise<PerfOmniErrorPoint[]> {
  const since = windowToSince(w);
  return apiFetch<PerfOmniErrorPoint[]>(
    `/api/stats?metric=omni_error_series&bucket=${bucket}&since=${since}`,
  );
}

export async function getPerfStagePercentiles(
  w: PerfWindow,
): Promise<PerfStagePercentiles> {
  const since = windowToSince(w);
  return apiFetch<PerfStagePercentiles>(
    `/api/stats?metric=stage_percentiles&since=${since}`,
  );
}

export async function listPerfTraces(
  w: PerfWindow,
  limit: number = 100,
): Promise<PerfTraceRow[]> {
  const since = windowToSince(w);
  return apiFetch<PerfTraceRow[]>(
    `/api/traces?since=${since}&limit=${limit}`,
  );
}

export async function listPerfAgentRuns(
  w: PerfWindow,
  limit: number = 50,
): Promise<PerfAgentRun[]> {
  const since = windowToSince(w);
  return apiFetch<PerfAgentRun[]>(
    `/api/agent_runs?since=${since}&limit=${limit}`,
  );
}

export async function getMemorySnapshot(): Promise<MemorySnapshot> {
  return apiFetch<MemorySnapshot>(`/api/monitor/memory`);
}

// uname 是进程级静态信息，模块级缓存：整个 web app 生命周期只发一次请求
let _unameLoaded = false;
let _unameValue: string | undefined;
let _unameInflight: Promise<string | undefined> | null = null;

export async function getUname(): Promise<string | undefined> {
  if (_unameLoaded) return _unameValue;
  if (_unameInflight) return _unameInflight;
  _unameInflight = apiFetch<MonitorMeta>(`/api/monitor/`)
    .then((m) => {
      _unameValue = m.uname;
      _unameLoaded = true;
      _unameInflight = null;
      return m.uname;
    })
    .catch((e) => {
      _unameInflight = null;
      throw e;
    });
  return _unameInflight;
}

export async function getMemorySeries(
  w: PerfWindow,
  bucket: PerfBucket,
): Promise<MemorySeries> {
  return apiFetch<MemorySeries>(
    `/api/monitor/memory/series?window=${w}&bucket=${bucket}`,
  );
}
