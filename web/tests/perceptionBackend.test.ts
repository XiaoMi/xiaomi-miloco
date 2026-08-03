import { describe, expect, it } from "vitest";
import {
  buildSwitchPayload,
  healthLine,
  isReachable,
} from "@/lib/perceptionBackend";
import { cloudHintText } from "@/components/PerceptionBackendCard";
import type { PerceptionBackendState } from "@/lib/types";

function state(
  over: Partial<PerceptionBackendState> = {},
): PerceptionBackendState {
  return {
    backend: "cloud",
    local_vision: {
      base_url: "http://127.0.0.1:18800",
      has_token: false,
      window_size: 12,
      container_fps: 20,
      video_short_edge: null,
      codec_target_canvas: 28,
      cloud: { window_size: 4, video_short_edge: 512, omni_fps: 1 },
    },
    health: null,
    error: null,
    blocking_static_rules: [],
    cloud_hint: null,
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
    expect(buildSwitchPayload("cloud", "http://x", "tok")).toEqual({
      backend: "cloud",
    });
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
    expect(
      healthLine(state({ error: "unreachable", health: healthy })).kind,
    ).toBe("unreachable");
  });

  it("凭证被拒不能显示成绿灯", () => {
    // 边车在凭证不对时仍回 200 + model_loaded。只看 health 是否存在的话,界面
    // 一路绿灯而每一窗推理都在 401 —— 感知静默停摆且无从察觉。
    const s = state({
      health: { ...healthy, auth_required: true, auth_ok: false },
    });
    expect(healthLine(s).kind).toBe("auth-rejected");
  });

  it("要求凭证且通过时正常显示", () => {
    const s = state({
      health: { ...healthy, auth_required: true, auth_ok: true },
    });
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

describe("isReachable", () => {
  it("与探活结论同源:凭证被拒时不算可达", () => {
    // 曾经分开算过:按钮显示绿色「可达」,紧邻一行显示「✗ 边车拒绝当前凭证」。
    const s = state({
      health: { ...healthy, auth_required: true, auth_ok: false },
    });
    expect(isReachable(s)).toBe(false);
  });

  it("不可达时不算可达,哪怕 health 还留着上一次的快照", () => {
    expect(isReachable(state({ error: "unreachable", health: healthy }))).toBe(
      false,
    );
  });

  it("模型还在加载时不算可达", () => {
    // 断言字面值,不是"与实现一致"—— 后者是把实现抄一遍,任何实现都成立。
    // 此刻后端会以 400「正在加载模型」拒绝切换,按钮却显示绿色可达是自相矛盾。
    const s = state({
      health: { ...healthy, model_loaded: false, status: "loading" },
    });
    expect(isReachable(s)).toBe(false);
    expect(healthLine(s).kind).toBe("loading");
  });

  it("一切正常时可达", () => {
    expect(isReachable(state({ health: healthy }))).toBe(true);
  });
});

describe("加载失败 ≠ 还在加载", () => {
  it("有 load_error 时必须报失败,而不是劝人再等", () => {
    // 权重路径写错时边车会**永远**停在 loading(它刻意不崩进程)。显示成
    // 「稍后再试」等于让用户一直等下去,而真正的原因就在 load_error 里。
    const s = state({
      health: {
        ...healthy,
        model_loaded: false,
        load_error: "FileNotFoundError: /no/such/dir",
      },
    });
    const line = healthLine(s);
    expect(line.kind).toBe("load-failed");
    expect(line.kind === "load-failed" && line.detail).toContain(
      "/no/such/dir",
    );
    expect(isReachable(s)).toBe(false);
  });

  it("没有 load_error 才是真的在加载", () => {
    const s = state({ health: { ...healthy, model_loaded: false } });
    expect(healthLine(s).kind).toBe("loading");
  });
});

describe("云端就绪度提示不能直出后端中文", () => {
  // 后端那句是硬编码中文。直接渲染 → 英文界面上突然冒出一句中文。切换失败那条
  // 路径早就改成了 code + 前端查表(PB_CODE_KEY),cloud_hint 当时漏掉了。
  const t = (k: string) => `T(${k})`;

  it("按 code 查本地化文案,不用后端的 message", () => {
    const out = cloudHintText(
      {
        code: "cloud_no_api_key",
        message: "云端通路当前未配置多模态大模型 API Key",
      },
      t,
    );
    expect(out).toBe("T(perceptionBackend.codes.cloud_no_api_key)");
    expect(out).not.toContain("云端");
  });

  it("具体缺失项只有后端知道 —— 拼在本地化文案之后,不丢", () => {
    const out = cloudHintText(
      {
        code: "cloud_models_missing",
        message: "x",
        detail: "det_4C.onnx 不存在",
      },
      t,
    );
    expect(out).toContain("T(perceptionBackend.codes.cloud_models_missing)");
    expect(out).toContain("det_4C.onnx 不存在");
  });

  it("不认识的 code 回落到后端 message —— 契约对更新的后端开放,不该让提示整个消失", () => {
    expect(
      cloudHintText({ code: "brand_new_code", message: "后端说了什么" }, t),
    ).toBe("后端说了什么");
  });

  it("没有提示时渲染空串", () => {
    expect(cloudHintText(null, t)).toBe("");
  });
});
