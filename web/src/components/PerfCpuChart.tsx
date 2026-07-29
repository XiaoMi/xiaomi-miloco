/**
 * 进程 CPU 占用图表。
 *
 * 单折线时序（hover 弹 tooltip 看时间点 + 百分比）。数据来自 /monitor/cpu/series。
 * CPU% 取自 psutil Process.cpu_percent，多核可 > 100，Y 轴自适应刻度。
 *
 * SVG 骨架与 PerfMemoryChart 的 MemoryChart 同款：viewBox 横向自适应 + HTML 浮层
 * 放轴标签和 tooltip，避免 SVG preserveAspectRatio 拉伸字号。
 */

import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { TFunction } from "i18next";
import type { AsyncState } from "@/hooks/useAsync";
import { formatPerfTs } from "@/lib/perfBucket";
import type { CpuSeries, PerfBucket } from "@/lib/types";

interface Props {
  seriesState: AsyncState<CpuSeries>;
  bucket: PerfBucket;
  windowMs: number;
}

export function PerfCpuChart({ seriesState, bucket: _bucket, windowMs }: Props) {
  const { t } = useTranslation();
  const series = seriesState.data;
  const points = series?.points ?? [];

  const headerLine = (() => {
    if (points.length > 0) {
      const cur = points[points.length - 1].cpu_pct;
      const peak = Math.max(...points.map((p) => p.cpu_pct));
      return [
        t("perf.cpuHeaderCur", { pct: cur.toFixed(1) }),
        t("perf.cpuHeaderPeak", { pct: peak.toFixed(1) }),
      ].join(" · ");
    }
    return seriesState.loading ? t("perf.loading") : t("perf.cpuHeaderEmpty");
  })();

  return (
    <section
      className="rounded-xl bg-bg-secondary border border-border shadow-sm p-5 md:p-6"
      aria-labelledby="perf-cpu-title"
    >
      <div className="flex items-baseline justify-between flex-wrap gap-3 mb-4">
        <h2 id="perf-cpu-title" className="text-title">
          {t("perf.cpuTitle")}
        </h2>
        <span className="text-caption text-text-tertiary">{headerLine}</span>
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
        <CpuChart points={points} spanMs={windowMs} t={t} />
      )}
    </section>
  );
}

interface ChartProps {
  points: CpuSeries["points"];
  spanMs: number;
  t: TFunction;
}

function CpuChart({ points, spanMs, t }: ChartProps) {
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);

  const n = points.length;

  const H = 240;
  const PAD_L = 50;
  const PAD_R = 16;
  const PAD_T = 12;
  const PAD_B = 28;
  const SVG_W = 1000;

  const vals = points.map((p) => p.cpu_pct);
  const dataMax = Math.max(0, ...vals);
  const ticks = chooseCpuYTicks(dataMax);
  const yMax = ticks[ticks.length - 1];

  const labelStep = Math.max(1, Math.ceil(n / 7));
  const pctOfSvg = (px: number) => (px / SVG_W) * 100;

  const xPctAt = (i: number) => {
    if (n <= 1) return 50;
    const innerW = 100 - pctOfSvg(PAD_L) - pctOfSvg(PAD_R);
    return pctOfSvg(PAD_L) + (i / (n - 1)) * innerW;
  };
  const yPxAt = (v: number) => {
    const innerH = H - PAD_T - PAD_B;
    const clamped = Math.max(0, Math.min(v, yMax));
    return H - PAD_B - (clamped / yMax) * innerH;
  };
  const xSvgAt = (i: number) => {
    if (n <= 1) return SVG_W / 2;
    const innerW = SVG_W - PAD_L - PAD_R;
    return PAD_L + (i / (n - 1)) * innerW;
  };

  const linePath = vals
    .map((v, i) => `${i === 0 ? "M" : "L"}${xSvgAt(i).toFixed(1)},${yPxAt(v).toFixed(1)}`)
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
        {ticks.map((v) => (
          <line
            key={v}
            x1={PAD_L}
            y1={yPxAt(v)}
            x2={SVG_W - PAD_R}
            y2={yPxAt(v)}
            className="stroke-border"
            strokeWidth="1"
            vectorEffect="non-scaling-stroke"
          />
        ))}

        <path
          d={linePath}
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

      {ticks.map((v) => (
        <div
          key={v}
          className="text-caption num absolute pointer-events-none text-text-tertiary"
          style={{
            top: yPxAt(v) - 7,
            left: 0,
            width: PAD_L - 6,
            textAlign: "right",
          }}
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
            <span className="text-text-secondary">CPU</span>
            <span className="num text-text-primary ml-auto">
              {points[hoverIdx].cpu_pct.toFixed(1)} %
            </span>
          </div>
        </div>
      )}
    </div>
  );
}

/** CPU% 语义的 Y 轴刻度。最小 top 钉在 10，避免空闲时低平线被拉满噪声放大。 */
function chooseCpuYTicks(dataMax: number): number[] {
  const niceTop = (() => {
    if (dataMax <= 10) return 10;
    if (dataMax <= 20) return 20;
    if (dataMax <= 50) return Math.ceil(dataMax / 10) * 10;
    if (dataMax <= 100) return 100;
    return Math.ceil(dataMax / 50) * 50;
  })();
  const step = niceTop / 4;
  return [0, step, step * 2, step * 3, niceTop];
}
