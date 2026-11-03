/**
 * 用户可见术语与设备分组初始状态的回归约束。
 *
 * 组件测试环境目前不含 DOM renderer，因此设备分组把展开判定抽为纯函数测试。
 * 统计文案直接检查中英文 i18n 资源，确保普通用户标题与开发者精确口径并存。
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { formatRelativeDuration } from "@/components/PerfKpiCards";
import { isRoomOpen } from "@/components/DevicesByRoom";

const perfInlinePath = fileURLToPath(
  new URL("../src/components/PerfInline.tsx", import.meta.url),
);
const perfRtfChartPath = fileURLToPath(
  new URL("../src/components/PerfRtfChart.tsx", import.meta.url),
);
const zhPerfPath = fileURLToPath(
  new URL("../src/i18n/locales/zh/perf.json", import.meta.url),
);
const enPerfPath = fileURLToPath(
  new URL("../src/i18n/locales/en/perf.json", import.meta.url),
);

describe("设备房间分组", () => {
  it("所有房间初始收起，仍允许用户逐个展开", () => {
    expect(isRoomOpen({}, "客厅")).toBe(false);
    expect(isRoomOpen({ 客厅: true }, "客厅")).toBe(true);
    expect(isRoomOpen({ 客厅: false }, "客厅")).toBe(false);
  });
});

describe("性能总览文案", () => {
  const perf = JSON.parse(readFileSync(zhPerfPath, "utf8")).perf as Record<
    string,
    string
  >;
  const enPerf = JSON.parse(readFileSync(enPerfPath, "utf8")).perf as Record<
    string,
    string
  >;

  it("首层 KPI 使用普通用户能理解的名称", () => {
    expect(perf.kpiCycle).toBe("已处理轮次");
    expect(perf.kpiShouldProcess).toBe("收到窗口");
    expect(enPerf.kpiCycle).toBe("Processed cycles");
    expect(enPerf.kpiShouldProcess).toBe("Windows received");
    expect(perf.kpiGateFilterRate).toBe("无需深度分析");
    expect(perf.kpiDropRate).toBe("未进入实时处理");
    expect(perf.kpiOmniErrorRate).toBe("AI 分析失败率");
    expect(perf.kpiRtfP95).toBe("整体处理相对耗时");
    expect(perf.kpiOmniRtfP95).toBe("AI 分析相对耗时");
    expect(perf.kpiAgentCall).toBe("触发后续任务");
  });

  it("说明保留可核对的技术指标名称和窗口调度口径", () => {
    expect(perf.kpiGateFilterRateHint).toContain("Gate 过滤率");
    expect(perf.kpiDropRateHint).toContain("窗口丢弃率");
    expect(perf.kpiDropRateHint).toContain("较旧片段");
    expect(perf.kpiDropRateHint).toContain("积压片段");
    expect(perf.kpiOmniErrorRateHint).toContain("Omni 错误率");
    expect(perf.kpiRtfP95Hint).toContain("P95");
    expect(perf.kpiRtfP95Hint).toContain("低于 1");
    expect(perf.kpiOmniRtfP95Hint).toContain("P95");
    expect(perf.kpiAgentCallHint).toContain("Agent 调用");
  });

  it("相对耗时显示倍数，零样本不伪装成零耗时", () => {
    expect(formatRelativeDuration(0)).toBe("—");
    expect(formatRelativeDuration(0.03)).toBe("0.03×");
    expect(formatRelativeDuration(1)).toBe("1.00×");

    const inlineSource = readFileSync(perfInlinePath, "utf8");
    expect(inlineSource).toContain("formatRelativeDuration(omniP95)");
    expect(inlineSource).not.toContain("omniP95.toFixed");

    const chartSource = readFileSync(perfRtfChartPath, "utf8");
    expect(chartSource).toContain("formatRelativeDuration(v)");
    expect(chartSource).not.toContain("v.toFixed(2)");
  });

  it("英文说明同样保留可核对的技术指标名称", () => {
    expect(enPerf.kpiGateFilterRateHint).toContain("Gate filter rate");
    expect(enPerf.kpiDropRateHint).toContain("window drop rate");
    expect(enPerf.kpiOmniErrorRateHint).toContain("Omni error rate");
    expect(enPerf.kpiRtfP95Hint).toContain("RTF P95");
    expect(enPerf.kpiOmniRtfP95Hint).toContain("Omni RTF P95");
    expect(enPerf.kpiAgentCallHint).toContain("Agent calls");
  });
});
