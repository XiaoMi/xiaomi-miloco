import { expect, test, vi } from "vitest";
import { registerHomeProfileScheduler } from "../src/home-profile/scheduler.js";
import { registerHooks } from "../src/hooks/index.js";

vi.mock("../src/miloco/config.js", () => ({ isSchedulerAutoManageEnabled: () => true }));
vi.mock("../src/config.js", () => ({ getPluginConfig: () => ({}) }));
vi.mock("../src/hooks/prompt.js", () => ({ registerBeforePromptBuildHook: vi.fn() }));
vi.mock("../src/hooks/trace.js", () => ({ registerTraceHooks: vi.fn() }));

test("IoT belongs to Observe, never a patrol inference hook", async () => {
  const handlers = new Map<string, (...args: any[]) => any>();
  const api = { on: (name: string, callback: any) => handlers.set(name, callback) } as any;
  const cron = { list: vi.fn().mockResolvedValue([]), add: vi.fn() };
  registerHomeProfileScheduler(api);
  registerHooks(api);
  await handlers.get("gateway_start")!({}, { getCron: () => cron });
  expect(cron.add).toHaveBeenCalledTimes(4);
  const tasks = cron.add.mock.calls.map(([task]) => task);
  const patrol = tasks.find(task => task.name === "miloco-home-patrol");
  const dreaming = tasks.find(task => task.name === "miloco-home-dreaming");
  expect(patrol.payload.message).not.toContain("IoT");
  expect(patrol.payload.message).not.toContain("iot-observation");
  expect(dreaming.payload.message).toContain("IoT 旁路");
  expect(patrol.schedule.expr).toBe("*/30 * * * *");
  expect(patrol.delivery.mode).toBe("none");
  expect(handlers.has("before_model_resolve")).toBe(false);
});
