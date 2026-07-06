import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../src/utils/logger.js", () => ({
  logger: { info: vi.fn(), warn: vi.fn(), error: vi.fn(), debug: vi.fn() },
}));

import { kResetSessionsWebhook } from "../src/webhooks/reset_sessions.js";

const KEYS = [
  "agent:main:miloco",
  "agent:main:miloco-rule",
  "agent:main:miloco-suggest",
];

function makeApi(deleteSession?: ReturnType<typeof vi.fn>) {
  const del = deleteSession ?? vi.fn(async () => {});
  const api = { runtime: { subagent: { deleteSession: del } } } as never;
  return { api, del };
}

// biome-ignore lint/suspicious/noExplicitAny: test payload shape
function invoke(api: unknown, payload: any) {
  // biome-ignore lint/suspicious/noExplicitAny: webhook returns any
  return kResetSessionsWebhook.action({ api, payload } as any) as Promise<any>;
}

afterEach(() => vi.clearAllMocks());

describe("kResetSessionsWebhook 批量重置 session", () => {
  it("全部删除成功 → 每个 key 各 deleteSession 一次，返回 reset 全集", async () => {
    const { api, del } = makeApi();
    const res = await invoke(api, { sessionKeys: KEYS });

    expect(del).toHaveBeenCalledTimes(3);
    for (const sessionKey of KEYS) {
      expect(del).toHaveBeenCalledWith({ sessionKey, deleteTranscript: true });
    }
    expect(res.reset).toEqual(KEYS);
    expect(res.failed).toEqual([]);
  });

  it("deleteTranscript 透传（false）", async () => {
    const { api, del } = makeApi();
    await invoke(api, {
      sessionKeys: ["agent:main:miloco"],
      deleteTranscript: false,
    });
    expect(del).toHaveBeenCalledWith({
      sessionKey: "agent:main:miloco",
      deleteTranscript: false,
    });
  });

  it("单个 key 删除失败 → 其余继续，failed 记录该 key", async () => {
    const del = vi.fn(async ({ sessionKey }: { sessionKey: string }) => {
      if (sessionKey === "agent:main:miloco-rule") throw new Error("boom");
    });
    const { api } = makeApi(del);
    const res = await invoke(api, { sessionKeys: KEYS });

    expect(del).toHaveBeenCalledTimes(3);
    expect(res.reset).toEqual(["agent:main:miloco", "agent:main:miloco-suggest"]);
    expect(res.failed).toEqual([
      { sessionKey: "agent:main:miloco-rule", error: "boom" },
    ]);
  });

  it("空 / 非数组 sessionKeys → 结构化错误，不触发 deleteSession", async () => {
    const { api, del } = makeApi();
    const empty = await invoke(api, { sessionKeys: [] });
    expect(empty.status).toBe("error");
    const missing = await invoke(api, {});
    expect(missing.status).toBe("error");
    expect(del).not.toHaveBeenCalled();
  });
});
