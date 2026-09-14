/**
 * 契约测试 — 米家授权接口的返回体解析。
 *
 * 同账号重绑时后端保留家庭与摄像头配置，并在响应里带回 scope_preserved。
 * 这个字段一旦被丢弃，弹窗就会照旧跑选家流程——而切家是「唯一启用」语义
 * （加入目标、停用其余），一跑就把后端刚保住的白名单冲掉。多家庭账号上，
 * 住户为修授权而重绑，回来看到的是另一批设备。
 *
 * 不连真 backend：vi 拦截 fetch，伪造 NormalResponse 形状。
 */

import { describe, it, expect, vi, afterEach } from "vitest";
import { realAuthorizeMiot } from "@/api/real";

const origFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = origFetch;
  vi.restoreAllMocks();
});

function mockAuthorizeResponse(data: unknown) {
  globalThis.fetch = vi.fn(async () =>
    new Response(JSON.stringify({ code: 0, message: "ok", data }), {
      status: 200,
      headers: { "content-type": "application/json" },
    }),
  ) as unknown as typeof fetch;
}

describe("realAuthorizeMiot", () => {
  it("同账号重绑：把 scope_preserved 带回来", async () => {
    mockAuthorizeResponse({ account_changed: false, scope_preserved: true });
    const r = await realAuthorizeMiot("the_code", "the_state");
    expect(r.scopePreserved).toBe(true);
    expect(r.accountChanged).toBe(false);
  });

  it("换了账号：配置已被重置，不该报成保留", async () => {
    mockAuthorizeResponse({ account_changed: true, scope_preserved: false });
    const r = await realAuthorizeMiot("the_code", "the_state");
    expect(r.scopePreserved).toBe(false);
    expect(r.accountChanged).toBe(true);
  });

  it("老后端不返回这两个字段时按原行为走", async () => {
    // data 为 null。缺省必须是「换了账号、配置未保留」——于是照旧跑选家流程。
    // 反过来默认「已保留」就会静默跳过选家，新绑的账号一个家庭都没启用。
    mockAuthorizeResponse(null);
    const r = await realAuthorizeMiot("the_code", "the_state");
    expect(r.scopePreserved).toBe(false);
    expect(r.accountChanged).toBe(true);
  });

  it("字段缺一半时各自独立取缺省", async () => {
    mockAuthorizeResponse({ account_changed: false });
    const r = await realAuthorizeMiot("the_code", "the_state");
    expect(r.accountChanged).toBe(false);
    expect(r.scopePreserved).toBe(false);
  });
});
