/**
 * 契约测试 — backend → 前端类型层的解析。
 *
 * 覆盖:
 * - realListActivity:GET /api/events(meaningful_events)字段映射 + 分页 query
 * - realGetUsageStats:GET /api/admin/token-usage/{buckets,daily} 折算
 *
 * 不连真 backend:vi 拦截 fetch,伪造 NormalResponse 形状;afterEach 还原原 fetch.
 */

import { describe, it, expect, vi, afterEach } from "vitest";
import {
  realListActivity,
  realGetUsageStats,
  realGetOmniConfig,
  realUpdateRtspCamera,
  realUpdateOmniConfig,
  realActivateOmniConfig,
  realDeleteOmniConfig,
  realListOmniModels,
  realTestOmniConfig,
  _resetUsageStatsCache,
} from "@/api/real";

// 这些用例直接覆写 globalThis.fetch(非 vi.spyOn),vi.restoreAllMocks() 只还原
// spy 不还原直接赋值——不显式存/还原会让最后一个 mock 泄漏到后续测试文件(同进程
// 跑时拿到脏 fetch)。存原引用,afterEach 里还原。
const originalFetch = globalThis.fetch;

afterEach(() => {
  vi.restoreAllMocks();
  globalThis.fetch = originalFetch;
  _resetUsageStatsCache(); // 清掉用量请求级缓存，保证用例间隔离
});

/**
 * 通用 fetch mock：根据 url 返一份预设响应。
 * matches 可以包含部分 url 字符串（substring 匹配）。
 */
function mockFetchByUrl(matches: Record<string, unknown>) {
  // 直接覆写 globalThis.fetch（避免 spy 在 ESM + node 内置 fetch 下捕获不到的边界）
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === "string" ? input : input.toString();
    for (const [key, body] of Object.entries(matches)) {
      if (url.includes(key)) {
        return new Response(JSON.stringify(body), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
    }
    return new Response("{}", { status: 404 });
  }) as unknown as typeof fetch;
}

/** 单接口 events list 形状的 fetch mock(realListActivity 用). */
function mockFetch(events: unknown[]) {
  globalThis.fetch = vi.fn(async () =>
    new Response(
      JSON.stringify({
        code: 0,
        message: "ok",
        data: { events },
      }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    ),
  ) as unknown as typeof fetch;
}

describe("realListActivity — /api/events 契约", () => {
  it("BackendMeaningfulEvent → ActivityEvent 字段映射", async () => {
    mockFetch([
      {
        event_id: "e1",
        timestamp: 1780374052720,
        text: "[感知引擎]规则提醒：\n1. 来自：客厅。触发条件：用户是否久坐超过1小时。触发原因：x。",
        has_rule_hit: true,
        has_suggestion: false,
        has_asr: false,
        snapshot_count: 3,
        device_ids: ["cam_living_01"],
        rule_names: { r1: "[sitting_reminder] 坐姿监测" },
        clip_kind: "frames",
      },
    ]);

    const events = await realListActivity();
    expect(events).toHaveLength(1);
    expect(events[0]).toMatchObject({
      id: "e1",
      timestamp: 1780374052720,
      snapshot_count: 3,
      device_ids: ["cam_living_01"],
      has_rule_hit: true,
      rule_names: { r1: "[sitting_reminder] 坐姿监测" },
      clip_kind: "frames",
    });
  });

  it("空 events 数组返空列表", async () => {
    mockFetch([]);
    const events = await realListActivity();
    expect(events).toEqual([]);
  });

  it("query 参数透传(since/before/limit/offset)", async () => {
    const calls: string[] = [];
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      calls.push(typeof input === "string" ? input : input.toString());
      return new Response(
        JSON.stringify({ code: 0, message: "ok", data: { events: [] } }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    }) as unknown as typeof fetch;

    await realListActivity({
      since: 1780000000000,
      before: 1780999999999,
      limit: 100,
      offset: 50,
    });
    expect(calls[0]).toContain("since=1780000000000");
    expect(calls[0]).toContain("before=1780999999999");
    expect(calls[0]).toContain("limit=100");
    expect(calls[0]).toContain("offset=50");
  });
});

describe("RTSP 摄像头配置契约", () => {
  it("PUT 更新 RTSP 摄像头：发送 name/url 并返回更新后的 ScopeCamera", async () => {
    const cap: { method?: string; body: unknown; url?: string } = { body: null };
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      cap.url = typeof input === "string" ? input : input.toString();
      cap.method = init?.method;
      cap.body = init?.body ? JSON.parse(init.body as string) : null;
      return new Response(
        JSON.stringify({
          code: 0,
          message: "ok",
          data: {
            did: "rtsp:abc",
            name: "后门",
            source: "rtsp",
            url: "rtsp://127.0.0.1:8554/new",
            room_name: "RTSP",
            is_online: true,
            in_use: true,
            connected: false,
          },
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    }) as unknown as typeof fetch;

    const camera = await realUpdateRtspCamera("rtsp:abc", {
      name: "后门",
      url: "rtsp://127.0.0.1:8554/new",
    });

    expect(cap.url).toContain("/api/miot/rtsp_cameras/rtsp%3Aabc");
    expect(cap.method).toBe("PUT");
    expect(cap.body).toEqual({
      name: "后门",
      url: "rtsp://127.0.0.1:8554/new",
    });
    expect(camera).toMatchObject({
      did: "rtsp:abc",
      name: "后门",
      source: "rtsp",
      url: "rtsp://127.0.0.1:8554/new",
      roomName: "RTSP",
      isOnline: true,
      inUse: true,
      connected: false,
    });
  });
});

describe("realGetUsageStats — today buckets 折算契约", () => {
  // 今天 00:00 的 ms 时间戳（桶 0，不依赖运行时刻，必在窗口内）
  const t0 = (() => {
    const d = new Date();
    d.setHours(0, 0, 0, 0);
    return d.getTime();
  })();

  // 服务端桶行（都落在桶 0 = t0），结构对齐 /token-usage/buckets 返回
  function bkt(type: string, calls: number, inp: number, out: number, cache: number, video: number, audio: number) {
    return {
      bucket_ms: t0,
      model: "mimo-v2.5",
      type,
      calls,
      input_tokens: inp,
      output_tokens: out,
      cache_tokens: cache,
      video_tokens: video,
      audio_tokens: audio,
    };
  }

  it("today：聚合 totals / by_type / rows / timeline 求和", async () => {
    mockFetchByUrl({
      "/api/admin/token-usage/buckets": {
        code: 0,
        message: "ok",
        data: {
          rows: [
            bkt("realtime", 2, 3000, 300, 800, 2200, 200),
            bkt("on_demand", 1, 3000, 500, 1000, 0, 1500),
          ],
          total: 2,
        },
      },
    });

    const s = await realGetUsageStats("today");

    expect(s.period).toBe("today");
    expect(s.calls).toBe(3);
    // 总量 = Σ(input + output)
    expect(s.total_tokens).toBe(6000 + 800);
    expect(s.totals).toEqual({ input: 6000, output: 800, cache: 1800, video: 2200, audio: 1700 });

    // by_type 按 tokens 降序：on_demand(3500) > realtime(3300)
    expect(s.by_type.map((g) => g.key)).toEqual(["on_demand", "realtime"]);
    expect(s.by_type[0].tokens).toBe(3500);
    expect(s.by_type[1].tokens).toBe(3300);
    expect(s.by_type[1].calls).toBe(2);

    // rows：model × type
    const rt = s.rows.find((r) => r.type === "realtime")!;
    expect(rt.model).toBe("mimo-v2.5");
    expect(rt.breakdown.video).toBe(2200);
    expect(rt.calls).toBe(2);

    // timeline：桶都落在 t0（桶 0）；至少 1 桶，桶求和 = 总量
    expect(s.timeline.length).toBeGreaterThanOrEqual(1);
    expect(s.timeline[0].tokens).toBe(6800);
    expect(s.timeline.reduce((a, b) => a + b.tokens, 0)).toBe(6800);
  });

  it("today：切换 bin 不改 totals，只影响 timeline 桶数", async () => {
    mockFetchByUrl({
      "/api/admin/token-usage/buckets": {
        code: 0,
        message: "ok",
        data: {
          rows: [bkt("realtime", 2, 3000, 300, 0, 1500, 0)],
          total: 1,
        },
      },
    });
    const hourly = await realGetUsageStats("today", 60);
    const fine = await realGetUsageStats("today", 10); // 不同 bin → 不同缓存 key
    expect(hourly.total_tokens).toBe(3300);
    expect(fine.total_tokens).toBe(hourly.total_tokens);
    // 更细的 bin → 桶数不少于整点桶
    expect(fine.timeline.length).toBeGreaterThanOrEqual(hourly.timeline.length);

    // 明细行：只有 realtime 数据，也要给该模型补一行 on_demand 0
    const od = hourly.rows.find((r) => r.model === "mimo-v2.5" && r.type === "on_demand");
    expect(od).toBeDefined();
    expect(od!.calls).toBe(0);
    expect(od!.tokens).toBe(0);
    const rt = hourly.rows.find((r) => r.model === "mimo-v2.5" && r.type === "realtime");
    expect(rt!.calls).toBe(2);
    // 不得重复：单模型只 2 行（realtime + on_demand），realtime 恰 1 行
    expect(hourly.rows).toHaveLength(2);
    expect(hourly.rows.filter((r) => r.type === "realtime")).toHaveLength(1);
    const keys = hourly.rows.map((r) => `${r.model}|${r.type}`);
    expect(new Set(keys).size).toBe(keys.length);
  });

  it("today：无桶时全 0、by_type 恒两项、rows 空、timeline 至少 1 桶", async () => {
    mockFetchByUrl({
      "/api/admin/token-usage/buckets": {
        code: 0,
        message: "ok",
        data: { rows: [], total: 0 },
      },
    });
    const s = await realGetUsageStats("today");
    expect(s.total_tokens).toBe(0);
    expect(s.calls).toBe(0);
    // by_type 恒含 realtime + on_demand 两项（无数据则 0）
    expect(s.by_type).toHaveLength(2);
    expect(s.by_type.every((g) => g.tokens === 0 && g.calls === 0)).toBe(true);
    expect([...s.by_type.map((g) => g.key)].sort()).toEqual(["on_demand", "realtime"]);
    expect(s.rows).toEqual([]);
    expect(s.timeline.length).toBeGreaterThanOrEqual(1);
  });
});

describe("realGetUsageStats — week daily 折算契约", () => {
  const todayStr = () => {
    const d = new Date();
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(
      d.getDate(),
    ).padStart(2, "0")}`;
  };

  it("week：按天聚合，timeline 7 点，今天点映射到当日合计", async () => {
    const date = todayStr();
    mockFetchByUrl({
      "/api/admin/token-usage/daily": {
        code: 0,
        message: "ok",
        data: {
          rows: [
            { date, model: "mimo-v2.5", type: "realtime", calls: 100, input_tokens: 5000, output_tokens: 500, cache_tokens: 1500, video_tokens: 4000, audio_tokens: 200 },
            { date, model: "mimo-v2.5", type: "on_demand", calls: 10, input_tokens: 2000, output_tokens: 800, cache_tokens: 800, video_tokens: 0, audio_tokens: 1200 },
          ],
          total: 2,
        },
      },
    });

    const s = await realGetUsageStats("week");
    expect(s.period).toBe("week");
    expect(s.calls).toBe(110);
    expect(s.total_tokens).toBe(5500 + 2800);
    expect(s.timeline).toHaveLength(7);
    // 最后一个点 = 今天 = 当日两行合计 tokens
    expect(s.timeline[6].tokens).toBe(8300);
    // 早于窗口的天补 0
    expect(s.timeline[0].tokens).toBe(0);
  });
});

describe("omni 配置契约 — 多档案", () => {
  const STATE = {
    code: 0,
    message: "ok",
    data: {
      active: {
        label: "配置1",
        model: "m1",
        base_url: "https://p/v1",
        api_key_masked: "sk-…cdef",
        has_key: true,
        enabled: true,
        capabilities: ["text", "image", "video", "audio"],
      },
      profiles: [
        { label: "配置1", model: "m1", base_url: "https://p/v1", api_key_masked: "sk-…cdef", has_key: true, enabled: true, capabilities: ["text", "image", "video", "audio"], active: true },
        { label: "配置2", model: "m2", base_url: "https://p/v1", api_key_masked: "sk-…cdef", has_key: true, enabled: false, capabilities: ["audio"], active: false },
      ],
    },
  };

  // 捕获请求(method+body)并返回 STATE
  function captureFetch() {
    const cap: { method?: string; body: unknown } = { body: null };
    globalThis.fetch = vi.fn(async (_i: RequestInfo | URL, init?: RequestInit) => {
      cap.method = init?.method;
      cap.body = init?.body ? JSON.parse(init.body as string) : null;
      return new Response(JSON.stringify(STATE), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }) as unknown as typeof fetch;
    return cap;
  }

  it("GET：解出 active + profiles", async () => {
    mockFetchByUrl({ "/api/admin/omni-config": STATE });
    const s = await realGetOmniConfig();
    expect(s.active.model).toBe("m1");
    expect(s.active.has_key).toBe(true);
    expect(s.profiles).toHaveLength(2);
    expect(s.profiles[0].active).toBe(true);
    expect(s.profiles[1].model).toBe("m2");
    expect(s.profiles[1].enabled).toBe(false);
    expect(s.profiles[1].capabilities).toEqual(["audio"]);
  });

  it("PUT 保存：含 label、带 api_key、method=PUT、返回 state", async () => {
    const cap = captureFetch();
    const s = await realUpdateOmniConfig({
      label: "配置1",
      model: "m1",
      base_url: "https://p/v1",
      api_key: "sk-abcdef",
      enabled: true,
      capabilities: ["image", "audio"],
    });
    expect(cap.method).toBe("PUT");
    expect(cap.body).toEqual({
      label: "配置1",
      model: "m1",
      base_url: "https://p/v1",
      api_key: "sk-abcdef",
      enabled: true,
      capabilities: ["image", "audio"],
    });
    expect(s.active.model).toBe("m1");
  });

  it("PUT 保存：未填 api_key → 不含该字段;带 original_label 表示改名", async () => {
    const cap = captureFetch();
    await realUpdateOmniConfig({
      label: "新名",
      model: "m2",
      base_url: "https://p/v1",
      original_label: "配置2",
    });
    expect("api_key" in (cap.body as object)).toBe(false);
    expect(cap.body).toEqual({
      label: "新名",
      model: "m2",
      base_url: "https://p/v1",
      original_label: "配置2",
    });
  });

  it("PUT 路由更新：可以发送 enabled=false 和能力子集", async () => {
    const cap = captureFetch();
    await realUpdateOmniConfig({
      label: "配置2",
      model: "m2",
      base_url: "https://p/v1",
      original_label: "配置2",
      activate: false,
      enabled: false,
      capabilities: ["audio"],
    });
    expect(cap.body).toEqual({
      label: "配置2",
      model: "m2",
      base_url: "https://p/v1",
      original_label: "配置2",
      activate: false,
      enabled: false,
      capabilities: ["audio"],
    });
  });

  it("activate：POST {label}", async () => {
    const cap = captureFetch();
    const s = await realActivateOmniConfig({ label: "配置1" });
    expect(cap.method).toBe("POST");
    expect(cap.body).toEqual({ label: "配置1" });
    expect(s.active.model).toBe("m1");
  });

  it("delete：POST {label}", async () => {
    const cap = captureFetch();
    await realDeleteOmniConfig({ label: "配置2" });
    expect(cap.method).toBe("POST");
    expect(cap.body).toEqual({ label: "配置2" });
  });

  it("listModels：返回 {ok, models}", async () => {
    mockFetchByUrl({
      "/api/admin/omni-config/models": {
        code: 0,
        message: "ok",
        data: { ok: true, models: ["a", "b"] },
      },
    });
    const res = await realListOmniModels({ base_url: "https://p/v1", api_key: "sk-x" });
    expect(res.ok).toBe(true);
    expect(res.models).toEqual(["a", "b"]);
  });

  it("测试连接：解析 {ok,status,latency_ms,message}", async () => {
    mockFetchByUrl({
      "/api/admin/omni-config/test": {
        code: 0,
        message: "ok",
        data: { ok: true, status: 200, latency_ms: 188, message: "连接正常" },
      },
    });
    const res = await realTestOmniConfig({
      label: "配置1",
      model: "m1",
      base_url: "https://p/v1",
      api_key: "sk-x",
    });
    expect(res.ok).toBe(true);
    expect(res.status).toBe(200);
    expect(res.message).toBe("连接正常");
  });
});
