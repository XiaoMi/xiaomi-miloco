/**
 * 时间分布图。周期与粒度由 UsagePage 的工具条统一控制并传入，本组件只负责画。
 *
 * 可切两种着色：
 *  - **合计**：单序列，用中性色（`usage-total`）。刻意不取四个模态色之一，否则
 *    「总量」会被读成「文本」。
 *  - **按模态**：柱子按模态堆叠，与左栏环形图共用同一套颜色和同一份图例，于是
 *    「今天视频占一半」和「视频集中在傍晚」成了同一个色块的两个聚合层级。
 *
 * 用 CSS flex 柱而不是 SVG：SVG 要撑满宽度就得 `preserveAspectRatio="none"`，那会把
 * 圆角拉成椭圆、把 1px 网格线横向拉粗、让虚线密度随窗口宽变化。flex 天然 1:1。
 *
 * 取数路径三条都通：鼠标、触屏（pointer 事件一并覆盖）、键盘（Tab 聚焦后方向键逐段、
 * Esc 收起），并另挂一个 sr-only 的 live region 播报当前桶。此前只有 mouseenter，
 * 触屏与键盘都读不到任何数值，aria-label 又是不含数据的死字符串——整块数据对键盘与
 * 读屏用户不可达。命中判定放在整条绘图区上按最近桶算，而非逐柱 hit-box：15 分钟粒度
 * 下柱宽只剩几个像素，逐柱命中会有死区。
 */

import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
} from "react";
import { useTranslation } from "react-i18next";
import type { UsagePeriod, UsageStats, UsageTimelinePoint } from "@/lib/types";
import { axisTokens, humanTokens } from "@/lib/formatTokens";
import { Segmented } from "./Segmented";

/** 绘图区高度（px）。x 轴刻度带在它之外，不占绘图高度。 */
const PLOT_H = 168;
/** 纵轴刻度带宽度（px），柱子从这里之后开始。 */
const GUTTER = 44;
/** 峰值直标那一行的高度（12px 字 × 1.45 行高 + 4px 下留白，向上取整）。 */
const PEAK_LABEL_H = 22;
/** 单根柱最大宽度：桶少时不至于糊成一整块色。 */
const BAR_MAX = 22;
/** 非零桶的最小可见高度（占绘图高的百分比）：否则尖峰对比下会渲染成亚像素、与空桶分不开。 */
const MIN_BAR_PCT = 0.9;

type Coloring = "total" | "modality";

/** 堆叠顺序（自下而上）。与图例、环形图同序，肉眼才好对。 */
const MODALITIES = [
  { key: "text", cls: "bg-usage-text", labelKey: "usage.modalityText" },
  { key: "video", cls: "bg-usage-video", labelKey: "usage.modalityVideo" },
  { key: "audio", cls: "bg-usage-audio", labelKey: "usage.modalityAudio" },
  { key: "output", cls: "bg-usage-output", labelKey: "usage.modalityOutput" },
] as const;

const PERIOD_KEYS: Record<UsagePeriod, string> = {
  today: "usage.periodToday",
  week: "usage.periodWeek",
  month: "usage.periodMonth",
};

function formatBucketLabel(
  ts: string,
  period: UsagePeriod,
  binMinutes: number,
): string {
  const d = new Date(ts);
  if (period === "today") {
    const hh = d.getHours().toString().padStart(2, "0");
    if (binMinutes < 60) return `${hh}:${d.getMinutes().toString().padStart(2, "0")}`;
    return `${hh}h`;
  }
  return `${d.getMonth() + 1}/${d.getDate()}`;
}

/** 纵轴上限取 1/2/5×10ⁿ 的漂亮数，柱高与刻度线都以它为基准。 */
function niceCeil(v: number): number {
  if (v <= 0) return 1;
  const base = Math.pow(10, Math.floor(Math.log10(v)));
  const f = v / base;
  const nice = f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10;
  return nice * base;
}

export function UsageTimelineChart({
  stats,
  binMinutes,
  costLabelOf,
}: {
  stats: UsageStats;
  binMinutes: number;
  /** 可选：给某个桶算一句费用估算（由上层注入，图表本身不关心单价）。 */
  costLabelOf?: (p: UsageTimelinePoint) => string | null;
}) {
  const { t } = useTranslation();
  // 用 stats.period（数据自带）而非外部选中值，避免切周期时数据未到位却用新周期
  // 格式化横轴导致的瞬态错渲染。
  const period = stats.period;
  const data = stats.timeline;

  const [coloring, setColoring] = useState<Coloring>("modality");
  const [activeIdx, setActiveIdx] = useState<number | null>(null);
  useEffect(() => setActiveIdx(null), [period, binMinutes]);

  const barsRef = useRef<HTMLDivElement | null>(null);
  const [plotW, setPlotW] = useState(0);
  // 横轴标签密度按**实际像素宽**算，不能只按桶数抽稀，否则窄容器下相邻标签会压字
  useLayoutEffect(() => {
    const node = barsRef.current;
    if (!node) return;
    const measure = () => setPlotW(node.clientWidth);
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(measure);
    ro.observe(node);
    return () => ro.disconnect();
  }, []);

  const n = data.length;
  const max = data.reduce((m, d) => Math.max(m, d.tokens), 0);
  const niceMax = niceCeil(max);
  const peakIdx = data.reduce((m, d, i) => (d.tokens > data[m].tokens ? i : m), 0);

  const pick = useCallback(
    (i: number | null) => {
      setActiveIdx(i == null ? null : Math.min(n - 1, Math.max(0, i)));
    },
    [n],
  );

  /**
   * 这次聚焦是指针带来的吗。
   *
   * 整条绘图区既可聚焦又收指针事件（命中按最近桶算，不用逐柱 hit-box——细粒度下柱宽
   * 只剩几像素、逐柱会有死区），于是「点一下」同时点着两条读数入口，而它们抢同一个
   * activeIdx。浏览器在 mousedown 上授予焦点，而 mousedown 排在 pointerdown 之后，
   * 不闸住的话 onFocus 会把刚命中的桶改写成峰值桶。触屏更糟：兼容鼠标事件攒到抬指
   * 之后才补发，排在 pointerleave 后面，「点一下」的净效果是
   * pick(命中) → pick(null) → pick(峰值)，而触屏没有 pointermove 来纠正。
   */
  const fromPointer = useRef(false);

  const onPointer = (e: ReactPointerEvent<HTMLDivElement>) => {
    const node = barsRef.current;
    if (!node || n === 0) return;
    const r = node.getBoundingClientRect();
    fromPointer.current = true;
    pick(Math.floor(((e.clientX - r.left) / r.width) * n));
  };

  const onKeyDown = (e: ReactKeyboardEvent<HTMLDivElement>) => {
    const base = activeIdx ?? peakIdx;
    switch (e.key) {
      case "ArrowRight":
      case "ArrowUp":
        pick(base + 1);
        break;
      case "ArrowLeft":
      case "ArrowDown":
        pick(base - 1);
        break;
      case "Home":
        pick(0);
        break;
      case "End":
        pick(n - 1);
        break;
      case "Escape":
        pick(null);
        return;
      default:
        return;
    }
    e.preventDefault();
  };

  // 全周期无用量：给一句空态，而不是画空图配「0 / 1 / 1」的无意义纵轴
  const isEmpty = max <= 0 || n === 0;
  const peak = isEmpty ? null : data[peakIdx];
  const active = activeIdx != null ? data[activeIdx] : null;

  return (
    <section aria-labelledby="usage-timeline-title">
      <div className="flex items-center justify-between flex-wrap gap-3 mb-3">
        <h3 id="usage-timeline-title" className="text-body font-semibold text-text-primary">
          {t("usage.timelineTitle")}
        </h3>
        {!isEmpty && (
          <Segmented
            ariaLabel={t("usage.coloringAria")}
            value={coloring}
            onChange={setColoring}
            options={[
              { key: "total" as Coloring, label: t("usage.coloringTotal") },
              { key: "modality" as Coloring, label: t("usage.coloringModality") },
            ]}
          />
        )}
      </div>

      {isEmpty || !peak ? (
        <div
          className="flex items-center justify-center text-caption text-text-secondary
                     border border-dashed border-border rounded-lg px-4 text-center"
          style={{ height: PLOT_H, marginLeft: GUTTER }}
        >
          {t("usage.timelineEmpty")}
        </div>
      ) : (
        <div className="relative">
          {/* 网格线：实线 hairline。虚线在图表里读作「预测 / 阈值」，而这只是网格。 */}
          <div
            className="absolute right-0 top-0 pointer-events-none"
            style={{ left: GUTTER, height: PLOT_H }}
            aria-hidden
          >
            {[0, 0.5, 1].map((r) => (
              <div
                key={r}
                className={`absolute left-0 right-0 h-px ${
                  r === 0 ? "bg-border-strong" : "bg-border"
                }`}
                style={{ top: PLOT_H - r * PLOT_H }}
              />
            ))}
          </div>

          {/* 纵轴刻度：与网格线同一个 top 换算，数字与线对齐到同一像素行 */}
          {[0, 0.5, 1].map((r) => (
            <div
              key={r}
              className="absolute left-0 text-caption num text-text-secondary text-right pr-1.5
                         -translate-y-1/2 pointer-events-none"
              style={{ width: GUTTER, top: PLOT_H - r * PLOT_H }}
              aria-hidden
            >
              {axisTokens(niceMax * r)}
            </div>
          ))}

          {/* 柱：整条绘图区一个 pointermove 做就近命中，避免细柱下的死区 */}
          <div
            ref={barsRef}
            tabIndex={0}
            role="img"
            aria-label={t("usage.chartAriaLabel", {
              period: t(PERIOD_KEYS[period]),
              bins: n,
              peakAt: formatBucketLabel(peak.ts, period, binMinutes),
              peakValue: humanTokens(peak.tokens),
            })}
            onPointerMove={onPointer}
            onPointerDown={onPointer}
            // 只有鼠标真的移出去才清空：触屏抬指后必发 pointerleave，
            // 而那一桶正是用户要读的数，且没有第二次机会把它选回来。
            onPointerLeave={(e) => {
              if (e.pointerType !== "mouse") return;
              fromPointer.current = false;
              pick(null);
            }}
            // 指针带来的焦点：命中的桶已经算好了，别覆盖。只有 Tab 进来
            // （此时没有任何指针交互跑过）才落到峰值桶当键盘起点。
            onFocus={() => {
              if (fromPointer.current) return;
              pick(peakIdx);
            }}
            onBlur={() => {
              fromPointer.current = false;
              pick(null);
            }}
            onKeyDown={onKeyDown}
            className="relative flex items-end rounded outline-none
                       focus-visible:outline focus-visible:outline-2
                       focus-visible:outline-offset-2 focus-visible:outline-brand-primary"
            style={{ height: PLOT_H, marginLeft: GUTTER }}
          >
            {data.map((d, i) => {
              const dim = activeIdx != null && activeIdx !== i;
              const pct =
                d.tokens > 0 ? Math.max((d.tokens / niceMax) * 100, MIN_BAR_PCT) : 0;
              return (
                <div
                  key={d.ts}
                  className={`flex-1 h-full flex items-end justify-center transition-opacity ${
                    dim ? "opacity-40" : ""
                  }`}
                >
                  {/* 强调用「其余变淡」而不是「当前变亮」：亮度方向在浅/深两个主题下
                      相反，而变淡在两边都成立，不必按主题分叉。 */}
                  <div
                    className={`w-[calc(100%-2px)] rounded-t-[3px] ${
                      coloring === "modality"
                        ? "flex flex-col justify-end gap-px overflow-hidden"
                        : "bg-usage-total"
                    }`}
                    style={{ height: `${pct}%`, maxWidth: BAR_MAX }}
                  >
                    {coloring === "modality" &&
                      // 自上而下渲染 → 视觉自下而上堆叠，与图例顺序一致
                      [...MODALITIES].reverse().map((m) =>
                        d[m.key] > 0 ? (
                          <span
                            key={m.key}
                            className={`block w-full first:rounded-t-[3px] ${m.cls}`}
                            style={{ flex: `${d[m.key]} 0 0` }}
                          />
                        ) : null,
                      )}
                  </div>
                </div>
              );
            })}
          </div>

          {/* 峰值直标：选择性直标，只标极值——每根都标就没人看了 */}
          <div
            className="absolute text-caption num text-text-secondary whitespace-nowrap
                       pointer-events-none"
            style={{
              left: `calc(${GUTTER}px + (100% - ${GUTTER}px) * ${(peakIdx + 0.5) / n})`,
              // 下钳一行的高度：柱贴着纵轴上界时柱顶只剩几像素，不钳这行字会顶到
              // 标题行上去。这里用常数是安全的——它只有一行固定文案，高度不随内容变，
              // 与那个行数会变的浮层不是一回事。
              top: Math.max(PLOT_H - (peak.tokens / niceMax) * PLOT_H, PEAK_LABEL_H),
              transform: "translate(-50%, -100%)",
              paddingBottom: 4,
            }}
            aria-hidden
          >
            {t("usage.peakLabel", { value: humanTokens(peak.tokens) })}
          </div>

          <AxisLabels data={data} period={period} binMinutes={binMinutes} plotW={plotW} />

          {active && (
            <Tooltip
              point={active}
              idx={activeIdx!}
              total={n}
              niceMax={niceMax}
              period={period}
              binMinutes={binMinutes}
              stacked={coloring === "modality"}
              costLabel={costLabelOf?.(active) ?? null}
            />
          )}

          {/* 读屏播报：浮层本身是视觉产物（aria-hidden），当前桶的数值走这里，
              键盘逐段移动时才有得念。 */}
          <p className="sr-only" role="status">
            {active
              ? t("usage.bucketReadout", {
                  at: formatBucketLabel(active.ts, period, binMinutes),
                  value: humanTokens(active.tokens),
                })
              : ""}
          </p>
        </div>
      )}
    </section>
  );
}

/**
 * 横轴刻度。密度按「可用像素 ÷ 标签宽」定，而不是按桶数固定抽稀——后者在窄容器下
 * 会让相邻标签互相压字（默认 1 小时视图在所有手机宽度下 00h 与 02h 就已重叠）。
 * 末桶标签始终画；倒数第二个若落在一个 step 之内就跳过，避免和它挤在一起。
 */
function AxisLabels({
  data,
  period,
  binMinutes,
  plotW,
}: {
  data: UsageTimelinePoint[];
  period: UsagePeriod;
  binMinutes: number;
  plotW: number;
}) {
  const n = data.length;
  if (n === 0) return null;
  const sample = formatBucketLabel(data[0].ts, period, binMinutes);
  // "13:20" 比 "13h" 宽；按字符数粗估即可，只要能随格式变化就够
  const labelPx = sample.length > 3 ? 46 : 32;
  const maxLabels = Math.max(2, Math.floor((plotW || 600) / labelPx));
  const step = Math.max(1, Math.ceil(n / maxLabels));

  const idxs: number[] = [];
  for (let i = 0; i < n; i += step) {
    if (i !== 0 && n - 1 - i < step) continue;
    idxs.push(i);
  }
  if (idxs[idxs.length - 1] !== n - 1) idxs.push(n - 1);

  return (
    <div className="relative h-[18px] mt-1.5" style={{ marginLeft: GUTTER }} aria-hidden>
      {idxs.map((i) => {
        const first = i === 0;
        const last = i === n - 1;
        return (
          <span
            key={data[i].ts}
            className="absolute top-0 text-caption num text-text-secondary whitespace-nowrap"
            style={
              first
                ? { left: 0 }
                : last
                  ? { right: 0 }
                  : { left: `${((i + 0.5) / n) * 100}%`, transform: "translateX(-50%)" }
            }
          >
            {formatBucketLabel(data[i].ts, period, binMinutes)}
          </span>
        );
      })}
    </div>
  );
}

/** 值在前、名在后（读者已经知道在看哪根柱，想要的是数）。序列用短线 key 而非填充方块。 */
function Tooltip({
  point,
  idx,
  total,
  niceMax,
  period,
  binMinutes,
  stacked,
  costLabel,
}: {
  point: UsageTimelinePoint;
  idx: number;
  total: number;
  niceMax: number;
  period: UsagePeriod;
  binMinutes: number;
  stacked: boolean;
  costLabel: string | null;
}) {
  const { t } = useTranslation();
  const centerPct = ((idx + 0.5) / total) * 100;
  // 贴边时改同侧对齐，否则首/末桶的浮层会越出卡片圆角边框
  const align = centerPct < 12 ? "left" : centerPct > 88 ? "right" : "center";
  const shiftX = align === "center" ? "-50%" : align === "right" ? "-100%" : "0";

  /**
   * 纵向落点按**实测高度**算，不用常数猜。
   *
   * 浮层的行数随「按模态 / 有没有费用行」变化，高度差两三倍；而柱越高浮层越靠上，
   * 拿一个固定地板去挡越界，只在「浮层恰好那么高」时成立——峰值柱贴着纵轴上界时
   * 柱顶只剩几像素，浮层会整个翻到绘图区上方、盖住卡片工具条上住户刚点过的控件。
   * 故先量出来：上方装不下就翻到柱顶下方，并夹在绘图区内——「按模态」把四个模态全列出时
   * 浮层比绘图区本身还高，不夹的话往下翻会越出绘图区、去盖下面的明细表。宁可盖住它正在
   * 解读的那张图，也不去盖邻区：图是浮层的上下文，而明细与工具条不是。
   */
  const boxRef = useRef<HTMLDivElement | null>(null);
  const [boxH, setBoxH] = useState(0);
  useLayoutEffect(() => {
    setBoxH(boxRef.current?.offsetHeight ?? 0);
  }, [point, stacked, costLabel]);

  const barTop = PLOT_H - (point.tokens / niceMax) * PLOT_H;
  const GAP = 8;
  const above = barTop - boxH - GAP;
  const top =
    above >= 0 ? above : Math.min(barTop + GAP, Math.max(0, PLOT_H - boxH));

  return (
    <div
      ref={boxRef}
      aria-hidden
      className="absolute z-10 pointer-events-none whitespace-nowrap text-caption
                 rounded-lg bg-bg-secondary border border-border shadow-md px-2.5 py-1.5"
      style={{
        left:
          align === "left"
            ? GUTTER
            : align === "right"
              ? "100%"
              : `calc(${GUTTER}px + (100% - ${GUTTER}px) * ${(idx + 0.5) / total})`,
        top,
        // 纵向已按实测高度算完，这里只做横向对齐
        transform: `translate(${shiftX}, 0)`,
        // 量到高度之前先隐形渲一帧，避免看到从默认位置跳过来
        visibility: boxH === 0 ? "hidden" : undefined,
      }}
    >
      <div className="num text-text-secondary mb-1">
        {formatBucketLabel(point.ts, period, binMinutes)}
      </div>
      <div className="flex items-baseline justify-between gap-3.5 pb-1 mb-1 border-b border-border">
        <span className="num font-semibold text-text-primary">{humanTokens(point.tokens)}</span>
        <span className="text-text-secondary">{t("usage.tokensUnit")}</span>
      </div>
      {costLabel && (
        <div className="flex items-baseline justify-between gap-3.5">
          <span className="num font-semibold text-text-primary">{costLabel}</span>
          <span className="text-text-secondary">{t("usage.costEstimate")}</span>
        </div>
      )}
      {stacked &&
        MODALITIES.map((m) =>
          point[m.key] > 0 ? (
            <div
              key={m.key}
              className="grid grid-cols-[12px_1fr_auto] items-center gap-2 leading-6"
            >
              <span className={`block h-0.5 rounded-full ${m.cls}`} />
              <span className="text-text-secondary">{t(m.labelKey)}</span>
              <span className="num font-semibold text-text-primary">
                {humanTokens(point[m.key])}
              </span>
            </div>
          ) : null,
        )}
    </div>
  );
}
