/**
 * 明细表:按**模型**一行(合并实时/用户两类调用),列出 调用次数 / 各模态 token / 费用估算。
 * 数据来自 backend omni 计费(cache/video/audio 均为 input 的子集)。
 *
 * 费用按行算而不是只给一个总数:单价是按模型存的,多模型时只有落到行上才对得起账。
 *
 * 口径说明从表下的一行小字改成标题旁的「?」——那句话是「看界面看不出来」的背景知识,
 * 需要时点开即可,常驻一行反而占位。
 */

import { useTranslation } from "react-i18next";
import type { TokenBreakdown, UsageStats } from "@/lib/types";
import { humanTokens } from "@/lib/formatTokens";
import { costInputOf, estimateCost, pricingFor, type UsagePricing } from "@/lib/usagePricing";
import { HelpTip } from "./HelpTip";

interface ModelRow {
  model: string;
  calls: number;
  breakdown: TokenBreakdown;
}

/** 把 model×type 明细行按模型合并(累加调用数与各模态),模型名升序。 */
function rowsByModel(stats: UsageStats): ModelRow[] {
  const byModel = new Map<string, ModelRow>();
  for (const r of stats.rows) {
    let m = byModel.get(r.model);
    if (!m) {
      m = {
        model: r.model,
        calls: 0,
        breakdown: { input: 0, output: 0, cache: 0, video: 0, audio: 0 },
      };
      byModel.set(r.model, m);
    }
    m.calls += r.calls;
    m.breakdown.input += r.breakdown.input;
    m.breakdown.output += r.breakdown.output;
    m.breakdown.cache += r.breakdown.cache;
    m.breakdown.video += r.breakdown.video;
    m.breakdown.audio += r.breakdown.audio;
  }
  return [...byModel.values()].sort((a, b) =>
    a.model < b.model ? -1 : a.model > b.model ? 1 : 0,
  );
}

export function UsageBreakdownTable({
  stats,
  pricing,
}: {
  stats: UsageStats;
  pricing: UsagePricing;
}) {
  const { t } = useTranslation();
  const rows = rowsByModel(stats);
  const money = (v: number) =>
    pricing.currency + (v >= 100 ? v.toFixed(0) : v >= 10 ? v.toFixed(1) : v.toFixed(2));

  const cols = [
    { key: "colModel", align: "left" as const },
    { key: "colCalls", align: "right" as const },
    { key: "colInput", align: "right" as const },
    { key: "colOutput", align: "right" as const },
    { key: "colCache", align: "right" as const },
    { key: "colCacheHitRate", align: "right" as const },
    { key: "colVideo", align: "right" as const },
    { key: "colAudio", align: "right" as const },
    { key: "colCost", align: "right" as const },
  ];

  return (
    <section aria-labelledby="usage-breakdown-title">
      <h3
        id="usage-breakdown-title"
        className="text-body font-semibold text-text-primary mb-3 flex items-center gap-1.5"
      >
        {t("usage.breakdownTitle")}
        <HelpTip text={t("usage.breakdownNote")} wide />
      </h3>

      {/* 横向可滚区要能被键盘聚焦并用方向键滚动，否则 200% 缩放下被 overflow 藏起来的
          列对键盘用户不可达（表内全是纯文本，没有任何可聚焦后代把滚动带出来）。 */}
      <div
        className="text-caption overflow-x-auto -mx-5 md:-mx-6 focus-visible:outline
                   focus-visible:outline-2 focus-visible:outline-offset-[-2px]
                   focus-visible:outline-brand-primary"
        tabIndex={0}
        role="region"
        aria-labelledby="usage-breakdown-title"
      >
        <table className="w-full whitespace-nowrap">
          <thead>
            <tr className="text-text-secondary border-b border-border">
              {cols.map((c, i) => (
                <th
                  key={c.key}
                  scope="col"
                  className={`py-2 font-normal ${
                    c.align === "left" ? "text-left" : "text-right num"
                  } ${i === 0 || i === cols.length - 1 ? "px-5 md:px-6" : "px-3"}`}
                >
                  {t(`usage.${c.key}`)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 ? (
              <tr>
                <td
                  colSpan={cols.length}
                  className="px-5 md:px-6 py-6 text-center text-text-secondary"
                >
                  {t("usage.noUsageData")}
                </td>
              </tr>
            ) : (
              rows.map((r) => {
                const cost = estimateCost(
                  costInputOf(r.breakdown),
                  pricingFor(pricing, r.model),
                  pricing.per,
                ).total;
                return (
                  <tr key={r.model} className="border-b border-border last:border-b-0">
                    <td className="px-5 md:px-6 py-2.5 text-text-primary">{r.model}</td>
                    <td className="px-3 py-2.5 text-right num text-text-secondary">
                      {r.calls.toLocaleString()}
                    </td>
                    <td className="px-3 py-2.5 text-right num text-text-primary">
                      {humanTokens(r.breakdown.input)}
                    </td>
                    <td className="px-3 py-2.5 text-right num text-text-primary">
                      {humanTokens(r.breakdown.output)}
                    </td>
                    {/* 数值列不用 text-tertiary：白卡上 2.81:1，承不住读数 */}
                    <td className="px-3 py-2.5 text-right num text-text-secondary">
                      {humanTokens(r.breakdown.cache)}
                    </td>
                    <td className="px-3 py-2.5 text-right num text-text-secondary">
                      {r.breakdown.input > 0
                        ? `${((r.breakdown.cache / r.breakdown.input) * 100).toFixed(1)}%`
                        : "—"}
                    </td>
                    <td className="px-3 py-2.5 text-right num text-text-secondary">
                      {humanTokens(r.breakdown.video)}
                    </td>
                    <td className="px-3 py-2.5 text-right num text-text-secondary">
                      {humanTokens(r.breakdown.audio)}
                    </td>
                    <td className="px-5 md:px-6 py-2.5 text-right num text-text-primary">
                      ≈ {money(cost)}
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}
