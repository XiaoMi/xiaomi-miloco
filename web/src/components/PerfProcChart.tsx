/**
 * 进程 CPU 占用 + 线程数图表。
 *
 * 双折线时序:CPU%(左 Y 轴 0-100%,brand 实线)+ 线程数(右 Y 轴,warning 虚线),
 * hover 弹 tooltip 看时间点 + 两个值。数据来自 /monitor/proc/series。CPU 原始值
 * 取自 psutil Process.cpu_percent(多核可 > 100),除以 core_count 归一化到 0-100%;
 * tooltip 括号内保留原始绝对值(满核百分比)。
 *
 * SVG 骨架与 PerfMemoryChart 同款:viewBox 横向自适应 + HTML 浮层放轴标签和
 * tooltip,避免 SVG preserveAspectRatio 拉伸字号。
 */

import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { TFunction } from "i18next";
import type { AsyncState } from "@/hooks/useAsync";
import { formatPerfTs } from "@/lib/perfBucket";
import type { ProcSeries, PerfBucket } from "@/lib/types";

interface Props {
  seriesState: AsyncState<ProcSeries>;
  bucket: PerfBucket;
  windowMs: number;
}

export function PerfProcChart({ seriesState, bucket: _bucket, windowMs }: Props) {
  const { t } = useTranslation();
  const series = seriesState.data;
  const points = series?.points ?? [];
  const coreCount = series?.core_count ?? 1;

  const headerLine = (() => {
    if (points.length > 0) {
      const last = points[points.length - 1];
      const peak = Math.max(...points.map((p) => p.cpu_pct));
      return [
        t("perf.cpuHeaderCur", { pct: (last.cpu_pct / coreCount).toFixed(1) }),
        t("perf.cpuHeaderPeak", { pct: (peak / coreCount).toFixed(1) }),
        t("perf.cpuHeaderThreads", { n: last.num_threads }),
      ].join(" · ");
    }
    return seriesState.loading ? t("perf.loading") : t("perf.cpuHeaderEmpty");
  })();

  return (
    <section
      className="rounded-xl bg-bg-secondary border border-border shadow-sm p-5 md:p-6"
      aria-labelledby="perf-proc-title"
    >
      <div className="flex items-baseline justify-between flex-wrap gap-3 mb-4">
        <h2 id="perf-proc-title" className="text-title">
          {t("perf.cpuTitle")}
        </h2>
        <div className="flex items-center gap-3 text-caption text-text-tertiary flex-wrap">
          <span className="inline-flex items-center gap-1.5">
            <span className="inline-block w-2.5 h-2.5 rounded-full bg-brand-primary" />
            {t("perf.cpuLegendCpu")}
          </span>
          <span className="inline-flex items-center gap-1.5">
            <span className="inline-block w-2.5 h-2.5 rounded-full bg-warning" />
            {t("perf.cpuLegendThreads")}
          </span>
          <span>{headerLine}</span>
        </div>
      </div>

      {seriesState.loading && !series ? (
        <div className="h-48 flex items-center justify-center text-text-secondary">
          {t("perf.loading")}
        </div>
      ) : seriesState.error ? (
        <div className="h-48 flex items-center justify-center text-error">
          {seriesState.error.message}
        </div>
      ) : points.length === 0 ? (
        <div className="h-48 flex items-center justify-center text-text-secondary">
          {t("perf.cpuEmptySeries")}
        </div>
      ) : (
        <ProcChart points={points} coreCount={coreCount} spanMs={windowMs} t={t} />
      )}
    </section>
  );
}

interface ChartProps {
  points: ProcSeries["points"];
  coreCount: number;
  spanMs: number;
  t: TFunction;
}

function ProcChart({ points, coreCount, spanMs, t }: ChartProps) {
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);

  const n = points.length;

  const H = 240;
  const PAD_L = 50;
  const PAD_R = 44; // 右 Y 轴(线程数)标签留空间
  const PAD_T = 12;
  const PAD_B = 28;
  const SVG_W = 1000;

  // 左轴:CPU% 归一化 = cpu_pct / 总核数,Y 轴钉死 [0,100]
  const cpuVals = points.map((p) => p.cpu_pct / coreCount);
  const CPU_TICKS = [0, 25, 50, 75, 100];
  const yCpuMax = 100;

  // 右轴:线程数
  const threadVals = points.map((p) => p.num_threads);
  const threadMax = Math.max(0, ...threadVals);
  const threadTicks = chooseThreadYTicks(threadMax);
  const yThreadMax = threadTicks[threadTicks.length - 1];

  const labelStep = Math.max(1, Math.ceil(n / 7));
  const pctOfSvg = (px: number) => (px / SVG_W) * 100;

  const xPctAt = (i: number) => {
    if (n <= 1) return 50;
    const innerW = 100 - pctOfSvg(PAD_L) - pctOfSvg(PAD_R);
    return pctOfSvg(PAD_L) + (i / (n - 1)) * innerW;
  };
  const innerH = H - PAD_T - PAD_B;
  const yCpuPxAt = (v: number) => {
    const clamped = Math.max(0, Math.min(v, yCpuMax));
    return H - PAD_B - (clamped / yCpuMax) * innerH;
  };
  const yThreadPxAt = (v: number) => {
    const clamped = Math.max(0, Math.min(v, yThreadMax));
    return H - PAD_B - (clamped / yThreadMax) * innerH;
  };
  const xSvgAt = (i: number) => {
    if (n <= 1) return SVG_W / 2;
    const innerW = SVG_W - PAD_L - PAD_R;
    return PAD_L + (i / (n - 1)) * innerW;
  };

  const cpuPath = cpuVals
    .map((v, i) => `${i === 0 ? "M" : "L"}${xSvgAt(i).toFixed(1)},${yCpuPxAt(v).toFixed(1)}`)
    .join("");
  const threadPath = threadVals
    .map((v, i) => `${i === 0 ? "M" : "L"}${xSvgAt(i).toFixed(1)},${yThreadPxAt(v).toFixed(1)}`)
    .join("");

  return (
    <div className="relative w-full" style={{ height: H }}>
      <svg
        viewBox={`0 0 ${SVG_W} ${H}`}
        className="w-full h-full"
        preserveAspectRatio="none"
        role="img"
        aria-label={t("perf.cpuAria")}
      >
        {CPU_TICKS.map((v) => (
          <line
            key={v}
            x1={PAD_L}
            y1={yCpuPxAt(v)}
            x2={SVG_W - PAD_R}
            y2={yCpuPxAt(v)}
            className="stroke-border"
            strokeWidth="1"
            vectorEffect="non-scaling-stroke"
          />
        ))}

        <path
          d={threadPath}
          className="stroke-warning"
          strokeWidth="1.5"
          fill="none"
          strokeLinejoin="round"
          strokeDasharray="4 3"
          vectorEffect="non-scaling-stroke"
        />
        <path
          d={cpuPath}
          className="stroke-brand-primary"
          strokeWidth="1.8"
          fill="none"
          strokeLinejoin="round"
          vectorEffect="non-scaling-stroke"
        />

        {hoverIdx !== null && points[hoverIdx] && (
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

        {points.map((_, i) => {
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

      {/* 左 Y 轴:CPU% 归一化 0-100 */}
      {CPU_TICKS.map((v) => (
        <div
          key={v}
          className="text-caption num absolute pointer-events-none text-text-tertiary"
          style={{ top: yCpuPxAt(v) - 7, left: 0, width: PAD_L - 6, textAlign: "right" }}
        >
          {v.toFixed(0)}
        </div>
      ))}

      {/* 右 Y 轴:线程数 */}
      {threadTicks.map((v) => (
        <div
          key={`th-${v}`}
          className="text-caption num absolute pointer-events-none text-warning"
          style={{ top: yThreadPxAt(v) - 7, right: 0, width: PAD_R - 6, textAlign: "left" }}
        >
          {v.toFixed(0)}
        </div>
      ))}

      {points.map((p, i) => {
        if (i % labelStep !== 0 && i !== n - 1) return null;
        return (
          <div
            key={p.ts}
            className="text-caption num absolute pointer-events-none text-text-tertiary"
            style={{
              top: H - 22,
              left: `${xPctAt(i)}%`,
              transform: "translateX(-50%)",
              whiteSpace: "nowrap",
            }}
          >
            {formatPerfTs(p.ts * 1000, { spanMs })}
          </div>
        );
      })}

      {hoverIdx !== null && points[hoverIdx] && (
        <div className="text-caption absolute top-0 right-0 px-3 py-2 rounded-lg bg-bg-secondary border border-border shadow-sm pointer-events-none z-10">
          <div className="num text-text-primary mb-1">
            {formatPerfTs(points[hoverIdx].ts * 1000, { spanMs })}
          </div>
          <div className="flex items-center gap-3">
            <span className="text-text-secondary">{t("perf.cpuLegendCpu")}</span>
            <span className="num text-text-primary ml-auto">
              {(points[hoverIdx].cpu_pct / coreCount).toFixed(1)}%
              <span className="text-text-tertiary ml-1">
                ({points[hoverIdx].cpu_pct.toFixed(0)}%)
              </span>
            </span>
          </div>
          <div className="flex items-center gap-3">
            <span className="text-text-secondary">{t("perf.cpuLegendThreads")}</span>
            <span className="num text-text-primary ml-auto">
              {points[hoverIdx].num_threads}
            </span>
          </div>
        </div>
      )}
    </div>
  );
}

/** 线程数语义的 Y 轴刻度。最小 top 钉在 20,避免线程数少时曲线噪声被放大。 */
function chooseThreadYTicks(dataMax: number): number[] {
  const niceTop = (() => {
    if (dataMax <= 20) return 20;
    if (dataMax <= 50) return 50;
    if (dataMax <= 100) return 100;
    if (dataMax <= 200) return 200;
    if (dataMax <= 500) return Math.ceil(dataMax / 100) * 100;
    return Math.ceil(dataMax / 200) * 200;
  })();
  const step = niceTop / 4;
  return [0, step, step * 2, step * 3, niceTop];
}
