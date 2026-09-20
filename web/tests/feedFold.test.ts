/**
 * 折叠视图的装配规则:分组、块级窗口、两档生成哪些行、计数、退出延迟、强度档位的读回。
 *
 * node 环境无 jsdom,故这里全是纯函数 —— 也正是把这些判断从组件里搬出来的理由(见
 * lib/feedFold 头)。**边界**:滚到哪、焦点交给谁、点开以后箭头的朝向、徽标的三态
 * (未展开 / 已展开且看得见 / 已展开但滚走了)都发生在 DOM 里,本文件一条也守不住;
 * 全绿推不出组件接线正确。
 *
 * 时间窗与并列次序的回归用例在 ActionsFeed.test.ts(与事件流的地平线同处一组,
 * 那两条规则要和事件侧一起读才对得上)。
 */

import { describe, it, expect, beforeEach, afterEach } from "vitest";
import {
  buildFoldRows,
  foldCounts,
  formatLatency,
  latencyMs,
  orphanChipOf,
  type FoldActionLike,
  type FoldBranch,
  type FoldRow,
  type FoldStrength,
} from "@/lib/feedFold";
import {
  FOLD_STRENGTH_DEFAULT,
  FOLD_STRENGTH_KEY,
  readStoredFoldStrength,
} from "@/hooks/useFoldStrength";
import type { ActivityEvent } from "@/lib/types";

const DAY = 1_000_000;

function ev(id: string, ts: number, extra: Partial<ActivityEvent> = {}): ActivityEvent {
  return { id, timestamp: ts, text: "x", device_ids: [], snapshot_count: 0, ...extra };
}

function act(extra: Partial<FoldActionLike> = {}): FoldActionLike {
  return {
    id: "a1",
    timestamp: DAY,
    did: "dev-1",
    device_name: "客厅灯",
    room: "客厅",
    action_type: "set_property",
    iid: "2.1",
    value_json: "true",
    success: 1,
    result_msg: null,
    error: null,
    ...extra,
  };
}

/** 装配的简写入口:只写这次用例关心的那几个参数。 */
function fold(
  events: ActivityEvent[],
  actions: FoldActionLike[],
  extra: {
    strength?: FoldStrength;
    showEvents?: boolean;
    showActions?: boolean;
    sinceMs?: number;
    beforeMs?: number;
    hasMoreEvents?: boolean;
  } = {},
): FoldRow[] {
  return buildFoldRows({
    events,
    actions,
    showEvents: extra.showEvents ?? true,
    showActions: extra.showActions ?? true,
    strength: extra.strength ?? "strong",
    sinceMs: extra.sinceMs,
    beforeMs: extra.beforeMs,
    hasMoreEvents: extra.hasMoreEvents,
  });
}

const labels = (rows: FoldRow[]) =>
  rows.map((r) => (r.kind === "event" ? r.event.id : r.kind === "action" ? r.action.id : r.key));

const branchesOf = (rows: FoldRow[], eventId: string): FoldBranch[] => {
  const row = rows.find((r) => r.kind === "event" && r.key === eventId);
  return row?.kind === "event" ? row.branches : [];
};

describe("buildFoldRows — 按宿主 + 相位收成支", () => {
  const host = ev("e1", DAY + 100);
  const enter = act({ id: "a-enter", timestamp: DAY + 110, trigger_event_id: "e1", phase: "enter" });
  const exit = act({ id: "a-exit", timestamp: DAY + 200, trigger_event_id: "e1", phase: "exit" });

  it("同一宿主同一相位归一支,支的时刻取最后一个成员(弱档据此落位)", () => {
    const rows = fold([host], [enter, exit], { strength: "weak" });
    const keys = labels(rows).filter((k) => k.includes("::"));
    expect(keys).toEqual(["e1::exit", "e1::enter"]); // 各自锚在自己最后一个成员的时刻上
    const enterRow = rows.find((r) => r.kind === "branch" && r.branch.phase === "enter");
    expect(enterRow?.ts).toBe(DAY + 110);
    const exitRow = rows.find((r) => r.kind === "branch" && r.branch.phase === "exit");
    expect(exitRow?.ts).toBe(DAY + 200);
  });

  it("同宿主不同相位是两支——退出支不是触发支的一部分", () => {
    const rows = fold([host], [enter, exit]);
    expect(branchesOf(rows, "e1").map((b) => b.phase)).toEqual(["enter", "exit"]);
  });

  it("相位缺失(有宿主但读不出相位)自成一枚空相位键,不冒充触发动作", () => {
    const rows = fold([host], [act({ id: "a-x", trigger_event_id: "e1" })], { strength: "weak" });
    expect(labels(rows)).toContain("e1::");
  });

  it("支内只要有失败成员,整支转红", () => {
    const rows = fold([host], [enter, act({ id: "a-bad", trigger_event_id: "e1", phase: "enter", success: 0 })]);
    expect(branchesOf(rows, "e1")[0].failed).toBe(true);
    // 全成功的支不红——判据是"有没有失败",不是"有没有动作"
    const clean = fold([host], [enter]);
    expect(branchesOf(clean, "e1")[0].failed).toBe(false);
  });

  it("动作的 checkbox 关掉 → 一条支都不收(数据不进来,不是画法变了)", () => {
    const rows = fold([host], [enter], { showActions: false });
    expect(labels(rows)).toEqual(["e1"]);
  });
});

describe("buildFoldRows — 挂不上宿主的三类", () => {
  it("触发事件未加载:有宿主 id 但事件不在手上 → hostMissing", () => {
    const rows = fold([], [act({ trigger_event_id: "e-gone", phase: "enter" })], {
      strength: "weak",
    });
    const b = rows.find((r) => r.kind === "branch");
    expect(b?.kind === "branch" && b.branch.chip).toBe("hostMissing");
  });

  it("宿主在手上 → 不打 chip(能说清楚的事不需要一句解释)", () => {
    const rows = fold([ev("e1", DAY)], [act({ trigger_event_id: "e1", phase: "enter" })]);
    expect(branchesOf(rows, "e1")[0].chip).toBeNull();
  });

  it("无触发源与早于链路记录分成两种 chip,不合并成一句", () => {
    expect(orphanChipOf(act({ trigger_event_id: null }))).toBe("noTrigger");
    expect(orphanChipOf(act({ trigger_event_id: null, phase: "legacy" }))).toBe("preLink");
    // 相位是别的值(不该出现的第三种)也当"没有触发源",不冒充历史行
    expect(orphanChipOf(act({ trigger_event_id: null, phase: "enter" }))).toBe("noTrigger");
  });

  it("单条无宿主动作按原样成行(不塞进只有一个成员的支),chip 跟到行上", () => {
    const rows = fold([], [act({ id: "a-lone" })]);
    expect(rows).toHaveLength(1);
    expect(rows[0].kind).toBe("action");
    expect(rows[0].kind === "action" && rows[0].chip).toBe("noTrigger");
  });
});

describe("buildFoldRows — 块级窗口", () => {
  // 宿主在窗外(早于 since),它的一条支落在窗内 —— 那条动作是真发生在用户选的段里的
  const host = ev("e-out", DAY + 100);
  const inWin = act({ id: "a-in", timestamp: DAY + 900, trigger_event_id: "e-out", phase: "exit" });
  const outWin = act({ id: "a-out", timestamp: DAY + 150, trigger_event_id: "e-out", phase: "enter" });

  it("宿主在窗外但支在窗内:宿主行留着并标 hostOutside(否则那条动作无家可归)", () => {
    const rows = fold([host], [inWin, outWin], { sinceMs: DAY + 500 });
    const row = rows.find((r) => r.kind === "event");
    expect(row?.kind === "event" && row.hostOutside).toBe(true);
    // 在窗内的那条支跟着宿主显示,窗外的另一条不显示
    expect(branchesOf(rows, "e-out").map((b) => b.phase)).toEqual(["exit"]);
  });

  it("宿主也在窗内 → 整块显示,不打 hostOutside", () => {
    const rows = fold([ev("e1", DAY + 800)], [act({ trigger_event_id: "e1", phase: "enter", timestamp: DAY + 850 })], {
      sinceMs: DAY + 500,
    });
    const row = rows.find((r) => r.kind === "event");
    expect(row?.kind === "event" && row.hostOutside).toBe(false);
  });

  it("宿主行在窗内、支整个在窗外 → 事件照画,支不画(事件行不受二次过滤)", () => {
    const rows = fold(
      [ev("e1", DAY + 800)],
      [act({ id: "a-early", timestamp: DAY + 100, trigger_event_id: "e1", phase: "enter" })],
      { sinceMs: DAY + 900 },
    );
    expect(labels(rows)).toEqual(["e1"]);
    expect(branchesOf(rows, "e1")).toEqual([]);
    // 它不是"因支而留"的,别打那枚 chip
    const row = rows[0];
    expect(row.kind === "event" && row.hostOutside).toBe(false);
  });

  it("宿主在窗内 → 整块显示:支的成员越过上界也跟着(块级命中,不逐条裁)", () => {
    const rows = fold(
      [ev("e1", DAY + 800)],
      [act({ id: "a-late", timestamp: DAY + 950, trigger_event_id: "e1", phase: "enter" })],
      { beforeMs: DAY + 820 },
    );
    expect(branchesOf(rows, "e1")).toHaveLength(1);
  });
});

describe("buildFoldRows — 两档各自生成哪些行", () => {
  const host = ev("e1", DAY + 100);
  const b = act({ id: "a1", timestamp: DAY + 200, trigger_event_id: "e1", phase: "exit" });

  it("强档:支并进事件行(0 行),徽标数据挂在事件行的 branches 上", () => {
    const rows = fold([host], [b], { strength: "strong" });
    expect(rows.map((r) => r.kind)).toEqual(["event"]);
    expect(branchesOf(rows, "e1")).toHaveLength(1);
  });

  it("弱档:支自己占一行,事件行的 branches 照样带着它(徽标与支行同时需要)", () => {
    const rows = fold([host], [b], { strength: "weak" });
    expect(labels(rows)).toEqual(["e1::exit", "e1"]);
    expect(branchesOf(rows, "e1")).toHaveLength(1);
  });

  it("强档但事件流被关掉:没有事件行可并,支退回成行(否则动作整个消失)", () => {
    const rows = fold([host], [b], { strength: "strong", showEvents: false });
    expect(rows.map((r) => r.kind)).toEqual(["branch"]);
    expect(rows[0].kind === "branch" && rows[0].hostRendered).toBe(false);
  });

  it("弱档且宿主行画得出来 → 支行的 ↩ 才有目标(hostRendered)", () => {
    const rows = fold([host], [b], { strength: "weak" });
    const br = rows.find((r) => r.kind === "branch");
    expect(br?.kind === "branch" && br.hostRendered).toBe(true);
    const noHost = fold([], [act({ trigger_event_id: "e-gone", phase: "enter" })], { strength: "weak" });
    expect(noHost[0].kind === "branch" && noHost[0].hostRendered).toBe(false);
  });

  it("两个 checkbox 都关 → 空", () => {
    expect(fold([host], [b], { showEvents: false, showActions: false })).toEqual([]);
  });
});

describe("buildFoldRows — 同一时刻的次序", () => {
  it("支与宿主同 ts:支排在事件之前(降序里更靠前,回返角标才恒为直落)", () => {
    const weak = fold(
      [ev("e1", DAY + 500)],
      [act({ id: "a1", timestamp: DAY + 500, trigger_event_id: "e1", phase: "enter" })],
      { strength: "weak" },
    );
    expect(labels(weak)).toEqual(["e1::enter", "e1"]);
  });

  it("单条无宿主动作与事件同 ts 时也排在事件前", () => {
    const rows = fold([ev("e1", DAY + 500)], [act({ id: "a1", timestamp: DAY + 500 })]);
    expect(labels(rows)).toEqual(["a1", "e1"]);
  });
});

describe("foldCounts — 数数据不数行", () => {
  const host = ev("e1", DAY + 100);
  const acts = [
    act({ id: "a1", timestamp: DAY + 110, trigger_event_id: "e1", phase: "enter" }),
    act({ id: "a2", timestamp: DAY + 120, trigger_event_id: "e1", phase: "enter" }),
    act({ id: "a3", timestamp: DAY + 200, trigger_event_id: "e1", phase: "exit" }),
    act({ id: "a4", timestamp: DAY + 300 }), // 无宿主,单条成行
  ];

  it("弱档:一支在事件行与自己的支行里各出现一次,只数一遍", () => {
    const rows = fold([host], acts, { strength: "weak" });
    expect(foldCounts(rows)).toEqual({ events: 1, actions: 4 });
  });

  it("强档:动作一条行都不占,报的仍是同一个数(切档位不该让计数变)", () => {
    const strong = foldCounts(fold([host], acts, { strength: "strong" }));
    const weak = foldCounts(fold([host], acts, { strength: "weak" }));
    expect(strong).toEqual({ events: 1, actions: 4 });
    expect(strong).toEqual(weak);
  });

  it("两个 checkbox 都关 → 全 0", () => {
    expect(foldCounts(fold([host], acts, { showEvents: false, showActions: false }))).toEqual({
      events: 0,
      actions: 0,
    });
  });
});

describe("latencyMs — 退出延迟只说该说的", () => {
  const host = ev("e1", DAY + 100);
  const branch = (phase: string, ts: number): FoldBranch => ({
    key: `e1::${phase}`,
    eventId: "e1",
    phase,
    ts,
    actions: [],
    failed: false,
    chip: null,
  });

  it("退出支:距宿主多久", () => {
    expect(latencyMs(branch("exit", DAY + 300), host)).toBe(200);
  });

  it("触发支没有可说的间隔(它就发生在那一刻)", () => {
    expect(latencyMs(branch("enter", DAY + 300), host)).toBeNull();
  });

  it("宿主不在手上 → 没有间隔可说,不拿 0 冒充", () => {
    expect(latencyMs(branch("exit", DAY + 300), undefined)).toBeNull();
  });

  it("时钟回拨导致支早于宿主 → 负数不渲染", () => {
    expect(latencyMs(branch("exit", DAY + 50), host)).toBeNull();
  });
});

describe("formatLatency — 单位走 i18n", () => {
  const t = (key: string, opts?: Record<string, unknown>) =>
    key === "actions.latencyDay"
      ? `${opts?.n}d`
      : key === "actions.latencyHour"
        ? `${opts?.n}h`
        : key === "actions.latencyMin"
          ? `${opts?.n}m`
          : `${opts?.n}s`;

  it("大单位在前,空的档位不占位置", () => {
    expect(formatLatency(41 * 60_000 + 25_000, t)).toBe("41m 25s");
    expect(formatLatency(2 * 3600_000 + 30_000, t)).toBe("2h 30s");
    expect(formatLatency(3 * 86400_000 + 60_000, t)).toBe("3d 1m");
  });

  it("不足一秒也要有个数(不能渲染成空字符串)", () => {
    expect(formatLatency(200, t)).toBe("0s");
    expect(formatLatency(0, t)).toBe("0s");
  });

  it("小数秒四舍五入到整数秒", () => {
    expect(formatLatency(1_600, t)).toBe("2s");
  });
});

describe("readStoredFoldStrength — 认不出的档位当没存过", () => {
  const store = new Map<string, string>();
  beforeEach(() => {
    store.clear();
    (globalThis as { localStorage?: unknown }).localStorage = {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => void store.set(k, v),
      removeItem: (k: string) => void store.delete(k),
    };
  });
  afterEach(() => {
    delete (globalThis as { localStorage?: unknown }).localStorage;
  });

  it("两档各自原样读回", () => {
    store.set(FOLD_STRENGTH_KEY, "weak");
    expect(readStoredFoldStrength()).toBe("weak");
    store.set(FOLD_STRENGTH_KEY, "strong");
    expect(readStoredFoldStrength()).toBe("strong");
  });

  it("没存过 → 默认", () => {
    expect(readStoredFoldStrength()).toBe(FOLD_STRENGTH_DEFAULT);
  });

  it("存了第三种值 → 退回默认,不让它流进渲染层(否则两处分支都不命中)", () => {
    for (const bad of ["", "mid", "STRONG", "true", "{}"]) {
      store.set(FOLD_STRENGTH_KEY, bad);
      expect(readStoredFoldStrength()).toBe(FOLD_STRENGTH_DEFAULT);
    }
  });

  it("存储整个不可用(隐私模式)→ 默认,不抛", () => {
    delete (globalThis as { localStorage?: unknown }).localStorage;
    expect(readStoredFoldStrength()).toBe(FOLD_STRENGTH_DEFAULT);
  });
});
