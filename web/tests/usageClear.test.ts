/**
 * 清除用量数据的两件事：**请求契约**，以及确认窗那句「连带删除某天」的**成立条件**。
 * 两者都属于「错了不会报错、只会静默做错事」，故放在一处。
 *
 * 先说请求契约。
 *
 * 这里守的是一件容易出事的事：**model 与 base_url 必须成对给或都不给**。
 * 只给一半时前端直接抛、不发请求：把半个目标丢掉会让「清这一项」静默变成
 * 「清所有模型」，这是这里最坏的失败方向。后端同样对半个目标返 400。
 *
 * 另一条：base_url 空串是**合法目标**（schema v3 之前的老数据，来源未记录），
 * 不是「未指定」。任何按真值判断的写法都会把这类行的定点清除变成全模型清除。
 *
 * 后一组（第二个 describe）不发任何请求，只判那句提示该不该出现：日表按整天删，所以
 * 只有边界那天真的落在日表已有的日期区间里才谈得上「连带」。说错的代价是双向的——
 * 只想清近期的人被吓退，而信了的人清完发现数据还在。
 */

import { describe, it, expect, vi, afterEach } from "vitest";
import { realClearUsageData } from "@/api/real";
import { dailyCaveatApplies } from "@/lib/usageTokens";

const originalFetch = globalThis.fetch;

afterEach(() => {
  vi.restoreAllMocks();
  globalThis.fetch = originalFetch;
});

function captureFetch() {
  const calls: { url: string; body: Record<string, unknown> }[] = [];
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    calls.push({
      url: typeof input === "string" ? input : input.toString(),
      body: JSON.parse(String(init?.body ?? "{}")),
    });
    return new Response(JSON.stringify({ code: 0, message: "ok", data: {} }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }) as unknown as typeof fetch;
  return calls;
}

describe("realClearUsageData 请求体", () => {
  it("无参 = 全清：四个字段都显式为 null，不靠字段缺省", async () => {
    const calls = captureFetch();
    await realClearUsageData();
    expect(calls[0].url).toContain("/api/admin/token-usage/clear");
    expect(calls[0].body).toEqual({ since_ms: null, model: null, base_url: null, from_date: null });
  });

  it("只给时间范围：目标仍为 null（所有模型）", async () => {
    const calls = captureFetch();
    await realClearUsageData({ sinceMs: 1_700_000_000_000 });
    expect(calls[0].body).toEqual({
      since_ms: 1_700_000_000_000,
      model: null,
      base_url: null,
      from_date: null,
    });
  });

  it("定点清除：model 与 base_url 原样带上", async () => {
    const calls = captureFetch();
    await realClearUsageData({
      sinceMs: null,
      model: "mimo-v2.5",
      baseUrl: "https://api.xiaomimimo.com/v1-test",
    });
    expect(calls[0].body).toEqual({
      since_ms: null,
      model: "mimo-v2.5",
      base_url: "https://api.xiaomimimo.com/v1-test",
      from_date: null,
    });
  });

  it("base_url 空串是目标本身，不能退化成「不限 endpoint」", async () => {
    const calls = captureFetch();
    await realClearUsageData({ model: "mimo-v2.5", baseUrl: "" });
    expect(calls[0].body).toEqual({ since_ms: null, model: "mimo-v2.5", base_url: "", from_date: null });
  });

  it("只给一半时抛错且**不发请求**：半个目标绝不能退化成全模型清除", async () => {
    const calls = captureFetch();
    await expect(realClearUsageData({ model: "mimo-v2.5" })).rejects.toThrow(/同时给/);
    await expect(realClearUsageData({ baseUrl: "https://api.x.com/v1" })).rejects.toThrow(
      /同时给/,
    );
    expect(calls).toHaveLength(0);
  });

  it("范围与目标可叠加", async () => {
    const calls = captureFetch();
    await realClearUsageData({ sinceMs: 42, model: "m", baseUrl: "u" });
    expect(calls[0].body).toEqual({
      since_ms: 42,
      model: "m",
      base_url: "u",
      from_date: null,
    });
  });

  it("界面显示的那一天原样带上：日表按盒子时区归日，提示按浏览器时区算", async () => {
    const calls = captureFetch();
    await realClearUsageData({ sinceMs: 1_700_000_000_000, fromDate: "2026-08-24" });
    expect(calls[0].body.from_date).toBe("2026-08-24");
  });

  it("全清不带边界日：没有「从哪天起」这回事", async () => {
    const calls = captureFetch();
    await realClearUsageData({ sinceMs: null, fromDate: "2026-08-24" });
    expect(calls[0].body.from_date).toBeNull();
  });
});

describe("「连带删除某天」这句提示的成立条件", () => {
  // 日表按 `date >= from_date` 整天删，但只有边界那天真的落在日表已有的日期区间里
  // 才谈得上「连带」。区间两头各挡一类落空。
  const EARLIEST = "2026-08-20";
  const LATEST = "2026-08-24";

  it("上界：边界日晚于日表最新日期（近 24 小时那档）→ 不说", () => {
    // 滚存截止天对齐且只搬更早的行，今天/昨天永远不在日表里
    expect(dailyCaveatApplies("2026-08-27", EARLIEST, LATEST)).toBe(false);
  });

  it("下界：边界日早于日表最早日期（盒子刚装几天）→ 不说", () => {
    // 8-15 是把下界拉开距离的**构造输入**，不是这组常量下「近 7 天」产得出的边界日：
    // EARLIEST=8-20 已意味着今天 ≥ 8-24（日表要滚存后才有 8-20），「近 7 天」边界日
    // 最早也只到 8-17。真实的下界落空长这样：今天 8-28、日表只有 8-22 起的数据，
    // 边界日 8-21 早于最早那天——`date >= 边界日` 删掉的都本就在所选范围内。
    expect(dailyCaveatApplies("2026-08-15", EARLIEST, LATEST)).toBe(false);
  });

  it("边界日正好是区间两端 → 都要说（那天会被整天删掉）", () => {
    expect(dailyCaveatApplies(LATEST, EARLIEST, LATEST)).toBe(true);
    expect(dailyCaveatApplies(EARLIEST, EARLIEST, LATEST)).toBe(true);
  });

  it("边界日落在区间内 → 要说", () => {
    expect(dailyCaveatApplies("2026-08-22", EARLIEST, LATEST)).toBe(true);
  });

  it("日表为空 / 接口只给了一头 → 不说，宁可不说也不说错", () => {
    expect(dailyCaveatApplies("2026-08-22", null, null)).toBe(false);
    expect(dailyCaveatApplies("2026-08-22", null, LATEST)).toBe(false);
    expect(dailyCaveatApplies("2026-08-22", EARLIEST, null)).toBe(false);
  });

  it("全清档没有边界日 → 不说", () => {
    expect(dailyCaveatApplies(null, EARLIEST, LATEST)).toBe(false);
  });

  it("跨月跨年按字典序比较仍成立（等宽零填充）", () => {
    expect(dailyCaveatApplies("2026-09-01", "2026-08-01", "2026-08-31")).toBe(false);
    expect(dailyCaveatApplies("2025-12-31", "2025-12-28", "2026-01-05")).toBe(true);
  });
});
