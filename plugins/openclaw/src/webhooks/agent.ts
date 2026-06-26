import {
  getTurnStatus,
  peekTurnMeta,
  registerTraceLink,
} from "../hooks/trace.js";
import { logger } from "../utils/logger.js";
import type { WebhookEntry } from "./index.js";

// waitForRun 兜底超时: backend 不传 timeoutMs 时用此值(对齐 backend WAIT_MS)。
const DEFAULT_WAIT_MS = 180_000;
// 溢出自愈重试 turn 的等待上限: 与首个失败 turn 串联,需有界以免逼近 backend HTTP 超时。
const RETRY_WAIT_MS = 60_000;
// trace meta 由 subagent_ended + setImmediate 写入,可能略滞后 waitForRun 返回; 短轮询兜住。
const META_POLL_TIMEOUT_MS = 2_000;
const META_POLL_INTERVAL_MS = 100;

interface IRequestBody {
  message: string;
  sessionKey?: string;
  lane?: string;
  idempotencyKey?: string;
  extraSystemPrompt?: string;
  traceId?: string;
  timeoutMs?: number;
  createdAtMs?: number;
  expiresAtMs?: number;
}

interface WaitResult {
  status: "ok" | "error" | "timeout";
  error?: string;
}

function isContextOverflow(text: string | null | undefined): boolean {
  return typeof text === "string" && /context overflow/i.test(text);
}

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

// 等本 run 的 trace meta 落定(done)后返回;超时仍未 done 返 undefined(按非溢出处理,安全降级)。
async function waitTurnMeta(runId: string, timeoutMs: number) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (getTurnStatus(runId) === "done") break;
    await sleep(META_POLL_INTERVAL_MS);
  }
  return peekTurnMeta(runId);
}

// 检测该 run 是否因上下文溢出而失败；命中则返回溢出文案(用于带回后端记录原因),否则 undefined。
// 溢出 turn 的 waitForRun 实测返回 status="ok"(give-up 分支返回 isError payload 而非抛错,
// 平台据此把终态判成非 error → status=ok、waitForRun 不带 error),故主信号取 trace meta 的
// success/errorMsg;同时兼容少见的真抛错路径(wait.error)。
async function detectOverflow(
  runId: string,
  wait: WaitResult,
): Promise<string | undefined> {
  if (wait.status === "error" && isContextOverflow(wait.error)) {
    return wait.error;
  }
  const meta = await waitTurnMeta(runId, META_POLL_TIMEOUT_MS);
  if (meta && meta.success === false && isContextOverflow(meta.errorMsg)) {
    return meta.errorMsg ?? undefined;
  }
  return undefined;
}

export const kAgentWebhook: WebhookEntry<IRequestBody> = {
  name: "agent",
  action: async ({ api, payload }) => {
    const {
      message,
      extraSystemPrompt,
      sessionKey = "main",
      lane,
      idempotencyKey = crypto.randomUUID(),
      traceId,
      timeoutMs,
      createdAtMs,
      expiresAtMs,
    } = payload;
    // 自愈双 turn 串联须留在 backend HTTP 超时内，startedAt 用于给重试 turn 算剩余等待预算。
    const startedAt = Date.now();

    const dropIfExpired = () => {
      if (!isFiniteNumber(expiresAtMs) || Date.now() <= expiresAtMs) {
        return null;
      }
      const delayMs = isFiniteNumber(createdAtMs) ? Date.now() - createdAtMs : null;
      logger.warn(
        `[DROPPED] stale agent webhook skipped before subagent.run session=${sessionKey} lane=${lane ?? ""} delay_ms=${delayMs ?? ""} expires_at_ms=${expiresAtMs}`,
      );
      return {
        runId: null,
        status: "ok",
        dropped: true,
        reason: "expired",
      };
    };

    const expired = dropIfExpired();
    if (expired) return expired;

    const runOnce = async (idem: string, waitMs: number) => {
      const result = await api.runtime.subagent.run({
        sessionKey,
        message,
        lane,
        deliver: false,
        idempotencyKey: idem,
        extraSystemPrompt,
      });
      if (traceId) {
        registerTraceLink(result.runId, traceId);
      }
      // 同步等待该 turn 跑完(或超时),再回传结果 — backend 单飞调度依赖此阻塞语义。
      const wait = (await api.runtime.subagent.waitForRun({
        runId: result.runId,
        timeoutMs: waitMs,
      })) as WaitResult;
      return { runId: result.runId, wait };
    };

    const first = await runOnce(idempotencyKey, timeoutMs ?? DEFAULT_WAIT_MS);

    // 上下文溢出自愈: plugin 侧无法 reset/clear session,只能 deleteSession 删旧会话重建。
    // 删除后同 sessionKey 再 run 自动建空会话;重试恒一次,不死循环。
    const overflowReason = await detectOverflow(first.runId, first.wait);
    if (overflowReason) {
      try {
        logger.warn(
          `[overflow-self-heal] context overflow on session=${sessionKey}; deleting session and retrying once`,
        );
        await api.runtime.subagent.deleteSession({
          sessionKey,
          deleteTranscript: true,
        });
        // 重试 turn 的等待预算：保证两段 turn 总时长落在本次 webhook 的 timeoutMs 内
        // （首个 turn 已耗 elapsed），再扣一次 trace meta 轮询；backend HTTP 超时在 timeoutMs
        // 之上还有 15s 缓冲吸收 deleteSession / 轮询 / HTTP 开销，故插件侧无需硬编码该缓冲。
        // 常规下首个 turn 秒级返回 → 预算充裕 → 取 RETRY_WAIT_MS 上限；首个 turn 慢时自动收窄。
        const elapsed = Date.now() - startedAt;
        const expiredBeforeRetry = dropIfExpired();
        if (expiredBeforeRetry) return expiredBeforeRetry;
        const retryWaitMs = Math.max(
          10_000,
          Math.min(
            RETRY_WAIT_MS,
            (timeoutMs ?? DEFAULT_WAIT_MS) - elapsed - META_POLL_TIMEOUT_MS,
          ),
        );
        const retry = await runOnce(`${idempotencyKey}:retry`, retryWaitMs);
        const retryOverflow = await detectOverflow(retry.runId, retry.wait);
        const recovered = !retryOverflow;
        if (recovered) {
          logger.info(
            `[overflow-self-heal] recovered session=${sessionKey} after reset`,
          );
        } else {
          // 重建后仍溢出 = 系统提示自身超预算(配置问题),删除重建救不回 → 停手,不再循环。
          logger.error(
            `[overflow-self-heal] still overflow after reset; session=${sessionKey} unrecoverable by delete (system prompt likely exceeds context budget)`,
          );
        }
        return {
          runId: retry.runId,
          status: retry.wait.status,
          // 把溢出文案带回后端: recovered 时为触发自愈的首个溢出原因,
          // 不可恢复时为重试仍溢出的原因;供 backend 记录"具体原因"。
          error: retry.wait.error ?? retryOverflow ?? overflowReason,
          recovered,
        };
      } catch (err) {
        // deleteSession 被拒(如主会话保护)或重试失败 → 返回首个结果,不把 webhook 打成 500。
        const msg = err instanceof Error ? err.message : String(err);
        logger.error(
          `[overflow-self-heal] reset failed for session=${sessionKey}: ${msg}`,
        );
      }
    }

    return {
      runId: first.runId,
      status: first.wait.status,
      error: first.wait.error,
    };
  },
};
