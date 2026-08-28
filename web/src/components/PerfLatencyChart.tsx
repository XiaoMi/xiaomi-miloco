/**
 * 处理耗时时间序列折线图,单位毫秒。纯 SVG,标签用 HTML 浮层避免非等比缩放拉伸字号。
 *
 * 图上三条线:
 *   ms_e2e_ok  — 仅成功 cycle 的端到端耗时 (主线,反映系统真实负载)
 *   ms_omni_ok — 仅成功 cycle 的 omni 单段耗时
 *   window_ms  — 本桶实测窗口跨度 = 「有多少时间可用」(灰虚线,参考线)
 *
 * 耗时线在参考线以下 = 处理跟得上采集;越过它 = 处理不过来,每一轮都在往后欠账。
 * 这条参考线取代了原先钉在「耗时/窗口 = 1.0」处的那条红线:判据完全相同,但两条线
 * 都是毫秒、同一个纵轴,读图不必先理解那个比值是什么。参考线逐桶取实测跨度而不是画
 * 配置标称值——跨度由帧到达决定、会围着配置值抖,画标称值会让判据比实际宽松或严苛。
 *
 * 「含失败」的两条(ms_e2e / ms_omni)与 cycle 段(ms_cycle)不画在图上,只在 hover 浮层
 * 里列出:五条线挤一张图彼此重叠、本来就分辨不出来,而它们的用处是看具体数值差
 * (omni 失败拖累耗时——超时拖长,限流拖短),逐行列数字比叠线更合用。
 *
 * 纵轴单位统一毫秒,不按量级切换秒/毫秒:同一页的阶段耗时图也是毫秒,两张图对着看
 * 时不该先做单位换算。
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
import type { PerfBucket, PerfLatencySeriesPoint } from "@/lib/types";
import { ChartGapOverlay } from "./ChartGapOverlay";

interface Props {
  state: AsyncState<PerfLatencySeriesPoint[]>;
  bucket: PerfBucket;
  windowMs: number;
  /** 嵌入「性能监测」大卡时去掉自身卡壳。 */
  embedded?: boolean;
}

const LATENCY_EMPTY = (ts: number): PerfLatencySeriesPoint => ({
  ts,
  ms_cycle: null,
  ms_e2e: null,
  ms_stream_e2e: null,
  ms_pipeline: null,
  ms_omni: null,
  ms_e2e_ok: null,
  ms_omni_ok: null,
  window_ms: null,
});

type LineKey =
  | "ms_cycle"
  | "ms_e2e"
  | "ms_omni"
  | "ms_e2e_ok"
  | "ms_omni_ok"
  | "window_ms";

interface LineDef {
  key: LineKey;
  labelKey: string;
  strokeClass: string;
  legendDotClass: string;
  /** true 时整条线画成虚线(参考线,不是被测量) */
  dashed?: boolean;
}

/**
 * 画在图上的线。只留三条,且三条的颜色必须互相分得开——颜色是这里唯一的身份编码。
 * (曾经五条同画,而 brand-primary 与 warning 在浅色档都是橙,图例里两格同色。)
 */
const CHART_LINES: LineDef[] = [
  { key: "ms_e2e_ok", labelKey: "perf.msE2eOk", strokeClass: "stroke-brand-primary", legendDotClass: "bg-brand-primary" },
  { key: "ms_omni_ok", labelKey: "perf.msOmniOk", strokeClass: "stroke-success", legendDotClass: "bg-success" },
  { key: "window_ms", labelKey: "perf.msWindow", strokeClass: "stroke-text-tertiary", legendDotClass: "bg-text-tertiary", dashed: true },
];

/**
 * hover 浮层逐行列出的量,含不画在图上的对比线。顺序即阅读顺序。
 *
 * 只有前 CHART_LINES 那几项在图上有对应的线,故只有它们配色块——色块的语义是
 * 「图上那个颜色的线」,给图上没有的项配色块只会让人去图上找一条不存在的线,
 * 而且这几项的颜色本来就与图上那三条撞(灰/蓝各撞一对)。
 */
const TOOLTIP_ROWS: LineDef[] = [
  ...CHART_LINES,
  { key: "ms_e2e", labelKey: "perf.msE2e", strokeClass: "stroke-text-tertiary", legendDotClass: "bg-text-tertiary", dashed: true },
  { key: "ms_omni", labelKey: "perf.msOmni", strokeClass: "stroke-info", legendDotClass: "bg-info", dashed: true },
  { key: "ms_cycle", labelKey: "perf.msCycle", strokeClass: "stroke-info", legendDotClass: "bg-info" },
];

/** 把步长取到 1 / 2 / 5 × 10^k。 */
function niceStep(raw: number): number {
  if (raw <= 0) return 1;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const norm = raw / mag;
  return (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10) * mag;
}

/**
 * 选 nice 刻度(毫秒),等距、全整数、含 0。返回升序数组。
 *
 * 先按「大约 5 段」定步长再往上取整到步长的倍数,而不是先定顶再等分——等分一个
 * nice 的顶会得到 nice 不了的中间刻度(0 / 1 / 2.2 / 5 这种由几何中位算出来的怪数字
 * 就是这么来的)。步长本身 nice,则每一档都 nice。
 */
function chooseYTicks(dataMax: number): number[] {
  const step = niceStep(Math.max(dataMax, 1) / 5);
  const top = Math.ceil(Math.max(dataMax, 1) / step) * step;
  const out: number[] = [];
  for (let v = 0; v <= top + step / 2; v += step) out.push(Math.round(v));
  return out;
}

export function PerfLatencyChart({ state, bucket, windowMs, embedded = false }: Props) {
  const { t } = useTranslation();
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);

  const dense = useMemo(() => {
    const raw = state.data ?? [];
    if (raw.length === 0) return raw;
    const until = Date.now();
    return densifyByBucket(raw, bucket, until - windowMs, until, LATENCY_EMPTY);
  }, [state.data, bucket, windowMs]);

  return (
    <section
      className={
        embedded
          ? ""
          : "rounded-xl bg-bg-secondary border border-border shadow-sm p-5 md:p-6"
      }
      aria-labelledby="perf-latency-title"
    >
      <div className="flex items-baseline justify-between flex-wrap gap-3 mb-4">
        <h2 id="perf-latency-title" className="text-title">
          {t("perf.latencyTitle")}
        </h2>
        {/* 单位挂在标题旁,而不是每个刻度、每行浮层都缀一遍 */}
        <span className="text-caption text-text-tertiary">{t("perf.latencyUnit")}</span>
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
            {CHART_LINES.map((l) => (
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
          {t("perf.latencyEmpty")}
        </div>
      )}
    </section>
  );
}

interface ChartProps {
  data: PerfLatencySeriesPoint[];
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

  // y 轴范围只看图上真画的那几条(含窗口参考线——它超出轴顶就等于判据看不见了)
  const allVals = data.flatMap((p) =>
    CHART_LINES.map((l) => p[l.key]).filter((v): v is number => v != null),
  );
  const dataMax = allVals.length > 0 ? Math.max(...allVals) : 1000;
  const ticks = chooseYTicks(dataMax);
  const yMax = ticks[ticks.length - 1];

  // x 轴标签密度:最多展示 7 个标签
  const labelStep = Math.max(1, Math.ceil(n / 7));

  const SVG_W = 1000;
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
    <div className="relative w-full" style={{ height: H }}>
      <svg
        viewBox={`0 0 ${SVG_W} ${H}`}
        className="w-full h-full"
        preserveAspectRatio="none"
        role="img"
        aria-label={t("perf.latencyChartAria")}
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
            className="stroke-border"
            strokeWidth="1"
            vectorEffect="non-scaling-stroke"
          />
        ))}

        {/* 折线:closed 实线 + pending 段虚线;参考线本身就是 dashed */}
        {CHART_LINES.map((l) => {
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
          className="text-caption num absolute pointer-events-none text-text-tertiary"
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
          {TOOLTIP_ROWS.map((l) => {
            const v = data[hoverIdx][l.key];
            const inChart = CHART_LINES.some((c) => c.key === l.key);
            return (
              <div key={l.key} className="flex items-center gap-1.5">
                <span
                  className={`inline-block w-2 h-2 rounded-sm ${
                    inChart ? l.legendDotClass : ""
                  }`}
                />
                <span className="text-text-secondary">{t(l.labelKey)}</span>
                <span className="num text-text-primary ml-auto">
                  {v == null ? "—" : Math.round(v)}
                </span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

