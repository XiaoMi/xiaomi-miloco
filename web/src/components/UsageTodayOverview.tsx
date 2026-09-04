/**
 * 「Token 用量」卡的左栏：总量 hero + 模态构成环形图 + 环右侧的竖列图例。
 *
 * 周期选择器与「清空数据」已移到 UsagePage 的工具条——筛选控件该一行统管它作用的
 * 全部内容，埋在某一段里会让人为了改一个条件往回滚。
 *
 * 形态取环形而非饼：品牌语言 §7 明写「占比 2-4 项 → 环形（donut），不用饼图」，
 * 禁忌里也有「饼图 > 5 片」。环心留白还多出一个去处——悬停某模态时显示该模态读数，
 * 而不是把 hero 已经给过的总量再复读一遍（一屏只该有一个 hero 数字）。
 *
 * 图例挪到环的右侧、与环并排（图例项自身仍是竖列）：左栏高度因此由环决定，比图例
 * 摆在环下面省约 90px；顺带整对比原先的饼图那一行还窄，窄屏反而不再溢出。
 */

import { useId, useState } from "react";
import { useTranslation } from "react-i18next";
import type { TokenBreakdown, UsageStats } from "@/lib/types";
import { humanTokens } from "@/lib/formatTokens";
import { textResidual } from "@/lib/usageTokens";
import { PERIOD_KEYS } from "@/lib/usagePeriods";

/** 环形图几何：viewBox 140×140，半径 54、环宽 20。 */
const R = 54;
const SW = 20;
const CIRC = 2 * Math.PI * R;
/** 扇区之间留 2.5 个单位的表面间隙——用留白分隔，而不是给色块描边。 */
const GAP = 2.5;
/** 环的绘制尺寸（px）。与图例并排，左栏高度由它决定。 */
const DONUT_PX = 128;

type ModalityKey = "text" | "video" | "audio" | "output";

const MODALITIES: {
  key: ModalityKey;
  labelKey: string;
  stroke: string;
  dot: string;
}[] = [
  { key: "text", labelKey: "usage.modalityText", stroke: "stroke-usage-text", dot: "bg-usage-text" },
  { key: "video", labelKey: "usage.modalityVideo", stroke: "stroke-usage-video", dot: "bg-usage-video" },
  { key: "audio", labelKey: "usage.modalityAudio", stroke: "stroke-usage-audio", dot: "bg-usage-audio" },
  { key: "output", labelKey: "usage.modalityOutput", stroke: "stroke-usage-output", dot: "bg-usage-output" },
];

/**
 * 模态构成。text 走共用的残差定义（含图片与系统提示——后端未单列 image 模态，
 * 所以它不是「纯文本」）。四项之和 = input + output = 总量。
 *
 * 残差公式不在这里再写一遍：环形图与时间分桶是同一条口径的两个消费方，
 * 各写一份就等于埋下「改一处漏一处」的隐性约定。
 */
function modalityValues(b: TokenBreakdown): Record<ModalityKey, number> {
  return {
    text: textResidual(b.input, b.video, b.audio),
    video: b.video,
    audio: b.audio,
    output: b.output,
  };
}

export function UsageTodayOverview({ stats }: { stats: UsageStats }) {
  const { t } = useTranslation();
  const headingId = useId();
  const { totals } = stats;
  const [hover, setHover] = useState<ModalityKey | null>(null);

  const values = modalityValues(totals);
  const segments = MODALITIES.map((m) => ({ ...m, value: values[m.key] })).filter(
    (s) => s.value > 0,
  );
  const total = stats.total_tokens;

  const cacheRate = totals.input > 0 ? (totals.cache / totals.input) * 100 : null;
  const sub = [
    t("usage.callsSummary", { calls: stats.calls.toLocaleString() }),
    cacheRate != null ? t("usage.cacheHitSummary", { rate: cacheRate.toFixed(1) }) : null,
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <section aria-labelledby={headingId}>
      {/* 标题只给读屏：双栏改版后周期选择器上移到卡顶工具条，画面上再挂一个二级标题
          是多余的；但它的两个兄弟（时间分布、明细）都有标题元素，这一栏若什么都不留，
          读屏按标题跳转时整块数字就是跳不到的。 */}
      <h3 id={headingId} className="sr-only">
        {t("usage.overviewHeading", { period: t(PERIOD_KEYS[stats.period]) })}
      </h3>
      {/* 总量与副行竖排，行距压到 2px：比默认行距紧，副行读起来才是上面那个数字的附注。 */}
      <div className="flex flex-col gap-0.5">
        <div className="flex items-baseline gap-2.5 flex-wrap">
          <span className="text-display-lg num text-text-primary">{humanTokens(total)}</span>
          <span className="text-title text-text-secondary font-normal">
            {t("usage.tokensUnit")}
          </span>
        </div>

        <div className="text-caption text-text-tertiary">{sub}</div>
      </div>

      {/* 环形图 + 右侧竖列图例 */}
      <div className="flex items-center gap-4 flex-wrap mt-5">
        <Donut
          segments={segments}
          total={total}
          hover={hover}
          onHover={setHover}
          centerLabel={
            hover
              ? {
                  name: t(MODALITIES.find((m) => m.key === hover)!.labelKey),
                  value: humanTokens(values[hover]),
                  pct: total > 0 ? `${((values[hover] / total) * 100).toFixed(1)}%` : "—",
                }
              : null
          }
        />
        <ul
          className="text-caption flex flex-col gap-1.5 shrink-0"
          aria-label={t("usage.legendAria")}
        >
          {segments.length === 0 ? (
            <li className="text-text-secondary">{t("usage.noUsage")}</li>
          ) : (
            segments.map((s) => (
              <li
                key={s.key}
                className="grid grid-cols-[10px_auto_1fr_auto] items-center gap-2"
                onMouseEnter={() => setHover(s.key)}
                onMouseLeave={() => setHover(null)}
              >
                <span className={`w-2.5 h-2.5 rounded-sm shrink-0 ${s.dot}`} aria-hidden />
                <span className="text-text-primary whitespace-nowrap">{t(s.labelKey)}</span>
                {/* 数值不用 text-tertiary：它在白卡上只有 2.81:1，承不住读数 */}
                <span className="num text-text-secondary text-right">{humanTokens(s.value)}</span>
                <span className="num text-text-primary text-right min-w-[46px]">
                  {total > 0 ? `${((s.value / total) * 100).toFixed(1)}%` : "—"}
                </span>
              </li>
            ))
          )}
        </ul>
      </div>
    </section>
  );
}

function Donut({
  segments,
  total,
  hover,
  onHover,
  centerLabel,
}: {
  segments: { key: ModalityKey; stroke: string; value: number }[];
  total: number;
  hover: ModalityKey | null;
  onHover: (k: ModalityKey | null) => void;
  centerLabel: { name: string; value: string; pct: string } | null;
}) {
  // 环本身是视觉产物：名称、数值、占比都在紧邻的图例里，读屏走图例即可
  const arcs: { key: ModalityKey; stroke: string; dash: string; offset: number }[] = [];
  let acc = 0;
  for (const s of segments) {
    const len = total > 0 ? (s.value / total) * CIRC : 0;
    const shown = Math.max(len - GAP, 0.4);
    arcs.push({ key: s.key, stroke: s.stroke, dash: `${shown} ${CIRC - shown}`, offset: -acc });
    acc += len;
  }

  return (
    <div className="relative shrink-0" style={{ width: DONUT_PX, height: DONUT_PX }}>
      <svg viewBox="0 0 140 140" width={DONUT_PX} height={DONUT_PX} className="block" aria-hidden>
        {arcs.length === 0 ? (
          <circle cx={70} cy={70} r={R} fill="none" strokeWidth={SW} className="stroke-bg-tertiary" />
        ) : (
          arcs.map((a) => (
            <circle
              key={a.key}
              cx={70}
              cy={70}
              r={R}
              fill="none"
              strokeWidth={SW}
              strokeDasharray={arcs.length === 1 ? undefined : a.dash}
              strokeDashoffset={arcs.length === 1 ? undefined : a.offset}
              transform="rotate(-90 70 70)"
              className={`${a.stroke} transition-opacity cursor-pointer ${
                hover && hover !== a.key ? "opacity-30" : "opacity-100"
              }`}
              onMouseEnter={() => onHover(a.key)}
              onMouseLeave={() => onHover(null)}
            />
          ))
        )}
      </svg>
      {/* 环心：默认留白，悬停时才给该模态读数——总量已由 hero 承担，复读属语义重复 */}
      {centerLabel && (
        <div
          className="absolute inset-0 flex flex-col items-center justify-center text-center px-2.5
                     pointer-events-none"
          aria-hidden
        >
          <span className="text-caption text-text-secondary leading-tight">{centerLabel.name}</span>
          <span className="text-body num text-text-primary leading-tight">{centerLabel.value}</span>
          <span className="text-caption text-text-secondary leading-tight">{centerLabel.pct}</span>
        </div>
      )}
    </div>
  );
}
