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
 *   6.4 进程 CPU/线程数(PerfProcChart)  — /api/monitor/proc/series
 *   6.5 进程内存(PerfMemoryChart)       — /api/monitor/memory + /series
 *   7. 最近 Agent 调用(PerfAgentList)    — /api/traces?has_agent=1
 *   8. 近期处理耗时(PerfTraceTimingChart)— latency_percentiles
 *   9. 原始 trace 列表(PerfTraceList)   — /api/traces
 *
 * 直接接 backend observability 真接口,不走 mock。空数据时各子区块自行降级显示。
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  getProcSeries,
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
import { PerfProcChart } from "./PerfProcChart";
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
import { RefreshIntervalInput } from "./RefreshIntervalInput";
import { useRefreshInterval } from "@/hooks/useRefreshInterval";

export function PerfPage() {
  const { t, i18n } = useTranslation();
  // 窗口选项随语言重算;memo 在 i18n.language 不变时保持引用稳定。
  const windows = useMemo(() => perfWindows(), [i18n.language]);
  const [windowKey, setWindow] = useState<PerfWindow>("1h");
  const { sec: refreshSec, setSec: setRefreshSec } = useRefreshInterval();
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
  const procSeries = useAsync(
    () => getProcSeries(windowKey, bucket),
    [windowKey, bucket],
    { errorLabel: t("perf.errProcSeries") },
  );
  // uname 是进程级静态信息，api 层模块级缓存，整 app 仅请求一次
  const [uname, setUname] = useState<string | undefined>();
  useEffect(() => {
    getUname().then(setUname).catch(() => {});
  }, []);

  // 用 ref 持住最新那份：reloadAll 每次渲染都是新函数，而下面的 effect 只跟窗口与周期
  // 走（否则定时器逐帧重建）。直接闭包引用的话，定时器与可视性回调都会一直调首帧那份
  // ——眼下各 reload 的依赖都由 windowKey 派生、行为恰好一致，但那是条没写下来的前提，
  // 而性能小卡那侧本来就是 ref 写法，两页同一个问题不该两套解法。
  const reloadAllRef = useRef<() => void>(() => {});
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
    procSeries.reload();
  };
  reloadAllRef.current = reloadAll;

  // 自动刷新周期由住户设定，与「模型」页共用同一个值（见 useRefreshInterval：一处改、
  // 两处生效，跨标签页也同步）。此前这里写死 30 秒，于是那个设置对这一整页不起作用——
  // 设成 5 秒这里仍是 30 秒，设成十分钟这里照旧每 30 秒打十余个接口。窗口切换或周期变化
  // 都重置 timer。
  useEffect(() => {
    const id = setInterval(() => reloadAllRef.current(), refreshSec * 1000);
    // 切回前台补刷一次，与用量卡、性能小卡同一个理由：后台标签页的定时器会被浏览器
    // 节流乃至冻结，切回来时不补就要等下一个周期到点。周期从写死 30 秒改成可设（上限
    // 24 小时）之后这条才真正要紧——原先最多慢几十秒，现在可能长时间停在旧数，而这
    // 一页十几张图正是用来判断感知跟不跟得上的。
    const onVisible = () => {
      if (document.visibilityState === "visible") reloadAllRef.current();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      clearInterval(id);
      document.removeEventListener("visibilitychange", onVisible);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [windowKey, refreshSec]);

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
          <RefreshIntervalInput sec={refreshSec} onChange={setRefreshSec} />
          <button
            type="button"
            onClick={reloadAll}
            className="text-caption px-3 py-1.5 rounded-md border border-border text-text-secondary hover:text-text-primary hover:border-border-strong transition-colors"
          >
            {t("common.refresh")}
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

      {/* 6.4 进程 CPU 占用 + 线程数时序，与 perf 因果链解耦的运行时观察项 */}
      <PerfProcChart seriesState={procSeries} bucket={bucket} windowMs={windowMs} />

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
