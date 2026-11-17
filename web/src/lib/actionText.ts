/**
 * 动作台账行 → 一句话。台账存的是机器口径（`iid` = `prop.2.1` / `action.5.1`，
 * `value_json` = 按 action_type 变形的 JSON），这一页要回答的却是「到底干了什么」。
 *
 * 三类取数，各自有各自的来源与回落：
 * - **名字**：`iid` → spec 的 `prop_description || description`，过 zhLabel 词表
 *   （与设备控制页同一个 `specLabel`，同一个属性两页同名）；`action.*` 改走
 *   `capabilityLabel` 取云端译名，与任务详情同一个口径。
 * - **值**：`value_json` 按 `action_type` 分派，再按 spec 的 format / value_list /
 *   unit / in_params 读成人话。
 * - **回落**：spec 取不到（设备不在 home 里 / 规格缓存没热起来）、值 JSON 读不出来、
 *   或值不在枚举里——**一律返回 null，由调用方退回原始 iid + 原始 JSON**。永不猜：
 *   猜错的名字比原始键更糟，原始键至少能被搜到。
 *
 * 中文拼接（顿号、书名号内的引号）走 i18next，不写死标点——英文语境下「、」是错的。
 */

import {
  capabilityLabel,
  propValueText,
  specLabel,
  type BackendPropSpec,
} from "@/api/real";

/** 本模块只读台账行的这三个字段；用结构类型而不是 import BackendActionRow，
 *  免得 lib 层反向依赖 components 层（后者带 React 与 i18n 初始化）。 */
export interface ActionLike {
  action_type: string;
  iid: string | null;
  value_json: string | null;
}

/** spec 表：iid → spec（`/api/miot/home` 里每台设备那一份）。 */
export type SpecTable = Record<string, BackendPropSpec> | undefined;

/** i18next 的 t 收敛成最小签名——本模块只用 key 与插值两件事。 */
type Translate = (key: string, options?: Record<string, unknown>) => string;

function parseJson(raw: string | null): unknown {
  if (raw === null || raw === "") return undefined;
  try {
    return JSON.parse(raw);
  } catch {
    return undefined; // 台账列被人工改过 / 老格式——不猜结构，整行回落
  }
}

/** 单个属性 → 「名字 值」；值读不出来返回 null，由调用方整行回落。
 *  **不退化成「只有名字」**——那会把「值没读懂」这件事藏起来，住户看到的
 *  是一句读得通、但没说全的话，比原始键更难察觉。 */
function propPhrase(
  iid: string,
  spec: BackendPropSpec,
  value: unknown,
): string | null {
  const text = propValueText(spec, value);
  return text === null ? null : `${specLabel(iid, spec)} ${text}`;
}

/** 生成式动作 params 里的回填占位符。**后端尚未产出**（预留给「执行前按画面生成
 *  文案」的能力）；接入后由服务端替换，展示层任何时候都不该把模板原样露给住户。
 *  任务详情据此换成「文案由画面生成」。 */
export const RESULT_PLACEHOLDER = /\{\{\s*result\s*\}\}/;

const paramText = (v: unknown): string =>
  typeof v === "string" ? v : JSON.stringify(v ?? null);

/** params 里是否含回填占位符。 */
export function containsResultPlaceholder(params: unknown[]): boolean {
  return params.some((v) => RESULT_PLACEHOLDER.test(paramText(v)));
}

/** 动作入参 → 「名字 值」。单参加引号、不贴参数名（text / content 这类实际参数名
 *  住户看不懂，引号已经说明「这是原文」）；多参按 `in_params` 的**原始下标**绑名，
 *  空值先滤掉但名字不因此前移。空参数 / 全是空值时返回 null。
 *
 *  任务详情与活动流共用这一个实现 —— 同一条动作在两页必须长得一样，
 *  否则住户拿详情页去活动流核对时会以为是两件事。 */
export function paramsPhrase(
  spec: BackendPropSpec | undefined,
  params: unknown[],
  t: Translate,
): string | null {
  const names = spec?.in_params ?? [];
  const kept = params
    .map((v, i) => ({ v, name: names[i]?.name }))
    .filter(({ v }) => v !== null && v !== undefined && v !== "");
  if (kept.length === 0) return null;
  // 原始只有一个参数才算「单参」：多入参动作只是空位被滤掉时，剩下的仍要带名
  if (params.length === 1) {
    return t("actions.quoted", { v: paramText(kept[0].v) });
  }
  return kept
    .map(({ v, name }) => (name ? `${name} ${paramText(v)}` : paramText(v)))
    .join(t("actions.listSep"));
}

/**
 * 台账行 → 一句话；读不出人话时返回 null（调用方退回今天的样子）。
 * `spec` 是该行 `did` 的 spec 表——没有就返回 null，不做半程翻译。
 */
export function describeAction(
  row: ActionLike,
  spec: SpecTable,
  t: Translate,
): string | null {
  const iid = row.iid;
  if (!iid) return null;

  // 场景行：did/iid 落的是 scene_id，不在设备 spec 里；名字在 value_json 里。
  if (row.action_type === "scene_trigger") {
    const v = parseJson(row.value_json);
    const name =
      v && typeof v === "object" && "scene_name" in v
        ? (v as { scene_name?: unknown }).scene_name
        : undefined;
    if (typeof name !== "string" || !name) return null;
    return `${t("actions.typeSceneTrigger")}${t("actions.quoted", { v: name })}`;
  }

  if (!spec) return null;
  const value = parseJson(row.value_json);
  if (value === undefined) return null;

  if (row.action_type === "set_property") {
    // value_json = 裸标量（json.dumps(request.value)），不是 {iid: value}。
    const s = spec[iid];
    return s ? propPhrase(iid, s, value) : null;
  }

  if (row.action_type === "set_properties") {
    // iid 是逗号拼接的复数键，value_json 是 {iid: value} 全集。
    if (!value || typeof value !== "object" || Array.isArray(value)) return null;
    const byIid = value as Record<string, unknown>;
    const parts: string[] = [];
    for (const one of iid.split(",")) {
      const key = one.trim();
      const s = spec[key];
      // 一行里有一个属性翻不出来就整行回落：半程翻译会画出「开灯、prop.3.4 4000」
      // 这种半人半机器的行，比整行原始键更难读。
      if (!s) return null;
      const part = propPhrase(key, s, byIid[key]);
      if (part === null) return null;
      parts.push(part);
    }
    return parts.length > 0 ? parts.join(t("actions.listSep")) : null;
  }

  if (row.action_type === "call_action") {
    const s = spec[iid];
    if (!s || !Array.isArray(value)) return null;
    // 能力名走与任务详情同一个口径：动作的英文原名在中文界面读不出来，
    // 两页必须同名，否则住户拿任务详情去活动流核对时会以为是两件事。
    const label = capabilityLabel(iid, s);
    const phrase = paramsPhrase(s, value, t);
    return phrase === null ? label : `${label} ${phrase}`;
  }

  return null;
}
