/**
 * Gate 过滤率折线图。数据源 /api/stats?metric=gate_pass_rate(返回通过率,
 * 前端转 1 - pass 展示)。
 *
 * 单线展示整体过滤率(= 1 - gate_passed):视频和音频都没通过的比率,等价于
 * cycle skip 率。给 KPI 卡的同名数字加个时间维度,能看凌晨高/白天低的昼夜趋势。
 *
 * 最右端如果落在还没结束的 bucket 上,改成虚线 + 半透明画出,提示该点仍在累积。
 */

import { Fragment, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import type { AsyncState } from "@/hooks/useAsync";
import {
  densifyByBucket,
  findGapRegions,
  formatPerfTs,
  splitClosedPending,
} from "@/lib/perfBucket";
import type { PerfBucket, PerfGatePoint } from "@/lib/types";
import { useMeasuredWidth } from "@/hooks/useMeasuredWidth";
import { ChartGapOverlay } from "./ChartGapOverlay";

interface Props {
  state: AsyncState<PerfGatePoint[]>;
  bucket: PerfBucket;
  windowMs: number;
  /** 嵌入「性能监测」大卡时去掉自身卡壳。 */
  embedded?: boolean;
}

const LINE_STROKE_CLASS = "stroke-brand-primary";
const LINE_DOT_CLASS = "bg-brand-primary";

// 空 bucket 字段填 null,折线会自动断开 (toFilterPct null in → null out)。
const GATE_EMPTY = (ts: number): PerfGatePoint => ({
  ts,
  overall: null,
  video: null,
  audio: null,
});

/** Y 轴固定 0-100% 刻度,5 档。 */
const Y_TICKS = [0, 25, 50, 75, 100];

export function PerfGateChart({ state, bucket, windowMs, embedded = false }: Props) {
  const { t } = useTranslation();
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);
  const data = useMemo(() => {
    const raw = state.data ?? [];
    if (raw.length === 0) return raw;
    const until = Date.now();
    return densifyByBucket(raw, bucket, until - windowMs, until, GATE_EMPTY);
  }, [state.data, bucket, windowMs]);

  return (
    <section
      className={
        embedded
          ? ""
          : "rounded-xl bg-bg-secondary border border-border shadow-sm p-5 md:p-6"
      }
      aria-labelledby="perf-gate-title"
    >
      <div className="flex items-baseline justify-between flex-wrap gap-3 mb-4">
        <h2 id="perf-gate-title" className="text-title">
          {t("perf.gateTitle")}
        </h2>
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
          {t("perf.noData")}
        </div>
      )}
    </section>
  );
}

interface ChartProps {
  data: PerfGatePoint[];
  bucket: PerfBucket;
  spanMs: number;
  hoverIdx: number | null;
  setHoverIdx: (i: number | null) => void;
}

/** backend 通过率(0-1)转过滤率百分比(0-100)。 */
function toFilterPct(passRate: number | null | undefined): number | null {
  if (passRate == null) return null;
  return (1 - passRate) * 100;
}

function Chart({ data, bucket, spanMs, hoverIdx, setHoverIdx }: ChartProps) {
  const { t } = useTranslation();
  const n = data.length;
  const { pending } = splitClosedPending(data, bucket);
  const pendingIdx = pending ? n - 1 : -1;
  const closedEnd = pendingIdx >= 0 ? pendingIdx : n;

  const H = 240;
  const PAD_L = 44;
  const PAD_R = 16;
  const PAD_T = 12;
  const PAD_B = 28;
  // SVG 单位 == CSS 像素,详见 useMeasuredWidth 的说明
  const [wrapRef, SVG_W] = useMeasuredWidth(1000);
  const yMax = 100;

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
  function linePathParts(): { closed: string; pending: string } {
    const closedParts: string[] = [];
    let started = false;
    for (let i = 0; i < closedEnd; i++) {
      const v = toFilterPct(data[i].overall);
      if (v == null) {
        started = false;
        continue;
      }
      closedParts.push(`${started ? "L" : "M"}${xSvgAt(i).toFixed(1)},${yPxAt(v).toFixed(1)}`);
      started = true;
    }

    let pendingPath = "";
    if (pendingIdx >= 0) {
      const pV = toFilterPct(data[pendingIdx].overall);
      // 只连紧邻的上一个 bucket,空洞中不跨连(见 PerfRtfChart 同处注释)。
      if (pV != null && pendingIdx > 0) {
        const prevV = toFilterPct(data[pendingIdx - 1].overall);
        if (prevV != null) {
          pendingPath = `M${xSvgAt(pendingIdx - 1).toFixed(1)},${yPxAt(prevV).toFixed(1)} L${xSvgAt(pendingIdx).toFixed(1)},${yPxAt(pV).toFixed(1)}`;
        }
      }
    }
    return { closed: closedParts.join(""), pending: pendingPath };
  }

  const labelStep = Math.max(1, Math.ceil(n / 7));

  const { closed, pending: pendingPath } = linePathParts();
  const pendingV =
    pendingIdx >= 0 ? toFilterPct(data[pendingIdx].overall) : null;

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
        aria-label={t("perf.gateChartAria")}
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
        {Y_TICKS.map((v) => (
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

        <Fragment>
          <path
            d={closed}
            className={LINE_STROKE_CLASS}
            strokeWidth="1.8"
            fill="none"
            strokeLinejoin="round"
            vectorEffect="non-scaling-stroke"
          />
          {pendingPath && (
            <path
              d={pendingPath}
              className={LINE_STROKE_CLASS}
              strokeWidth="1.8"
              strokeDasharray="4 4"
              opacity="0.5"
              fill="none"
              strokeLinejoin="round"
              vectorEffect="non-scaling-stroke"
            />
          )}
          {pendingIdx >= 0 && pendingV != null && (
            <circle
              cx={xSvgAt(pendingIdx)}
              cy={yPxAt(pendingV)}
              r="3"
              className={LINE_STROKE_CLASS}
              fill="none"
              strokeWidth="1.5"
              opacity="0.7"
              vectorEffect="non-scaling-stroke"
            />
          )}
        </Fragment>

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

      {/* Y 轴标签:百分比 */}
      {Y_TICKS.map((v) => (
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
          {v}%
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
        <div className="text-caption absolute top-0 right-0 px-3 py-2 rounded-lg bg-bg-secondary border border-border shadow-sm pointer-events-none z-10">
          <div className="num text-text-primary mb-1 flex items-center gap-2">
            <span>{formatPerfTs(data[hoverIdx].ts, { spanMs })}</span>
            {hoverIdx === pendingIdx && (
              <span className="text-text-tertiary text-[10px]">{t("perf.pending")}</span>
            )}
          </div>
          <div className="flex items-center gap-1.5">
            <span
              className={`inline-block w-2 h-2 rounded-sm ${LINE_DOT_CLASS}`}
            />
            <span className="text-text-secondary">{t("perf.gateTooltipFilterRate")}</span>
            <span className="num text-text-primary ml-auto">
              {(() => {
                const v = toFilterPct(data[hoverIdx].overall);
                return v == null ? "—" : `${v.toFixed(1)}%`;
              })()}
            </span>
          </div>
        </div>
      )}
    </div>
  );
}
