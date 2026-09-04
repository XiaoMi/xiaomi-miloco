/**
 * Omni 错误时序堆叠柱状图。数据源 /api/stats?metric=omni_error_series。
 *
 * X 轴:bucket 起点时间。Y 轴:该 bucket 内 omni 错误次数(三类堆叠)。
 *   - 限流 (rate_limit) — 红色,HTTPStatusError:429
 *   - 超时 (timeout)    — 橙色,httpx Timeout 类
 *   - 其他 (other)      — 灰色,5xx / ConnectError / 解析失败等
 *
 * 用堆叠柱状是为了同一时刻看清"错误总量 + 各类型占比",问题时段一眼可辨。
 *
 * 卡底警示语:限流期间窗口丢弃率仍受影响 (A 范围只过滤 RTF 失真,不过滤丢弃率)。
 */

import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import type { TFunction } from "i18next";
import type { AsyncState } from "@/hooks/useAsync";
import {
  densifyByBucket,
  findGapRegions,
  formatPerfTs,
  splitClosedPending,
} from "@/lib/perfBucket";
import { ChartGapOverlay } from "./ChartGapOverlay";
import type { PerfBucket, PerfOmniErrorPoint } from "@/lib/types";
import { useMeasuredWidth } from "@/hooks/useMeasuredWidth";

interface Props {
  state: AsyncState<PerfOmniErrorPoint[]>;
  bucket: PerfBucket;
  windowMs: number;
}

// 空 bucket 三类计数都给 0,堆叠柱高度 0 不可见。
const OMNI_ERR_EMPTY = (ts: number): PerfOmniErrorPoint => ({
  ts,
  rate_limit: 0,
  timeout: 0,
  other: 0,
});

type StackKey = "rate_limit" | "timeout" | "other";

interface StackDef {
  key: StackKey;
  labelKey: string;
  fillClass: string;
  legendDotClass: string;
}

// 堆叠顺序:底→顶 = 限流→超时→其他;红色限流压底,视觉锚点最稳。
const STACKS: StackDef[] = [
  {
    key: "rate_limit",
    labelKey: "perf.omniErrRateLimit",
    fillClass: "fill-error",
    legendDotClass: "bg-error",
  },
  {
    key: "timeout",
    labelKey: "perf.omniErrTimeout",
    fillClass: "fill-warning",
    legendDotClass: "bg-warning",
  },
  {
    key: "other",
    labelKey: "perf.omniErrOther",
    fillClass: "fill-text-tertiary",
    legendDotClass: "bg-text-tertiary",
  },
];

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

function totalOf(p: PerfOmniErrorPoint): number {
  return p.rate_limit + p.timeout + p.other;
}

export function PerfOmniErrorChart({ state, bucket, windowMs }: Props) {
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
      OMNI_ERR_EMPTY,
    );
  }, [state.data, bucket, windowMs]);
  const totals = data.reduce(
    (acc, b) => ({
      rate_limit: acc.rate_limit + b.rate_limit,
      timeout: acc.timeout + b.timeout,
      other: acc.other + b.other,
    }),
    { rate_limit: 0, timeout: 0, other: 0 },
  );
  const grandTotal = totals.rate_limit + totals.timeout + totals.other;
  const hasRateLimit = totals.rate_limit > 0;

  return (
    <section
      className="rounded-xl bg-bg-secondary border border-border shadow-sm p-5 md:p-6"
      aria-labelledby="perf-omni-err-title"
    >
      <div className="flex items-baseline justify-between flex-wrap gap-3 mb-4">
        <h2 id="perf-omni-err-title" className="text-title">
          {t("perf.omniErrTitle")}
        </h2>
        <span className="text-caption text-text-secondary">
          {t("perf.omniErrTotalPrefix")}{" "}
          <span className="num text-text-primary">
            {grandTotal.toLocaleString()}
          </span>{" "}
          {t("perf.omniErrTotalSuffix")}
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
            hoverIdx={hoverIdx}
            setHoverIdx={setHoverIdx}
            t={t}
          />
          {/* 图例 */}
          <div className="flex flex-wrap gap-x-4 gap-y-2 mt-3">
            {STACKS.map((s) => (
              <div key={s.key} className="text-caption flex items-center gap-1.5">
                <span
                  className={`inline-block w-3 h-3 rounded-sm ${s.legendDotClass}`}
                />
                <span className="text-text-secondary">
                  {t(s.labelKey)}{" "}
                  <span className="num text-text-tertiary">
                    {totals[s.key].toLocaleString()}
                  </span>
                </span>
              </div>
            ))}
          </div>
        </>
      ) : (
        <div className="h-48 flex items-center justify-center text-text-secondary">
          {t("perf.omniErrEmpty")}
        </div>
      )}

      {hasRateLimit && (
        <div className="text-caption text-error mt-3 pt-2 border-t border-border">
          {t("perf.omniErrWarn")}
        </div>
      )}
    </section>
  );
}

interface ChartProps {
  data: PerfOmniErrorPoint[];
  bucket: PerfBucket;
  spanMs: number;
  hoverIdx: number | null;
  setHoverIdx: (i: number | null) => void;
  t: TFunction;
}

function Chart({ data, bucket, spanMs, hoverIdx, setHoverIdx, t }: ChartProps) {
  const n = data.length;
  const { pending } = splitClosedPending(data, bucket);
  const pendingIdx = pending ? n - 1 : -1;

  const dataMax = Math.max(...data.map(totalOf), 0);
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
  const barW = n > 0 ? (SVG_W - PAD_L - PAD_R - barGap * (n - 1)) / n : 0;

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
        aria-label={t("perf.omniErrChartAria")}
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

        {/* 堆叠柱:从底到顶逐段累积。pending 段半透明 + 虚线边框。 */}
        {data.map((d, i) => {
          const isPending = i === pendingIdx;
          let accum = 0; // 从 0 累加,作为下一段的 yBottom
          return (
            <g
              key={d.ts}
              onMouseEnter={() => setHoverIdx(i)}
              onMouseLeave={() => setHoverIdx(null)}
              style={{ cursor: "pointer" }}
            >
              {/* hit-box */}
              <rect
                x={xSvgAt(i)}
                y={PAD_T}
                width={barW + barGap}
                height={H - PAD_T - PAD_B}
                fill="transparent"
              />
              {STACKS.map((s) => {
                const v = d[s.key];
                if (v === 0) return null;
                const yTop = yPxAt(accum + v);
                const yBot = yPxAt(accum);
                accum += v;
                const h = yBot - yTop;
                return (
                  <rect
                    key={s.key}
                    x={xSvgAt(i)}
                    y={yTop}
                    width={barW}
                    height={h}
                    className={s.fillClass}
                    opacity={isPending ? 0.4 : hoverIdx === i ? 1 : 0.85}
                    strokeDasharray={isPending ? "3 2" : undefined}
                    stroke={isPending ? "currentColor" : undefined}
                    strokeWidth={isPending ? 1 : undefined}
                    vectorEffect="non-scaling-stroke"
                  />
                );
              })}
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
        <div className="text-caption absolute top-0 right-0 px-3 py-2 rounded-lg bg-bg-secondary border border-border shadow-sm pointer-events-none z-10 min-w-[180px]">
          <div className="num text-text-primary mb-1 flex items-center gap-2">
            <span>{formatPerfTs(data[hoverIdx].ts, { spanMs })}</span>
            {hoverIdx === pendingIdx && (
              <span className="text-text-tertiary text-[10px]">{t("perf.pending")}</span>
            )}
          </div>
          {STACKS.map((s) => (
            <div key={s.key} className="flex items-center gap-2">
              <span
                className={`inline-block w-2 h-2 rounded-sm ${s.legendDotClass}`}
              />
              <span className="text-text-secondary">{t(s.labelKey)}</span>
              <span className="num text-text-primary ml-auto">
                {data[hoverIdx][s.key]}
              </span>
            </div>
          ))}
          <div className="flex items-center gap-2 pt-1 mt-1 border-t border-border">
            <span className="text-text-secondary">{t("perf.omniErrTotal")}</span>
            <span className="num text-text-primary font-semibold ml-auto">
              {totalOf(data[hoverIdx])}
            </span>
          </div>
        </div>
      )}
    </div>
  );
}
