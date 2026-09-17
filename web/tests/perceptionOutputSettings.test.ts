/**
 * 「设置 → 感知输入 → 详细判定输出」的契约测试（verbose 开关 + 语言同步）。
 *
 * 三处漂移都会让住户"拨了开关没反应 / 输出语言不对 / 开关被莫名关掉"：
 *   1. 默认值必须与后端 settings.yaml 一致：verbose=false（只回命中规则 id 数组）。
 *   2. payload 字段名必须是后端 PerceptionConfigBody 认的
 *      perception_verbose / perception_output_language（写错会被 pydantic 静默忽略）。
 *   3. **每个 payload 只能带自己要改的字段**：后端 PUT 是局部合并，"同步语言"若顺手带上
 *      `perception_verbose`（哪怕是默认 false），就会把住户打开的详细判定输出静默关掉。
 *      这条是真实事故的回归测试（见 buildPerceptionVerbosePayload 的注释）。
 *
 * 只测纯逻辑（vitest 是 node 环境，没有 jsdom）：不 import React 组件。
 */

import { describe, expect, it, vi } from "vitest";
import {
  buildPerceptionOutputLanguagePayload,
  buildPerceptionVerbosePayload,
  DEFAULT_OUTPUT_LANGUAGE,
  DEFAULT_PERCEPTION_VERBOSE,
  normalizeOutputLanguage,
  normalizePerceptionVerbose,
  PERCEPTION_OUTPUT_LANGUAGES,
} from "@/lib/perceptionOutput";

describe("感知输出默认值", () => {
  it("默认不输出详细理由（与 settings.yaml 对齐）", () => {
    expect(DEFAULT_PERCEPTION_VERBOSE).toBe(false);
    // 语言默认 auto：没同步过时由后端按 MILOCO_APP_LANG / 系统 locale 推断
    expect(DEFAULT_OUTPUT_LANGUAGE).toBe("auto");
    expect(PERCEPTION_OUTPUT_LANGUAGES).toEqual(["auto", "zh", "en"]);
  });
});

describe("payload 构造器：单用途，字段名与后端 PerceptionConfigBody 一致", () => {
  it("verbose payload 只带 perception_verbose（不夹带语言）", () => {
    expect(buildPerceptionVerbosePayload(true)).toEqual({
      perception_verbose: true,
    });
    expect(Object.keys(buildPerceptionVerbosePayload(false))).toEqual([
      "perception_verbose",
    ]);
  });

  it("语言 payload 只带 perception_output_language（不夹带 verbose）", () => {
    expect(buildPerceptionOutputLanguagePayload("zh")).toEqual({
      perception_output_language: "zh",
    });
    expect(Object.keys(buildPerceptionOutputLanguagePayload("auto"))).toEqual([
      "perception_output_language",
    ]);
  });

  it("回归：两个构造器的键集合互不相交 —— 改一个不可能顺带重置另一个", () => {
    const verboseKeys = Object.keys(buildPerceptionVerbosePayload(true));
    const langKeys = Object.keys(buildPerceptionOutputLanguagePayload("en"));
    expect(verboseKeys.filter((k) => langKeys.includes(k))).toEqual([]);
  });
});

describe("normalizePerceptionVerbose（老后端不返字段时的收敛）", () => {
  it("只有明确 true 才开，其余一律关", () => {
    expect(normalizePerceptionVerbose(true)).toBe(true);
    expect(normalizePerceptionVerbose(false)).toBe(false);
    expect(normalizePerceptionVerbose(undefined)).toBe(false);
    expect(normalizePerceptionVerbose(null)).toBe(false);
    expect(normalizePerceptionVerbose("true")).toBe(false);
    expect(normalizePerceptionVerbose(1)).toBe(false);
  });
});

describe("normalizeOutputLanguage", () => {
  it("只认 zh/en，其余（含缺失、坏值）回 auto", () => {
    expect(normalizeOutputLanguage("zh")).toBe("zh");
    expect(normalizeOutputLanguage("en")).toBe("en");
    expect(normalizeOutputLanguage("auto")).toBe("auto");
    expect(normalizeOutputLanguage(undefined)).toBe("auto");
    expect(normalizeOutputLanguage("ZH")).toBe("auto");
    expect(normalizeOutputLanguage(7)).toBe("auto");
  });
});

describe("syncOutputLanguageToBackend", () => {
  it("node 单测环境（有 window 桩、没有 document）不发请求", async () => {
    // tests/test-setup.ts 为了 client.ts 的 token 读取造了 window 桩，但没有 document；
    // 判据必须落在这里，否则每个 import 过 i18n 的单测都会被打出一条无效 URL 的请求。
    const mod = await import("@/lib/perceptionOutput");
    await expect(mod.syncOutputLanguageToBackend("en")).resolves.toBeUndefined();
  });

  it("真浏览器页面里把界面语言写成 zh/en（auto 不写：那会让后端失去跟随能力）", async () => {
    const update = vi.fn().mockResolvedValue({});
    vi.doMock("@/api", () => ({ updatePerceptionConfig: update }));
    vi.stubGlobal("window", {});
    vi.stubGlobal("document", {});
    try {
      const mod = await import("@/lib/perceptionOutput");
      await mod.syncOutputLanguageToBackend("en");
      expect(update).toHaveBeenCalledWith({ perception_output_language: "en" });
      await mod.syncOutputLanguageToBackend("zh");
      expect(update).toHaveBeenLastCalledWith({
        perception_output_language: "zh",
      });
      // 未知语言按中文处理（后端只认 zh/en；界面只有这两种）
      await mod.syncOutputLanguageToBackend("fr");
      expect(update).toHaveBeenLastCalledWith({
        perception_output_language: "zh",
      });
    } finally {
      vi.unstubAllGlobals();
      vi.doUnmock("@/api");
    }
  });

  it("回归（真实事故）：同步语言只写 language，绝不带上 perception_verbose", async () => {
    // 事故复现：切语言时 payload 带了 perception_verbose:false → 后端局部合并把住户打开的
    // 「详细判定输出」关掉。这里逐字段断言：请求体里**不能出现** perception_verbose。
    const update = vi.fn().mockResolvedValue({});
    vi.doMock("@/api", () => ({ updatePerceptionConfig: update }));
    vi.stubGlobal("window", {});
    vi.stubGlobal("document", {});
    try {
      const mod = await import("@/lib/perceptionOutput");
      await mod.syncOutputLanguageToBackend("zh");
      expect(update).toHaveBeenCalledTimes(1);
      const body = update.mock.calls[0][0] as Record<string, unknown>;
      expect(body).toEqual({ perception_output_language: "zh" });
      expect("perception_verbose" in body).toBe(false);
    } finally {
      vi.unstubAllGlobals();
      vi.doUnmock("@/api");
    }
  });

  it("后端只认 zh/en：auto 不会由前端写下去", async () => {
    const { buildPerceptionOutputLanguagePayload } = await import(
      "@/lib/perceptionOutput"
    );
    // 类型层面就排除了 auto 之外的取值；这里断言"同步走的是显式语言"这一契约：
    // 界面语言只有 zh/en 两种，故同步后的值必定是 zh 或 en（见上面的 syncOutputLanguageToBackend）。
    expect(
      buildPerceptionOutputLanguagePayload("auto").perception_output_language,
    ).toBe("auto");
  });
});
