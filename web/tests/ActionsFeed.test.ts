/**
 * 动作流数据层 + 单流合并测试。
 *
 * node 环境无 jsdom,沿用 real.test.ts 的做法:覆写 globalThis.fetch,直接测
 * 导出的逻辑函数,不渲 DOM —— 故本文件全绿推不出组件行为正确,组件内的
 * state 转移接线不在覆盖范围内。
 *
 * 覆盖对象:后端取数契约、动作类型映射,以及合流 / 分页 / 地平线几组导出纯函数;
 * 逐条断言以各 describe 标题为准,不在此列举(列举会随改动腐烂)。
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
import {
  feedLowerBound,
  hasGapAbove,
  mergeFeedRows,
  nextEvents,
  nextHasMore,
  nextOffset,
} from "@/components/ActivityFeed";
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

/**
 * 回归:「勾上事件+动作后,稍旧的感知事件消失、下面全是动作条目」。
 *
 * 成因是两个下界写成了 `sinceMs ?? 事件地平线`,而默认视图的 since 恒为今天 00:00
 * ——`??` 永远短路,地平线那支从来没跑过。老测试全都不传 sinceMs,恰好只覆盖了
 * 生产环境永远不会走的那条分支,所以这个 bug 带着绿灯上线。
 *
 * 这一组的共同前提:**sinceMs 已定义**(和生产默认态一致)。
 */
describe("mergeFeedRows — 事件地平线(回归:事件断流)", () => {
  const DAY = 1_000_000; // 今天 00:00
  // 已加载的一页事件(生产里是 PAGE_SIZE=50 条中最旧的那批)
  const loaded = [ev("e-new", DAY + 900), ev("e-oldest-loaded", DAY + 500)];
  // 动作一次拉全,铺满整个 since 窗口——包含地平线以下那段
  const acts = [
    row({ id: "a-top", timestamp: DAY + 950 }),
    row({ id: "a-in", timestamp: DAY + 600 }),
    row({ id: "a-at-horizon", timestamp: DAY + 500 }),
    row({ id: "a-below-1", timestamp: DAY + 400 }),
    row({ id: "a-below-2", timestamp: DAY + 100 }),
  ];
  const ids = (rows: ReturnType<typeof mergeFeedRows>) =>
    rows.map((x) => (x.kind === "event" ? x.event.id : x.action.id));
  /** since/before 只约束动作:事件是后端按同一时间窗查回来的,前端不再二次过滤。 */
  const actIds = (rows: ReturnType<typeof mergeFeedRows>) =>
    rows.flatMap((x) => (x.kind === "action" ? [x.action.id] : []));

  it("sinceMs 已定义时地平线仍生效:地平线以下的动作被裁(核心回归)", () => {
    const r = mergeFeedRows(loaded, acts, true, true, DAY, undefined, true);
    expect(ids(r)).toEqual([
      "a-top",
      "e-new",
      "a-in",
      "e-oldest-loaded",
      "a-at-horizon", // 同 ts:事件在前、动作在后
    ]);
    // 老实现在这里会把 a-below-1 / a-below-2 也放出来,列表尾部成为"只有动作"的墙
    expect(ids(r)).not.toContain("a-below-1");
    expect(ids(r)).not.toContain("a-below-2");
  });

  it("hasMoreEvents=false(事件已全部加载)→ 不设地平线,动作铺到 sinceMs", () => {
    const r = mergeFeedRows(loaded, acts, true, true, DAY, undefined, false);
    expect(ids(r)).toContain("a-below-1");
    expect(ids(r)).toContain("a-below-2");
  });

  it("翻页把地平线推下去,原先被裁的动作显出来", () => {
    const nextPage = [...loaded, ev("e-page2", DAY + 200)];
    const r = mergeFeedRows(nextPage, acts, true, true, DAY, undefined, true);
    expect(ids(r)).toContain("a-below-1"); // 400 >= 新地平线 200
    expect(ids(r)).not.toContain("a-below-2"); // 100 < 200,仍在地平线下
  });

  it("sinceMs 比地平线更晚时由 sinceMs 说了算(取 max,不是取地平线)", () => {
    const r = mergeFeedRows(loaded, acts, true, true, DAY + 700, undefined, true);
    // 地平线是 DAY+500,sinceMs 是 DAY+700 → 下界 700,a-in(600) 也被卡掉
    expect(actIds(r)).toEqual(["a-top"]);
  });

  it("事件 checkbox 关掉 → 无地平线,动作在 since 窗内全展示", () => {
    const r = mergeFeedRows(loaded, acts, false, true, DAY, undefined, true);
    expect(ids(r)).toEqual(["a-top", "a-in", "a-at-horizon", "a-below-1", "a-below-2"]);
  });

  it("beforeMs 与地平线同时生效(上下界互不干扰)", () => {
    const r = mergeFeedRows(loaded, acts, true, true, DAY, DAY + 700, true);
    // 上界 700 卡掉 a-top(950),下界(地平线 500)卡掉 a-below-*
    expect(actIds(r)).toEqual(["a-in", "a-at-horizon"]);
  });

  it("事件不受 since/before 二次过滤(后端已按同一时间窗查回)", () => {
    const r = mergeFeedRows(loaded, acts, true, true, DAY + 700, DAY + 800, true);
    // 两条事件都在窗外,但仍原样保留——窗只管动作
    expect(ids(r).filter((id) => id.startsWith("e-"))).toEqual([
      "e-new",
      "e-oldest-loaded",
    ]);
  });
});

describe("feedLowerBound — 两个下界取 max", () => {
  const evs = [ev("a", 500), ev("b", 900)];

  it("sinceMs 与地平线取较晚的那个", () => {
    expect(feedLowerBound(evs, true, 100, true)).toBe(500); // 地平线赢
    expect(feedLowerBound(evs, true, 800, true)).toBe(800); // sinceMs 赢
  });

  it("sinceMs 未定义时退回纯地平线", () => {
    expect(feedLowerBound(evs, true, undefined, true)).toBe(500);
  });

  it("hasMoreEvents=false / 不显事件 / 无事件 → 无地平线", () => {
    expect(feedLowerBound(evs, true, undefined, false)).toBe(-Infinity);
    expect(feedLowerBound(evs, false, undefined, true)).toBe(-Infinity);
    expect(feedLowerBound([], true, undefined, true)).toBe(-Infinity);
  });

  it("无事件但有 sinceMs → sinceMs 仍是硬界", () => {
    expect(feedLowerBound([], true, 300, true)).toBe(300);
  });

  it("默认 hasMoreEvents=true 是保守侧(不知道有没有更多时宁可裁)", () => {
    expect(feedLowerBound(evs, true, 100)).toBe(500);
  });
});

/**
 * hasMore 现在身兼两职:既控「查看更早」按钮,又是 feedLowerBound 的地平线闸。
 * 写错一次就会把地平线关掉、退回那堵动作墙,所以三种模式的规则单独钉住。
 * PAGE_SIZE = 50。
 */
describe("nextHasMore — 取数后的分页标记", () => {
  it("replace / append:满页=还有更早,短页=到底", () => {
    expect(nextHasMore("replace", false, 50)).toBe(true);
    expect(nextHasMore("replace", true, 12)).toBe(false);
    expect(nextHasMore("append", true, 50)).toBe(true);
    expect(nextHasMore("append", true, 3)).toBe(false);
  });

  it("refresh 满页 + 第 0 页与手里数据接不上(中间有空洞)→ 抬成 true", () => {
    // 回归:断线期间新增 >PAGE_SIZE 条事件,重连后第 0 页满页且与旧数据接不上 ——
    // 列表中间空出一段。此时不能标成"已到底":那是句关于完整性的断言,而它不成立。
    expect(nextHasMore("refresh", false, 50, true)).toBe(true);
  });

  it("refresh 满页但接得上 → 不得推翻短页给出的'已到底'", () => {
    // 回归:窗口已全部加载(hasMore=false),一次与数据无关的重连拿回满页第 0 页,
    // 且第 0 页与手里数据重叠、没有空洞。若把 hasMore 抬成 true,地平线会重新压上,
    // 窗内更早的动作被裁掉,提示语还说"更早的事件与动作尚未加载" —— 事件其实一条不缺。
    expect(nextHasMore("refresh", false, 50, false)).toBe(false);
  });

  it("refresh 短页不得把 true 打成 false(第 0 页答不了'下面还有没有')", () => {
    // 回归:用户翻到第 4 页、hasMore=true,一次重连若把它打成 false,
    // 按钮消失 + 地平线失效 → 动作墙回归。
    expect(nextHasMore("refresh", true, 12)).toBe(true);
    expect(nextHasMore("refresh", true, 0)).toBe(true);
  });

  it("refresh 在已到底的列表上拿到短页仍维持到底", () => {
    expect(nextHasMore("refresh", false, 12)).toBe(false);
  });
});

/**
 * hasGapAbove 是 refresh 抬升 hasMore 的唯一理由:第 0 页与手里最新的事件之间
 * 有没有空出一段。判松了 = 与数据无关的重连也会压上地平线(上面那条回归);
 * 判紧了 = 真有空洞却标成"已到底"。时间戳 DESC,故比较的是
 * "fresh 里最旧的" 与 "prev 里最新的"。
 */
describe("hasGapAbove — 重连第 0 页与手里数据接不接得上", () => {
  it("第 0 页整体比手里最新那条还新 → 有空洞", () => {
    // 手里最新 500,回来的第 0 页是 900 / 1000:800 前后那段没拿到。
    expect(hasGapAbove([ev("have", 500)], [ev("f-old", 900), ev("f-new", 1000)])).toBe(true);
  });

  it("第 0 页与手里数据有重叠 → 接得上,不算空洞", () => {
    // 回来的第 0 页含 500(与手里那条同一时间戳的另一条),说明这段没漏。
    expect(hasGapAbove([ev("have", 500)], [ev("f-old", 500), ev("f-new", 1000)])).toBe(false);
    expect(hasGapAbove([ev("have", 500)], [ev("f-old", 400), ev("f-new", 1000)])).toBe(false);
  });

  it("边界:第 0 页最旧一条恰好等于手里最新一条 → 接得上", () => {
    expect(hasGapAbove([ev("have", 900)], [ev("f", 900)])).toBe(false);
  });

  it("空页无空洞;手里为空当有空洞(保守:底下还有没有仍是未知)", () => {
    expect(hasGapAbove([ev("have", 900)], [])).toBe(false);
    expect(hasGapAbove([], [ev("f", 900)])).toBe(true);
  });
});

describe("nextOffset — 只进不退", () => {
  it("append 正常前进", () => {
    expect(nextOffset(50, 50, 50)).toBe(100);
  });

  it("refresh 拉第 0 页不得让已翻到的深度回退", () => {
    // 回归:翻到 200 条后重连,pageOffset+len = 50,直接写会让下次翻页重拉已有页。
    expect(nextOffset(200, 0, 50)).toBe(200);
  });

  it("首次 append 从 0 起算", () => {
    expect(nextOffset(0, 0, 37)).toBe(37);
  });
});

/**
 * 三种取数模式对事件列表的更新规则。merge 本身的不变量(dedup / 排序)由
 * ActivityFeed-merge.test.ts 守,这里只钉"哪种模式走哪条分支"——replace 与 merge
 * 写反正是本 PR 要修的那个 bug。
 *
 * 边界:覆盖的是分派语义。组件内 reload → mode="refresh" 的接线测不到
 * (测试环境是 node,无 DOM,渲染不了组件),那部分仍靠 code review。
 */
describe("nextEvents — replace 替换 / append・refresh 合并", () => {
  const prev = [ev("old", 100), ev("mid", 200)];
  const fresh = [ev("new", 300)];

  it("replace 硬替换:筛选段 / 切家变化,旧列表整体作废", () => {
    expect(nextEvents("replace", prev, fresh).map((e) => e.id)).toEqual(["new"]);
  });

  it("refresh 合并不替换:重连不丢已翻页事件(核心回归)", () => {
    expect(nextEvents("refresh", prev, fresh).map((e) => e.id)).toEqual([
      "new",
      "mid",
      "old",
    ]);
  });

  it("append 合并不替换:翻页接在已加载列表上,按 ts 重排", () => {
    expect(nextEvents("append", prev, fresh).map((e) => e.id)).toEqual([
      "new",
      "mid",
      "old",
    ]);
  });

  it("同 id 重叠时后到的赢(翻页窗口与已在列表的重叠)", () => {
    const overlap = nextEvents("append", [ev("dup", 100, { text: "旧" })], [
      ev("dup", 100, { text: "新" }),
    ]);
    expect(overlap).toHaveLength(1);
    expect(overlap[0].text).toBe("新");
  });
});
