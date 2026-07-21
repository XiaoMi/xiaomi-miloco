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
      const resolve = resolveSessionByKey(api, sessionKey);
      if (!resolve) {
        return jsonResult({
          ok: false,
          error: "当前 session 无有效的推送目标，无法绑定为通知渠道",
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
      const channels = resolveConfiguredTargets(api, nextKeys).targets;
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

function loadSessionStore(api: OpenClawPluginApi) {
  const cfg = getRuntimeConfig(api);
  const sessionCfg = (cfg as Record<string, unknown>).session as
    | { store?: string }
    | undefined;
  const storePath = api.runtime.agent.session.resolveStorePath(sessionCfg?.store);
  return api.runtime.agent.session.loadSessionStore(storePath) as Record<
    string,
    Record<string, unknown>
  >;
}

function resolveSessionByKey(
  api: OpenClawPluginApi,
  sessionKey: string,
): BoundSessionInfo | null {
  const store = loadSessionStore(api);
  const entry = store[sessionKey];
  if (!entry?.lastTo || !entry?.lastChannel) return null;
  return {
    channel: entry.lastChannel as string,
    to: entry.lastTo as string | undefined,
    accountId: entry.lastAccountId as string | undefined,
    threadId: entry.lastThreadId as string | number | undefined,
    sessionKey,
  };
}

function resolveConfiguredTargets(
  api: OpenClawPluginApi,
  sessionKeys?: string[],
): { targets: NotifyTarget[]; invalidSessionKeys: string[] } {
  const keys = sessionKeys ?? normalizeNotifySessionKeys(getPluginConfig(api));
  const targets: NotifyTarget[] = [];
  const invalidSessionKeys: string[] = [];
  for (const key of keys) {
    const target = resolveSessionByKey(api, key);
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
): NotifyTarget | null {
  const store = loadSessionStore(api);
  type TimedTarget = {
    channel: string;
    to: string | undefined;
    accountId: string | undefined;
    threadId: string | number | undefined;
    sessionKey: string;
    lastInteractionAt: number;
  };
  const candidates =
    preferredKeys && preferredKeys.length > 0
      ? preferredKeys
          .map((key) => {
            const entry = store[key];
            if (!entry?.lastTo || !entry?.lastChannel) return null;
            return {
              channel: entry.lastChannel as string,
              to: entry.lastTo as string | undefined,
              accountId: entry.lastAccountId as string | undefined,
              threadId: entry.lastThreadId as string | number | undefined,
              sessionKey: key,
              lastInteractionAt: toTimestamp(
                entry.lastInteractionAt ?? entry.updatedAt,
              ),
            };
          })
          .filter((v): v is TimedTarget => v !== null)
      : Object.entries(store)
          .map(([key, entry]) => {
            if (!entry?.lastTo || !entry?.lastChannel) return null;
            return {
              channel: entry.lastChannel as string,
              to: entry.lastTo as string | undefined,
              accountId: entry.lastAccountId as string | undefined,
              threadId: entry.lastThreadId as string | number | undefined,
              sessionKey: key,
              lastInteractionAt: toTimestamp(
                entry.lastInteractionAt ?? entry.updatedAt,
              ),
            };
          })
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
  const configured = resolveConfiguredTargets(api);
  if (configured.targets.length > 0) {
    return {
      target: selectMostRecentTarget(
        api,
        configured.targets.map((t) => t.sessionKey),
      ),
      targets: configured.targets,
      needsBind: false,
      invalidSessionKeys: configured.invalidSessionKeys,
    };
  }

  const hasConfiguredKeys = normalizeNotifySessionKeys(getPluginConfig(api)).length > 0;
  const fallback = selectMostRecentTarget(api);
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
