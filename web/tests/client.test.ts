/**
 * apiFetch 错误文案契约。
 *
 * 重点:backend 的 HTTPException(detail=对象)（omni preflight 失败就是这种）不能变成
 * "[object Object]"——住户至少要看得到 detail.message。
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { apiFetch } from "@/api/client";

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
  vi.restoreAllMocks();
});

function mockResponse(status: number, body: unknown, json = true) {
  globalThis.fetch = vi.fn(
    async () =>
      new Response(json ? JSON.stringify(body) : String(body), {
        status,
        headers: { "Content-Type": json ? "application/json" : "text/plain" },
      }),
  ) as unknown as typeof fetch;
}

async function messageOf(): Promise<string> {
  try {
    await apiFetch("/api/test");
    throw new Error("expected apiFetch to reject");
  } catch (e) {
    return e instanceof Error ? e.message : String(e);
  }
}

describe("apiFetch 4xx/5xx 错误文案", () => {
  it("FastAPI detail 是对象时取 detail.message，而不是 [object Object]", async () => {
    mockResponse(400, {
      detail: {
        ok: false,
        code: "rejected_authed",
        status: 400,
        latency_ms: 12,
        message: "已连接，但请求被拒绝（模型名或 API Key 可能有误）",
      },
    });
    expect(await messageOf()).toBe(
      "已连接，但请求被拒绝（模型名或 API Key 可能有误）",
    );
  });

  it("detail 是字符串时原样使用", async () => {
    mockResponse(400, { detail: "档案名不能为空" });
    expect(await messageOf()).toBe("档案名不能为空");
  });

  it("顶层 message（NormalResponse 风格）优先", async () => {
    mockResponse(404, { code: 1000, message: "Rule 'nope' not found" });
    expect(await messageOf()).toBe("Rule 'nope' not found");
  });

  it("detail 是数组（FastAPI 422 校验错误）时回退 HTTP 状态码", async () => {
    mockResponse(422, {
      detail: [
        { loc: ["body", "items"], msg: "field required", type: "value_error.missing" },
      ],
    });
    expect(await messageOf()).toBe("HTTP 422");
  });

  it("拿不到任何文案时回退 HTTP 状态码，不出现 [object Object]", async () => {
    mockResponse(400, { detail: { code: "no_key", ok: false } });
    expect(await messageOf()).toBe("HTTP 400");
  });

  it("响应体不是 JSON 时回退 HTTP 状态码", async () => {
    mockResponse(502, "<html>bad gateway</html>", false);
    expect(await messageOf()).toBe("HTTP 502");
  });
});
