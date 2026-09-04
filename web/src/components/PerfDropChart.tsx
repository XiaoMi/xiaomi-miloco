/**
 * 窗口丢弃柱状图。数据源 /api/stats?metric=drop_series。
 *
 * X 轴:bucket 起点时间。Y 轴:该 bucket 内累计丢的窗口数(绝对值)。
 * Hover 同时显示 overflow 触发次数和 cycle 总数,看丢弃密度。
 *
 * 用柱状而不是折线,因为窗口丢弃是"事件计数",离散值看绝对量;两根柱之间没有插值意义。
 *
 * 最右端如果落在还没结束的 bucket 上,柱子改成半透明 + 虚线边框画出,
 * 提示该桶仍在累积、绝对值还会涨。
 */

import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import type { AsyncState } from "@/hooks/useAsync";
import {
  densifyByBucket,
  findGapRegions,
  formatPerfTs,
  splitClosedPending,
} from "@/lib/perfBucket";
import type { PerfBucket, PerfDropPoint } from "@/lib/types";
import { useMeasuredWidth } from "@/hooks/useMeasuredWidth";
import { ChartGapOverlay } from "./ChartGapOverlay";

interface Props {
  state: AsyncState<PerfDropPoint[]>;
  bucket: PerfBucket;
  windowMs: number;
}

// 空 bucket dropped/overflow/cycle 都给 0,柱高 0 视觉上是空白带。
const DROP_EMPTY = (ts: number): PerfDropPoint => ({
  ts,
  dropped: 0,
  overflow_count: 0,
  cycle_count: 0,
});

function chooseYTicks(dataMax: number): number[] {
  if (dataMax <= 0) return [0, 1];
  const nice = (() => {
    if (dataMax <= 10) return Math.ceil(dataMax);
    if (dataMax <= 100) return Math.ceil(dataMax / 10) * 10;
    if (dataMax <= 1000) return Math.ceil(dataMax / 100) * 100;
    return Math.ceil(dataMax / 1000) * 1000;
  })();
  const mid = Math.round(nice / 2);
  return Array.from(new Set([0, mid, nice])).sort((a, b) => a - b);
}

export function PerfDropChart({ state, bucket, windowMs }: Props) {
  const { t } = useTranslation();
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);
  const data = useMemo(() => {
    const raw = state.data ?? [];
    if (raw.length === 0) return raw;
    const until = Date.now();
    return densifyByBucket(raw, bucket, until - windowMs, until, DROP_EMPTY);
  }, [state.data, bucket, windowMs]);
  const totalDropped = data.reduce((s, b) => s + b.dropped, 0);

  return (
    <section
      className="rounded-xl bg-bg-secondary border border-border shadow-sm p-5 md:p-6"
      aria-labelledby="perf-drop-title"
    >
      <div className="flex items-baseline justify-between flex-wrap gap-3 mb-4">
        <h2 id="perf-drop-title" className="text-title">
          {t("perf.dropTitle")}
        </h2>
        <span className="text-caption text-text-secondary">
          {t("perf.dropTotalPrefix")}{" "}
          <span className="num text-text-primary">
            {totalDropped.toLocaleString()}
          </span>{" "}
          {t("perf.dropTotalSuffix")}
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
        <Chart
          data={data}
          bucket={bucket}
          spanMs={windowMs}
          hoverIdx={hoverIdx}
          setHoverIdx={setHoverIdx}
        />
      ) : (
        <div className="h-48 flex items-center justify-center text-text-secondary">
          {t("perf.dropEmpty")}
        </div>
      )}
    </section>
  );
}

interface ChartProps {
  data: PerfDropPoint[];
  bucket: PerfBucket;
  spanMs: number;
  hoverIdx: number | null;
  setHoverIdx: (i: number | null) => void;
}

function Chart({ data, bucket, spanMs, hoverIdx, setHoverIdx }: ChartProps) {
  const { t } = useTranslation();
  const n = data.length;
  const { pending } = splitClosedPending(data, bucket);
  const pendingIdx = pending ? n - 1 : -1;

  const dataMax = Math.max(...data.map((d) => d.dropped), 0);
  const ticks = chooseYTicks(dataMax);
  const yMax = ticks[ticks.length - 1] || 1;

  const H = 220;
  const PAD_L = 48;
  const PAD_R = 16;
  const PAD_T = 12;
  const PAD_B = 28;
  // SVG 单位 == CSS 像素,详见 useMeasuredWidth 的说明
  const [wrapRef, SVG_W] = useMeasuredWidth(1000);
  const barGap = n > 30 ? 1 : 2;
  const barW =
    n > 0 ? (SVG_W - PAD_L - PAD_R - barGap * (n - 1)) / n : 0;

  const xSvgAt = (i: number) => PAD_L + i * (barW + barGap);
  const yPxAt = (v: number) => {
    const innerH = H - PAD_T - PAD_B;
    const clamped = Math.max(0, Math.min(v, yMax));
    return H - PAD_B - (clamped / yMax) * innerH;
  };
  const xPctAt = (i: number) => {
    const x = xSvgAt(i) + barW / 2;
    return (x / SVG_W) * 100;
  };

  const labelStep = Math.max(1, Math.ceil(n / 7));

  const gapRegions = useMemo(
    () => findGapRegions(data, pendingIdx),
    [data, pendingIdx],
  );

  return (
    <div ref={wrapRef} className="relative w-full" style={{ height: H }}>
      <svg
        viewBox={`0 0 ${SVG_W} ${H}`}
        className="w-full h-full"
        preserveAspectRatio="none"
        role="img"
        aria-label={t("perf.dropChartAria")}
      >
        {/* 柱状图 xSvgAt 是柱左边界,这里 +barW/2 转中心给 overlay 用 */}
        <ChartGapOverlay
          regions={gapRegions}
          xSvgAt={(i) => xSvgAt(i) + barW / 2}
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

        {/* 柱子。pending 柱画半透明 + 虚线边框,提示仍在累积。 */}
        {data.map((d, i) => {
          const h = ((H - PAD_T - PAD_B) * d.dropped) / yMax;
          const on = hoverIdx === i;
          const isPending = i === pendingIdx;
          const fillClass =
            d.dropped === 0
              ? "fill-bg-tertiary"
              : on
                ? "fill-error"
                : "fill-error opacity-70";
          return (
            <g
              key={d.ts}
              onMouseEnter={() => setHoverIdx(i)}
              onMouseLeave={() => setHoverIdx(null)}
              style={{ cursor: "pointer" }}
            >
              {/* 透明 hit-box 让贴近鼠标也能命中 */}
              <rect
                x={xSvgAt(i)}
                y={PAD_T}
                width={barW + barGap}
                height={H - PAD_T - PAD_B}
                fill="transparent"
              />
              <rect
                x={xSvgAt(i)}
                y={yPxAt(d.dropped)}
                width={barW}
                height={h}
                className={fillClass}
                opacity={isPending ? 0.35 : undefined}
                strokeDasharray={isPending ? "3 2" : undefined}
                stroke={isPending ? "currentColor" : undefined}
                strokeWidth={isPending ? 1 : undefined}
                vectorEffect="non-scaling-stroke"
              />
            </g>
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

      {hoverIdx !== null && data[hoverIdx] && (
        <div className="text-caption absolute top-0 right-0 px-3 py-2 rounded-lg bg-bg-secondary border border-border shadow-sm pointer-events-none z-10 min-w-[160px]">
          <div className="num text-text-primary mb-1 flex items-center gap-2">
            <span>{formatPerfTs(data[hoverIdx].ts, { spanMs })}</span>
            {hoverIdx === pendingIdx && (
              <span className="text-text-tertiary text-[10px]">{t("perf.pending")}</span>
            )}
          </div>
          <div className="flex items-center gap-2">
            <span className="text-text-secondary">{t("perf.dropTooltipDropped")}</span>
            <span className="num text-error font-semibold ml-auto">
              {data[hoverIdx].dropped}
            </span>
          </div>
          <div className="flex items-center gap-2">
            <span className="text-text-secondary">{t("perf.dropTooltipOverflow")}</span>
            <span className="num text-text-secondary ml-auto">
              {data[hoverIdx].overflow_count}
            </span>
          </div>
          <div className="flex items-center gap-2">
            <span className="text-text-secondary">{t("perf.dropTooltipCycle")}</span>
            <span className="num text-text-tertiary ml-auto">
              {data[hoverIdx].cycle_count}
            </span>
          </div>
        </div>
      )}
    </div>
  );
}
