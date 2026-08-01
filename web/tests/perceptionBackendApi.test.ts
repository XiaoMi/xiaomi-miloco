/**
 * ``/api/admin/perception-backend`` 两个 API 包装的契约测试。
 *
 * 这两条是界面与后端之间**唯一**的通道,而它们此前没有任何覆盖 —— 同一个模块的
 * 其余部分都有。URL 写错、方法写错、忘了从 NormalResponse 里取 .data,任何一样都会
 * 让整张卡片变成"加载失败",而类型检查与其它测试全绿。这个功能已经因为一次字段
 * 改名整体 500 过一次,教训就在这里。
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { realGetPerceptionBackend, realSetPerceptionBackend } from "@/api/real";

const originalFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = originalFetch;
});

const PAYLOAD = {
  backend: "local",
  local_vision: { base_url: "http://127.0.0.1:18800", has_token: true },
  health: {
    status: "ok", model_loaded: true, gate_available: false,
    gate_error: "mamba_ssm missing", device: "cuda:0", backend: "codec",
    auth_required: true, auth_ok: true,
  },
  error: null,
  blocking_static_rules: ["厨房明火关阀"],
  cloud_hint: "",
  local_capabilities: {
    needs_api_key: false, audio: false, identity: false,
    suggestions: false, static_rule_execution: false,
  },
};

function mock(status = 200, body: unknown = { code: 0, message: "ok", data: PAYLOAD }) {
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  globalThis.fetch = vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url: String(url), init });
    return new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    });
  }) as unknown as typeof fetch;
  return calls;
}

describe("realGetPerceptionBackend", () => {
  it("打到正确的端点并解开 NormalResponse", async () => {
    const calls = mock();
    const s = await realGetPerceptionBackend();
    expect(calls[0].url).toContain("/api/admin/perception-backend");
    expect(s.backend).toBe("local");
    // 卡片上每一处判定都依赖这些字段真的被带过来。
    expect(s.health?.auth_ok).toBe(true);
    expect(s.health?.gate_available).toBe(false);
    expect(s.blocking_static_rules).toEqual(["厨房明火关阀"]);
    expect(s.local_capabilities.static_rule_execution).toBe(false);
  });
});

describe("realSetPerceptionBackend", () => {
  it("以 POST 发出请求体,并回传新状态", async () => {
    const calls = mock();
    const s = await realSetPerceptionBackend({
      backend: "local",
      base_url: "http://gpu:18800",
      token: "secret",
    });
    expect(calls[0].init?.method).toBe("POST");
    expect(JSON.parse(String(calls[0].init?.body))).toEqual({
      backend: "local",
      base_url: "http://gpu:18800",
      token: "secret",
    });
    expect(s.backend).toBe("local");
  });

  it("后端拒绝时抛出,且带上原因 —— 卡片靠它给用户看 toast", async () => {
    mock(400, { detail: "以下规则会在感知层直接控制设备…厨房明火关阀" });
    await expect(
      realSetPerceptionBackend({ backend: "local", base_url: "http://gpu:18800" }),
    ).rejects.toThrow(/厨房明火关阀/);
  });
});
