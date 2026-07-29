/**
 * 性能 tab 主页面容器。
 *
 * 顶部:窗口切换(1h/6h/24h/3d) + 手动刷新。下方按因果顺序排版:
 *   1. KPI 卡(PerfKpiCards)             — summary
 *   2. 实时率时序(PerfRtfChart)         — rtf_series (含 e2e 双线对比)
 *   3. Gate 过滤率(PerfGateChart)       — gate_pass_rate
 *   4. Omni 错误时序(PerfOmniErrorChart)— omni_error_series
 *   5. 窗口丢弃数(PerfDropChart)        — drop_series
 *   6. 阶段耗时分布(PerfStageTable)     — stage_percentiles
 *   7. 最近 Agent 调用(PerfAgentList)    — /api/traces?has_agent=1
 *   8. 近期处理耗时(PerfTraceTimingChart)— latency_percentiles
 *   9. 原始 trace 列表(PerfTraceList)   — /api/traces
 *
 * 直接接 backend observability 真接口,不走 mock。空数据时各子区块自行降级显示。
 */

import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  getCpuSeries,
  getMemorySeries,
  getMemorySnapshot,
  getUname,
  getPerfDropSeries,
  getPerfGatePassRate,
  getPerfGateScorePercentiles,
  getPerfLatencyPercentiles,
  getPerfOmniErrorSeries,
  getPerfRtfSeries,
  getPerfStagePercentiles,
  getPerfSummary,
  listCameras,
  listPerfAgentRuns,
  listPerfTraces,
} from "@/api";
import { useAsync } from "@/hooks/useAsync";
import { WINDOW_MS, perfWindows, defaultBucket } from "@/lib/perfBucket";
import type { PerfTraceRow, PerfWindow } from "@/lib/types";

/** 算最近 trace 的 window_duration_ms 均值,给折线图当包长度参考线。 */
function avgWindowDuration(rows: PerfTraceRow[]): number | undefined {
  const vals = rows
    .map((r) => r.window_duration_ms)
    .filter((v): v is number => v != null && v > 0);
  if (vals.length === 0) return undefined;
  return vals.reduce((s, v) => s + v, 0) / vals.length;
}
import { PerfAgentList } from "./PerfAgentList";
import { PerfCpuChart } from "./PerfCpuChart";
import { PerfKpiCards } from "./PerfKpiCards";
import { PerfMemoryChart } from "./PerfMemoryChart";
import { PerfOmniErrorChart } from "./PerfOmniErrorChart";
import { PerfRtfChart } from "./PerfRtfChart";
import { PerfDropChart } from "./PerfDropChart";
import { PerfGateChart } from "./PerfGateChart";
import { PerfGateScoreTable } from "./PerfGateScoreTable";
import { PerfStageTable } from "./PerfStageTable";
import { PerfTraceList } from "./PerfTraceList";
import { PerfTraceTimingChart } from "./PerfTraceTimingChart";

export function PerfPage() {
  const { t, i18n } = useTranslation();
  // 窗口选项随语言重算;memo 在 i18n.language 不变时保持引用稳定。
  const windows = useMemo(() => perfWindows(), [i18n.language]);
  const [windowKey, setWindow] = useState<PerfWindow>("1h");
  const bucket = defaultBucket(windowKey);
  const windowMs = WINDOW_MS[windowKey];

  // 每个子区块独立 useAsync;窗口切换时 deps 变化自动重拉。
  const summary = useAsync(
    () => getPerfSummary(windowKey),
    [windowKey],
    { errorLabel: t("perf.errSummary") },
  );
  const rtf = useAsync(
    () => getPerfRtfSeries(windowKey, bucket),
    [windowKey, bucket],
    { errorLabel: t("perf.errRtfSeries") },
  );
  const stages = useAsync(
    () => getPerfStagePercentiles(windowKey),
    [windowKey],
    { errorLabel: t("perf.errStage") },
  );
  const traces = useAsync(
    () => listPerfTraces(windowKey, 20),
    [windowKey],
    { errorLabel: t("perf.errTraceList") },
  );
  const latency = useAsync(
    () => getPerfLatencyPercentiles(windowKey, bucket),
    [windowKey, bucket],
    { errorLabel: t("perf.errLatency") },
  );
  const gate = useAsync(
    () => getPerfGatePassRate(windowKey, bucket),
    [windowKey, bucket],
    { errorLabel: t("perf.errGate") },
  );
  const gateScores = useAsync(
    () => getPerfGateScorePercentiles(windowKey),
    [windowKey],
    { errorLabel: t("perf.errGateScore") },
  );
  // device_id → friendly name 映射,给 PerfGateScoreTable 用。failed/empty 时
  // 表格降级显示 device_id。摄像头列表跨时间窗口不变,不绑 [windowKey] deps。
  const cameras = useAsync(() => listCameras(), [], {
    errorLabel: t("perf.errCameras"),
  });
  const drop = useAsync(
    () => getPerfDropSeries(windowKey, bucket),
    [windowKey, bucket],
    { errorLabel: t("perf.errDrop") },
  );
  const omniErr = useAsync(
    () => getPerfOmniErrorSeries(windowKey, bucket),
    [windowKey, bucket],
    { errorLabel: t("perf.errOmni") },
  );
  const agentRuns = useAsync(
    () => listPerfAgentRuns(windowKey, 50),
    [windowKey],
    { errorLabel: t("perf.errAgentRuns") },
  );
  const memSnapshot = useAsync(
    () => getMemorySnapshot(),
    [],
    { errorLabel: t("perf.errMemSnapshot") },
  );
  const memSeries = useAsync(
    () => getMemorySeries(windowKey, bucket),
    [windowKey, bucket],
    { errorLabel: t("perf.errMemSeries") },
  );
  const cpuSeries = useAsync(
    () => getCpuSeries(windowKey, bucket),
    [windowKey, bucket],
    { errorLabel: t("perf.errCpuSeries") },
  );
  // uname 是进程级静态信息，api 层模块级缓存，整 app 仅请求一次
  const [uname, setUname] = useState<string | undefined>();
  useEffect(() => {
    getUname().then(setUname).catch(() => {});
  }, []);

  const reloadAll = () => {
    summary.reload();
    rtf.reload();
    stages.reload();
    traces.reload();
    latency.reload();
    gate.reload();
    gateScores.reload();
    drop.reload();
    omniErr.reload();
    agentRuns.reload();
    memSnapshot.reload();
    memSeries.reload();
    cpuSeries.reload();
  };

  // 30s 自动刷新。窗口切换会重置 timer。
  useEffect(() => {
    const id = setInterval(reloadAll, 30_000);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [windowKey]);

  return (
    <div className="space-y-6">
      {/* 窗口切换 + 刷新 */}
      <section
        className="rounded-xl bg-bg-secondary border border-border shadow-sm p-4 flex items-center justify-between flex-wrap gap-3"
        aria-label={t("perf.windowAria")}
      >
        <div className="flex gap-1 bg-bg-primary rounded-lg p-1" role="tablist">
          {windows.map((w) => {
            const on = windowKey === w.key;
            return (
              <button
                key={w.key}
                type="button"
                role="tab"
                aria-selected={on}
                onClick={() => setWindow(w.key)}
                className={`text-caption px-3 py-1 rounded-lg transition-colors ${
                  on
                    ? "bg-bg-secondary text-text-primary shadow-sm"
                    : "text-text-secondary hover:text-text-primary"
                }`}
              >
                {w.label}
              </button>
            );
          })}
        </div>
        <div className="flex items-center gap-3">
          <span className="text-caption text-text-tertiary">{t("perf.autoRefresh")}</span>
          <button
            type="button"
            onClick={reloadAll}
            className="text-caption px-3 py-1.5 rounded-md border border-border text-text-secondary hover:text-text-primary hover:border-border-strong transition-colors"
          >
            {t("perf.manualRefresh")}
          </button>
        </div>
      </section>

      {/* 1. KPI 卡 */}
      <PerfKpiCards state={summary} />

      {/* 2. RTF 时间序列 */}
      <PerfRtfChart state={rtf} bucket={bucket} windowMs={windowMs} />

      {/* 3. Gate 过滤率时间序列 + 打分分布 */}
      <PerfGateChart state={gate} bucket={bucket} windowMs={windowMs} />
      <PerfGateScoreTable state={gateScores} cameras={cameras.data ?? []} />

      {/* 4. Omni 错误时序(放窗口丢弃上方,因果相关挨着看) */}
      <PerfOmniErrorChart state={omniErr} bucket={bucket} windowMs={windowMs} />

      {/* 5. 窗口丢弃数(柱状图,绝对值) */}
      <PerfDropChart state={drop} bucket={bucket} windowMs={windowMs} />

      {/* 6. 阶段耗时分布 */}
      <PerfStageTable state={stages} />

      {/* 6.4 进程 CPU 占用时序，与 perf 因果链解耦的运行时观察项 */}
      <PerfCpuChart seriesState={cpuSeries} bucket={bucket} windowMs={windowMs} />

      {/* 6.5 进程内存（smaps + py_heap），与 perf 因果链解耦的运行时观察项 */}
      <PerfMemoryChart
        seriesState={memSeries}
        snapshotState={memSnapshot}
        bucket={bucket}
        windowMs={windowMs}
        uname={uname}
      />

      {/* 7. 最近 Agent 调用(指令 / 耗时 / LLM-Tool 次数) */}
      <PerfAgentList state={agentRuns} windowMs={windowMs} />

      {/* 8. 近期处理耗时(按 bucket 聚合 P50/P75/P95/P99) */}
      <PerfTraceTimingChart
        state={latency}
        bucket={bucket}
        windowMs={windowMs}
        windowMsRef={
          traces.data && traces.data.length > 0
            ? avgWindowDuration(traces.data)
            : undefined
        }
      />

      {/* 9. trace 列表 */}
      <PerfTraceList state={traces} windowMs={windowMs} />
    </div>
  );
}
