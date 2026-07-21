import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import type { OpenClawPluginApi } from "openclaw/plugin-sdk";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

const FIXTURE = path.resolve(
  __dirname,
  "../../..",
  "backend",
  "miloco",
  "tests",
  "fixtures",
  "config.sample.json",
);

async function makeApi(
  pluginConfig: Record<string, unknown> = {},
): Promise<OpenClawPluginApi> {
  const { kPluginId } = await import("../src/config.js");
  return {
    runtime: {
      config: {
        current: () => ({
          plugins: { entries: { [kPluginId]: { config: pluginConfig } } },
        }),
      },
    },
    config: {},
  } as unknown as OpenClawPluginApi;
}

/**
 * 与 cli/tests/test_cross_end_alignment.py、backend/miloco/tests/test_cross_end_alignment.py
 * 配对：同一份 fixture 在三端加载后字段语义完全一致。
 *
 * 这里用空 plugin 配置调用 loadSharedConfig(api)，确保插件侧不覆盖 fixture；
 * fixture 的 agent.webhook_url 与默认 gateway URL 一致，auth_bearer 已预置，因此
 * ensureAgentEssentials 的写入结果与 fixture 相同。
 */
describe("cross-end alignment", () => {
  let origHome: string | undefined;
  let origGatewayToken: string | undefined;
  let origSchedulerEnv: string | undefined;
  let origNotifyEnv: string | undefined;
  let tmpHome: string;

  beforeEach(() => {
    origHome = process.env.MILOCO_HOME;
    origGatewayToken = process.env.OPENCLAW_GATEWAY_TOKEN;
    // 插件的 scheduler / notify 读取器尊重 env 覆盖（对齐后端）；跨端字段对齐要读的是
    // fixture 本身，故清掉可能污染的 env（与 Python 两端「清空 MILOCO_* 后再跑」同理）。
    origSchedulerEnv = process.env.MILOCO_SCHEDULER__ENABLED;
    origNotifyEnv = process.env.MILOCO_NOTIFY__DEDUP_WINDOW_SEC;
    delete process.env.MILOCO_SCHEDULER__ENABLED;
    delete process.env.MILOCO_NOTIFY__DEDUP_WINDOW_SEC;
    tmpHome = mkdtempSync(path.join(tmpdir(), "miloco-home-"));
    writeFileSync(
      path.join(tmpHome, "config.json"),
      readFileSync(FIXTURE, "utf-8"),
    );
    process.env.MILOCO_HOME = tmpHome;
    // ensureAgentEssentials resolves auth from env — match the fixture value
    process.env.OPENCLAW_GATEWAY_TOKEN = "fixture-gateway-token";
  });

  afterEach(() => {
    if (origHome === undefined) delete process.env.MILOCO_HOME;
    else process.env.MILOCO_HOME = origHome;
    if (origGatewayToken === undefined)
      delete process.env.OPENCLAW_GATEWAY_TOKEN;
    else process.env.OPENCLAW_GATEWAY_TOKEN = origGatewayToken;
    if (origSchedulerEnv === undefined)
      delete process.env.MILOCO_SCHEDULER__ENABLED;
    else process.env.MILOCO_SCHEDULER__ENABLED = origSchedulerEnv;
    if (origNotifyEnv === undefined)
      delete process.env.MILOCO_NOTIFY__DEDUP_WINDOW_SEC;
    else process.env.MILOCO_NOTIFY__DEDUP_WINDOW_SEC = origNotifyEnv;
    rmSync(tmpHome, { recursive: true, force: true });
  });

  it("loadSharedConfig 加载 fixture 后字段与 Python 侧一致", async () => {
    const { loadSharedConfig } = await import("../src/miloco/config.js");
    const expected = JSON.parse(readFileSync(FIXTURE, "utf-8"));
    const api = await makeApi();
    const cfg = loadSharedConfig(api);

    expect(cfg.debug).toBe(expected.debug);
    expect(cfg.server.url).toBe(expected.server.url);
    expect(cfg.server.token).toBe(expected.server.token);
    expect(cfg.server.tls_verify).toBe(expected.server.tls_verify);
    expect(cfg.server.python_bin).toBe(expected.server.python_bin);
    expect(cfg.agent.webhook_url).toBe(expected.agent.webhook_url);
    expect(cfg.agent.auth_bearer).toBe(expected.agent.auth_bearer);
    expect(cfg.model.omni.model).toBe(expected.model.omni.model);
    expect(cfg.model.omni.base_url).toBe(expected.model.omni.base_url);
    expect(cfg.model.omni.api_key).toBe(expected.model.omni.api_key);
    expect(cfg.scheduler?.enabled).toBe(expected.scheduler.enabled);
    expect(cfg.notify?.dedup_window_sec).toBe(expected.notify.dedup_window_sec);
  });

  it("scheduler / notify 读取器读 fixture 的值与 backend 写出的形状一致", async () => {
    // 用生产实际消费方（而非 loadSharedConfig 的 schema 补齐）验证键名/形状契约：
    // 若任一端把 scheduler 写成扁平键或改了形状，这两个读取器会回落缺省而与 fixture 背离。
    const { isSchedulerAutoManageEnabled, getNotifyDedupWindowMs } =
      await import("../src/miloco/config.js");
    const expected = JSON.parse(readFileSync(FIXTURE, "utf-8"));

    expect(isSchedulerAutoManageEnabled()).toBe(expected.scheduler.enabled);
    expect(getNotifyDedupWindowMs()).toBe(
      expected.notify.dedup_window_sec * 1000,
    );
  });
});
