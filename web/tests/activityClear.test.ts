/**
 * 「日志」页一键清理的 API 层契约。
 *
 * 一次点击 = 三个 POST（三处存储，缺一处住户就会看到"清理没生效"）：
 *   - POST /api/events/clear                        → meaningful_events（miloco.db）
 *   - POST /api/perception/on-demand-logs/clear     → on_demand_log（miloco.db）
 *   - POST /api/actions/clear                       → action_ledger（observability.db，
 *                                                     「触发场景」就在这本台账里）
 * 汇总各自 deleted 条数；任一边失败整体 reject（UI 在 finally 里照样重拉真实状态）。
 * `/api/actions/clear` 所在的 observability router 在 perf.enabled=false 时不挂载，
 * 那种部署（日志页本来就没有动作流）下 404 按 0 条处理，不算清理失败。
 *
 * 不连真 backend：直接覆写 globalThis.fetch 伪造响应；afterEach 还原。
 */

import { describe, it, expect, vi, afterEach } from "vitest";
import { realClearActivityLogs } from "@/api/real";
import { ApiError } from "@/api/client";

const originalFetch = globalThis.fetch;

afterEach(() => {
  vi.restoreAllMocks();
  globalThis.fetch = originalFetch;
});

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function fail(status: number, message: string): Response {
  return new Response(JSON.stringify({ code: status, message }), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** 记录每次请求的 "METHOD path",并按 url 返回预设响应。 */
function mockClearEndpoints(opts: {
  events?: number;
  onDemand?: number;
  actions?: number;
  failEvents?: boolean;
  failOnDemand?: boolean;
  failActions?: boolean;
  /** 模拟 perf.enabled=false：observability router 未挂载 */
  actionsMissing?: boolean;
}): string[] {
  const calls: string[] = [];
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input.toString();
    calls.push(`${init?.method ?? "GET"} ${new URL(url, "http://localhost").pathname}`);
    if (url.includes("/api/events/clear")) {
      if (opts.failEvents) return fail(500, "events boom");
      return json({ code: 0, message: "ok", data: { deleted: opts.events ?? 0 } });
    }
    if (url.includes("/api/perception/on-demand-logs/clear")) {
      if (opts.failOnDemand) return fail(500, "od boom");
      return json({ code: 0, message: "ok", data: { deleted: opts.onDemand ?? 0 } });
    }
    if (url.includes("/api/actions/clear")) {
      if (opts.actionsMissing) return fail(404, "Not Found");
      if (opts.failActions) return fail(500, "actions boom");
      // 这个端点是裸 dict（observability router 不走 NormalResponse 包装）
      return json({ deleted: opts.actions ?? 0 });
    }
    return new Response("{}", { status: 404 });
  }) as unknown as typeof fetch;
  return calls;
}

const ALL_THREE = [
  "POST /api/actions/clear",
  "POST /api/events/clear",
  "POST /api/perception/on-demand-logs/clear",
];

describe("clearActivityLogs（一键清理）", () => {
  it("同时 POST 三个清理端点并汇总删除条数（含触发场景）", async () => {
    const calls = mockClearEndpoints({ events: 12, onDemand: 3, actions: 4 });

    await expect(realClearActivityLogs()).resolves.toEqual({
      events: 12,
      onDemand: 3,
      actions: 4,
    });

    expect(calls.sort()).toEqual(ALL_THREE);
  });

  it("三边都为空时返回 0（幂等,再点一次不报错）", async () => {
    mockClearEndpoints({});
    await expect(realClearActivityLogs()).resolves.toEqual({
      events: 0,
      onDemand: 0,
      actions: 0,
    });
  });

  it("事件清理失败时整体抛出,但三个请求都已发出", async () => {
    const calls = mockClearEndpoints({ failEvents: true, onDemand: 5, actions: 1 });

    const err = await realClearActivityLogs().catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(500);
    expect(calls.sort()).toEqual(ALL_THREE);
  });

  it("按需日志清理失败时同样整体抛出", async () => {
    mockClearEndpoints({ failOnDemand: true, events: 1 });
    const err = await realClearActivityLogs().catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(500);
  });

  it("动作台账清理失败时同样整体抛出（不能默默留下触发场景）", async () => {
    mockClearEndpoints({ failActions: true, events: 1, onDemand: 1 });
    const err = await realClearActivityLogs().catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(500);
  });

  it("perf 关闭 / 端点未挂载（404）按 0 条处理，不误报清理失败", async () => {
    mockClearEndpoints({ actionsMissing: true, events: 2, onDemand: 1 });
    await expect(realClearActivityLogs()).resolves.toEqual({
      events: 2,
      onDemand: 1,
      actions: 0,
    });
  });
});
