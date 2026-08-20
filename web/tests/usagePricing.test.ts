/**
 * 费用估算的口径护栏。守的是两个「算错量级很大」的地方：
 *  1. 逐模态计价必须用残差（input − video − audio），不能拿 input 再乘一次文本价，
 *     否则 video / audio 被收两遍。
 *  2. 缓存命中必须能单独计价，否则命中率高时费用会成倍高估。
 *
 * 取值刻意用整数、占比刚好 1/2，让期望值能手算核对，不靠对照实现反推。
 */
import { describe, it, expect, beforeEach, afterEach } from "vitest";
import {
  DEFAULT_MODEL_PRICING,
  PER_MTOKENS,
  PRICING_STORAGE_KEY,
  cacheLooksUndiscounted,
  cacheOverstatePct,
  estimateCost,
  knownPricingFor,
  loadPricing,
  pricingFor,
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

  it("pricingFor 优先级：住户存过的 > 预填 > 通用占位", () => {
    const p = { currency: "¥", per: PER_MTOKENS, byModel: {} } as const;
    expect(pricingFor({ ...p, byModel: {} }, "mimo-v2.5").input).toBe(1);
    expect(pricingFor({ ...p, byModel: {} }, "mimo-v2.5").mode).toBe("flat");
    // 存过的赢
    const saved = { ...DEFAULT_MODEL_PRICING, input: 42 };
    expect(pricingFor({ ...p, byModel: { "mimo-v2.5": saved } }, "mimo-v2.5").input).toBe(42);
    // 未知模型落通用占位
    expect(pricingFor({ ...p, byModel: {} }, "who-knows")).toEqual(DEFAULT_MODEL_PRICING);
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

  it("没配过的模型给出厂值，且返回的是副本（改它不污染出厂常量）", () => {
    const p = loadPricing();
    const pr = pricingFor(p, "never-configured");
    expect(pr).toEqual(DEFAULT_MODEL_PRICING);
    pr.video = 123;
    expect(DEFAULT_MODEL_PRICING.video).not.toBe(123);
  });

  it("存了能读回来，货币与按模型单价都保留", () => {
    const p = loadPricing();
    p.currency = "$";
    p.byModel["mimo-v2.5"] = { ...DEFAULT_MODEL_PRICING, video: 7.5 };
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
    expect(back.byModel["m"].output).toBe(DEFAULT_MODEL_PRICING.output);
    expect(back.byModel["m"].mode).toBe(DEFAULT_MODEL_PRICING.mode);
    // 补齐后可直接参与计算，不会算出 NaN
    expect(Number.isFinite(estimateCost(T, back.byModel["m"]).total)).toBe(true);
  });

  it("存储里是坏 JSON 时回落出厂值，不抛错", () => {
    store.set(PRICING_STORAGE_KEY, "{not json");
    expect(() => loadPricing()).not.toThrow();
    expect(loadPricing().currency).toBe("¥");
  });
});
