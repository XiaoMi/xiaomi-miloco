import { afterEach, describe, expect, it, vi } from "vitest";

import { realCreateCameraTask } from "@/api/real";

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

describe("direct camera task creation", () => {
  it("creates the task before its runnable perception rule", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(ok({ task_id: "web_task_1" }))
      .mockResolvedValueOnce(ok({ rule_id: "rule-1" }));
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    await realCreateCameraTask({
      taskId: "web_task_1",
      description: "阳台如厕异常监控",
      query: "猫咪短时间反复进出猫砂盆或持续停留超过五分钟",
      perceiveDeviceIds: ["balcony:ch1"],
      actionDescription: "通知住户并说明观察到的异常行为",
    });

    expect(fetchMock).toHaveBeenCalledTimes(2);
    const [taskUrl, taskInit] = fetchMock.mock.calls[0] as [
      string,
      RequestInit,
    ];
    expect(taskUrl).toBe("/api/tasks");
    expect(taskInit.method).toBe("POST");
    expect(JSON.parse(String(taskInit.body))).toEqual({
      task_id: "web_task_1",
      description: "阳台如厕异常监控",
      lifecycle: "permanent",
    });

    const [ruleUrl, ruleInit] = fetchMock.mock.calls[1] as [
      string,
      RequestInit,
    ];
    expect(ruleUrl).toBe("/api/rules");
    expect(ruleInit.method).toBe("POST");
    expect(JSON.parse(String(ruleInit.body))).toMatchObject({
      name: "阳台如厕异常监控 (web_task_1)",
      task_id: "web_task_1",
      mode: "event",
      direction: "enter",
      lifecycle: "permanent",
      condition: {
        perceive_device_ids: ["balcony:ch1"],
        query: "猫咪短时间反复进出猫砂盆或持续停留超过五分钟",
      },
      action_descriptions: ["通知住户并说明观察到的异常行为"],
    });
  });

  it("rolls the task back when rule creation fails", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(ok({ task_id: "web_task_2" }))
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ code: 1, message: "bad rule" }), {
          status: 400,
          headers: { "Content-Type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(ok());
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    await expect(
      realCreateCameraTask({
        taskId: "web_task_2",
        description: "测试任务",
        query: "测试条件",
        perceiveDeviceIds: ["cam-1"],
        actionDescription: "通知测试",
      }),
    ).rejects.toThrow();

    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(fetchMock.mock.calls[2]?.[0]).toBe(
      "/api/tasks/web_task_2?reason=abandoned",
    );
    expect((fetchMock.mock.calls[2]?.[1] as RequestInit).method).toBe("DELETE");
  });
});
