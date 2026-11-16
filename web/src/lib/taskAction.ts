/**
 * task 边界动作 → 一句话。
 *
 * 台账里的动作是 `{did, iid, value|params}` 的机器口径，详情页要回答的却是
 * 「哪个设备在做什么」。三类取数各自有来源与回落：
 *
 * - **设备**：`did` → `/api/miot/home` 的设备名与房间。取不到就显式标「未知设备」，
 *   不拿裸 did 冒充名字——那看起来像解析成功了。
 * - **能力**：`iid` → 设备 spec 的 `prop_description || description`，与设备页、日志页
 *   共用同一个 `specLabel`，同一个属性三处同名。
 * - **值**：`prop.*` 走 `propValueText`（bool / 枚举 / 单位三态）；`action.*` 取 params；
 *   `scene` 取场景名。读不出来时回落成原始值，不猜。
 *
 * 与 ``lib/actionText.ts`` 的分工：那边读的是**动作台账**（`value_json` 字符串），
 * 这边读的是**任务动作槽里的原始动作对象**，两者字段形态不同，不要合并。
 */

import { propValueText, specLabel, type TaskDeviceMeta } from "@/api/real";
import type { TaskAction } from "@/lib/types";

type Translate = (key: string, options?: Record<string, unknown>) => string;

export interface ActionCatalog {
  /** 与 realListDeviceMeta 返回的设备元数据同源，不另立一份。 */
  devices: Map<string, TaskDeviceMeta>;
  /** scene_id → 场景名 */
  scenes: Map<string, string>;
  /** 设备表是否已落地。未落地时 did 原样显示（不报「未知设备」——那是「查过了没有」）。 */
  ready: boolean;
  t: Translate;
}

export type TaskActionKind = "broadcast" | "control" | "scene";

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
      chips: cooldownChips(action, t),
      raw,
      unresolved: !name && catalog.ready,
    };
  }

  const device = catalog.devices.get(did);
  const spec = device?.spec?.[iid];

  const isActionCall = iid.startsWith("action.");
  const kind: TaskActionKind = isActionCall ? "broadcast" : "control";
  const capability = spec ? specLabel(iid, spec) : iid;

  let detail: string | null;
  if (action.generate) {
    // 生成式动作：带该标记时 params 里是 {{result}} 占位符，直接显示会被当成最终文案。
    detail = t("tasks.generatedByOmni");
  } else if (isActionCall) {
    const params = (action.params ?? [])
      .filter((p) => p !== null && p !== undefined && p !== "")
      .map((p) => (typeof p === "string" ? p : JSON.stringify(p)));
    detail = params.length ? params.join(t("tasks.actionParamSep")) : null;
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

/** 冷却：秒级字段优先、0 表示显式关闭；后端只给分钟级时回落到分钟。 */
function cooldownChips(action: TaskAction, t: Translate): string[] {
  const seconds = action.cooldownSeconds;
  if (seconds !== null && seconds !== undefined) {
    return [
      seconds === 0
        ? t("tasks.chipNoCooldown")
        : t("tasks.chipCooldownSeconds", { n: seconds }),
    ];
  }
  const minutes = action.cooldownMinutes;
  if (minutes !== null && minutes !== undefined) {
    return [t("tasks.chipCooldownMinutes", { n: minutes })];
  }
  return [];
}

/** 幂等三态：true / false / 未配置。未配置不显示——那是「没写」，不是一种状态。 */
function idempotentChips(action: TaskAction, t: Translate): string[] {
  if (action.idempotent === true) return [t("tasks.chipIdempotent")];
  if (action.idempotent === false) return [t("tasks.chipNonIdempotent")];
  return [];
}
