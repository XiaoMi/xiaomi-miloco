import type { FromSchema } from "json-schema-to-ts";
import type { OpenClawPluginApi } from "openclaw/plugin-sdk";
import { resolveGatewayAuth } from "openclaw/plugin-sdk/gateway-runtime";
import { getPluginConfig, type MilocoPluginConfig } from "../config.js";
import { resolveGatewayUrl } from "../utils/gateway.js";
import { readTextFileSync, writeTextFileSync } from "../utils/io.js";
import { createParser } from "../utils/schema.js";
import { milocoConfigFile } from "./paths.js";

/**
 * 与 backend/miloco/src/miloco/config/settings.schema.json 对齐的 miloco 用户配置契约。
 */
const SHARED_CONFIG_SCHEMA = {
  title: "MilocoSharedConfig",
  type: "object",
  additionalProperties: true,
  properties: {
    /** 是否启用调试模式：为 true 时 CLI / backend / openclaw 插件都会输出更详细的日志 */
    debug: {
      type: "boolean",
      default: false,
      description:
        "是否启用调试模式：为 true 时 CLI / backend / openclaw 插件都会输出更详细的日志",
    },
    /** miloco 后端服务相关配置（HTTP 访问、token、启动用 Python 解释器） */
    server: {
      type: "object",
      default: {},
      additionalProperties: true,
      properties: {
        url: {
          type: "string",
          default: "http://127.0.0.1:1810",
          description:
            "CLI 与插件访问 miloco 后端的 HTTP Base URL（永远 HTTP；跨网加密走反代）",
        },
        token: {
          type: "string",
          default: "",
          description:
            "CLI 与插件访问后端时使用的 Bearer Token；为空时由后端首次启动自动生成",
        },
        tls_verify: {
          type: "boolean",
          default: false,
          description:
            "CLI 访问后端时是否校验 TLS 证书；当前 backend 永远 HTTP 故无作用，保留供未来反代场景",
        },
        python_bin: {
          type: "string",
          default: "",
          description:
            "用于启动 miloco-backend 的 Python 解释器绝对路径（install.sh 探测后写入）",
        },
        tls_certfile: {
          type: "string",
          default: "",
          deprecated: true,
          description:
            "【已废弃】backend 永远 HTTP，跨网加密走反代+真证书；写了不生效，仅启动 warning",
        },
        tls_keyfile: {
          type: "string",
          default: "",
          deprecated: true,
          description: "【已废弃】见 tls_certfile",
        },
      },
      required: ["url", "token", "tls_verify", "python_bin"],
    },
    /** agent webhook 出站调用配置（webhook 地址 + 鉴权凭据） */
    agent: {
      type: "object",
      default: {},
      additionalProperties: true,
      properties: {
        webhook_url: {
          type: "string",
          default: "http://127.0.0.1:18789/miloco/webhook",
          description: "agent webhook 回调地址",
        },
        auth_bearer: {
          type: "string",
          default: "",
          description:
            "agent webhook 鉴权 Bearer 值；为空时不发送 Authorization 头",
        },
      },
      required: ["webhook_url", "auth_bearer"],
    },
    /** miloco 使用的第三方多模态模型配置 */
    model: {
      type: "object",
      default: {},
      additionalProperties: true,
      properties: {
        omni: {
          type: "object",
          default: {},
          additionalProperties: true,
          properties: {
            model: {
              type: "string",
              default: "xiaomi/mimo-v2.5",
              description: "多模态模型标识（provider/model）",
            },
            base_url: {
              type: "string",
              default: "https://api.xiaomimimo.com/v1",
              description:
                "多模态模型服务 Base URL（需兼容 OpenAI-compatible 协议）",
            },
            api_key: {
              type: "string",
              default: "",
              description:
                "多模态模型 API Key；为空时视为未配置，插件与后端启动前校验",
            },
          },
          required: ["model", "base_url", "api_key"],
        },
      },
      required: ["omni"],
    },
    /** 内置定时任务自动管理开关（与 settings.schema.json 的 scheduler 对齐） */
    scheduler: {
      type: "object",
      default: {},
      additionalProperties: true,
      properties: {
        enabled: {
          type: "boolean",
          default: true,
          description:
            "是否由 miloco 自动管理内置定时任务；关闭后网关启动时清除自动任务且不再重建",
        },
      },
    },
    /** 通知发送运行参数（与 settings.schema.json 的 notify 对齐） */
    notify: {
      type: "object",
      default: {},
      additionalProperties: true,
      properties: {
        dedup_window_sec: {
          type: "number",
          default: 60,
          description: "相同通知文案在此窗口（秒）内只发一次；<=0 = 关闭去重",
        },
      },
    },
  },
  required: ["debug", "server", "agent", "model"],
} as const;

export type MilocoSharedConfig = FromSchema<typeof SHARED_CONFIG_SCHEMA>;

const { parse: parseSharedConfig } = createParser(SHARED_CONFIG_SCHEMA);

const isRecord = (v: unknown): v is Record<string, unknown> =>
  typeof v === "object" && v !== null && !Array.isArray(v);

function sharedConfigPath(): string {
  return milocoConfigFile();
}

/**
 * 把「当前 plugin 配置 + gateway 凭据」合并进磁盘上的
 * ``~/.openclaw/miloco/config.json``，仅写入「用户已有 + 本次必须落盘 + 兜底」
 * 的字段（不污染 schema 默认值），然后返回经 schema 补齐的完整配置。
 */
export function loadSharedConfig(api: OpenClawPluginApi): MilocoSharedConfig {
  const plugin = getPluginConfig(api);
  const filePath = sharedConfigPath();

  const existingText = readTextOrUndefined(filePath);
  const existing = safeJsonParse(existingText);
  const raw: Record<string, unknown> = isRecord(existing)
    ? { ...existing }
    : {};

  mergePluginIntoRaw(raw, plugin);
  ensureAgentEssentials(raw, api);

  // 仅在合并后的内容与磁盘不同才落盘，避免每次 load 都产生冗余 IO / mtime 抖动。
  // 首次启动（文件缺失）或人工手改过格式时会执行一次归一化写入，之后稳态零写入。
  const serialized = `${JSON.stringify(raw, null, 2)}\n`;
  if (serialized !== existingText) {
    writeTextFileSync(filePath, serialized);
  }
  return parseSharedConfig(raw);
}

/**
 * 把 plugin 侧 ``debug`` / ``omni_*`` 合并进 raw：
 *   - ``debug``：``undefined`` 视为未设置，其它（含 ``false``）覆盖；
 *   - ``omni_*``：空字符串视为未设置，保留现有值；其它覆盖。
 */
function mergePluginIntoRaw(
  raw: Record<string, unknown>,
  plugin: MilocoPluginConfig,
): void {
  if (plugin.debug !== undefined) raw.debug = plugin.debug;

  if (plugin.omni_model || plugin.omni_base_url || plugin.omni_api_key) {
    const model = isRecord(raw.model) ? { ...raw.model } : {};
    const omni = isRecord(model.omni) ? { ...model.omni } : {};
    if (plugin.omni_model) omni.model = plugin.omni_model;
    if (plugin.omni_base_url) omni.base_url = plugin.omni_base_url;
    if (plugin.omni_api_key) omni.api_key = plugin.omni_api_key;
    model.omni = omni;
    raw.model = model;
  }
}

function ensureAgentEssentials(
  raw: Record<string, unknown>,
  api: OpenClawPluginApi,
): void {
  const agent = isRecord(raw.agent) ? { ...raw.agent } : {};

  if (typeof agent.webhook_url !== "string" || agent.webhook_url.length === 0) {
    agent.webhook_url = `${resolveGatewayUrl(api)}/miloco/webhook`;
  }

  // resolveGatewayAuth 方法依赖 openclaw >= v2026.4.27-beta.1
  // https://github.com/openclaw/openclaw/commit/af7f651db36f9b5c827713035ab14a80803dd9a8
  const authConfig = api.config.gateway?.auth ?? undefined;
  const resolved = resolveGatewayAuth({ authConfig, env: process.env });
  const bearer =
    resolved.mode === "token"
      ? resolved.token
      : resolved.mode === "password"
        ? resolved.password
        : undefined;
  agent.auth_bearer = bearer ?? "";

  raw.agent = agent;
}

type DeepPartial<T> = T extends object
  ? { [K in keyof T]?: DeepPartial<T[K]> }
  : T;

/**
 * 读取磁盘上的共享配置，将传入的 partial config 深度合并后写回，
 * 返回经 schema 补齐的完整配置。
 */
export function updateSharedConfig(
  partial: DeepPartial<MilocoSharedConfig>,
): MilocoSharedConfig {
  const filePath = sharedConfigPath();

  const existingText = readTextOrUndefined(filePath);
  const existing = safeJsonParse(existingText);
  const raw: Record<string, unknown> = isRecord(existing)
    ? { ...existing }
    : {};

  deepMerge(raw, partial);

  const serialized = `${JSON.stringify(raw, null, 2)}\n`;
  if (serialized !== existingText) {
    writeTextFileSync(filePath, serialized);
  }
  return parseSharedConfig(raw);
}

function deepMerge(
  target: Record<string, unknown>,
  source: Record<string, unknown>,
): void {
  for (const key of Object.keys(source)) {
    const srcVal = source[key];
    const tgtVal = target[key];
    if (isRecord(srcVal) && isRecord(tgtVal)) {
      const merged = { ...tgtVal };
      deepMerge(merged, srcVal);
      target[key] = merged;
    } else {
      target[key] = srcVal;
    }
  }
}

function readTextOrUndefined(filePath: string): string | undefined {
  try {
    return readTextFileSync(filePath);
  } catch {
    return undefined;
  }
}

function safeJsonParse(text: string | undefined): unknown {
  if (!text) return undefined;
  try {
    return JSON.parse(text);
  } catch {
    return undefined;
  }
}

// ─── 环境变量覆盖（与后端 pydantic-settings 对齐） ────────────────────────────
//
// 后端 `MilocoSettings` 用 pydantic-settings 以「环境变量 > config.json >
// settings.yaml > 默认值」的优先级解析配置（见 backend settings.py 的
// `SettingsConfigDict(env_prefix="MILOCO_", env_nested_delimiter="__",
// case_sensitive=False)`）。backend GET /admin/scheduler-config 与 Web 开关读
// `get_settings()`，因此 env 会覆盖界面显示值。
//
// 下面这批轻量读取器（scheduler / notify）此前直接 raw 读 config.json、无视 env，
// 导致「设了 MILOCO_SCHEDULER__ENABLED 时界面状态与插件实际行为背离」。这里补齐
// 同一套 env 覆盖语义，让插件生效值与 backend / Web 显示值一致。

/** 环境变量前缀，与后端 `SettingsConfigDict.env_prefix` 对齐。 */
const ENV_PREFIX = "MILOCO_";
/** 嵌套字段分隔符，与后端 `SettingsConfigDict.env_nested_delimiter` 对齐。 */
const ENV_NESTED_DELIMITER = "__";

/** pydantic v2 认可为 `true` 的布尔字符串（大小写不敏感）。 */
const ENV_TRUE_TOKENS = new Set(["1", "true", "t", "yes", "y", "on"]);
/** pydantic v2 认可为 `false` 的布尔字符串（大小写不敏感）。 */
const ENV_FALSE_TOKENS = new Set(["0", "false", "f", "no", "n", "off"]);

/**
 * 读取「与某配置路径对应的环境变量」原始字符串。
 * 路径 `["scheduler", "enabled"]` → `MILOCO_SCHEDULER__ENABLED`。
 * 大小写不敏感匹配（对齐后端 `case_sensitive=False`；POSIX env 名本身区分大小写，
 * 逐一 lower-case 比较以兼容用户误用小写）。
 */
function readEnvOverride(path: readonly string[]): string | undefined {
  const target = `${ENV_PREFIX}${path.join(ENV_NESTED_DELIMITER)}`.toLowerCase();
  for (const [key, value] of Object.entries(process.env)) {
    if (value !== undefined && key.toLowerCase() === target) return value;
  }
  return undefined;
}

/** 按 pydantic bool 规则解析环境变量字符串；无法识别返回 `undefined`。 */
function parseEnvBool(raw: string): boolean | undefined {
  const t = raw.trim().toLowerCase();
  if (ENV_TRUE_TOKENS.has(t)) return true;
  if (ENV_FALSE_TOKENS.has(t)) return false;
  return undefined;
}

/** 按 pydantic float 规则解析环境变量字符串；非数返回 `undefined`。 */
function parseEnvNumber(raw: string): number | undefined {
  const t = raw.trim();
  if (t === "") return undefined;
  const n = Number(t);
  return Number.isFinite(n) ? n : undefined;
}

/**
 * 统一解析「单个 miloco 标量配置项」的生效值，语义与后端 pydantic-settings 对齐：
 *
 *   环境变量 `MILOCO_<A>__<B>` > `config.json` 的 `A.B` > 调用方缺省 `fallback`。
 *
 * - `path`：`[section, field]` 两级路径（当前消费方都是两级，够用且更直白）；
 * - `coerceEnv`：把 env 原始字符串按目标类型解析，无法解析（含空值）返回 `undefined`；
 * - `coerceFile`：从 config.json 已解析的原始值取目标类型，类型不符返回 `undefined`。
 *
 * 与后端的一个刻意差异：env 值非法时后端会抛 ValidationError 直接崩，
 * 而插件回落到文件 / 缺省——避免误配 env 阻断网关启停这类关键路径。
 *
 * 刻意不走 {@link loadSharedConfig}：这些读取器只需一个标量，
 * 无需归一化落盘 / 解析 gateway auth，且会被高频调用（如每条通知）。
 */
function resolveScalarConfig<T>(
  path: readonly [string, string],
  coerceEnv: (raw: string) => T | undefined,
  coerceFile: (value: unknown) => T | undefined,
  fallback: T,
): T {
  const envRaw = readEnvOverride(path);
  if (envRaw !== undefined) {
    const parsed = coerceEnv(envRaw);
    if (parsed !== undefined) return parsed;
  }
  const existing = safeJsonParse(readTextOrUndefined(sharedConfigPath()));
  const raw = isRecord(existing) ? existing : {};
  const [section, field] = path;
  const obj = isRecord(raw[section]) ? raw[section] : undefined;
  const fileVal = obj !== undefined ? coerceFile(obj[field]) : undefined;
  return fileVal !== undefined ? fileVal : fallback;
}

/** 通知去重窗口默认值（秒），与 settings.schema.json / 后端 NotifySettings 对齐。 */
const DEFAULT_NOTIFY_DEDUP_SEC = 60;

/**
 * 无副作用读取通知去重窗口（毫秒）。读 `notify.dedup_window_sec`（秒，与后端
 * `MilocoSettings.notify` 同键），环境变量 `MILOCO_NOTIFY__DEDUP_WINDOW_SEC`
 * 优先（对齐后端）。非数（含缺失）按缺省 60；负值经 `Math.max(0, …)` 归零 =
 * 关闭去重，与后端 `MessageDeduper` 的 `window_sec<=0` 同义。返回毫秒。
 */
export function getNotifyDedupWindowMs(): number {
  const sec = resolveScalarConfig(
    ["notify", "dedup_window_sec"],
    parseEnvNumber,
    (v) => (typeof v === "number" && Number.isFinite(v) ? v : undefined),
    DEFAULT_NOTIFY_DEDUP_SEC,
  );
  return Math.max(0, sec) * 1000;
}

/**
 * 无副作用读取「是否自动管理内置定时任务」开关。读 `scheduler.enabled`（与后端
 * `SchedulerSettings` / CLI `scheduler.enabled` 同键），环境变量
 * `MILOCO_SCHEDULER__ENABLED` 优先（对齐后端 GET / Web 开关的 `get_settings()` 读法，
 * 消除「设了 env 时界面显示与插件实际行为背离」）。缺失或非布尔一律按缺省 `true`
 * （保持既有默认自动管理行为）。
 */
export function isSchedulerAutoManageEnabled(): boolean {
  return resolveScalarConfig(
    ["scheduler", "enabled"],
    parseEnvBool,
    (v) => (typeof v === "boolean" ? v : undefined),
    true,
  );
}
