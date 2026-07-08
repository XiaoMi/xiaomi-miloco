/**
 * omni health 契约测试:realRetryOmniProbe + realSubscribeOmniHealth。
 *
 * subscribeOmniHealth 用 EventSource,node 环境没有原生 EventSource,mock 掉。
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import {
  realRetryOmniProbe,
  realSubscribeOmniHealth,
} from "@/api/real";

const originalFetch = globalThis.fetch;

afterEach(() => {
  vi.restoreAllMocks();
  globalThis.fetch = originalFetch;
});


describe("realRetryOmniProbe", () => {
  it("POST /api/admin/omni-config/retry 并解出 data", async () => {
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init) => {
      const url = typeof input === "string" ? input : input.toString();
      expect(url).toContain("/api/admin/omni-config/retry");
      expect(init?.method).toBe("POST");
      return new Response(
        JSON.stringify({
          code: 0,
          message: "ok",
          data: {
            active: {
              label: "", model: "m", base_url: "https://x/v1",
              api_key_masked: "", has_key: true,
              health: {
                state: "ok", code: null, message: "",
                since_ms: 0, consecutive_failures: 0,
                next_probe_at_ms: null, last_probe_at_ms: null,
                last_probe_result: null,
              },
            },
            profiles: [],
          },
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    }) as typeof globalThis.fetch;

    const state = await realRetryOmniProbe();
    expect(state.active.health.state).toBe("ok");
  });
});


describe("realSubscribeOmniHealth", () => {
  it("挂 'omni_health' listener,收到消息回调,返回可 unsubscribe", () => {
    // Mock EventSource:capture listener,提供手动 emit + close 方法
    const listeners: Record<string, ((ev: MessageEvent) => void)[]> = {};
    let closed = false;
    class FakeEventSource {
      constructor(public url: string) {}
      addEventListener(name: string, cb: (ev: MessageEvent) => void) {
        (listeners[name] ??= []).push(cb);
      }
      close() { closed = true; }
    }
    (globalThis as unknown as { EventSource: typeof FakeEventSource }).EventSource = FakeEventSource;

    const received: unknown[] = [];
    const unsub = realSubscribeOmniHealth((h) => received.push(h));

    // 模拟 backend 推一条
    const health = {
      state: "warn", code: "unreachable", message: "网络不通",
      since_ms: 1000, consecutive_failures: 3,
      next_probe_at_ms: 2000, last_probe_at_ms: null, last_probe_result: null,
    };
    listeners["omni_health"][0](
      { data: JSON.stringify(health) } as MessageEvent,
    );
    expect(received).toEqual([health]);

    unsub();
    expect(closed).toBe(true);
  });

  it("JSON 解析失败时不 crash", () => {
    const listeners: Record<string, ((ev: MessageEvent) => void)[]> = {};
    class FakeEventSource {
      constructor(public url: string) {}
      addEventListener(name: string, cb: (ev: MessageEvent) => void) {
        (listeners[name] ??= []).push(cb);
      }
      close() {}
    }
    (globalThis as unknown as { EventSource: typeof FakeEventSource }).EventSource = FakeEventSource;

    const received: unknown[] = [];
    realSubscribeOmniHealth((h) => received.push(h));
    listeners["omni_health"][0](
      { data: "not-json" } as MessageEvent,
    );
    expect(received).toEqual([]);  // 没崩,也没记录
  });
});
