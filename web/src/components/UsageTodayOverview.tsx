/**
 * 「Token 用量」卡的左栏：总量 hero + 费用估算 + 模态构成环形图 + 横排图例。
 *
 * 周期选择器与「清空数据」已移到 UsagePage 的工具条——筛选控件该一行统管它作用的
 * 全部内容，埋在某一段里会让人为了改一个条件往回滚。
 *
 * 形态取环形而非饼：品牌语言 §7 明写「占比 2-4 项 → 环形（donut），不用饼图」，
 * 禁忌里也有「饼图 > 5 片」。环心留白还多出一个去处——悬停某模态时显示该模态读数，
 * 而不是把 hero 已经给过的总量再复读一遍（一屏只该有一个 hero 数字）。
 *
 * 图例横排在环右侧而不是竖在环下面：左栏高度因此由环决定，比竖排省约 90px；顺带
 * 整对比线上「饼 + 竖排图例」那一行还窄，窄屏反而不再溢出。
 */

import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { TokenBreakdown, UsageStats } from "@/lib/types";
import { humanTokens } from "@/lib/formatTokens";
import { costInputsByModel, summarizeCost, type UsagePricing } from "@/lib/usagePricing";
import { HelpTip } from "./HelpTip";

/** 环形图几何：viewBox 140×140，半径 54、环宽 20。 */
const R = 54;
const SW = 20;
const CIRC = 2 * Math.PI * R;
/** 扇区之间留 2.5 个单位的表面间隙——用留白分隔，而不是给色块描边。 */
const GAP = 2.5;

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
 * 模态构成。text 是 `input − video − audio` 的残差，含图片与系统提示——后端未单列
 * image 模态，所以它不是「纯文本」。四项之和 = input + output = 总量。
 */
function modalityValues(b: TokenBreakdown): Record<ModalityKey, number> {
  return {
    text: Math.max(b.input - b.video - b.audio, 0),
    video: b.video,
    audio: b.audio,
    output: b.output,
  };
}

export function UsageTodayOverview({
  stats,
  pricing,
  onOpenPricing,
}: {
  stats: UsageStats;
  pricing: UsagePricing;
  onOpenPricing: () => void;
}) {
  const { t, i18n } = useTranslation();
  const { totals } = stats;
  const [hover, setHover] = useState<ModalityKey | null>(null);

  const values = modalityValues(totals);
  const segments = MODALITIES.map((m) => ({ ...m, value: values[m.key] })).filter(
    (s) => s.value > 0,
  );
  const total = stats.total_tokens;

  // 费用按模型分别算再加总：不同供应商价目不同，一份全局单价有第二个模型时必然错。
  // 没有单价依据的模型不参与合计，而是被点名带出来——静默跳过会得到一个看着完整、
  // 实际漏算的数，而漏掉的可能正是大头。
  const { total: cost, unpriced } = summarizeCost(
    costInputsByModel(stats),
    pricing,
  );
  const money =
    pricing.currency + (cost >= 100 ? cost.toFixed(0) : cost >= 10 ? cost.toFixed(1) : cost.toFixed(2));
  /**
   * 有模型没录过价 → 金额照显示（那部分按 0 计），但把说明图标换成叹号并点名是谁。
   *
   * 为什么不另做一个「未设单价」状态把金额藏掉：藏掉等于让人无从判断「是没花钱还是算不出来」，
   * 而 0 本身就是个显然不对的数、看见就知道要去录价。
   */
  const unpricedCount = unpriced.length;

  const cacheRate = totals.input > 0 ? (totals.cache / totals.input) * 100 : null;
  const sub = [
    t("usage.callsSummary", { calls: stats.calls.toLocaleString() }),
    cacheRate != null ? t("usage.cacheHitSummary", { rate: cacheRate.toFixed(1) }) : null,
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <div>
      {/* 总量与费用：2×2 网格，数字与数字同基线、副行与副行同中线。
          为什么用网格而不是两个盒子并排——见 theme.css 的 .usage-hero-grid 注释，
          那里记着「盒高算术凑出来的对齐」实测是怎么散的。 */}
      <div className="usage-hero-grid">
        <div className="uh-hero flex items-baseline gap-2.5 flex-wrap">
          <span className="text-display-lg num text-text-primary">{humanTokens(total)}</span>
          <span className="text-title text-text-secondary font-normal">
            {t("usage.tokensUnit")}
          </span>
        </div>

        <div className="uh-cost flex items-baseline gap-1.5">
          <span className="text-caption text-text-tertiary cost-approx">≈</span>
          <span className="num text-text-secondary cost-value">{money}</span>
        </div>

        <div className="uh-sub text-caption text-text-tertiary">{sub}</div>

        <div className="uh-cap text-caption text-text-tertiary flex items-center gap-1.5 flex-wrap">
          <span>{t("usage.costEstimate")}</span>
          {/* 叹号态**只说具体问题**，不再附「按各模型单价在本机估算…」那段声明：
              声明是问号态的内容；有问题时把它一并显示只会稀释注意力。 */}
          <HelpTip
            tone={unpricedCount > 0 ? "warning" : "info"}
            text={
              unpricedCount > 0
                ? t("usage.costUnpriced", {
                    count: unpricedCount,
                    // 顿号是中文的枚举号，英文该用逗号——交给 Intl 按当前语言决定
                    models: new Intl.ListFormat(i18n.language, {
                      style: "narrow",
                      type: "unit",
                    }).format(unpriced),
                  })
                : t("usage.costHelp")
            }
            wide
          />
          <button
            type="button"
            onClick={onOpenPricing}
            className="text-caption px-2 py-0.5 rounded-md border border-border
                       text-text-secondary hover:text-text-primary hover:border-border-strong
                       transition-colors"
          >
            {t("usage.pricingOpen")}
          </button>
        </div>
      </div>

      {/* 环形图 + 横排图例 */}
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
    </div>
  );
}

function Donut({
  segments,
  total,
  hover,
  onHover,
  centerLabel,
  size = 128,
}: {
  segments: { key: ModalityKey; stroke: string; value: number }[];
  total: number;
  hover: ModalityKey | null;
  onHover: (k: ModalityKey | null) => void;
  centerLabel: { name: string; value: string; pct: string } | null;
  size?: number;
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
    <div className="relative shrink-0" style={{ width: size, height: size }}>
      <svg viewBox="0 0 140 140" width={size} height={size} className="block" aria-hidden>
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
