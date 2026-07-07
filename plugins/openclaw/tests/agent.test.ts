import { afterEach, describe, expect, it, vi } from "vitest";

// 控制 trace 检测信号：getTurnStatus 恒 "done"（不睡眠），peekTurnMeta 按 runId 返回 meta。
type TurnMeta = { success: boolean; errorMsg: string | null } | undefined;
const registerTraceLinkMock = vi.fn<(runId: string, traceId: string) => void>();
const getTurnStatusMock = vi.fn<() => string>(() => "done");
const peekTurnMetaMock = vi.fn<(runId: string) => TurnMeta>();

vi.mock("../src/hooks/trace.js", () => ({
  registerTraceLink: (runId: string, traceId: string) =>
    registerTraceLinkMock(runId, traceId),
  getTurnStatus: () => getTurnStatusMock(),
  peekTurnMeta: (runId: string) => peekTurnMetaMock(runId),
}));

// owner-channel 解析：按测试用例切换"解析到车主会话 / 无可用 channel"。
type ResolveResultLike = { target: { sessionKey: string } | null };
const resolveNotifyTargetMock = vi.fn<() => ResolveResultLike>(() => ({
  target: null,
}));

vi.mock("../src/tools/notify.js", () => ({
  resolveNotifyTarget: () => resolveNotifyTargetMock(),
}));

import { kAgentWebhook } from "../src/webhooks/agent.js";

const OVERFLOW = "Context overflow: prompt too large for the model (precheck).";
const SESSION = "agent:main:miloco-rule";

type Wait = { status: string; error?: string };

function makeApi(opts: {
  waitByRunId?: Record<string, Wait>;
  deleteSession?: ReturnType<typeof vi.fn>;
}) {
  // 平台实测：runId == 传入的 idempotencyKey，这里照此模拟以区分首次/重试。
  const run = vi.fn(async (p: { idempotencyKey: string }) => ({
    runId: p.idempotencyKey,
  }));
  const waitForRun = vi.fn(
    async (p: { runId: string }) =>
      opts.waitByRunId?.[p.runId] ?? { status: "ok" },
  );
  const deleteSession = opts.deleteSession ?? vi.fn(async () => {});
  const api = {
    runtime: { subagent: { run, waitForRun, deleteSession } },
  } as never;
  return { api, run, waitForRun, deleteSession };
}

function invoke(api: unknown, idempotencyKey = "t1") {
  return kAgentWebhook.action({
    api,
    payload: {
      message: "m",
      sessionKey: SESSION,
      idempotencyKey,
      traceId: "tr",
      timeoutMs: 1000,
    },
  } as never);
}

afterEach(() => {
  vi.clearAllMocks();
  getTurnStatusMock.mockReturnValue("done");
});

describe("kAgentWebhook 上下文溢出自愈", () => {
  it("溢出 → deleteSession 一次 → 重试成功 → recovered=true", async () => {
    peekTurnMetaMock.mockImplementation((runId: string) =>
      runId === "t1"
        ? { success: false, errorMsg: OVERFLOW }
        : { success: true, errorMsg: null },
    );
    const { api, run, waitForRun, deleteSession } = makeApi({
      waitByRunId: { t1: { status: "ok" }, "t1:retry": { status: "ok" } },
    });

    const res = (await invoke(api)) as {
      runId: string;
      status: string;
      error?: string;
      recovered?: boolean;
    };

    expect(deleteSession).toHaveBeenCalledTimes(1);
    expect(deleteSession).toHaveBeenCalledWith({
      sessionKey: SESSION,
      deleteTranscript: true,
    });
    expect(run).toHaveBeenCalledTimes(2);
    expect(res.runId).toBe("t1:retry");
    expect(res.status).toBe("ok");
    expect(res.recovered).toBe(true);
    // 即便已恢复，也把触发自愈的溢出原因带回后端
    expect(res.error).toContain("Context overflow");
    // 重试等待预算由 timeoutMs 推算而非固定 60s：payload timeoutMs=1000 < 下限 → 取 10s 地板
    expect(waitForRun).toHaveBeenNthCalledWith(2, {
      runId: "t1:retry",
      timeoutMs: 10_000,
    });
  });

  it("非溢出失败 → 不删除、不重试", async () => {
    peekTurnMetaMock.mockImplementation(() => ({
      success: false,
      errorMsg: "tool blew up",
    }));
    const { api, run, deleteSession } = makeApi({
      waitByRunId: { t1: { status: "error", error: "tool blew up" } },
    });

    const res = (await invoke(api)) as {
      runId: string;
      status: string;
      recovered?: boolean;
    };

    expect(deleteSession).not.toHaveBeenCalled();
    expect(run).toHaveBeenCalledTimes(1);
    expect(res.runId).toBe("t1");
    expect(res.status).toBe("error");
    expect(res.recovered).toBeUndefined();
  });

  it("deleteSession 抛错（如主会话保护）→ 返回首个结果、不崩", async () => {
    peekTurnMetaMock.mockImplementation(() => ({
      success: false,
      errorMsg: OVERFLOW,
    }));
    const deleteSession = vi.fn(async () => {
      throw new Error("Cannot delete the main session");
    });
    const { api, run } = makeApi({
      waitByRunId: { t1: { status: "ok" } },
      deleteSession,
    });

    const res = (await invoke(api)) as {
      runId: string;
      status: string;
      recovered?: boolean;
    };

    expect(deleteSession).toHaveBeenCalledTimes(1);
    expect(run).toHaveBeenCalledTimes(1); // 抛错发生在重试前 → 不重试
    expect(res.runId).toBe("t1");
    expect(res.recovered).toBeUndefined();
  });

  it("重试后仍溢出（系统提示型不可恢复）→ recovered=false、不死循环", async () => {
    peekTurnMetaMock.mockImplementation(() => ({
      success: false,
      errorMsg: OVERFLOW,
    }));
    const { api, run, deleteSession } = makeApi({
      waitByRunId: { t1: { status: "ok" }, "t1:retry": { status: "ok" } },
    });

    const res = (await invoke(api)) as {
      runId: string;
      error?: string;
      recovered?: boolean;
    };

    expect(deleteSession).toHaveBeenCalledTimes(1);
    expect(run).toHaveBeenCalledTimes(2); // 恰好两次：首次 + 一次重试，不再继续
    expect(res.runId).toBe("t1:retry");
    expect(res.recovered).toBe(false);
    expect(res.error).toContain("Context overflow"); // 不可恢复时带回溢出原因
  });

  it("未溢出（success=true）→ 行为不变，不触发自愈", async () => {
    peekTurnMetaMock.mockImplementation(() => ({
      success: true,
      errorMsg: null,
    }));
    const { api, run, deleteSession } = makeApi({
      waitByRunId: { t1: { status: "ok" } },
    });

    const res = (await invoke(api)) as {
      runId: string;
      status: string;
      recovered?: boolean;
    };

    expect(deleteSession).not.toHaveBeenCalled();
    expect(run).toHaveBeenCalledTimes(1);
    expect(res.runId).toBe("t1");
    expect(res.status).toBe("ok");
    expect(res.recovered).toBeUndefined();
  });
});

describe("kAgentWebhook owner-channel 投递", () => {
  function invokeWith(api: unknown, extra: Record<string, unknown>) {
    return kAgentWebhook.action({
      api,
      payload: {
        message: "m",
        sessionKey: SESSION,
        idempotencyKey: "t1",
        traceId: "tr",
        timeoutMs: 1000,
        ...extra,
      },
    } as never);
  }

  it("默认路径不变：deliver:false、用 payload sessionKey，且不做 channel 解析", async () => {
    peekTurnMetaMock.mockImplementation(() => ({
      success: true,
      errorMsg: null,
    }));
    const { api, run } = makeApi({ waitByRunId: { t1: { status: "ok" } } });

    await invoke(api);

    expect(resolveNotifyTargetMock).not.toHaveBeenCalled();
    expect(run).toHaveBeenCalledWith(
      expect.objectContaining({ sessionKey: SESSION, deliver: false }),
    );
  });

  it("resolveTarget=owner-channel：turn 跑在解析出的车主会话且 deliver:true", async () => {
    peekTurnMetaMock.mockImplementation(() => ({
      success: true,
      errorMsg: null,
    }));
    resolveNotifyTargetMock.mockReturnValue({
      target: { sessionKey: "wechat:dm:owner-1" },
    });
    const { api, run } = makeApi({ waitByRunId: { t1: { status: "ok" } } });

    const res = (await invokeWith(api, {
      resolveTarget: "owner-channel",
    })) as { runId: string; status: string };

    expect(run).toHaveBeenCalledWith(
      expect.objectContaining({
        sessionKey: "wechat:dm:owner-1",
        deliver: true,
      }),
    );
    expect(res.status).toBe("ok");
    expect(res.runId).toBe("t1");
  });

  it("owner-channel 显式 deliver:false 时尊重调用方", async () => {
    peekTurnMetaMock.mockImplementation(() => ({
      success: true,
      errorMsg: null,
    }));
    resolveNotifyTargetMock.mockReturnValue({
      target: { sessionKey: "wechat:dm:owner-1" },
    });
    const { api, run } = makeApi({ waitByRunId: { t1: { status: "ok" } } });

    await invokeWith(api, { resolveTarget: "owner-channel", deliver: false });

    expect(run).toHaveBeenCalledWith(
      expect.objectContaining({
        sessionKey: "wechat:dm:owner-1",
        deliver: false,
      }),
    );
  });

  it("无可用 IM channel → 结构化 no-channel（非抛错），不起 turn", async () => {
    resolveNotifyTargetMock.mockReturnValue({ target: null });
    const { api, run } = makeApi({});

    const res = (await invokeWith(api, {
      resolveTarget: "owner-channel",
    })) as { runId: string | null; status: string; error?: string };

    expect(run).not.toHaveBeenCalled();
    expect(res.runId).toBeNull();
    expect(res.status).toBe("no-channel");
    expect(res.error).toContain("no available IM channel");
  });

  it("owner-channel 溢出不做 deleteSession 自愈（不删用户真实会话）", async () => {
    peekTurnMetaMock.mockImplementation(() => ({
      success: false,
      errorMsg: OVERFLOW,
    }));
    resolveNotifyTargetMock.mockReturnValue({
      target: { sessionKey: "wechat:dm:owner-1" },
    });
    const { api, run, deleteSession } = makeApi({
      waitByRunId: { t1: { status: "ok" } },
    });

    const res = (await invokeWith(api, {
      resolveTarget: "owner-channel",
    })) as { runId: string };

    expect(deleteSession).not.toHaveBeenCalled();
    expect(run).toHaveBeenCalledTimes(1); // 无重试 turn
    expect(res.runId).toBe("t1");
  });
});
