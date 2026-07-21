/**
 * 动作流数据层 + 单流合并测试。
 *
 * node 环境无 jsdom,沿用 real.test.ts 的做法:覆写 globalThis.fetch,直接测
 * 导出的逻辑函数,不渲 DOM。
 *
 * 覆盖:
 * - fetchActions 解析 backend BARE 数组 + query 参数(limit / failed_only)
 * - actionTypeKey 纯映射
 * - mergeFeedRows:事件 + 动作交错顺序、checkbox 筛选、窗口裁剪规则
 *
 * (动作行时间列已改用与事件行同一 TimeLabel/smartTimeParts 渲染——专属
 * formatActionTime 及其测试随之删除;smartTimeParts 由 relativeTime.test.ts 覆盖。)
 */

import { describe, it, expect, vi, afterEach } from "vitest";
import {
  fetchActions,
  actionTypeKey,
  type BackendActionRow,
} from "@/components/ActionsFeed";
import { mergeFeedRows } from "@/components/ActivityFeed";
import type { ActivityEvent } from "@/lib/types";

const originalFetch = globalThis.fetch;

afterEach(() => {
  vi.restoreAllMocks();
  globalThis.fetch = originalFetch;
});

/** 捕获 fetch 收到的 url,并返一份 bare 数组响应(backend /api/actions 形状)。 */
function mockActions(rows: unknown[]): { url: () => string } {
  let captured = "";
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    captured = typeof input === "string" ? input : input.toString();
    return new Response(JSON.stringify(rows), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }) as unknown as typeof fetch;
  return { url: () => captured };
}

function row(extra: Partial<BackendActionRow> = {}): BackendActionRow {
  return {
    id: "a1",
    timestamp: 1780374052720,
    action_type: "set_property",
    did: "dev-1",
    device_name: "客厅灯",
    room: "客厅",
    iid: "2.1",
    value_json: "true",
    result_code: 0,
    result_msg: null,
    success: 1,
    error: null,
    trace_id: null,
    ...extra,
  };
}

function ev(id: string, ts: number, extra: Partial<ActivityEvent> = {}): ActivityEvent {
  return { id, timestamp: ts, text: "x", device_ids: [], snapshot_count: 0, ...extra };
}

describe("fetchActions — /api/actions 契约", () => {
  it("解析 backend bare 数组为行", async () => {
    mockActions([
      row({ id: "a1", device_name: "客厅灯" }),
      row({ id: "a2", device_name: "卧室空调", action_type: "call_action", success: 0, error: "-704 限频" }),
    ]);
    const rows = await fetchActions(false);
    expect(rows).toHaveLength(2);
    expect(rows[0].id).toBe("a1");
    expect(rows[0].device_name).toBe("客厅灯");
    expect(rows[1].success).toBe(0);
    expect(rows[1].error).toBe("-704 限频");
  });

  it("空数组 → 空态(rows 长度 0)", async () => {
    mockActions([]);
    const rows = await fetchActions(false);
    expect(rows).toEqual([]);
  });

  it("默认不带 failed_only,单流一次拉全 limit=500", async () => {
    const m = mockActions([]);
    await fetchActions(false);
    expect(m.url()).toContain("limit=500");
    expect(m.url()).not.toContain("failed_only");
  });

  it("failedOnly=true → query 带 failed_only=1", async () => {
    const m = mockActions([]);
    await fetchActions(true);
    expect(m.url()).toContain("failed_only=1");
    expect(m.url()).toContain("limit=500");
  });

  it("传 sinceMs/untilMs → query 带 since_ms/until_ms(动作与事件同段约束)", async () => {
    const m = mockActions([]);
    await fetchActions(false, 1000, 2000);
    expect(m.url()).toContain("since_ms=1000");
    expect(m.url()).toContain("until_ms=2000");
  });

  it("传 homeId → query 带 home_id(v4:切家后动作流只显当前家)", async () => {
    const m = mockActions([]);
    await fetchActions(false, undefined, undefined, "H1");
    expect(m.url()).toContain("home_id=H1");
  });

  it("不传 homeId → query 不带 home_id(scope 未加载时不过滤)", async () => {
    const m = mockActions([]);
    await fetchActions(false);
    expect(m.url()).not.toContain("home_id");
  });
});

describe("actionTypeKey", () => {
  it("set_property / set_properties 归设置属性", () => {
    expect(actionTypeKey("set_property")).toBe("actions.typeSetProperty");
    expect(actionTypeKey("set_properties")).toBe("actions.typeSetProperty");
  });
  it("call_action / scene_trigger 各自映射", () => {
    expect(actionTypeKey("call_action")).toBe("actions.typeCallAction");
    expect(actionTypeKey("scene_trigger")).toBe("actions.typeSceneTrigger");
  });
  it("未知类型 → typeUnknown", () => {
    expect(actionTypeKey("weird")).toBe("actions.typeUnknown");
  });
});

describe("mergeFeedRows — 单流合并 / 交错 / 窗口", () => {
  const events = [ev("e-new", 300), ev("e-mid", 200), ev("e-old", 100)];
  const actions = [
    row({ id: "act-newer", timestamp: 350 }), // 比最新事件更新
    row({ id: "act-inwin", timestamp: 250 }), // 落在事件窗内
    row({ id: "act-older", timestamp: 50 }), // 比最旧展示事件更早 → 属未翻到的分页窗
  ];

  it("两 flag 都开:按 ts DESC 交错,窗外(更早)动作被裁掉", () => {
    const r = mergeFeedRows(events, actions, true, true);
    // 350(act) 300(ev) 250(act) 200(ev) 100(ev);act-older(50)被裁
    expect(r.map((x) => (x.kind === "event" ? x.event.id : x.action.id))).toEqual([
      "act-newer",
      "e-new",
      "act-inwin",
      "e-mid",
      "e-old",
    ]);
  });

  it("显式时间窗:即使无事件,动作也按 since/before 卡界(修:事件空时动作曾无下界)", () => {
    const acts = [
      row({ id: "before-win", timestamp: 50 }),
      row({ id: "in-win", timestamp: 150 }),
      row({ id: "after-win", timestamp: 250 }),
    ];
    // 窗 [100, 200],无事件:只保留 in-win(150);before/after 都被卡掉
    const r = mergeFeedRows([], acts, true, true, 100, 200);
    expect(
      r.map((x) => (x.kind === "event" ? x.event.id : x.action.id)),
    ).toEqual(["in-win"]);
  });

  it("比最新展示事件更新的动作被保留在最上", () => {
    const r = mergeFeedRows(events, actions, true, true);
    expect(r[0].kind).toBe("action");
    expect(r[0].kind === "action" && r[0].action.id).toBe("act-newer");
  });

  it("仅事件(动作 flag 关):不含任何动作行", () => {
    const r = mergeFeedRows(events, actions, true, false);
    expect(r.every((x) => x.kind === "event")).toBe(true);
    expect(r.map((x) => x.ts)).toEqual([300, 200, 100]);
  });

  it("仅动作(事件 flag 关):动作不设窗口下界,全展示且不含事件", () => {
    const r = mergeFeedRows(events, actions, false, true);
    expect(r.every((x) => x.kind === "action")).toBe(true);
    // 无事件窗 → 连更早的 act-older 也保留,ts DESC
    expect(r.map((x) => x.ts)).toEqual([350, 250, 50]);
  });

  it("两 flag 都关 → 空(渲染层显 emptyFilter 提示)", () => {
    expect(mergeFeedRows(events, actions, false, false)).toEqual([]);
  });

  it("事件为空但显事件:动作不设下界(避免全裁),仍全展示", () => {
    const r = mergeFeedRows([], actions, true, true);
    expect(r.map((x) => x.ts)).toEqual([350, 250, 50]);
  });

  it("同 ts:事件排在动作前(因果:先有事件后有动作)", () => {
    const r = mergeFeedRows([ev("e", 200)], [row({ id: "a", timestamp: 200 })], true, true);
    expect(r[0].kind).toBe("event");
    expect(r[1].kind).toBe("action");
  });
});
