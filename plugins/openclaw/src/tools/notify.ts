import type { OpenClawPluginApi } from "openclaw/plugin-sdk/core";
import {
  jsonResult,
  type OpenClawPluginToolFactory,
} from "openclaw/plugin-sdk/core";
import { Type } from "typebox";
import {
  getPluginConfig,
  getRuntimeConfig,
  setPluginConfig,
} from "../config.js";
import { getNotifyDedupWindowMs } from "../miloco/config.js";
import { logger } from "../utils/logger.js";

type NotifyTarget = {
  channel: string;
  to?: string;
  accountId?: string;
  threadId?: string | number;
  sessionKey: string;
};

type BoundSessionInfo = NotifyTarget;

type PluginNotifyConfig = ReturnType<typeof getPluginConfig>;

export type BindReason = "not_configured" | "configured_but_invalid";

export type ResolveResult = {
  target: NotifyTarget | null;
  targets: NotifyTarget[];
  needsBind: boolean;
  bindReason?: BindReason;
  invalidSessionKeys?: string[];
};

export type NotifyResult = {
  ok: boolean;
  error?: string;
  channel?: string;
  channels?: string[];
  deliveredChannels?: string[];
  failedChannels?: string[];
  partialSuccess?: boolean;
  needsBind?: boolean;
  bindReason?: BindReason;
  fallbackChannel?: string;
  fallback?: boolean;
  nextAction?: string;
  bindHintExample?: string;
  deduped?: boolean;
};

type DeliverAttempt = {
  sessionKey: string;
  channel: string;
  ok: boolean;
  deduped?: boolean;
  error?: string;
};

const recentSends = new Map<string, number>();

// 与 miloco-notify skill references/channel-config.md 的「bindHint 模板」表保持一致；修改任一处需同步另一处。
// 返回给 agent 作为可直接翻译成主人语言的 bindHint 范例（兜底：agent 未加载 skill 时仍能照做）。
const BIND_HINT_EXAMPLE: Record<BindReason, string> = {
  not_configured:
    "您尚未设置 Miloco 通知频道，本条消息已临时发送到最近活跃的对话。回复「绑定通知频道」可将当前对话加入固定的 Miloco 通知频道列表，后续提醒、定时任务、告警等通知都会发送到所有已绑定通道。",
  configured_but_invalid:
    "您原先绑定的 Miloco 通知频道已全部失效，本条消息已临时发送到最近活跃的对话。请回复「绑定通知频道」重新加入有效通道。",
};

const PROMPT_EXAMPLE_BODY = "客厅的灯已经为您打开。";
const PROMPT_EXAMPLE_HINT = BIND_HINT_EXAMPLE.not_configured;

export function registerNotifyTool(api: OpenClawPluginApi) {
  const factory: OpenClawPluginToolFactory = (_ctx) => ({
    name: "miloco_im_push",
    label: "Send notification",
    description:
      "给主人推送一条 IM 通知。通常只传 message 调用即可。\n" +
      "本工具需配合 miloco-notify skill 使用（分级、选人、文案规范都在其中）。\n" +
      "本工具是 agent 内置工具（function call），直接调用即可——不是 miloco-cli 命令，切勿用 shell / miloco-cli 去执行。\n" +
      "重要：若返回 ok=false 且 needsBind=true，表示本条【尚未发出】——这是要你继续操作的信号，绝不能把它当作结果回复/转述给用户。你必须立刻再次调用本工具：message 保持不变，并补上 bindHint（把返回里的 bindHintExample 翻译成主人当前使用的语言）。补上 bindHint 后通知才会真正发送。\n" +
      "失败（ok=false 且非 needsBind）时不要用同一条 message 反复重推，会造成死循环；按 miloco-notify skill 规则降级或结束本轮。",
    parameters: Type.Object({
      message: Type.String({ description: "要发给主人的通知正文" }),
      bindHint: Type.Optional(
        Type.String({
          description:
            "仅当上次调用返回 needsBind=true 时才传：按 miloco-notify skill 的 bindHint 模板、用主人的语言写好的绑定引导语。工具会把它附在正文后一起发出；渠道已设置时无需传。",
        }),
      ),
    }),
    async execute(_toolCallId, params) {
      const { message, bindHint } = params as {
        message: string;
        bindHint?: string;
      };
      const result = await notifyOwner(api, message, { bindHint });
      return jsonResult(result);
    },
  });

  api.registerTool(factory, { name: "miloco_im_push" });

  const bindFactory: OpenClawPluginToolFactory = (ctx) => ({
    name: "miloco_notify_bind",
    label: "Bind notify channel",
    description: "绑定通知渠道。默认当前对话，也可指定 sessionKey。",
    parameters: Type.Object({
      sessionKey: Type.Optional(
        Type.String({ description: "目标 session key，留空则使用当前对话" }),
      ),
    }),
    async execute(_toolCallId, params) {
      const { sessionKey: inputKey } = params as { sessionKey?: string };
      const sessionKey = (inputKey || ctx.sessionKey || "").trim();
      if (!sessionKey) {
        return jsonResult({
          ok: false,
          error: "未指定 sessionKey 且当前上下文无 sessionKey",
        });
      }
      const store = listSessionStore(api);
      const resolve = resolveSessionByKey(store, sessionKey);
      if (!resolve) {
        return jsonResult({
          ok: false,
          error:
            "当前 session 无有效的推送目标，无法绑定为通知渠道（openclaw >= 2026.8 只扫默认 agent 名下的会话；若本对话属于其它 agent，请到默认 agent 的对话里绑定）",
        });
      }

      const pluginCfg = getPluginConfig(api);
      const currentKeys = normalizeNotifySessionKeys(pluginCfg);
      const changed = !currentKeys.includes(sessionKey);
      const nextKeys = changed ? [...currentKeys, sessionKey] : currentKeys;
      await setPluginConfig(api, {
        notifySessionKeys: nextKeys,
        notifySessionKey: "",
      });
      // 复用上面读的同一份快照：channel 与 channels/sessions 出自同一次枚举，
      // 不会因两次读之间宿主侧的写入而不自洽（顺带省一次全表扫描）。
      const channels = resolveConfiguredTargets(api, nextKeys, store).targets;
      return jsonResult({
        ok: true,
        changed,
        sessionKey,
        channel: resolve.channel,
        channels: channels.map((t) => t.channel),
        sessions: channels.map(toSessionView),
      });
    },
  });

  api.registerTool(bindFactory, { name: "miloco_notify_bind" });

  const unbindFactory: OpenClawPluginToolFactory = (ctx) => ({
    name: "miloco_notify_unbind",
    label: "Unbind notify channel",
    description:
      "解绑通知渠道。默认当前对话，也可指定 sessionKey；all=true 时清空全部绑定。",
    parameters: Type.Object({
      sessionKey: Type.Optional(
        Type.String({ description: "目标 session key，留空则使用当前对话" }),
      ),
      all: Type.Optional(
        Type.Boolean({ description: "是否清空全部已绑定通知渠道" }),
      ),
    }),
    async execute(_toolCallId, params) {
      const { sessionKey: inputKey, all } = params as {
        sessionKey?: string;
        all?: boolean;
      };
      const pluginCfg = getPluginConfig(api);
      const currentKeys = normalizeNotifySessionKeys(pluginCfg);

      if (all) {
        await setPluginConfig(api, { notifySessionKeys: [], notifySessionKey: "" });
        return jsonResult({
          ok: true,
          changed: currentKeys.length > 0,
          clearedAll: true,
          channels: [],
          sessions: [],
        });
      }

      const sessionKey = (inputKey || ctx.sessionKey || "").trim();
      if (!sessionKey) {
        return jsonResult({
          ok: false,
          error: "未指定 sessionKey 且当前上下文无 sessionKey",
        });
      }
      const nextKeys = currentKeys.filter((key) => key !== sessionKey);
      const changed = nextKeys.length !== currentKeys.length;
      await setPluginConfig(api, {
        notifySessionKeys: nextKeys,
        notifySessionKey: "",
      });
      const channels = resolveConfiguredTargets(api, nextKeys).targets;
      return jsonResult({
        ok: true,
        changed,
        sessionKey,
        channels: channels.map((t) => t.channel),
        sessions: channels.map(toSessionView),
      });
    },
  });

  api.registerTool(unbindFactory, { name: "miloco_notify_unbind" });
}

export function __resetNotifyDedup(): void {
  recentSends.clear();
}

export function toTimestamp(v: unknown): number {
  if (typeof v === "number") return v;
  if (typeof v === "string") {
    const ms = Date.parse(v);
    return Number.isNaN(ms) ? 0 : ms;
  }
  return 0;
}

function toSessionView(target: NotifyTarget) {
  return {
    sessionKey: target.sessionKey,
    channel: target.channel,
    to: target.to,
    accountId: target.accountId,
    threadId: target.threadId,
  };
}

function dedupKeyFor(sessionKey: string, message: string): string {
  return `${sessionKey}\n${message}`;
}

function pruneRecentSends(now: number, windowMs: number): void {
  for (const [k, ts] of recentSends) {
    if (now - ts >= windowMs) recentSends.delete(k);
  }
}

function normalizeNotifySessionKeys(
  pluginCfg: PluginNotifyConfig,
): string[] {
  const keys = Array.isArray(pluginCfg.notifySessionKeys)
    ? pluginCfg.notifySessionKeys
    : [];
  const deduped: string[] = [];
  for (const key of keys) {
    if (typeof key !== "string") continue;
    const trimmed = key.trim();
    if (!trimmed || deduped.includes(trimmed)) continue;
    deduped.push(trimmed);
  }
  return deduped;
}

type SessionStoreEntry = Record<string, unknown>;

/**
 * Minimal shape of the plugin runtime's agent session accessor, covering both
 * plugin-runtime generations:
 *
 *  - openclaw <= 2026.7.x exposes `resolveStorePath` + `loadSessionStore` (reads the
 *    whole session store file) and persists delivery at the entry top level as
 *    `lastTo` / `lastChannel` / `lastAccountId` / `lastThreadId`.
 *  - openclaw >= 2026.8 removed `loadSessionStore` from `runtime.agent.session` in
 *    favor of the entry-based `listSessionEntries` / `getSessionEntry`, made
 *    `resolveStorePath` throw `SessionStoreAgentIdRequiredError` when the store is
 *    empty, and moved delivery under `entry.delivery.route` (ChannelRouteRef).
 */
type SessionAccessor = {
  resolveStorePath?: (store?: string) => string;
  loadSessionStore?: (storePath: string) => Record<string, SessionStoreEntry>;
  listSessionEntries?: (params?: { agentId?: string }) => Array<{
    sessionKey: string;
    entry: SessionStoreEntry;
  }>;
};

function isRecordValue(v: unknown): v is Record<string, unknown> {
  return v !== null && typeof v === "object" && !Array.isArray(v);
}

// 宿主 normalizeAgentId（agent-id 模块）：小写、非法字符换 -、去首尾 -、截 64，
// 规范化为空回 "main"——无 id 的名册项在宿主同样落成 "main"，不丢弃。
function normalizeAgentIdValue(value: unknown): string {
  return (
    String(value ?? "")
      .trim()
      .toLowerCase()
      .replace(/[^a-z0-9_-]+/g, "-")
      .replace(/^-+/, "")
      .replace(/-+$/, "")
      .slice(0, 64) || "main"
  );
}

/**
 * 镜像宿主 readAgentRosterProperty（8.2/9.1 同）：roster 双形态且 entries 优先——
 * entries 键持 record → 用它（无视并存的 list：宿主此处直接短路返回，list 从不被
 * consult，两形态并存且内容不同时以 entries 为准是宿主语义而非猜测）；键存在但值
 * undefined → 宿主继续看 list；其余非 record 值连 list 兜底也吞掉（与宿主同）。
 * list 分支丢非对象项。
 */
function listRosterAgents(
  cfg: unknown,
): Array<Record<string, unknown> & { id: string }> {
  const agents = (
    cfg as { agents?: { entries?: unknown; list?: unknown } }
  ).agents;
  if (!isRecordValue(agents)) return [];
  if (Object.hasOwn(agents, "entries") && agents.entries !== undefined) {
    if (!isRecordValue(agents.entries)) return [];
    return Object.entries(agents.entries).flatMap(([id, entry]) =>
      isRecordValue(entry) ? [{ ...entry, id: normalizeAgentIdValue(id) }] : [],
    );
  }
  if (!Array.isArray(agents.list)) return [];
  return agents.list.flatMap((entry) =>
    isRecordValue(entry)
      ? [{ ...entry, id: normalizeAgentIdValue(entry.id) }]
      : [],
  );
}

/**
 * Default agent whose session store to scan. openclaw >= 2026.8 scopes the store
 * per agent, so the entry read must name an agent. This mirrors the host's own
 * ambient-owner resolution (agent-scope-config tryResolveAmbientOwnerAgentId;
 * identical in 2026.8.2 and 2026.9.1):
 *   1. `agents.defaults.systemAgent.agentId` — the materialized default; the host
 *      strips the raw `default: true` roster markers from the in-memory config at
 *      load (8.2 runtime-verified: disk keeps the marker, runtime.config.current()
 *      exposes entries without it), so this is what actually survives there;
 *   2. the sole `default: true` roster entry (unless `agents.ownership` is
 *      "explicit"), else the sole agent;
 *   3. the legacy implicit "main".
 * Roster assembly (entries-first-over-list etc.) lives in listRosterAgents.
 * Re-implemented here because the SDK export only exists in 2026.8+ while the
 * plugin compiles against 2026.5.20 typings, and runtime.agent exposes no
 * roster helper to call instead.
 *
 * Caller caveat: only this agent's sessions are read. `miloco_im_push` passes the
 * picked sessionKey to subagent.run (which carries no agentId of its own), but
 * `miloco_notify_bind` validates `ctx.sessionKey` against the same table — binding
 * from a conversation owned by another agent fails. Widening the read scope is a
 * separate delivery-scope decision.
 */
function resolveDefaultAgentIdFromConfig(cfg: unknown): string {
  const agents = (
    cfg as {
      agents?: {
        ownership?: unknown;
        defaults?: { systemAgent?: { agentId?: unknown } };
      };
    }
  ).agents;
  // 优先读宿主物化的默认 agent：加载期宿主把 legacy default 标记从内存态 roster 剥掉
  // （8.2 实测：磁盘 config 带 entries.home.default=true，runtime.config.current() 里
  // 变成 home:{}），物化落点是 defaults.systemAgent.agentId——这正是宿主 ambient owner
  // 解析（tryResolveAmbientOwnerAgentId）里 systemAgent 先于 legacy 标记的顺序。
  const systemAgentId = agents?.defaults?.systemAgent?.agentId;
  if (typeof systemAgentId === "string" && systemAgentId.trim()) {
    return normalizeAgentIdValue(systemAgentId);
  }
  const roster = listRosterAgents(cfg);
  if (roster.length === 0) return "main";
  if (agents?.ownership !== "explicit") {
    const marked = roster.filter((entry) => entry.default === true);
    if (marked.length === 1) return marked[0].id;
  }
  if (roster.length === 1) return roster[0].id;
  return "main";
}

// WARN 只报名册形状不报条目值：agent 条目可能携带 workspace/env 等不宜进日志的内容。
function summarizeAgentsShape(agents: unknown): string {
  if (!isRecordValue(agents)) {
    return JSON.stringify(agents)?.slice(0, 60) ?? String(agents);
  }
  const entries = agents.entries;
  const list = agents.list;
  const defaults = agents.defaults;
  return JSON.stringify({
    ownership: agents.ownership,
    systemAgent: isRecordValue(defaults) && isRecordValue(defaults.systemAgent)
      ? defaults.systemAgent.agentId
      : undefined,
    entries: Object.hasOwn(agents, "entries")
      ? isRecordValue(entries)
        ? Object.keys(entries)
        : typeof entries
      : undefined,
    list: Array.isArray(list)
      ? list.map((entry) =>
          isRecordValue(entry)
            ? typeof entry.id === "string"
              ? entry.id
              : "<no-id>"
            : "<non-object>",
        )
      : Object.hasOwn(agents, "list")
        ? typeof list
        : undefined,
  }).slice(0, 300);
}

// 名册属性是否「带了值但一个 agent 都给不出来」：entries:null / "garbage" 这类畸形，
// 以及 keys/元素全部非 record 的非空容器。合法空名册（entries:{} / list:[]）不算——
// 隐式 main 照常工作，告警它们是每条通知的假警报。
function hasUnusableRosterValue(cfg: unknown): boolean {
  const agents = (cfg as { agents?: Record<string, unknown> }).agents;
  if (!isRecordValue(agents)) return false;
  if (Object.hasOwn(agents, "entries") && agents.entries !== undefined) {
    const entries = agents.entries;
    if (!isRecordValue(entries)) return true;
    return (
      Object.keys(entries).length > 0 &&
      !Object.values(entries).some(isRecordValue)
    );
  }
  if (Object.hasOwn(agents, "list") && agents.list !== undefined) {
    const list = agents.list;
    if (!Array.isArray(list)) return true;
    return list.length > 0 && !list.some(isRecordValue);
  }
  return false;
}

function listSessionStore(api: OpenClawPluginApi): Record<string, SessionStoreEntry> {
  // 可选访问：宿主连 agent/session 对象都摘掉时，裸取会抛 TypeError，把 no-channel
  // 契约（结构化非 500）退化成 500——这里与「两代 API 都探测不到」同向降级。
  const runtime = api.runtime as { agent?: { session?: unknown } } | undefined;
  const session = runtime?.agent?.session as SessionAccessor | undefined;
  if (!session || typeof session !== "object") {
    logger.warn(
      "notify: 宿主会话访问器（runtime.agent.session）不可用，按空表降级" +
        "（通知与绑定将停在 no-channel）",
    );
    return {};
  }

  // openclaw >= 2026.8: entry-based read. Must pass an agent id (listSessionEntries
  // without it throws "Cannot resolve SQLite session scope without an agent id").
  // Only the default agent is scanned — delivery follows the sessionKey we pick,
  // so a wider read scope without a matching delivery scope buys nothing.
  if (typeof session.listSessionEntries === "function") {
    const cfg = getRuntimeConfig(api);
    const agentId = resolveDefaultAgentIdFromConfig(cfg);
    let entries: Array<{ sessionKey: string; entry: SessionStoreEntry }> = [];
    try {
      entries = session.listSessionEntries({ agentId }) ?? [];
    } catch (err) {
      // 宿主一般不会走到这里：agentId 恒为非空串，「agent 不存在」宿主折成空数组而非
      // 抛错（8.2/9.1 源码核实：resolveSqliteScope 只在 agentId 为假值时抛）。留这层
      // 保护兜宿主行为变化 / 存储层 IO 异常。读不到会话等同「主人从没在 IM 里说过话」，
      // 降级为空表——别把异常抛给 /miloco/webhook，那里靠 no-channel(非 500)告诉
      // backend「未送达、不重试传输」。
      logger.warn(
        `listSessionEntries(agentId=${agentId}) failed: ${
          err instanceof Error ? err.message : String(err)
        }`,
      );
      return {};
    }
    // 对账放在读之后：只有读回来确实是 0 条，才能断言「scope 猜偏」——名册里没有 main
    // 但 main 库仍有历史会话的升级宿主上，读是成功的，读前告警会每条通知刷假警报。
    // 局限（如实声明）：推导与名册同出 listRosterAgents，名册形态整段解析错时两边
    // 一致、这类对账天然失效——插件拿不到宿主名册的第二个来源，只能靠读到的数据兜底。
    // 名册属性带非空值却解析不出任何 agent（entries:null 等）是可定位的异常，单独告警；
    // 合法空名册（entries:{} / list:[]）不算畸形——隐式 main 照常工作，告警反而是噪音。
    const rosterIds = listRosterAgents(cfg).map((entry) => entry.id);
    if (rosterIds.length === 0 && hasUnusableRosterValue(cfg)) {
      logger.warn(
        `notify: agents 的 entries/list 带非空值却解析不出任何 agent，名册按空表处理；` +
          `插件读到的名册形状=${summarizeAgentsShape(
            (cfg as { agents?: unknown }).agents,
          )}`,
      );
    } else if (
      rosterIds.length > 0 &&
      !rosterIds.includes(agentId) &&
      entries.length === 0
    ) {
      logger.warn(
        `notify: 读了 agentId="${agentId}" 的会话库，0 条会话，而名册=[${rosterIds.join(", ")}]` +
          "——默认 agent 推导疑似偏了，通知与绑定都会停在 no-channel；宿主会把 roster 的 " +
          "default 标记物化进 agents.defaults.systemAgent.agentId，请核对该字段或名册标记。" +
          `插件读到的名册形状=${summarizeAgentsShape(
            (cfg as { agents?: unknown }).agents,
          )}`,
      );
    }
    const store: Record<string, SessionStoreEntry> = {};
    for (const { sessionKey, entry } of entries) {
      store[sessionKey] = entry;
    }
    return store;
  }

  // openclaw <= 2026.7.x: legacy whole-store file read.
  if (
    typeof session.resolveStorePath === "function" &&
    typeof session.loadSessionStore === "function"
  ) {
    const cfg = getRuntimeConfig(api);
    const sessionCfg = (cfg as Record<string, unknown>).session as
      | { store?: string }
      | undefined;
    try {
      return session.loadSessionStore(session.resolveStorePath(sessionCfg?.store));
    } catch (err) {
      // 会话文件损坏 / IO 异常同样降级空表——与 8.x 分支同一降级原则：异常穿透到
      // /miloco/webhook 会被打成 HTTP 500，backend 按传输失败记账重试，而 no-channel
      // （结构化非 500）才是「未送达、不重试传输」的正确信号。
      logger.warn(
        `legacy loadSessionStore failed: ${
          err instanceof Error ? err.message : String(err)
        }`,
      );
      return {};
    }
  }

  // 两条路径都探测不到：宿主把两代会话 API 都摘掉了（正是本插件适配过的那类宿主
  // 变更）。降级空表本身对（守 no-channel 非 500 契约），但必须留痕——否则「宿主
  // 变更导致读取失效」与「主人从没在 IM 说过话」在日志里完全同形，无从排查。
  logger.warn(
    "notify: 宿主既无 listSessionEntries 也无 resolveStorePath+loadSessionStore，" +
      "会话读取能力不可用，按空表降级（通知与绑定将停在 no-channel）",
  );
  return {};
}

type LastDeliveryRoute = {
  channel: string;
  to: string | undefined;
  accountId: string | undefined;
  threadId: string | number | undefined;
};

type SessionDeliveryRouteShape = {
  channel?: string;
  accountId?: string;
  target?: { to?: string };
  thread?: { id?: string | number };
};

/**
 * Read the persisted delivery route of one session entry, returning null when
 * the entry has no usable outbound target. Understands both entry shapes:
 *  - openclaw >= 2026.8 canonical `delivery.route` (delivery.kind === "external");
 *  - legacy top-level `lastTo` / `lastChannel` fields (pre-2026.8 entries, and
 *    unmigrated rows until `openclaw doctor --fix` rewrites them).
 */
function readLastDeliveryRoute(
  entry: SessionStoreEntry | undefined,
): LastDeliveryRoute | null {
  if (!entry) return null;

  const delivery = entry.delivery as
    | { kind?: string; route?: SessionDeliveryRouteShape }
    | undefined;

  // 有 delivery 且 kind 不是 external 时一律不投递:delivery 由宿主接管后,顶层
  // lastTo/lastChannel 只是迁移残留,不可靠(kind=none/internal 明确不对外;未知的未来
  // 枚举值同样不能确认可投,回退旧字段反而有错投风险)。仅两种情况回退老字段:
  // ① kind 缺失/无 delivery 的未迁移行;② kind=external 但 route 里 channel/target.to
  // 读不齐(惰性迁移写了一半,或宿主换了 route 形状)——此时控制流落到下方老字段分支。
  if (delivery?.kind && delivery.kind !== "external") return null;

  if (delivery?.kind === "external") {
    const route = delivery.route;
    const channel = route?.channel;
    const to = route?.target?.to;
    if (channel && to) {
      return {
        channel,
        to,
        accountId: route?.accountId,
        threadId: route?.thread?.id,
      };
    }
  }

  const channel = entry.lastChannel as string | undefined;
  const to = entry.lastTo as string | undefined;
  if (channel && to) {
    return {
      channel,
      to,
      accountId: entry.lastAccountId as string | undefined,
      threadId: entry.lastThreadId as string | number | undefined,
    };
  }
  return null;
}

function resolveSessionByKey(
  store: Record<string, SessionStoreEntry>,
  sessionKey: string,
): BoundSessionInfo | null {
  const route = readLastDeliveryRoute(store[sessionKey]);
  if (!route) return null;
  return { ...route, sessionKey };
}

function resolveConfiguredTargets(
  api: OpenClawPluginApi,
  sessionKeys?: string[],
  store: Record<string, SessionStoreEntry> = listSessionStore(api),
): { targets: NotifyTarget[]; invalidSessionKeys: string[] } {
  const keys = sessionKeys ?? normalizeNotifySessionKeys(getPluginConfig(api));
  const targets: NotifyTarget[] = [];
  const invalidSessionKeys: string[] = [];
  for (const key of keys) {
    const target = resolveSessionByKey(store, key);
    if (target) {
      targets.push(target);
    } else {
      invalidSessionKeys.push(key);
    }
  }
  return { targets, invalidSessionKeys };
}

function selectMostRecentTarget(
  api: OpenClawPluginApi,
  preferredKeys?: string[],
  store: Record<string, SessionStoreEntry> = listSessionStore(api),
): NotifyTarget | null {
  type TimedTarget = {
    channel: string;
    to: string | undefined;
    accountId: string | undefined;
    threadId: string | number | undefined;
    sessionKey: string;
    lastInteractionAt: number;
  };
  const toTimedTarget = (
    sessionKey: string,
    entry: SessionStoreEntry | undefined,
  ): TimedTarget | null => {
    const route = readLastDeliveryRoute(entry);
    if (!route) return null;
    return {
      channel: route.channel,
      to: route.to,
      accountId: route.accountId,
      threadId: route.threadId,
      sessionKey,
      // 排序键字段名已证实(2026-09-04 cat@mac openclaw 9.1 实测 100/100 会话顶层
      // 都有必填 updatedAt,98/100 有可选 lastInteractionAt,未随投递迁入 delivery)。
      lastInteractionAt: toTimestamp(
        entry?.lastInteractionAt ?? entry?.updatedAt,
      ),
    };
  };
  const candidates =
    preferredKeys && preferredKeys.length > 0
      ? preferredKeys
          .map((key) => toTimedTarget(key, store[key]))
          .filter((v): v is TimedTarget => v !== null)
      : Object.entries(store)
          .map(([key, entry]) => toTimedTarget(key, entry))
          .filter((v): v is TimedTarget => v !== null);

  let best: TimedTarget | null = null;
  for (const candidate of candidates) {
    if (!best || candidate.lastInteractionAt >= best.lastInteractionAt) {
      best = candidate;
    }
  }
  return best
    ? {
        channel: best.channel,
        to: best.to,
        accountId: best.accountId,
        threadId: best.threadId,
        sessionKey: best.sessionKey,
      }
    : null;
}

export function resolveNotifyTarget(api: OpenClawPluginApi): ResolveResult {
  // 单次读表后透传,避免每个 bound key 各读一遍全表、且多次读间不是同一快照。
  const store = listSessionStore(api);
  const configured = resolveConfiguredTargets(api, undefined, store);
  if (configured.targets.length > 0) {
    return {
      target: selectMostRecentTarget(
        api,
        configured.targets.map((t) => t.sessionKey),
        store,
      ),
      targets: configured.targets,
      needsBind: false,
      invalidSessionKeys: configured.invalidSessionKeys,
    };
  }

  const hasConfiguredKeys = normalizeNotifySessionKeys(getPluginConfig(api)).length > 0;
  const fallback = selectMostRecentTarget(api, undefined, store);
  const bindReason: BindReason = hasConfiguredKeys
    ? "configured_but_invalid"
    : "not_configured";

  return {
    target: fallback,
    targets: [],
    needsBind: true,
    bindReason,
    invalidSessionKeys: configured.invalidSessionKeys,
  };
}

async function deliverToTarget(
  api: OpenClawPluginApi,
  target: NotifyTarget,
  message: string,
  bindHint?: string,
): Promise<DeliverAttempt> {
  const windowMs = getNotifyDedupWindowMs();
  const dedupKey = dedupKeyFor(target.sessionKey, message);
  if (windowMs > 0) {
    const now = Date.now();
    pruneRecentSends(now, windowMs);
    const last = recentSends.get(dedupKey);
    if (last !== undefined && now - last < windowMs) {
      return {
        sessionKey: target.sessionKey,
        channel: target.channel,
        ok: true,
        deduped: true,
      };
    }
  }

  const body = bindHint ? `${message}\n---\n${bindHint}` : message;
  const deliverMessage = `<miloco-notification>${body}</miloco-notification>`;

  try {
    const { runId } = await api.runtime.subagent.run({
      sessionKey: target.sessionKey,
      extraSystemPrompt: [
        "# 当前任务",
        "你正在转发 miloco 发送给用户的通知。<miloco-notification></miloco-notification> 标签内是完整的消息正文，请将标签内部的内容原样转发给用户。",
        "",
        "## 注意事项",
        "- 只转发标签**内部**的文本，绝不要带上 <miloco-notification> 或 </miloco-notification> 标签本身。",
        "- 若标签内部出现 `---` 分割线及其下方的引导提示（仅 fallback 投递时会有），分割线与下方提示都要原封不动一并转发，不能丢弃、概括或改写；若没有则直接转发标签内全文即可。",
        "- 不要添加任何前缀、后缀、解释或寒暄。",
        "",
        "## 示例",
        "输入：",
        `<miloco-notification>${PROMPT_EXAMPLE_BODY}`,
        "---",
        `${PROMPT_EXAMPLE_HINT}</miloco-notification>`,
        "",
        "✅ 正确转发（去掉标签、保留分割线及下方提示）：",
        PROMPT_EXAMPLE_BODY,
        "---",
        PROMPT_EXAMPLE_HINT,
        "",
        "❌ 错误转发（带上了标签，或丢掉了分割线下方的提示）：",
        `<miloco-notification>${PROMPT_EXAMPLE_BODY}</miloco-notification>`,
      ].join("\n"),
      message: deliverMessage,
      deliver: true,
      lightContext: true,
      idempotencyKey: crypto.randomUUID(),
    });

    const result = await api.runtime.subagent.waitForRun({
      runId,
      timeoutMs: 30_000,
    });
    if (result.status === "ok") {
      if (windowMs > 0) recentSends.set(dedupKey, Date.now());
      return { sessionKey: target.sessionKey, channel: target.channel, ok: true };
    }
    return {
      sessionKey: target.sessionKey,
      channel: target.channel,
      ok: false,
      error: `subagent delivery failed: ${result.status} ${result.error ?? ""}`.trim(),
    };
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    return {
      sessionKey: target.sessionKey,
      channel: target.channel,
      ok: false,
      error: `delivery failed: ${msg}`,
    };
  }
}

export async function notifyOwner(
  api: OpenClawPluginApi,
  message: string,
  opts?: { bindHint?: string },
): Promise<NotifyResult> {
  const { target, targets, needsBind, bindReason } = resolveNotifyTarget(api);
  const bindHint = opts?.bindHint?.trim();

  if (needsBind) {
    if (!target) {
      return {
        ok: false,
        error: "no available IM channel — owner has never interacted via IM",
      };
    }
    if (!bindHint) {
      return {
        ok: false,
        needsBind: true,
        bindReason,
        fallbackChannel: target.channel,
        bindHintExample: BIND_HINT_EXAMPLE[bindReason ?? "not_configured"],
        error:
          "本条通知尚未发出。这是给你（agent）的执行指令，不是要回复给用户的内容——禁止把本说明转述给用户。",
        nextAction:
          "立即再次调用 miloco_im_push：message 保持本次内容不变，并补上 bindHint 参数——把 bindHintExample 翻译成主人当前使用的语言后作为 bindHint 传入。补上 bindHint 后通知才会真正发送。不要在对话里回复、也不要等待用户确认。",
      };
    }

    const attempt = await deliverToTarget(api, target, message, bindHint);
    if (attempt.ok) {
      return {
        ok: true,
        channel: target.channel,
        channels: [target.channel],
        deliveredChannels: [target.channel],
        failedChannels: [],
        fallback: true,
        ...(attempt.deduped ? { deduped: true } : {}),
      };
    }
    return { ok: false, error: attempt.error, failedChannels: [target.channel] };
  }

  const attempts = await Promise.all(
    targets.map((notifyTarget) => deliverToTarget(api, notifyTarget, message)),
  );
  const delivered = attempts.filter((attempt) => attempt.ok);
  const failed = attempts.filter((attempt) => !attempt.ok);
  const dedupedOnly = delivered.length > 0 && delivered.every((item) => item.deduped);
  if (delivered.length === 0) {
    return {
      ok: false,
      error:
        failed.map((item) => item.error).find(Boolean) ?? "delivery failed",
      channels: targets.map((item) => item.channel),
      deliveredChannels: [],
      failedChannels: failed.map((item) => item.channel),
    };
  }
  return {
    ok: true,
    channel: delivered[0]?.channel,
    channels: targets.map((item) => item.channel),
    deliveredChannels: delivered.map((item) => item.channel),
    failedChannels: failed.map((item) => item.channel),
    partialSuccess: failed.length > 0 ? true : undefined,
    deduped: dedupedOnly ? true : undefined,
  };
}
