import {
  mkdtempSync,
  readFileSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import type { OpenClawPluginApi } from "openclaw/plugin-sdk";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

/**
 * 构造能跑通 loadSharedConfig 的最小 api stub：
 *  - runtime.config.current() 暴露插件配置入口；
 *  - api.config.gateway 取默认（127.0.0.1:18789）。
 * resolveGatewayUrl 在 gateway 配置缺失时回落到默认，stub 无需显式提供。
 */
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
 * loadSharedConfig 是唯一入口：合并 plugin config / gateway auth 落盘和 schema 默认值补齐一次搞定。
 * 用户 config.json 中字段都是可选的，这里覆盖空文件 / 部分配置等多种输入。
 */
describe("loadSharedConfig", () => {
  let origHome: string | undefined;
  let tmpHome: string;
  let configPath: string;

  beforeEach(() => {
    origHome = process.env.MILOCO_HOME;
    tmpHome = mkdtempSync(path.join(tmpdir(), "miloco-home-"));
    configPath = path.join(tmpHome, "config.json");
    process.env.MILOCO_HOME = tmpHome;
  });

  afterEach(() => {
    if (origHome === undefined) delete process.env.MILOCO_HOME;
    else process.env.MILOCO_HOME = origHome;
    rmSync(tmpHome, { recursive: true, force: true });
  });

  it("config.json 缺失 → 返回值全部使用 schema 默认值", async () => {
    const { loadSharedConfig } = await import("../src/miloco/config.js");
    const api = await makeApi();
    const cfg = loadSharedConfig(api);
    expect(cfg.server.url).toBe("http://127.0.0.1:1810");
    expect(cfg.server.token).toBe("");
    expect(cfg.agent.webhook_url).toBe("http://127.0.0.1:18789/miloco/webhook");
    expect(cfg.agent.auth_bearer).toBe("");
    expect(cfg.model.omni.model).toBe("xiaomi/mimo-v2.5");
    expect(cfg.model.omni.api_key).toBe("");
  });

  it("config.json 只配置部分字段 → 其余走默认，已有字段保留", async () => {
    writeFileSync(
      configPath,
      JSON.stringify({
        debug: false,
        model: { omni: { api_key: "user-key" } },
      }),
    );
    const { loadSharedConfig } = await import("../src/miloco/config.js");
    const api = await makeApi();
    const cfg = loadSharedConfig(api);
    expect(cfg.debug).toBe(false);
    expect(cfg.server.url).toBe("http://127.0.0.1:1810");
    expect(cfg.server.python_bin).toBe("");
    expect(cfg.model.omni.api_key).toBe("user-key");
    expect(cfg.model.omni.model).toBe("xiaomi/mimo-v2.5");
  });

  it("plugin 非空字段覆盖 config.json", async () => {
    writeFileSync(
      configPath,
      JSON.stringify({
        debug: false,
        model: { omni: { api_key: "user-key", model: "old/model" } },
      }),
    );
    const { loadSharedConfig } = await import("../src/miloco/config.js");
    const api = await makeApi({
      debug: true,
      omni_model: "plugin/model",
      omni_api_key: "plugin-key",
    });
    const cfg = loadSharedConfig(api);
    expect(cfg.debug).toBe(true);
    expect(cfg.model.omni.model).toBe("plugin/model");
    expect(cfg.model.omni.api_key).toBe("plugin-key");
  });

  it("plugin 空字符串字段不覆盖 config.json 已有值", async () => {
    writeFileSync(
      configPath,
      JSON.stringify({ model: { omni: { api_key: "existing" } } }),
    );
    const { loadSharedConfig } = await import("../src/miloco/config.js");
    const api = await makeApi({ omni_api_key: "" });
    const cfg = loadSharedConfig(api);
    expect(cfg.model.omni.api_key).toBe("existing");
  });

  it("落盘只写「用户已有 + 本次必须字段」，不包含 schema 默认值", async () => {
    writeFileSync(
      configPath,
      JSON.stringify({ model: { omni: { api_key: "user-key" } } }),
    );
    const { loadSharedConfig } = await import("../src/miloco/config.js");
    const api = await makeApi();
    loadSharedConfig(api);

    const onDisk = JSON.parse(readFileSync(configPath, "utf-8"));
    expect(onDisk).toEqual({
      model: { omni: { api_key: "user-key" } },
      agent: {
        webhook_url: "http://127.0.0.1:18789/miloco/webhook",
        auth_bearer: "",
      },
    });
    // 明确不应写入 schema 默认值
    expect(onDisk.debug).toBeUndefined();
    expect(onDisk.server).toBeUndefined();
    expect(onDisk.model.omni.model).toBeUndefined();
    expect(onDisk.model.omni.base_url).toBeUndefined();
  });

  it("保留用户自定义 agent.webhook_url，不被默认 gateway 覆盖", async () => {
    writeFileSync(
      configPath,
      JSON.stringify({
        agent: { webhook_url: "https://proxy.local/miloco/webhook" },
      }),
    );
    const { loadSharedConfig } = await import("../src/miloco/config.js");
    const api = await makeApi();
    const cfg = loadSharedConfig(api);
    expect(cfg.agent.webhook_url).toBe("https://proxy.local/miloco/webhook");
    const onDisk = JSON.parse(readFileSync(configPath, "utf-8"));
    expect(onDisk.agent.webhook_url).toBe("https://proxy.local/miloco/webhook");
  });

  it("agent.webhook_url 缺失时按当前 gateway URL 回填", async () => {
    writeFileSync(configPath, JSON.stringify({ agent: {} }));
    const { loadSharedConfig } = await import("../src/miloco/config.js");
    const api = await makeApi();
    const cfg = loadSharedConfig(api);
    expect(cfg.agent.webhook_url).toBe("http://127.0.0.1:18789/miloco/webhook");
    const onDisk = JSON.parse(readFileSync(configPath, "utf-8"));
    expect(onDisk.agent.webhook_url).toBe(
      "http://127.0.0.1:18789/miloco/webhook",
    );
  });

  it("内容未变化时不重复写盘（稳态零 IO）", async () => {
    const { loadSharedConfig } = await import("../src/miloco/config.js");
    const api = await makeApi();
    loadSharedConfig(api); // 首次归一化写入
    const mtimeAfterFirst = statSync(configPath).mtimeMs;
    const textAfterFirst = readFileSync(configPath, "utf-8");

    // 再次加载——合并结果与磁盘相同，不应触发写入
    loadSharedConfig(api);
    const mtimeAfterSecond = statSync(configPath).mtimeMs;
    expect(mtimeAfterSecond).toBe(mtimeAfterFirst);
    expect(readFileSync(configPath, "utf-8")).toBe(textAfterFirst);
  });

  it("已有 token 不会被重新生成", async () => {
    writeFileSync(
      configPath,
      JSON.stringify({ server: { token: "preset-token" } }),
    );
    const { loadSharedConfig } = await import("../src/miloco/config.js");
    const api = await makeApi();
    const cfg = loadSharedConfig(api);
    expect(cfg.server.token).toBe("preset-token");
  });
});

describe("isSchedulerAutoManageEnabled", () => {
  let origHome: string | undefined;
  let origEnvOverride: string | undefined;
  let tmpHome: string;
  let configPath: string;

  beforeEach(() => {
    origHome = process.env.MILOCO_HOME;
    origEnvOverride = process.env.MILOCO_SCHEDULER__ENABLED;
    delete process.env.MILOCO_SCHEDULER__ENABLED;
    tmpHome = mkdtempSync(path.join(tmpdir(), "miloco-home-"));
    configPath = path.join(tmpHome, "config.json");
    process.env.MILOCO_HOME = tmpHome;
  });

  afterEach(() => {
    if (origHome === undefined) delete process.env.MILOCO_HOME;
    else process.env.MILOCO_HOME = origHome;
    if (origEnvOverride === undefined)
      delete process.env.MILOCO_SCHEDULER__ENABLED;
    else process.env.MILOCO_SCHEDULER__ENABLED = origEnvOverride;
    rmSync(tmpHome, { recursive: true, force: true });
  });

  it("config.json 缺失 → 默认开启（保持既有自动管理行为）", async () => {
    const { isSchedulerAutoManageEnabled } = await import(
      "../src/miloco/config.js"
    );
    expect(isSchedulerAutoManageEnabled()).toBe(true);
  });

  it("scheduler.enabled=false → 关闭", async () => {
    writeFileSync(configPath, JSON.stringify({ scheduler: { enabled: false } }));
    const { isSchedulerAutoManageEnabled } = await import(
      "../src/miloco/config.js"
    );
    expect(isSchedulerAutoManageEnabled()).toBe(false);
  });

  it("scheduler.enabled=true → 开启", async () => {
    writeFileSync(configPath, JSON.stringify({ scheduler: { enabled: true } }));
    const { isSchedulerAutoManageEnabled } = await import(
      "../src/miloco/config.js"
    );
    expect(isSchedulerAutoManageEnabled()).toBe(true);
  });

  it("非布尔（含缺 scheduler 段）→ 回落默认开启", async () => {
    writeFileSync(configPath, JSON.stringify({ scheduler: { enabled: "no" } }));
    const { isSchedulerAutoManageEnabled } = await import(
      "../src/miloco/config.js"
    );
    expect(isSchedulerAutoManageEnabled()).toBe(true);
  });

  // ── env 覆盖：与后端 pydantic-settings（env > config.json）对齐 ──────────
  // 消除 review 指出的「设了 MILOCO_SCHEDULER__ENABLED 时界面显示与插件实际行为背离」。

  it("MILOCO_SCHEDULER__ENABLED=false 覆盖 config.json 的 true", async () => {
    writeFileSync(configPath, JSON.stringify({ scheduler: { enabled: true } }));
    process.env.MILOCO_SCHEDULER__ENABLED = "false";
    const { isSchedulerAutoManageEnabled } = await import(
      "../src/miloco/config.js"
    );
    expect(isSchedulerAutoManageEnabled()).toBe(false);
  });

  it("MILOCO_SCHEDULER__ENABLED=true 覆盖 config.json 的 false", async () => {
    writeFileSync(configPath, JSON.stringify({ scheduler: { enabled: false } }));
    process.env.MILOCO_SCHEDULER__ENABLED = "true";
    const { isSchedulerAutoManageEnabled } = await import(
      "../src/miloco/config.js"
    );
    expect(isSchedulerAutoManageEnabled()).toBe(true);
  });

  it("env 覆盖认 pydantic 布尔别名（0/off/no，大小写不敏感）", async () => {
    writeFileSync(configPath, JSON.stringify({ scheduler: { enabled: true } }));
    for (const raw of ["0", "off", "No", "FALSE"]) {
      process.env.MILOCO_SCHEDULER__ENABLED = raw;
      const { isSchedulerAutoManageEnabled } = await import(
        "../src/miloco/config.js"
      );
      expect(isSchedulerAutoManageEnabled()).toBe(false);
    }
  });

  it("env 缺失时回落 config.json（不误伤原有读法）", async () => {
    writeFileSync(configPath, JSON.stringify({ scheduler: { enabled: false } }));
    const { isSchedulerAutoManageEnabled } = await import(
      "../src/miloco/config.js"
    );
    expect(isSchedulerAutoManageEnabled()).toBe(false);
  });

  it("env 值无法解析为布尔 → 回落 config.json（不崩、区别于后端抛错）", async () => {
    writeFileSync(configPath, JSON.stringify({ scheduler: { enabled: false } }));
    process.env.MILOCO_SCHEDULER__ENABLED = "maybe";
    const { isSchedulerAutoManageEnabled } = await import(
      "../src/miloco/config.js"
    );
    expect(isSchedulerAutoManageEnabled()).toBe(false);
  });
});

describe("getNotifyDedupWindowMs", () => {
  let origHome: string | undefined;
  let origEnvOverride: string | undefined;
  let tmpHome: string;
  let configPath: string;

  beforeEach(() => {
    origHome = process.env.MILOCO_HOME;
    origEnvOverride = process.env.MILOCO_NOTIFY__DEDUP_WINDOW_SEC;
    delete process.env.MILOCO_NOTIFY__DEDUP_WINDOW_SEC;
    tmpHome = mkdtempSync(path.join(tmpdir(), "miloco-home-"));
    configPath = path.join(tmpHome, "config.json");
    process.env.MILOCO_HOME = tmpHome;
  });

  afterEach(() => {
    if (origHome === undefined) delete process.env.MILOCO_HOME;
    else process.env.MILOCO_HOME = origHome;
    if (origEnvOverride === undefined)
      delete process.env.MILOCO_NOTIFY__DEDUP_WINDOW_SEC;
    else process.env.MILOCO_NOTIFY__DEDUP_WINDOW_SEC = origEnvOverride;
    rmSync(tmpHome, { recursive: true, force: true });
  });

  it("config.json 缺失 → 默认 60s（60000ms）", async () => {
    const { getNotifyDedupWindowMs } = await import("../src/miloco/config.js");
    expect(getNotifyDedupWindowMs()).toBe(60_000);
  });

  it("读 config.json 的 notify.dedup_window_sec", async () => {
    writeFileSync(
      configPath,
      JSON.stringify({ notify: { dedup_window_sec: 30 } }),
    );
    const { getNotifyDedupWindowMs } = await import("../src/miloco/config.js");
    expect(getNotifyDedupWindowMs()).toBe(30_000);
  });

  it("负值经 Math.max(0,…) 归零 = 关闭去重", async () => {
    writeFileSync(
      configPath,
      JSON.stringify({ notify: { dedup_window_sec: -5 } }),
    );
    const { getNotifyDedupWindowMs } = await import("../src/miloco/config.js");
    expect(getNotifyDedupWindowMs()).toBe(0);
  });

  it("MILOCO_NOTIFY__DEDUP_WINDOW_SEC 覆盖 config.json（对齐后端 env 优先）", async () => {
    writeFileSync(
      configPath,
      JSON.stringify({ notify: { dedup_window_sec: 30 } }),
    );
    process.env.MILOCO_NOTIFY__DEDUP_WINDOW_SEC = "90";
    const { getNotifyDedupWindowMs } = await import("../src/miloco/config.js");
    expect(getNotifyDedupWindowMs()).toBe(90_000);
  });

  it("env 非数 → 回落 config.json（不崩）", async () => {
    writeFileSync(
      configPath,
      JSON.stringify({ notify: { dedup_window_sec: 30 } }),
    );
    process.env.MILOCO_NOTIFY__DEDUP_WINDOW_SEC = "abc";
    const { getNotifyDedupWindowMs } = await import("../src/miloco/config.js");
    expect(getNotifyDedupWindowMs()).toBe(30_000);
  });
});
