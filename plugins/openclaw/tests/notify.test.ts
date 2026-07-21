import { beforeEach, describe, expect, it, vi } from "vitest";

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

// 去重是模块级状态：每个用例前清空，且默认窗口 60s（个别用例可覆盖）。
beforeEach(() => {
  __resetNotifyDedup();
  getNotifyDedupWindowMsMock.mockReturnValue(60_000);
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
