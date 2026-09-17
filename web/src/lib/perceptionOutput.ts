/**
 * 「设置 → 感知输入」的**感知输出**档：详细判定输出开关（verbose）+ 判定理由语言。
 *
 * 纯逻辑、无 React/DOM 依赖，便于 node 环境单测直接引用（同 perceptionInput.ts）。
 *
 * 后端契约（热读，免重启）：
 *   perception.engine.verbose          false(默认)=只回命中规则 id 数组；true=逐条 hit+不限字数 reason
 *   perception.engine.output_language  auto(默认)/zh/en —— 判定理由写哪种语言
 * 两个默认值必须与 backend settings.yaml 一致。
 */

/** 判定理由语言。auto = 跟随界面语言（后端按 MILOCO_APP_LANG → 系统 locale → 英文推断）。 */
export const PERCEPTION_OUTPUT_LANGUAGES = ["auto", "zh", "en"] as const;
export type PerceptionOutputLanguage = (typeof PERCEPTION_OUTPUT_LANGUAGES)[number];

/** 默认：不输出详细理由（只回命中规则 id 数组，省 token、判定更快）。 */
export const DEFAULT_PERCEPTION_VERBOSE = false;
/** 默认：跟随界面语言。 */
export const DEFAULT_OUTPUT_LANGUAGE: PerceptionOutputLanguage = "auto";

/** 远端值收敛：非 true 一律 false（老后端不返该字段 → false）。 */
export function normalizePerceptionVerbose(raw: unknown): boolean {
  return raw === true;
}

/** 远端值收敛：只认 zh/en，其余（含缺失、坏值）回 auto。 */
export function normalizeOutputLanguage(raw: unknown): PerceptionOutputLanguage {
  return raw === "zh" || raw === "en" ? raw : "auto";
}

/**
 * PUT payload：verbose 开关。字段名必须与后端 PerceptionConfigBody 一致
 * （写错会被 pydantic 静默忽略，表现为"保存成功但没生效"）。
 *
 * 为什么拆成两个单用途构造器、而不是一个 `{verbose, language}` 的合并函数：
 * 后端 PUT 是**局部合并**（只写请求里非 None 的字段），所以"只改 A"的调用方一旦在 payload 里
 * 顺手带上 B 的默认值，就会把用户设过的 B 按回默认。真实事故：切界面语言时用合并构造器同步
 * 语言，payload 带了 `perception_verbose: false` → 住户打开的「详细判定输出」被静默关掉。
 * 单用途构造器让这种事故在结构上就写不出来。
 */
export function buildPerceptionVerbosePayload(verbose: boolean): {
  perception_verbose: boolean;
} {
  return { perception_verbose: verbose };
}

/** PUT payload：判定理由输出语言（同上，只带这一个字段）。 */
export function buildPerceptionOutputLanguagePayload(
  language: PerceptionOutputLanguage,
): {
  perception_output_language: PerceptionOutputLanguage;
} {
  return { perception_output_language: language };
}

/**
 * 把当前界面语言同步给后端（写 `perception.engine.output_language`，热读下个窗口生效）。
 *
 * 为什么要有这一步：感知 system prompt 里的"输出语言"要跟随住户看到的界面语言，而后端
 * 并不知道网页上选了 zh 还是 en（那是 i18next/localStorage 的事）。切语言时把它同步过去，
 * 后端 `auto` 档就能落到具体语言；只在原生菜单/环境变量指定时，`auto` 靠 MILOCO_APP_LANG
 * 兜住。
 *
 * fire-and-forget：同步失败绝不该影响"切语言"本身（老后端没这个字段、或接口一时不可用，
 * 都只是让后端继续用 auto 推断），故调用方只需 `.catch(() => {})`。
 * api 用**动态 import**：i18n 模块 → api 模块 → i18n 是静态循环，只有延迟到调用时才安全。
 *
 * **只带 output_language 一个字段**（见上方单用途构造器的说明）：这一步是"切语言"的副作用，
 * 不能顺带改写住户自己设过的 verbose 等开关。
 */
export async function syncOutputLanguageToBackend(lang: string): Promise<void> {
  // 只在**真浏览器页面**里发请求。判据用 document 而不是 window：node 单测的 setup 给
  // client.ts 造了 window 桩（没有 document），只看 window 会在这里打出无效 URL 的请求。
  if (typeof window === "undefined" || typeof document === "undefined") return;
  const value: PerceptionOutputLanguage = lang === "en" ? "en" : "zh";
  const { updatePerceptionConfig } = await import("@/api");
  await updatePerceptionConfig(buildPerceptionOutputLanguagePayload(value));
}
