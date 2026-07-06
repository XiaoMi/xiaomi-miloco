import { logger } from "../utils/logger.js";
import type { WebhookEntry } from "./index.js";

interface IRequestBody {
  // 待重置（删除）的 session key 列表。由调用方（backend）传入，插件不硬编码业务 key。
  sessionKeys: string[];
  // 是否连 transcript 落盘文件一起删。默认 true = 彻底清空，符合"切换家庭防上下文干扰"语义。
  deleteTranscript?: boolean;
}

interface ResetResult {
  reset: string[];
  failed: { sessionKey: string; error: string }[];
}

// 批量 reset 指定 session：逐个 deleteSession，单个失败不影响其余（session 不存在按幂等成功
// 处理，deleteSession 通常对不存在的 key 也不抛错）。返回删除成功 / 失败清单供后端观测。
export const kResetSessionsWebhook: WebhookEntry<IRequestBody> = {
  name: "reset_sessions",
  action: async ({ api, payload }) => {
    const { sessionKeys, deleteTranscript = true } = payload ?? {};
    if (!Array.isArray(sessionKeys) || sessionKeys.length === 0) {
      return { status: "error", message: "sessionKeys must be a non-empty array" };
    }

    const result: ResetResult = { reset: [], failed: [] };
    for (const sessionKey of sessionKeys) {
      if (typeof sessionKey !== "string" || !sessionKey) {
        result.failed.push({
          sessionKey: String(sessionKey),
          error: "invalid sessionKey",
        });
        continue;
      }
      try {
        await api.runtime.subagent.deleteSession({ sessionKey, deleteTranscript });
        result.reset.push(sessionKey);
      } catch (err) {
        const error = err instanceof Error ? err.message : String(err);
        logger.warn(
          `[reset-sessions] deleteSession failed session=${sessionKey}: ${error}`,
        );
        result.failed.push({ sessionKey, error });
      }
    }

    logger.info(
      `[reset-sessions] reset=${result.reset.length} failed=${result.failed.length} deleteTranscript=${deleteTranscript}`,
    );
    return result;
  },
};
