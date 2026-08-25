/**
 * 费用估算的口径护栏。守的是两个「算错量级很大」的地方：
 *  1. 逐模态计价必须用残差（input − video − audio），不能拿 input 再乘一次文本价，
 *     否则 video / audio 被收两遍。
 *  2. 缓存命中必须能单独计价，否则命中率高时费用会成倍高估。
 *
 * 取值刻意用整数、占比刚好 1/2，让期望值能手算核对，不靠对照实现反推。
 */
import { describe, it, expect, beforeEach, afterEach } from "vitest";
import type { UsageStats } from "@/lib/types";
import {
  costInputsByModel,
  costInputsByTarget,
  costOfTimelinePoint,
  EMPTY_MODEL_PRICING,
  PER_MTOKENS,
  PRICING_STORAGE_KEY,
  cacheLooksUndiscounted,
  cacheOverstatePct,
  estimateCost,
  knownPricingFor,
  loadPricing,
  pricingFor,
  pricingSourceFor,
  seedPricingFor,
  summarizeCost,
  savePricing,
  type ModelPricing,
} from "@/lib/usagePricing";

// input = 100000 + 200000 + 100000 = 400000，cache 恰好占一半 → share = 0.5
const T = { text: 100_000, video: 200_000, audio: 100_000, output: 50_000, cache: 200_000 };

const MODALITY: ModelPricing = {
  mode: "modality",
  input: 9, // 故意给个不该被用到的值：modality 模式下读它就说明走错分支
  text: 1,
  video: 2,
  audio: 3,
  output: 4,
  cache: 0,
};
const FLAT: ModelPricing = { ...MODALITY, mode: "flat", input: 1 };

describe("estimateCost — 区分模态", () => {
  it("按残差与占比摊命中，总额可手算核对", () => {
    const r = estimateCost(T, MODALITY);
    // text 100000×0.5×1 + video 200000×0.5×2 + audio 100000×0.5×3 + output 50000×4
    // = 0.05 + 0.2 + 0.15 + 0.2（命中价 0，命中那一半不计）
    expect(r.total).toBeCloseTo(0.6, 10);
    expect(r.parts.reduce((a, p) => a + p.amount, 0)).toBeCloseTo(r.total, 10);
    expect(r.parts.map((p) => p.key)).toEqual(["text", "video", "audio", "output"]);
  });

  it("抬文本价只应带动『残差』那部分，不是整个 input（防 video/audio 收两遍）", () => {
    const base = estimateCost(T, MODALITY).total;
    const hi = estimateCost(T, { ...MODALITY, text: 2 }).total;
    // 残差未命中量 = 100000 × 0.5 = 50000 → 每 MTokens 加 1 元即 +0.05
    expect(hi - base).toBeCloseTo(0.05, 10);
    // 若误用 input（400000×0.5=200000）则会是 +0.2，这条断言正是用来钉死它
    expect(hi - base).not.toBeCloseTo(0.2, 3);
  });

  it("命中价能真正压低总额，且与告警给出的高估幅度自洽", () => {
    const cheap = estimateCost(T, MODALITY).total; // cache 价 0
    const full = estimateCost(T, { ...MODALITY, cache: MODALITY.text }).total;
    expect(full).toBeGreaterThan(cheap);
    const pct = cacheOverstatePct(T, MODALITY)!;
    // 命中占一半、命中价为 0 → 忽略折扣会高估 100%
    expect(pct).toBeCloseTo(100, 6);
  });

  it("命中价打了折就不该告警；只有没打折时才告警", () => {
    // 出厂占位价里命中价是文本价的十分之一 —— 已打折，不该亮告警
    expect(cacheLooksUndiscounted(MODALITY)).toBe(false);
    expect(cacheLooksUndiscounted({ ...MODALITY, cache: MODALITY.text })).toBe(true);
    // 差一点点（95% 以上）也算没打折
    expect(cacheLooksUndiscounted({ ...MODALITY, cache: MODALITY.text * 0.96 })).toBe(true);
    expect(cacheLooksUndiscounted({ ...MODALITY, cache: MODALITY.text * 0.5 })).toBe(false);
    // flat 模式看的是输入价，不是文本价
    expect(cacheLooksUndiscounted({ ...FLAT, input: 2, cache: 0.1, text: 0.1 })).toBe(false);
    expect(cacheLooksUndiscounted({ ...FLAT, input: 2, cache: 2, text: 99 })).toBe(true);
  });

  it("命中量超过输入时夹到输入量，各模态仍按命中价出账", () => {
    // 命中价必须非 0：若为 0，夹与不夹算出来都是「只剩输出」，测不出差别
    // （不夹时 1−share 为负、各项算成负数后被 add() 静默丢弃，总额恰好也等于输出）。
    const pr = { ...MODALITY, cache: 0.5 };
    const r = estimateCost({ ...T, cache: 10_000_000 }, pr);
    // 夹住后 share = 1 → 输入全按命中价：
    // (100000 + 200000 + 100000) × 0.5 / 1e6 = 0.2，加输出 0.2
    expect(r.total).toBeCloseTo(0.4, 10);
    // 四项都得在。不夹的话前三项算成负数被丢掉，只剩 output 一项
    expect(r.parts.map((p) => p.key)).toEqual(["text", "video", "audio", "output"]);
    for (const p of r.parts) expect(p.amount).toBeGreaterThan(0);
  });

  it("零用量 → 零费用、零分项", () => {
    const r = estimateCost({ text: 0, video: 0, audio: 0, output: 0, cache: 0 }, MODALITY);
    expect(r.total).toBe(0);
    expect(r.parts).toEqual([]);
  });
});

describe("estimateCost — 不区分模态", () => {
  it("只按未命中输入 / 命中 / 输出三项计", () => {
    const r = estimateCost(T, FLAT);
    // 未命中 200000×1 + 命中 200000×0 + 输出 50000×4 = 0.2 + 0 + 0.2
    expect(r.total).toBeCloseTo(0.4, 10);
    expect(r.parts.map((p) => p.key)).toEqual(["input", "output"]);
  });

  it("flat 模式不读各模态单价（改 video 价不影响结果）", () => {
    const a = estimateCost(T, FLAT).total;
    const b = estimateCost(T, { ...FLAT, video: 999 }).total;
    expect(b).toBeCloseTo(a, 10);
  });
});

describe("已知模型预填 — mimo-v2.5", () => {
  // 官方价目（https://mimo.mi.com/models/zh-CN/mimo-v2.5，2026-08-20）：
  // 输入未命中 ¥1/M、输入命中 ¥0.02/M、输出 ¥2/M，且不按模态分价。
  it("带不带厂商前缀都能命中，其它名字不命中", () => {
    expect(knownPricingFor("mimo-v2.5")?.input).toBe(1);
    expect(knownPricingFor("xiaomi/mimo-v2.5")?.input).toBe(1);
    expect(knownPricingFor("MiMo-V2.5")?.input).toBe(1);
    // 别把 pro / tts / v2 误当成 v2.5（页面未给它们的价）
    expect(knownPricingFor("mimo-v2.5-pro")).toBeNull();
    expect(knownPricingFor("mimo-v2.5-tts")).toBeNull();
    expect(knownPricingFor("mimo-v2")).toBeNull();
    expect(knownPricingFor("gemini-2.5-flash")).toBeNull();
  });

  it("三个价与官方一致，且默认走不区分模态", () => {
    const pr = knownPricingFor("mimo-v2.5")!;
    expect(pr.mode).toBe("flat");
    expect([pr.input, pr.cache, pr.output]).toEqual([1, 0.02, 2]);
  });

  it("官方不按模态分价 → 切到区分模态算出来的钱必须一样，不能因为换视角就跳数", () => {
    const pr = knownPricingFor("mimo-v2.5")!;
    const flat = estimateCost(T, pr).total;
    const byMod = estimateCost(T, { ...pr, mode: "modality" }).total;
    expect(byMod).toBeCloseTo(flat, 10);
  });

  it("按 .5 实机口径复算，与官方价手算结果一致", () => {
    // input 10.96M（其中 video 2.30M、audio 0）、output 189.1k、cache 5.39M
    const t = {
      text: 10_960_000 - 2_300_000,
      video: 2_300_000,
      audio: 0,
      output: 189_100,
      cache: 5_390_000,
    };
    const pr = knownPricingFor("mimo-v2.5")!;
    // (10.96−5.39)×1 + 5.39×0.02 + 0.1891×2 = 5.57 + 0.1078 + 0.3782
    expect(estimateCost(t, pr).total).toBeCloseTo(6.056, 3);
    // 忽略命中折扣会高估到 ¥11.34
    expect(estimateCost(t, { ...pr, cache: pr.input }).total).toBeCloseTo(11.338, 3);
  });

  it("pricingFor 优先级：住户存过的 > 预填；没依据返回 null", () => {
    const p = { currency: "¥", per: PER_MTOKENS, byModel: {} } as const;
    expect(pricingFor({ ...p, byModel: {} }, "mimo-v2.5")?.input).toBe(1);
    expect(pricingFor({ ...p, byModel: {} }, "mimo-v2.5")?.mode).toBe("flat");
    // 存过的赢
    const saved = { ...EMPTY_MODEL_PRICING, input: 42 };
    expect(pricingFor({ ...p, byModel: { "mimo-v2.5": saved } }, "mimo-v2.5")?.input).toBe(42);
    // 未知且没存过 → null。这里**必须**是 null 而不是占位价：
    // 落到占位价上就等于把编出来的钱显示成估算值。
    expect(pricingFor({ ...p, byModel: {} }, "who-knows")).toBeNull();
  });

  it("pricingSourceFor 三态分得开", () => {
    const p = { currency: "¥", per: PER_MTOKENS, byModel: {} };
    expect(pricingSourceFor(p, "who-knows")).toBe("unset");
    expect(pricingSourceFor(p, "mimo-v2.5")).toBe("known");
    expect(
      pricingSourceFor({ ...p, byModel: { "who-knows": EMPTY_MODEL_PRICING } }, "who-knows"),
    ).toBe("user");
    // 住户存过的要盖住预填，否则改不动已知模型的价
    expect(
      pricingSourceFor({ ...p, byModel: { "mimo-v2.5": EMPTY_MODEL_PRICING } }, "mimo-v2.5"),
    ).toBe("user");
  });

  it("seedPricingFor 只在没依据时给空单价，且给的是副本", () => {
    const p = { currency: "¥", per: PER_MTOKENS, byModel: {} };
    // 有依据的不该被空单价盖掉
    expect(seedPricingFor(p, "mimo-v2.5").input).toBe(1);
    const draft = seedPricingFor(p, "who-knows");
    expect(draft).toEqual(EMPTY_MODEL_PRICING);
    draft.video = 123;
    expect(EMPTY_MODEL_PRICING.video).not.toBe(123);
  });

  it("空单价必须**全为 0**——不许塞任何编出来的「像样默认值」", () => {
    // 这是本文件里最容易被"顺手改好"的一处：给它填上看似合理的价，
    // 从没录过价的模型就会显示出一个像样的金额，与真按住户单价估的数无从分辨。
    for (const [k, v] of Object.entries(EMPTY_MODEL_PRICING)) {
      if (k === "mode") continue;
      expect(v, `EMPTY_MODEL_PRICING.${k} 必须是 0`).toBe(0);
    }
    // 全 0 单价算出来的钱必须是 0，且不产生任何分项
    const r = estimateCost(
      { text: 9e6, video: 9e6, audio: 9e6, output: 9e6, cache: 3e6 },
      EMPTY_MODEL_PRICING,
    );
    expect(r.total).toBe(0);
    expect(r.parts).toEqual([]);
  });
});

describe("pricingFor / 持久化", () => {
  const store = new Map<string, string>();
  beforeEach(() => {
    store.clear();
    (globalThis as { localStorage?: unknown }).localStorage = {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => void store.set(k, v),
      removeItem: (k: string) => void store.delete(k),
    };
  });
  afterEach(() => {
    delete (globalThis as { localStorage?: unknown }).localStorage;
  });

  it("没配过的模型不给数，返回 null", () => {
    expect(pricingFor(loadPricing(), "never-configured")).toBeNull();
  });

  it("存了能读回来，货币与按模型单价都保留", () => {
    const p = loadPricing();
    p.currency = "$";
    p.byModel["mimo-v2.5"] = { ...EMPTY_MODEL_PRICING, video: 7.5 };
    savePricing(p);
    expect(store.get(PRICING_STORAGE_KEY)).toBeTruthy();

    const back = loadPricing();
    expect(back.currency).toBe("$");
    expect(back.byModel["mimo-v2.5"].video).toBe(7.5);
    expect(back.per).toBe(PER_MTOKENS);
  });

  it("存量数据缺字段时用出厂值补齐，不产生 undefined 单价", () => {
    store.set(PRICING_STORAGE_KEY, JSON.stringify({ byModel: { m: { video: 3 } } }));
    const back = loadPricing();
    expect(back.byModel["m"].video).toBe(3);
    expect(back.byModel["m"].output).toBe(EMPTY_MODEL_PRICING.output);
    expect(back.byModel["m"].mode).toBe(EMPTY_MODEL_PRICING.mode);
    // 补齐后可直接参与计算，不会算出 NaN
    expect(Number.isFinite(estimateCost(T, back.byModel["m"]).total)).toBe(true);
  });

  it("存储里是坏 JSON 时回落出厂值，不抛错", () => {
    store.set(PRICING_STORAGE_KEY, "{not json");
    expect(() => loadPricing()).not.toThrow();
    expect(loadPricing().currency).toBe("¥");
  });
});

describe("summarizeCost / costInputsByModel / costInputsByTarget", () => {
  const P = { currency: "¥", per: PER_MTOKENS, byModel: {} as Record<string, ModelPricing> };
  /** 1 MTokens 输入、1 MTokens 输出、无缓存无视频音频。 */
  const ci = (n = 1) => ({
    text: n * PER_MTOKENS,
    video: 0,
    audio: 0,
    output: n * PER_MTOKENS,
    cache: 0,
  });
  /** 每个模型一个目标（endpoint 留空）——旧用例只关心按模型名的取价与点名。 */
  const tg = (m: Record<string, ReturnType<typeof ci>>) =>
    Object.entries(m).map(([model, input]) => ({ model, baseUrl: "", input }));
  /** 输入 1、输出 2 → 每 1 MTokens 组合算 3 块。 */
  const FLAT: ModelPricing = {
    mode: "flat",
    input: 1,
    cache: 0,
    output: 2,
    text: 1,
    video: 1,
    audio: 1,
  };

  it("全都有单价：合计是各家之和，unpriced 为空", () => {
    const p = { ...P, byModel: { a: FLAT, b: FLAT } };
    const r = summarizeCost(tg({ a: ci(1), b: ci(2) }), p);
    expect(r.total).toBeCloseTo(3 + 6, 6);
    expect(r.priced).toBe(2);
    expect(r.unpriced).toEqual([]);
  });

  it("全都没单价：合计 0、priced 0，所有模型被点名", () => {
    const r = summarizeCost(tg({ z: ci(1), a: ci(1) }), P);
    expect(r.total).toBe(0);
    expect(r.priced).toBe(0);
    // 排过序，界面点名时次序稳定
    expect(r.unpriced).toEqual(["a", "z"]);
  });

  it("部分有单价：合计**只含**有价的那部分，其余点名带出", () => {
    const p = { ...P, byModel: { a: FLAT } };
    const r = summarizeCost(tg({ a: ci(1), b: ci(10) }), p);
    // b 是大头（30 块）却没单价：若被静默按占位价算进去，这里就不会是 3
    expect(r.total).toBeCloseTo(3, 6);
    expect(r.priced).toBe(1);
    expect(r.unpriced).toEqual(["b"]);
  });

  it("已知模型即使没存过也算有依据（走预填价目）", () => {
    const r = summarizeCost(tg({ "mimo-v2.5": ci(1) }), P);
    expect(r.priced).toBe(1);
    expect(r.unpriced).toEqual([]);
    // 输入 ¥1 + 输出 ¥2
    expect(r.total).toBeCloseTo(3, 6);
  });

  it("同名模型两个 endpoint：顶部合计必须等于各行费用之和", () => {
    // 区分模态 + 各模态单价不同 + 两个 endpoint 命中率不同 —— 这是唯一能让
    // 「先合并再摊命中」与「分别摊完再相加」分道扬镳的组合。合计若仍按模型名折，
    // 这条会红：界面上表现为各行加起来对不上顶部那个数，且没有任何报错。
    const MOD: ModelPricing = {
      mode: "modality",
      input: 0,
      text: 1,
      video: 8,
      audio: 3,
      cache: 0.02,
      output: 2,
    };
    const p = { ...P, byModel: { m: MOD } };
    const row = (base_url: string, input: number, video: number, cache: number) => ({
      model: "m",
      base_url,
      type: "realtime" as const,
      calls: 1,
      tokens: input,
      breakdown: { input, output: 1000, cache, video, audio: 0 },
    });
    const stats = {
      rows: [
        // 同一个模型名、两个 endpoint：命中率 80% 对 10%，视频占比也不同
        row("https://api.x.com/v1", 1_000_000, 600_000, 800_000),
        row("https://api.x.com/v1-test", 1_000_000, 50_000, 100_000),
      ],
    } as unknown as UsageStats;

    const targets = costInputsByTarget(stats);
    expect(targets).toHaveLength(2);
    const rowsTotal = targets.reduce(
      (acc, t) => acc + estimateCost(t.input, MOD, PER_MTOKENS).total,
      0,
    );
    expect(summarizeCost(targets, p).total).toBeCloseTo(rowsTotal, 9);

    // 反向钉住：按模型名先合并再算，确实会得出**另一个**数
    const merged = costInputsByModel(stats);
    const mergedTotal = estimateCost(merged.get("m")!, MOD, PER_MTOKENS).total;
    expect(Math.abs(mergedTotal - rowsTotal)).toBeGreaterThan(0.5);
  });

  it("costInputsByModel 把同模型的多行加起来，并对 input 取残差", () => {
    const row = (model: string, input: number, video: number, audio: number, output: number) => ({
      model,
      type: "realtime" as const,
      calls: 1,
      tokens: input + output,
      breakdown: { input, output, cache: 0, video, audio },
    });
    const m = costInputsByModel({
      rows: [row("a", 100, 30, 20, 5), row("a", 100, 0, 0, 5), row("b", 50, 0, 0, 1)],
    } as unknown as UsageStats);
    expect(m.size).toBe(2);
    // 残差：第一行 100−30−20=50，第二行 100 → 150
    expect(m.get("a")!.text).toBe(150);
    expect(m.get("a")!.video).toBe(30);
    expect(m.get("a")!.output).toBe(10);
    expect(m.get("b")!.text).toBe(50);
  });
});


describe("costOfTimelinePoint（时间分布浮层的钱）", () => {
  const P = (byModel: Record<string, ModelPricing>) => ({
    currency: "¥",
    per: PER_MTOKENS,
    byModel,
  });
  const FLAT = (input: number, output: number): ModelPricing => ({
    mode: "flat",
    input,
    cache: 0,
    output,
    text: input,
    video: input,
    audio: input,
  });
  const tgt = (model: string, base_url: string, text: number, output = 0) => ({
    model,
    base_url,
    text,
    video: 0,
    audio: 0,
    output,
    cache: 0,
  });
  const point = (targets: ReturnType<typeof tgt>[]) => ({
    ts: "2026-08-25T00:00:00.000Z",
    tokens: targets.reduce((a, t) => a + t.text + t.output, 0),
    text: targets.reduce((a, t) => a + t.text, 0),
    video: 0,
    audio: 0,
    output: targets.reduce((a, t) => a + t.output, 0),
    cache: 0,
    targets,
  });

  it("单模型：与直接按该模型计价相同", () => {
    const p = point([tgt("a", "https://x/v1", PER_MTOKENS)]);
    expect(costOfTimelinePoint(p, P({ a: FLAT(3, 0) }))).toBeCloseTo(3, 9);
  });

  it("两个模型各按各的价：不是拿其中一个的价乘整桶", () => {
    // a 便宜 b 贵；若按「第一个模型」的价乘整桶，会得出 2 而不是 11
    const p = point([
      tgt("a", "https://x/v1", PER_MTOKENS),
      tgt("b", "https://y/v1", PER_MTOKENS),
    ]);
    const pricing = P({ a: FLAT(1, 0), b: FLAT(10, 0) });
    expect(costOfTimelinePoint(p, pricing)).toBeCloseTo(11, 9);
    // 反向钉住旧做法的错法：拿排在前面那个模型的价乘整桶 token
    const wrong = (p.text / PER_MTOKENS) * 1;
    expect(Math.abs(wrong - 11)).toBeGreaterThan(8);
  });

  it("同名模型两个 endpoint：共用一份单价，仍各自计价再相加", () => {
    const p = point([
      tgt("a", "https://x/v1", PER_MTOKENS),
      tgt("a", "https://x/v1-test", PER_MTOKENS * 2),
    ]);
    expect(costOfTimelinePoint(p, P({ a: FLAT(2, 0) }))).toBeCloseTo(6, 9);
  });

  it("桶里有没录价的模型：整桶不给数，不给部分合计", () => {
    // 浮层里没有「费用估算」旁那种叹号可以点名缺谁，偏小的数无从分辨
    const p = point([
      tgt("a", "https://x/v1", PER_MTOKENS),
      tgt("nope", "https://y/v1", PER_MTOKENS * 100),
    ]);
    expect(costOfTimelinePoint(p, P({ a: FLAT(1, 0) }))).toBeNull();
  });

  it("零用量的目标既不计钱、也不拖累整桶", () => {
    const p = point([tgt("a", "https://x/v1", PER_MTOKENS), tgt("nope", "https://y/v1", 0)]);
    expect(costOfTimelinePoint(p, P({ a: FLAT(4, 0) }))).toBeCloseTo(4, 9);
  });

  it("空桶返回 null", () => {
    expect(costOfTimelinePoint(point([]), P({}))).toBeNull();
  });
});
