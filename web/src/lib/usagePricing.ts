/**
 * 用量费用估算 —— 单价、口径、持久化。
 *
 * 定位：**本机估算**，不是服务商账单。单价由住户自己填，算出来的钱只用于
 * 「大概烧了多少」的量级判断。故所有展示处都带 ≈ 前缀与「估算」字样。
 *
 * 为什么不入库：单价是**可变配置**，token 是**不可变事实**。存 token、单价另存、
 * 读时计算，意味着单价填错改一下、全部历史自动跟着修正；一旦把算好的钱写进库
 * 就把当时的价冻住了，改不动也说不清哪条按哪个价算的。真正需要持久化的场景是
 * 「服务商某天调价」，那要的是**带生效日期的单价历史**，而不是存结果——存结果只是
 * 它的劣化近似（丢了可审计性）。所以这里全在前端算，DB 不动。
 *
 * ⚠️ 两个口径必须守住，算错的量级都很大：
 *
 * 1. **逐模态计价要用残差**。后端的 input 已经含 video + audio（还含图片），所以
 *    分模态时用的是 `text(残差) + video + audio`，不能拿 input 再乘一次文本价——
 *    那样 video / audio 会被收两遍（实测多算 34%）。
 * 2. **缓存命中要单独计价**。cache ⊆ input，服务商对命中部分基本都打折。以典型的
 *    62% 命中率为例，命中价若是输入价的 10% 而这里按原价算，会把费用高估 128%。
 *    命中价没低于输入价时，设置弹窗会直接把高估幅度算给用户看。
 *
 * 作用域：单价按**模型名**存。之所以不按「模型 + Base URL」存，是因为用量表
 * （token_usage / token_usage_daily）只记 model 一列、不记 base_url，按复合键存
 * 会得到一堆永远匹配不上的死键。代价是同一个模型名挂在两个不同 endpoint 上且
 * 价格不同时，估算分不开这两者——设置弹窗里对此有说明。
 */

import { textResidual } from "@/lib/usageTokens";

/** 计价方式：不区分模态（输入/输出两价）或区分模态（各模态各一价）。 */
export type PricingMode = "flat" | "modality";

/** 一个模型的单价，均为「每 MTokens」。 */
export interface ModelPricing {
  mode: PricingMode;
  /** flat：未命中缓存的输入价。 */
  input: number;
  /** modality：残差项（文本 + 图片 + 系统提示）单价。 */
  text: number;
  video: number;
  audio: number;
  output: number;
  /** 命中缓存部分的单价（两种 mode 都用）。 */
  cache: number;
}

export interface UsagePricing {
  /** 货币符号，纯展示用，不做汇率换算。 */
  currency: string;
  /** 单价的计价基数：每 100 万 token。 */
  per: number;
  /** key = 模型名（与用量表的 model 列对齐）。 */
  byModel: Record<string, ModelPricing>;
}

/** 参与计价的 token 拆分。text 是残差，cache ⊆ text + video + audio。 */
export interface CostInput {
  text: number;
  video: number;
  audio: number;
  output: number;
  cache: number;
}

export interface CostPart {
  /** i18n key 的后缀，见 usage.costPart* */
  key: "input" | "cache" | "text" | "video" | "audio" | "output";
  amount: number;
}

export interface CostResult {
  total: number;
  parts: CostPart[];
}

export const PER_MTOKENS = 1_000_000;

/** 把 TokenBreakdown 形态折成计价用的拆分（残差规则见 usageTokens.textResidual）。 */
export function costInputOf(b: {
  input: number;
  output: number;
  cache: number;
  video: number;
  audio: number;
}): CostInput {
  return {
    text: textResidual(b.input, b.video, b.audio),
    video: b.video,
    audio: b.audio,
    output: b.output,
    cache: b.cache,
  };
}

/** 出厂单价：占位值，住户必须自己按服务商价目改。 */
export const DEFAULT_MODEL_PRICING: ModelPricing = {
  mode: "modality",
  input: 1,
  text: 1,
  video: 2.5,
  audio: 1.5,
  output: 4,
  cache: 0.1,
};

export const PRICING_STORAGE_KEY = "web:usage:pricing";

function defaultPricing(): UsagePricing {
  return { currency: "¥", per: PER_MTOKENS, byModel: {} };
}

/** 取某模型的单价；没配过就给出厂值（不写回，等用户真保存时才落盘）。 */
export function pricingFor(p: UsagePricing, model: string): ModelPricing {
  return p.byModel[model] ?? { ...DEFAULT_MODEL_PRICING };
}

/**
 * 估算一段用量的费用。
 *
 * flat：(输入 − 命中) × 输入价 + 命中 × 命中价 + 输出 × 输出价
 * modality：Σ 各输入模态 ×（未命中按本模态价、命中按命中价）+ 输出 × 输出价
 *   —— 命中量无法从后端得知来自哪个模态，故按各模态占输入的比例摊。
 */
export function estimateCost(
  t: CostInput,
  pr: ModelPricing,
  per: number = PER_MTOKENS,
): CostResult {
  const input = t.text + t.video + t.audio;
  // 上游偶发不自洽（命中 > 输入）时夹住，避免算出负数
  const cache = Math.min(Math.max(t.cache, 0), Math.max(input, 0));
  const parts: CostPart[] = [];
  let total = 0;
  const add = (key: CostPart["key"], amount: number) => {
    if (amount <= 0) return;
    parts.push({ key, amount });
    total += amount;
  };

  if (pr.mode === "flat") {
    add("input", (Math.max(input - cache, 0) / per) * pr.input);
    add("cache", (cache / per) * pr.cache);
  } else {
    const share = input > 0 ? cache / input : 0;
    const byMod: [CostPart["key"], number, number][] = [
      ["text", t.text, pr.text],
      ["video", t.video, pr.video],
      ["audio", t.audio, pr.audio],
    ];
    for (const [key, v, price] of byMod) {
      if (v <= 0) continue;
      add(key, ((v * (1 - share)) / per) * price + ((v * share) / per) * pr.cache);
    }
  }
  add("output", (t.output / per) * pr.output);
  return { total, parts };
}

/**
 * 命中价看起来**没有真打折**（≥ 基准价的 95%）。
 *
 * 这是「该不该告警」的判据，与 cacheOverstatePct 是两件事：后者只要本周期有命中量
 * 就总能算出一个正数（它回答「若忽略折扣会高估多少」），拿它当触发条件会让告警恒亮，
 * 而告警文案声称的前提（命中价没低于输入价）在已填折扣价时是假的。
 */
export function cacheLooksUndiscounted(pr: ModelPricing): boolean {
  const base = pr.mode === "flat" ? pr.input : pr.text;
  return base > 0 && pr.cache >= base * 0.95;
}

/**
 * 忽略缓存折扣会把费用高估多少（倍率 − 1）。用于设置弹窗的告警：
 * 命中占比高时这个数很大，不提示的话住户会以为估算「差不多」。
 * 返回 null 表示无从判断（没有命中量、或基准价为 0）。
 */
export function cacheOverstatePct(
  t: Pick<CostInput, "text" | "video" | "audio" | "cache">,
  pr: ModelPricing,
): number | null {
  const input = t.text + t.video + t.audio;
  const cache = Math.min(Math.max(t.cache, 0), Math.max(input, 0));
  const base = pr.mode === "flat" ? pr.input : pr.text;
  if (input <= 0 || cache <= 0 || base <= 0) return null;
  // 全按输入价 vs 命中部分按命中价，两者的比值
  const discounted = input - cache + cache * (pr.cache / base);
  if (discounted <= 0) return null;
  return (input / discounted - 1) * 100;
}

// ── 持久化（localStorage，与 web:theme / web:lang 同一套路）──────────
// 不进后端：这是「本机的估算设置」，且免掉一轮读写端点。将来若要跟着模型档案
// 跨设备同步，再搬进 config.json 的 omni profile。

export function loadPricing(): UsagePricing {
  const base = defaultPricing();
  if (typeof localStorage === "undefined") return base;
  try {
    const raw = localStorage.getItem(PRICING_STORAGE_KEY);
    if (!raw) return base;
    const parsed = JSON.parse(raw) as Partial<UsagePricing>;
    if (typeof parsed.currency === "string" && parsed.currency) {
      base.currency = parsed.currency;
    }
    if (parsed.byModel && typeof parsed.byModel === "object") {
      for (const [model, pr] of Object.entries(parsed.byModel)) {
        // 逐字段校验：存量数据可能来自旧版本，缺字段就用出厂值补齐
        base.byModel[model] = { ...DEFAULT_MODEL_PRICING, ...(pr ?? {}) };
      }
    }
  } catch {
    // 解析失败 / 存储不可用（隐私模式）→ 用出厂值，不报错也不清库
  }
  return base;
}

export function savePricing(p: UsagePricing): void {
  if (typeof localStorage === "undefined") return;
  try {
    localStorage.setItem(
      PRICING_STORAGE_KEY,
      JSON.stringify({ currency: p.currency, byModel: p.byModel }),
    );
  } catch {
    // 配额满 / 隐私模式：静默放弃持久化，本次会话内的设置仍生效
  }
}
