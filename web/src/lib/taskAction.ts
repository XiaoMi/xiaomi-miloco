/**
 * task 边界动作 → 一句话。
 *
 * 台账里的动作是 `{did, iid, value|params}` 的机器口径，详情页要回答的却是
 * 「哪个设备在做什么」。三类取数各自有来源与回落：
 *
 * - **设备**：`did` → `/api/miot/home` 的设备名与房间。取不到就显式标「未知设备」，
 *   不拿裸 did 冒充名字——那看起来像解析成功了。
 * - **能力**：`prop.*` 的 `iid` → 设备 spec 的 `prop_description || description`，与设备页、
 *   日志页共用同一个 `specLabel`，同一个属性三处同名；`action.*` 的 `prop_description`
 *   是英文 type_name（play-text），改取云端译名，见 `actionCapability`。
 * - **值**：`prop.*` 走 `propValueText`（bool / 枚举 / 单位三态）；`action.*` 取 params；
 *   `scene` 取场景名。读不出来时回落成原始值，不猜。
 *
 * 与 ``lib/actionText.ts`` 的分工：那边读的是**动作台账**（`value_json` 字符串），
 * 这边读的是**任务动作槽里的原始动作对象**，两者字段形态不同，不要合并。
 */

import {
  propValueText,
  specLabel,
  type BackendPropSpec,
  type TaskDeviceMeta,
} from "@/api/real";
import type { TaskAction } from "@/lib/types";

type Translate = (key: string, options?: Record<string, unknown>) => string;

/** 生成式动作 params 里的回填占位符。**后端尚未产出**（预留给「执行前按画面生成
 *  文案」的能力）；接入后由服务端替换，展示层任何时候都不该把模板原样露给住户。
 *  在那之前这条判断走不到，是刻意的前向兜底而不是已生效的契约。 */
const RESULT_PLACEHOLDER = /\{\{\s*result\s*\}\}/;

/** `action.<siid>.<aiid>` 是通用设备动作通道，不等于 TTS：只有「逐字念出」的
 *  播报（音箱 play-text）才贴「播报」；`execute-text-directive` 是让小爱理解后自己
 *  执行的一句话、还带 silent-execution，不保证出声，按设备动作展示。
 *  接入别的厂商 TTS 时按实测的 type_name 逐个加进正则，别用 `tts` 这种宽匹配。 */
const SPEECH_ACTION_TYPE = /play-text/i;

export interface ActionCatalog {
  /** 与 realListDeviceMeta 返回的设备元数据同源，不另立一份。 */
  devices: Map<string, TaskDeviceMeta>;
  /** scene_id → 场景名 */
  scenes: Map<string, string>;
  /** 设备表是否已落地。未落地时 did 原样显示（不报「未知设备」——那是「查过了没有」）。 */
  ready: boolean;
  t: Translate;
}

export type TaskActionKind = "broadcast" | "deviceAction" | "control" | "scene";

export interface DescribedTaskAction {
  kind: TaskActionKind;
  /** 设备名；解析不出时是「未知设备 <did>」 */
  device: string;
  room?: string;
  /** 能力名（iid → spec 描述）；无 spec 时回落成 iid */
  capability: string;
  /** 值 / 播报文本；无内容时 null */
  detail: string | null;
  /** 冷却 / 幂等 这类短标签，已按语言翻译 */
  chips: string[];
  /** 原始 `did · iid`，排查时照着搜 */
  raw: string;
  /** 目标设备不在当前账号可见范围 */
  unresolved: boolean;
}

/** 后端动作对象 → 展示用字段。永不抛：任何一环缺失都回落成原始值。 */
export function describeTaskAction(
  action: TaskAction,
  catalog: ActionCatalog,
): DescribedTaskAction {
  const { t } = catalog;
  const did = String(action.did ?? "");
  const iid = String(action.iid ?? "");
  const raw = `${did} · ${iid}`;

  // 场景动作：did 位置放的是 scene_id，不在设备表里，名字在场景表。
  if (iid === "scene") {
    const name = catalog.scenes.get(did);
    return {
      kind: "scene",
      device: name
        ? t("tasks.sceneTarget", { name })
        : catalog.ready
          ? t("tasks.sceneTargetUnknown")
          : did,
      // 场景行没有「能力」这一层：类型徽标已经写了「触发场景」，这里再填一遍
      // 会变成「[触发场景] 米家场景「回家」· 触发场景」。
      capability: "",
      detail: name || !catalog.ready ? null : did,
      chips: [...cooldownChips(action, t), ...idempotentChips(action, t)],
      raw,
      unresolved: !name && catalog.ready,
    };
  }

  const device = catalog.devices.get(did);
  const spec = device?.spec?.[iid];

  const isActionCall = iid.startsWith("action.");
  // type_name 最准（如 play-text）；老 spec 缺这个字段时退回描述文案。
  const actionType = spec?.type_name ?? spec?.prop_description ?? spec?.description ?? "";
  const kind: TaskActionKind = !isActionCall
    ? "control"
    : SPEECH_ACTION_TYPE.test(actionType)
      ? "broadcast"
      : "deviceAction";
  const capability = isActionCall
    ? actionCapability(spec, iid)
    : spec
      ? specLabel(iid, spec)
      : iid;

  let detail: string | null;
  if (isActionCall) {
    const text = (v: unknown): string =>
      typeof v === "string" ? v : JSON.stringify(v ?? null);
    const names = spec?.in_params ?? [];
    // task 边界槽在后端是 list[dict]，params 的元素形状没有校验：CLI 接受任意 JSON，
    // 写成字符串 / 对象都能落库。收敛成数组再走后面的逻辑，别让一条脏动作把整页抛崩。
    const params = Array.isArray(action.params)
      ? action.params
      : action.params === null || action.params === undefined
        ? []
        : [action.params];
    // 名字按**原始下标**取，再滤掉空值：先 filter 会让后面参数的名字整体前移，
    // 把「text-content」贴到下一个参数上。
    const kept = params
      .map((p, i) => ({ p, name: names[i]?.name }))
      .filter(({ p }) => p !== null && p !== undefined && p !== "");
    // 若将来接入「执行前按画面生成文案」，params 里会是 {{result}} 占位符（后端暂未
    // 产出，见 RESULT_PLACEHOLDER 的注释）；真拼出来展示等于把模板当成品给住户看。
    // 只声明一个入参的动作保留原文（「你好」不必写成「text 你好」）；声明了多个入参时
    // 即便空位被滤掉，剩下那个也要带名，否则布尔标志又会变成孤零零的 false。
    detail = kept.length
      ? kept.some(({ p }) => RESULT_PLACEHOLDER.test(text(p)))
        ? t("tasks.generatedByOmni")
        : names.length <= 1 && kept.length === 1
          ? text(kept[0].p)
          : kept
              .map(({ p, name }) => (name ? `${name} ${text(p)}` : text(p)))
              .join(t("tasks.actionParamSep"))
      : null;
  } else {
    const text = spec ? propValueText(spec, action.value) : null;
    detail = text ?? (action.value === null || action.value === undefined ? null : String(action.value));
  }

  return {
    kind,
    device: device ? device.name : catalog.ready ? t("tasks.unknownDevice", { did }) : did,
    room: device?.room,
    capability,
    detail,
    chips: [
      ...cooldownChips(action, t),
      ...idempotentChips(action, t),
    ],
    raw,
    unresolved: !device && catalog.ready,
  };
}

/** 动作的能力名。动作 spec 的 `prop_description` 是 MiOT 英文 type_name
 *  （play-text / ptz-calibrate），中文词表里没有，直接过 `specLabel` 会在中文界面
 *  露出机器名；云端译名在 `description`，形如「服务名 动作名」，而设备名里通常
 *  已经含服务名，剥掉前缀只留动作名。译名为空时回落到 `specLabel`。 */
function actionCapability(
  spec: BackendPropSpec | undefined,
  iid: string,
): string {
  if (!spec) return iid;
  const full = spec.description?.trim();
  if (!full) return specLabel(iid, spec);
  const service = spec.service_description?.trim();
  if (service && full.startsWith(`${service} `)) {
    const rest = full.slice(service.length + 1).trim();
    if (rest) return rest;
  }
  return full;
}

/** 冷却：后端只给分钟级 cooldown_minutes；未配置就不显示。 */
function cooldownChips(action: TaskAction, t: Translate): string[] {
  const minutes = action.cooldownMinutes;
  return minutes === null || minutes === undefined
    ? []
    : [t("tasks.chipCooldownMinutes", { n: minutes })];
}

/** 幂等：只标显式声明的 false。后端 `RuleAction.idempotent` 默认 true，rule 的动作
 *  整槽透传到 task 时走 `model_dump`、默认值被实体化——前端拿到的 true 分不清
 *  「显式声明幂等」与「没写」，绝大多数设备直控又都是幂等的，逐行挂一枚等于没说。 */
function idempotentChips(action: TaskAction, t: Translate): string[] {
  return action.idempotent === false ? [t("tasks.chipNonIdempotent")] : [];
}
