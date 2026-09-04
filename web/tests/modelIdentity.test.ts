/**
 * Base URL 的展示形式。核心是 shortenUrlSet 的「压短且保证互不相同」——
 * 压完撞车就等于显示了却分辨不出，而那是显示 URL 的全部意义。
 */
import { describe, it, expect } from "vitest";
import { shortenUrl, shortenUrlSet } from "@/lib/modelIdentity";

describe("shortenUrl", () => {
  const A = "https://api.xiaomimimo.com/v1";
  const B = "https://api.xiaomimimo.com/v1-test";
  const C = "https://mimo-staging.internal.corp/v1";
  const LONG_A = "https://api.xiaomimimo.com/openai/v1/chat";
  const LONG_B = "https://api.xiaomimimo.com/openai/v1beta/chat";

  it("先去掉 scheme —— 每行都一样的 8 个字符不该占预算", () => {
    expect(shortenUrl(A, 99)).toBe("api.xiaomimimo.com/v1");
    expect(shortenUrl("http://x.io/v1", 99)).toBe("x.io/v1");
  });

  it("没超上限就原样返回，不加省略号", () => {
    expect(shortenUrl(A, 21)).toBe("api.xiaomimimo.com/v1");
    expect(shortenUrl(A, 21)).not.toContain("…");
  });

  it("保住完整尾段 —— 差异在尾部，截掉就白显示", () => {
    for (const n of [14, 18, 22, 28]) {
      expect(shortenUrl(A, n).endsWith("/v1"), `上限 ${n}`).toBe(true);
      expect(shortenUrl(B, n).endsWith("/v1-test"), `上限 ${n}`).toBe(true);
    }
  });

  it("**同名两行截完必须还能分辨** —— 这是整个函数存在的理由", () => {
    // 三对 URL × 四个上限。其中 LONG_A/LONG_B 在四个上限下直接截尾都会撞成同一串，
    // A/B 在 14、18 撞——这正是「按固定规则逐个截」不够用的地方。
    for (const n of [14, 18, 22, 28]) {
      expect(shortenUrl(A, n), `A/B 上限 ${n}`).not.toBe(shortenUrl(B, n));
      expect(shortenUrl(A, n), `A/C 上限 ${n}`).not.toBe(shortenUrl(C, n));
      expect(shortenUrl(LONG_A, n), `长 A/B 上限 ${n}`).not.toBe(shortenUrl(LONG_B, n));
    }
  });

  it("主机不同时保留主机头，能认出「是哪家」", () => {
    expect(shortenUrl(C, 22).startsWith("mimo-staging")).toBe(true);
  });

  it("尾段快占满预算时退化成纯头部省略（保尾优先）", () => {
    // 尾段 /openai/v1beta/chat 有 19 字符，上限 22 时主机头只剩 2 → 退化
    const r = shortenUrl(LONG_B, 22);
    expect(r.startsWith("…")).toBe(true);
    expect(r.endsWith("/chat")).toBe(true);
  });

  it("产出长度不超上限", () => {
    for (const u of [A, B, C, LONG_A, LONG_B]) {
      for (const n of [12, 14, 18, 22, 28, 40]) {
        expect(shortenUrl(u, n).length, `${u} @ ${n}`).toBeLessThanOrEqual(n);
      }
    }
  });
});

describe("同一地址在不同预算下形状不同 —— 跨面板必须共用一份", () => {
  it("尾段吃满预算时会从「保主机头」退化成「纯头部省略」", () => {
    // 时间分布浮层与明细表就在同一屏上、读的是同一份数据，住户拿浮层里某一行去表里找
    // 对应那行是它们并排的理由。两处若各用各的预算，同一个 endpoint 一个以主机名开头、
    // 一个以省略号开头，肉眼不像同一台机器——所以短地址由页面层算一次、两处共用。
    const u = "https://api.corp.example.com/openai/v1-test";
    const wide = shortenUrl(u, 22);
    const tight = shortenUrl(u, 18);
    expect(wide).not.toBe(tight);
    expect(wide.startsWith("…")).toBe(false); // 预算够时保住主机头
    expect(tight.startsWith("…")).toBe(true); // 不够时退化成纯头部省略
    // 两者都保住了区分 endpoint 的尾段
    expect(wide.endsWith("/openai/v1-test")).toBe(true);
    expect(tight.endsWith("/openai/v1-test")).toBe(true);
  });
});

describe("shortenUrlSet", () => {
  const short = (m: Map<string, string>) => [...m.values()];

  it("差异落在预算之外时自动放宽，保证互不相同", () => {
    // 上限 20 下逐个截都会得到 "api-prod-cluster…/v1"，必须放宽
    const a = "https://api-prod-cluster-a.example.com/v1";
    const b = "https://api-prod-cluster-b.example.com/v1";
    expect(shortenUrl(a, 20)).toBe(shortenUrl(b, 20)); // 逐个截确实撞车
    const m = shortenUrlSet([a, b], 20);
    expect(new Set(short(m)).size).toBe(2);
  });

  it("能分开时不做无谓放宽，仍按 max 截", () => {
    const a = "https://api.xiaomimimo.com/v1";
    const b = "https://api.xiaomimimo.com/v1-test";
    const m = shortenUrlSet([a, b], 22);
    expect(m.get(a)).toBe(shortenUrl(a, 22));
    expect(m.get(b)).toBe(shortenUrl(b, 22));
  });

  it("单个 URL 不涉及撞车，按 max 截", () => {
    const a = "https://api.xiaomimimo.com/openai/v1/chat";
    expect(shortenUrlSet([a], 18).get(a)).toBe(shortenUrl(a, 18));
  });

  it("差异只在末尾一个字符也能分开", () => {
    const a = "https://a.example.com/very/long/path/segment/v1";
    const b = "https://a.example.com/very/long/path/segment/v2";
    expect(new Set(short(shortenUrlSet([a, b], 14))).size).toBe(2);
  });

  it("只差 scheme 的两条：兜底必须留着 scheme，否则永远分不开", () => {
    // 同一台机器先后换过是否走 TLS——身份键取记录原文，所以库里确实是两行。
    // 去掉 scheme 后两者完全相同，循环从 max 放宽到 hardMax 每一轮都撞车；
    // 若兜底再剥一次 scheme，表里就会显示两个一模一样的地址、后面数字却不同。
    const a = "http://192.168.1.10:8000/v1";
    const b = "https://192.168.1.10:8000/v1";
    const m = shortenUrlSet([a, b], 22);
    expect(new Set(short(m)).size).toBe(2);
    expect(m.get(a)).toBe(a);
    expect(m.get(b)).toBe(b);
  });

  it("共享长尾段、主机名只在中间不同：也会走到兜底，不是只有 scheme 那一类", () => {
    // 尾段 57 字符，从 max 到 hardMax 每一轮 budget 都 <= 3，压短退化成纯头部省略，
    // 取到的末尾字符全落在共享尾段里；唯一 budget > 3 的那轮（n=62）主机头只分到 4 个
    // 字符，"api-" 两条也一样。与上一条不同的是，这两条原文本身就分得开，不靠 scheme。
    const t = "/openai/deployments/gpt-realtime-preview/chat/completions";
    const a = `https://api-a.corp.example.com${t}`;
    const b = `https://api-b.corp.example.com${t}`;
    expect(a.replace(/^https?:\/\//, "")).not.toBe(b.replace(/^https?:\/\//, ""));
    const m = shortenUrlSet([a, b], 22);
    expect(m.get(a)).toBe(a);
    expect(m.get(b)).toBe(b);
    expect(new Set(short(m)).size).toBe(2);
  });

  it("主机名极长、只在末段差一个字符：压短后仍可分辨，且不带 scheme", () => {
    // 尾段被完整保住，所以第一轮预算就已经分得开——走的是循环的正常出口，
    // 不是兜底（兜底那条由上一个用例覆盖）。
    const base = "https://" + "x".repeat(90);
    const m = shortenUrlSet([base + "/a", base + "/b"], 14);
    expect(new Set(short(m)).size).toBe(2);
    expect(short(m).every((v) => !v.startsWith("https://"))).toBe(true);
    expect(short(m).every((v) => v.length <= 14)).toBe(true);
  });

  it("重复 URL 只算一项，不会因此判定撞车", () => {
    const a = "https://api.x.com/v1";
    const m = shortenUrlSet([a, a], 12);
    expect(m.size).toBe(1);
  });
});
