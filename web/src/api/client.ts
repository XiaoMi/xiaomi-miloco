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
  ) {
    super(message);
  }
}

/**
 * 从错误响应体里取住户可读的文案。
 *
 * FastAPI 的 HTTPException(detail=...) 在 detail 是字符串时原样返回；omni preflight
 * 失败时 detail 是对象（{code,message,...}）。旧写法把整个 detail 塞进 Error，String()
 * 后只剩 "[object Object]"，住户看不到失败原因。这里只取其中的 message 字符串。
 *
 * 导出给绕开 apiFetch 的 multipart 直连 fetch 复用，避免各处再手写一份。
 */
export function errorMessage(body: unknown): string | null {
  if (typeof body === "string") return body || null;
  if (!body || typeof body !== "object") return null;
  const b = body as { message?: unknown; detail?: unknown };
  if (typeof b.message === "string" && b.message) return b.message;
  if (typeof b.detail === "string") return b.detail || null;
  if (b.detail && typeof b.detail === "object") {
    const m = (b.detail as { message?: unknown }).message;
    if (typeof m === "string" && m) return m;
  }
  return null;
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
    try {
      msg = errorMessage(await resp.json()) ?? msg;
    } catch {
      // ignore
    }
    throw new ApiError(resp.status, msg);
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
    throw new ApiError(resp.status, body.message || i18n.t("api.bizError", { code: body.code }));
  }
  return body as T;
}
