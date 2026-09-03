/**
 * 契约测试 — backend → 前端类型层的解析。
 *
 * 覆盖:
 * - realListActivity:GET /api/events(meaningful_events)字段映射 + 分页 query
 * - realGetUsageStats:GET /api/admin/token-usage/{buckets,daily} 折算
 * - realEventRefUrl / realEventCropMeta:Smart Crop 参考帧 URL + crop 坐标契约
 * - 任务详情写接口:PATCH /api/tasks/{id}(描述)、PATCH /api/rules/{id}(触发条件)
 *
 * 不连真 backend:vi 拦截 fetch,伪造 NormalResponse 形状;afterEach 还原原 fetch.
 */

import { describe, it, expect, vi, afterEach } from "vitest";
import i18n from "@/i18n";
import { ApiError } from "@/api/client";
import {
  realListActivity,
  realListDevices,
  realGetUsageStats,
  realGetOmniConfig,
  realUpdateOmniConfig,
  realActivateOmniConfig,
  realDeleteOmniConfig,
  realListOmniModels,
  realTestOmniConfig,
  realEventRefUrl,
  realEventCropMeta,
  realUpdateRule,
  realUpdateTaskDescription,
  _resetUsageStatsCache,
} from "@/api/real";

// 这些用例直接覆写 globalThis.fetch(非 vi.spyOn),vi.restoreAllMocks() 只还原
// spy 不还原直接赋值——不显式存/还原会让最后一个 mock 泄漏到后续测试文件(同进程
// 跑时拿到脏 fetch)。存原引用,afterEach 里还原。
const originalFetch = globalThis.fetch;

afterEach(() => {
  vi.restoreAllMocks();
  globalThis.fetch = originalFetch;
  i18n.changeLanguage("zh");
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

describe("realListDevices — device status contract", () => {
  it("maps lock warning state to statusKind independently from localized statusText", async () => {
    mockFetchByUrl({
      "/api/miot/home": {
        code: 0,
        message: "ok",
        data: {
          devices: [
            {
              did: "lock-1",
              name: "Front Door",
              online: true,
              model: "lock.model",
              room: "Entry",
              category: "lock",
              spec: {
                "prop.2.1": {
                  format: "uint8",
                  type_name: "door-state",
                  value_list: [
                    { name: "Locked", value: 1 },
                    { name: "Unlocked", value: 2 },
                  ],
                },
              },
            },
          ],
          areas: [],
          scenes: [],
          persons: [],
        },
      },
      "/api/miot/devices/lock-1/status": {
        code: 0,
        message: "ok",
        data: { properties: [{ iid: "prop.2.1", value: 2, code: 0 }] },
      },
    });

    await i18n.changeLanguage("en");
    const devices = await realListDevices();
    expect(devices[0]).toMatchObject({
      statusText: "Unlocked",
      statusKind: "unlocked",
    });
  });
});

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
        has_trace: true,
        has_ref: true,
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
      has_trace: true,
      has_ref: true,
    });
  });

  it("老 backend 不返 has_ref → undefined(前端据此不渲染参考卡,不是 false 也不报错)", async () => {
    mockFetch([
      {
        event_id: "e-old",
        timestamp: 1780374052720,
        text: "x",
        snapshot_count: 1,
        device_ids: ["cam_1"],
      },
    ]);
    const events = await realListActivity();
    expect(events[0].has_ref).toBeUndefined();
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

describe("Smart Crop 参考帧契约 — realEventRefUrl / realEventCropMeta", () => {
  // resolveToken 读 window.__MILOCO_TOKEN__(backend SPA handler 注入),用例间要还原,
  // 否则带 token 的用例会污染后面断言"裸路径"的用例。
  const originalToken = window.__MILOCO_TOKEN__;
  afterEach(() => {
    window.__MILOCO_TOKEN__ = originalToken;
  });

  it("无 token 时是裸路径(<img> 不能设 header,所以只能走 query)", () => {
    window.__MILOCO_TOKEN__ = undefined;
    expect(realEventRefUrl("e1", "cam_1")).toBe("/api/events/e1/ref/cam_1");
  });

  it("有 token 时拼 ?token=,且 event/device/token 都过 encodeURIComponent", () => {
    window.__MILOCO_TOKEN__ = "tok/en+1";
    expect(realEventRefUrl("e 1", "cam#1")).toBe(
      "/api/events/e%201/ref/cam%231?token=tok%2Fen%2B1",
    );
  });

  it("占位 token 未被注入时不当真 token 用(不拼 query)", () => {
    window.__MILOCO_TOKEN__ = "__MILOCO_INJECT_TOKEN_HERE__";
    expect(realEventRefUrl("e1", "cam_1")).toBe("/api/events/e1/ref/cam_1");
  });

  it("realEventCropMeta：解出 region / frame_size / crop 短边", async () => {
    mockFetchByUrl({
      "/api/events/e1/crop/cam_1": {
        code: 0,
        message: "ok",
        data: {
          region_xyxy: [430, 122, 590, 318],
          frame_size_wh: [640, 360],
          crop_short_edge: 360,
        },
      },
    });
    const crop = await realEventCropMeta("e1", "cam_1");
    expect(crop).not.toBeNull();
    expect(crop!.region_xyxy).toEqual([430, 122, 590, 318]);
    expect(crop!.frame_size_wh).toEqual([640, 360]);
    expect(crop!.crop_short_edge).toBe(360);
  });

  it("410(这台 device 本次没裁切 / trace 已清)→ resolve null,不当异常抛", async () => {
    // 410 是**预期结果**:调用方拿 null 就能判定「无 crop」并整卡不渲染,
    // 不必 import ApiError 去认状态码(组件只依赖 @/api 门面).
    globalThis.fetch = vi.fn(async () =>
      new Response(
        JSON.stringify({ code: 1, message: "crop meta not available" }),
        { status: 410, headers: { "Content-Type": "application/json" } },
      ),
    ) as unknown as typeof fetch;

    await expect(realEventCropMeta("e1", "cam_1")).resolves.toBeNull();
  });

  it("非 410 错误照旧 reject——「没问出来」不能和「确定没有」混成同一个 null", async () => {
    globalThis.fetch = vi.fn(async () =>
      new Response(JSON.stringify({ code: 1, message: "boom" }), {
        status: 500,
        headers: { "Content-Type": "application/json" },
      }),
    ) as unknown as typeof fetch;

    await expect(realEventCropMeta("e1", "cam_1")).rejects.toBeInstanceOf(ApiError);
  });

  it("500 crop meta unreadable(裁过但 trace 读坏)→ reject,不折成 null", async () => {
    // 后端刻意用 500 而非 410 表达这一档:ref.jpg 还在盘上,折成 null 会让
    // RefFrameCard 把整张参考卡藏掉,丢的信息远多于少画一个框。
    // 与上一条的区别只在 message —— 单独立一条是为了钉住这个后端契约:
    // 哪天有人把它改回 410 图省事,这里会红。
    globalThis.fetch = vi.fn(async () =>
      new Response(JSON.stringify({ code: 1, message: "crop meta unreadable" }), {
        status: 500,
        headers: { "Content-Type": "application/json" },
      }),
    ) as unknown as typeof fetch;

    await expect(realEventCropMeta("e1", "cam_1")).rejects.toBeInstanceOf(ApiError);
  });

  it("404(event/device 不存在)也 reject,不折成 null", async () => {
    globalThis.fetch = vi.fn(async () =>
      new Response(JSON.stringify({ code: 1, message: "not found" }), {
        status: 404,
        headers: { "Content-Type": "application/json" },
      }),
    ) as unknown as typeof fetch;

    await expect(realEventCropMeta("e1", "cam_1")).rejects.toBeInstanceOf(ApiError);
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
      },
      profiles: [
        { label: "配置1", model: "m1", base_url: "https://p/v1", api_key_masked: "sk-…cdef", has_key: true, active: true },
        { label: "配置2", model: "m2", base_url: "https://p/v1", api_key_masked: "sk-…cdef", has_key: true, active: false },
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
  });

  it("PUT 保存：含 label、带 api_key、method=PUT、返回 state", async () => {
    const cap = captureFetch();
    const s = await realUpdateOmniConfig({
      label: "配置1",
      model: "m1",
      base_url: "https://p/v1",
      api_key: "sk-abcdef",
    });
    expect(cap.method).toBe("PUT");
    expect(cap.body).toEqual({
      label: "配置1",
      model: "m1",
      base_url: "https://p/v1",
      api_key: "sk-abcdef",
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

describe("任务详情就地编辑 — 写接口契约", () => {
  /** 捕获 url / method / body，回一个空 NormalResponse。 */
  function captureFetch() {
    const cap: { url?: string; method?: string; body: unknown } = { body: null };
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      cap.url = typeof input === "string" ? input : input.toString();
      cap.method = init?.method;
      cap.body = init?.body ? JSON.parse(init.body as string) : null;
      return new Response(JSON.stringify({ code: 0, message: "ok", data: null }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }) as unknown as typeof fetch;
    return cap;
  }

  it("改任务描述：PATCH /api/tasks/{id} {description}", async () => {
    const cap = captureFetch();
    await realUpdateTaskDescription("piano_practice", "每天练琴 30 分钟");
    expect(cap.method).toBe("PATCH");
    expect(cap.url).toBe("/api/tasks/piano_practice");
    expect(cap.body).toEqual({ description: "每天练琴 30 分钟" });
  });

  it("改触发条件：PATCH /api/rules/{id} 只带 condition.query（不动感知设备）", async () => {
    const cap = captureFetch();
    await realUpdateRule("rule-1", { query: "孩子在书桌前学习" });
    expect(cap.method).toBe("PATCH");
    expect(cap.url).toBe("/api/rules/rule-1");
    // 只提交 condition.query：backend patch_rule 按 model_fields_set 合并，
    // 缺失的 perceive_device_ids 保留原值。多带字段会误改感知设备。
    expect(cap.body).toEqual({ condition: { query: "孩子在书桌前学习" } });
  });

  it("改规则名 / 意图文案：一次 PATCH 合并提交，未改的字段不带上", async () => {
    const cap = captureFetch();
    await realUpdateRule("rule-1", {
      name: "[piano_practice] 孩子坐在钢琴前",
      actionDescriptions: ["提醒住户", "记一次练琴"],
    });
    expect(cap.method).toBe("PATCH");
    // 只带这次真改过的字段：query / on_*_desc 缺席 → backend 保留原值。
    expect(cap.body).toEqual({
      name: "[piano_practice] 孩子坐在钢琴前",
      action_descriptions: ["提醒住户", "记一次练琴"],
    });
  });

  it("state 模式的进入 / 离开文案各自映射到 snake_case 字段", async () => {
    const cap = captureFetch();
    await realUpdateRule("rule-2", {
      onEnterDesc: "进入时提醒",
      onExitDesc: "离开时关灯",
      onTargetDesc: "满 30 分钟表扬一句",
    });
    expect(cap.body).toEqual({
      on_enter_desc: "进入时提醒",
      on_exit_desc: "离开时关灯",
      on_target_desc: "满 30 分钟表扬一句",
    });
  });

  it("空 patch → 空 body，不误清任何字段", async () => {
    const cap = captureFetch();
    await realUpdateRule("rule-3", {});
    expect(cap.body).toEqual({});
  });

  it("id 走 encodeURIComponent，特殊字符不越界成新路径", async () => {
    const cap = captureFetch();
    await realUpdateRule("a/b?c", { query: "x" });
    expect(cap.url).toBe("/api/rules/a%2Fb%3Fc");
  });

  it("backend 4xx → ApiError 带 message，调用方能 toast", async () => {
    globalThis.fetch = vi.fn(
      async () =>
        new Response(JSON.stringify({ message: "Rule 'nope' not found" }), {
          status: 404,
          headers: { "Content-Type": "application/json" },
        }),
    ) as unknown as typeof fetch;
    await expect(realUpdateRule("nope", { query: "x" })).rejects.toThrow(
      "Rule 'nope' not found",
    );
  });
});
