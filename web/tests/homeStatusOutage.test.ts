/**
 * realHomeStatus — 后端不可达时必须抛错，不能合成一份「成功的假状态」。
 *
 * 首页状态每 30s 轮询一次。三路请求各自 catch 成 null 的话，后端整体不可达时
 * 会返回 bound:false / authDegraded:false / 设备数 0——上层看来每次轮询都成功，
 * 于是状态条在后端重启的十几秒里退回黄色「未连」，红色失效提示也被一并吞掉。
 * 抛错才能让 useAsync 走 error 路径、保留上一份数据。
 *
 * 单路失败仍要按原逻辑降级：那是局部故障，其余数据还有意义。
 */

import { describe, it, expect, vi, afterEach } from "vitest";
import { realHomeStatus } from "@/api/real";

const origFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = origFetch;
  vi.restoreAllMocks();
});

function ok(data: unknown) {
  return new Response(JSON.stringify({ code: 0, message: "ok", data }), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

/** 各端点的最小可用形状——按真实响应给，别让测试数据本身成为失败原因。 */
function payloadFor(url: string) {
  if (url.includes("/api/miot/status")) {
    return { is_bound: true, auth_state: "ok" };
  }
  if (url.includes("/api/perception/engine/status")) {
    return { running: true };
  }
  // 家庭那一路：devices / areas 是必有字段
  return { devices: [], areas: [] };
}

/** 按 URL 决定成功还是失败；失败用网络级拒绝，模拟后端不可达。 */
function mockByUrl(fail: (url: string) => boolean) {
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(
      typeof input === "string" ? input : ((input as Request).url ?? input),
    );
    if (fail(url)) throw new TypeError("network down");
    return ok(payloadFor(url));
  }) as unknown as typeof fetch;
}

describe("realHomeStatus 在后端不可达时", () => {
  it("三路全灭 → 抛错（让上层保留旧数据，状态条不闪）", async () => {
    mockByUrl(() => true);
    await expect(realHomeStatus()).rejects.toThrow(/home status unavailable/);
  });

  it("三路全灭时不能返回「未绑定」的假状态", async () => {
    mockByUrl(() => true);
    // 若这里能拿到结果，说明假状态又回来了——状态条会退回「未连」
    const r = await realHomeStatus().catch(() => null);
    expect(r).toBeNull();
  });

  it("只有米家那一路失败 → 仍返回结果，按原逻辑降级", async () => {
    mockByUrl((u) => u.includes("/api/miot/status"));
    const r = await realHomeStatus();
    expect(r).toBeTruthy();
    expect(r.miot.bound).toBe(false); // 该路失败，降级为未绑定
  });

  it("只有感知那一路失败 → 仍返回结果", async () => {
    mockByUrl((u) => u.includes("/api/perception/engine/status"));
    const r = await realHomeStatus();
    expect(r).toBeTruthy();
  });
});
