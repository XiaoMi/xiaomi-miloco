/**
 * RTF 时间序列折线图。纯 SVG,标签用 HTML 浮层避免 preserveAspectRatio 字号拉伸。
 *
 * 5 条 RTF 变体:
 *   rtf_e2e_ok     — 仅成功 cycle 的端到端 RTF (主线,反映系统真实负载)
 *   rtf_e2e        — 全部 cycle (含失败) 端到端 RTF (灰虚线,对比用)
 *   rtf            — cycle 处理本身 (内核视角)
 *   rtf_omni_ok    — 仅成功 cycle 的 omni 单段 RTF (omni 真实推理实时性)
 *   rtf_omni       — 全部 cycle (含失败) omni 单段 RTF (橙虚线,对比用)
 *
 * rtf_e2e_ok / rtf_omni_ok 与对应"含失败"线的差值反映 omni 失败拖累 rtf 的程度:
 * 超时拖长,限流拖短。
 *
 * 1.0 红虚线 = 实时性边界。Y 轴根据数据 max 自适应刻度,确保 >1 时也能看清高度。
 *
 * 最右端如果落在还没结束的 bucket 上,改成虚线 + 半透明画出,提示该点仍在累积、
 * 样本不足时 AVG 可能跳。语义参考 lib/perfBucket.ts splitClosedPending。
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
import type { PerfBucket, PerfRtfPoint } from "@/lib/types";
import { useMeasuredWidth } from "@/hooks/useMeasuredWidth";
import { ChartGapOverlay } from "./ChartGapOverlay";

interface Props {
  state: AsyncState<PerfRtfPoint[]>;
  bucket: PerfBucket;
  windowMs: number;
  /** 嵌入「性能监测」大卡时去掉自身卡壳。 */
  embedded?: boolean;
}

const RTF_EMPTY = (ts: number): PerfRtfPoint => ({
  ts,
  rtf: null,
  rtf_e2e: null,
  rtf_stream_e2e: null,
  rtf_pipeline: null,
  rtf_omni: null,
  rtf_e2e_ok: null,
  rtf_omni_ok: null,
});

type LineKey = "rtf" | "rtf_e2e" | "rtf_omni" | "rtf_e2e_ok" | "rtf_omni_ok";

interface LineDef {
  key: LineKey;
  labelKey: string;
  strokeClass: string;
  legendDotClass: string;
  /** true 时整条线画成虚线(对比线,不是主指标) */
  dashed?: boolean;
}

const LINES: LineDef[] = [
  { key: "rtf_e2e_ok", labelKey: "perf.rtfE2eOk", strokeClass: "stroke-brand-primary", legendDotClass: "bg-brand-primary" },
  { key: "rtf_e2e", labelKey: "perf.rtfE2e", strokeClass: "stroke-text-tertiary", legendDotClass: "bg-text-tertiary", dashed: true },
  { key: "rtf", labelKey: "perf.rtfCycle", strokeClass: "stroke-info", legendDotClass: "bg-info" },
  { key: "rtf_omni_ok", labelKey: "perf.rtfOmniOk", strokeClass: "stroke-success", legendDotClass: "bg-success" },
  { key: "rtf_omni", labelKey: "perf.rtfOmni", strokeClass: "stroke-warning", legendDotClass: "bg-warning", dashed: true },
];

/** 选 nice 刻度:始终含 0 和 1.0(1.0 是红虚线载体),顶部按 dataMax 取整。返回升序数组。 */
function chooseYTicks(dataMax: number): number[] {
  if (dataMax <= 1.2) return [0, 0.5, 1.0];
  // 顶部刻度向上取到 nice number
  const niceTop = (() => {
    if (dataMax <= 2) return 2;
    if (dataMax <= 3) return 3;
    if (dataMax <= 5) return 5;
    if (dataMax <= 8) return 8;
    if (dataMax <= 10) return 10;
    if (dataMax <= 15) return 15;
    if (dataMax <= 20) return 20;
    if (dataMax <= 30) return 30;
    if (dataMax <= 50) return 50;
    if (dataMax <= 100) return 100;
    const mag = Math.pow(10, Math.floor(Math.log10(dataMax)));
    return Math.ceil(dataMax / mag) * mag;
  })();
  if (niceTop > 5) {
    // 大区间:0..niceTop 4 等分均匀分布 + 强制塞 1.0(红虚线载体)
    const step = niceTop / 4;
    const ticks = new Set([0, 1, step, step * 2, step * 3, niceTop]);
    return Array.from(ticks).sort((a, b) => a - b);
  }
  // 小区间(niceTop<=5):用几何中位,1.0 跟 niceTop 距离较近时还能均衡
  const mid = Math.round(Math.sqrt(1 * niceTop) * 10) / 10;
  const ticks = new Set([0, 1, mid, niceTop]);
  return Array.from(ticks).sort((a, b) => a - b);
}

export function PerfRtfChart({ state, bucket, windowMs, embedded = false }: Props) {
  const { t } = useTranslation();
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);

  const dense = useMemo(() => {
    const raw = state.data ?? [];
    if (raw.length === 0) return raw;
    const until = Date.now();
    return densifyByBucket(raw, bucket, until - windowMs, until, RTF_EMPTY);
  }, [state.data, bucket, windowMs]);

  return (
    <section
      className={
        embedded
          ? ""
          : "rounded-xl bg-bg-secondary border border-border shadow-sm p-5 md:p-6"
      }
      aria-labelledby="perf-rtf-title"
    >
      <div className="flex items-baseline justify-between flex-wrap gap-3 mb-4">
        <h2 id="perf-rtf-title" className="text-title">
          {t("perf.rtfTitle")}
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
      ) : dense.length > 0 ? (
        <>
          <Chart
            data={dense}
            bucket={bucket}
            spanMs={windowMs}
            hoverIdx={hoverIdx}
            setHoverIdx={setHoverIdx}
            t={t}
          />
          {/* 图例 */}
          <div className="flex flex-wrap gap-x-4 gap-y-2 mt-3">
            {LINES.map((l) => (
              <div key={l.key} className="text-caption flex items-center gap-1.5">
                <span
                  className={`inline-block w-3 h-0.5 rounded-full ${l.legendDotClass}`}
                />
                <span className="text-text-secondary">{t(l.labelKey)}</span>
              </div>
            ))}
          </div>
        </>
      ) : (
        <div className="h-48 flex items-center justify-center text-text-secondary">
          {t("perf.rtfEmpty")}
        </div>
      )}
    </section>
  );
}

interface ChartProps {
  data: PerfRtfPoint[];
  bucket: PerfBucket;
  spanMs: number;
  hoverIdx: number | null;
  setHoverIdx: (i: number | null) => void;
  t: TFunction;
}

function Chart({ data, bucket, spanMs, hoverIdx, setHoverIdx, t }: ChartProps) {
  const H = 240;
  const PAD_L = 44;
  const PAD_R = 16;
  const PAD_T = 12;
  const PAD_B = 28;
  const n = data.length;

  // 最右端 bucket 是否仍在累积。pendingIdx === n-1 时表示该点 pending,
  // 渲染上单独画一段虚线 + 半透明。
  const { pending } = splitClosedPending(data, bucket);
  const pendingIdx = pending ? n - 1 : -1;
  const closedEnd = pendingIdx >= 0 ? pendingIdx : n;

  const gapRegions = useMemo(
    () => findGapRegions(data, pendingIdx),
    [data, pendingIdx],
  );

  // y 轴范围:确保至少到 1.2,数据高时自适应
  const allVals = data.flatMap((p) =>
    LINES.map((l) => p[l.key]).filter((v): v is number => v != null),
  );
  const dataMax = allVals.length > 0 ? Math.max(...allVals) : 1.2;
  const ticks = chooseYTicks(dataMax);
  const yMax = ticks[ticks.length - 1];

  // x 轴标签密度:步长按 ceil(n/7) 取(≤7 个),末点无条件补标 → 合计最多 8 个
  const labelStep = Math.max(1, Math.ceil(n / 7));

  // SVG 单位 == CSS 像素,详见 useMeasuredWidth 的说明
  const [wrapRef, SVG_W] = useMeasuredWidth(1000);
  const pctOfSvg = (px: number) => (px / SVG_W) * 100;

  // 百分比定位(0~100%),让 HTML 浮层和 SVG 都按容器宽缩放
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

  /** 拆 closed 实线段 + pending 虚线段。pending 段是从最后一个 closed 有效点
   *  到 pending 点的单段连线;closed 段没有有效点时 pending 段返回空(画孤点)。 */
  function linePathParts(key: LineKey): { closed: string; pending: string } {
    const closedParts: string[] = [];
    let started = false;
    for (let i = 0; i < closedEnd; i++) {
      const v = data[i][key];
      if (v == null) {
        started = false;
        continue;
      }
      const cmd = started ? "L" : "M";
      closedParts.push(`${cmd}${xSvgAt(i).toFixed(1)},${yPxAt(v).toFixed(1)}`);
      started = true;
    }

    let pendingPath = "";
    if (pendingIdx >= 0) {
      const pV = data[pendingIdx][key];
      // 只连"紧邻 pending 的上一个 bucket"。densify 之后,如果中间是断电
      // 等长空洞,紧邻 bucket 是 null → 不画虚线(画一个 pending 点足以),
      // 避免跨 N 小时空白连一条横线让人误以为是数据。
      if (pV != null && pendingIdx > 0) {
        const prevV = data[pendingIdx - 1][key];
        if (prevV != null) {
          pendingPath = `M${xSvgAt(pendingIdx - 1).toFixed(1)},${yPxAt(prevV).toFixed(1)} L${xSvgAt(pendingIdx).toFixed(1)},${yPxAt(pV).toFixed(1)}`;
        }
      }
    }
    return { closed: closedParts.join(""), pending: pendingPath };
  }

  return (
    <div ref={wrapRef} className="relative w-full" style={{ height: H }}>
      <svg
        viewBox={`0 0 ${SVG_W} ${H}`}
        className="w-full h-full"
        preserveAspectRatio="none"
        role="img"
        aria-label={t("perf.rtfChartAria")}
      >
        {/* 无数据区域斜纹底色 — 在最底层,让 y 网格/折线浮在上面 */}
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

        {/* y 网格 */}
        {ticks.map((v) => (
          <line
            key={v}
            x1={PAD_L}
            y1={yPxAt(v)}
            x2={SVG_W - PAD_R}
            y2={yPxAt(v)}
            className={v === 1.0 ? "stroke-error" : "stroke-border"}
            strokeWidth={v === 1.0 ? "1.5" : "1"}
            strokeDasharray={v === 1.0 ? "6 6" : undefined}
            vectorEffect="non-scaling-stroke"
          />
        ))}

        {/* 折线:closed 实线 + pending 段虚线;对比线本身就是 dashed */}
        {LINES.map((l) => {
          const { closed, pending: pendingPath } = linePathParts(l.key);
          return (
            <Fragment key={l.key}>
              <path
                d={closed}
                className={l.strokeClass}
                strokeWidth={l.dashed ? "1.4" : "1.8"}
                strokeDasharray={l.dashed ? "5 4" : undefined}
                opacity={l.dashed ? 0.7 : 1}
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
              {/* pending 点单独画个小圆,提示这是仍在累积的点 */}
              {pendingIdx >= 0 && data[pendingIdx][l.key] != null && (
                <circle
                  cx={xSvgAt(pendingIdx)}
                  cy={yPxAt(data[pendingIdx][l.key] as number)}
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

      {/* y 轴标签 — HTML 浮层,不被 SVG 拉伸 */}
      {ticks.map((v) => (
        <div
          key={v}
          className={`text-caption num absolute pointer-events-none ${
            v === 1.0 ? "text-error" : "text-text-tertiary"
          }`}
          style={{
            top: yPxAt(v) - 7,
            left: 0,
            width: PAD_L - 6,
            textAlign: "right",
          }}
        >
          {v < 10 ? v.toFixed(1) : v.toFixed(0)}
        </div>
      ))}

      {/* x 轴标签 — HTML 浮层。pending 点的标签淡色 */}
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
          {LINES.map((l) => {
            const v = data[hoverIdx][l.key];
            return (
              <div key={l.key} className="flex items-center gap-1.5">
                <span
                  className={`inline-block w-2 h-2 rounded-sm ${l.legendDotClass}`}
                />
                <span className="text-text-secondary">{t(l.labelKey)}</span>
                <span className="num text-text-primary ml-auto">
                  {v == null ? "—" : v.toFixed(2)}
                </span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

