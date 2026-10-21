/**
 * 米家 OAuth 回调页给的 base64 payload 解析单测。
 *
 * 用户从 mico.api.mijia.tech/login_redirect 复制的字符串本质是
 * `base64(JSON.stringify({code, state}))`。Python cli 端有相同逻辑
 * （cli/src/miloco_cli/commands/account.py::_parse_auth_payload），
 * 这里覆盖前端版本的边界 case。
 */

import { describe, it, expect } from "vitest";
import { parsePayload } from "@/components/MiotBindDialog";

function makePayload(obj: object): string {
  return btoa(JSON.stringify(obj));
}

describe("parsePayload — 米家 OAuth base64 payload 解析", () => {
  it("合法 payload → 返 {code, state} 并 trim", () => {
    const raw = makePayload({ code: "abc123", state: "xyz789" });
    const out = parsePayload(raw);
    expect(out).toEqual({ code: "abc123", state: "xyz789" });
  });

  it("payload 前后含空白也能解（用户 copy-paste 容易带换行）", () => {
    const raw = makePayload({ code: "abc", state: "xyz" });
    const out = parsePayload(`  \n${raw}\n  `);
    expect(out).toEqual({ code: "abc", state: "xyz" });
  });

  it("空字符串 → 错误「请先复制授权码」", () => {
    expect(parsePayload("")).toEqual({
      error: "请先复制授权码再粘贴",
    });
    expect(parsePayload("   ")).toEqual({
      error: "请先复制授权码再粘贴",
    });
  });

  it("非合法 base64 → 错误「不是合法的 base64」", () => {
    const out = parsePayload("not!base64@@@");
    expect(out).toHaveProperty("error");
    expect((out as { error: string }).error).toContain("授权码不对");
  });

  it("base64 解出来不是 JSON → 错误「内容解析失败」", () => {
    const raw = btoa("just plain text not json");
    const out = parsePayload(raw);
    expect(out).toHaveProperty("error");
    expect((out as { error: string }).error).toContain("读不出来");
  });

  it("JSON 缺 code 字段 → 错误「缺少 code 或 state」", () => {
    const raw = makePayload({ state: "xyz" });
    const out = parsePayload(raw);
    expect(out).toEqual({ error: "授权码不完整，请重新复制" });
  });

  it("JSON 缺 state 字段 → 错误「缺少 code 或 state」", () => {
    const raw = makePayload({ code: "abc" });
    const out = parsePayload(raw);
    expect(out).toEqual({ error: "授权码不完整，请重新复制" });
  });

  it("JSON code 字段是数字（类型错误）→ 错误「缺少 code 或 state」", () => {
    const raw = makePayload({ code: 123, state: "xyz" });
    const out = parsePayload(raw);
    expect(out).toEqual({ error: "授权码不完整，请重新复制" });
  });

  it("JSON code 是空字符串 → 错误「code 或 state 为空」", () => {
    const raw = makePayload({ code: "  ", state: "xyz" });
    const out = parsePayload(raw);
    expect(out).toEqual({ error: "授权码不完整，请重新复制" });
  });

  it("JSON state 是空字符串 → 错误「code 或 state 为空」", () => {
    const raw = makePayload({ code: "abc", state: "" });
    const out = parsePayload(raw);
    expect(out).toEqual({ error: "授权码不完整，请重新复制" });
  });

  it("真实小米 OAuth 回调 payload 结构（含 union_id 等多余字段也 OK）", () => {
    // 实际线上 payload 可能含 mijia 自加字段，前端只要 code/state 就够
    const raw = makePayload({
      code: "real_oauth_code_xxxxx",
      state: "d71cead5eff7b1309dea4265423f93bc37db0179",
      union_id: "WhvJ3lC_10gI9yQwwn3swNz5vFVPCJ8m8OtkF16w",
    });
    const out = parsePayload(raw);
    expect(out).toEqual({
      code: "real_oauth_code_xxxxx",
      state: "d71cead5eff7b1309dea4265423f93bc37db0179",
    });
  });
});
