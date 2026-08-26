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
 * 2. **缓存命中要单独计价**。cache ⊆ input，服务商对命中部分基本都打折，而折扣往往很深：
 *    MiMo v2.5 的命中价是未命中价的 1/50。实机实测（命中占输入 49.2%）下，按原价算
 *    会把费用高估 87%。命中价没低于输入价时，设置弹窗会直接把高估幅度算给用户看。
 *
 * ⚠️ 第三条：**没有录过价就按 0 算，并把是哪些模型点出来**。绝不能拿编出来的占位价充数——
 *    那会让一个从没设过价的模型也显示出像样的金额，与真按住户单价算出来的数无从分辨。
 *    故 `pricingFor` 对「没有依据」的模型返回 null（由 `summarizeCost` 记进 unpriced，
 *    金额贡献 0），界面在「费用估算」后用黄色叹号点名是哪些模型；要可编辑起始值的
 *    走 `seedPricingFor`，它给的是一份**全 0** 的空单价。
 *
 * 作用域：单价按**模型名**存，而模型的唯一身份其实是 (模型名 + Base URL)——
 * 自 DB schema v3 起用量表已经记录了 base_url，所以「按复合键存」在技术上是可行的。
 * 现在仍按模型名存是一个**有意的取舍**：改键会让住户已保存的单价全部失配，
 * 而这件事该单独一步做、带迁移。代价是同一模型名挂在两个不同 endpoint 且价格不同时，
 * 两边共用一份单价——设置弹窗里对此有说明。
 */

import type { UsageStats, UsageTimelinePoint } from "@/lib/types";
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
  /** 分项名。取值与计价口径一一对应（flat 出 input/cache/output，modality 出各模态）。 */
  key: "input" | "cache" | "text" | "video" | "audio" | "output";
  amount: number;
}

export interface CostResult {
  total: number;
  /**
   * 分项拆解。**界面目前只用 total**；parts 留着是因为用例靠它钉住「钱是怎么算出来的」——
   * 只断言总额的话，两个方向相反的口径错误可以互相抵消而测不出来。
   */
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

/**
 * 空单价：**全 0**。没手动录过价的模型就是这个。
 *
 * 为什么不给「像样的占位值」：一个编出来的价目会算出一个像样的金额，与真按住户单价
 * 估出来的数在界面上无从分辨，可信度却差一个量级。全 0 则算出 0——**0 是个显然不对
 * 的数**，看见它就知道要去录价，不会被误当成结论。哪些模型没录价，由界面上「费用估算」
 * 后面的黄色叹号点名。
 *
 * 名字里刻意不带 DEFAULT：那会招下一个人把它当成合理默认值直接用来展示。
 */
export const EMPTY_MODEL_PRICING: ModelPricing = {
  mode: "flat",
  input: 0,
  text: 0,
  video: 0,
  audio: 0,
  output: 0,
  cache: 0,
};

export const PRICING_STORAGE_KEY = "web:usage:pricing";

/**
 * 已知模型的预填单价。住户还是能改，这里只是省掉「上官网抄一遍」这一步，
 * 也避免住户从一份全 0 的空单价起步——那样费用一栏一直是「按 0 计」，等于没有估算。
 *
 * 每条都必须注明**出处与抓取日期** —— 服务商随时可能调价，将来核对时得知道
 * 这几个数是哪天从哪抄的，而不是猜。
 */
export const KNOWN_MODEL_PRICING: {
  /** 匹配用量表里的 model 列。带厂商前缀（xiaomi/mimo-v2.5）与不带的都要命中。 */
  match: RegExp;
  /** 该价目本身的计价货币，仅作提示——货币是全局设置，不由这里改。 */
  currency: string;
  pricing: ModelPricing;
}[] = [
  {
    // MiMo v2.5 —— https://mimo.mi.com/models/zh-CN/mimo-v2.5（2026-08-20 抓取）
    // 输入(缓存未命中) ¥1/百万、输入(缓存命中) ¥0.02/百万、输出 ¥2/百万。
    //
    // 官方价目**不按模态分价**：文本 / 图像 / 视频 / 音频共用同一套输入价，
    // 输出只有文本。故 mode 取「不区分模态」。各模态价一并填成与输入价相同，
    // 这样住户手动切到「区分模态」时算出来的钱与不区分完全一致，不会因为切了
    // 个视角就跳数。
    //
    // 命中价是未命中价的 1/50 —— 在这个模型上「缓存单独计价」不是边角优化：
    // 命中率五成时，按原价算会把费用高估近九成。
    match: /(^|\/)mimo-v2\.5$/i,
    currency: "¥",
    pricing: {
      mode: "flat",
      input: 1,
      cache: 0.02,
      output: 2,
      text: 1,
      video: 1,
      audio: 1,
    },
  },
];

/** 命中预填表则返回其单价副本，否则 null。 */
export function knownPricingFor(model: string): ModelPricing | null {
  const hit = KNOWN_MODEL_PRICING.find((k) => k.match.test(model.trim()));
  return hit ? { ...hit.pricing } : null;
}

function defaultPricing(): UsagePricing {
  return { currency: "¥", per: PER_MTOKENS, byModel: {} };
}

/** 这份单价的来源，决定界面该不该把算出来的钱当回事。 */
export type PricingSource =
  /** 住户自己填过并保存 */
  | "user"
  /** 命中 KNOWN_MODEL_PRICING，有公开价目出处 */
  | "known"
  /** 两者皆无——算出来的钱没有任何依据 */
  | "unset";

export function pricingSourceFor(p: UsagePricing, model: string): PricingSource {
  if (p.byModel[model]) return "user";
  if (knownPricingFor(model)) return "known";
  return "unset";
}

/**
 * 取某模型**有依据的**单价：住户存过的 → 已知模型预填。两者都没有则返回 null。
 *
 * 返回可空是刻意的：这样每个展示位置都被类型系统逼着写出「没单价时显示什么」，
 * 而不能顺手落到占位价上把编出来的钱显示成估算值。要草稿值请用 seedPricingFor。
 */
export function pricingFor(p: UsagePricing, model: string): ModelPricing | null {
  return p.byModel[model] ?? knownPricingFor(model);
}

/**
 * 设置弹窗用的起始值：有依据的优先，没有就给一份全 0 的空单价让住户填。
 * 只有「住户看得见、改得动」的地方才该调它。
 */
export function seedPricingFor(p: UsagePricing, model: string): ModelPricing {
  return pricingFor(p, model) ?? { ...EMPTY_MODEL_PRICING };
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
 * 把明细行按模型折成计价用的拆分（残差规则见 usageTokens.textResidual）。
 *
 * 放在 lib 而不是组件里：单价弹窗要用它列出「本周期有哪些模型」并算命中率提示。
 * 注意它**不参与算钱**——钱一律走 costInputsByTarget 那把「模型名 + endpoint」的键，
 * 总览、明细行、弹窗预览、时间分布浮层四处同键，否则各处的合计会互相对不上。
 */
export function costInputsByModel(stats: UsageStats): Map<string, CostInput> {
  const out = new Map<string, CostInput>();
  for (const r of stats.rows) {
    const add = costInputOf(r.breakdown);
    const cur = out.get(r.model);
    if (!cur) {
      out.set(r.model, { ...add });
      continue;
    }
    cur.text += add.text;
    cur.video += add.video;
    cur.audio += add.audio;
    cur.output += add.output;
    cur.cache += add.cache;
  }
  return out;
}

/**
 * 折成**计价目标**：一个目标 = 一个「模型名 + endpoint」，与明细表的分行方式同一把键。
 *
 * 为什么合计不能按模型名折：区分模态那档里，命中量是按 `命中 / 输入` 的比例摊到各模态的，
 * 而这个比例对 token **不是线性的**——把两个 endpoint 先加起来再摊，和分别摊完再相加，
 * 结果不相等（两边命中率不同、或模态配比不同时）。明细按目标算、合计按模型名算，
 * 就会出现「各行费用之和 ≠ 顶部合计」且没有任何报错。同一把键是唯一能保证可加的做法。
 *
 * 不分模态那档本来就是线性的、怎么折都相等；各模态单价相等时也退化成线性——
 * 预填的 MiMo v2.5 正好两者都占，所以这个偏差在自测里几乎不可能暴露。
 */
export function costInputsByTarget(
  stats: UsageStats,
): { model: string; baseUrl: string; input: CostInput }[] {
  // 分隔符用 \u001f：模型名与 URL 都可能含空格
  const out = new Map<string, { model: string; baseUrl: string; input: CostInput }>();
  for (const r of stats.rows) {
    const baseUrl = r.base_url ?? "";
    const key = `${r.model}\u001f${baseUrl}`;
    const add = costInputOf(r.breakdown);
    const cur = out.get(key);
    if (!cur) {
      out.set(key, { model: r.model, baseUrl, input: { ...add } });
      continue;
    }
    cur.input.text += add.text;
    cur.input.video += add.video;
    cur.input.audio += add.audio;
    cur.input.output += add.output;
    cur.input.cache += add.cache;
  }
  return [...out.values()];
}

/**
 * 一个时间桶的费用：**逐「模型名 + endpoint」按各自单价算完再相加**。
 *
 * 桶是跨模型合并的，所以不能拿某一个模型的单价去乘整桶 token——那是拿甲的价算
 * 乙的量，而算出来的数看起来和别处一样可信。折叠键与顶部合计、明细行一致。
 *
 * 桶里只要有**一个有用量却没录过单价**的目标就整桶返回 null，不给部分合计：
 * 浮层里没有「费用估算」旁边那种叹号可以点名缺谁，一个偏小的数在这里无从分辨。
 */
export function costOfTimelinePoint(
  p: UsageTimelinePoint,
  pricing: UsagePricing,
): number | null {
  let total = 0;
  let priced = false;
  for (const t of p.targets) {
    const used = t.text + t.video + t.audio + t.output;
    if (used <= 0 && t.cache <= 0) continue; // 零用量的目标不参与，也不该拖累整桶
    const pr = pricingFor(pricing, t.model);
    if (!pr) return null;
    total += estimateCost(
      { text: t.text, video: t.video, audio: t.audio, output: t.output, cache: t.cache },
      pr,
      pricing.per,
    ).total;
    priced = true;
  }
  return priced ? total : null;
}

/**
 * 每个模型的费用：先逐「模型名 + endpoint」按各自单价算，再归到模型名下。
 *
 * 单价弹窗底部的预览与顶部合计必须是同一个数——它们是同一屏上、同一段时间的
 * 「总共花了多少」。而「先按模型名把两个 endpoint 加起来再算」与「分别算完再相加」
 * 在区分模态那档并不相等（命中量按 `命中 / 输入` 摊，这个比例对 token 不线性），
 * 所以两处不能各自折叠：这里只出 per-model 的数，合计由调用方相加，折叠键与
 * costInputsByTarget / summarizeCost 完全一致。
 *
 * 没录过单价的模型按**全 0** 计入而不是跳过——弹窗要显示「按当前表单值这个模型算多少」，
 * 而全 0 的贡献恰好是 0，于是它与 summarizeCost（把没依据的模型排除在外）的合计仍相等。
 */
export function costPerModelFromTargets(
  targets: { model: string; baseUrl: string; input: CostInput }[],
  p: UsagePricing,
): Map<string, number> {
  const out = new Map<string, number>();
  for (const { model, input } of targets) {
    const pr = pricingFor(p, model) ?? EMPTY_MODEL_PRICING;
    out.set(model, (out.get(model) ?? 0) + estimateCost(input, pr, p.per).total);
  }
  return out;
}

export interface CostSummary {
  /** 有单价的那些模型的合计。unpriced 非空时这是**部分**合计，不是全部。 */
  total: number;
  /** 参与合计的模型数。0 表示一分钱都算不出来。 */
  priced: number;
  /** 有用量、但没有任何单价依据的模型名（已排序，供界面点名）。 */
  unpriced: string[];
}

/**
 * 跨模型汇总费用，并把「哪些模型算不出来」一并带出来。
 *
 * 之所以要返回 unpriced 而不是内部静默跳过：跳过会得到一个看起来完整、实际漏算的合计，
 * 而漏掉的那部分可能是大头。界面必须能说出「少算了谁」。
 */
export function summarizeCost(
  targets: { model: string; baseUrl: string; input: CostInput }[],
  p: UsagePricing,
): CostSummary {
  let total = 0;
  // 钱按**目标**加（与明细同键，保证可加），而模型数与点名按**模型名**去重——
  // 界面要说的是「哪几个模型没录价」，不是「哪几个 endpoint」。
  const pricedModels = new Set<string>();
  const unpricedModels = new Set<string>();
  for (const { model, input } of targets) {
    const pr = pricingFor(p, model);
    if (!pr) {
      unpricedModels.add(model);
      continue;
    }
    total += estimateCost(input, pr, p.per).total;
    pricedModels.add(model);
  }
  return {
    total,
    priced: pricedModels.size,
    unpriced: [...unpricedModels].sort(),
  };
}

/**
 * 命中价看起来**没有真打折**（≥ 基准价的 95%）。
 *
 * 只做「该不该告警」这一件事。刻意不给「会高估多少」——那个数要拿一个**假定的**
 * 服务商折扣才算得出来；若拿住户自己填的这份没打折的价当基准，结果恒等于 0。
 * 告警要说的是「你把命中按原价填了，而本周期命中占了这么多」，占比本身有依据、够用。
 */
export function cacheLooksUndiscounted(pr: ModelPricing): boolean {
  const base = pr.mode === "flat" ? pr.input : pr.text;
  return base > 0 && pr.cache >= base * 0.95;
}


// ── 持久化（localStorage，与 web:theme / web:lang 同一套路）──────────
// 不进后端：这是「本机的估算设置」，且免掉一轮读写端点。将来若要跟着模型档案
// 跨设备同步，再搬进 config.json 的 omni profile。

/**
 * 把弹窗的改动叠加到**已存**的单价表上。
 *
 * 为什么不能直接保存草稿：弹窗只列当前统计周期里出现过的模型（没有用量就没有行可调），
 * 而单价表是跨周期共用的一张全量表。草稿按「本周期的模型」从零重建，直接整表写回
 * 等于把上周录过、这周没用到的模型的单价**静默删掉**——那些价是一条条手敲的，
 * 界面上只会变成「—」并被算进「没录价」，没有任何提示。
 *
 * 只写住户真的动过的模型还有第二个作用：没动过的已知模型不会被写进本机表，
 * 它的来源仍是「预填」而不是「用户价」——否则以后代码里更新官方价目，
 * 这台机器会被自己存下的同值副本永久盖住。
 */
export function mergeEditedPricing(
  stored: UsagePricing,
  draft: UsagePricing,
  touched: Iterable<string>,
): UsagePricing {
  const byModel = { ...stored.byModel };
  for (const m of touched) {
    const pr = draft.byModel[m];
    if (pr) byModel[m] = pr;
  }
  // currency / per 跟草稿走：它们是整表级设置，弹窗里就能改
  return { ...draft, byModel };
}

/** 单价必须是有限非负数；否则回落到给定兜底值。 */
function num(v: unknown, fallback: number): number {
  return typeof v === "number" && Number.isFinite(v) && v >= 0 ? v : fallback;
}

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
      for (const [model, raw] of Object.entries(parsed.byModel)) {
        // 存量数据可能来自旧版本，缺的字段补齐时优先用该模型的已知价目，
        // 没有就补 0——别用编出来的数去补一个有公开价目的模型。
        const fill = knownPricingFor(model) ?? EMPTY_MODEL_PRICING;
        // 逐字段校验，不是整条铺上去：本机存储是住户、扩展、以及将来版本的自己
        // 都能写的地方。存进一个字符串价（`{"input":"1"}`）会让计价算出 NaN，
        // 一路显示成「¥NaN」且不报错——界面输入框拦得住，存储层拦不住。
        if (!raw || typeof raw !== "object") continue; // 整条不可用 → 保持默认
        const q = raw as Partial<ModelPricing>;
        base.byModel[model] = {
          mode: q.mode === "flat" || q.mode === "modality" ? q.mode : fill.mode,
          input: num(q.input, fill.input),
          text: num(q.text, fill.text),
          video: num(q.video, fill.video),
          audio: num(q.audio, fill.audio),
          output: num(q.output, fill.output),
          cache: num(q.cache, fill.cache),
        };
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
