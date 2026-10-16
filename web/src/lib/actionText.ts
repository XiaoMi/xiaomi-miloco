/**
 * 动作台账行 → 一句话。台账存的是机器口径（`iid` = `prop.2.1` / `action.5.1`，
 * `value_json` = 按 action_type 变形的 JSON），这一页要回答的却是「到底干了什么」。
 *
 * 三类取数，各自有各自的来源与回落：
 * - **名字**：`iid` → spec 的 `prop_description || description`，过 zhLabel 词表
 *   （与设备控制页同一个 `specLabel`，同一个属性两页同名）。
 * - **值**：`value_json` 按 `action_type` 分派，再按 spec 的 format / value_list /
 *   unit / in_params 读成人话。
 * - **回落**：spec 取不到（设备不在 home 里 / 规格缓存没热起来）、值 JSON 读不出来、
 *   或值不在枚举里——**一律返回 null，由调用方退回原始 iid + 原始 JSON**。永不猜：
 *   猜错的名字比原始键更糟，原始键至少能被搜到。
 *
 * 中文拼接（顿号、书名号内的引号）走 i18next，不写死标点——英文语境下「、」是错的。
 */

import { propValueText, specLabel, type BackendPropSpec } from "@/api/real";

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

/** 动作入参 → 「名字 值」。一个参数时省掉参数名改加引号：TTS 这类动作的实际
 *  参数名（text / content）住户看不懂，引号已经说明了「这是原文」。 */
function paramsPhrase(
  spec: BackendPropSpec,
  params: unknown[],
  t: Translate,
): string | null {
  const text = (v: unknown): string =>
    typeof v === "string" ? v : JSON.stringify(v ?? null);
  if (params.length === 0) return null;
  if (params.length === 1) return t("actions.quoted", { v: text(params[0]) });
  const names = spec.in_params ?? [];
  return params
    .map((v, i) => {
      const name = names[i]?.name;
      return name ? `${name} ${text(v)}` : text(v);
    })
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
    const label = specLabel(iid, s);
    const phrase = paramsPhrase(s, value, t);
    return phrase === null ? label : `${label} ${phrase}`;
  }

  return null;
}
