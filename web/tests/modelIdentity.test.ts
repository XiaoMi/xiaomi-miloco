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
    // 这四组是实测撞车过的组合：直接截尾在这里会把两行截成完全一样。
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

  it("怎么放宽都分不开时退回原文（去 scheme），绝不给出重复项", () => {
    // 构造两个只在极深处差一个字符、且超过 hardMax 的 URL
    const base = "https://" + "x".repeat(90);
    const m = shortenUrlSet([base + "/a", base + "/b"], 14);
    expect(new Set(short(m)).size).toBe(2);
    expect(short(m).every((v) => !v.startsWith("https://"))).toBe(true);
  });

  it("重复 URL 只算一项，不会因此判定撞车", () => {
    const a = "https://api.x.com/v1";
    const m = shortenUrlSet([a, a], 12);
    expect(m.size).toBe(1);
  });
});
