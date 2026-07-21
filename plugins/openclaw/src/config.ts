import type { FromSchema } from "json-schema-to-ts";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import type {
  OpenClawConfig,
  OpenClawPluginApi,
  OpenClawPluginConfigSchema,
} from "openclaw/plugin-sdk";
import pluginJson from "../openclaw.plugin.json" with { type: "json" };
import { createParser } from "./utils/schema.js";

export const kPluginRootDir = join(
  dirname(fileURLToPath(import.meta.url)),

  "..",
);

export const kPluginJSON = pluginJson;
export const kPluginId = pluginJson.id;
export const kPluginName = pluginJson.name;
export const kPluginDescription = pluginJson.description;

const MILOCO_PLUGIN_CONFIG_SCHEMA = {
  jsonSchema: {
    title: "MilocoPluginConfig",
    type: "object",
    additionalProperties: true,
    properties: {
      /** 调试模式：覆盖 config.json 顶层 debug */
      debug: {
        type: "boolean",
        description: "调试模式：覆盖 config.json 顶层 debug",
      },
      /** 多模态模型标识（provider/model）：覆盖 config.json model.omni.model */
      omni_model: {
        type: "string",
        description:
          "多模态模型标识（provider/model）：覆盖 config.json model.omni.model",
      },
      /** 多模态模型服务 Base URL：覆盖 config.json model.omni.base_url */
      omni_base_url: {
        type: "string",
        description:
          "多模态模型服务 Base URL：覆盖 config.json model.omni.base_url",
      },
      /** 多模态模型 API Key：覆盖 config.json model.omni.api_key */
      omni_api_key: {
        type: "string",
        description: "多模态模型 API Key：覆盖 config.json model.omni.api_key",
      },
      /** 通知目标 sessionKey 列表：指定接收通知的 IM channel */
      notifySessionKeys: {
        type: "array",
        items: { type: "string" },
        description:
          "通知目标 sessionKey 列表：指定接收 miloco 通知的 IM channel sessions",
      },
      /** 兼容旧配置：单个通知目标 sessionKey */
      notifySessionKey: {
        type: "string",
        description:
          "【兼容旧配置】单个通知目标 sessionKey：指定接收 miloco 通知的 IM channel session",
      },
    },
  },
  uiHints: {
    debug: {
      label: "调试模式",
      help: "打开调试模式，方便排查定位问题。",
    },
    omni_model: {
      label: "多模态模型",
      help: "留空则使用 ~/.openclaw/miloco/config.json model.omni.model。",
    },
    omni_base_url: {
      label: "多模态模型 Base URL",
      help: "留空则使用 ~/.openclaw/miloco/config.json model.omni.base_url。",
    },
    omni_api_key: {
      label: "多模态模型 API Key",
      help: "留空则使用 ~/.openclaw/miloco/config.json model.omni.api_key。",
    },
    notifySessionKeys: {
      label: "通知目标 Channels",
      help: "指定接收 miloco 通知的 IM channel session keys。留空则自动选择最近活跃的 channel。",
    },
    notifySessionKey: {
      label: "通知目标 Channel（旧）",
      help: "兼容旧配置的单个 IM channel session key；读取时会自动并入新列表配置。",
    },
  },
} as const satisfies OpenClawPluginConfigSchema;

export type MilocoPluginConfig = FromSchema<
  typeof MILOCO_PLUGIN_CONFIG_SCHEMA.jsonSchema
>;

export const MilocoPluginConfigSchema = {
  ...createParser(MILOCO_PLUGIN_CONFIG_SCHEMA.jsonSchema),
  ...MILOCO_PLUGIN_CONFIG_SCHEMA,
};

const resolvePluginConfig = (config: unknown): MilocoPluginConfig => {
  return MilocoPluginConfigSchema.parse(config ?? {});
};

let kPluginConfig: MilocoPluginConfig | undefined;

export function getRuntimeConfig(api: OpenClawPluginApi): OpenClawConfig {
  // current 方法依赖 openclaw >= v2026.4.26-beta.1
  // https://github.com/openclaw/openclaw/commit/7f3f108521f45ba14b65b2ffe507b5ee88979671
  return (api.runtime.config?.current?.() ?? api.config) as OpenClawConfig;
}

export function getPluginConfig(api: OpenClawPluginApi) {
  if (kPluginConfig) return kPluginConfig;
  const cfg = getRuntimeConfig(api);
  const plugin = cfg.plugins?.entries?.[kPluginId];
  const parsed = resolvePluginConfig(plugin?.config);
  const legacyKey =
    typeof parsed.notifySessionKey === "string" &&
    parsed.notifySessionKey.trim().length > 0
      ? parsed.notifySessionKey.trim()
      : undefined;
  const mergedKeys = Array.isArray(parsed.notifySessionKeys)
    ? parsed.notifySessionKeys
        .map((v) => (typeof v === "string" ? v.trim() : ""))
        .filter((v, idx, arr) => v.length > 0 && arr.indexOf(v) === idx)
    : [];
  if (legacyKey && !mergedKeys.includes(legacyKey)) {
    mergedKeys.unshift(legacyKey);
  }
  return { ...parsed, notifySessionKeys: mergedKeys };
}

/**
 * 更新插件配置（默认为浅合并）
 */
export async function setPluginConfig(
  api: OpenClawPluginApi,
  config: Partial<MilocoPluginConfig>,
) {
  const cfg = getRuntimeConfig(api);
  const plugin = cfg.plugins?.entries?.[kPluginId];
  if (!plugin) {
    return; // 插件没有正常安装
  }
  const oldConfig = getPluginConfig(api);
  const normalizedConfig =
    "notifySessionKeys" in config && !("notifySessionKey" in config)
      ? { ...config, notifySessionKey: "" }
      : config;
  const newConfig = { ...oldConfig, ...normalizedConfig };
  kPluginConfig = newConfig;

  // mutateConfigFile 方法依赖 openclaw >= v2026.4.26-beta.1
  // https://github.com/openclaw/openclaw/commit/4336a7f3a9c6f44fa2d9310fc2eabd1ba3b21c5f
  await api.runtime.config.mutateConfigFile({
    afterWrite: { mode: "none", reason: "plugin config update" },
    mutate(draft) {
      if (!draft.plugins?.entries?.[kPluginId]) return;
      draft.plugins.entries[kPluginId] = {
        ...draft.plugins.entries[kPluginId],
        config: newConfig,
      };
    },
  });
}
