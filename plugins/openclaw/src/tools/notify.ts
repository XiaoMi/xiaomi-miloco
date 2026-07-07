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

  const setChannelFactory: OpenClawPluginToolFactory = (ctx) => ({
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
      const sessionKey = inputKey || ctx.sessionKey;
      if (!sessionKey) {
        return jsonResult({
          ok: false,
          error: "未指定 sessionKey 且当前上下文无 sessionKey",
        });
      }

      const cfg = getRuntimeConfig(api);
      const sessionCfg = (cfg as Record<string, unknown>).session as
        | { store?: string }
        | undefined;
      const storePath = api.runtime.agent.session.resolveStorePath(
        sessionCfg?.store,
      );
      const store = api.runtime.agent.session.loadSessionStore(
        storePath,
      ) as Record<string, Record<string, unknown>>;

      const entry = store[sessionKey];
      if (!entry || !entry.lastTo || !entry.lastChannel) {
        return jsonResult({
          ok: false,
          error: "当前 session 无有效的推送目标，无法绑定为通知渠道",
        });
      }

      await setPluginConfig(api, { notifySessionKey: sessionKey });
      return jsonResult({
        ok: true,
        channel: entry.lastChannel as string,
        sessionKey,
      });
    },
  });

  api.registerTool(setChannelFactory, { name: "miloco_notify_bind" });
}

type NotifyTarget = {
  channel: string;
  to?: string;
  accountId?: string;
  threadId?: string | number;
  sessionKey?: string;
};

export type NotifyResult = {
  ok: boolean;
  error?: string;
  channel?: string;
  needsBind?: boolean;
  bindReason?: BindReason;
  fallbackChannel?: string;
  fallback?: boolean;
  nextAction?: string;
  bindHintExample?: string;
  /** true 表示相同通知在去重窗口内已发过一次，本次被静默去重（未再次投递）。 */
  deduped?: boolean;
};

// 相同 (接收人, 文案) 的短窗去重兜底：agent 循环重发时不把同一条通知 1:1 透传给
// 用户。窗口来自 config.json 的 notify.dedup_window_sec（见 getNotifyDedupWindowMs），
// 记录仅在成功投递后 → 失败可立即重试。进程内存即可：通知都在单个 agent 进程里发。
const recentSends = new Map<string, number>();

function dedupKeyFor(sessionKey: string, message: string): string {
  return `${sessionKey}\n${message}`;
}

function pruneRecentSends(now: number, windowMs: number): void {
  for (const [k, ts] of recentSends) {
    if (now - ts >= windowMs) recentSends.delete(k);
  }
}

/** 测试专用：清空去重表，避免模块级状态跨用例串扰。生产代码不调用。 */
export function __resetNotifyDedup(): void {
  recentSends.clear();
}

// 与 miloco-notify skill references/channel-config.md 的「bindHint 模板」表保持一致；修改任一处需同步另一处。
// 返回给 agent 作为可直接翻译成主人语言的 bindHint 范例（兜底：agent 未加载 skill 时仍能照做）。
const BIND_HINT_EXAMPLE: Record<BindReason, string> = {
  not_configured:
    "您尚未设置 Miloco 通知频道，本条消息已临时发送到最近活跃的对话。回复「绑定通知频道」可将当前对话设为固定的 Miloco 通知频道，后续提醒、定时任务、告警等通知都将发送至此。",
  configured_but_invalid:
    "您原先绑定的 Miloco 通知频道已失效，本条消息已临时发送到最近活跃的对话。请回复「绑定通知频道」重新绑定。",
};

// 转发 prompt 示例所用的引导语，与实际 fallback 投递的模板共用同一字面量，避免二者漂移。
const PROMPT_EXAMPLE_BODY = "客厅的灯已经为您打开。";
const PROMPT_EXAMPLE_HINT = BIND_HINT_EXAMPLE.not_configured;

export function toTimestamp(v: unknown): number {
  if (typeof v === "number") return v;
  if (typeof v === "string") {
    const ms = Date.parse(v);
    return Number.isNaN(ms) ? 0 : ms;
  }
  return 0;
}

export type BindReason = "not_configured" | "configured_but_invalid";

export type ResolveResult = {
  target: (NotifyTarget & { sessionKey: string }) | null;
  needsBind: boolean;
  bindReason?: BindReason;
};

export function resolveNotifyTarget(api: OpenClawPluginApi): ResolveResult {
  const cfg = getRuntimeConfig(api);
  const sessionCfg = (cfg as Record<string, unknown>).session as
    | { store?: string }
    | undefined;

  const storePath = api.runtime.agent.session.resolveStorePath(
    sessionCfg?.store,
  );
  const store = api.runtime.agent.session.loadSessionStore(storePath) as Record<
    string,
    Record<string, unknown>
  >;

  const pluginCfg = getPluginConfig(api);
  const preferredKey = pluginCfg.notifySessionKey;

  // 已配置且有效 → 正常使用
  if (preferredKey) {
    const entry = store[preferredKey];
    if (entry?.lastTo && entry?.lastChannel) {
      return {
        needsBind: false,
        target: {
          channel: (entry.lastChannel as string) ?? "unknown",
          to: entry.lastTo as string | undefined,
          accountId: entry.lastAccountId as string | undefined,
          threadId: entry.lastThreadId as string | number | undefined,
          sessionKey: preferredKey,
        },
      };
    }
  }

  // 未配置或配置无效 → fallback 到最近活跃 channel，标记需要绑定
  const bindReason: BindReason = preferredKey
    ? "configured_but_invalid"
    : "not_configured";
  let best: (NotifyTarget & { lastInteractionAt: number }) | null = null;

  for (const [key, value] of Object.entries(store)) {
    const entry = value;
    if (!entry.lastChannel || !entry.lastTo) continue;

    const channel = entry.lastChannel as string;
    const to = entry.lastTo as string;
    const interactedAt = toTimestamp(
      entry.lastInteractionAt ?? entry.updatedAt,
    );

    if (!best || interactedAt >= best.lastInteractionAt) {
      best = {
        channel,
        to,
        accountId: entry.lastAccountId as string | undefined,
        threadId: entry.lastThreadId as string | number | undefined,
        sessionKey: key,
        lastInteractionAt: interactedAt,
      };
    }
  }

  if (best?.sessionKey) {
    return {
      needsBind: true,
      bindReason,
      target: {
        channel: best.channel,
        to: best.to,
        accountId: best.accountId,
        threadId: best.threadId,
        sessionKey: best.sessionKey,
      },
    };
  }

  return { target: null, needsBind: true, bindReason };
}

export async function notifyOwner(
  api: OpenClawPluginApi,
  message: string,
  opts?: { bindHint?: string },
): Promise<NotifyResult> {
  const { target, needsBind, bindReason } = resolveNotifyTarget(api);
  if (!target) {
    return {
      ok: false,
      error: "no available IM channel — owner has never interacted via IM",
    };
  }

  const bindHint = opts?.bindHint?.trim();

  // 未绑定（或绑定已失效）且 agent 未提供绑定引导语：不发送，把决策交回 agent。
  // agent 应先加载 miloco-notify skill，再按用户语言写好 bindHint 重新调用。
  if (needsBind && !bindHint) {
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

  // 到这里即将真正投递（needsBind 且缺 bindHint 的分支已在上面返回、未发送、不计入
  // 去重）。同一 (接收人, 文案) 在窗口内重复调用 → 静默去重、返回 ok，让 agent 停止重试。
  const windowMs = getNotifyDedupWindowMs();
  const dedupKey = dedupKeyFor(target.sessionKey, message);
  if (windowMs > 0) {
    const now = Date.now();
    pruneRecentSends(now, windowMs);
    const last = recentSends.get(dedupKey);
    if (last !== undefined && now - last < windowMs) {
      return { ok: true, channel: target.channel, deduped: true };
    }
  }

  // 已绑定时忽略 bindHint；fallback 投递时把 bindHint 拼到正文之后。
  const body = needsBind && bindHint ? `${message}\n---\n${bindHint}` : message;
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
      // 仅在成功投递后记录 → 失败不写、可立即重试。
      if (windowMs > 0) recentSends.set(dedupKey, Date.now());
      return {
        ok: true,
        channel: target.channel,
        ...(needsBind ? { fallback: true } : {}),
      };
    }
    const error =
      `subagent delivery failed: ${result.status} ${result.error ?? ""}`.trim();
    return { ok: false, error };
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    return { ok: false, error: `delivery failed: ${msg}` };
  }
}
