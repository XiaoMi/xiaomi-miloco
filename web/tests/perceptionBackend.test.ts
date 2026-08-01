import { describe, expect, it } from "vitest";
import { buildSwitchPayload, healthLine } from "@/lib/perceptionBackend";
import type { PerceptionBackendState } from "@/lib/types";

function state(over: Partial<PerceptionBackendState> = {}): PerceptionBackendState {
  return {
    backend: "cloud",
    local_vision: { base_url: "http://127.0.0.1:18800", has_token: false },
    health: null,
    error: null,
    blocking_static_rules: [],
    cloud_hint: "",
    local_capabilities: {
      needs_api_key: false,
      audio: false,
      identity: false,
      suggestions: false,
      static_rule_execution: false,
    },
    ...over,
  };
}

const healthy = {
  status: "ok",
  model_loaded: true,
  gate_available: true,
  gate_error: null,
  device: "cuda:0",
  backend: "codec",
};

describe("buildSwitchPayload", () => {
  it("切回云端时不带地址与凭证", () => {
    expect(buildSwitchPayload("cloud", "http://x", "tok")).toEqual({ backend: "cloud" });
  });

  it("留空的凭证不提交 —— 空串会被后端当成「清空已存凭证」", () => {
    const p = buildSwitchPayload("local", "http://x ", "   ");
    expect(p).toEqual({ backend: "local", base_url: "http://x" });
    expect("token" in p).toBe(false);
  });

  it("填了才带上,且去掉首尾空白", () => {
    expect(buildSwitchPayload("local", " http://x ", " tok ")).toEqual({
      backend: "local",
      base_url: "http://x",
      token: "tok",
    });
  });
});

describe("healthLine", () => {
  it("不可达优先于一切", () => {
    expect(healthLine(state({ error: "unreachable", health: healthy })).kind).toBe(
      "unreachable",
    );
  });

  it("凭证被拒不能显示成绿灯", () => {
    // 边车在凭证不对时仍回 200 + model_loaded。只看 health 是否存在的话,界面
    // 一路绿灯而每一窗推理都在 401 —— 感知静默停摆且无从察觉。
    const s = state({ health: { ...healthy, auth_required: true, auth_ok: false } });
    expect(healthLine(s).kind).toBe("auth-rejected");
  });

  it("要求凭证且通过时正常显示", () => {
    const s = state({ health: { ...healthy, auth_required: true, auth_ok: true } });
    expect(healthLine(s)).toEqual({
      kind: "ok",
      device: "cuda:0",
      backend: "codec",
      gateOff: false,
    });
  });

  it("门控不可用时标出来,但不影响可用性判断", () => {
    const s = state({ health: { ...healthy, gate_available: false } });
    expect(healthLine(s)).toMatchObject({ kind: "ok", gateOff: true });
  });

  it("没探到就什么都不显示,而不是假装正常", () => {
    expect(healthLine(state()).kind).toBe("none");
  });
});

describe("边界形状", () => {
  it("切回云端不带地址 —— 带上会触发强制探活,把唯一的退路也堵死", () => {
    const p = buildSwitchPayload("cloud", "http://typed-but-unsaved", "tok");
    expect(p).toEqual({ backend: "cloud" });
  });

  it("边车只回了最小字段时不崩,也不假装门控可用", () => {
    // _sanitize_health 只拷贝边车真的返回了的键,所以一个最小实现会让这四个
    // 字段全部缺席。契约对第三方实现开放,这种形状必须能正常渲染。
    const s = state({
      health: { status: "ok", model_loaded: true } as never,
    });
    expect(healthLine(s)).toEqual({
      kind: "ok",
      device: "",
      backend: "",
      gateOff: true,
    });
  });
});
