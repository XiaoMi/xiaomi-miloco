/**
 * 真实 backend 接口包装。
 *
 * 每个函数返回 backend 的 normalized 数据；调用方失败可 catch 后回退 mock。
 * 与 mock 对齐返回类型（types.ts），让 src/api/index.ts 能透明切换。
 */

import { apiFetch, resolveToken } from "./client";
import { authHeaders } from "./register";
import i18n from "@/i18n";
import type {
  ActivityEvent,
  Device,
  DeviceCategory,
  DeviceProperty,
  HomeEntries,
  HomeEntry,
  HomeEntrySource,
  HomeEntryType,
  HomeStatus,
  PerceptionCamera,
  Person,
  Scene,
  ScopeCamera,
  ScopeHome,
  Task,
  TokenBreakdown,
  UsageCallType,
  UsageGroup,
  UsagePeriod,
  UsageRow,
  UsageStats,
  OmniConfigState,
  OmniConfigUpdate,
  OmniProfileRef,
  OmniTestResult,
  OmniModelsResult,
} from "@/lib/types";

// backend NormalResponse 包装：{ code, message, data }
interface Normal<T> {
  code: number;
  message: string;
  data: T;
}

// ── 米家绑定状态 ───────────────────────────────────────────
interface MiotStatus {
  is_bound: boolean;
  user_info?: { uid: string; nickname: string; icon?: string };
  /** 最多投喂数(后端 MAX_ENABLED_CAMERAS)，随状态下发。 */
  max_enabled_cameras?: number;
}

// 太短 / 全标点 / 数字 ID / 全是零宽字符 的 nickname 不算可读名字
function cleanAccountName(n: string | undefined): string | undefined {
  if (!n) return undefined;
  // 剥离零宽 / RTL 控制字符（U+200B-U+200D / U+FEFF）
  const trimmed = n.replace(/[​-‍﻿]/g, "").trim();
  if (trimmed.length < 2) return undefined;
  if (/^[\d\s.\-_*]+$/.test(trimmed)) return undefined;
  return trimmed;
}

interface PerceptionEngineStatus {
  running: boolean;
  engine?: {
    ready: boolean;
    status: string;
    message: string;
  };
  interval_seconds: number;
  today_inference_count: number;
  active_sources: { did: string; name: string }[];
}

interface MiotHome {
  // 注:backend 仍返 home_name 字段(给 cli home_info 缓存用),但前端 family-ui
  // 走 /api/miot/scope/homes 拿 ScopeHome[] 显示家名,不读这里。声明保留兼容
  // backend response shape 但前端不消费,避免误用引入跟 scopeHomes 不一致的家名源。
  home_name?: string | null;
  devices: BackendDevice[];
  areas: { name: string }[];
  scenes: { scene_id: string; scene_name: string }[];
  persons: BackendPerson[];
}

// 首屏 useAsync 会并行触发 realHomeStatus / realListDevices / realListScenes，
// 三者底下都打 /api/miot/home。加一个 5s TTL 的请求级缓存避免重复打 backend
// （后者会再去打小米云 MiOT API，慢 + 浪费配额）。
// **5s 内返同一个 promise 是有意为之**——首屏 useAsync 并发触发 status/persons/devices
// 时三路 fetch 合并成一次 backend 调用。住户主动刷新（reload）超过 5s 后才会拿新值；
// 如果 5s 内 reload 拿到 stale resolve 是可接受的（家庭名 / 设备数变化频率远低于 5s）。
let homeCache: Promise<Normal<MiotHome>> | null = null;
let homeCacheTs = 0;
const HOME_TTL_MS = 5000;

function fetchMiotHome(): Promise<Normal<MiotHome>> {
  if (homeCache && Date.now() - homeCacheTs < HOME_TTL_MS) return homeCache;
  const p = apiFetch<Normal<MiotHome>>("/api/miot/home");
  homeCache = p;
  homeCacheTs = Date.now();
  // 失败时立即 invalidate(同时清 ts),不让 5s TTL 把所有 caller 绑死在同一个
  // rejected promise 上,reload 按钮重试也能立即去 backend。
  p.catch(() => {
    if (homeCache === p) {
      homeCache = null;
      homeCacheTs = 0;
    }
  });
  return homeCache;
}

// PUT 后写穿 cache:把 cache 直接换成新一轮的 fetch promise,不等下次 caller
// 触发 fetch。同时盖掉可能仍 in-flight 的旧 promise(它会 resolve 出 PUT 前的
// 旧数据)。这样新 caller(典型:status.reload 紧接着 switchScopeHome)拿到的
// 就是 PUT 后的数据,消除"罕见 race 读旧值"窗口。
function invalidateMiotHomeCache(): void {
  const fresh = apiFetch<Normal<MiotHome>>("/api/miot/home");
  homeCache = fresh;
  homeCacheTs = Date.now();
  fresh.catch(() => {
    if (homeCache === fresh) {
      homeCache = null;
      homeCacheTs = 0;
    }
  });
}

interface BackendDevice {
  did: string;
  name: string;
  online: boolean;
  model: string;
  room?: string;
  category?: string;
  spec?: Record<string, BackendPropSpec>;
  sub_devices?: unknown;
}

interface BackendPropSpec {
  description?: string;
  prop_description?: string;
  format?: string;
  writeable?: boolean;
  readable?: boolean;
  type_name?: string;
  service_type_name?: string;
  service_description?: string;
  unit?: string;
  value_list?: { name: string; value: number | string }[];
  value_range?: number[];
}

interface BackendDeviceStatus {
  properties: { iid: string; value: unknown; code: number }[];
}

interface BackendPerson {
  id: string;
  name: string;
  role?: string;
  face_enrolled: boolean;
  voice_enrolled: boolean;
  // v2:主 backend list_persons 直接合并 identity_lib 样本计数,
  // 不再需要单跑 register_server 拉 tier_a_body / tier_a_face。
  num_tier_a_body?: number;
  num_tier_c?: number;
  has_tier_a?: boolean;
  created_at?: string;
  updated_at?: string;
}

interface BackendCamera {
  did: string;
  name: string;
  device_type: string;
  online: boolean;
  room_id?: string;
  room_name?: string;
}

// ── 状态条聚合 ────────────────────────────────────────────
export async function realHomeStatus(): Promise<HomeStatus> {
  const [miot, home, engine] = await Promise.all([
    apiFetch<Normal<MiotStatus>>("/api/miot/status").catch(() => null),
    fetchMiotHome().catch(() => null),
    apiFetch<Normal<PerceptionEngineStatus>>(
      "/api/perception/engine/status",
    ).catch(() => null),
  ]);

  // areas 里偶尔混入 home_id（纯数字字符串），过滤掉
  const realAreas = (home?.data.areas ?? []).filter(
    (a) => !/^\d+$/.test(a.name),
  );

  return {
    miot: {
      bound: !!miot?.data.is_bound,
      accountName: cleanAccountName(miot?.data.user_info?.nickname),
      userIcon: miot?.data.user_info?.icon,
      userUid: miot?.data.user_info?.uid,
      devicesCount: home?.data.devices.length ?? 0,
      roomsCount: realAreas.length,
    },
    perception: {
      running: !!engine?.data.running,
      // engine 字段在当前 backend schema 强制存在，?? 兜底防御 OpenAPI client
      // 编译期 type narrow 失败 / 运行时手改 settings 导致字段缺位的极端边界。
      // 兜 false 而非 running——engine 字段缺失(schema 不一致 / 旧 backend)时按
      // ready=false 让 StatusRibbon 显"还没准备好"安全态,而不是装作正常运行掩盖
      // 模型缺失警告。
      ready: engine?.data.engine?.ready ?? false,
      engineMessage: engine?.data.engine?.message,
    },
    // 后端 MAX_ENABLED_CAMERAS（唯一来源）；status 拿不到时兜底 4。
    maxEnabledCameras: miot?.data.max_enabled_cameras ?? 4,
  };
}

// ── 家人 ──────────────────────────────────────────────────
// "已认识 / 还没认识" 的真值看主 backend list_persons 合并出来的 has_tier_a
// (识别库 identity_lib/persons/<id>/tier_a/ 下有 body 或 face 样本即视为已登记)。
// 旧的 biometric 表 + face_enrolled 字段已停写,只作为最后兜底。
export async function realListPersons(): Promise<Person[]> {
  const r = await apiFetch<Normal<BackendPerson[]>>("/api/identity/persons");
  return r.data.map((p, i) => {
    return {
      id: p.id,
      name: p.name,
      // role 直接透传：后端已是干净的家庭角色(SQL 迁移清掉了 role==name 镜像)，
      // 不再需要前端 alias!==name 的防御性兜底。
      role: p.role,
      // has_tier_a 来自主 backend 合并 identity_lib 的样本计数(num_tier_a_body / face);
      // 老字段 face_enrolled 留作兜底(老部署 backend 没 has_tier_a 时)。
      faceEnrolled: p.has_tier_a ?? p.face_enrolled,
      voiceEnrolled: p.voice_enrolled,
      avatarHue: i % 6,
    };
  });
}

export async function realCreatePerson(payload: {
  name: string;
  role?: string;
}): Promise<Person> {
  const r = await apiFetch<Normal<{ person_id: string }>>(
    "/api/identity/persons",
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
  return {
    id: r.data.person_id,
    name: payload.name,
    role: payload.role,
    faceEnrolled: false,
    voiceEnrolled: false,
    avatarHue: Math.floor(Math.random() * 6),
  };
}

export async function realUpdatePerson(
  id: string,
  payload: { name?: string; role?: string },
): Promise<void> {
  await apiFetch<Normal<null>>(`/api/identity/persons/${id}`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export async function realDeletePerson(id: string): Promise<void> {
  await apiFetch<Normal<null>>(`/api/identity/persons/${id}`, {
    method: "DELETE",
  });
}

// 录入身份样本：multipart 上传 base64 图给 /persons/{id}/samples
export async function realEnrollPersonSample(
  personId: string,
  imageBase64: string,
): Promise<void> {
  // base64（不含 data: 前缀）→ Uint8Array → Blob
  const raw = imageBase64.replace(/^data:image\/[a-z]+;base64,/, "");
  const bytes = Uint8Array.from(atob(raw), (c) => c.charCodeAt(0));
  const blob = new Blob([bytes], { type: "image/jpeg" });

  const form = new FormData();
  form.append("body_image", blob, "snapshot.jpg");
  form.append("source", "family_ui");

  // 直接 fetch，不走 apiFetch（避免被设上 Content-Type: application/json）
  const resp = await fetch(
    `/api/identity/persons/${personId}/samples`,
    {
      method: "POST",
      body: form,
      headers: authHeaders(),
    },
  );
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    throw new Error(body.message ?? body.detail ?? `HTTP ${resp.status}`);
  }
}

// ── 家庭档案（home_profile：候选区 / 正式区记忆）─────────────
// backend Entry 走 snake_case + 兜底字段；前端 HomeEntry 是 camelCase。
interface BackendHomeEntry {
  id: string;
  type: HomeEntryType;
  subject_id?: string | null;
  subject_name?: string | null;
  content: string;
  confidence: number;
  evidence_count: number;
  first_seen: string;
  last_seen: string;
  source: HomeEntrySource;
  evidence_log?: string[];
  archived?: boolean;
}

interface BackendHomeEntries {
  profile?: BackendHomeEntry[];
  candidates?: BackendHomeEntry[];
  ready_to_promote?: string[];
}

interface HomeOpResult {
  op: string;
  id: string;
  ok: boolean;
  message?: string | null;
}

// agent 证据累加语义的 op 输入（snake_case，直达 backend）。content/type 等
// 由 UI 填，subject_* 也透传——成员记忆带 person_id，非人记忆留空。
export interface HomeEntryPayload {
  type: HomeEntryType;
  content: string;
  subject_id?: string | null;
  subject_name?: string | null;
  confidence?: number;
  source?: HomeEntrySource;
}

export interface HomeProfileOp {
  op: "add" | "merge" | "replace" | "update" | "delete";
  id?: string;
  from?: string;
  date?: string;
  entry?: HomeEntryPayload;
  edit?: Partial<HomeEntryPayload>;
}

export interface HomeCandidateOp {
  op: "add" | "merge" | "update" | "delete";
  id?: string;
  // backend CandidateOp.date 必填
  date: string;
  entry?: HomeEntryPayload;
  edit?: Partial<HomeEntryPayload>;
}

function mapHomeEntry(e: BackendHomeEntry): HomeEntry {
  return {
    id: e.id,
    type: e.type,
    subjectId: e.subject_id ?? null,
    subjectName: e.subject_name ?? null,
    content: e.content,
    confidence: e.confidence,
    evidenceCount: e.evidence_count,
    firstSeen: e.first_seen,
    lastSeen: e.last_seen,
    source: e.source,
    evidenceLog: e.evidence_log ?? [],
    archived: e.archived,
  };
}

// 设备 spec 文案随 UI 语言切换；这些字符串在取数映射时烘焙进 device 对象,
// LanguageSwitcher 切语言后整页 reload 重新拉取,故此处按当前语言查表即可。
// langKey() 取当前语言基码(zh/en/…),用于查下方 PROP_LABEL/UNIT/ENUM_VALUE/
// STATUS_TEXT 这些"语言列"表;加一门语言只需给每张表补一列(+ 补 i18n JSON),
// 不动判断逻辑——取代了原先散落的 isEn() 三元。命中不上回退到 zh 列。
function langKey(): string {
  return i18n.language?.split("-")[0] || "zh";
}

// 任一 op 失败即抛——让 useAsync / toast 把 backend 的 message 透给住户，
// 避免「请求 200 但条目没动」的静默失败。
function assertOpsOk(results: HomeOpResult[]): void {
  const failed = results.find((r) => !r.ok);
  if (failed) throw new Error(failed.message ?? i18n.t("miot.opFail", { op: failed.op }));
}

export async function realListHomeEntries(
  target: "profile" | "candidates" | "both" = "both",
): Promise<HomeEntries> {
  const r = await apiFetch<Normal<BackendHomeEntries>>(
    `/api/home-profile/entries?target=${target}`,
  );
  return {
    profile: (r.data.profile ?? []).map(mapHomeEntry),
    candidates: (r.data.candidates ?? []).map(mapHomeEntry),
    readyToPromote: r.data.ready_to_promote ?? [],
  };
}

export async function realProfileWrite(
  ops: HomeProfileOp[],
  userEdit = true,
): Promise<void> {
  const r = await apiFetch<Normal<HomeOpResult[]>>(
    "/api/home-profile/profile:write",
    {
      method: "POST",
      body: JSON.stringify({ ops, user_edit: userEdit }),
    },
  );
  assertOpsOk(r.data);
}

export async function realCandidateWrite(ops: HomeCandidateOp[]): Promise<void> {
  const r = await apiFetch<Normal<HomeOpResult[]>>(
    "/api/home-profile/candidates:write",
    {
      method: "POST",
      body: JSON.stringify({ ops }),
    },
  );
  assertOpsOk(r.data);
}

export async function realCommitHomeProfile(): Promise<void> {
  await apiFetch<Normal<unknown>>("/api/home-profile/commit", {
    method: "POST",
  });
}

// ── 设备 ──────────────────────────────────────────────────
const CATEGORY_MAP: Record<string, DeviceCategory> = {
  light: "light",
  "ceiling-light": "light",
  "wall-switch": "light",
  "air-conditioner": "aircond",
  "air-purifier": "purifier",
  fan: "fan",
  "smart-curtain": "curtain",
  curtain: "curtain",
  lock: "lock",
  "smart-lock": "lock",
  tv: "tv",
  television: "tv",
  "set-top-box": "tv",
  camera: "camera",
};

function mapCategory(raw: string | undefined): DeviceCategory {
  if (!raw) return "other";
  return CATEGORY_MAP[raw] ?? "other";
}

// backend 偶尔把 home_id（纯数字字符串）当 room 返回，把这种归到"未分配"
function cleanRoom(raw: string | undefined): string {
  const unassigned = i18n.t("miot.unassigned");
  if (!raw) return unassigned;
  if (/^\d+$/.test(raw)) return unassigned;
  return raw;
}

// 去掉米家设备名常见的脏前缀（`.的XXX`、`*的XXX` 等）
function cleanDeviceName(raw: string): string {
  return raw.replace(/^[\s.\-_*]*的?\s*/, "") || raw;
}

const DANGEROUS_CATEGORIES = new Set(["lock", "smart-lock", "gas-stove"]);

// 米家 spec 给的 description / value 多半是英文——按 UI 语言译成住户能看懂的文案。
// 仅家庭面板常见字段;命中不上回退原文(英文模式天然回退到原始英文 spec 名)。
// 结构:每张表按语言分列(zh/en/…),加语言补一列即可,无 isEn() 三元。
const PROP_LABEL: Record<string, Record<string, string>> = {
  zh: {
    "Switch Status": "开关",
    "Device Fault": "故障状态",
    Mode: "模式",
    "Relative Humidity": "湿度",
    "PM2.5 Density": "PM2.5",
    Temperature: "温度",
    "Target Temperature": "目标温度",
    "Air Quality": "空气质量",
    "Filter Life Level": "滤芯剩余",
    "Filter Used Time": "已用时长",
    "Filter Left Time": "剩余天数",
    Alarm: "提示音",
    Brightness: "屏幕亮度",
    "Physical Control Locked": "童锁",
    "Fan Level": "风量档位",
    Volume: "音量",
    Mute: "静音",
    "Indicator Light": "指示灯",
    "Battery Level": "电量",
    Lock: "门锁",
    Locked: "已锁",
  },
  // 仅列需要更友好英文的字段;其余命中不上由 zhLabel 回退到原始英文 spec 名。
  en: {
    "Physical Control Locked": "Child Lock",
    "Filter Life Level": "Filter Life",
    "Filter Used Time": "Used Time",
    "Filter Left Time": "Days Left",
    Brightness: "Screen Brightness",
    Alarm: "Alarm Sound",
    Lock: "Door Lock",
    Locked: "Locked",
  },
};

// 符号类单位(%/°C/µg/m³)语言无关,各列一致;词类单位按语言给词。
const UNIT: Record<string, Record<string, string>> = {
  zh: {
    percentage: "%",
    celsius: "°C",
    hours: "小时",
    days: "天",
    "µg/m3": "µg/m³",
    "ug/m3": "µg/m³",
    minutes: "分",
    seconds: "秒",
    none: "",
  },
  en: {
    percentage: "%",
    celsius: "°C",
    hours: "h",
    days: "d",
    "µg/m3": "µg/m³",
    "ug/m3": "µg/m³",
    minutes: "min",
    seconds: "s",
    none: "",
  },
};

// 枚举值本身即英文 spec 名,英文模式直接回退原文,故 en 列留空。
const ENUM_VALUE: Record<string, Record<string, string>> = {
  zh: {
    Auto: "自动",
    Sleep: "睡眠",
    Favorite: "收藏",
    Close: "关",
    Closed: "关",
    Open: "开",
    Bright: "亮",
    Brightest: "最亮",
    Dim: "暗",
    Off: "关",
    On: "开",
    Cool: "制冷",
    Heat: "制热",
    Dry: "除湿",
    Fan: "送风",
    Low: "低",
    Mid: "中",
    Middle: "中",
    Medium: "中",
    High: "高",
  },
  en: {},
};

// 命中给译名,未命中回退原始英文 spec 名(英文模式天然就是原文)。
function zhLabel(en: string): string {
  return (PROP_LABEL[langKey()] ?? PROP_LABEL.zh)[en] ?? en;
}
function zhUnit(en: string | undefined): string | undefined {
  if (!en) return en;
  return (UNIT[langKey()] ?? UNIT.zh)[en] ?? en;
}
function zhEnumValue(en: string | number): string {
  if (typeof en !== "string") return String(en);
  const map = ENUM_VALUE[langKey()] ?? ENUM_VALUE.zh;
  if (map[en]) return map[en];
  // "Level0".."Level14" → 中文档位 "0 档"…;非中文语言回退原始 spec 名("Level3")。
  if (langKey() === "zh") {
    const m = en.match(/^Level(\d+)$/);
    if (m) return `${m[1]} 档`;
  }
  return en;
}

// 给单个 fetch 包一层超时（毫秒）；超时返回 fallback。
// Camera 这类 spec 大的设备 backend status 拉取可能 5-6 秒，
// 不能让一个慢设备卡住整个设备列表的 loading。
function withTimeout<T>(p: Promise<T>, ms: number, fallback: T): Promise<T> {
  return new Promise((resolve) => {
    const timer = setTimeout(() => resolve(fallback), ms);
    p.then((v) => {
      clearTimeout(timer);
      resolve(v);
    }).catch(() => {
      clearTimeout(timer);
      resolve(fallback);
    });
  });
}

// 并发上限控制：N 路 worker 跑完 task 列表。结果保持原顺序。
// 用途：30+ 设备首屏时不要全打满 → MiOT 云端有 ~10 QPS 上限。
async function batchWithConcurrency<T>(
  tasks: (() => Promise<T>)[],
  concurrency: number,
): Promise<T[]> {
  const results: T[] = new Array(tasks.length);
  let idx = 0;
  const run = async (): Promise<void> => {
    while (idx < tasks.length) {
      const i = idx++;
      results[i] = await tasks[i]();
    }
  };
  await Promise.all(
    Array.from({ length: Math.min(concurrency, tasks.length) }, run),
  );
  return results;
}

export async function realListDevices(): Promise<Device[]> {
  const r = await fetchMiotHome();
  const devices = r.data.devices;

  // 拉每个设备的属性 status；单个 2.5s 超时——超了就当空属性。
  // 设备列表能立即出来，sheet 打开时再单独重新拉 status（TODO 后续做）。
  // 并发上限 6 路：30+ 设备的大户型同时打出 30 个请求会触发 MiOT 云端 -704
  // 限频（同 uid 约 10 QPS），导致部分设备属性静默丢失。
  const statusResults = await batchWithConcurrency(
    devices.map(
      (d) => () =>
        withTimeout(
          apiFetch<Normal<BackendDeviceStatus>>(
            `/api/miot/devices/${d.did}/status`,
          )
            .then((s) => s.data.properties)
            .catch(() => [] as BackendDeviceStatus["properties"]),
          2500,
          [] as BackendDeviceStatus["properties"],
        ),
    ),
    6,
  );

  const mapped = devices.map((d, i) => {
    const props = statusResults[i];
    const valueByIid = new Map(props.map((p) => [p.iid, p.value]));
    const cat = mapCategory(d.category);
    const dangerous = DANGEROUS_CATEGORIES.has(d.category ?? "");

    // mainSwitch：约定 prop.2.1（开关）；只在它真的存在且 readable 时使用
    const mainSwitchSpec = d.spec?.["prop.2.1"];
    const mainSwitch =
      mainSwitchSpec && mainSwitchSpec.format === "bool"
        ? {
            iid: "prop.2.1",
            current: Boolean(valueByIid.get("prop.2.1") ?? false),
          }
        : undefined;

    const allProps: DeviceProperty[] = Object.entries(d.spec ?? {})
      .filter(([iid]) => iid.startsWith("prop."))
      .map(([iid, spec]) => mapProp(iid, spec, valueByIid.get(iid)));

    return {
      did: d.did,
      name: cleanDeviceName(d.name),
      category: cat,
      room: cleanRoom(d.room),
      online: d.online,
      statusText: humanDeviceStatus(d, mainSwitch?.current, valueByIid),
      dangerous,
      mainSwitch,
      props: allProps,
    };
  });

  // 按 (room, name) 排序，避免 backend 返回顺序抖动
  return mapped.sort((a, b) => {
    if (a.room !== b.room) return a.room.localeCompare(b.room, "zh");
    return a.name.localeCompare(b.name, "zh");
  });
}

function mapProp(
  iid: string,
  spec: BackendPropSpec,
  value: unknown,
): DeviceProperty {
  const rawLabel = spec.prop_description || spec.description || iid;
  const label = zhLabel(rawLabel);
  const unit = zhUnit(spec.unit);
  const writeable = !!spec.writeable;

  if (!writeable) {
    return {
      iid,
      label,
      type: "readonly",
      value: (value as DeviceProperty["value"]) ?? "—",
      unit,
    };
  }
  if (spec.format === "bool") {
    return {
      iid,
      label,
      type: "switch",
      value: Boolean(value ?? false),
    };
  }
  if (spec.value_list && spec.value_list.length > 0) {
    return {
      iid,
      label,
      type: "enum",
      value: (value as string | number) ?? spec.value_list[0]?.value,
      options: spec.value_list.map((v) => ({
        label: zhEnumValue(v.name),
        value: v.value,
      })),
    };
  }
  if (spec.value_range && spec.value_range.length >= 2) {
    return {
      iid,
      label,
      type: "number",
      value: Number(value ?? spec.value_range[0]),
      unit,
      min: spec.value_range[0],
      max: spec.value_range[1],
      step: spec.value_range[2] ?? 1,
    };
  }
  return {
    iid,
    label,
    type: "readonly",
    value: (value as DeviceProperty["value"]) ?? "—",
    unit,
  };
}

type StatusText = {
  offline: string;
  locked: string;
  unlocked: string;
  connected: string;
  off: string;
  running: string;
  sleepMode: string;
  autoMode: string;
  on: string;
};
const STATUS_TEXT: Record<string, StatusText> = {
  zh: {
    offline: "已离线",
    locked: "已锁",
    unlocked: "未锁",
    connected: "已连接",
    off: "关闭",
    running: "运行中",
    sleepMode: "睡眠档",
    autoMode: "自动档",
    on: "开着",
  },
  en: {
    offline: "Offline",
    locked: "Locked",
    unlocked: "Unlocked",
    connected: "Connected",
    off: "Off",
    running: "Running",
    sleepMode: "Sleep",
    autoMode: "Auto",
    on: "On",
  },
};

function humanDeviceStatus(
  d: BackendDevice,
  mainOn: boolean | undefined,
  values: Map<string, unknown>,
): string {
  const s = STATUS_TEXT[langKey()] ?? STATUS_TEXT.zh;
  if (!d.online) return s.offline;
  const cat = mapCategory(d.category);

  if (cat === "lock") {
    const v = values.get("prop.2.1");
    return v ? s.locked : s.unlocked;
  }
  if (mainOn === undefined) return s.connected;
  if (cat === "aircond") {
    if (!mainOn) return s.off;
    const t = values.get("prop.2.5") ?? values.get("prop.2.4");
    if (typeof t === "number") return `${t}°C`;
    return s.running;
  }
  if (cat === "purifier") {
    if (!mainOn) return s.off;
    const mode = values.get("prop.2.4");
    if (mode === 1) return s.sleepMode;
    if (mode === 0) return s.autoMode;
    return s.running;
  }
  return mainOn ? s.on : s.off;
}

export async function realControlDeviceProp(
  did: string,
  iid: string,
  value: number | string | boolean,
): Promise<void> {
  await apiFetch<Normal<unknown>>(`/api/miot/devices/${did}/control`, {
    method: "POST",
    body: JSON.stringify({ type: "set_property", iid, value }),
  });
}

// ── 场景 ──────────────────────────────────────────────────
export async function realListScenes(): Promise<Scene[]> {
  const r = await fetchMiotHome();
  return r.data.scenes.map((s) => ({ id: s.scene_id, name: s.scene_name }));
}

export async function realTriggerScene(id: string): Promise<void> {
  await apiFetch<Normal<unknown>>(`/api/miot/scenes/${id}/trigger`, {
    method: "POST",
  });
}

// ── 摄像头 ────────────────────────────────────────────────
export async function realListCameras(): Promise<PerceptionCamera[]> {
  const r = await apiFetch<Normal<BackendCamera[]>>("/api/perception/devices");
  return r.data
    .filter((c) => c.device_type === "camera" || !c.device_type)
    .map((c) => ({
      did: c.did,
      name: c.name,
      channel: 0,
      roomName: c.room_name,
    }));
}

// ── 米家账号绑定 OAuth ────────────────────────────────────
// 流程跟 cli/src/miloco_cli/commands/account.py 同源：
//   ① POST /api/miot/bind → backend 返 oauth_url（小米授权页）
//   ② 用户浏览器打开 oauth_url 走授权 → 回调到
//      https://mico.api.mijia.tech/login_redirect 显示 base64 payload
//   ③ 用户复制 payload → 前端解 base64 → POST /api/miot/authorize {code, state}
// redirect_uri 写死小米域名（manager.py），webUI 无法直接拿到 code/state，
// 必须靠用户复制粘贴；这是小米 OAuth 的硬约束。
export async function realBindMiot(): Promise<{ oauthUrl: string }> {
  const r = await apiFetch<Normal<{ oauth_url: string }>>("/api/miot/bind", {
    method: "POST",
  });
  return { oauthUrl: r.data.oauth_url };
}

export async function realAuthorizeMiot(
  code: string,
  state: string,
): Promise<void> {
  await apiFetch<Normal<unknown>>("/api/miot/authorize", {
    method: "POST",
    body: JSON.stringify({ code, state }),
  });
}

export async function realUnbindMiot(): Promise<void> {
  await apiFetch<Normal<unknown>>("/api/miot/unbind", { method: "POST" });
}

// ── 米家 scope 家庭（多家庭启用/停用切换）──────────────────────
interface BackendScopeHome {
  home_id: string;
  home_name: string | null;
  in_use: boolean;
}

export async function realListScopeHomes(): Promise<ScopeHome[]> {
  const r = await apiFetch<Normal<BackendScopeHome[]>>("/api/miot/scope/homes");
  return r.data.map((h) => ({
    homeId: h.home_id,
    homeName: h.home_name ?? h.home_id,
    inUse: h.in_use,
  }));
}

export async function realSwitchScopeHome(homeId: string): Promise<void> {
  await apiFetch<Normal<unknown>>("/api/miot/scope/homes", {
    method: "PUT",
    body: JSON.stringify({ home_id: homeId }),
  });
  // 写后立即 invalidate + 主动 prefetch homeCache:消除"PUT 后立即调 status.reload()
  // 但拿到 in-flight 旧 promise"的 race 窗口。invalidateMiotHomeCache 内部把 cache
  // 直接换成新一轮 fetch promise,新 caller 等的就是 PUT 后的数据。
  invalidateMiotHomeCache();
}

// ── 米家 scope 摄像头（启用 / 禁用全集）────────────────────────
// 飞书 docx WImSdXQHEobWMaxFWoxcUsBpnjz 设计规范：
//   - GET 返全部相机 + 状态（含已禁用）
//   - PUT in_use=false 时 backend 校验 did 必须存在；in_use=true 任意（清理脏数据）
interface BackendScopeCamera {
  did: string;
  name: string | null;
  room_name?: string | null;
  is_online: boolean;
  in_use: boolean;
  connected: boolean;
}

export async function realListScopeCameras(): Promise<ScopeCamera[]> {
  const r = await apiFetch<Normal<BackendScopeCamera[]>>(
    "/api/miot/scope/cameras",
  );
  return r.data.map((c) => ({
    did: c.did,
    name: c.name ?? c.did,
    roomName: c.room_name ?? undefined,
    isOnline: c.is_online,
    inUse: c.in_use,
    connected: c.connected,
  }));
}

// 轻量刷新相机「云端 online」状态——list_cameras_with_state 只读内存缓存
// (_camera_info_dict),相机重新上线后该缓存不会自愈,网页刷新拉到的是旧值(一直显
// "已离线")。加载相机列表前先调这个,让在线状态真实。**走 /refresh_camera_online
// (只更新缓存元数据、不碰解码/流),不是 /refresh_miot_cameras(会过 update_camera_info
// 在共用 SDK 实例上重注册解码 → 瞬时卡流)**。
// 8s 节流:防住户连续刷新「此刻」页狂打米家云(米家 -704 限频);失败静默,调用方
// 仍能拿到上一份缓存,不阻断列表渲染。
let lastCamRefreshTs = 0;
const CAM_REFRESH_THROTTLE_MS = 8000;
export async function realRefreshCameraOnline(): Promise<void> {
  const now = Date.now();
  if (now - lastCamRefreshTs < CAM_REFRESH_THROTTLE_MS) return;
  lastCamRefreshTs = now;
  await apiFetch<Normal<unknown>>("/api/miot/refresh_camera_online");
}

export async function realToggleScopeCamera(
  dids: string[],
  inUse: boolean,
): Promise<void> {
  await apiFetch<Normal<unknown>>("/api/miot/scope/cameras", {
    method: "PUT",
    body: JSON.stringify({ items: dids.map((did) => ({ did, in_use: inUse })) }),
  });
  // 写后立即 invalidate + 主动 prefetch homeCache(同 switchScopeHome 同款消 race)。
  invalidateMiotHomeCache();
}

// ── 今天发生了什么(meaningful_events)───────────────────────

/** GET /api/events 返回单元(对齐后端 MeaningfulEvent Pydantic 模型). */
interface BackendMeaningfulEvent {
  event_id: string;
  timestamp: number;
  text: string;
  has_rule_hit?: boolean;
  has_suggestion?: boolean;
  has_asr?: boolean;
  snapshot_count: number;
  device_ids: string[];
  rule_names?: Record<string, string>;
  /** 服务端根据落盘文件后缀计算:"mp4" 视频路径 / "m4a" audio-only / null 未落盘. */
  clip_kind?: "mp4" | "m4a" | null;
  has_trace?: boolean;
  has_feedback?: boolean;
  feedback_pack_path?: string | null;
  feedback_pack_size?: number | null;
}

export async function realListActivity(opts?: {
  since?: number;
  before?: number;
  limit?: number;
  offset?: number;
}): Promise<ActivityEvent[]> {
  const params = new URLSearchParams();
  if (opts?.since !== undefined) params.set("since", String(opts.since));
  if (opts?.before !== undefined) params.set("before", String(opts.before));
  params.set("limit", String(opts?.limit ?? 50));
  if (opts?.offset !== undefined) params.set("offset", String(opts.offset));
  const qs = params.toString();
  const resp = await apiFetch<Normal<{ events: BackendMeaningfulEvent[] }>>(
    qs ? `/api/events?${qs}` : "/api/events",
  );
  return resp.data.events.map(
    (e): ActivityEvent => ({
      id: e.event_id,
      timestamp: e.timestamp,
      text: e.text,
      has_rule_hit: e.has_rule_hit,
      has_suggestion: e.has_suggestion,
      has_asr: e.has_asr,
      snapshot_count: e.snapshot_count,
      device_ids: e.device_ids,
      rule_names: e.rule_names,
      clip_kind: e.clip_kind,
      has_trace: e.has_trace,
      has_feedback: e.has_feedback,
      feedback_pack_path: e.feedback_pack_path,
      feedback_pack_size: e.feedback_pack_size,
    }),
  );
}

/**
 * 拼事件 clip mp4 URL,带 `?token=...` query 鉴权(<video> 无法设 Authorization header).
 * 后端 `verify_token_query_fallback` 支持 query token.
 *
 * clip = omni 上传给 LLM 的字节级 mp4(零重编):
 * - 视频路径:含 H264 + AAC
 * - audio-only 路径:仅 AAC(浏览器 <video> 控件能 render audio-only track)
 */
export function realEventClipUrl(
  event_id: string,
  device_id: string,
): string {
  const token = resolveToken();
  const base = `/api/events/${encodeURIComponent(event_id)}/clip/${encodeURIComponent(device_id)}`;
  return token ? `${base}?token=${encodeURIComponent(token)}` : base;
}

/**
 * 订阅 `/api/events/stream` SSE,新事件来时 callback `(ActivityEvent)`.
 * 返回 unsubscribe 函数.
 *
 * 注:EventSource 也无法设 Authorization header,用 `?token=...` 兜底.
 *
 * onOpen 可选:**仅在断线后重连成功时**触发(EventSource 'open' 事件首次也会 fire,
 * 我们用 firstOpenSkipped 旗标跳过初次连接).调用方典型用法是借此 reload 一次列表,
 * 补回断开期间错过的事件(spec B13).
 *
 * S3 修复:首次 open 不触发 onOpen — 避免跟父组件的 initial fetchPage 重复;
 * 父组件已经在挂载时通过 useAsync(listActivity) 拉过一次列表了.
 */
export function realSubscribeEvents(
  onEvent: (e: ActivityEvent) => void,
  onOpen?: () => void,
): () => void {
  const token = resolveToken();
  const url = token
    ? `/api/events/stream?token=${encodeURIComponent(token)}`
    : "/api/events/stream";
  const es = new EventSource(url);
  let firstOpenSeen = false;
  if (onOpen) {
    es.addEventListener("open", () => {
      if (!firstOpenSeen) {
        firstOpenSeen = true; // 跳过首次连接,只在重连时回调
        return;
      }
      onOpen();
    });
  }
  es.addEventListener("new_event", (ev) => {
    try {
      const payload = JSON.parse((ev as MessageEvent).data) as BackendMeaningfulEvent;
      onEvent({
        id: payload.event_id,
        timestamp: payload.timestamp,
        text: payload.text,
        has_rule_hit: payload.has_rule_hit,
        has_suggestion: payload.has_suggestion,
        has_asr: payload.has_asr,
        snapshot_count: payload.snapshot_count,
        device_ids: payload.device_ids,
        rule_names: payload.rule_names,
        clip_kind: payload.clip_kind,
        has_trace: payload.has_trace,
        has_feedback: payload.has_feedback,
        feedback_pack_path: payload.feedback_pack_path,
        feedback_pack_size: payload.feedback_pack_size,
      });
    } catch {
      // ignore malformed payload
    }
  });
  return () => es.close();
}

// ── 事件反馈 ────────────────────────────────────────────────
export async function realSubmitEventFeedback(
  eventId: string,
  errorTypes: string[],
  feedbackText: string,
  includeGallery: boolean,
): Promise<{ uploaded: boolean; upload_key?: string; pack_path: string; pack_size_bytes: number }> {
  const resp = await apiFetch<
    Normal<{ event_id: string; pack_path: string; pack_size_bytes: number; uploaded: boolean; upload_key: string | null }>
  >("/api/admin/events/feedback", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      event_id: eventId,
      error_types: errorTypes,
      feedback_text: feedbackText,
      include_gallery: includeGallery,
    }),
  });
  return {
    uploaded: resp.data.uploaded,
    upload_key: resp.data.upload_key ?? undefined,
    pack_path: resp.data.pack_path,
    pack_size_bytes: resp.data.pack_size_bytes,
  };
}

export async function realRevealFile(path: string): Promise<void> {
  await apiFetch<Normal<null>>("/api/admin/reveal-file", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path }),
  });
}

// ── 让它休息 / 唤醒 ────────────────────────────────────────
export async function realPausePerception(): Promise<void> {
  await apiFetch<Normal<unknown>>("/api/perception/engine/stop", {
    method: "POST",
  });
}

export async function realResumePerception(): Promise<void> {
  await apiFetch<Normal<unknown>>("/api/perception/engine/start", {
    method: "POST",
  });
}

// ── Token 用量统计（用量 tab）─────────────────────────────────
// 数据全部来自 omni/MiMo 计费。backend 两个接口：
//   today      → /api/admin/token-usage/buckets  服务端按桶聚合（bin 分钟粒度）
//   week/month → /api/admin/token-usage/daily    按 date/model/type 聚合（滚动近 N 天）
// 这里把两种形态都归一成 Unit[] 再折算成 UsageStats。

// today：服务端已按 (时间桶 × model × type) 聚合，每行是一个桶的小计。
interface BucketRow {
  bucket_ms: number; // 桶起始 ms epoch
  model: string;
  type: string;
  calls: number;
  input_tokens: number;
  output_tokens: number;
  cache_tokens: number;
  video_tokens: number;
  audio_tokens: number;
}

interface DailyRow {
  date: string; // YYYY-MM-DD（backend 已按 localtime 归日）
  model: string;
  type: string;
  calls: number;
  input_tokens: number;
  output_tokens: number;
  cache_tokens: number;
  video_tokens: number;
  audio_tokens: number;
}

/** bucket / daily 聚合行的统一形态。 */
interface UsageUnit {
  model: string;
  type: string;
  calls: number;
  input_tokens: number;
  output_tokens: number;
  cache_tokens: number;
  video_tokens: number;
  audio_tokens: number;
}

const ONE_DAY_MS = 86_400_000;

function emptyBreakdown(): TokenBreakdown {
  return { input: 0, output: 0, cache: 0, video: 0, audio: 0 };
}

function asCallType(t: string): UsageCallType {
  return t === "on_demand" ? "on_demand" : "realtime";
}

/** 把一个 Unit 的各模态计入 breakdown 累加器。 */
function accBreakdown(acc: TokenBreakdown, u: UsageUnit): void {
  acc.input += u.input_tokens || 0;
  acc.output += u.output_tokens || 0;
  acc.cache += u.cache_tokens || 0;
  acc.video += u.video_tokens || 0;
  acc.audio += u.audio_tokens || 0;
}

/** 总量口径：input 已含全部模态，故总 = input + output。 */
function breakdownTotal(b: TokenBreakdown): number {
  return b.input + b.output;
}

/** YYYY-MM-DD（本地时区，与 backend daily 的 localtime 归日对齐）。 */
function localDateStr(d: Date): string {
  const y = d.getFullYear();
  const m = `${d.getMonth() + 1}`.padStart(2, "0");
  const day = `${d.getDate()}`.padStart(2, "0");
  return `${y}-${m}-${day}`;
}

/**
 * today：把服务端返回的桶行铺满一整天（00:00 → 次日 00:00），缺的桶补 0。
 * 服务端已按 bin 聚合，这里只负责对齐到整天的连续桶骨架。tokens = input + output。
 */
function bucketTimeline(
  rows: BucketRow[],
  binMinutes: number,
): { ts: string; tokens: number }[] {
  const start = new Date();
  start.setHours(0, 0, 0, 0);
  const startMs = start.getTime();
  const binMs = Math.max(1, binMinutes) * 60_000;
  const n = Math.max(1, Math.ceil(ONE_DAY_MS / binMs)); // 覆盖整天
  const buckets = Array.from({ length: n }, (_, i) => ({
    ts: new Date(startMs + i * binMs).toISOString(),
    tokens: 0,
  }));
  for (const r of rows) {
    const idx = Math.floor((r.bucket_ms - startMs) / binMs);
    if (idx >= 0 && idx < n) {
      buckets[idx].tokens += (r.input_tokens || 0) + (r.output_tokens || 0);
    }
  }
  return buckets;
}

/** week/month：连续 N 天（含今天），缺数据的天补 0。 */
function dailyTimeline(
  rows: DailyRow[],
  days: number,
): { ts: string; tokens: number }[] {
  const byDate = new Map<string, number>();
  for (const r of rows) {
    const t = (r.input_tokens || 0) + (r.output_tokens || 0);
    byDate.set(r.date, (byDate.get(r.date) ?? 0) + t);
  }
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const out: { ts: string; tokens: number }[] = [];
  for (let i = days - 1; i >= 0; i--) {
    const d = new Date(today.getTime() - i * ONE_DAY_MS);
    out.push({ ts: d.toISOString(), tokens: byDate.get(localDateStr(d)) ?? 0 });
  }
  return out;
}

/** Unit[] → UsageStats：汇总 totals、按调用类型聚合、按 model×type 出明细行。 */
function unitsToStats(
  period: UsagePeriod,
  units: UsageUnit[],
  timeline: { ts: string; tokens: number }[],
): UsageStats {
  const totals = emptyBreakdown();
  let calls = 0;
  // 预置两种调用类型，保证 realtime / on_demand 都恒显示（无数据则为 0）。
  const byType = new Map<UsageCallType, UsageGroup>([
    ["realtime", { key: "realtime", calls: 0, tokens: 0, breakdown: emptyBreakdown() }],
    ["on_demand", { key: "on_demand", calls: 0, tokens: 0, breakdown: emptyBreakdown() }],
  ]);
  const rowMap = new Map<string, UsageRow>();

  for (const u of units) {
    accBreakdown(totals, u);
    calls += u.calls || 0;

    const tk = asCallType(u.type);
    const g = byType.get(tk)!;
    g.calls += u.calls || 0;
    accBreakdown(g.breakdown, u);
    g.tokens = breakdownTotal(g.breakdown);

    const rk = `${u.model} ${tk}`;
    let row = rowMap.get(rk);
    if (!row) {
      row = {
        model: u.model,
        type: tk,
        calls: 0,
        tokens: 0,
        breakdown: emptyBreakdown(),
      };
      rowMap.set(rk, row);
    }
    row.calls += u.calls || 0;
    accBreakdown(row.breakdown, u);
    row.tokens = breakdownTotal(row.breakdown);
  }

  // 每个出现过的模型都补齐 realtime + on_demand 两行（缺的填 0），与 by_type 恒显示一致。
  // 注意：分隔符必须与主循环的 rowMap key 一致（ ），否则 has() 命中失败会补出重复行。
  for (const model of new Set([...rowMap.values()].map((r) => r.model))) {
    for (const tk of ["realtime", "on_demand"] as UsageCallType[]) {
      const rk = `${model} ${tk}`;
      if (!rowMap.has(rk)) {
        rowMap.set(rk, {
          model,
          type: tk,
          calls: 0,
          tokens: 0,
          breakdown: emptyBreakdown(),
        });
      }
    }
  }

  // 明细排序：模型名按字母序分组；组内固定 realtime（实时感知）在前、on_demand（用户感知）在后。
  const typeRank = (t: string): number => (t === "realtime" ? 0 : 1);
  const rows = [...rowMap.values()].sort((a, b) => {
    if (a.model !== b.model) return a.model < b.model ? -1 : 1;
    return typeRank(a.type) - typeRank(b.type);
  });

  return {
    period,
    total_tokens: breakdownTotal(totals),
    calls,
    totals,
    by_type: [...byType.values()].sort((a, b) => b.tokens - a.tokens),
    rows,
    timeline,
  };
}

function bucketToUnit(b: BucketRow): UsageUnit {
  return { ...b };
}

function rowToUnit(d: DailyRow): UsageUnit {
  return { ...d };
}

// 请求级缓存：UsagePage 与 UsageTimelineChart 在挂载同一 tick 各打一次 today，
// 按 (period, bin) 合并并发请求 + 5s TTL，避免重复打较重的 token-usage 接口（同 fetchMiotHome 思路）。
const usageCache = new Map<string, { ts: number; p: Promise<UsageStats> }>();
const USAGE_TTL_MS = 5000;

/** 测试用：清空用量请求缓存，避免跨用例命中陈旧数据。 */
export function _resetUsageStatsCache(): void {
  usageCache.clear();
}

export function realGetUsageStats(
  period: UsagePeriod = "today",
  binMinutes = 60,
): Promise<UsageStats> {
  // bin 只对 today 生效（week/month 是按天的 daily rollup）。
  const key = period === "today" ? `today:${binMinutes}` : period;
  const hit = usageCache.get(key);
  if (hit && Date.now() - hit.ts < USAGE_TTL_MS) return hit.p;
  const p = fetchUsageStats(period, binMinutes);
  usageCache.set(key, { ts: Date.now(), p });
  // 失败时清掉缓存，避免一次错误锁死 TTL。
  p.catch(() => usageCache.delete(key));
  return p;
}

// 清空全部用量数据（实时表 + 日聚合）。清完顺手失效请求级缓存，确保下次取到空。
export async function realClearUsageData(): Promise<void> {
  await apiFetch<Normal<unknown>>("/api/admin/token-usage/clear", {
    method: "POST",
  });
  _resetUsageStatsCache();
}

// ── omni 模型配置（「模型」页内读/写，多档案切换） ──────────────────
// GET 拿 {active, profiles}（api_key 打码）；写操作回写 config.json，感知下个推理周期热生效。
export async function realGetOmniConfig(): Promise<OmniConfigState> {
  const r = await apiFetch<Normal<OmniConfigState>>("/api/admin/omni-config");
  return r.data;
}

// 保存一套配置(upsert 档案)。api_key 留空=沿用该档案原 key;activate=false 只入列表不激活。
export async function realUpdateOmniConfig(
  input: OmniConfigUpdate,
): Promise<OmniConfigState> {
  const body: Record<string, string | boolean> = {
    label: input.label,
    model: input.model,
    base_url: input.base_url,
  };
  if (input.api_key) body.api_key = input.api_key;
  if (input.original_label !== undefined) body.original_label = input.original_label;
  if (input.activate !== undefined) body.activate = input.activate;
  const r = await apiFetch<Normal<OmniConfigState>>("/api/admin/omni-config", {
    method: "PUT",
    body: JSON.stringify(body),
  });
  return r.data;
}

// 切换当前生效配置为某套已存档案。
export async function realActivateOmniConfig(
  ref: OmniProfileRef,
): Promise<OmniConfigState> {
  const r = await apiFetch<Normal<OmniConfigState>>(
    "/api/admin/omni-config/activate",
    { method: "POST", body: JSON.stringify(ref) },
  );
  return r.data;
}

// 删除一套已存档案（不影响当前生效配置）。
export async function realDeleteOmniConfig(
  ref: OmniProfileRef,
): Promise<OmniConfigState> {
  const r = await apiFetch<Normal<OmniConfigState>>(
    "/api/admin/omni-config/delete",
    { method: "POST", body: JSON.stringify(ref) },
  );
  return r.data;
}

// 停用当前生效模型:回未配态 + 软停感知,但保留档案(可再启用)。
export async function realDeactivateOmniConfig(
  ref: OmniProfileRef,
): Promise<OmniConfigState> {
  const r = await apiFetch<Normal<OmniConfigState>>(
    "/api/admin/omni-config/deactivate",
    { method: "POST", body: JSON.stringify(ref) },
  );
  return r.data;
}

// 拉取某 Base URL 下可用模型列表（供模型下拉）。api_key 留空则用同 base_url 已存 key。
export async function realListOmniModels(input: {
  base_url: string;
  api_key?: string;
  label?: string;
}): Promise<OmniModelsResult> {
  const body: Record<string, string> = { base_url: input.base_url };
  if (input.api_key) body.api_key = input.api_key;
  if (input.label) body.label = input.label;
  const r = await apiFetch<Normal<OmniModelsResult>>(
    "/api/admin/omni-config/models",
    { method: "POST", body: JSON.stringify(body) },
  );
  return r.data;
}

// 测试连接：用表单值（api_key 留空则测已保存的 key）发一次 GET /models 探测。
export async function realTestOmniConfig(
  input: OmniConfigUpdate,
): Promise<OmniTestResult> {
  const body: Record<string, string> = {
    label: input.label,
    model: input.model,
    base_url: input.base_url,
  };
  if (input.api_key) body.api_key = input.api_key;
  const r = await apiFetch<Normal<OmniTestResult>>(
    "/api/admin/omni-config/test",
    { method: "POST", body: JSON.stringify(body) },
  );
  return r.data;
}

async function fetchUsageStats(
  period: UsagePeriod,
  binMinutes: number,
): Promise<UsageStats> {
  if (period === "today") {
    // 服务端按 bin 桶聚合（响应大小由桶数封顶，不随事件数增长，不会触顶截断）。
    // 显式传 client 本地 00:00 的窗口，与 bucketTimeline 的骨架起点锚定同一绝对时刻，
    // 避免浏览器/服务器时区不一致时今日早段被后端窗口或前端骨架静默丢弃。
    const start = new Date();
    start.setHours(0, 0, 0, 0);
    const startMs = start.getTime();
    const r = await apiFetch<Normal<{ rows: BucketRow[]; total: number }>>(
      `/api/admin/token-usage/buckets?bin=${binMinutes}` +
        `&since=${startMs}&until=${startMs + ONE_DAY_MS}`,
    );
    const rows = r.data.rows ?? [];
    return unitsToStats(period, rows.map(bucketToUnit), bucketTimeline(rows, binMinutes));
  }

  // week / month：滚动近 N 天（含今天）的 daily 聚合
  const days = period === "week" ? 7 : 30;
  const until = new Date();
  until.setHours(0, 0, 0, 0);
  const since = new Date(until.getTime() - (days - 1) * ONE_DAY_MS);
  const qs = `since=${localDateStr(since)}&until=${localDateStr(until)}`;
  const r = await apiFetch<Normal<{ rows: DailyRow[]; total: number }>>(
    `/api/admin/token-usage/daily?${qs}`,
  );
  const rows = r.data.rows ?? [];
  return unitsToStats(period, rows.map(rowToUnit), dailyTimeline(rows, days));
}


// ── 任务（task）─────────────────────────────────────────────
// summary 视图 = task 基础字段 + record 进度摘要（window=day：progress 走 snapshot，
// duration/event 走今日累计）。derived 形态按 kind 多态，原样透传给 UI 自行解读。
interface BackendTaskSummary {
  task_id: string;
  description: string;
  status: "active" | "paused";
  created_at: string;
  record: {
    kind: "progress" | "duration" | "event";
    completed: boolean;
    active_session: { started_at: string; elapsed_minutes: number } | null;
    derived: Record<string, unknown>;
  } | null;
}

export async function realListTasks(): Promise<Task[]> {
  const r = await apiFetch<Normal<BackendTaskSummary[]>>(
    "/api/tasks/summary?window=day",
  );
  return (r.data ?? []).map((t) => ({
    taskId: t.task_id,
    description: t.description,
    status: t.status,
    createdAt: t.created_at,
    record: t.record
      ? {
          kind: t.record.kind,
          completed: t.record.completed,
          activeSession: t.record.active_session
            ? {
                startedAt: t.record.active_session.started_at,
                elapsedMinutes: t.record.active_session.elapsed_minutes,
              }
            : null,
          derived: t.record.derived ?? {},
        }
      : null,
  }));
}

export async function realSetTaskEnabled(
  taskId: string,
  enabled: boolean,
): Promise<void> {
  await apiFetch<Normal<unknown>>(
    `/api/tasks/${encodeURIComponent(taskId)}/${enabled ? "enable" : "disable"}`,
    { method: "POST" },
  );
}

// 住户手动删除按"主动放弃"记入终止审计（reason=abandoned）。
export async function realDeleteTask(taskId: string): Promise<void> {
  await apiFetch<Normal<unknown>>(
    `/api/tasks/${encodeURIComponent(taskId)}?reason=abandoned`,
    { method: "DELETE" },
  );
}
