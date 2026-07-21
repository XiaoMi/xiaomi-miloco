import type { IncomingMessage, ServerResponse } from "node:http";
import type { OpenClawPluginApi } from "openclaw/plugin-sdk";
import { getPluginConfig, type MilocoPluginConfig } from "../config.js";
import { logger } from "../utils/logger.js";
import { kAgentWebhook } from "./agent.js";
import { kGetTraceWebhook } from "./get_trace.js";
import { kResetSessionsWebhook } from "./reset_sessions.js";

// 已处理标记
export const ALREADY_HANDLED = "ALREADY_HANDLED";

const kWebhooks = [
  kAgentWebhook, // 向 Agent 发消息
  kGetTraceWebhook, // backend 反向取 agent turn 元数据
  kResetSessionsWebhook, // 切换家庭时批量重置 miloco session
].reduce(
  (acc, e) => {
    acc[e.name] = e.action;
    return acc;
  },
  {} as Record<string, WebhookEntry["action"]>,
);

export interface WebhookResponse<T = unknown> {
  code: number;
  message: string;
  data?: T;
}

function ok<T>(data?: T): WebhookResponse<T> {
  return { code: 0, message: "ok", data };
}

function fail(code: number, message: string): WebhookResponse {
  return { code, message };
}

// biome-ignore lint/suspicious/noExplicitAny: payload type is any
export type WebhookEntry<T = any> = {
  name: string;
  action: (ctx: {
    payload: T;
    api: OpenClawPluginApi;
    config: MilocoPluginConfig;
    req: IncomingMessage;
    res: ServerResponse<IncomingMessage>;
    // biome-ignore lint/suspicious/noExplicitAny: any is used to return the result
  }) => any;
};

function sendJson(
  res: ServerResponse,
  statusCode: number,
  body: WebhookResponse,
) {
  res.writeHead(statusCode, { "Content-Type": "application/json" });
  res.end(JSON.stringify(body), "utf-8");
}

export function registerHttpRoutes(api: OpenClawPluginApi) {
  const config = getPluginConfig(api);
  api.registerHttpRoute({
    path: "/miloco/webhook",
    auth: "gateway",
    match: "exact",
    handler: async (req, res) => {
      // --- 解析请求体 ---
      let action: string;
      // biome-ignore lint/suspicious/noExplicitAny: any is used to parse the body
      let payload: any;
      try {
        const body = await parseJsonBody<{
          action: string;
          // biome-ignore lint/suspicious/noExplicitAny: any is used to parse the body
          payload: any;
        }>(req);
        action = body.action;
        payload = body.payload;
      } catch {
        sendJson(res, 400, fail(1001, "Invalid JSON body"));
        return true;
      }

      if (!action) {
        sendJson(res, 400, fail(1001, "Missing action field"));
        return true;
      }

      logger.info(
        `🔥 call webhook action:${action} payload: ${JSON.stringify(payload)}`,
      );

      const webhook = kWebhooks[action];

      if (!webhook) {
        sendJson(res, 404, fail(2001, `Action '${action}' not found`));
        return true;
      }

      try {
        const result = await webhook({
          payload,
          api,
          config,
          req,
          res,
        });
        if (result !== ALREADY_HANDLED) {
          sendJson(res, 200, ok(result));
        }
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        logger.error(`webhook action '${action}' error: ${message}`);
        sendJson(res, 500, fail(3000, message));
      }

      return true;
    },
  });
}

function parseJsonBody<T>(req: IncomingMessage): Promise<T> {
  return new Promise((resolve, reject) => {
    let body = "";
    req.on("data", (chunk) => (body += chunk.toString()));
    req.on("end", () => {
      try {
        const json = JSON.parse(body || "{}");
        resolve(json);
      } catch {
        reject(new Error("parse json body failed"));
      }
    });
    req.on("error", reject);
  });
}
