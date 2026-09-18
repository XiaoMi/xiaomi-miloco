import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";

import { realCreateCameraTask } from "@/api/real";
import { TaskCreateDialog } from "@/components/TaskCreateDialog";
import "@/i18n";

const originalFetch = globalThis.fetch;

afterEach(() => {
  vi.restoreAllMocks();
  globalThis.fetch = originalFetch;
});

function ok(data: unknown = null) {
  return new Response(JSON.stringify({ code: 0, message: "OK", data }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function renderCameraState(
  camerasState:
    | { kind: "loading" }
    | { kind: "error"; message: string }
    | { kind: "ready" },
) {
  return renderToStaticMarkup(
    <TaskCreateDialog
      cameras={[]}
      camerasState={camerasState}
      onCamerasRetry={() => undefined}
      onClose={() => undefined}
      onCreated={() => undefined}
    />,
  );
}

describe("task creation review regressions", () => {
  it("uses the task id to keep rule names unique", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(ok({ task_id: "web_task_1" }))
      .mockResolvedValueOnce(ok({ rule_id: "rule-1" }));
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    await realCreateCameraTask({
      taskId: "web_task_1",
      description: "看孩子练琴",
      query: "孩子正在练琴",
      perceiveDeviceIds: ["cam-1"],
      actionDescription: "通知我",
    });

    const ruleInit = fetchMock.mock.calls[1]?.[1] as RequestInit;
    expect(JSON.parse(String(ruleInit.body)).name).toBe(
      "看孩子练琴 (web_task_1)",
    );
  });

  it("distinguishes camera loading from a truly empty camera list", () => {
    const html = renderCameraState({ kind: "loading" });
    expect(html).toContain("正在加载摄像头");
    expect(html).not.toContain("当前没有正在使用的摄像头");
  });

  it("shows camera load failures with a retry action", () => {
    const html = renderCameraState({
      kind: "error",
      message: "network offline",
    });
    expect(html).toContain("摄像头列表加载失败");
    expect(html).toContain("network offline");
    expect(html).toContain("重试");
    expect(html).not.toContain("当前没有正在使用的摄像头");
  });

  it("keeps the existing empty message for a loaded empty list", () => {
    const html = renderCameraState({ kind: "ready" });
    expect(html).toContain("当前没有正在使用的摄像头");
  });
});
