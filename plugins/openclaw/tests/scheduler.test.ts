import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import type { OpenClawPluginApi } from "openclaw/plugin-sdk";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { registerHomeProfileScheduler } from "../src/home-profile/scheduler.js";

/**
 * 覆盖 scheduler.enabled 开关对 gateway_start 行为的控制：
 *  - 默认 / true：reconcile —— 为内置任务调用 add；
 *  - false：teardown —— 只清除已存在的 managed 任务，绝不 add。
 */

type CronJob = { id: string; name?: string; description?: string };

function makeCron(existing: CronJob[] = []) {
  const jobs = [...existing];
  const calls = { add: 0, remove: 0, update: 0 };
  return {
    calls,
    jobs,
    service: {
      list: async () => jobs.slice(),
      add: async (job: CronJob) => {
        calls.add += 1;
        jobs.push({ ...job, id: `new-${calls.add}` });
      },
      update: async () => {
        calls.update += 1;
      },
      remove: async (id: string) => {
        calls.remove += 1;
        const i = jobs.findIndex((j) => j.id === id);
        if (i >= 0) jobs.splice(i, 1);
      },
    },
  };
}

function makeApi(): {
  api: OpenClawPluginApi;
  fireStart: (cron: unknown) => Promise<void>;
} {
  const handlers: Record<string, (event: unknown, ctx: unknown) => unknown> =
    {};
  const api = {
    on: (name: string, fn: (event: unknown, ctx: unknown) => unknown) => {
      handlers[name] = fn;
    },
  } as unknown as OpenClawPluginApi;
  return {
    api,
    fireStart: async (cron) => {
      await handlers.gateway_start?.({}, { getCron: () => cron } as unknown);
    },
  };
}

describe("home-profile scheduler toggle", () => {
  let origHome: string | undefined;
  let tmpHome: string;

  beforeEach(() => {
    origHome = process.env.MILOCO_HOME;
    tmpHome = mkdtempSync(path.join(tmpdir(), "miloco-home-"));
    process.env.MILOCO_HOME = tmpHome;
  });

  afterEach(() => {
    if (origHome === undefined) delete process.env.MILOCO_HOME;
    else process.env.MILOCO_HOME = origHome;
    rmSync(tmpHome, { recursive: true, force: true });
  });

  it("默认（无 config.json）→ 网关启动时创建内置任务", async () => {
    const { api, fireStart } = makeApi();
    const cron = makeCron();
    registerHomeProfileScheduler(api);
    await fireStart(cron.service);
    expect(cron.calls.add).toBeGreaterThan(0);
    expect(cron.calls.remove).toBe(0);
  });

  it("scheduler.enabled=false → 清除已存在 managed 任务且不创建", async () => {
    writeFileSync(
      path.join(tmpHome, "config.json"),
      JSON.stringify({ scheduler: { enabled: false } }),
    );
    const { api, fireStart } = makeApi();
    // 预置一个 managed 任务（描述含 [miloco:home-profile] tag）
    const cron = makeCron([
      {
        id: "old-1",
        name: "miloco-home-patrol",
        description: "[miloco:home-profile] miloco-home-patrol",
      },
    ]);
    registerHomeProfileScheduler(api);
    await fireStart(cron.service);
    expect(cron.calls.add).toBe(0);
    expect(cron.calls.remove).toBe(1);
  });
});
