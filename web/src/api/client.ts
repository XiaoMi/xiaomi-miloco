/** fetch 包装：自动注入 Bearer token；统一错误。 */

import i18n from "@/i18n";

declare global {
  interface Window {
    __MILOCO_TOKEN__?: string;
  }
}

export function resolveToken(): string {
  const injected = window.__MILOCO_TOKEN__;
  // 未被 backend SPA handler 注入时还是占位字面量 "__MILOCO_INJECT_TOKEN_HERE__"，
  // 走 guard 返空串避免把假 token 当真用（fetch 也就不带 Authorization 头）。
  // 用宽前缀 "__MILOCO_" 而不是只挡 "__MILOCO_INJECT_"——backend token 是
  // uuid.uuid4() 生成（hex+dash），永远不会以 __MILOCO_ 打头；多挡一层防止旧版
  // 字面量 "__MILOCO_TOKEN__"（旧 placeholder）若意外残留也会被识别。
  if (injected && !injected.startsWith("__MILOCO_")) return injected;
  return "";
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    /** 后端给的机器可读错误码(detail 为对象时的 `code`)。
     *  按它查本地化文案 —— backend message 是硬编码中文,直接注入会污染英文界面。 */
    public code?: string,
    /** 结构化 detail 里除 code/message 之外的字段,原样带出。
     *
     *  只有 code 是不够的:有些错误要附带**数据**才说得清楚(如 blocking_rules
     *  的 `rules` 数组是受影响的规则名)。而 message 是中文整句,拿它去拼就等于
     *  把中文注进界面 —— 正是 code 这套机制要避免的。带上纯数据字段,调用方就能
     *  用本地化的句子 + 后端的数据自己拼。 */
    public data?: Record<string, unknown>,
  ) {
    super(message);
  }
}

export async function apiFetch<T>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  const token = resolveToken();
  const headers = new Headers(init?.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init?.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  const resp = await fetch(path, { ...init, headers });
  if (!resp.ok) {
    let msg = `HTTP ${resp.status}`;
    let code: string | undefined;
    let data: Record<string, unknown> | undefined;
    try {
      const body = await resp.json();
      // FastAPI 的 detail 既可能是字符串,也可能是结构化对象
      // (`HTTPException(400, detail={"code":…, "message":…})`,见 admin/router)。
      // 不解包的话,对象会被 String() 成 "[object Object]" 塞进 Error.message ——
      // 用户看到的就是这七个字。omni 的 PUT 早就走在这条路上。
      const detail = body.detail;
      if (detail && typeof detail === "object") {
        code = typeof detail.code === "string" ? detail.code : undefined;
        msg = typeof detail.message === "string" ? detail.message : msg;
        // eslint-disable-next-line @typescript-eslint/no-unused-vars
        const { code: _c, message: _m, ...rest } = detail;
        data = Object.keys(rest).length ? rest : undefined;
      } else {
        msg = body.message ?? detail ?? msg;
      }
    } catch {
      // ignore
    }
    throw new ApiError(resp.status, msg, code, data);
  }
  // backend NormalResponse 业务错(HTTP 200 但 body.code != 0)也当错处理。
  // 当前 backend 全走 HTTPException → handle_exception → 4xx,没用 200+code != 0
  // 这种约定。这条防御为前置兼容层 — 未来若引入 200 业务错码不漏。
  // resp.json() 解析失败：捕获后包成 ApiError,避免把原生 SyntaxError 透给调用方
  // (家用路由 captive portal 兜底页 / nginx 加 banner / 网络注入 等场景下,
  // backend 返 200 但 body 不是 JSON,toast 直接显英文 "Unexpected token < in JSON
  // at position 0" 住户看不懂)。
  let body: T & { code?: number; message?: string };
  try {
    body = (await resp.json()) as T & { code?: number; message?: string };
  } catch {
    throw new ApiError(resp.status, i18n.t("api.invalidJson"));
  }
  if (typeof body.code === "number" && body.code !== 0) {
    // `||` 而非 `??`：?? 只挡 null/undefined,空串 "" 也是合法 message,但住户看到
    // "" 跟"无错误"无法区分,需要用 ?code? 兜底让住户至少看到 code 编码。
    throw new ApiError(
      resp.status,
      body.message || i18n.t("api.bizError", { code: body.code }),
    );
  }
  return body as T;
}
