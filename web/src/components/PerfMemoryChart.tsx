/**
 * 进程内存图表。
 *
 * 三部分：
 *   1. RSS 时序折线（hover 弹 tooltip 看完整明细）
 *   2. 内存分区 top-N 表（mapping basename + RSS + count；Linux 走 smaps、macOS 走 vmmap，前端无感）
 *   3. Python top-N 类型表（module.qualname + MB + count + total 行）
 *
 * 数据：snapshotState（/monitor/memory）+ seriesState（/monitor/memory/series）。
 * 内存分区 / python_heap 段独立 `?.` 判空 —— 分区不可用时图表 + 表格降级文案；
 * python_heap 缺时类型表降级。
 *
 * SVG 模式参照 PerfLatencyChart：viewBox 横向自适应 + HTML 浮层放轴标签和 tooltip，
 * 避免 SVG preserveAspectRatio 拉伸字号。
 */

import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { TFunction } from "i18next";
import type { AsyncState } from "@/hooks/useAsync";
import { formatPerfTs } from "@/lib/perfBucket";
import type { MemorySeries, MemorySnapshot, PerfBucket } from "@/lib/types";

interface Props {
  seriesState: AsyncState<MemorySeries>;
  snapshotState: AsyncState<MemorySnapshot>;
  bucket: PerfBucket;
  windowMs: number;
  uname?: string;
}

const KB_TO_MB = (kb: number | undefined): string =>
  kb == null ? "—" : (kb / 1024).toFixed(1);

export function PerfMemoryChart({
  seriesState,
  snapshotState,
  bucket: _bucket,
  windowMs,
  uname,
}: Props) {
  const { t } = useTranslation();
  const snap = snapshotState.data;
  const series = seriesState.data;
  const hasMemoryRegions = snap?.total_rss_kb != null;

  const headerParts: string[] = [];
  if (hasMemoryRegions) {
    headerParts.push(t("perf.memHeaderRss", { mb: KB_TO_MB(snap?.total_rss_kb) }));
  }
  if (snap?.python_heap) {
    headerParts.push(
      t("perf.memHeaderPy", {
        objects: snap.python_heap.total_objects.toLocaleString(),
        mb: KB_TO_MB(snap.python_heap.total_size_kb),
      }),
    );
  }
  const headerLine =
    headerParts.length > 0
      ? headerParts.join(" · ")
      : snapshotState.loading
        ? t("perf.loading")
        : t("perf.memHeaderEmpty");

  return (
    <section
      className="rounded-xl bg-bg-secondary border border-border shadow-sm p-5 md:p-6"
      aria-labelledby="perf-memory-title"
    >
      <div className="flex items-baseline justify-between flex-wrap gap-3 mb-1">
        <h2 id="perf-memory-title" className="text-title">
          {t("perf.memTitle")}
        </h2>
        <span className="text-caption text-text-tertiary">{headerLine}</span>
      </div>
      {uname && (
        <p className="text-caption text-text-tertiary font-mono break-all mb-4">
          {uname}
        </p>
      )}
      {!uname && <div className="mb-4" />}

      {seriesState.loading && !series ? (
        <div className="h-48 flex items-center justify-center text-text-secondary">
          {t("perf.loading")}
        </div>
      ) : seriesState.error ? (
        <div className="h-48 flex items-center justify-center text-error">
          {seriesState.error.message}
        </div>
      ) : !hasMemoryRegions ? (
        <div className="h-48 flex items-center justify-center text-text-secondary">
          {t("perf.memRegionsUnavailable")}
        </div>
      ) : !series || series.points.length === 0 ? (
        <div className="h-48 flex items-center justify-center text-text-secondary">
          {t("perf.memEmptySeries")}
        </div>
      ) : (
        <MemoryChart series={series} spanMs={windowMs} t={t} />
      )}

      {snap?.categories ? (
        <MemoryRegionTable
          categories={snap.categories}
          other_rss_kb={snap.other_rss_kb ?? 0}
          other_count={snap.other_count ?? 0}
          total_rss_kb={snap.total_rss_kb ?? 0}
          t={t}
        />
      ) : (
        <p className="mt-4 text-caption text-text-tertiary">
          {snapshotState.error
            ? t("perf.memRegionsLoadFailed")
            : t("perf.memRegionsUnavailable")}
        </p>
      )}

      {snap?.python_heap ? (
        <PyHeapTable heap={snap.python_heap} t={t} />
      ) : (
        <p className="mt-4 text-caption text-text-tertiary">
          {t("perf.memPyHeapCollecting")}
        </p>
      )}
    </section>
  );
}

interface ChartProps {
  series: MemorySeries;
  spanMs: number;
  t: TFunction;
}

function MemoryChart({ series, spanMs, t }: ChartProps) {
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);

  const points = series.points;
  const n = points.length;

  const H = 240;
  const PAD_L = 50;
  const PAD_R = 16;
  const PAD_T = 12;
  const PAD_B = 28;
  const SVG_W = 1000;

  const rssVals = points.map((p) => p.rss_kb / 1024);
  const dataMax = Math.max(1, ...rssVals);
  const ticks = chooseYTicks(dataMax);
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

  const rssPath = rssVals
    .map((v, i) => `${i === 0 ? "M" : "L"}${xSvgAt(i).toFixed(1)},${yPxAt(v).toFixed(1)}`)
    .join("");

  return (
    <div className="relative w-full" style={{ height: H }}>
      <svg
        viewBox={`0 0 ${SVG_W} ${H}`}
        className="w-full h-full"
        preserveAspectRatio="none"
        role="img"
        aria-label={t("perf.memRssAria")}
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
          d={rssPath}
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
          {v < 10 ? v.toFixed(1) : v.toFixed(0)}
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
            <span className="text-text-secondary">RSS</span>
            <span className="num text-text-primary ml-auto">
              {KB_TO_MB(points[hoverIdx].rss_kb)} MB
            </span>
          </div>
        </div>
      )}
    </div>
  );
}

function chooseYTicks(dataMax: number): number[] {
  const niceTop = (() => {
    if (dataMax <= 100) return Math.ceil(dataMax / 10) * 10;
    if (dataMax <= 500) return Math.ceil(dataMax / 50) * 50;
    if (dataMax <= 1000) return Math.ceil(dataMax / 100) * 100;
    if (dataMax <= 5000) return Math.ceil(dataMax / 500) * 500;
    const mag = Math.pow(10, Math.floor(Math.log10(dataMax)));
    return Math.ceil(dataMax / mag) * mag;
  })();
  const step = niceTop / 4;
  return [0, step, step * 2, step * 3, niceTop];
}

interface MemoryRegionTableProps {
  categories: NonNullable<MemorySnapshot["categories"]>;
  other_rss_kb: number;
  other_count: number;
  total_rss_kb: number;
  t: TFunction;
}

function MemoryRegionTable({
  categories,
  other_rss_kb,
  other_count,
  total_rss_kb,
  t,
}: MemoryRegionTableProps) {
  const totalCount =
    categories.reduce((s, c) => s + c.count, 0) + other_count;
  return (
    <div className="mt-4">
      <h3 className="text-caption text-text-secondary mb-2">{t("perf.memRegionsHeading")}</h3>
      <table className="w-full text-caption table-fixed">
        <colgroup>
          <col className="w-[60%]" />
          <col className="w-[25%]" />
          <col className="w-[15%]" />
        </colgroup>
        <thead className="text-text-tertiary border-b border-border">
          <tr>
            <th className="text-left py-1">{t("perf.memColName")}</th>
            <th className="text-right py-1">{t("perf.memColMb")}</th>
            <th className="text-right py-1">{t("perf.memColCount")}</th>
          </tr>
        </thead>
        <tbody>
          {categories.map((c) => (
            <tr key={c.name} className="border-b border-border last:border-b-0">
              <td className="py-1 font-mono break-all">{c.name}</td>
              <td className="text-right py-1">{KB_TO_MB(c.rss_kb)}</td>
              <td className="text-right py-1">{c.count}</td>
            </tr>
          ))}
          {other_count > 0 && (
            <tr className="text-text-tertiary">
              <td className="py-1 italic">{t("perf.memOther")}</td>
              <td className="text-right py-1">{KB_TO_MB(other_rss_kb)}</td>
              <td className="text-right py-1">{other_count}</td>
            </tr>
          )}
          <tr className="font-medium">
            <td className="py-1">{t("perf.memTotal")}</td>
            <td className="text-right py-1">{KB_TO_MB(total_rss_kb)}</td>
            <td className="text-right py-1">{totalCount.toLocaleString()}</td>
          </tr>
        </tbody>
      </table>
    </div>
  );
}

interface PyHeapTableProps {
  heap: NonNullable<MemorySnapshot["python_heap"]>;
  t: TFunction;
}

function PyHeapTable({ heap, t }: PyHeapTableProps) {
  return (
    <div className="mt-4">
      <h3 className="text-caption text-text-secondary mb-2">
        {t("perf.memPyHeading")}
      </h3>
      <table className="w-full text-caption table-fixed">
        <colgroup>
          <col className="w-[60%]" />
          <col className="w-[25%]" />
          <col className="w-[15%]" />
        </colgroup>
        <thead className="text-text-tertiary border-b border-border">
          <tr>
            <th className="text-left py-1">{t("perf.memColName")}</th>
            <th className="text-right py-1">{t("perf.memColMb")}</th>
            <th className="text-right py-1">{t("perf.memColCount")}</th>
          </tr>
        </thead>
        <tbody>
          {heap.types.map((t) => (
            <tr key={t.qualname} className="border-b border-border last:border-b-0">
              <td className="py-1 font-mono break-all">{t.qualname}</td>
              <td className="text-right py-1">{KB_TO_MB(t.size_kb)}</td>
              <td className="text-right py-1">{t.count.toLocaleString()}</td>
            </tr>
          ))}
          {heap.other_count > 0 && (
            <tr className="text-text-tertiary">
              <td className="py-1 italic">{t("perf.memOther")}</td>
              <td className="text-right py-1">{KB_TO_MB(heap.other_size_kb)}</td>
              <td className="text-right py-1">
                {heap.other_count.toLocaleString()}
              </td>
            </tr>
          )}
          <tr className="font-medium">
            <td className="py-1">{t("perf.memTotal")}</td>
            <td className="text-right py-1">{KB_TO_MB(heap.total_size_kb)}</td>
            <td className="text-right py-1">
              {heap.total_objects.toLocaleString()}
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  );
}
