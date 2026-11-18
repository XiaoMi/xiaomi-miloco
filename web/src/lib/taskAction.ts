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
 *   是英文的人类可读名（Play Text），改取云端译名，见 `capabilityLabel`。
 * - **值**：`prop.*` 走 `propValueText`（bool / 枚举 / 单位三态）；`action.*` 取 params；
 *   `scene` 取场景名。读不出来时回落成原始值，不猜。
 *
 * 与 ``lib/actionText.ts`` 的分工：那边读的是**动作台账**（`value_json` 字符串），
 * 这边读的是**任务动作槽里的原始动作对象**，两者字段形态不同，不要合并。
 */

import {
  capabilityLabel,
  propValueText,
  type TaskDeviceMeta,
} from "@/api/real";
import {
  containsResultPlaceholder,
  paramsPhrase,
} from "@/lib/actionText";
import type { TaskAction } from "@/lib/types";

type Translate = (key: string, options?: Record<string, unknown>) => string;

/** `action.<siid>.<aiid>` 是通用设备动作通道，不等于 TTS：只有「逐字念出」的
 *  播报（音箱 play-text）才贴「播报」；`execute-text-directive` 是让小爱理解后自己
 *  执行的一句话、还带 silent-execution，不保证出声，按设备动作展示。
 *  接入别的厂商 TTS 时按实测的 type_name 逐个加进正则，别用 `tts` 这种宽匹配。 */
const SPEECH_ACTION_TYPE = /play-text/i;

/** 老 spec 缺 `type_name` 时退回 `prop_description`——它是英文的人类可读名
 *  （``Play Text``），不是 URN 里的 `play-text`，直接过正则匹配不上。按 CLI
 *  catalog 的同款规则归一化（小写、空格与下划线转连字符）再比。 */
const normalizeActionType = (s: string): string =>
  s.trim().toLowerCase().replace(/[\s_]+/g, "-");

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
  // type_name 最准（URN 里的 play-text）；老 spec 缺这个字段时退回 prop_description，
  // 归一化后再过正则，否则 "Play Text" 会被误判成设备动作。
  const actionType = normalizeActionType(
    spec?.type_name ?? spec?.prop_description ?? spec?.description ?? "",
  );
  const kind: TaskActionKind = !isActionCall
    ? "control"
    : SPEECH_ACTION_TYPE.test(actionType)
      ? "broadcast"
      : "deviceAction";
  // 与活动流（actionText.describeAction）共用同一个能力名口径。
  const capability = spec ? capabilityLabel(iid, spec) : iid;

  let detail: string | null;
  if (isActionCall) {
    // task 边界槽在后端是 list[dict]，params 的元素形状没有校验：CLI 接受任意 JSON，
    // 写成字符串 / 对象都能落库。收敛成数组再走后面的逻辑，别让一条脏动作把整页抛崩。
    const params = Array.isArray(action.params)
      ? action.params
      : action.params === null || action.params === undefined
        ? []
        : [action.params];
    // 入参拼接与活动流共用同一个实现（paramsPhrase）：绑名、滤空位、引号、分隔符
    // 只有一套，否则同一条动作在两页会长得不一样。占位符这一档任务详情要换成
    // 「文案由画面生成」，所以先判一次。
    detail = containsResultPlaceholder(params)
      ? t("tasks.generatedByOmni")
      : paramsPhrase(spec, params, t);
  } else {
    const text = spec ? propValueText(spec, action.value) : null;
    // 与 params 那条同口径：非字符串按 JSON 显示。String() 会把对象 / 数组压成
    // [object Object] / "3,4" 这种读不出信息的字，而同样的脏数据在 params 那侧
    // 是原样 JSON——同一个槽里的两种形状不该有两种回落。
    const literal =
      action.value === null || action.value === undefined
        ? null
        : typeof action.value === "string"
          ? action.value
          : JSON.stringify(action.value);
    detail = text ?? literal;
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
