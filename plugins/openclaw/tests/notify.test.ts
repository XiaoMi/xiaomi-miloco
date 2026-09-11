import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const getRuntimeConfigMock = vi.fn();
const getPluginConfigMock = vi.fn();
const getNotifyDedupWindowMsMock = vi.fn(() => 60_000);

vi.mock("../src/config.js", () => ({
  getRuntimeConfig: (...args: unknown[]) => getRuntimeConfigMock(...args),
  getPluginConfig: (...args: unknown[]) => getPluginConfigMock(...args),
}));

vi.mock("../src/miloco/config.js", () => ({
  getNotifyDedupWindowMs: () => getNotifyDedupWindowMsMock(),
}));

import {
  __resetNotifyDedup,
  notifyOwner,
  resolveNotifyTarget,
  toTimestamp,
} from "../src/tools/notify.js";
import { logger } from "../src/utils/logger.js";

// 去重是模块级状态：每个用例前清空，且默认窗口 60s（个别用例可覆盖）。
beforeEach(() => {
  __resetNotifyDedup();
  getNotifyDedupWindowMsMock.mockReturnValue(60_000);
});

// logger 是模块级单例：用例里 init 过 spy 的必须拆掉，避免泄漏进后续用例。
afterEach(() => {
  logger.init(undefined as any);
});

type SubagentMock = {
  run: ReturnType<typeof vi.fn>;
  waitForRun: ReturnType<typeof vi.fn>;
};

function makeApi(
  store: Record<string, Record<string, unknown>>,
  subagent?: SubagentMock,
) {
  return {
    runtime: {
      agent: {
        session: {
          resolveStorePath: vi.fn(() => "/fake/store"),
          loadSessionStore: vi.fn(() => store),
        },
      },
      subagent,
    },
  } as any;
}

function makeSubagent(
  waitResult: Record<string, unknown> = { status: "ok" },
): SubagentMock {
  return {
    run: vi.fn(async () => ({ runId: "run-1" })),
    waitForRun: vi.fn(async () => waitResult),
  };
}

// openclaw >= 2026.8 的 runtime:agent.session 不再暴露 resolveStorePath/
// loadSessionStore，投递信息收进 entry.delivery.route。这里只给 listSessionEntries，
// 若实现还去碰已移除的旧方法会直接抛错 → 用例过 = 兼容路径生效。
function makeApiV2(
  entries: Array<{ sessionKey: string; entry: Record<string, unknown> }>,
  subagent?: SubagentMock,
) {
  return {
    runtime: {
      agent: {
        session: {
          listSessionEntries: vi.fn(() => entries),
        },
      },
      subagent,
    },
  } as any;
}

// ─── toTimestamp ─────────────────────────────────────────────────────────────

describe("toTimestamp", () => {
  it("number → passthrough", () => {
    expect(toTimestamp(1716700000000)).toBe(1716700000000);
  });

  it("valid ISO string → epoch ms", () => {
    const iso = "2026-05-14T10:00:00Z";
    expect(toTimestamp(iso)).toBe(Date.parse(iso));
  });

  it("invalid string → 0", () => {
    expect(toTimestamp("not-a-date")).toBe(0);
  });

  it("undefined → 0", () => {
    expect(toTimestamp(undefined)).toBe(0);
  });

  it("null → 0", () => {
    expect(toTimestamp(null)).toBe(0);
  });

  it("object → 0", () => {
    expect(toTimestamp({})).toBe(0);
  });
});

// ─── resolveNotifyTarget ────────────────────────────────────────────────────

describe("resolveNotifyTarget", () => {
  it("已配置 notifySessionKeys 且有效 → needsBind: false", () => {
    const store = {
      "wechat:abc": {
        lastChannel: "wechat",
        lastTo: "user123",
        lastAccountId: "acc1",
        lastThreadId: "t1",
      },
    };
    const api = makeApi(store);
    getRuntimeConfigMock.mockReturnValue({ session: {} });
    getPluginConfigMock.mockReturnValue({ notifySessionKeys: ["wechat:abc"] });

    const result = resolveNotifyTarget(api);
    expect(result.needsBind).toBe(false);
    expect(result.targets).toHaveLength(1);
    expect(result.target).toEqual({
      channel: "wechat",
      to: "user123",
      accountId: "acc1",
      threadId: "t1",
      sessionKey: "wechat:abc",
    });
    expect(result.bindReason).toBeUndefined();
  });

  it("已配置但 session 无 lastTo → fallback + bindReason: configured_but_invalid", () => {
    const store = {
      "wechat:abc": { lastChannel: "wechat" },
      "telegram:xyz": {
        lastChannel: "telegram",
        lastTo: "tg_user",
        lastInteractionAt: 1000,
      },
    };
    const api = makeApi(store);
    getRuntimeConfigMock.mockReturnValue({ session: {} });
    getPluginConfigMock.mockReturnValue({ notifySessionKeys: ["wechat:abc"] });

    const result = resolveNotifyTarget(api);
    expect(result.needsBind).toBe(true);
    expect(result.bindReason).toBe("configured_but_invalid");
    expect(result.target?.channel).toBe("telegram");
    expect(result.target?.sessionKey).toBe("telegram:xyz");
  });

  it("已配置但 session 不存在 → fallback + bindReason: configured_but_invalid", () => {
    const store = {
      "telegram:xyz": {
        lastChannel: "telegram",
        lastTo: "tg_user",
        lastInteractionAt: 2000,
      },
    };
    const api = makeApi(store);
    getRuntimeConfigMock.mockReturnValue({ session: {} });
    getPluginConfigMock.mockReturnValue({
      notifySessionKeys: ["wechat:nonexist"],
    });

    const result = resolveNotifyTarget(api);
    expect(result.needsBind).toBe(true);
    expect(result.bindReason).toBe("configured_but_invalid");
    expect(result.target?.sessionKey).toBe("telegram:xyz");
  });

  it("未配置 → fallback 到最近活跃 + bindReason: not_configured", () => {
    const store = {
      "wechat:old": {
        lastChannel: "wechat",
        lastTo: "user_old",
        lastInteractionAt: 1000,
      },
      "telegram:new": {
        lastChannel: "telegram",
        lastTo: "user_new",
        lastInteractionAt: 5000,
      },
    };
    const api = makeApi(store);
    getRuntimeConfigMock.mockReturnValue({ session: {} });
    getPluginConfigMock.mockReturnValue({});

    const result = resolveNotifyTarget(api);
    expect(result.needsBind).toBe(true);
    expect(result.bindReason).toBe("not_configured");
    expect(result.target?.channel).toBe("telegram");
    expect(result.target?.sessionKey).toBe("telegram:new");
  });

  it("未配置 + 用 updatedAt 作为 fallback 排序依据", () => {
    const store = {
      "a:1": {
        lastChannel: "a",
        lastTo: "u1",
        updatedAt: "2026-05-10T10:00:00Z",
      },
      "b:2": {
        lastChannel: "b",
        lastTo: "u2",
        updatedAt: "2026-05-14T10:00:00Z",
      },
    };
    const api = makeApi(store);
    getRuntimeConfigMock.mockReturnValue({ session: {} });
    getPluginConfigMock.mockReturnValue({});

    const result = resolveNotifyTarget(api);
    expect(result.target?.sessionKey).toBe("b:2");
  });

  it("store 为空 → target: null", () => {
    const api = makeApi({});
    getRuntimeConfigMock.mockReturnValue({ session: {} });
    getPluginConfigMock.mockReturnValue({});

    const result = resolveNotifyTarget(api);
    expect(result.target).toBeNull();
    expect(result.needsBind).toBe(true);
    expect(result.bindReason).toBe("not_configured");
  });

  it("store 中所有 entry 无 lastTo → target: null", () => {
    const store = {
      "a:1": { lastChannel: "wechat" },
      "b:2": { lastChannel: "telegram", lastInteractionAt: 9999 },
    };
    const api = makeApi(store);
    getRuntimeConfigMock.mockReturnValue({ session: {} });
    getPluginConfigMock.mockReturnValue({});

    const result = resolveNotifyTarget(api);
    expect(result.target).toBeNull();
  });

  it("多 session 同 lastInteractionAt → 取最后遍历的（稳定性）", () => {
    const store = {
      "a:1": {
        lastChannel: "a",
        lastTo: "u1",
        lastInteractionAt: 3000,
      },
      "b:2": {
        lastChannel: "b",
        lastTo: "u2",
        lastInteractionAt: 3000,
      },
    };
    const api = makeApi(store);
    getRuntimeConfigMock.mockReturnValue({ session: {} });
    getPluginConfigMock.mockReturnValue({});

    const result = resolveNotifyTarget(api);
    // >= means later entry wins when equal
    expect(result.target?.sessionKey).toBe("b:2");
  });
});

// ─── notifyOwner ─────────────────────────────────────────────────────────────

describe("notifyOwner", () => {
  const boundStore = {
    "wechat:abc": { lastChannel: "wechat", lastTo: "user123" },
  };
  const unboundStore = {
    "telegram:xyz": {
      lastChannel: "telegram",
      lastTo: "tg_user",
      lastInteractionAt: 1000,
    },
  };

  it("无任何可用 channel → ok:false，不调用 subagent", async () => {
    const subagent = makeSubagent();
    const api = makeApi({}, subagent);
    getRuntimeConfigMock.mockReturnValue({ session: {} });
    getPluginConfigMock.mockReturnValue({});

    const result = await notifyOwner(api, "hello");
    expect(result.ok).toBe(false);
    expect(result.error).toContain("no available IM channel");
    expect(subagent.run).not.toHaveBeenCalled();
  });

  it("未绑定且未提供 bindHint → 不发送，返回 needsBind 交回 agent", async () => {
    const subagent = makeSubagent();
    const api = makeApi(unboundStore, subagent);
    getRuntimeConfigMock.mockReturnValue({ session: {} });
    getPluginConfigMock.mockReturnValue({});

    const result = await notifyOwner(api, "提醒该吃药了");
    expect(result.ok).toBe(false);
    expect(result.needsBind).toBe(true);
    expect(result.bindReason).toBe("not_configured");
    expect(result.fallbackChannel).toBe("telegram");
    // 返回里自带可翻译的 bindHint 范例 + 明确的下一步指令（不依赖 agent 去加载 skill）
    expect(result.bindHintExample).toContain("Miloco 通知频道");
    expect(result.nextAction).toContain("bindHint");
    expect(subagent.run).not.toHaveBeenCalled();
  });

  it("配置失效且未提供 bindHint → bindReason: configured_but_invalid，不发送", async () => {
    const subagent = makeSubagent();
    const api = makeApi(unboundStore, subagent);
    getRuntimeConfigMock.mockReturnValue({ session: {} });
    getPluginConfigMock.mockReturnValue({ notifySessionKeys: ["gone:404"] });

    const result = await notifyOwner(api, "msg");
    expect(result.ok).toBe(false);
    expect(result.needsBind).toBe(true);
    expect(result.bindReason).toBe("configured_but_invalid");
    expect(subagent.run).not.toHaveBeenCalled();
  });

  it("未绑定 + 提供 bindHint → 投递到 fallback，fallback:true，正文拼接 bindHint", async () => {
    const subagent = makeSubagent({ status: "ok" });
    const api = makeApi(unboundStore, subagent);
    getRuntimeConfigMock.mockReturnValue({ session: {} });
    getPluginConfigMock.mockReturnValue({});

    const result = await notifyOwner(api, "该吃药了", {
      bindHint: "回复「绑定通知频道」可固定到此",
    });
    expect(result.ok).toBe(true);
    expect(result.channel).toBe("telegram");
    expect(result.fallback).toBe(true);
    expect(subagent.run).toHaveBeenCalledTimes(1);

    const arg = subagent.run.mock.calls[0][0] as { message: string };
    expect(arg.message).toBe(
      "<miloco-notification>该吃药了\n---\n回复「绑定通知频道」可固定到此</miloco-notification>",
    );
  });

  it("空白 bindHint 视为未提供 → 不发送", async () => {
    const subagent = makeSubagent();
    const api = makeApi(unboundStore, subagent);
    getRuntimeConfigMock.mockReturnValue({ session: {} });
    getPluginConfigMock.mockReturnValue({});

    const result = await notifyOwner(api, "msg", { bindHint: "   " });
    expect(result.ok).toBe(false);
    expect(result.needsBind).toBe(true);
    expect(subagent.run).not.toHaveBeenCalled();
  });

  it("已绑定且有效 → 正常发送、无 fallback，且忽略 bindHint", async () => {
    const subagent = makeSubagent({ status: "ok" });
    const api = makeApi(boundStore, subagent);
    getRuntimeConfigMock.mockReturnValue({ session: {} });
    getPluginConfigMock.mockReturnValue({ notifySessionKeys: ["wechat:abc"] });

    const result = await notifyOwner(api, "正文", {
      bindHint: "不应出现的引导语",
    });
    expect(result.ok).toBe(true);
    expect(result.channel).toBe("wechat");
    expect(result.deliveredChannels).toEqual(["wechat"]);
    expect(result.fallback).toBeUndefined();

    const arg = subagent.run.mock.calls[0][0] as { message: string };
    expect(arg.message).toBe("<miloco-notification>正文</miloco-notification>");
    expect(arg.message).not.toContain("不应出现的引导语");
    // 回归保护：工具不再注入任何写死的中文提示
    expect(arg.message).not.toContain("提示：");
  });

  it("subagent 投递失败 → ok:false 带 error", async () => {
    const subagent = makeSubagent({ status: "error", error: "boom" });
    const api = makeApi(boundStore, subagent);
    getRuntimeConfigMock.mockReturnValue({ session: {} });
    getPluginConfigMock.mockReturnValue({ notifySessionKeys: ["wechat:abc"] });

    const result = await notifyOwner(api, "正文");
    expect(result.ok).toBe(false);
    expect(result.error).toContain("subagent delivery failed");
    expect(result.error).toContain("boom");
  });

  it("多通道已绑定 → 向全部有效通道投递", async () => {
    const subagent = makeSubagent({ status: "ok" });
    const api = makeApi(
      {
        "wechat:abc": { lastChannel: "wechat", lastTo: "user123" },
        "telegram:xyz": { lastChannel: "telegram", lastTo: "tg_user" },
      },
      subagent,
    );
    getRuntimeConfigMock.mockReturnValue({ session: {} });
    getPluginConfigMock.mockReturnValue({
      notifySessionKeys: ["wechat:abc", "telegram:xyz"],
    });

    const result = await notifyOwner(api, "正文");
    expect(result.ok).toBe(true);
    expect(result.deliveredChannels).toEqual(["wechat", "telegram"]);
    expect(subagent.run).toHaveBeenCalledTimes(2);
  });
});

// ─── notifyOwner 去重 ─────────────────────────────────────────────────────────

describe("notifyOwner dedup", () => {
  const boundStore = {
    "wechat:abc": { lastChannel: "wechat", lastTo: "user123" },
  };

  function boundApi(subagent: SubagentMock) {
    const api = makeApi(boundStore, subagent);
    getRuntimeConfigMock.mockReturnValue({ session: {} });
    getPluginConfigMock.mockReturnValue({ notifySessionKeys: ["wechat:abc"] });
    return api;
  }

  it("窗口内相同 (接收人, 文案) 第二次 → deduped:true 且不再投递", async () => {
    const subagent = makeSubagent({ status: "ok" });
    const api = boundApi(subagent);

    const first = await notifyOwner(api, "同一条通知");
    expect(first.ok).toBe(true);
    expect(first.deduped).toBeUndefined();

    const second = await notifyOwner(api, "同一条通知");
    expect(second.ok).toBe(true);
    expect(second.deduped).toBe(true);
    expect(subagent.run).toHaveBeenCalledTimes(1); // 第二次没有再投递
  });

  it("不同文案不互相去重", async () => {
    const subagent = makeSubagent({ status: "ok" });
    const api = boundApi(subagent);

    await notifyOwner(api, "文案 A");
    const other = await notifyOwner(api, "文案 B");
    expect(other.deduped).toBeUndefined();
    expect(subagent.run).toHaveBeenCalledTimes(2);
  });

  it("不同接收人同文案不互相去重", async () => {
    const subagent = makeSubagent({ status: "ok" });
    const api1 = boundApi(subagent);
    await notifyOwner(api1, "共用文案");

    const api2 = makeApi(
      { "telegram:xyz": { lastChannel: "telegram", lastTo: "tg" } },
      subagent,
    );
    getRuntimeConfigMock.mockReturnValue({ session: {} });
    getPluginConfigMock.mockReturnValue({ notifySessionKeys: ["telegram:xyz"] });
    const r = await notifyOwner(api2, "共用文案");
    expect(r.deduped).toBeUndefined();
    expect(subagent.run).toHaveBeenCalledTimes(2);
  });

  it("投递失败不记录 → 可立即重发（不被去重）", async () => {
    const subagent = makeSubagent({ status: "error", error: "boom" });
    const api = boundApi(subagent);

    const first = await notifyOwner(api, "重试文案");
    expect(first.ok).toBe(false);
    const second = await notifyOwner(api, "重试文案");
    expect(second.ok).toBe(false);
    expect(second.deduped).toBeUndefined();
    expect(subagent.run).toHaveBeenCalledTimes(2); // 两次都真的投递了
  });

  it("超过窗口 → 可再次投递", async () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime(0);
      const subagent = makeSubagent({ status: "ok" });
      const api = boundApi(subagent);

      await notifyOwner(api, "定时播报");
      vi.setSystemTime(60_001); // 超过 60s 窗口
      const later = await notifyOwner(api, "定时播报");
      expect(later.deduped).toBeUndefined();
      expect(subagent.run).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it("needsBind → 补 bindHint 往返不被去重（首次未发送不计入）", async () => {
    const subagent = makeSubagent({ status: "ok" });
    const api = makeApi(
      {
        "telegram:xyz": {
          lastChannel: "telegram",
          lastTo: "tg_user",
          lastInteractionAt: 1000,
        },
      },
      subagent,
    );
    getRuntimeConfigMock.mockReturnValue({ session: {} });
    getPluginConfigMock.mockReturnValue({});

    const r1 = await notifyOwner(api, "该吃药了");
    expect(r1.needsBind).toBe(true);
    expect(subagent.run).not.toHaveBeenCalled(); // 未发送 → 不计入去重

    const r2 = await notifyOwner(api, "该吃药了", { bindHint: "绑定引导" });
    expect(r2.ok).toBe(true);
    expect(r2.deduped).toBeUndefined();
    expect(subagent.run).toHaveBeenCalledTimes(1); // 真正投递
  });

  it("window=0 关闭去重 → 每次都投递", async () => {
    getNotifyDedupWindowMsMock.mockReturnValue(0);
    const subagent = makeSubagent({ status: "ok" });
    const api = boundApi(subagent);

    await notifyOwner(api, "重复也发");
    const second = await notifyOwner(api, "重复也发");
    expect(second.deduped).toBeUndefined();
    expect(subagent.run).toHaveBeenCalledTimes(2);
  });
});

// ─── openclaw >= 2026.8 runtime 兼容（listSessionEntries / delivery.route）────

describe("2026.8 runtime session API 兼容", () => {
  const routeEntry = {
    sessionKey: "wechat:abc",
    entry: {
      delivery: {
        kind: "external",
        route: {
          channel: "wechat",
          accountId: "acc1",
          target: { to: "user123" },
          thread: { id: "t1" },
        },
      },
    },
  };

  it("已配置 + delivery.route 有效 → needsBind: false，目标为新结构字段", () => {
    const api = makeApiV2([routeEntry]);
    getRuntimeConfigMock.mockReturnValue({ session: {} });
    getPluginConfigMock.mockReturnValue({ notifySessionKeys: ["wechat:abc"] });

    const result = resolveNotifyTarget(api);
    expect(result.needsBind).toBe(false);
    expect(result.target).toEqual({
      channel: "wechat",
      to: "user123",
      accountId: "acc1",
      threadId: "t1",
      sessionKey: "wechat:abc",
    });
  });

  it("delivery.kind = none/internal（无 route）→ 视为无推送目标", () => {
    const api = makeApiV2([
      { sessionKey: "wechat:abc", entry: { delivery: { kind: "none" } } },
      { sessionKey: "mail:def", entry: { delivery: { kind: "internal" } } },
    ]);
    getRuntimeConfigMock.mockReturnValue({ session: {} });
    getPluginConfigMock.mockReturnValue({ notifySessionKeys: ["wechat:abc", "mail:def"] });

    const result = resolveNotifyTarget(api);
    expect(result.needsBind).toBe(true);
    expect(result.bindReason).toBe("configured_but_invalid");
    expect(result.invalidSessionKeys).toEqual(["wechat:abc", "mail:def"]);
  });

  it("新 runtime 读到未迁移的 legacy 顶层 lastTo → 回退识别", () => {
    const api = makeApiV2([
      {
        sessionKey: "telegram:xyz",
        entry: { lastChannel: "telegram", lastTo: "tg_user", lastInteractionAt: 1000 },
      },
    ]);
    getRuntimeConfigMock.mockReturnValue({ session: {} });
    getPluginConfigMock.mockReturnValue({});

    const result = resolveNotifyTarget(api);
    expect(result.needsBind).toBe(true);
    expect(result.bindReason).toBe("not_configured");
    expect(result.target?.channel).toBe("telegram");
    expect(result.target?.to).toBe("tg_user");
  });

  it("notifyOwner 走新 runtime 正常投递", async () => {
    const subagent = makeSubagent({ status: "ok" });
    const api = makeApiV2([routeEntry], subagent);
    getRuntimeConfigMock.mockReturnValue({ session: {} });
    getPluginConfigMock.mockReturnValue({ notifySessionKeys: ["wechat:abc"] });

    const result = await notifyOwner(api, "正文");
    expect(result.ok).toBe(true);
    expect(result.channel).toBe("wechat");
    expect(subagent.run).toHaveBeenCalledTimes(1);
  });

  it("新版路径按 config 默认 agent 传 agentId 给 listSessionEntries（默认 main）", () => {
    const api = makeApiV2([]);
    getRuntimeConfigMock.mockReturnValue({ session: {}, agents: { entries: { main: {} } } });
    getPluginConfigMock.mockReturnValue({});

    resolveNotifyTarget(api);
    expect(api.runtime.agent.session.listSessionEntries).toHaveBeenCalledWith({
      agentId: "main",
    });
  });

  it("无 roster 配置（agents 缺失）→ 默认 main", () => {
    const api = makeApiV2([]);
    getRuntimeConfigMock.mockReturnValue({ session: {} });
    getPluginConfigMock.mockReturnValue({});

    resolveNotifyTarget(api);
    expect(api.runtime.agent.session.listSessionEntries).toHaveBeenCalledWith({
      agentId: "main",
    });
  });

  it("entries 唯一 agent 非 main 时用该 id", () => {
    const api = makeApiV2([]);
    getRuntimeConfigMock.mockReturnValue({ session: {}, agents: { entries: { alice: {} } } });
    getPluginConfigMock.mockReturnValue({});

    resolveNotifyTarget(api);
    expect(api.runtime.agent.session.listSessionEntries).toHaveBeenCalledWith({
      agentId: "alice",
    });
  });

  it("agents.list 唯一非 main agent → 用该 id（host 走 list 兜底）", () => {
    const api = makeApiV2([]);
    getRuntimeConfigMock.mockReturnValue({
      session: {},
      agents: { list: [{ id: "home" }] },
    });
    getPluginConfigMock.mockReturnValue({});

    resolveNotifyTarget(api);
    expect(api.runtime.agent.session.listSessionEntries).toHaveBeenCalledWith({
      agentId: "home",
    });
  });

  it("entries 与 list 并存时 entries 优先（host readAgentRosterProperty 语义）", () => {
    const api = makeApiV2([]);
    getRuntimeConfigMock.mockReturnValue({
      session: {},
      agents: { entries: { home: {} }, list: [{ id: "other" }] },
    });
    getPluginConfigMock.mockReturnValue({});

    resolveNotifyTarget(api);
    expect(api.runtime.agent.session.listSessionEntries).toHaveBeenCalledWith({
      agentId: "home",
    });
  });

  it("多 agent 中唯一 default:true 标记者胜出（非 main）", () => {
    const api = makeApiV2([]);
    getRuntimeConfigMock.mockReturnValue({
      session: {},
      agents: {
        list: [{ id: "home", default: true }, { id: "guest" }],
      },
    });
    getPluginConfigMock.mockReturnValue({});

    resolveNotifyTarget(api);
    expect(api.runtime.agent.session.listSessionEntries).toHaveBeenCalledWith({
      agentId: "home",
    });
  });

  it("多 agent 无 default 标记且不唯一 → 回退 main（host 该场景会抛，读侧降级）", () => {
    const api = makeApiV2([]);
    getRuntimeConfigMock.mockReturnValue({
      session: {},
      agents: { entries: { home: {}, guest: {} } },
    });
    getPluginConfigMock.mockReturnValue({});

    resolveNotifyTarget(api);
    expect(api.runtime.agent.session.listSessionEntries).toHaveBeenCalledWith({
      agentId: "main",
    });
  });

  it("agent id 规范化：大小写/非法字符/首尾连字符 → host normalizeAgentId", () => {
    const api = makeApiV2([]);
    getRuntimeConfigMock.mockReturnValue({
      session: {},
      agents: { list: [{ id: "Home Bot!" }] },
    });
    getPluginConfigMock.mockReturnValue({});

    resolveNotifyTarget(api);
    expect(api.runtime.agent.session.listSessionEntries).toHaveBeenCalledWith({
      agentId: "home-bot",
    });
  });

  it("8.2 runtime 真实形态：default 标记被宿主剥离、物化进 defaults.systemAgent → 读 systemAgent", () => {
    const api = makeApiV2([]);
    getRuntimeConfigMock.mockReturnValue({
      session: {},
      agents: {
        defaults: { systemAgent: { agentId: "home" } },
        entries: { home: {}, guest: {} },
      },
    });
    getPluginConfigMock.mockReturnValue({});

    resolveNotifyTarget(api);
    expect(api.runtime.agent.session.listSessionEntries).toHaveBeenCalledWith({
      agentId: "home",
    });
  });

  it("entries 键存在但值 undefined → 宿主语义继续看 list（!== void 0 守卫）", () => {
    const api = makeApiV2([]);
    getRuntimeConfigMock.mockReturnValue({
      session: {},
      agents: { entries: undefined, list: [{ id: "home" }] },
    });
    getPluginConfigMock.mockReturnValue({});

    resolveNotifyTarget(api);
    expect(api.runtime.agent.session.listSessionEntries).toHaveBeenCalledWith({
      agentId: "home",
    });
  });

  it("名册多 agent 无 default 标记 → 推导 main 不在名册 → 打指名 WARN（宿主返回空表不抛错）", () => {
    const warnSpy = vi.fn();
    logger.init({ logger: { warn: warnSpy } } as any);
    const api = makeApiV2([]);
    getRuntimeConfigMock.mockReturnValue({
      session: {},
      agents: { entries: { home: {}, guest: {} } },
    });
    getPluginConfigMock.mockReturnValue({});

    resolveNotifyTarget(api);
    expect(warnSpy).toHaveBeenCalledTimes(1);
    expect(String(warnSpy.mock.calls[0][0])).toContain('agentId="main"');
    expect(String(warnSpy.mock.calls[0][0])).toContain("home, guest");
  });

  it("合法空名册（entries:{}）不算畸形 → 不告警，隐式 main 照常工作", () => {
    const warnSpy = vi.fn();
    logger.init({ logger: { warn: warnSpy } } as any);
    const api = makeApiV2([]);
    getRuntimeConfigMock.mockReturnValue({
      session: {},
      agents: { entries: {} },
    });
    getPluginConfigMock.mockReturnValue({});

    resolveNotifyTarget(api);
    expect(api.runtime.agent.session.listSessionEntries).toHaveBeenCalledWith({
      agentId: "main",
    });
    expect(warnSpy).not.toHaveBeenCalled();
  });

  it("entries 与 list 并存且 list 带唯一 default 标记 → 仍按 entries 解析（宿主 readAgentRosterProperty 短路）", () => {
    const api = makeApiV2([]);
    getRuntimeConfigMock.mockReturnValue({
      session: {},
      agents: {
        entries: { alpha: {} },
        list: [{ id: "beta", default: true }],
      },
    });
    getPluginConfigMock.mockReturnValue({});

    resolveNotifyTarget(api);
    expect(api.runtime.agent.session.listSessionEntries).toHaveBeenCalledWith({
      agentId: "alpha",
    });
  });

  it("id 规范化顺序与宿主一致：去首尾 - 早于截 64 → 65 字符 id 截出尾连字符", () => {
    const api = makeApiV2([]);
    getRuntimeConfigMock.mockReturnValue({
      session: {},
      agents: { list: [{ id: "a".repeat(63) + " b" }] },
    });
    getPluginConfigMock.mockReturnValue({});

    resolveNotifyTarget(api);
    expect(api.runtime.agent.session.listSessionEntries).toHaveBeenCalledWith({
      agentId: "a".repeat(63) + "-",
    });
  });

  it("宿主会话访问器缺失（runtime.agent.session 被摘）→ 空表降级 + WARN，不抛 500", () => {
    const warnSpy = vi.fn();
    logger.init({ logger: { warn: warnSpy } } as any);
    const api = { runtime: { agent: {} } } as any;
    getRuntimeConfigMock.mockReturnValue({ session: {} });
    getPluginConfigMock.mockReturnValue({});

    let result: ReturnType<typeof resolveNotifyTarget> | undefined;
    expect(() => {
      result = resolveNotifyTarget(api);
    }).not.toThrow();
    expect(result?.target).toBeNull();
    expect(warnSpy).toHaveBeenCalledTimes(1);
    expect(String(warnSpy.mock.calls[0][0])).toContain("会话访问器");
  });

  it("两代会话 API 都探测不到 → 空表降级 + WARN（宿主再摘 API 的复发形态可排查）", () => {
    const warnSpy = vi.fn();
    logger.init({ logger: { warn: warnSpy } } as any);
    const api = { runtime: { agent: { session: {} } } } as any;
    getRuntimeConfigMock.mockReturnValue({ session: {} });
    getPluginConfigMock.mockReturnValue({});

    let result: ReturnType<typeof resolveNotifyTarget> | undefined;
    expect(() => {
      result = resolveNotifyTarget(api);
    }).not.toThrow();
    expect(result?.target).toBeNull();
    expect(warnSpy).toHaveBeenCalledTimes(1);
    expect(String(warnSpy.mock.calls[0][0])).toContain("会话读取能力不可用");
  });

  it("kind=external 但 route 读不齐 → 回退顶层 lastTo/lastChannel（惰性迁移写一半）", () => {
    const api = makeApiV2([
      {
        sessionKey: "wechat:abc",
        entry: {
          delivery: { kind: "external", route: { channel: "wechat" } },
          lastChannel: "wechat",
          lastTo: "user123",
        },
      },
    ]);
    getRuntimeConfigMock.mockReturnValue({ session: {} });
    getPluginConfigMock.mockReturnValue({});

    const result = resolveNotifyTarget(api);
    expect(result.target?.channel).toBe("wechat");
    expect(result.target?.to).toBe("user123");
  });

  it("kind=external 但 route 读不齐且无顶层老字段 → 无目标", () => {
    const api = makeApiV2([
      {
        sessionKey: "wechat:abc",
        entry: { delivery: { kind: "external" } },
      },
    ]);
    getRuntimeConfigMock.mockReturnValue({ session: {} });
    getPluginConfigMock.mockReturnValue({});

    expect(resolveNotifyTarget(api).target).toBeNull();
  });

  it("老宿主 legacy 读失败（会话文件损坏）→ 空表降级 + WARN，不抛 500", () => {
    const warnSpy = vi.fn();
    logger.init({ logger: { warn: warnSpy } } as any);
    const api = {
      runtime: {
        agent: {
          session: {
            resolveStorePath: vi.fn(() => "/tmp/sessions.json"),
            loadSessionStore: vi.fn(() => {
              throw new Error("Unexpected token } in JSON");
            }),
          },
        },
      },
    } as any;
    getRuntimeConfigMock.mockReturnValue({ session: {} });
    getPluginConfigMock.mockReturnValue({});

    let result: ReturnType<typeof resolveNotifyTarget> | undefined;
    expect(() => {
      result = resolveNotifyTarget(api);
    }).not.toThrow();
    expect(result?.target).toBeNull();
    expect(warnSpy).toHaveBeenCalledTimes(1);
    expect(String(warnSpy.mock.calls[0][0])).toContain(
      "legacy loadSessionStore failed",
    );
  });

  it("listSessionEntries 抛错（agentId 在宿主上不存在）→ 降级为空表，不抛给调用方", () => {
    const api = {
      runtime: {
        agent: {
          session: {
            listSessionEntries: vi.fn(() => {
              throw new Error(
                "Cannot resolve SQLite session scope without an agent id",
              );
            }),
          },
        },
      },
    } as any;
    getRuntimeConfigMock.mockReturnValue({
      session: {},
      agents: { entries: { home: {}, guest: {} } },
    });
    getPluginConfigMock.mockReturnValue({});

    expect(() => resolveNotifyTarget(api)).not.toThrow();
    expect(resolveNotifyTarget(api).target).toBeNull();
  });

  it("对账不误报：agentId 不在名册但读到了会话（legacy main 库仍有数据的升级宿主）→ 无 WARN", () => {
    const warnSpy = vi.fn();
    logger.init({ logger: { warn: warnSpy } } as any);
    const api = makeApiV2([
      {
        sessionKey: "wechat:abc",
        entry: {
          delivery: {
            kind: "external",
            route: {
              channel: "wechat",
              target: { to: "user123" },
            },
          },
        },
      },
    ]);
    getRuntimeConfigMock.mockReturnValue({
      session: {},
      agents: { ownership: "explicit", entries: { home: {}, guest: {} } },
    });
    getPluginConfigMock.mockReturnValue({});

    resolveNotifyTarget(api);
    expect(warnSpy).not.toHaveBeenCalled();
  });

  it("entries 属性存在但值为 null → 名册解析为空的异常路径单独告警", () => {
    const warnSpy = vi.fn();
    logger.init({ logger: { warn: warnSpy } } as any);
    const api = makeApiV2([]);
    getRuntimeConfigMock.mockReturnValue({
      session: {},
      agents: { entries: null },
    });
    getPluginConfigMock.mockReturnValue({});

    resolveNotifyTarget(api);
    expect(api.runtime.agent.session.listSessionEntries).toHaveBeenCalledWith({
      agentId: "main",
    });
    expect(warnSpy).toHaveBeenCalledTimes(1);
    expect(String(warnSpy.mock.calls[0][0])).toContain("解析不出任何 agent");
    expect(String(warnSpy.mock.calls[0][0])).toContain('"entries":"object"');
  });

  it("list 中无 id 的项与宿主同样落成 main 成员（不丢弃），多成员时推导 main 不告警", () => {
    const warnSpy = vi.fn();
    logger.init({ logger: { warn: warnSpy } } as any);
    const api = makeApiV2([]);
    getRuntimeConfigMock.mockReturnValue({
      session: {},
      agents: { list: [{ id: "home" }, {}] },
    });
    getPluginConfigMock.mockReturnValue({});

    resolveNotifyTarget(api);
    expect(api.runtime.agent.session.listSessionEntries).toHaveBeenCalledWith({
      agentId: "main",
    });
    expect(warnSpy).not.toHaveBeenCalled();
  });

  it("delivery.kind = none 但残留 legacy lastTo → 仍视为无推送目标", () => {
    const api = makeApiV2([
      {
        sessionKey: "wechat:abc",
        entry: {
          delivery: { kind: "none" },
          lastChannel: "wechat",
          lastTo: "user123",
        },
      },
    ]);
    getRuntimeConfigMock.mockReturnValue({ session: {} });
    getPluginConfigMock.mockReturnValue({ notifySessionKeys: ["wechat:abc"] });

    const result = resolveNotifyTarget(api);
    expect(result.needsBind).toBe(true);
    expect(result.bindReason).toBe("configured_but_invalid");
  });

  it("多条 delivery.route 候选按最近活跃时间排序（不依赖枚举顺序）", () => {
    const mk = (key: string, to: string, ts: number) => ({
      sessionKey: key,
      entry: {
        delivery: {
          kind: "external",
          route: { channel: "wechat", target: { to } },
        },
        lastInteractionAt: ts,
      },
    });
    const api = makeApiV2([
      mk("wechat:new", "new_user", 2000),
      mk("wechat:old", "old_user", 1000),
    ]);
    getRuntimeConfigMock.mockReturnValue({ session: {} });
    getPluginConfigMock.mockReturnValue({});

    const result = resolveNotifyTarget(api);
    expect(result.target?.to).toBe("new_user");
  });
});
