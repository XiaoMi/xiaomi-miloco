/**
 * 近期处理耗时折线图。数据源:/api/stats?metric=latency_percentiles
 *
 * 按 bucket 聚合 cycle_total_ms 的 P50 / P75 / P95 / P99 四条线,覆盖整个时间窗口。
 * 跟处理耗时折线相同的 bucket 粒度(1h→5m,24h→1h),抽样自然均匀。
 * 红虚线 = 1 个窗口长度(常 3000ms)的参考线,超出即"单 cycle 处理 > 一个窗口"。
 *
 * 最右端如果落在还没结束的 bucket 上,改成虚线 + 半透明画出,提示该点仍在累积。
 */

import { Fragment, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import type { TFunction } from "i18next";
import type { AsyncState } from "@/hooks/useAsync";
import {
  densifyByBucket,
  findGapRegions,
  formatPerfTs,
  splitClosedPending,
} from "@/lib/perfBucket";
import type { PerfBucket, PerfLatencyPoint } from "@/lib/types";
import { ChartGapOverlay } from "./ChartGapOverlay";

interface Props {
  state: AsyncState<PerfLatencyPoint[]>;
  bucket: PerfBucket;
  /** 时间窗口跨度(ms),用于把稀疏 bucket 列表补齐成等距时间序列。 */
  windowMs: number;
  /** 包长度均值参考线(ms)。来自 trace 列表中的 window_duration_ms 均值,
   *  或者 backend 配置(常 3000)。可选,不传则不画参考线。 */
  windowMsRef?: number;
}

const LATENCY_EMPTY = (ts: number): PerfLatencyPoint => ({
  ts,
  p50: null,
  p75: null,
  p95: null,
  p99: null,
});

type LineKey = "p50" | "p75" | "p95" | "p99";

interface LineDef {
  key: LineKey;
  label: string;
  strokeClass: string;
  legendDotClass: string;
}

const LINES: LineDef[] = [
  { key: "p99", label: "P99", strokeClass: "stroke-error", legendDotClass: "bg-error" },
  { key: "p95", label: "P95", strokeClass: "stroke-warning", legendDotClass: "bg-warning" },
  { key: "p75", label: "P75", strokeClass: "stroke-success", legendDotClass: "bg-success" },
  { key: "p50", label: "P50", strokeClass: "stroke-info", legendDotClass: "bg-info" },
];

function chooseTicks(dataMax: number, baseline: number): number[] {
  const target = Math.max(dataMax, baseline) * 1.1;
  const nice = (() => {
    if (target <= 500) return Math.ceil(target / 100) * 100;
    if (target <= 2000) return Math.ceil(target / 500) * 500;
    if (target <= 10_000) return Math.ceil(target / 1000) * 1000;
    return Math.ceil(target / 5000) * 5000;
  })();
  const mid = Math.round(nice / 2 / 100) * 100;
  return Array.from(new Set([0, mid, nice])).sort((a, b) => a - b);
}

export function PerfTraceTimingChart({
  state,
  bucket,
  windowMs,
  windowMsRef,
}: Props) {
  const { t } = useTranslation();
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);
  const data = useMemo(() => {
    const raw = state.data ?? [];
    if (raw.length === 0) return raw;
    const until = Date.now();
    return densifyByBucket(
      raw,
      bucket,
      until - windowMs,
      until,
      LATENCY_EMPTY,
    );
  }, [state.data, bucket, windowMs]);

  return (
    <section
      className="rounded-xl bg-bg-secondary border border-border shadow-sm p-5 md:p-6"
      aria-labelledby="perf-trace-timing-title"
    >
      <div className="flex items-baseline justify-between flex-wrap gap-3 mb-4">
        <h2 id="perf-trace-timing-title" className="text-title">
          {t("perf.timingTitle")}
        </h2>
        <span className="text-caption text-text-secondary">
          {t("perf.timingSubtitle")}
        </span>
      </div>

      {state.loading && !state.data ? (
        <div className="h-48 flex items-center justify-center text-text-secondary">
          {t("perf.loading")}
        </div>
      ) : state.error ? (
        <div className="h-48 flex items-center justify-center text-error">
          {state.error.message}
        </div>
      ) : data.length > 0 ? (
        <>
          <Chart
            data={data}
            bucket={bucket}
            spanMs={windowMs}
            windowMsRef={windowMsRef}
            hoverIdx={hoverIdx}
            setHoverIdx={setHoverIdx}
            t={t}
          />
          <div className="flex flex-wrap gap-x-4 gap-y-2 mt-3">
            {LINES.map((l) => (
              <div key={l.key} className="text-caption flex items-center gap-1.5">
                <span
                  className={`inline-block w-3 h-0.5 rounded-full ${l.legendDotClass}`}
                />
                <span className="text-text-secondary">{l.label}</span>
              </div>
            ))}
          </div>
        </>
      ) : (
        <div className="h-48 flex items-center justify-center text-text-secondary">
          {t("perf.timingEmpty")}
        </div>
      )}
    </section>
  );
}

interface ChartProps {
  data: PerfLatencyPoint[];
  bucket: PerfBucket;
  spanMs: number;
  windowMsRef?: number;
  hoverIdx: number | null;
  setHoverIdx: (i: number | null) => void;
  t: TFunction;
}

function Chart({
  data,
  bucket,
  spanMs,
  windowMsRef,
  hoverIdx,
  setHoverIdx,
  t,
}: ChartProps) {
  const n = data.length;
  const { pending } = splitClosedPending(data, bucket);
  const pendingIdx = pending ? n - 1 : -1;
  const closedEnd = pendingIdx >= 0 ? pendingIdx : n;

  const allVals = data.flatMap((p) =>
    LINES.map((l) => p[l.key]).filter((v): v is number => v != null && v > 0),
  );
  const dataMax = allVals.length > 0 ? Math.max(...allVals) : 0;
  const ticks = chooseTicks(dataMax, windowMsRef ?? 0);
  const yMax = ticks[ticks.length - 1];

  const H = 240;
  const PAD_L = 56;
  const PAD_R = 16;
  const PAD_T = 12;
  const PAD_B = 28;
  const SVG_W = 1000;

  const xSvgAt = (i: number) => {
    if (n <= 1) return SVG_W / 2;
    const innerW = SVG_W - PAD_L - PAD_R;
    return PAD_L + (i / (n - 1)) * innerW;
  };
  const yPxAt = (v: number) => {
    const innerH = H - PAD_T - PAD_B;
    const clamped = Math.max(0, Math.min(v, yMax));
    return H - PAD_B - (clamped / yMax) * innerH;
  };
  const xPctAt = (i: number) => {
    if (n <= 1) return 50;
    const innerWPct = 100 - (PAD_L / SVG_W) * 100 - (PAD_R / SVG_W) * 100;
    return (PAD_L / SVG_W) * 100 + (i / (n - 1)) * innerWPct;
  };

  /** 拆 closed 实线段 + pending 虚线段。 */
  function linePathParts(key: LineKey): { closed: string; pending: string } {
    const closedParts: string[] = [];
    let started = false;
    for (let i = 0; i < closedEnd; i++) {
      const v = data[i][key];
      if (v == null || v <= 0) {
        started = false;
        continue;
      }
      closedParts.push(`${started ? "L" : "M"}${xSvgAt(i).toFixed(1)},${yPxAt(v).toFixed(1)}`);
      started = true;
    }

    let pendingPath = "";
    if (pendingIdx >= 0) {
      const pV = data[pendingIdx][key];
      // 只连紧邻的上一个 bucket,空洞中不跨连(见 PerfLatencyChart 同处注释)。
      if (pV != null && pV > 0 && pendingIdx > 0) {
        const prevV = data[pendingIdx - 1][key];
        if (prevV != null && prevV > 0) {
          pendingPath = `M${xSvgAt(pendingIdx - 1).toFixed(1)},${yPxAt(prevV).toFixed(1)} L${xSvgAt(pendingIdx).toFixed(1)},${yPxAt(pV).toFixed(1)}`;
        }
      }
    }
    return { closed: closedParts.join(""), pending: pendingPath };
  }

  const labelStep = Math.max(1, Math.ceil(n / 7));

  const gapRegions = useMemo(
    () => findGapRegions(data, pendingIdx),
    [data, pendingIdx],
  );

  return (
    <div className="relative w-full" style={{ height: H }}>
      <svg
        viewBox={`0 0 ${SVG_W} ${H}`}
        className="w-full h-full"
        preserveAspectRatio="none"
        role="img"
        aria-label={t("perf.timingChartAria")}
      >
        <ChartGapOverlay
          regions={gapRegions}
          xSvgAt={xSvgAt}
          n={n}
          svgW={SVG_W}
          padL={PAD_L}
          padR={PAD_R}
          padT={PAD_T}
          padB={PAD_B}
          chartH={H}
        />
        {/* Y 网格 */}
        {ticks.map((v) => (
          <line
            key={`tick-${v}`}
            x1={PAD_L}
            y1={yPxAt(v)}
            x2={SVG_W - PAD_R}
            y2={yPxAt(v)}
            className="stroke-border"
            strokeWidth="1"
            vectorEffect="non-scaling-stroke"
          />
        ))}

        {/* 包长度参考线 */}
        {windowMsRef && windowMsRef > 0 && (
          <line
            x1={PAD_L}
            y1={yPxAt(windowMsRef)}
            x2={SVG_W - PAD_R}
            y2={yPxAt(windowMsRef)}
            className="stroke-error"
            strokeWidth="1.5"
            strokeDasharray="6 6"
            vectorEffect="non-scaling-stroke"
          />
        )}

        {/* 三条折线:closed 实线 + pending 段虚线 */}
        {LINES.map((l) => {
          const { closed, pending: pendingPath } = linePathParts(l.key);
          const pendingV = pendingIdx >= 0 ? data[pendingIdx][l.key] : null;
          return (
            <Fragment key={l.key}>
              <path
                d={closed}
                className={l.strokeClass}
                strokeWidth="1.8"
                fill="none"
                strokeLinejoin="round"
                vectorEffect="non-scaling-stroke"
              />
              {pendingPath && (
                <path
                  d={pendingPath}
                  className={l.strokeClass}
                  strokeWidth="1.8"
                  strokeDasharray="4 4"
                  opacity="0.5"
                  fill="none"
                  strokeLinejoin="round"
                  vectorEffect="non-scaling-stroke"
                />
              )}
              {pendingIdx >= 0 && pendingV != null && pendingV > 0 && (
                <circle
                  cx={xSvgAt(pendingIdx)}
                  cy={yPxAt(pendingV)}
                  r="3"
                  className={l.strokeClass}
                  fill="none"
                  strokeWidth="1.5"
                  opacity="0.7"
                  vectorEffect="non-scaling-stroke"
                />
              )}
            </Fragment>
          );
        })}

        {/* hover 竖线 */}
        {hoverIdx !== null && data[hoverIdx] && (
          <line
            x1={xSvgAt(hoverIdx)}
            y1={PAD_T}
            x2={xSvgAt(hoverIdx)}
            y2={H - PAD_B}
            className="stroke-border-strong"
            strokeWidth="1"
            vectorEffect="non-scaling-stroke"
          />
        )}

        {/* hover hit area */}
        {data.map((_, i) => {
          const x = xSvgAt(i);
          const half = n > 1 ? (SVG_W - PAD_L - PAD_R) / (n - 1) / 2 : SVG_W;
          return (
            <rect
              key={i}
              x={x - half}
              y={PAD_T}
              width={half * 2}
              height={H - PAD_T - PAD_B}
              fill="transparent"
              onMouseEnter={() => setHoverIdx(i)}
              onMouseLeave={() => setHoverIdx(null)}
              style={{ cursor: "pointer" }}
            />
          );
        })}
      </svg>

      {/* Y 轴标签 */}
      {ticks.map((v) => (
        <div
          key={`label-${v}`}
          className="text-caption num text-text-tertiary absolute pointer-events-none"
          style={{
            top: yPxAt(v) - 7,
            left: 0,
            width: PAD_L - 6,
            textAlign: "right",
          }}
        >
          {v}
        </div>
      ))}

      {windowMsRef && windowMsRef > 0 && (
        <div
          className="text-caption num text-error absolute pointer-events-none"
          style={{
            top: yPxAt(windowMsRef) - 7,
            left: 0,
            width: PAD_L - 6,
            textAlign: "right",
          }}
        >
          {windowMsRef.toFixed(0)}
        </div>
      )}

      {/* X 轴标签 */}
      {data.map((p, i) => {
        if (i % labelStep !== 0 && i !== n - 1) return null;
        const isPending = i === pendingIdx;
        return (
          <div
            key={p.ts}
            className={`text-caption num absolute pointer-events-none ${
              isPending ? "text-text-tertiary opacity-60" : "text-text-tertiary"
            }`}
            style={{
              top: H - 22,
              left: `${xPctAt(i)}%`,
              transform: "translateX(-50%)",
              whiteSpace: "nowrap",
            }}
          >
            {formatPerfTs(p.ts, { spanMs })}
          </div>
        );
      })}

      {/* hover tooltip */}
      {hoverIdx !== null && data[hoverIdx] && (
        <div className="text-caption absolute top-0 right-0 px-3 py-2 rounded-lg bg-bg-secondary border border-border shadow-sm pointer-events-none z-10">
          <div className="num text-text-primary mb-1 flex items-center gap-2">
            <span>{formatPerfTs(data[hoverIdx].ts, { spanMs })}</span>
            {hoverIdx === pendingIdx && (
              <span className="text-text-tertiary text-[10px]">{t("perf.pending")}</span>
            )}
          </div>
          {LINES.map((l) => (
            <div key={l.key} className="flex items-center gap-1.5">
              <span
                className={`inline-block w-2 h-2 rounded-sm ${l.legendDotClass}`}
              />
              <span className="text-text-secondary">{l.label}</span>
              <span className="num text-text-primary ml-auto">
                {data[hoverIdx][l.key]?.toFixed(0) ?? "—"} ms
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
