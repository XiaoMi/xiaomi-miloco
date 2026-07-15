import { beforeEach, describe, expect, it, vi } from "vitest";

function makeApi(config: Record<string, unknown>) {
  return {
    config,
    runtime: {
      config: {
        current: () => config,
        mutateConfigFile: vi.fn(async ({ mutate }) => {
          mutate(config);
        }),
      },
    },
  } as any;
}

describe("plugin config notify session compatibility", () => {
  beforeEach(() => {
    vi.resetModules();
  });

  it("兼容读取旧单值 notifySessionKey", async () => {
    const { getPluginConfig, kPluginId } = await import("../src/config.js");
    const api = makeApi({
      plugins: {
        entries: {
          [kPluginId]: {
            config: {
              notifySessionKey: "wechat:legacy",
            },
          },
        },
      },
    });

    const result = getPluginConfig(api);
    expect(result.notifySessionKeys).toEqual(["wechat:legacy"]);
  });

  it("写入 notifySessionKeys 时会清空旧 notifySessionKey，避免解绑后旧值复活", async () => {
    const { getPluginConfig, kPluginId, setPluginConfig } = await import(
      "../src/config.js"
    );
    const config = {
      plugins: {
        entries: {
          [kPluginId]: {
            config: {
              notifySessionKey: "wechat:legacy",
            },
          },
        },
      },
    } as Record<string, any>;
    const api = makeApi(config);

    await setPluginConfig(api, { notifySessionKeys: ["telegram:new"] });

    expect(config.plugins.entries[kPluginId].config.notifySessionKey).toBe("");
    expect(config.plugins.entries[kPluginId].config.notifySessionKeys).toEqual([
      "telegram:new",
    ]);
    expect(getPluginConfig(api).notifySessionKeys).toEqual(["telegram:new"]);
  });
});
